"""轉檔腳本測試（migrate-to-postgres-and-containerize tasks 6.1–6.7）。

來源是測試內建立的小型 SQLite，目的是 TEST_DATABASE_URL 指向的 Postgres。
未設定 TEST_DATABASE_URL 時整個檔案略過（見 tests/conftest.py）。
"""

from __future__ import annotations

import hashlib
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import repository as repo
from app.db.models import Base, Message, RealtimeSignal, User
from app.db.models import Session as SessionModel
from tests.conftest import ROOT, TEST_DATABASE_URL, requires_postgres

sys.path.insert(0, str(ROOT / "scripts"))
import migrate_sqlite_to_postgres as mig  # noqa: E402

pytestmark = [pytest.mark.postgres, requires_postgres]


@pytest.fixture
def source(tmp_path) -> Path:
    """一個「POC 跑過一陣子」的 SQLite：兩位使用者、兩場對話、訊號、報告、速率紀錄。"""
    path = tmp_path / "poc.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with Session() as db:
        sunny = repo.upsert_persona(
            db, slug="sunny", name="晴晴", gender="female", ig_account_id="17841400000001"
        )
        for n in (1, 2):
            user, link, _ = repo.resolve_user(
                db, persona_id=sunny.id, igsid=f"IGSID_{n}", ig_username=f"user_{n}"
            )
            link.topic_memory = ["咖啡", f"話題{n}"]
            session, _ = repo.get_or_create_session(db, user.id, sunny.id)
            msgs = [
                repo.save_inbound_message(
                    db, session=session, platform_message_id=f"mid_{n}_{i}", content=f"第{i}則"
                )
                for i in range(3)
            ]
            repo.replace_session_signals(db, session.id, [
                {"message_id": msgs[1].id, "signal_type": "landmine", "dimension": "分寸與界線"},
            ])
            report = repo.get_or_create_coach_report(db, session.id)
            repo.mark_report_generated(db, report, {"摘要": "聊得不錯"})
            repo.end_session(db, session)
        repo.bump_rate_budget(db, persona_id=sunny.id, sent=3)
        db.commit()

    engine.dispose()
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_完整搬移_筆數與關聯一致_來源不變(source, pg):
    before = _sha256(source)
    lines: list[str] = []

    mig.migrate(source, TEST_DATABASE_URL, out=lines.append)

    assert _sha256(source) == before, "來源檔案不得被修改"
    assert any("messages" in line and line.split()[-2:] == ["6", "6"] for line in lines)
    assert any("關聯一致：realtime_signals.message_id" in line for line in lines)

    with pg() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 6
        signal = db.scalars(select(RealtimeSignal)).first()
        assert db.get(Message, signal.message_id).content == "第1則"
        link = db.get(User, 1).links[0]
        assert link.topic_memory == ["咖啡", "話題1"]
        session = db.get(SessionModel, 1)
        assert session.invite_sent is False
        # 無時區的舊時間值一律視為 UTC
        assert session.started_at.utcoffset() == timedelta(0)
        assert session.ended_at is not None


def test_轉檔後新資料的id不與既有資料衝突(source, pg):
    mig.migrate(source, TEST_DATABASE_URL, out=lambda _: None)

    with pg() as db:
        max_user = db.scalar(select(func.max(User.id)))
        max_message = db.scalar(select(func.max(Message.id)))

        user, _link, _ = repo.resolve_user(db, persona_id=1, igsid="IGSID_NEW", ig_username="new")
        session, _ = repo.get_or_create_session(db, user.id, 1)
        msg = repo.save_inbound_message(
            db, session=session, platform_message_id="mid_new", content="新的一則"
        )
        db.commit()

    assert user.id > max_user
    assert msg.id > max_message


def test_目的資料庫已有資料時拒絕執行(source, pg):
    with pg() as db:
        repo.upsert_persona(db, slug="sunny", name="晴晴", gender="female", ig_account_id="x")
        db.commit()

    with pytest.raises(mig.MigrationRefused, match="已有資料"):
        mig.migrate(source, TEST_DATABASE_URL, out=lambda _: None)


def test_中途失敗時整批回復_目的資料庫維持空的(source, pg, monkeypatch):
    real_verify = mig._verify

    def _fail_after_writes(conn, data, out):
        real_verify(conn, data, out)
        raise RuntimeError("模擬寫入後才發生的錯誤")

    monkeypatch.setattr(mig, "_verify", _fail_after_writes)

    with pytest.raises(RuntimeError, match="模擬"):
        mig.migrate(source, TEST_DATABASE_URL, out=lambda _: None)

    with pg() as db:
        for table in Base.metadata.sorted_tables:
            assert db.scalar(select(func.count()).select_from(table)) == 0, table.name


def test_資料預檢_超長字串與NUL字元會列出後停止(source, pg):
    import sqlite3

    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE sessions SET invite_skip_reason = ? WHERE id = 1", ("x" * 40,))
        conn.execute("UPDATE messages SET content = ? WHERE id = 2", ("壞\x00掉",))

    with pytest.raises(mig.MigrationRefused) as exc:
        mig.migrate(source, TEST_DATABASE_URL, out=lambda _: None)

    message = str(exc.value)
    assert "sessions id=1 欄位 invite_skip_reason 長度 40 超過上限 32" in message
    assert "messages id=2 欄位 content 含 NUL 字元" in message

    with pg() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 0


def test_目的資料庫未套用遷移時拒絕執行(source, pg):
    from sqlalchemy import text

    with pg() as db:
        db.execute(text("DELETE FROM alembic_version"))
        db.commit()

    with pytest.raises(mig.MigrationRefused, match="alembic upgrade head"):
        mig.migrate(source, TEST_DATABASE_URL, out=lambda _: None)
