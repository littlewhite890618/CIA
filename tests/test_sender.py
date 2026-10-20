"""發送與平台規則測試（tasks 4.1、4.3–4.8）。

對應 openspec/changes/add-ig-persona-chat-mvp/specs/ig-messaging/spec.md
的四個 Requirement：24 小時視窗、禁止對未互動使用者發訊、訊息長度切分
與順序保證、發訊速率配額。

全部不碰網路：InstagramClient 換成假物件，時間以虛擬時鐘推進，
因此「配額釋出後重試」不需要真的等一小時。
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import timedelta

import pytest

from app.channels.instagram import (
    InstagramRateLimited,
    InstagramSendError,
    InstagramWindowExpired,
)
from app.channels.rate_limit import RateLimiter
from app.channels.sender import (
    REASON_NO_INBOUND,
    REASON_QUOTA_WAIT_TOO_LONG,
    REASON_SEND_FAILED,
    REASON_WINDOW_EXPIRED,
    REASON_WINDOW_EXPIRED_BY_PLATFORM,
    MessageSender,
)
from app.channels.splitter import MAX_BYTES, split_message
from app.db import repository as repo
from app.db.models import Message, MessageDirection, RateBudget
from app.db.models import Session as SessionModel
from app.db.models import utcnow

IG_ACCOUNT_ID = "17841400000001"
IGSID = "IGSID_TEST"


# ═══════════════════════════════════════════════════════════
#  4.5 訊息切分
# ═══════════════════════════════════════════════════════════

def _strip_ws(s: str) -> str:
    return "".join(s.split())


# 涵蓋各種會影響位元組長度計算的組合。純中文每字 3 bytes、
# emoji 多為 4 bytes，混合時最容易在邊界出錯。
SPLIT_SAMPLES = [
    "短訊息",
    "你好嗎？" * 300,                        # 純中文，遠超上限
    "Hello world. " * 200,                   # 純 ASCII
    "中文 mixed with English 混雜 " * 100,   # 中英混雜
    "🌿🎉😊" * 200,                          # 全 emoji（4 bytes/字）
    "第一段內容。\n\n第二段內容。\n\n" * 80,  # 有段落分隔
    "無標點的長字串" * 200,                   # 沒有任何自然分隔符，必須硬切
    "a" * 999,
    "a" * 1000,
    "a" * 1001,
]


@pytest.mark.parametrize("text", SPLIT_SAMPLES)
def test_切分_每則都不超過1000bytes(text):
    for chunk in split_message(text):
        assert len(chunk.encode("utf-8")) <= MAX_BYTES


@pytest.mark.parametrize("text", SPLIT_SAMPLES)
def test_切分_內容不遺失也不重複(text):
    """最重要的一項。

    切分邏輯最常見的 bug 是漏字或重複字，而且肉眼幾乎看不出來——
    一段 300 字的回覆少了一個字，人工檢查不可能發現。
    比對時忽略空白，因為切分點兩側的空白會被刻意去除。
    """
    chunks = split_message(text)
    assert _strip_ws("".join(chunks)) == _strip_ws(text)


@pytest.mark.parametrize("text", SPLIT_SAMPLES)
def test_切分_每則都是合法UTF8(text):
    """切在多位元組字元中間會產生亂碼，使用者會看到問號或方框。"""
    for chunk in split_message(text):
        encoded = chunk.encode("utf-8")
        assert encoded.decode("utf-8") == chunk


def test_切分_短訊息不切():
    assert split_message("嗨，今天過得好嗎？") == ["嗨，今天過得好嗎？"]


def test_切分_空內容回傳空清單():
    assert split_message("") == []
    assert split_message("   \n\n  ") == []


def test_切分_優先切在段落邊界():
    # 兩段各約 600 bytes（中文 200 字），合計超過上限，
    # 段落分隔存在時應該正好切在兩段之間。
    para = "這是一段測試內容。" * 22
    chunks = split_message(f"{para}\n\n{para}")
    assert len(chunks) == 2
    assert chunks[0] == para
    assert chunks[1] == para


def test_切分_次選句末標點():
    # 沒有段落分隔，但有句號 —— 切點應落在句號之後，不會把句子斬斷
    text = "這是一個完整的句子。" * 80
    chunks = split_message(text)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert chunk.endswith("。"), f"切點沒落在句末：{chunk[-10:]}"


def test_切分_中文不會被切成半個字():
    """1000 不是 3 的倍數，純中文必然會在某一則的尾端遇到邊界問題。"""
    chunks = split_message("測" * 1000)
    assert len(chunks) > 1
    for chunk in chunks:
        assert set(chunk) == {"測"}, "出現了非預期字元，代表切壞了"


# ═══════════════════════════════════════════════════════════
#  發訊測試共用設施
# ═══════════════════════════════════════════════════════════

class FakeClient:
    """假的 Instagram 客戶端。

    記錄所有呼叫，並可設定前 N 次拋出指定例外，用於驗證退避重試。
    """

    def __init__(self, *, raise_first: Exception | None = None, raise_times: int = 0) -> None:
        self.sent: list[str] = []
        self.actions: list[str] = []
        self._raise_first = raise_first
        self._raise_times = raise_times
        self._counter = 0

    async def send_text(self, igsid: str, text: str) -> str:
        if self._raise_times > 0:
            self._raise_times -= 1
            assert self._raise_first is not None
            raise self._raise_first
        self._counter += 1
        self.sent.append(text)
        return f"mid_{self._counter}"

    async def send_sender_action(self, igsid: str, action: str) -> None:
        self.actions.append(action)

    async def send_button_template(
        self, igsid: str, text: str, buttons: list[dict[str, str]]
    ) -> str:
        self._counter += 1
        self.sent.append(text)
        return f"mid_{self._counter}"


class FakeClock:
    """虛擬時鐘。搭配假的 asyncio.sleep 使用，讓等待瞬間完成但時間有推進。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """裝上虛擬時鐘，並讓 asyncio.sleep 只推進時間、不真的等待。

    沒有這個的話，「配額滿了要等一小時」的測試就得真的跑一小時。
    """
    c = FakeClock()
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds, *args, **kwargs):
        c.now += seconds
        await real_sleep(0)  # 讓出控制權，維持與真實行為一致的協程語意

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return c


@pytest.fixture
def env(monkeypatch, tmp_path):
    """獨立的檔案資料庫，並重載 app.db.session 讓 engine 指向它。

    重載順序的理由同 tests/test_webhook.py：engine 在模組層級建立，
    不重載就會沿用正式的 data/poc.db。
    """
    db_file = tmp_path / "sender.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")

    from app.config import get_settings

    get_settings.cache_clear()

    import app.db.session as session_module

    importlib.reload(session_module)
    session_module.init_db()

    yield session_module

    get_settings.cache_clear()


def _seed(session_module, *, with_inbound: bool = True, window_hours: float | None = 24.0):
    """建立角色、使用者、場次，回傳 (session_id, persona_id)。"""
    with session_module.db_session() as db:
        persona = repo.upsert_persona(
            db, slug="sunny", name="晴晴", gender="female", ig_account_id=IG_ACCOUNT_ID
        )
        user, _link, _new = repo.resolve_user(
            db, persona_id=persona.id, igsid=IGSID, ig_username="tester"
        )
        session, _ = repo.get_or_create_session(db, user.id, persona.id)

        if with_inbound:
            repo.save_inbound_message(
                db, session=session, platform_message_id="mid_in_1", content="嗨"
            )

        session.window_expires_at = (
            None if window_hours is None else utcnow() + timedelta(hours=window_hours)
        )
        db.flush()
        return session.id, persona.id


def _make_sender(client, limiter=None, clock=None, **kwargs):
    limiter = limiter or RateLimiter(limit=1000, clock=clock or (lambda: 0.0))
    kwargs.setdefault("retry_base_seconds", 1.0)
    return MessageSender(client, limiter, **kwargs)


def _messages(session_module, session_id):
    with session_module.db_session() as db:
        rows = db.query(Message).filter(Message.session_id == session_id).order_by(Message.id).all()
        return [(m.direction, m.content, m.platform_message_id, dict(m.meta or {})) for m in rows]


# ═══════════════════════════════════════════════════════════
#  4.3 24 小時視窗
# ═══════════════════════════════════════════════════════════

def test_視窗內_正常送出(env, clock):
    session_id, persona_id = _seed(env)
    client = FakeClient()
    sender = _make_sender(client, clock=clock)

    outcome = asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="嗨")
    )

    assert outcome.ok
    assert client.sent == ["嗨"]


def test_視窗逾期_不呼叫API且標記未送達(env, clock):
    session_id, persona_id = _seed(env, window_hours=-1)  # 一小時前就過期
    client = FakeClient()
    sender = _make_sender(client, clock=clock)

    outcome = asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="嗨")
    )

    assert outcome.blocked_reason == REASON_WINDOW_EXPIRED
    assert client.sent == [], "視窗逾期時絕不能呼叫發訊 API"

    outbound = [m for m in _messages(env, session_id) if m[0] == MessageDirection.OUTBOUND]
    assert len(outbound) == 1
    assert outbound[0][2] is None                      # 沒有平台訊息 ID
    assert outbound[0][3]["delivered"] is False
    assert outbound[0][3]["reason"] == REASON_WINDOW_EXPIRED


def test_視窗欄位為空_視為關閉(env, clock):
    session_id, persona_id = _seed(env, window_hours=None)
    client = FakeClient()
    sender = _make_sender(client, clock=clock)

    outcome = asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="嗨")
    )

    assert outcome.blocked_reason == REASON_WINDOW_EXPIRED
    assert client.sent == []


# ═══════════════════════════════════════════════════════════
#  4.4 禁止對未互動使用者發訊
# ═══════════════════════════════════════════════════════════

def test_無入站紀錄_拒絕發訊(env, clock):
    session_id, persona_id = _seed(env, with_inbound=False)
    client = FakeClient()
    sender = _make_sender(client, clock=clock)

    outcome = asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="嗨")
    )

    assert outcome.blocked_reason == REASON_NO_INBOUND
    assert client.sent == []

    outbound = [m for m in _messages(env, session_id) if m[0] == MessageDirection.OUTBOUND]
    assert outbound[0][3]["reason"] == REASON_NO_INBOUND


# ═══════════════════════════════════════════════════════════
#  4.5 + 4.6 切分與順序
# ═══════════════════════════════════════════════════════════

def test_長訊息_切分後依序送出(env, clock):
    session_id, persona_id = _seed(env)
    client = FakeClient()
    sender = _make_sender(client, clock=clock)

    text = "這是一段測試內容。" * 150   # 約 4000 bytes
    outcome = asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text=text)
    )

    assert outcome.ok
    assert len(client.sent) > 1
    # 送出順序必須與切分順序一致
    assert client.sent == outcome.delivered
    assert _strip_ws("".join(client.sent)) == _strip_ws(text)

    # 每一則都要落庫，且都拿到平台訊息 ID
    outbound = [m for m in _messages(env, session_id) if m[0] == MessageDirection.OUTBOUND]
    assert len(outbound) == len(client.sent)
    assert [m[1] for m in outbound] == client.sent
    assert all(m[2] is not None for m in outbound)


def test_中途失敗_停止送出後續片段(env, clock):
    """只送出前半段比完全不送更糟——使用者會看到沒頭沒尾的一段話。"""
    session_id, persona_id = _seed(env)

    class FailOnSecond(FakeClient):
        async def send_text(self, igsid, text):
            if len(self.sent) >= 1:
                raise InstagramSendError("模擬失敗")
            return await super().send_text(igsid, text)

    client = FailOnSecond()
    sender = _make_sender(client, clock=clock)

    text = "這是一段測試內容。" * 150
    outcome = asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text=text)
    )

    assert outcome.blocked_reason == REASON_SEND_FAILED
    assert len(client.sent) == 1, "第二則失敗後不應繼續送第三則"


# ═══════════════════════════════════════════════════════════
#  4.1 輸入狀態提示
# ═══════════════════════════════════════════════════════════

def test_送出期間顯示輸入中(env, clock):
    session_id, persona_id = _seed(env)
    client = FakeClient()
    sender = _make_sender(client, clock=clock)

    asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="嗨")
    )

    assert client.actions == ["typing_on", "typing_off"]


def test_被阻擋時不顯示輸入中(env, clock):
    """視窗已過期卻顯示「輸入中」，使用者會等一則永遠不會來的訊息。"""
    session_id, persona_id = _seed(env, window_hours=-1)
    client = FakeClient()
    sender = _make_sender(client, clock=clock)

    asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="嗨")
    )

    assert client.actions == []


# ═══════════════════════════════════════════════════════════
#  4.7 發訊速率配額
# ═══════════════════════════════════════════════════════════

def test_配額充足_立即送出(env, clock):
    session_id, persona_id = _seed(env)
    client = FakeClient()
    limiter = RateLimiter(limit=5, window_seconds=3600, clock=clock)
    sender = _make_sender(client, limiter=limiter, clock=clock)

    asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="嗨")
    )

    assert clock.now == 0.0, "配額充足時不應該有任何等待"
    assert limiter.current_count(str(persona_id)) == 1


def test_配額用盡_暫緩後仍送出且不丟訊息(env, clock):
    """spec 要求「暫緩該則訊息並於配額釋出後重試，不丟棄訊息」。"""
    session_id, persona_id = _seed(env)
    client = FakeClient()
    limiter = RateLimiter(limit=2, window_seconds=3600, clock=clock)
    sender = _make_sender(
        client, limiter=limiter, clock=clock, max_wait_seconds=7200
    )

    text = "這是一段測試內容。" * 150   # 會切成 4 則以上
    outcome = asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text=text)
    )

    chunks = split_message(text)
    assert outcome.ok
    assert len(client.sent) == len(chunks), "超出配額的訊息被丟掉了"
    assert client.sent == chunks
    assert clock.now > 0, "配額用盡時應該有等待發生"


def test_等待時間超過上限_放棄該則並記錄(env, clock):
    session_id, persona_id = _seed(env)
    client = FakeClient()
    limiter = RateLimiter(limit=1, window_seconds=3600, clock=clock)
    sender = _make_sender(
        client, limiter=limiter, clock=clock, max_wait_seconds=10
    )

    text = "這是一段測試內容。" * 150
    outcome = asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text=text)
    )

    # 原因要具體到「配額等待超限」，不能混進籠統的發訊失敗——
    # 兩者的處置完全不同：前者調高上限或降低發訊量就能解決。
    assert outcome.blocked_reason == REASON_QUOTA_WAIT_TOO_LONG
    assert len(client.sent) == 1, "配額只夠一則，其餘應放棄而非無限等待"

    outbound = [m for m in _messages(env, session_id) if m[0] == MessageDirection.OUTBOUND]
    assert outbound[-1][3]["reason"] == REASON_QUOTA_WAIT_TOO_LONG


# ═══════════════════════════════════════════════════════════
#  4.8 速率限制退避重試
# ═══════════════════════════════════════════════════════════

def test_速率限制錯誤_退避後重試成功(env, clock):
    session_id, persona_id = _seed(env)
    client = FakeClient(raise_first=InstagramRateLimited("code 613"), raise_times=2)
    sender = _make_sender(client, clock=clock, max_retries=3, retry_base_seconds=2.0)

    outcome = asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="嗨")
    )

    assert outcome.ok
    assert outcome.rate_limited_hits == 2
    assert client.sent == ["嗨"]
    # 退避為 base × 2^n：2 + 4 = 6 秒
    assert clock.now == pytest.approx(6.0)


def test_速率限制_寫入rate_budget觀測資料(env, clock):
    """這張表是驗證「200 則／小時」假設的唯一實測資料來源。"""
    session_id, persona_id = _seed(env)
    client = FakeClient(raise_first=InstagramRateLimited("code 4"), raise_times=1)
    sender = _make_sender(client, clock=clock)

    asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="嗨")
    )

    with env.db_session() as db:
        budget = db.query(RateBudget).filter(RateBudget.persona_id == persona_id).one()
        assert budget.rate_limited_count == 1
        assert budget.sent_count == 1


def test_重試次數用盡_放棄並標記未送達(env, clock):
    session_id, persona_id = _seed(env)
    client = FakeClient(raise_first=InstagramRateLimited("code 613"), raise_times=99)
    sender = _make_sender(client, clock=clock, max_retries=2)

    outcome = asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="嗨")
    )

    assert outcome.blocked_reason == REASON_SEND_FAILED
    assert client.sent == []
    outbound = [m for m in _messages(env, session_id) if m[0] == MessageDirection.OUTBOUND]
    assert outbound[0][3]["reason"] == REASON_SEND_FAILED


def test_平台判定視窗逾期_記錄為與我方不一致(env, clock):
    """我方檢查放行、Meta 卻回 subcode 2534022。

    2026-09-10 實測確認 Meta 對逾期發訊回傳
    `code 10 / error_subcode 2534022`（「這則訊息並未在允許的期間內傳送」）。
    正常情況下 sender 的視窗檢查會先擋下，走到這裡代表兩邊的判斷不一致
    （時鐘偏移，或 Meta 的計算方式與我們假設的不同）。這種落差必須能從
    觀測資料看出來，不能被埋進籠統的 send_api_failed。
    """
    session_id, persona_id = _seed(env)   # 視窗在我方看來是開著的
    client = FakeClient(raise_first=InstagramWindowExpired("subcode 2534022"), raise_times=99)
    sender = _make_sender(client, clock=clock, max_retries=3)

    outcome = asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="嗨")
    )

    assert outcome.blocked_reason == REASON_WINDOW_EXPIRED_BY_PLATFORM
    assert outcome.blocked_reason != REASON_WINDOW_EXPIRED, "要與我方主動擋下的原因分開"
    assert clock.now == 0.0, "視窗逾期重試無意義，不該退避等待"

    outbound = [m for m in _messages(env, session_id) if m[0] == MessageDirection.OUTBOUND]
    assert outbound[-1][3]["reason"] == REASON_WINDOW_EXPIRED_BY_PLATFORM


def test_視窗逾期subcode_被正確分類(env, clock):
    """驗證 subcode 2534022 真的會被轉成 InstagramWindowExpired。

    上一個測試直接餵已分類好的例外，所以測不到辨識邏輯本身；
    這個測試從 HTTP 回應層開始，補上那一段。
    """
    import httpx

    from app.channels.instagram import InstagramClient, WINDOW_EXPIRED_SUBCODE

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "message": "這則訊息並未在允許的期間內傳送。",
                    "type": "IGApiException",
                    "code": 10,
                    "error_subcode": WINDOW_EXPIRED_SUBCODE,
                }
            },
        )

    client = InstagramClient("fake_token", "17841400000001")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _run():
        try:
            await client.send_text("IGSID_X", "嗨")
        except InstagramWindowExpired:
            return "window_expired"
        except InstagramSendError:
            return "generic"
        finally:
            await client.aclose()

    assert asyncio.run(_run()) == "window_expired"


def test_一般失敗_不重試(env, clock):
    """權限不足、內容違規這類錯誤重試也不會變好，重試只是徒增風險。"""
    session_id, persona_id = _seed(env)
    client = FakeClient(raise_first=InstagramSendError("HTTP 400"), raise_times=99)
    sender = _make_sender(client, clock=clock, max_retries=3)

    outcome = asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="嗨")
    )

    assert outcome.blocked_reason == REASON_SEND_FAILED
    assert outcome.rate_limited_hits == 0
    assert clock.now == 0.0, "一般失敗不應該有退避等待"


# ═══════════════════════════════════════════════════════════
#  邊界
# ═══════════════════════════════════════════════════════════

def test_空內容_不呼叫任何API(env, clock):
    session_id, persona_id = _seed(env)
    client = FakeClient()
    sender = _make_sender(client, clock=clock)

    outcome = asyncio.run(
        sender.send_reply(session_id=session_id, persona_id=persona_id, igsid=IGSID, text="   ")
    )

    assert not outcome.ok
    assert outcome.blocked_reason is None
    assert client.sent == []
    assert client.actions == []


def test_場次不存在_安全放棄(env, clock):
    _seed(env)
    client = FakeClient()
    sender = _make_sender(client, clock=clock)

    outcome = asyncio.run(
        sender.send_reply(session_id=99999, persona_id=1, igsid=IGSID, text="嗨")
    )

    assert not outcome.ok
    assert client.sent == []


# ═══════════════════════════════════════════════════════════
#  M9：send_button_template（教練報告邀請）沿用 send_reply 的前置檢查
#
#  原本是 send_quick_reply，2026-09-23 真機實測發現 Quick Reply 在
#  Instagram 上不會渲染任何按鈕（API 回 200 但畫面上沒有東西），
#  改用 Button Template 後真機驗證按鈕正常顯示。
# ═══════════════════════════════════════════════════════════


def test_button_template_視窗內_正常送出並記錄(env, clock):
    session_id, persona_id = _seed(env)
    client = FakeClient()
    sender = _make_sender(client, clock=clock)

    outcome = asyncio.run(
        sender.send_button_template(
            session_id=session_id,
            persona_id=persona_id,
            igsid=IGSID,
            text="想看今天的回饋嗎？",
            buttons=[{"type": "postback", "title": "看回饋", "payload": "X"}],
        )
    )

    assert outcome.ok
    assert client.sent == ["想看今天的回饋嗎？"]

    outbound = [m for m in _messages(env, session_id) if m[0] == MessageDirection.OUTBOUND]
    assert len(outbound) == 1
    assert outbound[0][2] == "mid_1"


def test_button_template_視窗逾期時不呼叫API(env, clock):
    session_id, persona_id = _seed(env, window_hours=-1)
    client = FakeClient()
    sender = _make_sender(client, clock=clock)

    outcome = asyncio.run(
        sender.send_button_template(
            session_id=session_id,
            persona_id=persona_id,
            igsid=IGSID,
            text="想看今天的回饋嗎？",
            buttons=[{"type": "postback", "title": "看回饋", "payload": "X"}],
        )
    )

    assert outcome.blocked_reason == REASON_WINDOW_EXPIRED
    assert client.sent == [], "視窗逾期時絕不能呼叫發訊 API"


def test_button_template_未互動使用者不發送(env, clock):
    session_id, persona_id = _seed(env, with_inbound=False)
    client = FakeClient()
    sender = _make_sender(client, clock=clock)

    outcome = asyncio.run(
        sender.send_button_template(
            session_id=session_id,
            persona_id=persona_id,
            igsid=IGSID,
            text="想看今天的回饋嗎？",
            buttons=[{"type": "postback", "title": "看回饋", "payload": "X"}],
        )
    )

    assert outcome.blocked_reason == REASON_NO_INBOUND
    assert client.sent == []
