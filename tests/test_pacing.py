"""擬真對話節奏測試（task 7.8）。

對應 openspec/changes/add-ig-persona-chat-mvp/specs/persona-chat/spec.md
的 Requirement「擬真對話節奏」兩個 Scenario：回覆延遲、訊息拆分。

全部不碰網路也不碰時間：InstagramClient 與 MessageSender 換成假物件，
sleep 換成記錄器。因此「等 20 秒才送出」這件事的測試是毫秒級的。
"""

from __future__ import annotations

import asyncio
import random

import pytest

from app.channels.sender import REASON_WINDOW_EXPIRED, SendOutcome
from app.dialog.delivery import TYPING_LEAD_SECONDS, send_paced_reply
from app.dialog.pacing import plan_reply, split_into_bubbles
from app.personas.schema import DelaySeconds, PersonaRuntimeParams

TWO_SENTENCES = "今天班上那個孩子又哭了，我陪他坐了好久。後來他跟我說他只是想媽媽，我聽了有點鼻酸呀。"
ONE_SENTENCE = "今天班上那個孩子又哭了，我陪他坐了好久，後來他跟我說他只是想媽媽。"

# 帶自然段落（單個換行）的內容，用來觸發拆分——拆分已不再由 runtime 機率
# 控制，而是依 LLM 輸出本身的段落結構決定，見 app/dialog/pacing.py。
TWO_PARAGRAPHS = (
    "今天班上那個孩子又哭了，我陪他坐了好久，他說他只是想媽媽了。\n"
    "後來安撫了一陣子他才慢慢平靜下來，我聽了也有點鼻酸呀。"
)


def _runtime(*, min_s=8, max_s=20, typing=True) -> PersonaRuntimeParams:
    return PersonaRuntimeParams(
        reply_delay_seconds=DelaySeconds(min=min_s, max=max_s),
        typing_indicator=typing,
    )


# 晴晴的實際設定（app/personas/cards/sunny.yaml）
SUNNY_RUNTIME = _runtime()


def _strip_ws(s: str) -> str:
    return "".join(s.split())


# ═══════════════════════════════════════════════════════════
#  Scenario: 回覆延遲
# ═══════════════════════════════════════════════════════════

def test_延遲永遠落在人設卡設定的區間內():
    # 跑多個種子而非單一種子：均勻分布要驗的是「不會跑出界」，
    # 單一種子只能證明那一次沒跑出界。
    for seed in range(200):
        plan = plan_reply(TWO_SENTENCES, SUNNY_RUNTIME, random.Random(seed))
        assert 8 <= plan.lead_delay <= 20


def test_不同角色的延遲區間各自生效():
    fast = plan_reply("嗨嗨", _runtime(min_s=1, max_s=2), random.Random(7))
    slow = plan_reply("嗨嗨", _runtime(min_s=30, max_s=60), random.Random(7))
    assert 1 <= fast.lead_delay <= 2
    assert 30 <= slow.lead_delay <= 60


def test_延遲不為零():
    """秒回是最直接的 AI 破綻，這是 7.8 存在的理由。"""
    plan = plan_reply(TWO_SENTENCES, SUNNY_RUNTIME, random.Random(0))
    assert plan.lead_delay > 0


# ═══════════════════════════════════════════════════════════
#  Scenario: 訊息拆分
#
#  拆分不再由 runtime 機率控制，而是依 LLM 輸出本身的自然段落
#  （單個換行）決定，見 app/dialog/pacing.py 的 split_into_bubbles()。
# ═══════════════════════════════════════════════════════════

def test_只有一句話時必定單則():
    for seed in range(50):
        plan = plan_reply(ONE_SENTENCE, SUNNY_RUNTIME, random.Random(seed))
        assert len(plan.bubbles) == 1


def test_句號處拆成兩則():
    assert len(split_into_bubbles(TWO_SENTENCES)) == 2


def test_問號前的陳述與反問拆成兩則且emoji跟著反問():
    text = "我通常會去河濱騎腳踏車，讓風吹一吹，心情會好一點。你平常壓力大時，會想做些什麼來放鬆嗎？☺️"
    head, tail = split_into_bubbles(text)
    assert head.endswith("好一點。")
    assert tail.startswith("你平常") and tail.endswith("嗎？☺️")


def test_刪節號不算句末():
    text = "嗯嗯，真的覺得很不公平呢……明明不是你的問題，卻要承受這些，當下被罵的時候你心裡很委屈嗎？"
    assert split_into_bubbles(text) == [text]


def test_三句以上只在最後一句前切():
    text = "今天班上那個孩子又哭了。我陪他坐了好久好久。後來他終於平靜下來了。你今天工作還順利嗎？"
    bubbles = split_into_bubbles(text)
    assert len(bubbles) == 2
    assert bubbles[1] == "你今天工作還順利嗎？"


def test_有兩段自然段落時必定兩則():
    for seed in range(50):
        plan = plan_reply(TWO_PARAGRAPHS, SUNNY_RUNTIME, random.Random(seed))
        assert len(plan.bubbles) == 2


def test_三段以上自然段落合併為兩則():
    text = (
        "第一段內容講今天班上發生的事情，那個孩子又哭了一次。\n"
        "第二段補充一些細節和感受，說她陪著坐了好久才安撫下來。\n"
        "第三段問對方今天過得如何，工作會不會也很累呢。"
    )
    bubbles = split_into_bubbles(text)
    assert len(bubbles) == 2


def test_拆分後內容不增不減不亂序():
    plan = plan_reply(TWO_PARAGRAPHS, SUNNY_RUNTIME, random.Random(1))
    assert _strip_ws("".join(plan.bubbles)) == _strip_ws(TWO_PARAGRAPHS)


def test_太短的段落即使有分段也不拆():
    plan = plan_reply("嗯嗯。\n我懂。", SUNNY_RUNTIME, random.Random(1))
    assert len(plan.bubbles) == 1


def test_只有一句話的內容不拆():
    """沒有 \\n\\n 分段，就當作單一段落，不拆。"""
    one = "今天真的好累喔，下班的時候天都黑了，但還是想跟你說說話呢。"
    assert split_into_bubbles(one) == [one]


def test_空內容不產生任何則數():
    plan = plan_reply("   ", SUNNY_RUNTIME, random.Random(1))
    assert plan.bubbles == []


def test_則間間隔短於首則延遲():
    """真人是想很久才開口、然後連著打完，不是每則都重新思考半分鐘。"""
    plan = plan_reply(TWO_PARAGRAPHS, _runtime(min_s=18, max_s=20), random.Random(3))
    assert len(plan.gap_delays) == 1
    assert plan.gap_delays[0] < plan.lead_delay


# ═══════════════════════════════════════════════════════════
#  送出編排：typing 提示與等待時序
# ═══════════════════════════════════════════════════════════

class FakeClient:
    """只記錄 sender_action，不發任何請求。"""

    def __init__(self) -> None:
        self.actions: list[str] = []

    async def send_sender_action(self, igsid: str, action: str) -> None:
        self.actions.append(action)


class FakeSender:
    """記錄送出順序；可指定從第幾則開始失敗。"""

    def __init__(self, fail_at: int | None = None) -> None:
        self.sent: list[str] = []
        self.typing_flags: list[bool] = []
        self._fail_at = fail_at

    async def send_reply(self, *, session_id, persona_id, igsid, text, typing=True):
        self.typing_flags.append(typing)
        index = len(self.sent)
        self.sent.append(text)
        if self._fail_at is not None and index >= self._fail_at:
            return SendOutcome(blocked_reason=REASON_WINDOW_EXPIRED)
        return SendOutcome(delivered=[text])


class Recorder:
    """假 sleep：記錄被要求等多久，但不真的等。"""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.waits)


async def _run(runtime, *, seed=1, text=ONE_SENTENCE, fail_at=None):
    client, sender, rec = FakeClient(), FakeSender(fail_at), Recorder()
    outcomes = await send_paced_reply(
        sender=sender,
        client=client,
        session_id=1,
        persona_id=1,
        igsid="IGSID_TEST",
        text=text,
        runtime=runtime,
        rng=random.Random(seed),
        sleep=rec.sleep,
    )
    return client, sender, rec, outcomes


def test_等待總時數等於計畫的延遲總和():
    runtime = SUNNY_RUNTIME
    plan = plan_reply(TWO_PARAGRAPHS, runtime, random.Random(1))
    _client, _sender, rec, _ = asyncio.run(_run(runtime, seed=1, text=TWO_PARAGRAPHS))
    assert rec.total == pytest.approx(plan.total_delay)


def test_送出前確實等待過():
    """這是 7.8 的核心斷言：不是生成完就送。"""
    _client, _sender, rec, _ = asyncio.run(_run(SUNNY_RUNTIME))
    assert rec.total >= 8


def test_typing_indicator_關閉時完全不送提示():
    client, _sender, _rec, _ = asyncio.run(_run(_runtime(typing=False)))
    assert client.actions == []


def test_typing_提示在送出前亮起():
    client, sender, _rec, _ = asyncio.run(_run(_runtime()))
    assert client.actions[0] == "typing_on"
    assert len(sender.sent) == 1


def test_typing_只在末段亮起不整段掛著():
    """Meta 的提示約 10–20 秒自動消失，整段延遲全程掛著會在使用者眼前提早熄掉。"""
    runtime = _runtime(min_s=30, max_s=30)
    _client, _sender, rec, _ = asyncio.run(_run(runtime))
    # 第一段安靜等待，第二段才是 typing 亮起後的等待
    assert rec.waits[0] == pytest.approx(30 - TYPING_LEAD_SECONDS)
    assert rec.waits[1] == pytest.approx(TYPING_LEAD_SECONDS)


def test_延遲短於_typing_前置時間時不會等到負數():
    runtime = _runtime(min_s=1, max_s=1)
    _client, _sender, rec, _ = asyncio.run(_run(runtime))
    assert all(w >= 0 for w in rec.waits)
    assert rec.total == pytest.approx(1.0)


def test_sender_不重複開關_typing():
    """節奏層自己管 typing，傳進 sender 的必須是 False，否則兩邊互相打架。"""
    _client, sender, _rec, _ = asyncio.run(_run(SUNNY_RUNTIME, text=TWO_PARAGRAPHS))
    assert sender.typing_flags == [False, False]


def test_中途失敗仍送出_typing_off():
    """不能把使用者留在「對方正在輸入」的畫面上。"""
    client, _sender, _rec, outcomes = asyncio.run(
        _run(SUNNY_RUNTIME, text=TWO_PARAGRAPHS, fail_at=0)
    )
    assert client.actions[-1] == "typing_off"
    assert len(outcomes) == 1  # 第一則就失敗，不送第二則


def test_中途失敗不送出後續則數():
    _client, sender, _rec, _ = asyncio.run(_run(SUNNY_RUNTIME, text=TWO_PARAGRAPHS, fail_at=0))
    assert len(sender.sent) == 1


def test_多則依序送出():
    _client, sender, _rec, _ = asyncio.run(_run(SUNNY_RUNTIME, text=TWO_PARAGRAPHS))
    plan = plan_reply(TWO_PARAGRAPHS, SUNNY_RUNTIME, random.Random(1))
    assert sender.sent == plan.bubbles


def test_空內容不呼叫發訊也不送提示():
    client, sender, rec, outcomes = asyncio.run(_run(SUNNY_RUNTIME, text="   "))
    assert outcomes == []
    assert sender.sent == []
    assert client.actions == []
    assert rec.waits == []


# ═══════════════════════════════════════════════════════════
#  7.5 × 7.8 交互：揭露必須留在第一則
# ═══════════════════════════════════════════════════════════

def test_揭露文字永遠落在第一則():
    """若拆分把揭露推到第二則，使用者會先看到角色說話、才被告知對方是 AI。

    compose_reply 會在揭露文字後接單個換行再接原文，天然形成兩段，
    不需要額外構造帶分段的內容。
    """
    from app.personas.disclosure import compose_reply

    text = compose_reply(TWO_SENTENCES, disclose=True, persona_name="晴晴")
    for seed in range(50):
        plan = plan_reply(text, SUNNY_RUNTIME, random.Random(seed))
        head = plan.bubbles[0]
        assert "AI" in head or "自動" in head, f"seed={seed} 的第一則不含揭露：{head!r}"
