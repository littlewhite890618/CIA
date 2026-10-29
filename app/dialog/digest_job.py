"""每日教練回饋連結的送出（daily-coach-report-link 第 5 群，design.md 決策 1～3）。

對話靜默結束後不再馬上發邀請。每天固定時刻（預設台灣時間 04:00）把「已結束、
還沒處理過」的對話依使用者分組，每位使用者送一則附連結的訊息；連結打開後才
產生報告（見 app/web/report_api.py）。

- 「下一個 04:00」不靠時間區間判斷，而是「沒處理過的都撈」：凌晨 02:00 結束的
  對話自然會在當天 04:00 被撈到
- 服務在 04:00 停機：啟動時以「上一個 04:00」為界補跑一次，只撈那之前就結束、
  卻沒被處理的對話；之後才結束的照常等下一個 04:00，不會在部署當下提早送出
- 互動未達下限的對話記下略過原因，不再每天重複掃描
- 送出用這位使用者最新一段對話的 24 小時視窗（MessageSender 的前置檢查），
  訊息標記為系統訊息，不進入對話上下文

發訊與連結網址由呼叫端（app/main.py）注入，這裡只管「撈誰、怎麼分組、記錄結果」，
方便以假的發訊函式測試。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.channels.sender import SendOutcome
from app.db import repository as repo
from app.db import session as session_module
from app.db.models import Session as SessionModel
from app.db.models import SystemMessageKind
from app.logging_config import get_logger

log = get_logger(__name__)

SKIP_TOO_FEW_MESSAGES = "too_few_messages"
SKIP_NO_LINK = "no_user_link"


def digest_message_text(url: str, ttl_days: int) -> str:
    """凌晨送出的訊息文案。實作時暫定，與使用者確認後再調（design.md Open Questions）。"""
    return (
        "幫你整理了上次聊天的小回饋，點這裡看看：\n"
        f"{url}\n"
        f"（{ttl_days} 天內有效，只能在你打開它的那個裝置上看）"
    )


# ── 排程時刻 ──────────────────────────────────────


def _scheduled_on(day: datetime, at: str) -> datetime:
    hour, minute = (int(x) for x in at.split(":"))
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def last_run_at(now: datetime, *, at: str, tz_name: str) -> datetime:
    """不晚於 now 的最近一個排定時刻（UTC）。"""
    tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    today = _scheduled_on(local, at)
    run = today if today <= local else today - timedelta(days=1)
    return run.astimezone(timezone.utc)


def next_run_at(now: datetime, *, at: str, tz_name: str) -> datetime:
    """晚於 now 的下一個排定時刻（UTC）。"""
    tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    today = _scheduled_on(local, at)
    run = today if today > local else today + timedelta(days=1)
    return run.astimezone(timezone.utc)


# ── 執行一次 ──────────────────────────────────────

SendFn = Callable[[int, int, str, str, dict], Awaitable[SendOutcome]]
"""send(session_id, persona_id, igsid, text, meta) → SendOutcome"""


@dataclass
class DigestRunResult:
    sent: list[int] = field(default_factory=list)  # 送出的每日回饋 id
    not_sent: dict[int, str] = field(default_factory=dict)  # 每日回饋 id → 原因
    skipped_sessions: dict[int, str] = field(default_factory=dict)  # 場次 id → 原因


async def run_digest(
    before: datetime,
    *,
    send: SendFn,
    url_for: Callable[[int], str],
    min_user_messages: int,
    ttl_days: int,
) -> DigestRunResult:
    """把 before 之前結束、還沒處理過的對話，依使用者各送一則回饋連結。"""
    result = DigestRunResult()
    groups: dict[int, list[int]] = {}

    with session_module.db_session() as db:
        for session in repo.pending_digest_sessions(db, before):
            link = repo.get_link_by_session(db, session)
            if link is None:
                repo.mark_invite_skipped(db, session, reason=SKIP_NO_LINK)
                result.skipped_sessions[session.id] = SKIP_NO_LINK
                continue
            if repo.inbound_chat_count(db, session.id) < min_user_messages:
                repo.mark_invite_skipped(db, session, reason=SKIP_TOO_FEW_MESSAGES)
                result.skipped_sessions[session.id] = SKIP_TOO_FEW_MESSAGES
                continue
            groups.setdefault(link.id, []).append(session.id)

    for link_id, session_ids in groups.items():
        # 每位使用者各自一個交易：先把場次掛上每日回饋（掛上即視為已處理），
        # 送不出去也不會在下次被重送
        with session_module.db_session() as db:
            link = repo.get_link_by_id(db, link_id)
            sessions = [db.get(SessionModel, sid) for sid in session_ids]
            digest = repo.create_digest(db, link, sessions)
            latest = repo.latest_session_for_link(db, link)
            digest_id, latest_id = digest.id, latest.id
            persona_id, igsid = link.persona_id, link.igsid

        outcome = await send(
            latest_id,
            persona_id,
            igsid,
            digest_message_text(url_for(digest_id), ttl_days),
            {"system": SystemMessageKind.COACH_DIGEST},
        )

        with session_module.db_session() as db:
            digest = repo.get_digest(db, digest_id)
            if outcome.ok:
                repo.mark_digest_sent(db, digest)
                result.sent.append(digest_id)
            else:
                reason = outcome.blocked_reason or "send_failed"
                repo.mark_digest_skipped(db, digest, reason)
                result.not_sent[digest_id] = reason

    log.info(
        "每日教練回饋送出完成",
        extra={
            "before": before.isoformat(),
            "sent": len(result.sent),
            "not_sent": result.not_sent,
            "skipped_sessions": len(result.skipped_sessions),
        },
    )
    return result
