"""Postgres 冒煙測試（migrate-to-postgres-and-containerize 決策 4）。

其餘測試都跑 SQLite；這組專門抓兩種資料庫行為不同的地方——字串長度
限制、時區、布林值、唯一約束與交易回復。只在設定 TEST_DATABASE_URL
時執行，否則整個檔案略過，不讓平常跑測試也得先起資料庫。

⚠️ TEST_DATABASE_URL 指向的資料庫會被整個清空（DROP SCHEMA public），
務必使用專門給測試的資料庫，不要指向正式資料。

    docker run -d --name cia-pg-test -e POSTGRES_USER=cia -e POSTGRES_PASSWORD=test \
        -e POSTGRES_DB=cia_test -p 127.0.0.1:55433:5432 postgres:16
    $env:TEST_DATABASE_URL = "postgresql+psycopg://cia:test@127.0.0.1:55433/cia_test"
    pytest -m postgres
"""

from __future__ import annotations

import importlib
from datetime import timedelta, timezone

import pytest

from app.db import repository as repo
from app.db.models import Session as SessionModel
from app.db.models import SessionState, utcnow
from tests.conftest import requires_postgres

pytestmark = [pytest.mark.postgres, requires_postgres]


def test_遷移後init_db通過啟動檢查(pg):
    import app.db.session as session_module

    importlib.reload(session_module)
    session_module.init_db()  # 版本落後或連不上會拋 DatabaseNotReady
    assert session_module.check_connection() is True


def test_收訊息_去重_靜默結束(pg):
    Session = pg

    with Session() as db:
        persona = repo.upsert_persona(
            db, slug="sunny", name="晴晴", gender="female", ig_account_id="17841400000001"
        )
        user, _link, _ = repo.resolve_user(
            db, persona_id=persona.id, igsid="IGSID_PG", ig_username="pg_user"
        )
        session, is_new = repo.get_or_create_session(db, user.id, persona.id)
        assert is_new
        repo.touch_session_by_user_activity(db, session)
        first = repo.save_inbound_message(
            db, session=session, platform_message_id="mid_pg_1", content="嗨"
        )
        assert first is not None
        db.commit()
        session_id = session.id

    # Meta 重送同一則：Postgres 的唯一約束擋下，且回復後交易仍可繼續使用
    with Session() as db:
        session = db.get(SessionModel, session_id)
        dup = repo.save_inbound_message(
            db, session=session, platform_message_id="mid_pg_1", content="嗨"
        )
        assert dup is None
        session = db.get(SessionModel, session_id)
        assert session.message_count == 1

    # 時間欄位讀回帶 UTC 時區（SQLite 讀回是無時區的）
    with Session() as db:
        session = db.get(SessionModel, session_id)
        assert session.last_message_at.tzinfo is not None
        assert session.last_message_at.utcoffset() == timedelta(0)
        assert session.window_expires_at.tzinfo is not None

        session.last_message_at = utcnow() - timedelta(seconds=2000)
        db.commit()

    with Session() as db:
        silent = repo.find_silent_sessions(db, threshold_seconds=1500)
        assert [s.id for s in silent] == [session_id]
        repo.end_session(db, silent[0])
        db.commit()

    with Session() as db:
        session = db.get(SessionModel, session_id)
        assert session.state == SessionState.ENDED
        assert session.ended_at.tzinfo is not None
        assert session.ended_at.astimezone(timezone.utc) <= utcnow()
        assert repo.find_silent_sessions(db, threshold_seconds=1500) == []


def test_字串超過欄位長度時Postgres拒絕寫入(pg):
    """SQLite 不檢查 VARCHAR(n)，Postgres 會——轉檔腳本的預檢就是為了這個。"""
    from sqlalchemy.exc import DataError

    Session = pg
    with Session() as db, pytest.raises(DataError):
        repo.upsert_persona(
            db, slug="x" * 65, name="太長", gender="female", ig_account_id="1"
        )
