"""設定連結只能在一個瀏覽器上使用（add-persona-authoring-console 任務 5.8）。

連結本身是無狀態的簽章憑證（settings_link.py），誰拿到都能用。這裡補上
「第一個打開的瀏覽器才算數」：

- 第一次有請求帶著這條連結進來，記下連結的雜湊，並產生一組隨機代碼
  交給那個瀏覽器（HttpOnly cookie）；資料庫只存兩者的雜湊
- 之後的請求必須同時帶連結與代碼；只有連結沒有代碼（例如被轉貼到另一支
  手機）一律拒絕

只有 /api/settings 會登記，頁面本身（/settings）不會。IG 為了產生連結預覽卡片
會去抓網址，若頁面也登記，使用者還沒點開連結就已經被預覽機器人用掉了。

換裝置要重新傳「設定」拿新連結，這是刻意的代價。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from app.db.models import SettingsLinkClaim

COOKIE_NAME = "settings_claim"
# 連結過期後多留一天再清，純粹避免表一直長大；留著不影響正確性（過期的連結本來就會被拒絕）
_PURGE_AFTER = timedelta(days=1)


@dataclass(frozen=True)
class ClaimResult:
    allowed: bool
    # 這次是第一次打開：要把這組代碼用 cookie 交給瀏覽器。已登記過則為 None
    new_code: str | None = None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def check_or_claim(
    db: DBSession,
    token: str,
    code: str | None,
    expires_at: int,
    *,
    now: datetime | None = None,
) -> ClaimResult:
    """連結尚未被登記就登記給這次的請求；已登記則要求帶著對應的代碼。

    token 須已通過簽章與時效驗證（settings_link.verify_token_with_expiry）。
    """
    now = now or datetime.now(timezone.utc)
    token_hash = _hash(token)

    row = db.scalar(select(SettingsLinkClaim).where(SettingsLinkClaim.token_hash == token_hash))
    if row is not None:
        return ClaimResult(allowed=_code_matches(row, code))

    new_code = secrets.token_urlsafe(32)
    db.execute(delete(SettingsLinkClaim).where(SettingsLinkClaim.expires_at < now - _PURGE_AFTER))
    try:
        # savepoint：同一條連結被兩個請求同時第一次打開時，輸的那個只回滾自己這筆
        with db.begin_nested():
            db.add(
                SettingsLinkClaim(
                    token_hash=token_hash,
                    claim_hash=_hash(new_code),
                    claimed_at=now,
                    expires_at=datetime.fromtimestamp(expires_at, timezone.utc),
                )
            )
    except IntegrityError:
        return ClaimResult(allowed=False)
    return ClaimResult(allowed=True, new_code=new_code)


def _code_matches(row: SettingsLinkClaim, code: str | None) -> bool:
    return bool(code) and hmac.compare_digest(_hash(code), row.claim_hash)
