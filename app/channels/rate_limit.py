"""發訊速率配額（task 4.7）。

**這個上限沒有官方文件背書。** Meta 官方公布的是每秒呼叫數
（文字訊息每帳號每秒 100 次，見 graph-api/overview/rate-limiting），
那個數字遠高於本系統的用量，不會成為瓶頸。

真正會擋住我們的是 Meta 未公開的反垃圾行為偵測——多家第三方工具商
回報約 200 則自動 DM／小時／帳號。撞到它的症狀不是 API 回錯誤，
而是**帳號被限制或停權**，代價遠高於少送幾則訊息。因此此處寧可保守。

實作選擇：記憶體滾動計數，不用 Redis。
POC 已決定不起 Docker（task 1.2），沒有 Redis 可用。單一進程的
記憶體計數在此情境下等價——重啟後歸零只會讓發訊「更保守」，
不會違反平台規則。Phase 1 換 Redis 時只需替換此類別，介面不變。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable

from app.logging_config import get_logger

log = get_logger(__name__)


class RateLimiter:
    """每個角色帳號各自獨立的滾動時間窗計數器。

    以 deque 保存每次發訊的時間戳，查詢時先丟棄離開時間窗的舊紀錄。
    相較於「固定時間窗計數」，滾動窗不會在整點交界處讓瞬間用量翻倍。

    clock 可注入，讓測試能以虛擬時間驗證「等待後配額釋出」的行為，
    而不需要真的等一小時。正式執行一律使用 time.monotonic——
    它不受系統時間調整影響，計算時間差比 time.time() 可靠。
    """

    def __init__(
        self,
        limit: int,
        window_seconds: int = 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        events = self._events[key]
        cutoff = now - self._window
        while events and events[0] <= cutoff:
            events.popleft()
        return events

    def seconds_until_slot(self, key: str) -> float:
        """還要等多久才有配額。0 表示現在就能送。"""
        now = self._clock()
        events = self._prune(key, now)
        if len(events) < self._limit:
            return 0.0
        # 最舊的那筆離開時間窗時，就空出一個名額
        return max(0.0, events[0] + self._window - now)

    def record(self, key: str) -> None:
        """記錄一次成功發訊。"""
        now = self._clock()
        self._prune(key, now)
        self._events[key].append(now)

    def current_count(self, key: str) -> int:
        return len(self._prune(key, self._clock()))
