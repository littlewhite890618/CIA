"""教練回饋網頁的 API（daily-coach-report-link 第 6 群與任務 4.2）。

走真的 HTTP 請求：列出對話與狀態（只讀、不呼叫模型）、逐段產生並保存、
已產生不重做、分析未完成不產生、失敗可重試、拿不到別人的回饋、
連結只能在第一個打開的瀏覽器使用、頁面本身不登記連結。
"""

from __future__ import annotations

import asyncio

import pytest

from app.auth.settings_claim import REPORT_COOKIE_NAME
from app.auth.settings_link import REPORT_PURPOSE, issue_token
from app.db.models import CoachDigest, SignalStatus, utcnow
from app.db.models import Session as SessionModel
from tests.test_coach_report import (  # noqa: F401  (make_client 為 fixture)
    VALID_REPORT_JSON,
    _backdate_session,
    _get_coach_report,
    _post,
    make_client,
)

SECRET = "s" * 40  # 與 make_client(links_enabled=True) 相同


def _token(digest_id: int, ttl: int = 7 * 86400) -> str:
    return issue_token(digest_id, secret=SECRET, ttl_seconds=ttl, purpose=REPORT_PURPOSE)


def _chat_and_end(client, igsid: str, start: int) -> int:
    _post(client, f"{igsid}_{start}", "我今天心情不太好", igsid)
    _post(client, f"{igsid}_{start + 1}", "想找人聊聊", igsid)
    session_id = _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())
    return session_id


def _digest_of(session_id: int) -> int:
    from app.db.session import db_session

    with db_session() as db:
        return db.get(SessionModel, session_id).coach_digest_id


@pytest.fixture
def report(make_client):
    """一位使用者聊了兩段、凌晨的回饋已送出。回傳 (client, token, [兩段 session id])。"""
    def _make(coaching_replies: list[str] | None = None):
        client = make_client(links_enabled=True, coaching_replies=coaching_replies)
        client.base_url = "https://testserver"  # cookie 帶 Secure，測試用戶端改走 https 才送得回來
        first = _chat_and_end(client, "IGSID_R", 1)
        second = _chat_and_end(client, "IGSID_R", 3)
        asyncio.run(client._main_module._run_digest(utcnow()))
        digest_id = _digest_of(first)
        assert digest_id == _digest_of(second)
        return client, _token(digest_id), [first, second]
    return _make


def _get(client, token):
    return client.get("/api/report", params={"t": token})


def _generate(client, token, session_id):
    return client.post(f"/api/report/sessions/{session_id}/generate", params={"t": token})


# ═══════════════════════════════════════════════════════════
#  6.1 列出對話
# ═══════════════════════════════════════════════════════════


def test_列出對話依時間先後且只讀不呼叫模型(report):
    client, token, (first, second) = report()
    body = _get(client, token).json()

    assert [s["id"] for s in body["sessions"]] == [first, second]
    assert {s["status"] for s in body["sessions"]} == {"not_generated"}
    assert all(s["startedAt"] and s["endedAt"] for s in body["sessions"])
    assert body["personaName"] == "晴晴"
    assert client._fake_coaching.calls == []


# ═══════════════════════════════════════════════════════════
#  6.2 逐段產生
# ═══════════════════════════════════════════════════════════


def test_產生後保存且再打開不重做(report):
    client, token, (first, _second) = report()
    resp = _generate(client, token, first).json()

    assert resp["status"] == "ready"
    assert resp["sections"][0]["title"] == "這次聊起來的感覺"
    assert len(client._fake_coaching.calls) == 1

    _generate(client, token, first)
    listed = {s["id"]: s for s in _get(client, token).json()["sessions"]}
    assert listed[first]["status"] == "ready" and listed[first]["sections"]
    assert len(client._fake_coaching.calls) == 1  # 沒有再次呼叫模型

    seen_at = _get_coach_report(client._main_module, first).delivered_at
    assert seen_at is not None
    _get(client, token)
    assert _get_coach_report(client._main_module, first).delivered_at == seen_at  # 不覆蓋


@pytest.mark.parametrize(("signal_status", "expected"), [(None, "analysis_pending"),
                                                          (SignalStatus.FAILED, "analysis_failed")])
def test_行為分析未完成或失敗時不產生(report, signal_status, expected):
    from app.db.session import db_session

    client, token, (first, _second) = report()
    with db_session() as db:
        db.get(SessionModel, first).signal_status = signal_status

    assert _generate(client, token, first).json()["status"] == expected
    assert client._fake_coaching.calls == []


def test_產生失敗不留半份並可重試(report):
    client, token, (first, _second) = report(coaching_replies=["模型亂回的一段話", VALID_REPORT_JSON])

    failed = _generate(client, token, first).json()
    assert failed["status"] == "generation_failed" and failed["sections"] == []
    assert not _get_coach_report(client._main_module, first).content  # 沒有任何段落內容

    assert _generate(client, token, first).json()["status"] == "ready"  # 重新整理後重試


def test_不能產生不屬於這則回饋的對話(report):
    client, token, (first, _second) = report()
    other = _chat_and_end(client, "IGSID_OTHER", 1)
    asyncio.run(client._main_module._run_digest(utcnow()))
    assert _digest_of(other) != _digest_of(first)

    resp = _generate(client, token, other)
    assert resp.status_code == 401 and resp.json() == {"error": "invalid"}
    assert client._fake_coaching.calls == []


# ═══════════════════════════════════════════════════════════
#  4.2 連結的存取
# ═══════════════════════════════════════════════════════════


def test_換瀏覽器被拒絕_原瀏覽器可重新整理(report):
    client, token, (first, _second) = report()
    resp = _get(client, token)
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"{REPORT_COOKIE_NAME}=") and "Path=/api/report" in cookie

    assert _get(client, token).status_code == 200  # 重新整理
    client.cookies.clear()  # 換一支手機
    assert _get(client, token).json() == {"error": "used"}
    assert _generate(client, token, first).json() == {"error": "used"}
    assert client._fake_coaching.calls == []


def test_過期_竄改_用設定連結冒用都被拒絕(report):
    client, token, (first, _second) = report()
    digest_id = _digest_of(first)
    assert _get(client, _token(digest_id, ttl=-10)).json() == {"error": "expired"}
    assert _get(client, "garbage.token").json() == {"error": "invalid"}
    settings_token = issue_token(digest_id, secret=SECRET, ttl_seconds=600)  # 設定連結的前綴
    assert _get(client, settings_token).json() == {"error": "invalid"}


def test_頁面本身不登記連結且預覽圖片是完整網址(report, tmp_path, monkeypatch):
    """IG 為了預覽卡片會抓網址；頁面若也登記，使用者點開前連結就被預覽機器人用掉了。"""
    import app.web.report_api as report_api

    (tmp_path / "report.html").write_text(
        '<meta property="og:image" content="__PUBLIC_BASE_URL__/assets/og-sunny.jpg" />', encoding="utf-8"
    )
    monkeypatch.setattr(report_api, "FRONTEND_DIST", tmp_path)
    client, token, _ = report()

    page = client.get("/report", params={"t": token})
    assert 'content="https://example.ngrok-free.dev/assets/og-sunny.jpg"' in page.text
    assert page.headers["cache-control"] == "no-store"

    client.cookies.clear()
    assert _get(client, token).status_code == 200  # 使用者本人仍是第一個


def test_回饋功能未啟用時回404(make_client):
    client = make_client(links_enabled=False)
    assert client.get("/api/report", params={"t": "x.y"}).status_code == 404
