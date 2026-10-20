"""建立第一個管理員帳號（add-persona-authoring-console 任務 5.7）。

系統剛上線時沒有任何帳號，無法從管理員頁面建立第一個管理員，
所以只有這一次需要用指令。之後的管理員與客戶帳號一律從管理員頁面建立；
已經有管理員時，這支指令會拒絕執行。

用法（容器內執行，資料庫用容器的連線設定）：

    docker compose exec app python scripts/create_admin.py <登入帳號>

不用 Docker、在主機上直接執行：

    .venv/Scripts/python.exe scripts/create_admin.py <登入帳號>

密碼由系統產生，只在這裡印出一次，請立即保存。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import accounts  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import db_session, init_db  # noqa: E402
from app.logging_config import setup_logging  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建立第一個管理員帳號")
    parser.add_argument("username", help="登入帳號（3～64 個英數字元，可含 _ . @ -）")
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level, settings.environment)
    init_db()

    with db_session() as db:
        if accounts.has_admin(db):
            print("已經有管理員了。請登入管理員頁面新增其他管理員，不要用這支指令。", file=sys.stderr)
            return 1
        try:
            account, password = accounts.create_admin(db, args.username)
        except accounts.AccountError as exc:
            print(f"無法建立：{exc}", file=sys.stderr)
            return 1
        username = account.username

    print(f"已建立管理員：{username}")
    print(f"密碼：{password}")
    print("密碼只會顯示這一次，請立即保存。登入後台網址：<對外網址>/login")
    return 0


if __name__ == "__main__":
    sys.exit(main())
