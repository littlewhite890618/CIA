"""發訊錯誤的分類與告警（add-persona-authoring-console 第 9 群：發訊配速的監測）。

所有角色都在同一個 IG 帳號上（design.md 決策 12），帳號一旦被限速或被判違規，
整個產品就停擺。這裡把每次發訊失敗分類、寫進 send_errors 表，嚴重的類別另記
一行「【告警】」開頭的 warning log。寫進資料表而不只寫 log：容器每次重新部署，
舊的 log 就沒了，事後要查被判違規的原因時會找不到。

分類依據 Meta Graph API 的錯誤代碼。只有確定意義的代碼才歸類，其餘一律「其他」，
錯誤原文另存在 detail，日後遇到新代碼再補分類。
"""

from __future__ import annotations

from app.channels.instagram import (
    RATE_LIMIT_CODES,
    WINDOW_EXPIRED_SUBCODE,
    InstagramRateLimited,
    InstagramSendError,
    InstagramWindowExpired,
)

RATE_LIMITED = "rate_limited"  # 限速：現在不行、等一下可以
POLICY_BLOCK = "policy_block"  # 疑似違規被封鎖：最需要知道的一類
TOKEN_INVALID = "token_invalid"  # 存取權杖失效：晴晴會完全發不出訊息
WINDOW_EXPIRED = "window_expired"  # 24 小時視窗逾期：正常現象，不告警
OTHER = "other"

CATEGORIES = (RATE_LIMITED, POLICY_BLOCK, TOKEN_INVALID, WINDOW_EXPIRED, OTHER)

CATEGORY_LABELS = {
    RATE_LIMITED: "限速",
    POLICY_BLOCK: "疑似違規",
    TOKEN_INVALID: "權杖失效",
    WINDOW_EXPIRED: "視窗逾期",
    OTHER: "其他",
}

# 出現就要人看的類別
ALERT_CATEGORIES = frozenset({RATE_LIMITED, POLICY_BLOCK, TOKEN_INVALID})

# 368：「這個動作被判定為濫用或不被允許」，帳號因違反政策被暫時封鎖
POLICY_BLOCK_CODES = frozenset({368})
# 190：存取權杖無效或過期（OAuthException）
TOKEN_INVALID_CODES = frozenset({190})


def classify(exc: InstagramSendError) -> str:
    if isinstance(exc, InstagramRateLimited) or exc.code in RATE_LIMIT_CODES or exc.status == 429:
        return RATE_LIMITED
    if isinstance(exc, InstagramWindowExpired) or exc.subcode == WINDOW_EXPIRED_SUBCODE:
        return WINDOW_EXPIRED
    if exc.code in POLICY_BLOCK_CODES:
        return POLICY_BLOCK
    if exc.code in TOKEN_INVALID_CODES:
        return TOKEN_INVALID
    return OTHER
