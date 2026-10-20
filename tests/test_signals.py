"""訊號偵測（task 8.2）。

對應 openspec/changes/add-ig-persona-chat-mvp/specs/realtime-signals/spec.md
的「行為訊號偵測」「訊號偵測不阻塞對話」Requirement，判定準則見
docs/即時訊號判定準則.md。不連外網：LLMRouter 換成 FakeProvider
（同 test_topics.py 的手法）。

模型判斷得準不準（「加個 LINE 吧」有沒有被認成 L1）單元測試驗不出來，
留給 8.5／9.8 用真實模型與真實對話檢查。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Message, MessageDirection, Severity, SignalType
from app.dialog.signals import (
    DIMENSIONS,
    SIGNAL_ITEMS,
    SignalParseError,
    apply_session_rules,
    detect_bursts,
    detect_signals,
    format_transcript,
    parse_signals,
)
from app.llm.base import LLMProvider, LLMResponse
from app.llm.router import LLMRouter
from app.personas.schema import Boundaries

T0 = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)

BOUNDARIES = Boundaries(
    warms_up_to=["使用者願意談自己真實的感受"],
    cools_down_on=["評論她的身材或外貌"],
    never_engages=["索取電話 / LINE / 其他聯絡方式"],
    cooling_behavior="退化成客服式安撫",
)


def _msg(id_: int, direction: str, content: str, seconds: int) -> Message:
    return Message(
        id=id_,
        session_id=1,
        direction=direction,
        content=content,
        sent_at=T0 + timedelta(seconds=seconds),
    )


def _user(id_: int, content: str, seconds: int) -> Message:
    return _msg(id_, MessageDirection.INBOUND, content, seconds)


def _bot(id_: int, content: str, seconds: int) -> Message:
    return _msg(id_, MessageDirection.OUTBOUND, content, seconds)


# 一般節奏的對話：使用者訊息 1、3、5，間隔都很長，不會觸發 L10
CONVO = [
    _user(1, "嗨", 0),
    _bot(2, "嗨嗨，今天過得好嗎？", 20),
    _user(3, "你週末都去哪玩？", 120),
    _bot(4, "我會去河濱騎車呀 🌿", 140),
    _user(5, "加個 LINE 吧", 300),
]


class FakeAnalysisProvider(LLMProvider):
    def __init__(self, text: str = "[]") -> None:
        self._text = text
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-analysis-1"

    async def generate(self, *, system_prompt, messages, temperature=0.9, max_output_tokens=1024):
        self.calls.append({"system_prompt": system_prompt, "messages": list(messages)})
        return LLMResponse(text=self._text, provider="fake", model=self.model, elapsed_ms=1)


def _detect(messages, text="[]", session_number=1, router=None):
    provider = FakeAnalysisProvider(text)
    router = router or LLMRouter({"analysis": provider})
    result = asyncio.run(
        detect_signals(
            router,
            messages,
            BOUNDARIES,
            session_number=session_number,
            burst_window_seconds=30,
            burst_min_messages=3,
        )
    )
    return result, provider


# ═══════════════════════════════════════════════════════════
#  A. 解析模型回傳
# ═══════════════════════════════════════════════════════════

def test_合法JSON轉成訊號並由對照表補上欄位():
    text = json.dumps([{"message_id": 5, "item": "L1", "reason": "第三則就要 LINE"}])
    [s] = parse_signals(text, {1, 3, 5})
    assert s.message_id == 5
    assert s.item == "L1"
    assert s.signal_type == SignalType.LANDMINE
    assert s.severity == Severity.HIGH
    assert s.dimension == "分寸與界線"
    assert s.reason == "第三則就要 LINE"


def test_不存在的代號被丟掉_其他保留():
    text = json.dumps([
        {"message_id": 3, "item": "L11", "reason": "亂編的"},
        {"message_id": 3, "item": "H1", "reason": "開放式提問"},
    ])
    assert [s.item for s in parse_signals(text, {1, 3, 5})] == ["H1"]


def test_message_id不是這場的使用者訊息時丟掉():
    text = json.dumps([
        {"message_id": 2, "item": "K1", "reason": "指到角色訊息"},
        {"message_id": 999, "item": "K1", "reason": "不存在的編號"},
        {"message_id": 1, "item": "K1", "reason": "正常"},
    ])
    assert [s.message_id for s in parse_signals(text, {1, 3, 5})] == [1]


def test_容忍code_fence包住的JSON_格式壞掉拋出解析錯誤():
    """task 8.4：格式壞掉要和「合法的空陣列」分開，前者是偵測失敗。"""
    fenced = '```json\n[{"message_id": 3, "item": "H1", "reason": "x"}]\n```'
    assert [s.item for s in parse_signals(fenced, {3})] == ["H1"]
    with pytest.raises(SignalParseError):
        parse_signals("我覺得這段對話很棒", {3})
    with pytest.raises(SignalParseError):
        parse_signals('[{"message_id": 3, "item": ', {3})


def test_空陣列回傳空清單():
    assert parse_signals("[]", {1, 3}) == []


def test_模型標了L10也丟掉_由程式判斷():
    text = json.dumps([{"message_id": 3, "item": "L10", "reason": "x"}])
    assert parse_signals(text, {3}) == []


# ═══════════════════════════════════════════════════════════
#  B. 對照表
# ═══════════════════════════════════════════════════════════

def test_只有地雷有嚴重程度():
    for code, spec in SIGNAL_ITEMS.items():
        if spec.signal_type == SignalType.LANDMINE:
            assert spec.severity in {Severity.HIGH, Severity.MEDIUM, Severity.LOW}, code
        else:
            assert spec.severity is None, code


def test_對照表的維度都在六項之內():
    assert {spec.dimension for spec in SIGNAL_ITEMS.values()} <= DIMENSIONS
    assert len(DIMENSIONS) == 6


def test_六項維度每一項都至少有一個訊號項目():
    """task 8.5：spec「訊號涵蓋教練評分維度」——每個維度都要有訊號可以回答。"""
    assert {spec.dimension for spec in SIGNAL_ITEMS.values()} == DIMENSIONS


# ═══════════════════════════════════════════════════════════
#  C. 程式直接判斷的項目
# ═══════════════════════════════════════════════════════════

def test_L10_30秒內連發3則命中_記在第3則():
    msgs = [_user(1, "欸", 0), _user(2, "在嗎", 10), _user(3, "怎麼不回", 20), _user(4, "？", 25)]
    [s] = detect_bursts(msgs, 30, 3)
    assert s.item == "L10"
    assert s.message_id == 3
    assert s.dimension == "節奏"


def test_L10_間隔夠長或中間有角色回覆時不命中():
    spaced = [_user(1, "欸", 0), _user(2, "在嗎", 20), _user(3, "怎麼不回", 40)]
    assert detect_bursts(spaced, 30, 3) == []

    interrupted = [_user(1, "欸", 0), _user(2, "在嗎", 5), _bot(3, "在呀", 8), _user(4, "好", 10)]
    assert detect_bursts(interrupted, 30, 3) == []


def test_L3_前三次對話保留():
    text = json.dumps([{"message_id": 5, "item": "L3", "reason": "約見面"}])
    signals = parse_signals(text, {5})
    assert [s.item for s in apply_session_rules(signals, session_number=3)] == ["L3"]


def test_L3_超過三次對話丟掉_其他訊號不受影響():
    text = json.dumps([
        {"message_id": 5, "item": "L3", "reason": "約見面"},
        {"message_id": 5, "item": "H1", "reason": "開放式提問"},
    ])
    signals = parse_signals(text, {5})
    assert [s.item for s in apply_session_rules(signals, session_number=4)] == ["H1"]


# ═══════════════════════════════════════════════════════════
#  C2. format_transcript：角色泡泡合併（2026-09-24 真機發現的歸因錯誤修正）
# ═══════════════════════════════════════════════════════════

def test_連續角色泡泡合併成一行():
    messages = [
        _user(1, "還行啊妳呢？", 0),
        _bot(2, "嗯嗯，我今天也還算順利呀。", 1),
        _bot(3, "你今天工作有發生什麼特別的事嗎？", 2),
        _user(4, "沒什麼特別的事欸", 10),
    ]
    text = format_transcript(messages)
    lines = text.splitlines()
    assert "角色：嗯嗯，我今天也還算順利呀。 你今天工作有發生什麼特別的事嗎？" in lines
    # 合併後不該再出現任何一行是單獨的「角色：你今天工作...」
    assert "角色：你今天工作有發生什麼特別的事嗎？" not in lines


def test_角色泡泡不跨使用者訊息合併():
    messages = [
        _bot(1, "第一段角色回覆。", 0),
        _user(2, "使用者插話", 1),
        _bot(3, "第二段角色回覆。", 2),
    ]
    lines = format_transcript(messages).splitlines()
    assert "角色：第一段角色回覆。" in lines
    assert "[#2] 使用者：使用者插話" in lines
    assert "角色：第二段角色回覆。" in lines
    # 兩段角色回覆中間隔著使用者訊息，不能被誤合併成一行
    assert "角色：第一段角色回覆。 第二段角色回覆。" not in lines


def test_使用者連續訊息各自成行不合併():
    messages = [
        _user(1, "第一則", 0),
        _user(2, "第二則", 1),
    ]
    lines = format_transcript(messages).splitlines()
    assert "[#1] 使用者：第一則" in lines
    assert "[#2] 使用者：第二則" in lines


# ═══════════════════════════════════════════════════════════
#  D. detect_signals 整合
# ═══════════════════════════════════════════════════════════

def test_整段對話包成單一使用者訊息_只有使用者訊息標編號():
    _result, provider = _detect(CONVO)
    [call] = provider.calls
    [only] = call["messages"]
    assert only.role == "user"
    assert "[#1] 使用者：嗨" in only.content
    assert "[#5] 使用者：加個 LINE 吧" in only.content
    assert "角色：嗨嗨，今天過得好嗎？" in only.content
    assert "[#2]" not in only.content


def test_走analysis路徑而非dialog():
    dialog = FakeAnalysisProvider("不該被呼叫")
    analysis = FakeAnalysisProvider("[]")
    router = LLMRouter({"dialog": dialog, "analysis": analysis})
    _detect(CONVO, router=router)
    assert dialog.calls == []
    assert len(analysis.calls) == 1


def test_呼叫失敗時不拋出例外_回報失敗且不回傳部分結果():
    """task 8.4：失敗時連程式判斷的 L10 也不回傳，避免殘缺結果被當成完整結果。"""

    class BrokenProvider(FakeAnalysisProvider):
        async def generate(self, **kwargs):
            raise RuntimeError("未預期的錯誤")

    burst = [_user(1, "欸", 0), _user(2, "在嗎", 5), _user(3, "？", 10)]
    router = LLMRouter({"analysis": BrokenProvider()}, max_retries=0)
    result, _provider = _detect(burst, router=router)
    assert result.ok is False
    assert result.signals == []
    assert "未預期的錯誤" in result.error


def test_模型輸出格式壞掉視為失敗():
    result, _provider = _detect(CONVO, text="這段對話很自然")
    assert result.ok is False
    assert "SignalParseError" in result.error


def test_模型回傳空陣列視為成功_沒有訊號():
    result, _provider = _detect(CONVO, text="[]")
    assert result.ok is True
    assert result.signals == []


def test_沒有使用者訊息時不呼叫模型():
    result, provider = _detect([_bot(1, "嗨嗨", 0)])
    assert result.ok is True
    assert result.signals == []
    assert provider.calls == []


def test_人設卡邊界放進提示詞():
    _result, provider = _detect(CONVO)
    prompt = provider.calls[0]["system_prompt"]
    assert "評論她的身材或外貌" in prompt
    assert "索取電話 / LINE / 其他聯絡方式" in prompt


def test_完整流程_模型結果與L10合併回傳():
    text = json.dumps([{"message_id": 5, "item": "L1", "reason": "要 LINE"}])
    result, _provider = _detect(CONVO, text=text)
    assert result.ok is True
    assert [(s.message_id, s.item) for s in result.signals] == [(5, "L1")]
