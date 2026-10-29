"""繁體中文輸出（daily-coach-report-link 第 2 群，design.md 決策 7）。

模型偶爾夾雜簡體字：晴晴的回覆、教練報告、對話摘要送出或保存前轉成繁體。
原本就是繁體的內容不能被改寫，使用者傳來的訊息也不轉。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.db.models import Message, MessageDirection
from app.dialog.coach_report import REPORT_SECTIONS, build_system_prompt as report_prompt, generate_report
from app.dialog.summary import _SYSTEM_PROMPT as SUMMARY_PROMPT, summarize
from app.dialog.traditional import LANGUAGE_RULE, to_traditional
from app.llm.router import LLMRouter
from app.personas.coaching_settings import default_coaching
from app.personas.prompt import render_system_prompt
from tests.test_personas import _prompt_fields
from tests.test_reply_generation import (  # noqa: F401  (make_client 為 fixture)
    FakeDialogProvider,
    _post,
    make_client,
)

# ═══════════════════════════════════════════════════════════
#  轉換
# ═══════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("我今天头发好乱，发了一张照片给你", "我今天頭髮好亂，發了一張照片給你"),
        ("没关系啦，我们周末去骑脚踏车", "沒關係啦，我們週末去騎腳踏車"),  # 「系」「周」單看是繁體字
        ("这个问题让我很头疼，你觉得呢？", "這個問題讓我很頭疼，你覺得呢？"),
    ],
)
def test_簡體字轉成繁體(src, expected):
    assert to_traditional(src) == expected


@pytest.mark.parametrize(
    "text",
    [
        "乾淨、干擾、幹嘛",  # 直接用 OpenCC 會把「干擾」改成「幹擾」
        "我住台灣，也寫作臺灣",  # 直接用 OpenCC 會把「台」改成「臺」
        "裡面、後來、皇后、什麼、公里",
        "辛苦了～我懂那種感覺，週末去動一動真的很放鬆😂",
        "嗨，我是晴晴，戀愛顧問🌿",
        "ok 123 :)",
        "",
    ],
)
def test_原本就是繁體的內容逐字不變(text):
    assert to_traditional(text) == text


# ═══════════════════════════════════════════════════════════
#  提示
# ═══════════════════════════════════════════════════════════


def test_三份提示都要求繁體中文():
    custom = dict(default_coaching(), reportTone="直接具體：重點先講，不繞圈子")
    assert LANGUAGE_RULE in render_system_prompt(_prompt_fields())
    assert LANGUAGE_RULE in report_prompt([], "晴晴")
    assert LANGUAGE_RULE in report_prompt([], "晴晴", custom)
    assert LANGUAGE_RULE in SUMMARY_PROMPT


# ═══════════════════════════════════════════════════════════
#  套用：報告、摘要、晴晴的回覆
# ═══════════════════════════════════════════════════════════


def test_報告內容轉成繁體():
    simplified = {k: f"{k}：没关系，我们下次见" for k in REPORT_SECTIONS}
    router = LLMRouter({"coaching": FakeDialogProvider([json.dumps(simplified, ensure_ascii=False)])})
    result = asyncio.run(generate_report(router, [], "晴晴"))
    assert result.ok
    assert all(v.endswith("沒關係，我們下次見") for v in result.content.values())
    assert list(result.content) == list(REPORT_SECTIONS)  # 段落名稱不動


def test_摘要轉成繁體():
    router = LLMRouter({"analysis": FakeDialogProvider(["- 使用者养了一只猫叫小黑"])})
    msg = Message(id=1, session_id=1, direction=MessageDirection.INBOUND, content="嗨")
    assert asyncio.run(summarize(router, None, [msg])) == "- 使用者養了一隻貓叫小黑"


def test_晴晴的回覆轉繁體_使用者訊息維持原樣(make_client):
    from app.db.session import db_session

    client = make_client(replies=["没关系啦，我们周末见"])
    _post(client, "mid_1", "我们周末见", "IGSID_TRAD")

    with db_session() as db:
        rows = db.query(Message).order_by(Message.id).all()
        inbound = [m.content for m in rows if m.direction == MessageDirection.INBOUND]
        outbound = " ".join(m.content for m in rows if m.direction == MessageDirection.OUTBOUND)
    assert inbound == ["我们周末见"]
    assert "沒關係啦，我們週末見" in outbound
    assert "们" not in outbound
