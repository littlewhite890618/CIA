"""每日回饋的資料層（daily-coach-report-link 第 3 群，design.md 決策 1、3）。

撈出待送場次的規則、互動數的計算、建立每日回饋並掛上場次，以及遷移 0008
（含把改版前的舊場次標記為已處理）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import repository as repo
from app.db.models import Base, CoachDigest, Session, SessionState, SystemMessageKind, utcnow
from tests.conftest import ROOT, TEST_DATABASE_URL, requires_postgres


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session


def _user(db, igsid: str):
    persona = repo.upsert_persona(db, slug="sunny", name="晴晴", gender="female", ig_account_id="IGA")
    _user, link, _ = repo.resolve_user(db, persona_id=persona.id, igsid=igsid, ig_username=igsid)
    return link


def _ended_session(db, link, *, minutes_ago: int, inbound: int = 2) -> Session:
    """建立一段已結束的對話：使用者傳 inbound 則訊息，minutes_ago 分鐘前結束。"""
    session, _ = repo.get_or_create_session(db, link.user_id, link.persona_id)
    for i in range(inbound):
        repo.save_inbound_message(db, session=session, platform_message_id=f"{session.id}-{i}", content=f"第{i}句")
    repo.end_session(db, session)
    session.ended_at = utcnow() - timedelta(minutes=minutes_ago)
    db.flush()
    return session


# ═══════════════════════════════════════════════════════════
#  撈出待送的場次
# ═══════════════════════════════════════════════════════════


def test_撈出已結束且未處理的場次依結束時間排序(db):
    link = _user(db, "U1")
    later = _ended_session(db, link, minutes_ago=30)
    earlier = _ended_session(db, link, minutes_ago=120)
    assert [s.id for s in repo.pending_digest_sessions(db, utcnow())] == [earlier.id, later.id]


def test_進行中或在送出時刻之後才結束的不撈(db):
    link = _user(db, "U2")
    repo.get_or_create_session(db, link.user_id, link.persona_id)  # 進行中
    assert repo.pending_digest_sessions(db, utcnow()) == []

    ended = _ended_session(db, link, minutes_ago=5)  # 結束同一段對話
    assert repo.pending_digest_sessions(db, utcnow() - timedelta(minutes=10)) == []
    assert [s.id for s in repo.pending_digest_sessions(db, utcnow())] == [ended.id]


def test_已處理過的場次不再撈(db):
    link = _user(db, "U3")
    in_digest = _ended_session(db, link, minutes_ago=60)
    old_button = _ended_session(db, link, minutes_ago=50)
    skipped = _ended_session(db, link, minutes_ago=40)
    fresh = _ended_session(db, link, minutes_ago=30)

    repo.create_digest(db, link, [in_digest])
    old_button.invite_sent = True  # 改版前已用按鈕邀請過
    repo.mark_invite_skipped(db, skipped, reason="too_few_messages")

    assert [s.id for s in repo.pending_digest_sessions(db, utcnow())] == [fresh.id]


def test_互動數不算關鍵字這類系統訊息(db):
    link = _user(db, "U4")
    session = _ended_session(db, link, minutes_ago=10, inbound=1)
    repo.save_inbound_message(
        db, session=session, platform_message_id="kw", content="芝麻開門",
        meta={"system": SystemMessageKind.SETTINGS_LINK},
    )
    assert repo.inbound_chat_count(db, session.id) == 1


# ═══════════════════════════════════════════════════════════
#  建立與讀取每日回饋
# ═══════════════════════════════════════════════════════════


def test_建立每日回饋並依開始時間列出場次(db):
    link = _user(db, "U5")
    first = _ended_session(db, link, minutes_ago=120)
    second = _ended_session(db, link, minutes_ago=30)

    digest = repo.create_digest(db, link, [second, first])
    assert [s.id for s in repo.digest_sessions(db, digest.id)] == [first.id, second.id]
    assert digest.sent_at is None and digest.skip_reason is None

    repo.mark_digest_skipped(db, digest, "messaging_window_expired")
    assert digest.skip_reason == "messaging_window_expired"
    repo.mark_digest_sent(db, digest)
    assert digest.sent_at is not None and digest.skip_reason is None


def test_最新一段對話用來送出(db):
    link = _user(db, "U6")
    _ended_session(db, link, minutes_ago=120)
    latest = _ended_session(db, link, minutes_ago=30)
    assert repo.latest_session_for_link(db, link).id == latest.id


# ═══════════════════════════════════════════════════════════
#  遷移 0008（Postgres）
# ═══════════════════════════════════════════════════════════


@pytest.mark.postgres
@requires_postgres
def test_遷移0008把改版前未處理的舊場次標記為已處理(pg):
    """否則第一次每日送出會把幾週前的對話一起寄給使用者。"""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect, text

    cfg = Config(str(ROOT / "alembic.ini"))
    command.downgrade(cfg, "0007")
    engine = pg.kw["bind"]
    assert "coach_digests" not in inspect(engine).get_table_names()

    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO personas (slug, name, gender, ig_account_id, source, status, created_at) "
            "VALUES ('sunny', '晴晴', 'female', '17841400000001', 'human', 'online', now())"
        ))
        conn.execute(text("INSERT INTO users (ig_username, identity_confirmed, created_at) VALUES ('u', true, now())"))
        for state, sent, reason in [("ended", False, None), ("ended", True, None),
                                    ("ended", False, "too_few_messages"), ("active", False, None)]:
            conn.execute(text(
                "INSERT INTO sessions (user_id, persona_id, started_at, last_message_at, invite_sent, "
                "invite_skip_reason, message_count, state) "
                "SELECT u.id, p.id, now(), now(), :sent, :reason, 0, :state FROM users u, personas p"
            ), {"sent": sent, "reason": reason, "state": state})

    command.upgrade(cfg, "head")

    with pg() as db:
        ended_unsent = db.query(Session).filter(
            Session.state == SessionState.ENDED, Session.invite_sent.is_(False)
        )
        assert sorted(s.invite_skip_reason for s in ended_unsent) == ["before_daily_digest", "too_few_messages"]
        assert db.query(Session).filter(Session.state == SessionState.ACTIVE).one().invite_skip_reason is None
        assert db.query(Session).filter(Session.invite_sent.is_(True)).one().invite_skip_reason is None
        assert db.query(CoachDigest).count() == 0

    command.downgrade(cfg, "0007")
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT count(*) FROM sessions WHERE invite_skip_reason = 'before_daily_digest'"
        )).scalar() == 0
    command.upgrade(cfg, "head")
