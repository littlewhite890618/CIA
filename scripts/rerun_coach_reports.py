"""重新生成失敗的教練報告（M9 task 5.2）。

`coach_reports.generated_at` 為 NULL 且 `failure_reason` 有值，代表這場
的報告從未成功生成過——可能是模型呼叫本身失敗，也可能是使用者點擊時
訊號還沒就緒（`failure_reason="signals_not_ready"`）。這支腳本撈出這些
場次重試；若訊號本身也失敗，需先用 rerun_signals.py 補齊，再跑這支。

跟 rerun_signals.py 是同一種腳本：不需要使用者再互動一次，直接拿資料庫
裡已存的訊號重新呼叫 coaching 路徑。生成成功後**立即嘗試送達**——與
webhook 觸發的路徑不同，這裡沒有「點擊當下的 session」可以拿來當送達
視窗依據，改用該使用者＋角色目前的 UserPersonaLink 查 igsid，
並用該場次自己的 session_id 做送達（若視窗已逾期，直接送就會失敗，
保留為已生成未送達，等使用者下次互動時由 5.1 的補送邏輯接手，
不是這支腳本的責任）。

用法（在專案根目錄執行）：

    .venv/Scripts/python.exe scripts/rerun_coach_reports.py              # 全部重跑
    .venv/Scripts/python.exe scripts/rerun_coach_reports.py --list       # 只列出，不重跑
    .venv/Scripts/python.exe scripts/rerun_coach_reports.py --session 26 # 指定某場
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.channels.instagram import InstagramClient  # noqa: E402
from app.channels.rate_limit import RateLimiter  # noqa: E402
from app.channels.sender import MessageSender  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import repository as repo  # noqa: E402
from app.db.models import Persona  # noqa: E402
from app.db.models import Session as SessionModel  # noqa: E402
from app.db.models import SignalStatus  # noqa: E402
from app.db.session import db_session, init_db  # noqa: E402
from app.dialog.coach_report import format_report_text, generate_report  # noqa: E402
from app.personas.coaching_settings import session_coaching  # noqa: E402
from app.llm import build_router  # noqa: E402
from app.logging_config import setup_logging  # noqa: E402


def _target_sessions(explicit: list[int] | None) -> list[int]:
    with db_session() as db:
        if explicit:
            return explicit
        return repo.failed_coach_report_session_ids(db)


def _describe(session_id: int) -> str:
    with db_session() as db:
        s = db.get(SessionModel, session_id)
        report = repo.get_coach_report(db, session_id)
        if s is None:
            return f"session {session_id}：不存在"
        reason = report.failure_reason if report else None
        return f"session {session_id}：signal_status={s.signal_status} failure_reason={reason}"


async def _rerun_one(router, session_id: int, rate_limiter: RateLimiter) -> str:
    """回傳這場的處理結果描述。"""
    with db_session() as db:
        session = db.get(SessionModel, session_id)
        if session is None:
            return "不存在，略過"
        if session.signal_status != SignalStatus.OK:
            return f"訊號尚未就緒（{session.signal_status}），請先跑 rerun_signals.py"

        signals = repo.signals_for_session(db, session_id)
        persona = db.get(Persona, session.persona_id)
        persona_name = persona.name if persona else "角色"
        link = repo.get_link_by_session(db, session)
        igsid = link.igsid if link else None
        persona_id = session.persona_id
        coaching = session_coaching(db, session)

    result = await generate_report(router, signals, persona_name, coaching)
    with db_session() as db:
        report = repo.get_or_create_coach_report(db, session_id)
        if not result.ok or result.content is None:
            repo.mark_report_failed(db, report, result.error or "unknown")
            return f"生成仍失敗：{result.error}"
        repo.mark_report_generated(db, report, result.content)

    if igsid is None:
        return "生成成功，但找不到 igsid，無法送達，留待下次使用者互動時補送"

    text = format_report_text(result.content)
    client = InstagramClient(get_settings().ig_access_token, get_settings().ig_account_id)
    sender = MessageSender(client, rate_limiter)
    try:
        outcome = await sender.send_reply(
            session_id=session_id, persona_id=persona_id, igsid=igsid, text=text
        )
    finally:
        await client.aclose()

    if outcome.ok:
        with db_session() as db:
            fresh = repo.get_coach_report(db, session_id)
            if fresh is not None:
                repo.mark_report_delivered(db, fresh)
        return "生成成功並已送達"
    return f"生成成功，送達失敗（{outcome.blocked_reason}），保留待補送"


async def _rerun(session_ids: list[int]) -> int:
    settings = get_settings()
    missing_keys = settings.missing_llm_keys()
    if missing_keys:
        print(f"LLM 金鑰未設定：{missing_keys}，無法重跑")
        return 1

    router = build_router(
        settings.llm_path_configs,
        api_keys=settings.llm_api_keys,
        timeout=float(settings.llm_timeout_seconds),
        max_retries=settings.llm_max_retries,
        retry_base_seconds=settings.llm_retry_base_seconds,
        fallback_path_configs=settings.llm_fallback_path_configs,
    )
    rate_limiter = RateLimiter(settings.send_rate_limit_per_hour)
    failures = 0
    try:
        for session_id in session_ids:
            outcome = await _rerun_one(router, session_id, rate_limiter)
            print(f"session {session_id}：{outcome}")
            if "失敗" in outcome or "不存在" in outcome:
                failures += 1
    finally:
        await router.aclose()
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="重新生成失敗的教練報告（M9 task 5.2）")
    parser.add_argument("--session", type=int, action="append", help="指定場次編號，可重複")
    parser.add_argument("--list", action="store_true", help="只列出目標場次，不重跑")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level, settings.environment)
    init_db()

    session_ids = _target_sessions(args.session)
    if not session_ids:
        print("沒有需要重跑的報告")
        return 0

    for session_id in session_ids:
        print(_describe(session_id))
    if args.list:
        return 0

    return asyncio.run(_rerun(session_ids))


if __name__ == "__main__":
    sys.exit(main())
