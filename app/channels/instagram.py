"""Instagram 通道：webhook 接收、簽章驗證、事件解析、訊息發送。

對應 openspec tasks 3.1–3.7（webhook 接收與驗證）。
發送 API（4.1）僅實作背景回覆所需的最小功能，完整版（typing indicator、
訊息切分、速率配額）留待正式版本補上。

API 端點格式已於 2026-09-08 查證 Meta 官方文件確認，非憑記憶撰寫：
- 發訊：POST https://graph.instagram.com/v25.0/<IG_ID>/messages
- 取得使用者名稱：GET https://graph.instagram.com/v25.0/<IGSID>?fields=username
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Literal, TypedDict

import httpx

from app.logging_config import get_logger

log = get_logger(__name__)

GRAPH_API_BASE = "https://graph.instagram.com/v25.0"


# ═══════════════════════════════════════════════════════════
#  3.1 webhook 訂閱驗證
# ═══════════════════════════════════════════════════════════

def verify_subscription(mode: str | None, token: str | None, verify_token: str) -> bool:
    """Meta 建立 webhook 訂閱時會發 GET 請求驗證。

    比對 hub.verify_token 與設定值相符，且 hub.mode 為 subscribe，
    相符才允許回傳 hub.challenge。
    """
    return mode == "subscribe" and token is not None and hmac.compare_digest(token, verify_token)


# ═══════════════════════════════════════════════════════════
#  3.2 簽章驗證
# ═══════════════════════════════════════════════════════════

def verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """驗證 X-Hub-Signature-256。

    關鍵：必須對「原始 request body 的 bytes」計算 HMAC，不能先解析成
    dict 再重新序列化——重新序列化後的位元組順序、空白可能與原始不同，
    會導致簽章怎麼算都對不上。呼叫端必須傳入尚未解析的 raw bytes。
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = signature_header.removeprefix("sha256=")
    computed = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, computed)


# ═══════════════════════════════════════════════════════════
#  3.3 webhook 事件解析
# ═══════════════════════════════════════════════════════════

class MessageEvent(TypedDict):
    kind: Literal["message"]
    entry_id: str          # 接收方的 IG 帳號 ID，用於 3.4 角色路由
    igsid: str              # 發送方（使用者）的 IGSID
    platform_message_id: str
    text: str


class PostbackEvent(TypedDict):
    kind: Literal["postback"]
    entry_id: str
    igsid: str
    payload: str


ParsedEvent = MessageEvent | PostbackEvent


def parse_webhook_payload(body: dict[str, Any]) -> list[ParsedEvent | None]:
    """攤平 entry[].messaging[]，逐筆轉成內部事件格式。

    單筆若格式不符預期，回傳 None 而非拋例外——3.5 要求
    「單一請求多筆事件，任一筆失敗不影響其餘」，用 None 讓
    呼叫端可以直接濾掉壞掉的那一筆，不中斷整批處理。
    """
    events: list[ParsedEvent | None] = []

    for entry in body.get("entry", []):
        entry_id = entry.get("id")
        for item in entry.get("messaging", []):
            try:
                events.append(_parse_one(entry_id, item))
            except (KeyError, TypeError) as exc:
                log.warning("事件格式不符預期，略過此筆", extra={"error": str(exc), "raw": item})
                events.append(None)

    return events


def _parse_one(entry_id: str, item: dict[str, Any]) -> ParsedEvent | None:
    igsid = item["sender"]["id"]

    if "message" in item:
        # 回音事件：角色帳號「自己發出」的訊息，Meta 會原樣回送一份。
        # 必須濾掉，否則 sender.id 會是角色帳號本身，被當成一般使用者
        # 建檔、開場次，然後回一則訊息給自己——那則回覆又產生新的回音，
        # 形成無窮迴圈。2026-09-08 實際觀察到單一帳號灌進 104 筆回音。
        if item["message"].get("is_echo"):
            return None

        return MessageEvent(
            kind="message",
            entry_id=entry_id,
            igsid=igsid,
            platform_message_id=item["message"]["mid"],
            text=item["message"].get("text", ""),
        )
    if "postback" in item:
        return PostbackEvent(
            kind="postback",
            entry_id=entry_id,
            igsid=igsid,
            payload=item["postback"].get("payload", ""),
        )
    raise KeyError("既非 message 也非 postback")


# ═══════════════════════════════════════════════════════════
#  Graph API 客戶端（4.1）
# ═══════════════════════════════════════════════════════════

# Meta 的速率限制錯誤碼（2026-09-09 查證）：
#   4     app 層級配額
#   17    使用者層級配額
#   32    粉專 token 配額
#   613   該 API 自訂的呼叫頻率上限
#   80006 / 80007  商務訊息用途的配額
# 另外 HTTP 429 也代表同一件事。
RATE_LIMIT_CODES = frozenset({4, 17, 32, 613, 80006, 80007})

# 24 小時訊息視窗已逾期。2026-09-10 實測 Meta 的回應：
#   HTTP 403 / IGApiException / code 10 / error_subcode 2534022
#   「這則訊息並未在允許的期間內傳送。」
# code 10 本身涵蓋多種權限問題，必須靠 subcode 才能認出是視窗逾期。
WINDOW_EXPIRED_SUBCODE = 2534022


class InstagramSendError(Exception):
    """發訊失敗。呼叫端可據此決定是否重試。

    status、code、subcode、detail 是 Meta 回應的原始資訊（連線失敗時為 None），
    供 app/channels/send_errors.py 分類並寫進 send_errors 表（第 9 群監測）。
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | None = None,
        subcode: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.subcode = subcode
        self.detail = detail


class InstagramWindowExpired(InstagramSendError):
    """平台端判定 24 小時訊息視窗已逾期。

    正常情況下 sender 的視窗檢查會先擋下來，走不到這裡。會走到這裡代表
    我方紀錄與 Meta 的判斷不一致——例如時鐘偏移，或 Meta 的實際計算方式
    與我們假設的有差異。單獨分類是為了讓這種不一致在觀測資料裡看得出來，
    而不是被混進籠統的「發訊失敗」。
    """


class InstagramRateLimited(InstagramSendError):
    """觸發平台速率限制。與一般失敗分開，因為處置方式不同——

    一般失敗多半重試也沒用（權限不足、視窗過期、內容違規），
    速率限制則是「現在不行、等一下可以」，應退避後重試而非放棄。
    """


class InstagramClient:
    """封裝發訊、輸入狀態提示與使用者資料查詢。

    此類別只負責「發出一次請求並回報結果」。切分、配額、重試、
    視窗檢查等平台規則由 app/channels/sender.py 統一處理——
    把「怎麼呼叫 API」與「什麼時候可以呼叫」分開，兩邊都比較好測。
    """

    def __init__(self, access_token: str, ig_account_id: str, timeout: float = 10.0) -> None:
        self._token = access_token
        self._ig_account_id = ig_account_id
        self._timeout = timeout
        # 延遲建立（task 6.1 之後才發現的效能問題）：httpx.AsyncClient 的
        # 建構要載入 CA 憑證、建出 SSL context，約 0.3–0.5 秒。這個類別
        # 本來每次呼叫都是「用完即丟」（sender.py、_send_reply 皆如此），
        # 6.1 讓 _handle_message 對每個新使用者也各建一個實例（僅為了
        # fetch_username 這一次呼叫），實測讓整套測試從 12 秒拖到 63 秒。
        # 與 app/llm/gemini.py 的 GeminiProvider._http 是同一個修法。
        self._client: httpx.AsyncClient | None = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        # 從未呼叫過就沒有客戶端可關，不必為了關閉而先建一個出來
        if self._client is not None:
            await self._client.aclose()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _post_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        """送出一次 /messages 請求，失敗時轉成對應的例外。"""
        url = f"{GRAPH_API_BASE}/{self._ig_account_id}/messages"
        try:
            resp = await self._http.post(url, headers=self._headers, json=payload)
        except httpx.RequestError as exc:
            raise InstagramSendError(f"連線失敗：{exc}", detail=str(exc)[:500]) from exc

        if resp.status_code == 429:
            raise InstagramRateLimited("HTTP 429", status=429, detail=resp.text[:500])

        if resp.is_error:
            code = subcode = None
            try:
                error = resp.json().get("error", {})
                code = error.get("code")
                subcode = error.get("error_subcode")
            except (ValueError, AttributeError):
                pass  # 回應不是 JSON 物件，當一般失敗處理
            info = dict(status=resp.status_code, code=code, subcode=subcode, detail=resp.text[:500])
            if code in RATE_LIMIT_CODES:
                raise InstagramRateLimited(f"error code {code}", **info)
            if subcode == WINDOW_EXPIRED_SUBCODE:
                raise InstagramWindowExpired(f"error subcode {subcode}", **info)
            raise InstagramSendError(f"HTTP {resp.status_code}: {resp.text[:200]}", **info)

        try:
            return resp.json()
        except ValueError:
            return {}

    async def send_text(self, igsid: str, text: str) -> str | None:
        """送出純文字訊息，回傳 Meta 給的訊息 ID。

        失敗時拋出 InstagramSendError（速率限制為其子類別
        InstagramRateLimited），由 sender 決定重試或放棄。
        """
        data = await self._post_message(
            {"recipient": {"id": igsid}, "message": {"text": text}}
        )
        return data.get("message_id")

    async def send_button_template(
        self, igsid: str, text: str, buttons: list[dict[str, str]]
    ) -> str | None:
        """送出帶按鈕的訊息（M9：教練報告邀請）。

        ⚠️ 2026-09-23 實測：Quick Reply（message.quick_replies）**在 Instagram
        Login API 上不會渲染任何按鈕**——API 回 200、正常拿到 message_id，
        但手機 App 與網頁版皆看不到按鈕，是 Graph API「不認得的欄位默默
        忽略」的典型行為，不是逾期或設定問題。改用 Button Template
        （attachment.type=template, template_type=button），真機驗證按鈕
        會正常顯示，點擊後觸發 messaging_postbacks（見 _parse_one 既有的
        postback 分支，本方法的按鈕因此用 type="postback"，不是
        "web_url"）。docs/技術可行性與架構規劃.md §1.1–1.2 對 Quick Reply
        「手機＋桌面皆可觸發」的判斷需訂正，該文件當初可能是把 Messenger
        平台的 Quick Reply（確實支援桌面）與 Instagram 平台的搞混——兩者
        API 形狀相似但不是同一個產品。

        buttons 為 [{"type": "postback", "title": ..., "payload": ...}]，
        最多 3 個（Meta 官方限制）。text 上限 640 字元。
        """
        data = await self._post_message(
            {
                "recipient": {"id": igsid},
                "message": {
                    "attachment": {
                        "type": "template",
                        "payload": {
                            "template_type": "button",
                            "text": text,
                            "buttons": buttons,
                        },
                    }
                },
            }
        )
        return data.get("message_id")

    async def send_sender_action(self, igsid: str, action: str) -> None:
        """送出輸入狀態提示或已讀標記（task 4.1）。

        action 可為 typing_on / typing_off / mark_seen。
        Meta 規定 sender_action 必須單獨送出，**不能與訊息內容同一個請求**
        （2026-09-09 查證 Sender Actions 文件）。

        這是純粹的體驗裝飾，失敗不應影響訊息本身，因此在此吞掉例外。
        """
        try:
            await self._post_message({"recipient": {"id": igsid}, "sender_action": action})
        except InstagramSendError as exc:
            log.info("輸入狀態提示送出失敗，不影響訊息", extra={"action": action, "error": str(exc)})

    async def fetch_username(self, igsid: str) -> str | None:
        """取得使用者名稱，供跨角色歸戶使用。best-effort：失敗回傳 None，
        呼叫端（resolve_user）已支援 ig_username=None 的降級路徑。
        """
        url = f"{GRAPH_API_BASE}/{igsid}"
        try:
            resp = await self._http.get(
                url, params={"fields": "username", "access_token": self._token}
            )
            resp.raise_for_status()
            return resp.json().get("username")
        except httpx.HTTPError as exc:
            log.info(
                "取得使用者名稱失敗，將以身分未確認狀態繼續",
                extra={"igsid": igsid, "error": str(exc)},
            )
            return None
