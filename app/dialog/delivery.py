"""依擬真節奏送出回覆（task 7.8 的編排部分）。

把 app/dialog/pacing.py 算出的 ReplyPlan 實際執行：等待 → 顯示輸入中 →
送出。平台規則（視窗、切分、配額、重試）全部留給 MessageSender，
本模組不重複那些判斷。

task 7.9（延遲期間使用者又發訊）以 is_stale 回呼實作，而非
asyncio.Task.cancel()——後者需要能跨請求存取彼此 Task 物件的排程機制，
現有的 FastAPI BackgroundTasks 並不提供，見 app/db/repository.py 的
has_newer_inbound() 文件說明。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from app.channels.instagram import InstagramClient
from app.channels.sender import MessageSender, SendOutcome
from app.dialog.pacing import ReplyPlan, plan_reply
from app.logging_config import get_logger
from app.personas.schema import PersonaRuntimeParams

log = get_logger(__name__)

# 「輸入中」提示在送出前多久亮起。
#
# 不整段掛著是有原因的：Meta 的 typing 提示約 10–20 秒就自動消失，
# 而晴晴的延遲上限是 20 秒。整段掛 typing 會貼著那個消失時間，
# 看起來像「打到一半放棄了」，比不顯示更怪。
TYPING_LEAD_SECONDS = 8.0

# 實際用來等待的函式。抽成模組層級變數，是為了讓「不是在測節奏」的測試
# （例如 tests/test_webhook.py 測的是 webhook 解析與去重）能整個換掉它——
# 否則每一筆假 payload 都要真的等 8–20 秒，整個測試套件會從數秒變成數分鐘。
# 與該檔案把 send_text／send_sender_action 換成假物件是同一個理由。
DEFAULT_SLEEP: Callable[[float], Awaitable[None]] = asyncio.sleep


async def send_paced_reply(
    *,
    sender: MessageSender,
    client: InstagramClient,
    session_id: int,
    persona_id: int,
    igsid: str,
    text: str,
    runtime: PersonaRuntimeParams,
    rng: random.Random | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    plan: ReplyPlan | None = None,
    is_stale: Callable[[], bool] | None = None,
) -> list[SendOutcome]:
    """依人設卡的節奏參數送出回覆，回傳每一則的送出結果。

    sleep 可注入，測試因此不需要真的等 20 秒（見 tests/test_pacing.py）。
    plan 可注入，供需要指定確切節奏的測試使用；一般呼叫端不必傳。

    is_stale（task 7.9）在送出第一則之前檢查一次：使用者是否在等待期間
    又發了新訊息。只檢查一次、只在第一則之前——延遲區間（8–20 秒）遠比
    則與則之間的間隔（2–6 秒）長，是使用者最可能在等待期間插話的窗口；
    一旦決定開始送出，中途再檢查的邊際效益低，不值得為此複雜化。
    過時就完全不送，讓新訊息自己觸發的下一次回覆去處理——那次呼叫的
    上下文本來就會包含這則被跳過的訊息。

    中途某一則送不出去就停止後續：只送出半段話比完全不送更糟，
    這與 MessageSender 對切分片段的處置一致。
    """
    # 於呼叫當下解析而非用參數預設值：預設值在函式定義時就綁死，
    # 測試替換 DEFAULT_SLEEP 會失效。
    sleep = sleep or DEFAULT_SLEEP

    plan = plan or plan_reply(text, runtime, rng)
    if not plan.bubbles:
        return []

    outcomes: list[SendOutcome] = []
    typing_on = False

    try:
        for index, bubble in enumerate(plan.bubbles):
            wait = plan.lead_delay if index == 0 else plan.gap_delays[index - 1]

            # 前半段安靜等待（還沒開始打字），末段才亮起輸入中。
            quiet = max(wait - TYPING_LEAD_SECONDS, 0.0)
            if quiet:
                await sleep(quiet)

            if runtime.typing_indicator and not typing_on:
                await client.send_sender_action(igsid, "typing_on")
                typing_on = True

            typing_span = min(wait, TYPING_LEAD_SECONDS)
            if typing_span:
                await sleep(typing_span)

            # 7.9：只在第一則、剛等完 lead_delay 之後檢查——這是使用者
            # 最可能插話的窗口。若過時，直接放棄整次回覆，不送任何一則。
            if index == 0 and is_stale is not None and is_stale():
                log.info(
                    "送出前發現使用者已發送新訊息，放棄此次回覆",
                    extra={"session_id": session_id},
                )
                break

            outcome = await sender.send_reply(
                session_id=session_id,
                persona_id=persona_id,
                igsid=igsid,
                text=bubble,
                typing=False,  # 本模組自己管，不讓 sender 重複開關
            )
            outcomes.append(outcome)

            # 訊息「確實送達」時 Meta 才會清除提示，下一則要重新亮起。
            # 送失敗時不能跟著歸零——沒有訊息抵達，提示還亮在對方畫面上，
            # 得靠底下的 finally 明確關掉。
            if outcome.ok:
                typing_on = False

            if not outcome.ok:
                log.info(
                    "此則未送出，停止後續則數",
                    extra={
                        "session_id": session_id,
                        "bubble_index": index,
                        "blocked_reason": outcome.blocked_reason,
                    },
                )
                break
    finally:
        # 中途失敗或被取消時，不能把使用者留在「對方正在輸入」的畫面上。
        if typing_on:
            await client.send_sender_action(igsid, "typing_off")

    log.info(
        "擬真節奏送出完成",
        extra={
            "session_id": session_id,
            "bubbles": len(plan.bubbles),
            "sent": sum(1 for o in outcomes if o.ok),
            "lead_delay": round(plan.lead_delay, 1),
            "total_delay": round(plan.total_delay, 1),
        },
    )
    return outcomes
