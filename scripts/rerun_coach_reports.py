"""重新生成失敗的教練報告（M9 task 5.2）。

`coach_reports.generated_at` 為 NULL 且 `failure_reason` 有值，代表這場
的報告從未成功生成過——可能是模型呼叫本身失敗，也可能是使用者點擊時
訊號還沒就緒（`failure_reason="signals_not_ready"`）。這支腳本撈出這些
場次重試；若訊號本身也失敗，需先用 rerun_signals.py 補齊，再跑這支。

跟 rerun_signals.py 是同一種腳本：不需要使用者再互動一次，直接拿資料庫
裡已存的訊號重新呼叫 coaching 路徑。daily-coach-report-link 之後報告只在
回饋網頁上呈現，這裡**只產生並保存**，不再私訊送出；使用者打開連結就會看到。
產生的邏輯與網頁共用 app/dialog/report_service.py。

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

from app.config import get_settings  # noqa: E402
from app.db import repository as repo  # noqa: E402
from app.db.models import Session as SessionModel  # noqa: E402
from app.db.session import db_session, init_db  # noqa: E402
from app.dialog.report_service import (  # noqa: E402
    ANALYSIS_FAILED,
    ANALYSIS_PENDING,
    GENERATION_FAILED,
    MISSING,
    READY,
    generate_session_report,
)
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


async def _rerun_one(router, session_id: int) -> str:
    """回傳這場的處理結果描述。"""
    result = await generate_session_report(router, session_id)
    return {
        READY: "已產生（使用者打開回饋連結就會看到）",
        MISSING: "不存在，略過",
        ANALYSIS_PENDING: "行為分析尚未完成，請稍後再試",
        ANALYSIS_FAILED: "行為分析失敗，請先跑 rerun_signals.py",
        GENERATION_FAILED: "產生仍失敗，見 log",
    }[result.status]


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
    failures = 0
    try:
        for session_id in session_ids:
            outcome = await _rerun_one(router, session_id)
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
