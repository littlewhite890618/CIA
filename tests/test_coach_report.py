"""教練報告：生成模組單元測試 + 邀請／觸發／送達／補救的整合測試
（M5＋M9，tasks 2.5、3.5、4.5、5.3）。

單元測試段直接測 app/dialog/coach_report.py 的純函式。
整合測試段的架構與 tests/test_reply_generation.py 相同：走完整
webhook → 背景任務管線，額外注入 coaching 路徑的 FakeProvider
與 InstagramClient.send_button_template 的假實作。

邀請按鈕原本用 Quick Reply，2026-09-23 真機實測發現 Instagram 不會
渲染，改用 Button Template——點擊觸發的是 messaging_postbacks 事件，
不是一般 message 事件，見 app/dialog/coach_report.py 的說明。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.dialog.coach_report import (
    INVITE_PAYLOAD_PREFIX,
    display_sections,
    generate_report,
    parse_invite_payload,
    parse_report,
)
from app.llm.base import LLMMessage, LLMProvider, LLMResponse
from app.llm.router import LLMRouter

APP_SECRET = "test_app_secret"
VERIFY_TOKEN = "test_verify_token"
IG_ACCOUNT_ID = "17841400000001"

VALID_REPORT_JSON = json.dumps(
    {
        "摘要": "整體聊得很自然",
        "做得好": "你有接住對方說的煩惱，例如「聽起來很辛苦」",
        "可調整": "有兩則只回了「嗯嗯」，可以再多回應一點",
        "改寫示範": "把「嗯嗯」換成「聽起來今天特別累，發生什麼事了嗎？」",
        "下次可試": "可以多問一句對方的感受，而不只是事實",
    },
    ensure_ascii=False,
)


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ═══════════════════════════════════════════════════════════
#  單元測試：app/dialog/coach_report.py（task 3.5 的純函式部分）
# ═══════════════════════════════════════════════════════════


def test_舊邀請按鈕的payload可解析出session_id():
    """改版後不再送按鈕，但使用者手上的舊按鈕被點擊時仍要認得（任務 5.4）。"""
    assert parse_invite_payload(f"{INVITE_PAYLOAD_PREFIX}42") == 42


def test_邀請payload格式不符時回傳None():
    assert parse_invite_payload("不是這個格式的字串") is None
    assert parse_invite_payload(None) is None
    assert parse_invite_payload(f"{INVITE_PAYLOAD_PREFIX}不是數字") is None


def test_解析合法的五段結構():
    content = parse_report(VALID_REPORT_JSON)
    assert set(content) == {"摘要", "做得好", "可調整", "改寫示範", "下次可試"}
    assert "聽起來很辛苦" in content["做得好"]


def test_解析時缺少段落應拋出錯誤():
    bad = json.dumps({"摘要": "還可以", "做得好": "有接話"})  # 缺三段
    with pytest.raises(Exception):
        parse_report(bad)


def test_解析時不是合法JSON應拋出錯誤():
    with pytest.raises(Exception):
        parse_report("這不是 JSON，模型亂輸出了一段話")


def test_網頁段落套上中文標題並依內容順序():
    sections = display_sections(json.loads(VALID_REPORT_JSON))
    assert [s["title"] for s in sections] == [
        "這次聊起來的感覺", "做得好的地方", "可以再調整的地方", "換個說法試試", "下次可以試試",
    ]
    assert "整體聊得很自然" in sections[0]["body"]


def test_網頁段落內容為空時回傳空清單():
    assert display_sections({}) == []
    assert display_sections(None) == []


class _FakeCoachingProvider(LLMProvider):
    def __init__(self, reply_text: str) -> None:
        self.calls: list[dict] = []
        self._reply_text = reply_text

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-coaching-1"

    async def generate(self, *, system_prompt, messages, temperature=0.9, max_output_tokens=1024):
        self.calls.append({"system_prompt": system_prompt, "messages": list(messages)})
        return LLMResponse(text=self._reply_text, provider="fake", model=self.model, elapsed_ms=1)


def test_generate_report成功時回傳五段內容且引用訊號原因(monkeypatch):
    from app.db.models import RealtimeSignal, SignalType

    signal = RealtimeSignal(
        id=1,
        session_id=1,
        message_id=10,
        signal_type=SignalType.HIGHLIGHT,
        severity=None,
        dimension="情緒感知",
        reason="使用者說「聽起來很辛苦」，準確接住角色的情緒",
    )
    provider = _FakeCoachingProvider(VALID_REPORT_JSON)
    router = LLMRouter({"coaching": provider})

    result = asyncio.run(generate_report(router, [signal], "晴晴"))

    assert result.ok
    assert result.content is not None
    assert len(provider.calls) == 1
    # 訊號的 reason（含原句）必須真的被送進提示，這是「唯一資料來源」的保證
    assert "聽起來很辛苦" in provider.calls[0]["system_prompt"]
    assert isinstance(provider.calls[0]["messages"][0], LLMMessage)


def test_generate_report解析失敗時回傳失敗原因不拋出例外():
    provider = _FakeCoachingProvider("模型亂回一段不是 JSON 的話")
    router = LLMRouter({"coaching": provider})

    result = asyncio.run(generate_report(router, [], "晴晴"))

    assert not result.ok
    assert result.content is None
    assert result.error is not None


def test_generate_report沒有訊號時仍能產生報告():
    provider = _FakeCoachingProvider(VALID_REPORT_JSON)
    router = LLMRouter({"coaching": provider})

    result = asyncio.run(generate_report(router, [], "晴晴"))

    assert result.ok
    # 沒有訊號時提示要說明「這場沒有訊號」，而不是留白讓模型自己編
    assert "沒有偵測到" in provider.calls[0]["system_prompt"]


# ═══════════════════════════════════════════════════════════
#  整合測試用的 fixture 與輔助函式
#  （daily-coach-report-link 之後的流程測試見 tests/test_daily_digest.py、
#   tests/test_report_api.py；舊的按鈕邀請流程已移除）
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    def _make(
        dialog_replies: list[str] | None = None,
        analysis_replies: list[str] | None = None,
        coaching_replies: list[str] | None = None,
        links_enabled: bool = False,
    ):
        import importlib

        db_file = tmp_path / "test.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        # 不受本機 .env 影響。links_enabled 才開啟設定頁與每日回饋連結
        #（兩者共用簽章密鑰與對外網址）
        monkeypatch.setenv("SETTINGS_LINK_SECRET", "s" * 40 if links_enabled else "")
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.ngrok-free.dev" if links_enabled else "")
        monkeypatch.setenv("IG_APP_SECRET", APP_SECRET)
        monkeypatch.setenv("META_APP_SECRET", "不該被用來驗章的密鑰")
        monkeypatch.setenv("META_WEBHOOK_VERIFY_TOKEN", VERIFY_TOKEN)
        monkeypatch.setenv("IG_ACCESS_TOKEN", "fake_token")
        monkeypatch.setenv("IG_ACCOUNT_ID", IG_ACCOUNT_ID)
        monkeypatch.setenv("GEMINI_API_KEY", "")
        monkeypatch.setenv("WEBHOOK_SKIP_SIGNATURE_CHECK", "false")
        # 互動門檻降到 2，測試不需要灌一堆訊息才能觸發邀請
        monkeypatch.setenv("COACH_REPORT_MIN_USER_MESSAGES", "2")

        from app.config import get_settings

        get_settings.cache_clear()

        import app.db.session as session_module

        importlib.reload(session_module)

        import app.main as main_module

        importlib.reload(main_module)

        _fake_id_counter = iter(range(1, 100_000))

        async def _fake_send_text(self, igsid, text):
            return f"fake_message_id_{next(_fake_id_counter)}"

        async def _fake_sender_action(self, igsid, action):
            return None

        async def _fake_fetch_username(self, igsid):
            return None

        async def _fake_send_button_template(self, igsid, text, buttons):
            return f"fake_message_id_{next(_fake_id_counter)}"

        monkeypatch.setattr(main_module.InstagramClient, "send_text", _fake_send_text)
        monkeypatch.setattr(main_module.InstagramClient, "send_sender_action", _fake_sender_action)
        monkeypatch.setattr(main_module.InstagramClient, "fetch_username", _fake_fetch_username)
        monkeypatch.setattr(
            main_module.InstagramClient, "send_button_template", _fake_send_button_template
        )

        import app.dialog.delivery as delivery_module

        async def _no_sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(delivery_module, "DEFAULT_SLEEP", _no_sleep)

        client = TestClient(main_module.app)
        client.__enter__()

        fake_dialog = _FakeCoachingProvider(
            "\n".join(dialog_replies or ["嗯嗯，我懂你的感覺呢。"])
        )
        # 兩者都走 analysis 路徑：話題萃取先呼叫、訊號偵測後呼叫，
        # 順序見 app/main.py 的 _close_session_and_analyze。
        fake_analysis = _MultiReplyProvider(analysis_replies or ["", "[]"])
        fake_coaching = _FakeCoachingProvider(
            (coaching_replies or [VALID_REPORT_JSON])[0]
        )
        if coaching_replies:
            fake_coaching = _MultiReplyProvider(coaching_replies)

        main_module.llm_router = LLMRouter(
            {"dialog": fake_dialog, "analysis": fake_analysis, "coaching": fake_coaching}
        )

        client._main_module = main_module  # type: ignore[attr-defined]
        client._fake_coaching = fake_coaching  # type: ignore[attr-defined]
        return client

    yield _make

    from app.config import get_settings

    get_settings.cache_clear()


class _MultiReplyProvider(LLMProvider):
    """依呼叫次序回傳不同內容，不足時沿用最後一則。"""

    def __init__(self, replies: list[str]) -> None:
        self.calls: list[dict] = []
        self._replies = list(replies)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-multi-1"

    async def generate(self, *, system_prompt, messages, temperature=0.9, max_output_tokens=1024):
        self.calls.append({"system_prompt": system_prompt, "messages": list(messages)})
        idx = min(len(self.calls) - 1, len(self._replies) - 1)
        return LLMResponse(text=self._replies[idx], provider="fake", model=self.model, elapsed_ms=1)


def _message_payload(mid: str = "mid_001", text: str = "嗨", igsid: str = "IGSID_A") -> dict:
    return {
        "object": "instagram",
        "entry": [
            {
                "id": IG_ACCOUNT_ID,
                "messaging": [
                    {
                        "sender": {"id": igsid},
                        "recipient": {"id": IG_ACCOUNT_ID},
                        "message": {"mid": mid, "text": text},
                    }
                ],
            }
        ],
    }


def _postback_payload(payload: str, igsid: str) -> dict:
    """邀請按鈕（Button Template，type="postback"）點擊後 Meta 送來的事件。

    真正是 messaging_postbacks 事件，不帶 mid——postback 沒有訊息識別碼
    這個概念，見 app/channels/instagram.py 的 PostbackEvent。
    """
    return {
        "object": "instagram",
        "entry": [
            {
                "id": IG_ACCOUNT_ID,
                "messaging": [
                    {
                        "sender": {"id": igsid},
                        "recipient": {"id": IG_ACCOUNT_ID},
                        "postback": {"payload": payload},
                    }
                ],
            }
        ],
    }


def _post(client, mid: str, text: str, igsid: str) -> None:
    body = json.dumps(_message_payload(mid, text, igsid)).encode()
    resp = client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"}
    )
    assert resp.status_code == 200


def _post_postback(client, payload: str, igsid: str) -> None:
    body = json.dumps(_postback_payload(payload, igsid)).encode()
    resp = client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"}
    )
    assert resp.status_code == 200


def _backdate_session(main_module, igsid: str, minutes: int) -> int:
    """把該使用者最新場次的 last_message_at 往前推，模擬靜默。回傳 session id。"""
    from app.db.models import Persona
    from app.db.models import Session as SessionModel
    from app.db.models import UserPersonaLink
    from app.db.session import db_session
    from app.db.models import utcnow

    with db_session() as db:
        persona = db.query(Persona).filter(Persona.ig_account_id == IG_ACCOUNT_ID).one()
        link = (
            db.query(UserPersonaLink)
            .filter(UserPersonaLink.persona_id == persona.id, UserPersonaLink.igsid == igsid)
            .one()
        )
        session = (
            db.query(SessionModel)
            .filter(SessionModel.user_id == link.user_id, SessionModel.persona_id == persona.id)
            .order_by(SessionModel.id.desc())
            .first()
        )
        session.last_message_at = utcnow() - timedelta(minutes=minutes)
        db.flush()
        return session.id


def _get_session(main_module, session_id: int):
    from app.db.models import Session as SessionModel
    from app.db.session import db_session

    with db_session() as db:
        s = db.get(SessionModel, session_id)
        db.expunge(s)
        return s


def _get_coach_report(main_module, session_id: int):
    from app.db import repository as repo
    from app.db.session import db_session

    with db_session() as db:
        r = repo.get_coach_report(db, session_id)
        if r is not None:
            db.expunge(r)
        return r
