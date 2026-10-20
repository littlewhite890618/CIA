"""重跑訊號偵測（task 8.4）。

session 結束時的訊號偵測若失敗（模型錯誤、逾時、輸出格式壞掉），該場次
會被標成 signal_status = failed。這支腳本撈出那些場次，拿資料庫裡已存的
對話再呼叫一次 analysis 路徑重新偵測，不需要使用者再聊一次。

每重跑一場就多一次 analysis 呼叫，會用到模型額度。

用法（在專案根目錄執行）：

    .venv/Scripts/python.exe scripts/rerun_signals.py              # 重跑所有 failed 的場次
    .venv/Scripts/python.exe scripts/rerun_signals.py --list       # 只列出，不重跑
    .venv/Scripts/python.exe scripts/rerun_signals.py --session 26 # 指定某場（不論狀態）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 讓腳本能從專案根目錄以外的位置執行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.db import repository as repo  # noqa: E402
from app.db.models import Session as SessionModel  # noqa: E402
from app.db.models import SessionState  # noqa: E402
from app.db.session import db_session, init_db  # noqa: E402
from app.dialog.signal_job import run_signal_detection  # noqa: E402
from app.llm import build_router  # noqa: E402
from app.logging_config import setup_logging  # noqa: E402
from app.personas.loader import load_all_cards  # noqa: E402


def _target_sessions(explicit: list[int] | None) -> list[int]:
    with db_session() as db:
        if explicit:
            return explicit
        return repo.failed_signal_session_ids(db)


def _describe(session_id: int) -> str:
    with db_session() as db:
        s = db.get(SessionModel, session_id)
        if s is None:
            return f"session {session_id}：不存在"
        return (
            f"session {session_id}：state={s.state} signal_status={s.signal_status} "
            f"error={(s.signal_error or '')[:120]}"
        )


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
    cards = load_all_cards()
    failures = 0
    try:
        for session_id in session_ids:
            with db_session() as db:
                s = db.get(SessionModel, session_id)
                state = s.state if s else None
            if state is None:
                print(f"session {session_id}：不存在，略過")
                failures += 1
                continue
            if state != SessionState.ENDED:
                # 還在進行中的場次，結束時會自動偵測，現在跑只會拿到不完整的對話
                print(f"session {session_id}：尚未結束，略過")
                continue

            result = await run_signal_detection(
                router,
                session_id,
                cards,
                burst_window_seconds=settings.signal_burst_window_seconds,
                burst_min_messages=settings.signal_burst_min_messages,
            )
            if result is not None and result.ok:
                print(f"session {session_id}：成功，{len(result.signals)} 筆訊號")
            else:
                failures += 1
                error = result.error if result else "未知錯誤"
                print(f"session {session_id}：仍然失敗，{error}")
    finally:
        await router.aclose()
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="重跑訊號偵測（task 8.4）")
    parser.add_argument("--session", type=int, action="append", help="指定場次編號，可重複")
    parser.add_argument("--list", action="store_true", help="只列出目標場次，不重跑")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level, settings.environment)
    init_db()

    session_ids = _target_sessions(args.session)
    if not session_ids:
        print("沒有需要重跑的場次")
        return 0

    for session_id in session_ids:
        print(_describe(session_id))
    if args.list:
        return 0

    return asyncio.run(_rerun(session_ids))


if __name__ == "__main__":
    sys.exit(main())
