"""使用者設定頁連結的簽發與驗證（add-persona-authoring-console 任務 5.1，design.md 決策 13）。

使用者在私訊傳「設定」，系統回一條連結。連結本身就是憑證：持有者可以
編輯「某位使用者 × 某個共用角色」的設定，不需要帳號密碼——私訊已經確認
了對方是誰，連結只發給他。

連結格式：<對外網址>/settings?t=<token>
token 格式：<payload>.<簽章>，兩段皆為 base64url（無補位）
    payload = "<link_id>.<到期時間 unix 秒>"
    簽章    = HMAC-SHA256(密鑰, "settings-link:v1|" + payload)

設計取捨：
- 無狀態：不存資料庫，驗證只靠簽章與到期時間。代價是無法在到期前
  個別撤銷某條連結；有效期短（預設 30 分鐘），可接受
- 簽章不過一律回「無效」，不區分原因、也不查資料庫，竄改者無從得知
  某位使用者是否存在（tenant-access spec「竄改連結」）
- 簽章通過才判斷是否過期，所以「已過期」只會對自己的真連結出現，
  可以放心提示使用者重新傳「設定」
- 簽章內容含用途前綴：教練回饋連結（daily-coach-report-link 任務 4.1）
  共用同一把密鑰與同一套格式，但前綴不同，兩種 token 互相無法冒用。
  回饋連結的 payload 綁的是每日回饋的 id（coach_digests.id），不是 link_id
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone

MIN_SECRET_LENGTH = 32
SETTINGS_PURPOSE = b"settings-link:v1|"
REPORT_PURPOSE = b"coach-report:v1|"


class SettingsLinkError(Exception):
    """連結無法使用。"""


class SettingsLinkInvalid(SettingsLinkError):
    """格式錯誤或簽章不符——可能是竄改，也可能是複製時截斷。不透露是哪一種。"""


class SettingsLinkExpired(SettingsLinkError):
    """簽章正確但已過期。"""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _check_secret(secret: str) -> bytes:
    if len(secret) < MIN_SECRET_LENGTH:
        raise ValueError(f"設定頁連結的密鑰至少需要 {MIN_SECRET_LENGTH} 個字元")
    return secret.encode("utf-8")


def _sign(key: bytes, payload_b64: str, purpose: bytes) -> str:
    return _b64encode(hmac.new(key, purpose + payload_b64.encode("ascii"), hashlib.sha256).digest())


def _now_ts(now: datetime | None) -> int:
    return int((now or datetime.now(timezone.utc)).timestamp())


def issue_token(
    link_id: int,
    *,
    secret: str,
    ttl_seconds: int,
    now: datetime | None = None,
    purpose: bytes = SETTINGS_PURPOSE,
) -> str:
    """簽發一個綁定 link_id（使用者 × 共用角色；回饋連結則為每日回饋 id）的 token。"""
    key = _check_secret(secret)
    expires_at = _now_ts(now) + ttl_seconds
    payload_b64 = _b64encode(f"{link_id}.{expires_at}".encode("ascii"))
    return f"{payload_b64}.{_sign(key, payload_b64, purpose)}"


def verify_token(
    token: str, *, secret: str, now: datetime | None = None, purpose: bytes = SETTINGS_PURPOSE
) -> int:
    """驗證 token，回傳 link_id。

    簽章不符或格式錯誤拋 SettingsLinkInvalid；簽章正確但過期拋 SettingsLinkExpired。
    """
    return verify_token_with_expiry(token, secret=secret, now=now, purpose=purpose)[0]


def verify_token_with_expiry(
    token: str, *, secret: str, now: datetime | None = None, purpose: bytes = SETTINGS_PURPOSE
) -> tuple[int, int]:
    """同 verify_token，另外回傳到期時間（unix 秒），供設定頁顯示剩餘時間。"""
    key = _check_secret(secret)

    parts = token.split(".") if isinstance(token, str) else []
    # 非 ASCII 字元先擋下：否則計算簽章時編碼失敗會變成未處理的例外（HTTP 500），
    # 而不是「連結無效」
    if len(parts) != 2 or not all(p and p.isascii() for p in parts):
        raise SettingsLinkInvalid("連結無效")
    payload_b64, signature = parts

    if not hmac.compare_digest(_sign(key, payload_b64, purpose), signature):
        raise SettingsLinkInvalid("連結無效")

    try:
        link_text, expires_text = _b64decode(payload_b64).decode("ascii").split(".")
        link_id, expires_at = int(link_text), int(expires_text)
    except (ValueError, UnicodeDecodeError) as exc:
        # 簽章正確卻解不開，只可能是密鑰外洩後的偽造，同樣視為無效
        raise SettingsLinkInvalid("連結無效") from exc

    if _now_ts(now) >= expires_at:
        raise SettingsLinkExpired("連結已過期")
    return link_id, expires_at


def build_settings_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/settings?t={token}"


def build_report_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/report?t={token}"
