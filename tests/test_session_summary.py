"""長對話摘要（add-persona-authoring-console 任務 8，design.md 決策 10）。

對應 specs/conversation-session/spec.md「對話記憶」：短對話不產生摘要、
超過門檻產生摘要、增量更新沿用舊摘要不重讀整段、失敗時退回近期訊息、
提示中確實含有摘要。不連外網：LLM 換成假的 provider。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.db.models import Message, MessageDirection
from app.dialog.summary import format_input, plan_summary, summarize
from app.llm import LLMError
from app.llm.base import LLMProvider
from app.llm.router import LLMRouter
from app.personas.prompt import render_system_prompt
from tests.test_personas import _prompt_fields
from tests.test_reply_generation import (  # noqa: F401  (make_client 為 fixture)
    IG_ACCOUNT_ID,
    FakeDialogProvider,
    _post,
    make_client,
)

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _messages(count: int, start_id: int = 1) -> list[Message]:
    return [
        Message(
            id=start_id + i,
            session_id=1,
            direction=MessageDirection.INBOUND if i % 2 == 0 else MessageDirection.OUTBOUND,
            content=f"第{start_id + i}則",
            sent_at=T0,
        )
        for i in range(count)
    ]


WINDOW = dict(trigger=60, keep_recent=40, update_batch=20)


# ═══════════════════════════════════════════════════════════
#  要不要摘要、摘要哪些
# ═══════════════════════════════════════════════════════════


def test_未超過門檻不產生摘要():
    assert plan_summary(_messages(60), has_summary=False, **WINDOW) is None


def test_超過門檻時保留最近的原文其餘摘要():
    plan = plan_summary(_messages(61), has_summary=False, **WINDOW)
    assert [m.id for m in plan.messages] == list(range(1, 22))  # 61 − 40 = 21 則
    assert plan.until_message_id == 21


def test_已有摘要時多出一批才更新():
    # 摘要涵蓋到第 21 則，之後 59 則：40 則保留 + 19 則，還不到一批
    assert plan_summary(_messages(59, start_id=22), has_summary=True, **WINDOW) is None

    plan = plan_summary(_messages(60, start_id=22), has_summary=True, **WINDOW)
    assert [m.id for m in plan.messages] == list(range(22, 42))
    assert plan.until_message_id == 41


def test_增量更新的輸入是舊摘要加新訊息而非整段對話():
    text = format_input("- 使用者養了一隻叫小黑的貓", _messages(2, start_id=22))
    assert "【先前的摘要】\n- 使用者養了一隻叫小黑的貓" in text
    assert "使用者：第22則" in text
    assert "角色：第23則" in text
    assert "第1則" not in text


# ═══════════════════════════════════════════════════════════
#  模型呼叫
# ═══════════════════════════════════════════════════════════


class _Broken(LLMProvider):
    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "broken"

    async def generate(self, **kwargs):
        raise LLMError("模擬供應商失敗")


def test_模型失敗時回傳None不拋出例外():
    router = LLMRouter({"analysis": _Broken()}, max_retries=0)
    assert asyncio.run(summarize(router, None, _messages(3))) is None


def test_模型回空白時視為失敗():
    router = LLMRouter({"analysis": FakeDialogProvider(["   "])})
    assert asyncio.run(summarize(router, None, _messages(3))) is None


# ═══════════════════════════════════════════════════════════
#  提示
# ═══════════════════════════════════════════════════════════


def test_提示中含有摘要段落():
    prompt = render_system_prompt(_prompt_fields(), conversation_summary="- 使用者養了一隻叫小黑的貓")
    assert "【這次聊天稍早的內容（摘要）】" in prompt
    assert prompt.rstrip().endswith("- 使用者養了一隻叫小黑的貓")


def test_沒有摘要時提示與導入前相同():
    assert render_system_prompt(_prompt_fields(), conversation_summary=None) == render_system_prompt(
        _prompt_fields()
    )


# ═══════════════════════════════════════════════════════════
#  整合：私訊 → 回覆 → 背景摘要 → 下一次回覆用上摘要
# ═══════════════════════════════════════════════════════════

SUMMARY_1 = "- 使用者養了一隻叫小黑的貓"
SUMMARY_2 = "- 使用者養了一隻叫小黑的貓\n- 使用者下週要去台南"


@pytest.fixture
def small_window(monkeypatch):
    """門檻縮小，不必灌幾十則訊息：超過 6 則摘要、保留 4 則、再多 2 則就更新。"""
    monkeypatch.setenv("SUMMARY_TRIGGER_MESSAGES", "6")
    monkeypatch.setenv("SUMMARY_KEEP_RECENT", "4")
    monkeypatch.setenv("SUMMARY_UPDATE_BATCH", "2")


def _session(main_module):
    from app.db.models import Session as SessionModel
    from app.db.session import db_session

    with db_session() as db:
        session = db.query(SessionModel).one()
        db.expunge(session)
        return session


def _chat(client, igsid: str, texts: list[str], start: int = 1) -> None:
    for i, text in enumerate(texts, start):
        assert _post(client, f"mid_{i}", text, igsid).status_code == 200


def _swap_analysis(client, provider) -> None:
    client._main_module.llm_router = LLMRouter({
        "dialog": client._fake_provider,
        "analysis": provider,
    }, max_retries=0)


def test_短對話不產生摘要(make_client, small_window):
    client = make_client(replies=["嗯"])
    _chat(client, "IGSID_S1", ["嗨", "在嗎"])
    assert _session(client._main_module).summary is None
    assert client._fake_analysis_provider.calls == []


def test_超過門檻後下一則回覆帶著摘要且不重送已摘要的原文(make_client, small_window):
    client = make_client(replies=["嗯"])
    analysis = FakeDialogProvider([SUMMARY_1])
    _swap_analysis(client, analysis)

    _chat(client, "IGSID_S2", ["我養了一隻貓叫小黑", "牠很黏人", "今天帶牠去打針", "好累喔"])

    session = _session(client._main_module)
    assert session.summary == SUMMARY_1
    assert session.summary_until_message_id is not None
    assert "我養了一隻貓叫小黑" in analysis.calls[0]["messages"][0].content

    _chat(client, "IGSID_S2", ["你記得我的貓叫什麼嗎"], start=5)

    last = client._fake_provider.calls[-1]
    assert SUMMARY_1 in last["system_prompt"]
    sent = [m.content for m in last["messages"]]
    assert "我養了一隻貓叫小黑" not in sent  # 已在摘要裡，不再以原文送
    assert sent[-1] == "你記得我的貓叫什麼嗎"


def test_再多一批訊息時以舊摘要加新訊息更新(make_client, small_window):
    client = make_client(replies=["嗯"])
    analysis = FakeDialogProvider([SUMMARY_1, SUMMARY_2])
    _swap_analysis(client, analysis)

    _chat(client, "IGSID_S3", ["我養了一隻貓叫小黑", "牠很黏人", "今天帶牠去打針", "好累喔"])
    first_until = _session(client._main_module).summary_until_message_id
    # 一問一答剛好多出 2 則，達到一批
    _chat(client, "IGSID_S3", ["下週要去台南找朋友"], start=5)

    session = _session(client._main_module)
    assert len(analysis.calls) == 2
    second_input = analysis.calls[1]["messages"][0].content
    assert f"【先前的摘要】\n{SUMMARY_1}" in second_input
    assert "我養了一隻貓叫小黑" not in second_input  # 不重讀已摘要的部分
    assert session.summary == SUMMARY_2
    assert session.summary_until_message_id > first_until


def test_摘要失敗時照常回覆且下一次再試(make_client, small_window):
    client = make_client(replies=["嗯"])
    _swap_analysis(client, _Broken())

    _chat(client, "IGSID_S4", ["我養了一隻貓叫小黑", "牠很黏人", "今天帶牠去打針", "好累喔"])
    assert _session(client._main_module).summary is None
    replies_before = len(client._fake_provider.calls)

    analysis = FakeDialogProvider([SUMMARY_1])
    _swap_analysis(client, analysis)
    _chat(client, "IGSID_S4", ["還在嗎"], start=5)

    assert len(client._fake_provider.calls) == replies_before + 1  # 角色照常回覆
    assert _session(client._main_module).summary == SUMMARY_1  # 條件仍成立，自動重試成功
