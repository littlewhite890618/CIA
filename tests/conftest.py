"""共用 fixture。

`pg`：給標記為 postgres 的測試使用（migrate-to-postgres-and-containerize
決策 4）。只在設定 TEST_DATABASE_URL 時才有意義，用法見
tests/test_postgres_smoke.py 開頭。

⚠️ TEST_DATABASE_URL 指向的資料庫會被整個清空（DROP SCHEMA public）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")

requires_postgres = pytest.mark.skipif(not TEST_DATABASE_URL, reason="未設定 TEST_DATABASE_URL")

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def pg(monkeypatch):
    """清空測試資料庫並套用全部遷移，回傳 sessionmaker（engine 掛在 .kw['bind']）。"""
    from alembic import command
    from alembic.config import Config

    from app.config import get_settings

    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    # alembic/env.py 從 app.config 讀連線字串，必須讓它看到測試資料庫
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")

    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    engine.dispose()
    get_settings.cache_clear()
