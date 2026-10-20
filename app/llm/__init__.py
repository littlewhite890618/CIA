"""LLM 供應層（第 5 任務群）。

對外只需要這幾個名字：

    from app.llm import LLMMessage, LLMRouter, build_router

    router = build_router(settings.llm_path_configs, api_keys=..., timeout=...)
    text, response = await router.generate_or_fallback(
        "dialog", system_prompt=..., messages=[LLMMessage("user", "嗨")]
    )

供應商的實作細節（gemini.py）不從這裡匯出——呼叫端不應該 import 到
任何一家供應商，那會讓「抽換供應商不需改業務邏輯」變成空話。
"""

from app.llm.base import (
    LLMBlocked,
    LLMConfigError,
    LLMError,
    LLMMessage,
    LLMProvider,
    LLMRateLimited,
    LLMResponse,
    LLMTimeout,
    PathConfig,
)
from app.llm.fallback import DIALOG_FALLBACKS, pick_fallback
from app.llm.registry import supported_providers
from app.llm.router import LLMRouter, build_router

__all__ = [
    "DIALOG_FALLBACKS",
    "LLMBlocked",
    "LLMConfigError",
    "LLMError",
    "LLMMessage",
    "LLMProvider",
    "LLMRateLimited",
    "LLMResponse",
    "LLMRouter",
    "LLMTimeout",
    "PathConfig",
    "build_router",
    "pick_fallback",
    "supported_providers",
]
