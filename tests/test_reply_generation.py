"""角色回覆生成、邊界反應、延遲期間插話（tasks 7.6、7.7、7.9）。

對應 openspec/changes/add-ig-persona-chat-mvp/specs/persona-chat/spec.md
的「角色回覆生成」「擬真對話節奏」Scenario，以及 design.md 風險登記
「擬真延遲期間使用者又發訊，導致回覆已不合時宜」。

架構與 test_webhook.py 相同（走完整 webhook → 背景任務管線，從 DB
讀回結果），額外在 TestClient 建立後把 llm_router 換成 FakeProvider，
確保不連外網、不消耗真實額度、結果可預期。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.llm.base import LLMMessage, LLMProvider, LLMResponse
from app.llm.router import LLMRouter

APP_SECRET = "test_app_secret"
VERIFY_TOKEN = "test_verify_token"
IG_ACCOUNT_ID = "17841400000001"


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class FakeDialogProvider(LLMProvider):
    """記錄每次呼叫收到的訊息，回傳預先排好的回覆序列。

    用來驗證 7.6：呼叫端是否把正確的 system_prompt 與對話歷史傳進來。
    """

    def __init__(self, replies: list[str] | None = None) -> None:
        self.calls: list[dict] = []
        self._replies = list(replies or ["嗯嗯，我懂你的感覺呢。"])

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-dialog-1"

    async def generate(self, *, system_prompt, messages, temperature=0.9, max_output_tokens=1024):
        self.calls.append({"system_prompt": system_prompt, "messages": list(messages)})
        text = self._replies[min(len(self.calls) - 1, len(self._replies) - 1)]
        return LLMResponse(text=text, provider="fake", model=self.model, elapsed_ms=1)


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    """回傳一個工廠函式：呼叫後給一個已接上 fake_provider 的 TestClient。

    與 test_webhook.py 的 client fixture採同一套重載與假物件手法
    （見該檔詳細註解），差異只在於這裡額外注入 FakeDialogProvider。
    """

    def _make(
        replies: list[str] | None = None,
        topics_reply: str = "",
        settings_link: bool = False,
    ):
        import importlib

        db_file = tmp_path / "test.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        # 明確指定，不受本機 .env 影響：設定頁功能（M13 任務 5.1）預設關閉；
        # 對外網址留空也讓登入 cookie 不帶 Secure，TestClient 走 http 才送得回來
        monkeypatch.setenv("SETTINGS_LINK_SECRET", "s" * 40 if settings_link else "")
        monkeypatch.setenv(
            "PUBLIC_BASE_URL", "https://example.ngrok-free.dev" if settings_link else ""
        )
        monkeypatch.setenv("IG_APP_SECRET", APP_SECRET)
        monkeypatch.setenv("META_APP_SECRET", "不該被用來驗章的密鑰")
        monkeypatch.setenv("META_WEBHOOK_VERIFY_TOKEN", VERIFY_TOKEN)
        monkeypatch.setenv("IG_ACCESS_TOKEN", "fake_token")
        monkeypatch.setenv("IG_ACCOUNT_ID", IG_ACCOUNT_ID)
        # .env 裡有正式帳號名稱；測試一律當作未設定，「回到聊天」連到收件匣
        monkeypatch.setenv("IG_USERNAME", "")
        monkeypatch.setenv("GEMINI_API_KEY", "")  # 不建立真的 GeminiProvider
        monkeypatch.setenv("WEBHOOK_SKIP_SIGNATURE_CHECK", "false")

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
            return None  # task 6.1：見 test_webhook.py 同名假物件的說明

        monkeypatch.setattr(main_module.InstagramClient, "send_text", _fake_send_text)
        monkeypatch.setattr(main_module.InstagramClient, "send_sender_action", _fake_sender_action)
        monkeypatch.setattr(main_module.InstagramClient, "fetch_username", _fake_fetch_username)

        import app.dialog.delivery as delivery_module

        async def _no_sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(delivery_module, "DEFAULT_SLEEP", _no_sleep)

        client = TestClient(main_module.app)
        client.__enter__()

        # 7.6：換成假供應商，不連外網、內容可預期。
        # 6.7 的話題萃取走 analysis 路徑，同樣需要假供應商——
        # topics_reply 預設空字串，代表「萃取不到明確話題」，
        # 需要驗證萃取行為的測試自行指定。
        fake_provider = FakeDialogProvider(replies)
        fake_analysis_provider = FakeDialogProvider([topics_reply] if topics_reply else [""])
        main_module.llm_router = LLMRouter({
            "dialog": fake_provider,
            "analysis": fake_analysis_provider,
        })

        client._main_module = main_module  # type: ignore[attr-defined]
        client._fake_provider = fake_provider  # type: ignore[attr-defined]
        client._fake_analysis_provider = fake_analysis_provider  # type: ignore[attr-defined]
        return client

    yield _make

    from app.config import get_settings

    get_settings.cache_clear()


def _message_payload(mid: str, text: str, igsid: str) -> dict:
    return {
        "object": "instagram",
        "entry": [
            {
                "id": IG_ACCOUNT_ID,
                "time": 1234567890,
                "messaging": [
                    {
                        "sender": {"id": igsid},
                        "recipient": {"id": IG_ACCOUNT_ID},
                        "timestamp": 1234567890,
                        "message": {"mid": mid, "text": text},
                    }
                ],
            }
        ],
    }


async def _simulate_inbound_during_delay(main_module, mid: str, text: str, igsid: str) -> None:
    """在背景任務的 sleep 期間，模擬「使用者又發了一則訊息」。

    不能用 client.post()：此時已身處事件迴圈執行緒內（正在跑前一則訊息
    的背景任務），TestClient 的同步 HTTP 介面靠 anyio 的 blocking portal
    從*外部*執行緒呼叫事件迴圈，同執行緒內重入會直接拋錯
    （"This method cannot be called from the event loop thread"）。

    改為直接呼叫 _handle_event 落庫，再手動執行它排入的背景任務——
    這正是 Starlette 在正常請求週期裡對 BackgroundTasks 做的事，
    只是這裡手動觸發，讓我們能在另一個背景任務「內部」安全地做到。
    """
    from fastapi import BackgroundTasks

    from app.channels.instagram import parse_webhook_payload

    payload = _message_payload(mid, text, igsid)
    event = parse_webhook_payload(payload)[0]

    background_tasks = BackgroundTasks()
    await main_module._handle_event(event, background_tasks)
    await background_tasks()


def _post(client: TestClient, mid: str, text: str, igsid: str):
    body = json.dumps(_message_payload(mid, text, igsid)).encode()
    return client.post(
        "/webhook", content=body,
        headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"},
    )


def _outbound_texts(main_module, igsid: str) -> list[str]:
    from app.db.session import db_session
    from app.db.models import Message, MessageDirection, Persona
    from app.db.models import Session as SessionModel
    from app.db.models import UserPersonaLink

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
            .one()
        )
        rows = (
            db.query(Message)
            .filter(Message.session_id == session.id, Message.direction == MessageDirection.OUTBOUND)
            .order_by(Message.id)
            .all()
        )
        return [m.content for m in rows]


# ═══════════════════════════════════════════════════════════
#  7.6：角色回覆生成
# ═══════════════════════════════════════════════════════════

def test_回覆內容來自LLM而非固定字串(make_client):
    client = make_client(replies=["今天過得還不錯，你呢？☺️"])
    resp = _post(client, "mid_1", "嗨嗨", "IGSID_1")
    assert resp.status_code == 200

    texts = _outbound_texts(client._main_module, "IGSID_1")
    assert any("今天過得還不錯" in t for t in texts)


def test_系統提示包含人設卡的身分與語言風格(make_client):
    client = make_client()
    _post(client, "mid_1", "嗨嗨", "IGSID_1")

    call = client._fake_provider.calls[0]
    assert "晴晴" in call["system_prompt"]
    assert "幼兒園老師" in call["system_prompt"]


def test_上下文包含先前的對話歷史(make_client):
    client = make_client(replies=["第一句回覆。", "第二句回覆，接續前文。"])
    _post(client, "mid_1", "我今天心情不好", "IGSID_1")
    _post(client, "mid_2", "因為工作的事", "IGSID_1")

    second_call = client._fake_provider.calls[1]
    contents = [m.content for m in second_call["messages"]]
    assert "我今天心情不好" in contents
    assert "因為工作的事" in contents
    # 第一輪的回覆也該在歷史裡，讓模型知道自己說過什麼
    assert any("第一句回覆" in c for c in contents)


def test_對話歷史依方向對應到正確角色(make_client):
    client = make_client(replies=["回覆內容。"])
    _post(client, "mid_1", "使用者說的話", "IGSID_1")

    call = client._fake_provider.calls[0]
    roles = {m.content: m.role for m in call["messages"]}
    assert roles["使用者說的話"] == "user"


def test_缺金鑰時走降級不呼叫LLM(make_client):
    """llm_router 為 None 時（金鑰未設），直接用降級語句，不嘗試呼叫。"""
    client = make_client()
    client._main_module.llm_router = None  # 模擬金鑰未設定的狀態

    resp = _post(client, "mid_1", "嗨嗨", "IGSID_1")
    assert resp.status_code == 200

    from app.llm import DIALOG_FALLBACKS

    texts = _outbound_texts(client._main_module, "IGSID_1")
    assert any(any(fb in t for fb in DIALOG_FALLBACKS) for t in texts)


# ═══════════════════════════════════════════════════════════
#  7.7：邊界反應（結構層級——實際行為驗證屬 9.4，需真實 LLM 對話）
# ═══════════════════════════════════════════════════════════

def test_系統提示包含變冷淡話題清單(make_client):
    client = make_client()
    _post(client, "mid_1", "嗨嗨", "IGSID_1")

    prompt = client._fake_provider.calls[0]["system_prompt"]
    assert "評論她的身材或外貌" in prompt


def test_系統提示包含絕不接話題清單(make_client):
    client = make_client()
    _post(client, "mid_1", "嗨嗨", "IGSID_1")

    prompt = client._fake_provider.calls[0]["system_prompt"]
    assert "索取電話" in prompt or "電話" in prompt


def test_系統提示包含回暖指示(make_client):
    """回暖是 7.7 補上的段落——原本的提示只交代怎麼變冷淡，沒交代怎麼恢復。"""
    client = make_client()
    _post(client, "mid_1", "嗨嗨", "IGSID_1")

    prompt = client._fake_provider.calls[0]["system_prompt"]
    assert "回暖" in prompt
    assert "不要記仇" in prompt or "不要維持冷淡" in prompt


# ═══════════════════════════════════════════════════════════
#  7.9：延遲期間使用者又發訊
# ═══════════════════════════════════════════════════════════

def test_延遲期間插話則放棄過時回覆(make_client, monkeypatch):
    """核心情境：使用者在等待期間又發了一則新訊息，準備中的回覆應放棄，
    不送出任何內容——由新訊息自己觸發的回覆去處理，那次會自然帶入
    這兩則訊息的完整上下文（見 test_上下文包含先前的對話歷史）。
    """
    # 兩句刻意不同，才能從 outbound 內容分辨出「是哪一次回覆送出的」。
    client = make_client(replies=["第一次回覆的內容。", "第二次的正常回覆。"])
    igsid = "IGSID_STALE"

    import app.dialog.delivery as delivery_module

    # 模擬「使用者在延遲期間又發訊」：sleep 被呼叫（代表延遲開始跑）時，
    # 直接把第二則訊息寫進資料庫——時序上等同於使用者趁角色還在
    # 「輸入中」的空檔又打了一句話。
    #
    # 只觸發一次：DEFAULT_SLEEP 在整個測試期間都被替換掉，若不設旗標，
    # 第二則訊息自己那次回覆的 sleep 也會重入這個函式，等於「使用者」
    # 又發了第三、第四…則訊息，形成無窮遞迴（雖然重複 mid 會被去重擋下，
    # 但徒增雜訊，也偏離「只插話一次」的測試情境）。
    triggered = False

    async def _sleep_then_insert_second_message(seconds: float) -> None:
        nonlocal triggered
        if not triggered:
            triggered = True
            await _simulate_inbound_during_delay(
                client._main_module, "mid_interrupt", "等等我還有話說", igsid
            )

    monkeypatch.setattr(delivery_module, "DEFAULT_SLEEP", _sleep_then_insert_second_message)

    first = json.dumps(_message_payload("mid_first", "我等你回覆", igsid)).encode()
    client.post(
        "/webhook", content=first,
        headers={"X-Hub-Signature-256": sign(first), "Content-Type": "application/json"},
    )

    texts = _outbound_texts(client._main_module, igsid)
    # 第一則回覆因過時被放棄；第二則訊息自己觸發的回覆正常送出。
    assert not any("第一次回覆的內容" in t for t in texts)
    assert any("第二次的正常回覆" in t for t in texts)
    assert len(texts) == 1


def test_插話後的新回覆帶有完整上下文(make_client, monkeypatch):
    """接續上一項：新訊息觸發的回覆，上下文須同時包含兩則使用者訊息。"""
    client = make_client(replies=["第一次回覆。", "看到兩則訊息後的回覆。"])
    igsid = "IGSID_STALE_2"

    import app.dialog.delivery as delivery_module

    triggered = False

    async def _sleep_then_insert_second_message(seconds: float) -> None:
        nonlocal triggered
        if not triggered:
            triggered = True
            await _simulate_inbound_during_delay(
                client._main_module, "mid_interrupt_2", "後來想到的補充", igsid
            )

    monkeypatch.setattr(delivery_module, "DEFAULT_SLEEP", _sleep_then_insert_second_message)

    first = json.dumps(_message_payload("mid_first_2", "原本想說的話", igsid)).encode()
    client.post(
        "/webhook", content=first,
        headers={"X-Hub-Signature-256": sign(first), "Content-Type": "application/json"},
    )

    # 最後一次呼叫 LLM 時（第二則訊息觸發的那次），上下文要同時看得到
    # 兩則使用者訊息，而不是只看到自己那一句。
    last_call = client._fake_provider.calls[-1]
    contents = [m.content for m in last_call["messages"]]
    assert "原本想說的話" in contents
    assert "後來想到的補充" in contents


def test_沒有插話時正常送出不受影響(make_client):
    """反面案例：確認 is_stale 機制不會誤判正常情況為過時。"""
    client = make_client(replies=["正常的回覆內容。"])
    resp = _post(client, "mid_1", "嗨嗨", "IGSID_NORMAL")
    assert resp.status_code == 200

    texts = _outbound_texts(client._main_module, "IGSID_NORMAL")
    assert any("正常的回覆內容" in t for t in texts)


# ═══════════════════════════════════════════════════════════
#  6.4 靜默輪詢 ／ 6.7 話題萃取（完整鏈路）
# ═══════════════════════════════════════════════════════════

def _backdate_session(main_module, igsid: str, minutes: int) -> None:
    """把某個 igsid 目前進行中的 session 最後活動時間推回過去，
    模擬「已經靜默這麼久」，讓 find_silent_sessions 掃得到。
    """
    from datetime import timedelta

    from app.db.models import Persona, Session as SessionModel, UserPersonaLink, utcnow
    from app.db.session import db_session

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
            .one()
        )
        session.last_message_at = utcnow() - timedelta(minutes=minutes)


def _session_state_and_topics(igsid: str) -> tuple[str, list[str]]:
    from app.db.models import Persona, Session as SessionModel, UserPersonaLink
    from app.db.session import db_session

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
        return session.state, list(link.topic_memory or [])


def test_靜默逾門檻的session被標記結束(make_client):
    client = make_client(replies=["嗨，你好呀。"])
    igsid = "IGSID_SILENCE_1"
    _post(client, "mid_1", "我是幼兒園老師", igsid)

    _backdate_session(client._main_module, igsid, minutes=30)  # 門檻預設 25 分鐘

    asyncio.run(client._main_module._run_silence_pass())

    state, _topics = _session_state_and_topics(igsid)
    assert state == "ended"


def test_session結束時萃取話題並存入連結(make_client):
    client = make_client(replies=["嗨，你好呀。"], topics_reply="幼兒園老師\n河濱騎車")
    igsid = "IGSID_SILENCE_2"
    _post(client, "mid_1", "我是幼兒園老師，喜歡騎腳踏車", igsid)

    _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    _state, topics = _session_state_and_topics(igsid)
    assert topics == ["幼兒園老師", "河濱騎車"]

    # 萃取確實走 analysis 路徑，不是 dialog
    assert client._fake_analysis_provider.calls
    assert len(client._fake_analysis_provider.calls[0]["messages"]) >= 1


def test_下次見面時系統提示包含先前話題(make_client):
    """完整鏈路：session 結束 → 萃取話題 → 新 session 的系統提示看得到。"""
    client = make_client(
        replies=["第一輪的回覆。", "還記得你說過的事。"],
        topics_reply="幼兒園老師",
    )
    igsid = "IGSID_SILENCE_3"

    _post(client, "mid_1", "我是幼兒園老師", igsid)
    _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    # 使用者回訪：因為前一個 session 已結束，這會開啟新的 session
    _post(client, "mid_2", "嗨，好久不見", igsid)

    last_call = client._fake_provider.calls[-1]
    assert "幼兒園老師" in last_call["system_prompt"]
    assert "之前聊過的話題" in last_call["system_prompt"]


def test_session結束時觸發訊號偵測(make_client):
    """task 8.2：訊號偵測接在話題萃取之後，同走 analysis 路徑。"""
    client = make_client(replies=["嗨，你好呀。"])
    igsid = "IGSID_SILENCE_SIGNALS"
    _post(client, "mid_1", "你週末都去哪玩？", igsid)

    _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    calls = client._fake_analysis_provider.calls
    assert len(calls) == 2  # 話題萃取 + 訊號偵測
    assert "地雷" in calls[1]["system_prompt"]
    assert "使用者：你週末都去哪玩？" in calls[1]["messages"][0].content


def test_session結束時偵測到的訊號寫入資料庫(make_client):
    """task 8.3：偵測結果落庫，含由對照表補上的嚴重程度與維度。"""
    from app.db.models import Message, MessageDirection, RealtimeSignal
    from app.db.session import db_session

    client = make_client(replies=["嗨，你好呀。"])
    igsid = "IGSID_SILENCE_SIGNALS_DB"
    _post(client, "mid_1", "加個 LINE 吧", igsid)

    with db_session() as db:
        inbound = db.query(Message).filter(Message.direction == MessageDirection.INBOUND).one()
        inbound_id = inbound.id

    # 第一次呼叫是話題萃取，第二次是訊號偵測
    client._fake_analysis_provider._replies = [
        "",
        json.dumps([{"message_id": inbound_id, "item": "L1", "reason": "要 LINE"}]),
    ]
    _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    with db_session() as db:
        [signal] = db.query(RealtimeSignal).all()
        assert signal.message_id == inbound_id
        assert signal.signal_type == "landmine"
        assert signal.severity == "high"
        assert signal.dimension == "分寸與界線"
        assert signal.reason == "要 LINE"


def _session_signal_status(igsid: str) -> tuple[str | None, str | None, int]:
    from app.db.models import Persona, RealtimeSignal, Session as SessionModel, UserPersonaLink
    from app.db.session import db_session

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
        count = db.query(RealtimeSignal).filter(RealtimeSignal.session_id == session.id).count()
        return session.signal_status, session.signal_error, count


def test_偵測成功時場次標記ok(make_client):
    """task 8.4：模型回傳合法的空陣列，代表這場沒有訊號，不是失敗。"""
    client = make_client(replies=["嗨，你好呀。"])
    igsid = "IGSID_SIGNAL_OK"
    _post(client, "mid_1", "嗨嗨", igsid)

    client._fake_analysis_provider._replies = ["", "[]"]
    _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    status, error, count = _session_signal_status(igsid)
    assert (status, error, count) == ("ok", None, 0)


def test_偵測失敗時標記failed且不寫入訊號(make_client):
    """task 8.4：失敗與「沒有訊號」要能分辨，且記下原因供重跑。"""
    client = make_client(replies=["嗨，你好呀。"])
    igsid = "IGSID_SIGNAL_FAIL"
    _post(client, "mid_1", "嗨嗨", igsid)

    client._fake_analysis_provider._replies = ["", "這不是 JSON"]
    _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    status, error, count = _session_signal_status(igsid)
    assert status == "failed"
    assert "SignalParseError" in error
    assert count == 0


def test_重跑失敗的場次成功後改標ok並寫入訊號(make_client):
    """task 8.4：scripts/rerun_signals.py 呼叫的就是 run_signal_detection。"""
    from app.db import repository as repo
    from app.db.models import Message, MessageDirection
    from app.db.session import db_session
    from app.dialog.signal_job import run_signal_detection

    client = make_client(replies=["嗨，你好呀。"])
    igsid = "IGSID_SIGNAL_RERUN"
    _post(client, "mid_1", "加個 LINE 吧", igsid)

    client._fake_analysis_provider._replies = ["", "這不是 JSON"]
    _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())
    assert _session_signal_status(igsid)[0] == "failed"

    with db_session() as db:
        [session_id] = repo.failed_signal_session_ids(db)
        inbound_id = db.query(Message).filter(Message.direction == MessageDirection.INBOUND).one().id

    rerun_provider = FakeDialogProvider([json.dumps([{"message_id": inbound_id, "item": "L1", "reason": "要 LINE"}])])
    result = asyncio.run(run_signal_detection(
        LLMRouter({"analysis": rerun_provider}),
        session_id,
        client._main_module.persona_cards,
        burst_window_seconds=30,
        burst_min_messages=3,
    ))

    assert result.ok is True
    assert _session_signal_status(igsid) == ("ok", None, 1)
    with db_session() as db:
        assert repo.failed_signal_session_ids(db) == []


def test_一場結束時出錯不影響同一輪的其他場次(make_client, monkeypatch):
    """task 8.4：每場各自隔離，不因前一場拋出例外就跳過後面的場次。"""
    client = make_client(replies=["嗨，你好呀。"])
    main_module = client._main_module
    for igsid in ("IGSID_ISO_1", "IGSID_ISO_2"):
        _post(client, f"mid_{igsid}", "嗨嗨", igsid)
        _backdate_session(main_module, igsid, minutes=30)

    original = main_module._close_session_and_analyze
    handled: list[int] = []

    async def _flaky(session_id: int) -> None:
        handled.append(session_id)
        if len(handled) == 1:
            raise RuntimeError("第一場出錯")
        await original(session_id)

    monkeypatch.setattr(main_module, "_close_session_and_analyze", _flaky)
    asyncio.run(main_module._run_silence_pass())

    assert len(handled) == 2
    state, _topics = _session_state_and_topics("IGSID_ISO_2")
    assert state == "ended"


def test_未靜默的session不受輪詢影響(make_client):
    """反面案例：確認輪詢不會誤傷還在進行中的正常對話。"""
    client = make_client(replies=["嗨，你好呀。"])
    igsid = "IGSID_SILENCE_NORMAL"
    _post(client, "mid_1", "嗨嗨", igsid)
    # 不 backdate，維持剛剛才活動的狀態

    asyncio.run(client._main_module._run_silence_pass())

    state, _topics = _session_state_and_topics(igsid)
    assert state == "active"


def test_萃取不到話題時不影響session正常結束(make_client):
    """topics_reply 預設空字串 → extract_topics 回傳空清單，
    session 仍應正常標記結束，只是沒有話題可記。"""
    client = make_client(replies=["嗨，你好呀。"])  # topics_reply 預設 ""
    igsid = "IGSID_SILENCE_NO_TOPIC"
    _post(client, "mid_1", "嗨嗨", igsid)

    _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    state, topics = _session_state_and_topics(igsid)
    assert state == "ended"
    assert topics == []
