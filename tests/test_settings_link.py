"""使用者設定頁連結（add-persona-authoring-console 任務 5.1、5.4，design.md 決策 13、14）。

單元層驗證 token 的簽發與驗證（逾時、竄改、錯誤密鑰）；整合層走完整
webhook 管線，確認「設定」回覆連結、不呼叫 LLM、新對話帶 AI 揭露，
且「設定」與連結不混進對話上下文、訊號偵測與插話判定。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from app.auth.settings_link import (
    SettingsLinkExpired,
    SettingsLinkInvalid,
    build_settings_url,
    issue_token,
    verify_token,
)
from tests.test_reply_generation import (  # noqa: F401  (make_client 為 fixture)
    IG_ACCOUNT_ID,
    _backdate_session,
    _outbound_texts,
    _post,
    _simulate_inbound_during_delay,
    make_client,
)

SECRET = "s" * 40
OTHER_SECRET = "o" * 40
BASE_URL = "https://example.ngrok-free.dev"
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════
#  token 簽發與驗證
# ═══════════════════════════════════════════════════════════

def test_簽發後可驗證並取回連結編號():
    token = issue_token(42, secret=SECRET, ttl_seconds=1800, now=NOW)
    assert verify_token(token, secret=SECRET, now=NOW) == 42


def test_有效期內可重複使用():
    """保存前可能先開頁面看一下、改完再存，同一條連結要能用不只一次。"""
    token = issue_token(42, secret=SECRET, ttl_seconds=1800, now=NOW)
    later = NOW + timedelta(minutes=29)
    assert verify_token(token, secret=SECRET, now=later) == 42
    assert verify_token(token, secret=SECRET, now=later) == 42


def test_逾時後失效():
    token = issue_token(42, secret=SECRET, ttl_seconds=1800, now=NOW)
    with pytest.raises(SettingsLinkExpired):
        verify_token(token, secret=SECRET, now=NOW + timedelta(seconds=1800))


def test_竄改連結編號後無效():
    """把 payload 換成另一位使用者的編號，簽章就對不上。"""
    token = issue_token(42, secret=SECRET, ttl_seconds=1800, now=NOW)
    _payload, signature = token.split(".")
    other_payload = issue_token(43, secret=SECRET, ttl_seconds=1800, now=NOW).split(".")[0]
    with pytest.raises(SettingsLinkInvalid):
        verify_token(f"{other_payload}.{signature}", secret=SECRET, now=NOW)


def test_竄改簽章後無效():
    token = issue_token(42, secret=SECRET, ttl_seconds=1800, now=NOW)
    payload, signature = token.split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(SettingsLinkInvalid):
        verify_token(f"{payload}.{flipped}", secret=SECRET, now=NOW)


def test_換了密鑰後舊連結無效():
    token = issue_token(42, secret=SECRET, ttl_seconds=1800, now=NOW)
    with pytest.raises(SettingsLinkInvalid):
        verify_token(token, secret=OTHER_SECRET, now=NOW)


def test_竄改且過期的連結回報無效而非過期():
    """「過期」只能對真連結說，否則等於告訴竄改者「這個編號的使用者存在」。"""
    token = issue_token(42, secret=SECRET, ttl_seconds=1, now=NOW)
    payload, signature = token.split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(SettingsLinkInvalid):
        verify_token(f"{payload}.{flipped}", secret=SECRET, now=NOW + timedelta(days=1))


@pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", ".", "abc.", ".abc", "中文.中文"])
def test_格式錯誤的連結無效(garbage):
    with pytest.raises(SettingsLinkInvalid):
        verify_token(garbage, secret=SECRET, now=NOW)


def test_密鑰太短時拒絕簽發與驗證():
    with pytest.raises(ValueError):
        issue_token(42, secret="short", ttl_seconds=1800)
    with pytest.raises(ValueError):
        verify_token("a.b", secret="short")


def test_組連結時處理結尾斜線():
    assert build_settings_url(BASE_URL + "/", "tok") == f"{BASE_URL}/settings?t=tok"
    assert build_settings_url(BASE_URL, "tok") == f"{BASE_URL}/settings?t=tok"


@pytest.mark.parametrize(
    ("secret", "base_url", "enabled"),
    [(SECRET, BASE_URL, True), ("short", BASE_URL, False), (SECRET, "", False), ("", "", False)],
)
def test_密鑰或網址缺一則功能關閉(monkeypatch, secret, base_url, enabled):
    from app.config import Settings

    monkeypatch.setenv("SETTINGS_LINK_SECRET", secret)
    monkeypatch.setenv("PUBLIC_BASE_URL", base_url)
    assert Settings().settings_link_enabled is enabled


# ═══════════════════════════════════════════════════════════
#  整合：完整 webhook 管線
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def enabled_client(make_client):
    """設定頁功能啟用的 TestClient（密鑰與網址同本檔的 SECRET、BASE_URL）。"""

    def _make(**kwargs):
        return make_client(settings_link=True, **kwargs)

    return _make


def _link_id(igsid: str) -> int:
    from app.db.models import Persona, UserPersonaLink
    from app.db.session import db_session

    with db_session() as db:
        persona = db.query(Persona).filter(Persona.ig_account_id == IG_ACCOUNT_ID).one()
        return (
            db.query(UserPersonaLink)
            .filter(UserPersonaLink.persona_id == persona.id, UserPersonaLink.igsid == igsid)
            .one()
            .id
        )


def _token_from(text: str) -> str:
    url = re.search(r"https://\S+", text).group(0)
    assert url.startswith(f"{BASE_URL}/settings?")
    return parse_qs(urlparse(url).query)["t"][0]


def test_第一則就傳設定_回覆連結且帶AI揭露不呼叫LLM(enabled_client):
    client = enabled_client()
    igsid = "IGSID_SET_FIRST"
    resp = _post(client, "mid_1", "設定", igsid)
    assert resp.status_code == 200

    assert client._fake_provider.calls == []  # 系統回覆，不是角色在聊天

    [text] = _outbound_texts(client._main_module, igsid)
    assert "AI" in text and "晴晴" in text  # 新對話的第一則回覆必須揭露
    assert verify_token(_token_from(text), secret=SECRET) == _link_id(igsid)


def test_聊天中傳設定_不重複揭露(enabled_client):
    client = enabled_client(replies=["嗨呀。"])
    igsid = "IGSID_SET_MID"
    _post(client, "mid_1", "嗨", igsid)
    _post(client, "mid_2", " 設定 ", igsid)  # 前後空白仍算關鍵字

    texts = _outbound_texts(client._main_module, igsid)
    assert len(client._fake_provider.calls) == 1
    link_reply = texts[-1]
    assert f"{BASE_URL}/settings?t=" in link_reply
    assert "AI" not in link_reply


def test_連結綁定各自的使用者(enabled_client):
    client = enabled_client()
    _post(client, "mid_a", "設定", "IGSID_SET_A")
    _post(client, "mid_b", "設定", "IGSID_SET_B")

    token_a = _token_from(_outbound_texts(client._main_module, "IGSID_SET_A")[0])
    token_b = _token_from(_outbound_texts(client._main_module, "IGSID_SET_B")[0])
    assert verify_token(token_a, secret=SECRET) == _link_id("IGSID_SET_A")
    assert verify_token(token_b, secret=SECRET) == _link_id("IGSID_SET_B")
    assert _link_id("IGSID_SET_A") != _link_id("IGSID_SET_B")


def test_含有關鍵字的一般句子不算(enabled_client):
    client = enabled_client(replies=["回覆。"])
    _post(client, "mid_1", "我想設定一個目標", "IGSID_SET_SENTENCE")
    assert len(client._fake_provider.calls) == 1


def test_功能未啟用時設定當一般訊息處理(make_client):
    client = make_client(replies=["嗯嗯，想設定什麼呢？"])
    _post(client, "mid_1", "設定", "IGSID_SET_OFF")

    assert len(client._fake_provider.calls) == 1
    texts = _outbound_texts(client._main_module, "IGSID_SET_OFF")
    assert not any("/settings?t=" in t for t in texts)


def test_設定與連結不進入之後的對話上下文(enabled_client):
    client = enabled_client(replies=["嗨呀。", "我在呀。"])
    igsid = "IGSID_SET_CONTEXT"
    _post(client, "mid_1", "嗨", igsid)
    _post(client, "mid_2", "設定", igsid)
    _post(client, "mid_3", "在嗎", igsid)

    contents = [m.content for m in client._fake_provider.calls[-1]["messages"]]
    assert "嗨" in contents and "在嗎" in contents
    assert "設定" not in contents
    assert not any("/settings?t=" in c for c in contents)


def test_設定與連結不進入訊號偵測與話題萃取(enabled_client):
    client = enabled_client(replies=["你週末喜歡做什麼呢？"])
    igsid = "IGSID_SET_SIGNALS"
    _post(client, "mid_1", "嗨嗨", igsid)
    _post(client, "mid_2", "設定", igsid)

    _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    topic_call, signal_call = client._fake_analysis_provider.calls
    for call in (topic_call, signal_call):
        transcript = call["messages"][0].content
        assert "嗨嗨" in transcript
        assert "使用者：設定" not in transcript
        assert "/settings?t=" not in transcript


def test_延遲期間傳設定_不丟棄準備中的回覆(enabled_client, monkeypatch):
    """「設定」不觸發一般回覆；若它讓準備中的回覆被丟棄，使用者的上一句就沒人回了。"""
    client = enabled_client(replies=["我在聽，你說。"])
    igsid = "IGSID_SET_STALE"

    import app.dialog.delivery as delivery_module

    triggered = False

    async def _sleep_then_ask_for_settings(seconds: float) -> None:
        nonlocal triggered
        if not triggered:
            triggered = True
            await _simulate_inbound_during_delay(client._main_module, "mid_settings", "設定", igsid)

    monkeypatch.setattr(delivery_module, "DEFAULT_SLEEP", _sleep_then_ask_for_settings)
    _post(client, "mid_first", "我今天有點累", igsid)

    texts = _outbound_texts(client._main_module, igsid)
    assert any("我在聽，你說" in t for t in texts)
    assert any("/settings?t=" in t for t in texts)


HINT = "想調整我的個性，隨時傳「設定」給我就好"


def test_提示_新對話揭露時附上設定提示(enabled_client):
    """任務 5.5：讓新使用者知道可以傳「設定」。"""
    client = enabled_client(replies=["嗨呀，今天過得好嗎？"])
    _post(client, "mid_1", "嗨", "IGSID_HINT_NEW")

    joined = "\n".join(_outbound_texts(client._main_module, "IGSID_HINT_NEW"))
    assert "AI" in joined
    assert HINT in joined
    assert joined.index("AI") < joined.index(HINT) < joined.index("今天過得好嗎")


def test_提示_同一場後續回覆不再附上(enabled_client):
    client = enabled_client(replies=["嗨呀。", "我在呀。"])
    igsid = "IGSID_HINT_LATER"
    _post(client, "mid_1", "嗨", igsid)
    _post(client, "mid_2", "在嗎", igsid)

    texts = _outbound_texts(client._main_module, igsid)
    assert sum(HINT in t for t in texts) == 1


def test_提示_功能未啟用時不附上(make_client):
    """沒有設定頁就不能叫使用者傳「設定」，否則傳了只會得到一般回覆。"""
    client = make_client(replies=["嗨呀。"])
    _post(client, "mid_1", "嗨", "IGSID_HINT_OFF")

    joined = "\n".join(_outbound_texts(client._main_module, "IGSID_HINT_OFF"))
    assert "AI" in joined
    assert HINT not in joined


def test_提示_設定連結那則不附上提示(enabled_client):
    """已經在要連結了，不必再提示他可以傳「設定」。"""
    client = enabled_client()
    _post(client, "mid_1", "設定", "IGSID_HINT_LINK")

    [text] = _outbound_texts(client._main_module, "IGSID_HINT_LINK")
    assert "AI" in text
    assert HINT not in text


def test_重送的設定事件不重複發連結(enabled_client):
    """Meta 重送同一事件時由 platform_message_id 去重，與一般訊息相同。"""
    client = enabled_client()
    igsid = "IGSID_SET_DUP"
    _post(client, "mid_dup", "設定", igsid)
    _post(client, "mid_dup", "設定", igsid)

    assert len(_outbound_texts(client._main_module, igsid)) == 1
