"""LLM 供應層的介面與共用型別（task 5.1）。

依 design.md 決策 5：設定單位是「用途路徑」而非「供應商」——
對話生成要快、即時分析要便宜、教練回饋要準，三者對延遲與成本的要求
截然不同。呼叫端只說「我要走 dialog 路徑」，不需要知道背後是誰。

例外分三類，因為處置方式不同：
    LLMRateLimited  現在不行、等一下可以  -> 退避重試
    LLMTimeout      這次太慢              -> 重試一次通常會好
    LLMBlocked      內容被供應商擋下       -> 重試無意義，改走降級
    LLMError        其餘失敗              -> 依情況重試或放棄

免費模型的速率上限低且不穩定（design.md 的第三項外部約束），
所以失敗在這一層是**常態而非例外**，介面設計以此為前提。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

# 對話中的角色。沿用 Gemini 的命名（user／model），
# 其他供應商若用 assistant 等字眼，由各自的實作負責轉換。
Role = Literal["user", "model"]


@dataclass(frozen=True)
class LLMMessage:
    """送進模型的單則對話訊息。"""

    role: Role
    content: str


@dataclass
class LLMResponse:
    """一次成功呼叫的結果，含 5.6 要求記錄的全部欄位。"""

    text: str
    provider: str
    model: str
    elapsed_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    attempts: int = 1

    @property
    def total_tokens(self) -> int | None:
        if self.prompt_tokens is None or self.completion_tokens is None:
            return None
        return self.prompt_tokens + self.completion_tokens


# ═══════════════════════════════════════════════════════════
#  例外
# ═══════════════════════════════════════════════════════════

class LLMError(Exception):
    """呼叫語言模型失敗的基底類別。"""


class LLMRateLimited(LLMError):
    """觸及供應商的速率上限。退避後重試有機會成功。"""


class LLMTimeout(LLMError):
    """呼叫超過設定的逾時時限，已中止。"""


class LLMBlocked(LLMError):
    """內容被供應商的安全機制擋下。

    與其他失敗分開，因為**重試毫無意義**——同樣的輸入還是會被擋。
    這個產品談的是戀愛對話，觸發供應商安全機制的機率不低，
    分類出來才能在觀測資料裡看見它、也才能讓上層改走降級回應。
    """


class LLMConfigError(LLMError):
    """設定有問題，例如指向未實作的供應商或缺少金鑰。

    這類錯誤應在啟動時就浮現（task 5.2），不該等到執行期。
    """


# ═══════════════════════════════════════════════════════════
#  供應商介面
# ═══════════════════════════════════════════════════════════

class LLMProvider(ABC):
    """所有供應商的共同介面。

    只有一個方法。刻意保持最小——介面愈大，新增供應商的成本愈高，
    而 design.md 要求「更換供應商不需修改呼叫端業務邏輯」。

    重試、退避、降級**不在這一層**：那是 LLMRouter 的職責，
    對所有供應商都一樣，沒有理由讓每家各寫一份。
    這與 channels 層的分工一致（instagram.py 只管「怎麼呼叫一次」，
    sender.py 管「什麼時候可以呼叫、失敗了怎麼辦」）。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """供應商識別名，需與設定值一致（如 "gemini"）。"""

    @property
    @abstractmethod
    def model(self) -> str:
        """實際使用的模型名稱。"""

    @abstractmethod
    async def generate(
        self,
        *,
        system_prompt: str,
        messages: list[LLMMessage],
        temperature: float = 0.9,
        max_output_tokens: int = 1024,
    ) -> LLMResponse:
        """產生一次回應。

        失敗時拋出 LLMError 的子類別，由 LLMRouter 決定重試或放棄。
        逾時的中止責任在此層（各家客戶端的逾時設定方式不同）。
        """

    async def aclose(self) -> None:
        """釋放連線資源。預設不做事，有持有連線的實作要覆寫。"""
        return None


@dataclass
class PathConfig:
    """一條具名路徑的設定（task 5.1）。

    三條路徑各自繫結供應商與模型，互不影響——變更其中一條不需要
    動到另外兩條，也不需要改任何業務邏輯。
    """

    path: str
    provider: str
    model: str
    extra: dict[str, str] = field(default_factory=dict)
