"""長對話摘要（add-persona-authoring-console 任務 8，design.md 決策 10）。

同一個場次超過門檻（預設 60 則）時，近期訊息的窗口裝不下整段對話，
角色會忘記開頭說過的事。這裡把較早的訊息寫成摘要存進 sessions 表，
回覆時摘要放進 system prompt、近期訊息照舊當對話輪次。

- 第一次：超過門檻時，保留最近 keep_recent 則原文，其餘寫成摘要
- 之後：沒被摘要的訊息又比 keep_recent 多出 update_batch 則時，以
  「舊摘要＋新超出的訊息」更新，不重讀整段對話

在回覆送出之後執行，不延遲回覆。走 analysis 路徑：背景工作、要便宜。
失敗時不寫入任何東西，回覆照常只用近期訊息；條件仍成立，下一次回覆後
會自動再試（spec「摘要產生失敗」）。

與話題記憶（topics.py）的分工：話題記憶跨場次、場次結束後才萃取；
摘要只管同一個場次、在對話進行中產生。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.db import repository as repo
from app.db.models import Message, MessageDirection
from app.db.models import Session as SessionModel
from app.db.session import db_session
from app.dialog.traditional import LANGUAGE_RULE, to_traditional
from app.llm import LLMError, LLMMessage
from app.llm.router import LLMRouter
from app.logging_config import get_logger

log = get_logger(__name__)

MAX_SUMMARY_CHARS = 400

_SPEAKER = {MessageDirection.INBOUND: "使用者", MessageDirection.OUTBOUND: "角色"}

_SYSTEM_PROMPT = (
    "你在幫一個 AI 聊天角色整理記憶。以下是她和使用者這次聊天前段的紀錄"
    "（如果有，還會附上先前已經整理好的摘要）。請寫成摘要，讓角色之後回覆時"
    "還記得前面聊過什麼。\n\n"
    "規則：\n"
    "- 只記雙方明確說過的事，不要推測、不要補充、不要評論對話好壞\n"
    "- 角色說過關於自己的事（住哪、在忙什麼、喜歡什麼）也要記，避免之後前後矛盾\n"
    "- 使用者提過的人名、地點、時間、正在發生的事要保留細節\n"
    "- 有先前的摘要時，把新內容併進去；舊摘要裡的事除非被新對話推翻，否則保留\n"
    f"- 條列，每行一點，用「使用者」「角色」稱呼雙方，總長不超過 {MAX_SUMMARY_CHARS} 字\n"
    f"- {LANGUAGE_RULE}\n\n"
    "只輸出摘要本身，不要標題或說明。"
)


@dataclass(frozen=True)
class SummaryPlan:
    """這次要摘要的訊息，以及寫入後的涵蓋範圍（最後一則的 id）。"""

    messages: list[Message]
    until_message_id: int


def plan_summary(
    unsummarized: Sequence[Message],
    *,
    has_summary: bool,
    trigger: int,
    keep_recent: int,
    update_batch: int,
) -> SummaryPlan | None:
    """決定這次要不要摘要、摘要哪些。unsummarized 是摘要範圍之後的訊息，依 id 正序。

    沒有摘要時，unsummarized 就是整個場次：超過 trigger 則才摘要。
    有摘要時，多出 keep_recent + update_batch 則才更新——每次回覆都更新
    太浪費，也讓摘要被反覆改寫而失真。
    """
    threshold = keep_recent + update_batch if has_summary else trigger + 1
    if len(unsummarized) < threshold:
        return None
    older = list(unsummarized[: len(unsummarized) - keep_recent])
    return SummaryPlan(messages=older, until_message_id=older[-1].id)


def format_input(previous_summary: str | None, messages: Sequence[Message]) -> str:
    lines = [
        f"{_SPEAKER.get(m.direction, m.direction)}：{m.content.strip()}"
        for m in messages
        if m.content and m.content.strip()
    ]
    transcript = "\n".join(lines)
    if previous_summary:
        return f"【先前的摘要】\n{previous_summary}\n\n【之後的對話紀錄】\n{transcript}"
    return f"【對話紀錄】\n{transcript}"


async def summarize(
    router: LLMRouter, previous_summary: str | None, messages: Sequence[Message]
) -> str | None:
    """產生摘要。任何失敗回傳 None，不拋出例外。

    用 generate 而非 generate_or_fallback：後者失敗時回傳對話用的降級語句
    （「欸我剛剛恍神了一下」），那不是摘要，寫進去會污染之後每一次回覆。
    整段紀錄包成單一使用者訊息：以模型輪次結尾會被 Gemini 拒收（見 topics.py）。
    """
    try:
        response = await router.generate(
            "analysis",
            system_prompt=_SYSTEM_PROMPT,
            messages=[LLMMessage(role="user", content=format_input(previous_summary, messages))],
            temperature=0.2,  # 要忠於原文，不要發揮
            max_output_tokens=1024,
        )
    except LLMError as exc:
        log.warning("對話摘要產生失敗", extra={"error_type": type(exc).__name__, "error": str(exc)[:500]})
        return None
    except Exception:
        log.exception("對話摘要發生未預期例外")
        return None
    text = to_traditional(response.text.strip())  # 任務 2.3
    return text or None


# 單一 worker（見 Dockerfile 的 --workers 1），行程內記錄就足以避免同一場次
# 同時跑兩次摘要：使用者連發幾則時，每則回覆送出後都會呼叫一次
_running: set[int] = set()


async def update_session_summary(
    router: LLMRouter,
    session_id: int,
    *,
    trigger: int,
    keep_recent: int,
    update_batch: int,
) -> bool:
    """視需要產生或更新場次摘要。回傳這次是否寫入了新摘要。"""
    if session_id in _running:
        return False
    _running.add(session_id)
    try:
        with db_session() as db:
            session = db.get(SessionModel, session_id)
            if session is None:
                return False
            previous = session.summary
            until = session.summary_until_message_id
            unsummarized = repo.session_messages_after(db, session_id, until)

        plan = plan_summary(
            unsummarized,
            has_summary=previous is not None,
            trigger=trigger,
            keep_recent=keep_recent,
            update_batch=update_batch,
        )
        if plan is None:
            return False

        text = await summarize(router, previous, plan.messages)
        if text is None:
            return False

        with db_session() as db:
            saved = repo.save_session_summary(db, session_id, text, plan.until_message_id, until)
        log.info(
            "對話摘要已更新" if saved else "對話摘要的範圍已被更新，放棄這次結果",
            extra={
                "session_id": session_id,
                "summarized": len(plan.messages),
                "until_message_id": plan.until_message_id,
                "incremental": previous is not None,
                "summary_len": len(text),
            },
        )
        return saved
    finally:
        _running.discard(session_id)
