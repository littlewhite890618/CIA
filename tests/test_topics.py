"""話題萃取（task 6.7）。

對應 openspec/changes/add-ig-persona-chat-mvp/specs/conversation-session/spec.md
的「跨 Session 的話題記憶」Requirement。不連外網：LLMRouter 換成
FakeProvider（同 test_llm.py 的手法）。
"""

from __future__ import annotations

import asyncio

from app.dialog.topics import MAX_TOPICS, _parse_topics, extract_topics
from app.llm.base import LLMMessage, LLMProvider, LLMResponse
from app.llm.router import LLMRouter

MESSAGES = [LLMMessage(role="user", content="我是幼兒園老師，週末喜歡騎腳踏車")]


class FakeAnalysisProvider(LLMProvider):
    def __init__(self, text: str = "幼兒園老師\n騎腳踏車") -> None:
        self._text = text
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-analysis-1"

    async def generate(self, *, system_prompt, messages, temperature=0.9, max_output_tokens=1024):
        self.calls += 1
        return LLMResponse(text=self._text, provider="fake", model=self.model, elapsed_ms=1)


def _router(text: str = "幼兒園老師\n騎腳踏車") -> tuple[LLMRouter, FakeAnalysisProvider]:
    provider = FakeAnalysisProvider(text)
    return LLMRouter({"analysis": provider}), provider


# ═══════════════════════════════════════════════════════════
#  文字解析
# ═══════════════════════════════════════════════════════════

def test_逐行解析為話題清單():
    assert _parse_topics("幼兒園老師\n騎腳踏車") == ["幼兒園老師", "騎腳踏車"]


def test_容忍常見的項目符號與編號前綴():
    text = "- 幼兒園老師\n1. 騎腳踏車\n• 心理podcast"
    assert _parse_topics(text) == ["幼兒園老師", "騎腳踏車", "心理podcast"]


def test_去除重複話題():
    assert _parse_topics("幼兒園老師\n幼兒園老師") == ["幼兒園老師"]


def test_空行不算話題():
    assert _parse_topics("幼兒園老師\n\n\n騎腳踏車") == ["幼兒園老師", "騎腳踏車"]


def test_最多只取前八個():
    text = "\n".join(f"話題{i}" for i in range(20))
    assert len(_parse_topics(text)) == MAX_TOPICS


def test_空字串回傳空清單():
    assert _parse_topics("") == []
    assert _parse_topics("   \n  ") == []


# ═══════════════════════════════════════════════════════════
#  extract_topics 整合
# ═══════════════════════════════════════════════════════════

def test_成功萃取回傳話題清單():
    router, _provider = _router()
    topics = asyncio.run(extract_topics(router, MESSAGES))
    assert topics == ["幼兒園老師", "騎腳踏車"]


def test_走analysis路徑而非dialog():
    """6.7 是背景分析工作，design.md 決策 5 指定這類任務走 analysis。"""
    dialog_provider = FakeAnalysisProvider("不該被呼叫")
    analysis_provider = FakeAnalysisProvider("幼兒園老師")
    router = LLMRouter({"dialog": dialog_provider, "analysis": analysis_provider})

    topics = asyncio.run(extract_topics(router, MESSAGES))
    assert topics == ["幼兒園老師"]
    assert dialog_provider.calls == 0
    assert analysis_provider.calls == 1


def test_無明確話題時回傳空清單():
    router, _provider = _router(text="")
    assert asyncio.run(extract_topics(router, MESSAGES)) == []


def test_沒有對話內容時不呼叫模型():
    router, provider = _router()
    topics = asyncio.run(extract_topics(router, []))
    assert topics == []
    assert provider.calls == 0


def test_呼叫失敗時安靜回傳空清單不拋出例外():
    """這是背景工作，失敗只代表下次見面少一點記憶，不是對話中斷，
    不該讓呼叫端（靜默輪詢迴圈）因此中斷。"""

    class BrokenProvider(FakeAnalysisProvider):
        async def generate(self, **kwargs):
            raise RuntimeError("未預期的錯誤")

    router = LLMRouter({"analysis": BrokenProvider()}, max_retries=0)
    topics = asyncio.run(extract_topics(router, MESSAGES))
    assert topics == []


def test_對話包成單一使用者訊息_不以模型輪次結尾():
    """Gemini 會以 400 拒收以模型輪次結尾的請求，話題萃取因此曾一直失敗。"""

    class Capture(FakeAnalysisProvider):
        async def generate(self, *, system_prompt, messages, temperature=0.9, max_output_tokens=1024):
            self.seen = messages
            return await super().generate(
                system_prompt=system_prompt, messages=messages,
                temperature=temperature, max_output_tokens=max_output_tokens,
            )

    provider = Capture()
    router = LLMRouter({"analysis": provider})
    convo = [
        LLMMessage(role="user", content="我被主管罵"),
        LLMMessage(role="model", content="辛苦了"),
    ]
    asyncio.run(extract_topics(router, convo))

    assert len(provider.seen) == 1
    assert provider.seen[0].role == "user"
    assert "使用者：我被主管罵" in provider.seen[0].content
    assert "角色：辛苦了" in provider.seen[0].content
