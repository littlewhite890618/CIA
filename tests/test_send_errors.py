"""發訊錯誤的分類、紀錄與告警（add-persona-authoring-console 第 9 群：發訊配速的監測）。

Meta 的回應以 httpx.MockTransport 模擬，確認錯誤代碼一路帶到 send_errors 表；
發訊流程沿用 test_sender.py 的假客戶端與虛擬時鐘。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

import httpx
import pytest

from app.channels.instagram import (
    InstagramClient,
    InstagramRateLimited,
    InstagramSendError,
    InstagramWindowExpired,
)
from app.channels.send_errors import (
    OTHER,
    POLICY_BLOCK,
    RATE_LIMITED,
    TOKEN_INVALID,
    WINDOW_EXPIRED,
    classify,
)
from app.db import repository as repo
from app.db.models import SendError, utcnow
from scripts.send_health import render
from tests.test_sender import IGSID, FakeClient, _make_sender, _seed, clock, env  # noqa: F401


def _meta_error(status: int, code: int, subcode: int | None = None, message: str = "錯誤說明"):
    body = {"error": {"message": message, "type": "OAuthException", "code": code}}
    if subcode is not None:
        body["error"]["error_subcode"] = subcode

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=json.dumps(body, ensure_ascii=False).encode())

    return handler


def _raise_from(handler) -> InstagramSendError:
    client = InstagramClient("fake_token", "17841400000001")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run():
        try:
            await client.send_text("IGSID_X", "嗨")
        except InstagramSendError as exc:
            return exc
        finally:
            await client.aclose()

    exc = asyncio.run(_run())
    assert exc is not None
    return exc


# ═══════════════════════════════════════════════════════════
#  分類
# ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("status", "code", "subcode", "expected"),
    [
        (400, 613, None, RATE_LIMITED),
        (400, 4, None, RATE_LIMITED),
        (403, 10, 2534022, WINDOW_EXPIRED),
        (400, 368, 1390008, POLICY_BLOCK),
        (401, 190, 463, TOKEN_INVALID),
        (400, 100, None, OTHER),
        (403, 10, 2018278, OTHER),  # code 10 的其他權限問題不能誤判成視窗逾期
    ],
)
def test_依Meta錯誤代碼分類並保留原始資訊(status, code, subcode, expected):
    exc = _raise_from(_meta_error(status, code, subcode, message="Meta 給的說明"))
    assert classify(exc) == expected
    assert (exc.status, exc.code, exc.subcode) == (status, code, subcode)
    assert "Meta 給的說明" in exc.detail


def test_HTTP429視為限速():
    exc = _raise_from(lambda request: httpx.Response(429, text="Too Many Requests"))
    assert isinstance(exc, InstagramRateLimited)
    assert classify(exc) == RATE_LIMITED


def test_連線失敗歸為其他():
    def handler(request):
        raise httpx.ConnectError("連不上")

    exc = _raise_from(handler)
    assert exc.status is None
    assert classify(exc) == OTHER


# ═══════════════════════════════════════════════════════════
#  寫進 send_errors 表與告警
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def sender_log(caplog, monkeypatch):
    """收 sender 的 warning。Postgres 模式的測試會跑 alembic，它的日誌設定會把
    已存在的 logger 停用（disable_existing_loggers），這裡明確打開。"""
    monkeypatch.setattr(logging.getLogger("app.channels.sender"), "disabled", False)
    with caplog.at_level(logging.WARNING):
        yield caplog


def _errors(env):
    with env.db_session() as db:
        return [(e.category, e.error_code, e.session_id) for e in db.query(SendError).order_by(SendError.id)]


def test_疑似違規寫入資料表並告警(env, clock, sender_log):
    session_id, persona_id = _seed(env)
    blocked = InstagramSendError("HTTP 400", status=400, code=368, subcode=1390008, detail="deemed abusive")
    sender = _make_sender(FakeClient(raise_first=blocked, raise_times=99), clock=clock)

    outcome = asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="嗨")
    )

    assert not outcome.ok
    assert _errors(env) == [(POLICY_BLOCK, 368, session_id)]
    assert any(r.getMessage() == "【告警】發訊失敗：疑似違規" for r in sender_log.records)


def test_限速每次重試都留紀錄(env, clock, sender_log):
    session_id, persona_id = _seed(env)
    client = FakeClient(raise_first=InstagramRateLimited("error code 613", code=613), raise_times=2)
    sender = _make_sender(client, clock=clock, max_retries=3)

    outcome = asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="嗨")
    )

    assert outcome.ok  # 退避後送出，行為與導入監測前相同
    assert _errors(env) == [(RATE_LIMITED, 613, session_id)] * 2
    assert sum(r.getMessage() == "【告警】發訊失敗：限速" for r in sender_log.records) == 2


def test_視窗逾期與其他錯誤只記錄不告警(env, clock, sender_log):
    session_id, persona_id = _seed(env)
    for exc in (
        InstagramWindowExpired("error subcode 2534022", status=403, code=10, subcode=2534022),
        InstagramSendError("HTTP 400", status=400, code=100),
    ):
        sender = _make_sender(FakeClient(raise_first=exc, raise_times=99), clock=clock)
        asyncio.run(
            sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="嗨")
        )

    assert [c for c, _code, _sid in _errors(env)] == [WINDOW_EXPIRED, OTHER]
    assert not any(r.getMessage().startswith("【告警】") for r in sender_log.records)


def test_紀錄寫入失敗不影響發訊流程(env, clock, monkeypatch):
    session_id, persona_id = _seed(env)

    def broken(*args, **kwargs):
        raise RuntimeError("資料庫壞了")

    monkeypatch.setattr(repo, "record_send_error", broken)
    client = FakeClient(raise_first=InstagramRateLimited("error code 4", code=4), raise_times=1)
    sender = _make_sender(client, clock=clock)

    outcome = asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="嗨")
    )
    assert outcome.ok


# ═══════════════════════════════════════════════════════════
#  報表
# ═══════════════════════════════════════════════════════════


def test_每小時統計合併發訊量與錯誤(env, clock):
    session_id, persona_id = _seed(env)
    now = utcnow()
    with env.db_session() as db:
        repo.bump_rate_budget(db, persona_id=persona_id, sent=5)
        for category in (RATE_LIMITED, RATE_LIMITED, WINDOW_EXPIRED):
            repo.record_send_error(
                db, persona_id=persona_id, session_id=session_id, category=category,
                http_status=400, error_code=613, error_subcode=None, detail="x",
            )
        hours = repo.send_activity_since(db, now - timedelta(hours=1))
        errors = repo.recent_send_errors(db, 10)
        for e in errors:
            db.expunge(e)

    (counts,) = hours.values()
    assert counts == {"sent": 5, RATE_LIMITED: 2, WINDOW_EXPIRED: 1}

    text = render(hours, errors, 24)
    assert text.startswith("最近 24 小時：發出 5 則，單一小時最多 5 則，告警 2 次")
    assert "最近的發訊錯誤：" in text
