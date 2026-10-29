"""產生並保存單段對話的教練報告（daily-coach-report-link 任務 6.2，design.md 決策 5）。

回饋網頁打開時，前端對每一段「可產生」的對話依序呼叫這裡；scripts/rerun_coach_reports.py
補產生失敗的報告也走這裡，兩邊的行為一致。

- 已產生的直接回傳，不再呼叫模型（spec「再次打開」）
- 行為分析還沒完成或失敗：不以不完整的資料產生（spec「行為分析尚未完成」）
- 產生失敗只記原因，不留半份內容；下次呼叫會再試（spec「重新整理後重試」）
- 同一段對話同時被觸發兩次（例如快速重新整理）：以行程內的鎖排隊，後到的
  拿到前一個產生好的結果，不會呼叫兩次模型（單一 worker，見 Dockerfile）
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.db import repository as repo
from app.db import session as session_module
from app.db.models import CoachReport, Persona, SignalStatus
from app.db.models import Session as SessionModel
from app.dialog.coach_report import generate_report
from app.llm.router import LLMRouter
from app.logging_config import get_logger
from app.personas.coaching_settings import session_coaching

log = get_logger(__name__)

READY = "ready"  # 報告已產生
ANALYSIS_PENDING = "analysis_pending"  # 行為分析尚未完成
ANALYSIS_FAILED = "analysis_failed"  # 行為分析失敗，待 scripts/rerun_signals.py
GENERATION_FAILED = "generation_failed"  # 模型產生失敗，可重試
NOT_GENERATED = "not_generated"  # 分析已完成、還沒產生（打開網頁時才產生）
MISSING = "missing"  # 場次不存在

_locks: dict[int, asyncio.Lock] = {}


@dataclass(frozen=True)
class SessionReport:
    status: str
    content: dict[str, str] | None = None


def report_status(session: SessionModel, report: CoachReport | None) -> str:
    """不呼叫模型，只看目前狀態（網頁列出對話清單時用）。"""
    if report is not None and report.generated_at is not None:
        return READY
    if session.signal_status == SignalStatus.FAILED:
        return ANALYSIS_FAILED
    if session.signal_status != SignalStatus.OK:
        return ANALYSIS_PENDING
    if report is not None and report.failure_reason:
        return GENERATION_FAILED
    return NOT_GENERATED


async def generate_session_report(router: LLMRouter | None, session_id: int) -> SessionReport:
    lock = _locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        with session_module.db_session() as db:
            session = db.get(SessionModel, session_id)
            if session is None:
                return SessionReport(MISSING)
            report = repo.get_coach_report(db, session_id)
            status = report_status(session, report)
            if status == READY:
                return SessionReport(READY, report.content)
            if status in (ANALYSIS_PENDING, ANALYSIS_FAILED):
                return SessionReport(status)
            signals = repo.signals_for_session(db, session_id)
            for s in signals:
                db.expunge(s)
            persona = db.get(Persona, session.persona_id)
            persona_name = persona.name if persona else "角色"
            coaching = session_coaching(db, session)

        if router is None:
            log.warning("LLM 未設定，無法產生教練報告", extra={"session_id": session_id})
            return SessionReport(GENERATION_FAILED)

        result = await generate_report(router, signals, persona_name, coaching)

        with session_module.db_session() as db:
            report = repo.get_or_create_coach_report(db, session_id)
            if result.ok and result.content is not None:
                repo.mark_report_generated(db, report, result.content)
                log.info("教練報告已產生", extra={"session_id": session_id})
                return SessionReport(READY, result.content)
            repo.mark_report_failed(db, report, result.error or "unknown")
            log.warning("教練報告產生失敗", extra={"session_id": session_id, "error": result.error})
            return SessionReport(GENERATION_FAILED)
