"""對話上下文組裝（task 7.6）。

把資料庫的 Message 記錄轉換成 LLM 供應層看得懂的格式。這是唯一知道
「DB 的 direction 欄位」與「LLM 的 role 欄位」怎麼對應的地方——上下兩層
（repository 與 llm）都不需要認識對方的型別。
"""

from __future__ import annotations

from app.db.models import Message, MessageDirection
from app.llm import LLMMessage

_ROLE_MAP = {
    MessageDirection.INBOUND: "user",
    MessageDirection.OUTBOUND: "model",
}


def build_context(messages: list[Message]) -> list[LLMMessage]:
    """把最近訊息轉換成 LLM 對話歷史，順序原樣保留（呼叫端已依時間排序）。

    出站訊息若帶有 7.5 的揭露文字前綴（見 disclosure.compose_reply），
    會原樣留在歷史裡——這正是我們要的：讓模型「記得」自己說過什麼，
    包含開場揭露，不必特別過濾。
    """
    return [
        LLMMessage(role=_ROLE_MAP[m.direction], content=m.content)
        for m in messages
        if m.content and m.content.strip()
    ]


_SPEAKER_LABEL = {
    MessageDirection.INBOUND: "使用者",
    MessageDirection.OUTBOUND: "你",
}


def format_previous_conversation(messages: list[Message]) -> list[str]:
    """把前幾個場次的最後幾則訊息轉成「說話者：內容」文字行。

    刻意寫成文字放進 system prompt，而不是接在對話輪次前面：舊場次的
    訊息與目前場次之間隔了一段時間，當成連續輪次會讓模型以為是同一口氣
    講的話，也可能造成同角色連續輪次而被 Gemini 拒收。
    """
    return [
        f"{_SPEAKER_LABEL[m.direction]}：{m.content.strip()}"
        for m in messages
        if m.content and m.content.strip()
    ]
