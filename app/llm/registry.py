"""供應商註冊表（tasks 5.2、5.3）。

**這是「有哪些供應商」的唯一真相來源。** app/config.py 的設定驗證
也讀這裡，所以新增一家供應商只要在 `PROVIDERS` 加一行，設定驗證、
錯誤訊息、可用清單全部自動跟上——不需要在兩個地方各維護一份名單，
那種寫法遲早會不一致。

新增供應商的完整步驟：
    1. 寫一個 LLMProvider 子類別（參考 app/llm/gemini.py）
    2. 在下方 PROVIDERS 加一行
    3. 改環境變數 DIALOG_PROVIDER 等
呼叫端的業務邏輯一行都不必動——這是 design.md 對供應層的要求。
"""

from __future__ import annotations

from collections.abc import Callable

from app.llm.base import LLMConfigError, LLMProvider, PathConfig
from app.llm.gemini import GeminiProvider

# 供應商名稱 -> 建構函式。
# 建構函式簽章統一為 (api_key, model, timeout) -> LLMProvider，
# 讓 build_provider 不必知道任何一家的細節。
ProviderFactory = Callable[..., LLMProvider]

PROVIDERS: dict[str, ProviderFactory] = {
    "gemini": GeminiProvider,
}


def supported_providers() -> list[str]:
    """目前已實作的供應商名稱，排序後回傳。供設定驗證與錯誤訊息使用。"""
    return sorted(PROVIDERS)


def build_provider(config: PathConfig, api_key: str, timeout: float) -> LLMProvider:
    """依路徑設定建出供應商實例。

    指向未實作的供應商、或缺金鑰時拋出 LLMConfigError——這兩種都是
    設定問題，應在啟動時就炸掉（task 5.2），不是等到使用者發訊、
    背景任務裡才失敗。錯誤訊息一律指出**該改哪個環境變數**，
    否則看到「供應商不存在」還是不知道要動哪裡。
    """
    factory = PROVIDERS.get(config.provider)
    if factory is None:
        raise LLMConfigError(
            f"LLM 路徑 '{config.path}' 指向未實作的供應商 '{config.provider}'。"
            f" 目前支援：{supported_providers()}。"
            f" 請修正環境變數 {config.path.upper()}_PROVIDER。"
        )

    if not api_key:
        raise LLMConfigError(
            f"LLM 路徑 '{config.path}' 使用 '{config.provider}'，但金鑰未設定。"
            f" 請於 .env 填入 {config.provider.upper()}_API_KEY。"
        )

    # extra 承載供應商專屬的選項，不污染共用介面。目前只有 Gemini 的
    # thinking_budget 用到：對話路徑要關（快），教練回饋路徑要開（準）。
    extra: dict[str, object] = {}
    if "thinking_budget" in config.extra:
        raw = config.extra["thinking_budget"]
        if raw in ("", "auto", "none"):
            extra["thinking_level"] = None  # 交給模型自行決定，不送 thinkingConfig
        else:
            extra["thinking_budget"] = int(raw)

    return factory(api_key, config.model, timeout=timeout, **extra)
