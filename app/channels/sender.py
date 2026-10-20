"""發訊與平台規則強制（tasks 4.1、4.3–4.8）。

依 design.md 決策：「平台規則（視窗、速率、長度）由通道層統一強制，
上層功能不需各自處理」。M3 對話生成只要呼叫 send_reply()，不需要知道
1000 bytes 上限、24 小時視窗或速率配額的存在。

職責分工：
    instagram.py  怎麼呼叫 API（一次請求、一個結果）
    splitter.py   內容怎麼切
    rate_limit.py 什麼時候可以送
    sender.py     把上述三者串起來，並在送出前後維護資料庫狀態
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.channels.instagram import (
    InstagramClient,
    InstagramRateLimited,
    InstagramSendError,
    InstagramWindowExpired,
)
from app.channels.rate_limit import RateLimiter
from app.channels.send_errors import ALERT_CATEGORIES, CATEGORY_LABELS, classify
from app.channels.splitter import split_message
from app.db import repository as repo
from app.db import session as session_module
from app.db.models import Session as SessionModel
from app.logging_config import get_logger

log = get_logger(__name__)


# 未送達原因。寫進 messages.meta.reason，作為觀測資料。
REASON_NO_INBOUND = "no_inbound_interaction"
REASON_WINDOW_EXPIRED = "messaging_window_expired"
REASON_SEND_FAILED = "send_api_failed"
REASON_SESSION_MISSING = "session_not_found"
# 我方檢查認為視窗還開著，但 Meta 拒收。兩者不一致本身就是值得觀測的訊號，
# 與 REASON_WINDOW_EXPIRED（我方主動擋下）分開記錄。
REASON_WINDOW_EXPIRED_BY_PLATFORM = "messaging_window_expired_by_platform"
REASON_QUOTA_WAIT_TOO_LONG = "quota_wait_exceeded_limit"


@dataclass
class SendOutcome:
    """一次 send_reply 的結果，供呼叫端記錄與測試斷言。"""

    delivered: list[str] = field(default_factory=list)   # 成功送出的各片段內容
    blocked_reason: str | None = None                    # 未送出時的原因
    rate_limited_hits: int = 0                           # 觸發平台速率限制的次數

    @property
    def ok(self) -> bool:
        return self.blocked_reason is None and bool(self.delivered)


class MessageSender:
    """在送出前強制執行 Meta 平台規則。

    每一步對應的任務：
        4.4 未互動使用者阻擋   送出前檢查是否有入站訊息
        4.3 24 小時視窗檢查    逾期則不呼叫 API，標記未送達
        4.5 訊息切分           超過 1000 bytes 切成多則
        4.1 輸入狀態提示       送出期間顯示「輸入中」
        4.7 發訊速率配額       達上限時暫緩，不丟棄訊息
        4.6 順序保證           逐則循序 await，天然保證順序
        4.8 速率限制退避重試   退避後重試，並記錄至 rate_budget
    """

    def __init__(
        self,
        client: InstagramClient,
        limiter: RateLimiter,
        *,
        max_retries: int = 3,
        retry_base_seconds: float = 2.0,
        max_wait_seconds: float = 300.0,
    ) -> None:
        self._client = client
        self._limiter = limiter
        self._max_retries = max_retries
        self._retry_base = retry_base_seconds
        self._max_wait = max_wait_seconds

    # ── 對外介面 ──────────────────────────────────────

    async def send_reply(
        self,
        *,
        session_id: int,
        persona_id: int,
        igsid: str,
        text: str,
        typing: bool = True,
        meta: dict[str, Any] | None = None,
    ) -> SendOutcome:
        """送出一則回覆（必要時切成多則）。

        typing=False 用於呼叫端自行接管輸入狀態提示的情形——
        app/dialog/delivery.py 為了讓「輸入中」在擬真延遲的末段才亮起
        （task 7.8），需要自己控制 typing_on 的時機，此時不能讓這裡
        重複開關。預設 True 維持既有行為。

        meta 寫入落庫的訊息，用於標記系統訊息（models.SystemMessageKind），
        使其不進入對話上下文。
        """
        outcome = SendOutcome()

        chunks = split_message(text)
        if not chunks:
            log.info("內容為空，不送出", extra={"session_id": session_id})
            return outcome

        blocked = self._precheck(session_id, text)
        if blocked is not None:
            outcome.blocked_reason = blocked
            return outcome

        if typing:
            await self._client.send_sender_action(igsid, "typing_on")
        try:
            for chunk in chunks:
                # 逐則 await：這就是 4.6 的順序保證。改成並行送出會快一點，
                # 但 Meta 不保證併發請求的抵達順序，切分後的內容會錯亂。
                message_id, hits, reason = await self._send_one(
                    persona_id, igsid, chunk, None, session_id=session_id
                )
                outcome.rate_limited_hits += hits

                if message_id is None:
                    # 中途失敗就停止：只送出前半段比完全不送更糟，
                    # 使用者會看到一段沒頭沒尾的話。
                    reason = reason or REASON_SEND_FAILED
                    outcome.blocked_reason = reason
                    self._record_undelivered(session_id, chunk, reason)
                    break

                outcome.delivered.append(chunk)
                self._record_delivered(session_id, persona_id, chunk, message_id, meta)
        finally:
            if typing:
                await self._client.send_sender_action(igsid, "typing_off")

        return outcome

    async def send_button_template(
        self,
        *,
        session_id: int,
        persona_id: int,
        igsid: str,
        text: str,
        buttons: list[dict[str, str]],
    ) -> SendOutcome:
        """送出帶按鈕的單則訊息（M9：教練報告邀請）。

        沿用與 send_reply 相同的前置檢查（未互動阻擋、視窗檢查）——
        design.md 決策 6：「邀請與報告都沿用同一條發訊管線，自動繼承
        這個行為，不另寫檢查」。不經過 split_message：按鈕依附在單一則
        訊息上，呼叫端需自行保證 text 夠短（邀請文案本來就該是一兩句話，
        不會有切分需求）。

        原本用 Quick Reply，2026-09-23 真機實測發現 Instagram 不會渲染，
        改用 Button Template——見 InstagramClient.send_button_template 的
        說明。
        """
        outcome = SendOutcome()

        blocked = self._precheck(session_id, text)
        if blocked is not None:
            outcome.blocked_reason = blocked
            return outcome

        await self._client.send_sender_action(igsid, "typing_on")
        try:
            message_id, hits, reason = await self._send_one(
                persona_id,
                igsid,
                text,
                lambda: self._client.send_button_template(igsid, text, buttons),
                session_id=session_id,
            )
            outcome.rate_limited_hits += hits

            if message_id is None:
                reason = reason or REASON_SEND_FAILED
                outcome.blocked_reason = reason
                self._record_undelivered(session_id, text, reason)
            else:
                outcome.delivered.append(text)
                self._record_delivered(session_id, persona_id, text, message_id)
        finally:
            await self._client.send_sender_action(igsid, "typing_off")

        return outcome

    # ── 前置檢查 ──────────────────────────────────────

    def _precheck(self, session_id: int, text: str) -> str | None:
        """回傳阻擋原因，可送出則回傳 None。"""
        with session_module.db_session() as db:
            session = db.get(SessionModel, session_id)
            if session is None:
                log.warning("場次不存在，放棄發訊", extra={"session_id": session_id})
                return REASON_SESSION_MISSING

            # 4.4：Meta 禁止對從未互動的使用者主動發訊。先查這項，
            # 因為它比視窗檢查更根本——沒互動過就談不上視窗。
            if not repo.has_any_inbound(db, session.id):
                repo.save_undelivered_message(
                    db, session=session, content=text, reason=REASON_NO_INBOUND
                )
                return REASON_NO_INBOUND

            # 4.3：視窗逾期就不呼叫 API。硬送只會拿到錯誤回應，
            # 還可能被計入異常行為。
            if not repo.is_window_open(session):
                repo.save_undelivered_message(
                    db, session=session, content=text, reason=REASON_WINDOW_EXPIRED
                )
                return REASON_WINDOW_EXPIRED

        return None

    # ── 送出單一片段 ───────────────────────────────────

    async def _send_one(
        self,
        persona_id: int,
        igsid: str,
        text: str,
        send_fn: Callable[[], Awaitable[str | None]] | None,
        *,
        session_id: int | None = None,
    ) -> tuple[str | None, int, str | None]:
        """送出一則。

        send_fn 是實際呼叫平台 API 的動作，預設為 client.send_text(igsid, text)；
        M9 的教練報告邀請需要送 Button Template（見 send_button_template），
        傳入不同的 send_fn 即可沿用這裡的配額等待、退避重試、錯誤分類，
        不必重寫一份。

        回傳 (平台訊息 ID 或 None, 觸發速率限制的次數, 失敗原因或 None)。
        失敗原因會被寫進未送達紀錄，所以要夠具體才有觀測價值。
        """
        send_fn = send_fn or (lambda: self._client.send_text(igsid, text))
        key = str(persona_id)
        rate_limited_hits = 0

        for attempt in range(self._max_retries + 1):
            # 4.7：配額不足時等待而非丟棄。等待上限存在是為了避免
            # 背景任務無限期卡住；超過上限就放棄這一則並記錄原因。
            wait = self._limiter.seconds_until_slot(key)
            if wait > 0:
                if wait > self._max_wait:
                    log.warning(
                        "配額釋出時間超過等待上限，放棄此則",
                        extra={"persona_id": persona_id, "wait_seconds": round(wait)},
                    )
                    return None, rate_limited_hits, REASON_QUOTA_WAIT_TOO_LONG
                log.info("配額已滿，暫緩發訊", extra={"wait_seconds": round(wait, 1)})
                await asyncio.sleep(wait)

            try:
                message_id = await send_fn()
                self._limiter.record(key)
                return message_id, rate_limited_hits, None
            except InstagramSendError as exc:
                # 第 9 群監測：每次失敗都留紀錄（限速後的每次重試也算），再依類型處置
                self._record_send_error(persona_id, session_id, exc)

                if isinstance(exc, InstagramWindowExpired):
                    # 我方檢查放行、Meta 卻拒收——兩者不一致。重試無意義，
                    # 但要如實記錄，否則這個落差會被埋進籠統的發訊失敗裡。
                    log.warning(
                        "平台判定視窗逾期，與我方檢查不一致",
                        extra={"igsid": igsid, "error": str(exc)},
                    )
                    return None, rate_limited_hits, REASON_WINDOW_EXPIRED_BY_PLATFORM

                if isinstance(exc, InstagramRateLimited):
                    # 4.8：平台明確回報速率限制 —— 退避後重試，並記錄下來，
                    # 這是找出安全配速的實測資料來源（第 9 群）。
                    rate_limited_hits += 1
                    self._record_rate_limited(persona_id)
                    backoff = self._retry_base * (2 ** attempt)
                    log.warning(
                        "觸發平台速率限制，退避後重試",
                        extra={"attempt": attempt + 1, "backoff": backoff, "error": str(exc)},
                    )
                    await asyncio.sleep(backoff)
                    continue

                # 一般失敗（權限不足、內容違規）重試也不會變好，直接放棄。
                log.warning("發訊失敗，不重試", extra={"igsid": igsid, "error": str(exc)})
                return None, rate_limited_hits, REASON_SEND_FAILED

        log.warning("重試次數用盡仍未送出", extra={"igsid": igsid})
        return None, rate_limited_hits, REASON_SEND_FAILED

    def _record_send_error(
        self, persona_id: int, session_id: int | None, exc: InstagramSendError
    ) -> None:
        """寫進 send_errors 表；限速、疑似違規、權杖失效另記一行【告警】。

        記錄本身失敗不能讓發訊流程跟著壞掉——監測是旁支，不是主線。
        """
        category = classify(exc)
        if category in ALERT_CATEGORIES:
            log.warning(
                f"【告警】發訊失敗：{CATEGORY_LABELS[category]}",
                extra={
                    "category": category,
                    "persona_id": persona_id,
                    "session_id": session_id,
                    "http_status": exc.status,
                    "error_code": exc.code,
                    "error_subcode": exc.subcode,
                    "detail": (exc.detail or str(exc))[:300],
                },
            )
        try:
            with session_module.db_session() as db:
                repo.record_send_error(
                    db,
                    persona_id=persona_id,
                    session_id=session_id,
                    category=category,
                    http_status=exc.status,
                    error_code=exc.code,
                    error_subcode=exc.subcode,
                    detail=exc.detail or str(exc),
                )
        except Exception:
            log.exception("發訊錯誤紀錄寫入失敗", extra={"category": category})

    # ── 資料庫寫入 ────────────────────────────────────
    #
    # 每次都開短生命週期的 session。發訊過程中有 sleep（退避、配額等待），
    # 跨越那些等待持有連線會白白佔用連線池。

    def _record_delivered(
        self,
        session_id: int,
        persona_id: int,
        content: str,
        message_id: str | None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        with session_module.db_session() as db:
            session = db.get(SessionModel, session_id)
            if session is None:
                return
            repo.save_outbound_message(
                db, session=session, content=content, platform_message_id=message_id, meta=meta
            )
            repo.bump_rate_budget(db, persona_id=persona_id, sent=1)

    def _record_undelivered(self, session_id: int, content: str, reason: str) -> None:
        with session_module.db_session() as db:
            session = db.get(SessionModel, session_id)
            if session is None:
                return
            repo.save_undelivered_message(db, session=session, content=content, reason=reason)

    def _record_rate_limited(self, persona_id: int) -> None:
        with session_module.db_session() as db:
            repo.bump_rate_budget(db, persona_id=persona_id, rate_limited=1)
