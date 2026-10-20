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
    build_invite_buttons,
    format_report_text,
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


def test_邀請payload的組裝與解析可還原session_id():
    buttons = build_invite_buttons(42)
    assert len(buttons) == 1
    payload = buttons[0]["payload"]
    assert payload.startswith(INVITE_PAYLOAD_PREFIX)
    assert parse_invite_payload(payload) == 42


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


def test_format_report_text以空行分隔各段且套上中文標籤():
    text = format_report_text(json.loads(VALID_REPORT_JSON))
    assert "\n\n" in text
    assert "【做得好的地方】" in text
    assert "整體聊得很自然" in text


def test_format_report_text內容為空時回傳空字串():
    assert format_report_text({}) == ""


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
#  整合測試：webhook → 邀請 → 點擊 → 生成 → 送達（tasks 2.5、4.5、5.3）
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    def _make(
        dialog_replies: list[str] | None = None,
        analysis_replies: list[str] | None = None,
        coaching_replies: list[str] | None = None,
    ):
        import importlib

        db_file = tmp_path / "test.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        # 不受本機 .env 影響：設定頁功能（M13 任務 5.1）關閉
        monkeypatch.setenv("SETTINGS_LINK_SECRET", "")
        monkeypatch.setenv("PUBLIC_BASE_URL", "")
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


def test_訊息足夠且靜默結束後發送邀請且僅發一次(make_client):
    client = make_client()
    igsid = "IGSID_INVITE_1"
    _post(client, "mid_1", "我今天心情不太好", igsid)
    _post(client, "mid_2", "想找人聊聊", igsid)

    session_id = _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    session = _get_session(client._main_module, session_id)
    assert session.invite_sent is True

    # 再跑一次輪詢：session 已 ENDED，find_silent_sessions 不會再選到它，
    # 不會重複發送（沿用既有機制，這裡驗證邀請沒有被觸發第二次的副作用）。
    asyncio.run(client._main_module._run_silence_pass())
    session_again = _get_session(client._main_module, session_id)
    assert session_again.invite_sent is True


def test_互動過少不發送邀請並記錄原因(make_client):
    client = make_client()
    igsid = "IGSID_INVITE_2"
    _post(client, "mid_1", "嗨", igsid)  # 只有一則，門檻設為 2

    session_id = _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    session = _get_session(client._main_module, session_id)
    assert session.invite_sent is False
    assert session.invite_skip_reason == "too_few_messages"


def test_點擊邀請後生成報告並送達內容可回溯至訊號(make_client):
    client = make_client()
    igsid = "IGSID_CLICK_1"
    _post(client, "mid_1", "我今天心情不太好", igsid)
    _post(client, "mid_2", "你今天過得怎麼樣？", igsid)

    # 訊號的 message_id 必須是這個 session 真實入站訊息的 id，否則
    # parse_signals 會把它當成不合法的訊號丟棄（見 signals.py）——
    # 訊息 id 要等實際發送過才知道，所以在建立 client 之後才回頭
    # 把 analysis 假供應商（此時已被 llm_router 持有同一個實例）的
    # 回覆內容換成真正引用得到的 message_id。
    from app.db.models import Message, MessageDirection
    from app.db.session import db_session

    with db_session() as db:
        second_inbound_id = (
            db.query(Message.id)
            .filter(
                Message.platform_message_id == "mid_2",
                Message.direction == MessageDirection.INBOUND,
            )
            .scalar()
        )

    signal_json = json.dumps(
        [
            {
                "message_id": second_inbound_id,
                "item": "H1",
                "reason": "問了開放式問題「你今天過得怎麼樣？」",
            }
        ],
        ensure_ascii=False,
    )
    analysis_provider = client._main_module.llm_router.provider_for("analysis")
    analysis_provider._replies = ["", signal_json]

    session_id = _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    from app.dialog.coach_report import build_invite_buttons

    payload = build_invite_buttons(session_id)[0]["payload"]
    _post_postback(client, payload, igsid)

    report = _get_coach_report(client._main_module, session_id)
    assert report is not None
    assert report.generated_at is not None
    assert report.delivered_at is not None

    # 訊號真的落庫且被讀出來、送進了 coaching 提示——這才是「可回溯至訊號」
    # 的實際保證；FakeProvider 回的是固定 JSON，不會自己引用提示內容，
    # 所以不能斷言在 report.content 裡，要斷言在送進模型的 system_prompt 裡。
    coaching_calls = client._fake_coaching.calls
    assert len(coaching_calls) == 1
    assert "你今天過得怎麼樣" in coaching_calls[0]["system_prompt"]


def test_重複點擊不重新生成只重送(make_client):
    client = make_client()
    igsid = "IGSID_CLICK_2"
    _post(client, "mid_1", "我今天心情不太好", igsid)
    _post(client, "mid_2", "想找人聊聊", igsid)

    session_id = _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    from app.dialog.coach_report import build_invite_buttons

    payload = build_invite_buttons(session_id)[0]["payload"]
    _post_postback(client, payload, igsid)
    _post_postback(client, payload, igsid)

    # coaching FakeProvider 只該被呼叫一次——第二次點擊直接送出既有內容
    assert len(client._fake_coaching.calls) == 1


def test_訊號尚未就緒時點擊不生成報告(make_client):
    client = make_client()
    igsid = "IGSID_CLICK_3"
    _post(client, "mid_1", "我今天心情不太好", igsid)
    _post(client, "mid_2", "想找人聊聊", igsid)

    session_id = _backdate_session(client._main_module, igsid, minutes=30)

    # 直接用一個「訊號未偵測」的假 session_id 模擬點擊邀請時訊號還沒跑完：
    # 呼叫 _handle_coach_report_click 前先清掉 signal_status。
    from app.db.models import Session as SessionModel, SignalStatus
    from app.db.session import db_session

    asyncio.run(client._main_module._run_silence_pass())
    with db_session() as db:
        s = db.get(SessionModel, session_id)
        s.signal_status = None
        db.flush()

    from app.dialog.coach_report import build_invite_buttons

    payload = build_invite_buttons(session_id)[0]["payload"]
    _post_postback(client, payload, igsid)

    report = _get_coach_report(client._main_module, session_id)
    assert report is not None
    assert report.generated_at is None
    assert report.failure_reason == "signals_not_ready"
    assert len(client._fake_coaching.calls) == 0


def test_送達時視窗逾期保留為已生成未送達(make_client):
    """task 4.4：不直接走點擊流程——點擊本身會重開視窗，沒辦法在同一次
    點擊裡同時「剛生成」又「視窗已逾期」。直接呼叫 _deliver_coach_report，
    模擬報告已生成、但負責送達的那個 session 視窗已經過期的情境。
    """
    client = make_client()
    igsid = "IGSID_WINDOW_1"
    _post(client, "mid_1", "我今天心情不太好", igsid)
    session_id = _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    from app.db import repository as repo
    from app.db.session import db_session

    with db_session() as db:
        report = repo.get_or_create_coach_report(db, session_id)
        repo.mark_report_generated(db, report, {"摘要": "測試內容", "做得好": "x", "可調整": "x", "改寫示範": "x", "下次可試": "x"})
        s = db.get(client._main_module.SessionModel, session_id)
        s.window_expires_at = s.window_expires_at - timedelta(hours=48)  # 讓視窗過期
        db.flush()

    asyncio.run(
        client._main_module._deliver_coach_report(session_id, session_id, igsid)
    )

    report = _get_coach_report(client._main_module, session_id)
    assert report.generated_at is not None, "生成的內容不能因為送達失敗被清掉"
    assert report.delivered_at is None


def test_傳送不相關訊息不會自動補送先前未送達的報告(make_client):
    """task 5.1（2026-09-24 產品決策修訂）：報告與當下對話無關時突然送出
    會讓使用者一頭霧水。改為只有使用者明確再次點擊查看回饋才補送，
    任何其他新訊息都不該觸發。
    """
    client = make_client()
    igsid = "IGSID_RESEND_1"
    _post(client, "mid_1", "我今天心情不太好", igsid)
    session_id = _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    from app.db import repository as repo
    from app.db.session import db_session

    with db_session() as db:
        report = repo.get_or_create_coach_report(db, session_id)
        repo.mark_report_generated(
            db,
            report,
            {"摘要": "測試內容", "做得好": "x", "可調整": "x", "改寫示範": "x", "下次可試": "x"},
        )
        # delivered_at 維持 NULL，模擬先前送達失敗

    _post(client, "mid_2", "我又回來了，這跟報告完全無關", igsid)

    report = _get_coach_report(client._main_module, session_id)
    assert report.delivered_at is None, "不相關的新訊息不該觸發自動補送"


def test_再次點擊按鈕補送先前未送達的報告(make_client):
    """使用者主動再次點擊查看回饋，是唯一能觸發補送的途徑（task 5.1）。
    補送不能重新呼叫模型。
    """
    client = make_client()
    igsid = "IGSID_RESEND_2"
    _post(client, "mid_1", "我今天心情不太好", igsid)
    session_id = _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    from app.db import repository as repo
    from app.db.session import db_session

    with db_session() as db:
        report = repo.get_or_create_coach_report(db, session_id)
        repo.mark_report_generated(
            db,
            report,
            {"摘要": "測試內容", "做得好": "x", "可調整": "x", "改寫示範": "x", "下次可試": "x"},
        )
        # delivered_at 維持 NULL，模擬先前送達失敗

    from app.dialog.coach_report import build_invite_buttons

    payload = build_invite_buttons(session_id)[0]["payload"]
    _post_postback(client, payload, igsid)

    report = _get_coach_report(client._main_module, session_id)
    assert report.delivered_at is not None
    # 補送不能重新呼叫模型——generated_at 早就有值，already_generated 分支跳過生成
    assert len(client._fake_coaching.calls) == 0
