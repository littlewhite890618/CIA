"""話題萃取（task 6.7）。

session 結束時，把整段對話濃縮成幾個話題關鍵字，存進
`user_persona_links.topic_memory`，下次見面時載入 system prompt，
讓角色不重複詢問已經問過的基本問題（見 render_system_prompt 的
known_topics 參數）。

走 analysis 路徑而非 dialog：這是背景分析工作，不在使用者等待的
關鍵路徑上，對延遲、語氣沒有要求，符合 design.md 決策 5 對這條路徑
的定位（「即時分析要便宜」）。失敗時安靜放棄——這是背景工作，
失敗只代表下次見面少一點記憶，不是對話中斷，不值得為此重試或告警。
"""

from __future__ import annotations

from app.llm import LLMMessage
from app.llm.router import LLMRouter
from app.logging_config import get_logger

log = get_logger(__name__)

MAX_TOPICS = 8

_SPEAKER = {"user": "使用者", "model": "角色"}

_EXTRACTION_SYSTEM_PROMPT = (
    "以下是一段使用者與 AI 角色的對話紀錄。請萃取這段對話裡使用者提到的"
    "「話題」——例如職業、興趣、正在煩惱的事、提過的人名或地點。\n"
    "每行一個話題，用簡短詞語描述（不超過 10 個字），不要重複，"
    "不要加任何編號或標點，最多列出 8 個。\n"
    "如果對話內容太少、沒有明確話題，就什麼都不要輸出。"
)


def _parse_topics(text: str) -> list[str]:
    """把模型輸出的逐行文字轉成話題清單，容忍常見的編號/項目符號前綴。"""
    topics: list[str] = []
    for line in text.splitlines():
        topic = line.strip().lstrip("-•*0123456789. 、")
        if topic and topic not in topics:
            topics.append(topic)
    return topics[:MAX_TOPICS]


async def extract_topics(router: LLMRouter, messages: list[LLMMessage]) -> list[str]:
    """萃取話題。任何失敗（含降級回應）都回傳空清單，呼叫端不需要
    分辨「真的沒話題」與「呼叫失敗」——兩者的處置一樣：這次不更新記憶。
    """
    if not messages:
        return []

    # 整段對話包成單一使用者訊息，而非原樣當聊天輪次傳入：對話最後一則
    # 是角色（模型）自己說的話，Gemini 會以 400 拒收「以模型輪次結尾」的
    # 請求，萃取因此一直失敗、topic_memory 永遠是空的（2026-09-21 查證）。
    transcript = "\n".join(
        f"{_SPEAKER.get(m.role, m.role)}：{m.content}" for m in messages
    )

    try:
        text, response = await router.generate_or_fallback(
            "analysis",
            system_prompt=_EXTRACTION_SYSTEM_PROMPT,
            messages=[LLMMessage(role="user", content=transcript)],
            temperature=0.3,  # 萃取要穩定一致，不需要對話生成那種變化
            max_output_tokens=200,
        )
    except Exception:
        log.exception("話題萃取呼叫發生未預期例外，放棄這次萃取")
        return []

    if response is None:
        # generate_or_fallback 走了降級語句，代表這次真正的呼叫失敗了——
        # 降級語句（如「欸我剛剛恍神了一下」）本身不是有效的話題清單。
        return []

    return _parse_topics(text)
