"""訊號偵測的完整流程：讀對話 → 偵測 → 落庫 → 記錄狀態（tasks 8.3、8.4）。

session 結束時由 main._close_session_and_analyze 呼叫，事後重跑由
scripts/rerun_signals.py 呼叫，兩邊共用這一份，避免重跑的行為與
正常流程分岔。

自己開短生命週期的 db_session：中間夾著一次 LLM 呼叫，不該讓資料庫
連線在等待網路 I/O 時一直被佔用著（同 main 的話題萃取）。
"""

from __future__ import annotations

from app.db import repository as repo
from app.db.models import Persona, SignalStatus
from app.db.models import Session as SessionModel
from app.db.session import db_session
from app.dialog.signals import DetectionResult, detect_signals
from app.llm.router import LLMRouter
from app.logging_config import get_logger
from app.personas.coaching_settings import session_coaching
from app.personas.schema import PersonaCard
from app.personas.user_versions import resolve_card

log = get_logger(__name__)

# 一場對話撈多少則。200 則遠超過 POC 展示規模，只是給個實務上限。
MAX_MESSAGES = 200


async def run_signal_detection(
    router: LLMRouter,
    session_id: int,
    persona_cards: dict[str, PersonaCard],
    *,
    burst_window_seconds: int,
    burst_min_messages: int,
) -> DetectionResult | None:
    """偵測一場 session 並落庫。場次不存在回傳 None。

    成功：以這次結果取代該場全部訊號，狀態標 ok。
    失敗：不動既有訊號，狀態標 failed 並記下原因，留待重跑。
    """
    with db_session() as db:
        session = db.get(SessionModel, session_id)
        if session is None:
            return None
        history = repo.recent_messages(db, session_id, MAX_MESSAGES)
        persona = db.get(Persona, session.persona_id)
        slug = persona.slug if persona else None
        session_number = repo.session_number(db, session)

        # design.md 決策 13：邊界依使用者版本判定。角色依使用者設的邊界反應，
        # 偵測標準也要一致，否則會出現「角色沒反應、報告卻說他踩雷」。
        # 偵測在 session 結束時才跑，用的是當下的版本——對話中途改設定的
        # 使用者，整場會以最新版本判定。
        default_card = persona_cards.get(slug) if slug else None
        link = repo.get_link_by_session(db, session)
        card = resolve_card(db, default_card, link) if default_card is not None else None
        coaching = session_coaching(db, session)

    if card is None:
        result = DetectionResult(ok=False, signals=[], error=f"找不到人設卡：{slug}")
    else:
        result = await detect_signals(
            router,
            history,
            card.boundaries,
            session_number=session_number,
            burst_window_seconds=burst_window_seconds,
            burst_min_messages=burst_min_messages,
            coaching=coaching,
        )

    with db_session() as db:
        if result.ok:
            repo.replace_session_signals(
                db,
                session_id,
                [
                    {
                        "message_id": s.message_id,
                        "signal_type": s.signal_type,
                        "severity": s.severity,
                        "dimension": s.dimension,
                        "reason": s.reason,
                    }
                    for s in result.signals
                ],
            )
            repo.set_signal_status(db, session_id, SignalStatus.OK)
        else:
            repo.set_signal_status(db, session_id, SignalStatus.FAILED, result.error)

    if result.ok:
        log.info(
            "訊號偵測完成",
            extra={
                "session_id": session_id,
                "count": len(result.signals),
                "signals": [
                    {"message_id": s.message_id, "item": s.item, "reason": s.reason}
                    for s in result.signals
                ],
            },
        )
    else:
        log.warning(
            "訊號偵測失敗，已標記待重跑",
            extra={"session_id": session_id, "error": result.error},
        )
    return result
