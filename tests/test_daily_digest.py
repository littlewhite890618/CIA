"""每日教練回饋連結（daily-coach-report-link 第 5 群，design.md 決策 1～3、6）。

走真的 webhook 管線：對話靜默結束後不發任何回饋訊息；到了排定時刻把結束的
對話依使用者合併成一則附連結的訊息；24 小時視窗逾期與互動不足都記錄原因；
點擊改版前的舊按鈕改回覆連結。發訊 API 換成假的（見 tests/test_coach_report.py）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import CoachDigest, Message, MessageDirection, SystemMessageKind, utcnow
from app.db.models import Session as SessionModel
from app.dialog.coach_report import INVITE_BUTTON_TITLE, INVITE_PAYLOAD_PREFIX
from app.dialog.digest_job import last_run_at, next_run_at
from tests.test_coach_report import (  # noqa: F401  (make_client 為 fixture)
    _backdate_session,
    _post,
    _post_postback,
    make_client,
)

TAIPEI = timezone(timedelta(hours=8))


def _tw(y, mo, d, h, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=TAIPEI)


# ═══════════════════════════════════════════════════════════
#  排定時刻（任務 5.2）
# ═══════════════════════════════════════════════════════════

AT = dict(at="04:00", tz_name="Asia/Taipei")


@pytest.mark.parametrize(
    ("ended", "expected"),
    [
        (_tw(2026, 10, 21, 23, 0), _tw(2026, 10, 22, 4, 0)),  # 晚上聊完 → 隔天 04:00
        (_tw(2026, 10, 22, 2, 0), _tw(2026, 10, 22, 4, 0)),  # 半夜聊完 → 當天 04:00
        (_tw(2026, 10, 22, 4, 0), _tw(2026, 10, 23, 4, 0)),  # 剛好 04:00 → 下一個
    ],
)
def test_下一個送出時刻(ended, expected):
    assert next_run_at(ended, **AT) == expected


def test_上一個送出時刻用於啟動補跑():
    assert last_run_at(_tw(2026, 10, 22, 15, 0), **AT) == _tw(2026, 10, 22, 4, 0)
    assert last_run_at(_tw(2026, 10, 22, 3, 0), **AT) == _tw(2026, 10, 21, 4, 0)


def test_下一個送出時刻一定在24小時視窗內():
    """使用者最後一則訊息後至少 25 分鐘對話才結束，下一個 04:00 一定在 24 小時內。"""
    for hour in range(24):
        last_message = _tw(2026, 10, 22, hour, 30)
        run = next_run_at(last_message + timedelta(minutes=25), **AT)
        assert run - last_message < timedelta(hours=24)


# ═══════════════════════════════════════════════════════════
#  流程（任務 5.1、5.3）
# ═══════════════════════════════════════════════════════════


def _chat_and_end(client, igsid: str, texts: list[str], start: int = 1) -> int:
    for i, text in enumerate(texts, start):
        _post(client, f"{igsid}_{i}", text, igsid)
    session_id = _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())
    return session_id


def _outbound(session_ids: list[int]) -> list[Message]:
    from app.db.session import db_session

    with db_session() as db:
        rows = (
            db.query(Message)
            .filter(Message.session_id.in_(session_ids), Message.direction == MessageDirection.OUTBOUND)
            .order_by(Message.id)
            .all()
        )
        for m in rows:
            db.expunge(m)
        return rows


def _digest_messages(session_ids: list[int]) -> list[Message]:
    return [m for m in _outbound(session_ids) if (m.meta or {}).get("system") == SystemMessageKind.COACH_DIGEST]


def _run(client) -> None:
    asyncio.run(client._main_module._run_digest(utcnow()))


def test_對話結束當下不送任何回饋訊息(make_client):
    client = make_client(links_enabled=True)
    session_id = _chat_and_end(client, "IGSID_D1", ["我今天心情不太好", "想找人聊聊"])

    texts = [m.content for m in _outbound([session_id])]
    assert not any("/report?t=" in t for t in texts)
    assert not any("回饋" in t for t in texts)


def test_多段對話只送一則一個連結(make_client):
    client = make_client(links_enabled=True)
    first = _chat_and_end(client, "IGSID_D2", ["我今天心情不太好", "想找人聊聊"])
    second = _chat_and_end(client, "IGSID_D2", ["晚上又來了", "還是睡不著"], start=3)
    assert first != second

    _run(client)

    sent = _digest_messages([first, second])
    assert len(sent) == 1
    assert sent[0].content.count("/report?t=") == 1
    assert sent[0].session_id == second  # 用最新一段對話的視窗送出

    from app.db.session import db_session

    with db_session() as db:
        digest = db.query(CoachDigest).one()
        assert digest.sent_at is not None
        assert {s.coach_digest_id for s in db.query(SessionModel)} == {digest.id}

    _run(client)  # 再跑一次：已送過的不重送
    assert len(_digest_messages([first, second])) == 1


def test_回饋訊息不進入之後的對話上下文(make_client):
    client = make_client(links_enabled=True)
    session_id = _chat_and_end(client, "IGSID_D3", ["我今天心情不太好", "想找人聊聊"])
    _run(client)
    assert _digest_messages([session_id])

    _post(client, "IGSID_D3_next", "早安", "IGSID_D3")
    sent_to_model = [m.content for m in client._main_module.llm_router.provider_for("dialog").calls[-1]["messages"]]
    assert not any("/report?t=" in c for c in sent_to_model)


def test_互動不足的對話不列入也不送(make_client):
    client = make_client(links_enabled=True)
    session_id = _chat_and_end(client, "IGSID_D4", ["嗨"])  # 門檻為 2
    _run(client)

    assert _digest_messages([session_id]) == []
    from app.db.session import db_session

    with db_session() as db:
        assert db.get(SessionModel, session_id).invite_skip_reason == "too_few_messages"
        assert db.query(CoachDigest).count() == 0


def test_視窗逾期不呼叫發訊API並記錄原因(make_client):
    from app.db.session import db_session

    client = make_client(links_enabled=True)
    session_id = _chat_and_end(client, "IGSID_D5", ["我今天心情不太好", "想找人聊聊"])
    with db_session() as db:
        db.get(SessionModel, session_id).window_expires_at = utcnow() - timedelta(minutes=1)

    _run(client)

    assert _digest_messages([session_id]) == []
    with db_session() as db:
        digest = db.query(CoachDigest).one()
        assert digest.sent_at is None
        assert digest.skip_reason == "messaging_window_expired"


def test_啟動補跑只撈上一個排定時刻之前結束的對話(make_client):
    """部署在下午：早上才結束的對話不該在部署當下提早送出。"""
    client = make_client(links_enabled=True)
    session_id = _chat_and_end(client, "IGSID_D6", ["我今天心情不太好", "想找人聊聊"])

    asyncio.run(client._main_module._run_digest(utcnow() - timedelta(hours=1)))
    assert _digest_messages([session_id]) == []


# ═══════════════════════════════════════════════════════════
#  舊按鈕（任務 5.4）
# ═══════════════════════════════════════════════════════════


def test_點擊舊按鈕回覆回饋連結而非報告內容(make_client):
    client = make_client(links_enabled=True)
    session_id = _chat_and_end(client, "IGSID_D7", ["我今天心情不太好", "想找人聊聊"])

    _post_postback(client, f"{INVITE_PAYLOAD_PREFIX}{session_id}", "IGSID_D7")

    from app.db.session import db_session

    with db_session() as db:
        latest = db.query(SessionModel).order_by(SessionModel.id.desc()).first().id
        clicked = db.query(Message).filter(Message.content == INVITE_BUTTON_TITLE).one()
        assert (clicked.meta or {}).get("system") == SystemMessageKind.COACH_DIGEST  # 不當聊天內容
    sent = _digest_messages([session_id, latest])
    assert len(sent) == 1 and "/report?t=" in sent[0].content
    assert client._fake_coaching.calls == []  # 沒有在私訊中產生、送出報告
