"""路徑路由、重試與呼叫記錄（tasks 5.4、5.6）。

三條路徑（dialog／analysis／coaching）各自繫結一個供應商實例，
呼叫端只說要走哪條路徑，不需要知道背後是誰、也不需要自己寫重試。

職責分工與 channels 層一致：
    gemini.py   怎麼呼叫一次（一次請求、一個結果或一個例外）
    registry.py 有哪些供應商可以用
    router.py   什麼時候重試、失敗了怎麼辦、記錄了什麼
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable

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
from app.llm.fallback import pick_fallback
from app.logging_config import get_logger

log = get_logger(__name__)

# 值得重試的失敗。LLMBlocked 不在其中——同樣的輸入必然再次被擋，
# 重試只是白白多等幾秒再失敗一次。
RETRYABLE = (LLMRateLimited, LLMTimeout)


class LLMRouter:
    """依路徑名稱取得供應商並執行呼叫。

    一個行程一個實例，於啟動時建立（見 app/main.py 的 lifespan）：
    供應商實例持有 httpx 連線池，每次呼叫各建一個等於放棄連線重用。
    """

    def __init__(
        self,
        providers: dict[str, LLMProvider],
        *,
        fallback_providers: dict[str, LLMProvider] | None = None,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._providers = providers
        # 只有部分路徑會有備援（目前僅 dialog，見 config.py 的
        # dialog_fallback_model）。主要 provider 的重試全部用盡後才試這裡，
        # 且每次呼叫都從主要 provider 重新開始——不會因為上次切了備援，
        # 這次就跳過主要 provider 直接用備援（見 generate() 的說明）。
        self._fallback_providers = fallback_providers or {}
        self._max_retries = max_retries
        self._retry_base = retry_base_seconds
        # 測試注入假 sleep，不必真的等退避秒數
        self._sleep = sleep or asyncio.sleep

    @property
    def paths(self) -> list[str]:
        return sorted(self._providers)

    def provider_for(self, path: str) -> LLMProvider:
        provider = self._providers.get(path)
        if provider is None:
            raise LLMConfigError(
                f"未設定的 LLM 路徑 '{path}'。已設定的路徑：{self.paths}"
            )
        return provider

    async def aclose(self) -> None:
        for provider in (*self._providers.values(), *self._fallback_providers.values()):
            await provider.aclose()

    # ── 呼叫 ──────────────────────────────────────────

    async def generate(
        self,
        path: str,
        *,
        system_prompt: str,
        messages: list[LLMMessage],
        temperature: float = 0.9,
        max_output_tokens: int = 1024,
    ) -> LLMResponse:
        """走指定路徑產生回應，含退避重試（5.4）與呼叫記錄（5.6）。

        主要 provider 的重試全部用盡後，若這條路徑有設定備援 provider
        （見 config.py 的 dialog_fallback_model），改對備援跑一次同樣
        完整的重試流程；備援也失敗才真正拋出例外。每次呼叫都是從主要
        provider 重新開始——不會因為上一次切去用了備援，這次就跳過主要
        provider（多花一輪重試的時間，換來額度只要有恢復就自動用回主要
        模型，不需要額外狀態或人工切回）。

        兩者都失敗就拋出最後一個例外，由呼叫端決定如何降級——
        `generate_or_fallback()` 是對話路徑的現成選擇。
        """
        provider = self.provider_for(path)
        try:
            return await self._generate_with_retry(
                path, provider, system_prompt, messages, temperature, max_output_tokens
            )
        except LLMError as primary_error:
            fallback = self._fallback_providers.get(path)
            if fallback is None:
                raise

            log.warning(
                "主要模型失敗，改試備援模型",
                extra={
                    "path": path,
                    "primary_provider": provider.name,
                    "primary_model": provider.model,
                    "fallback_provider": fallback.name,
                    "fallback_model": fallback.model,
                    "error_type": type(primary_error).__name__,
                },
            )
            try:
                return await self._generate_with_retry(
                    path, fallback, system_prompt, messages, temperature, max_output_tokens
                )
            except LLMError:
                # 備援也失敗：讓使用者看到的降級語句一致地對應「主要模型
                # 的」錯誤原因，而非備援的——備援只是次要嘗試，不該蓋掉
                # 真正的根因。
                raise primary_error from None

    async def _generate_with_retry(
        self,
        path: str,
        provider: LLMProvider,
        system_prompt: str,
        messages: list[LLMMessage],
        temperature: float,
        max_output_tokens: int,
    ) -> LLMResponse:
        """對單一 provider 執行退避重試，用盡仍失敗就拋出最後一個例外。"""
        started = time.perf_counter()
        last_error: LLMError | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await provider.generate(
                    system_prompt=system_prompt,
                    messages=messages,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                )
            except RETRYABLE as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    break
                backoff = self._retry_base * (2 ** attempt)
                log.warning(
                    "LLM 呼叫失敗，退避後重試",
                    extra={
                        "path": path,
                        "provider": provider.name,
                        "model": provider.model,
                        "attempt": attempt + 1,
                        "backoff": backoff,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                await self._sleep(backoff)
                continue
            except LLMError as exc:
                # 不可重試的失敗（被擋、設定錯、回應格式壞掉）
                last_error = exc
                break

            response.attempts = attempt + 1
            self._log_success(path, response)
            return response

        assert last_error is not None  # 迴圈只在設定了 last_error 時 break
        self._log_failure(path, provider, last_error, started)
        raise last_error

    async def generate_or_fallback(
        self,
        path: str,
        *,
        system_prompt: str,
        messages: list[LLMMessage],
        temperature: float = 0.9,
        max_output_tokens: int = 1024,
        rng: random.Random | None = None,
    ) -> tuple[str, LLMResponse | None]:
        """同 generate()，但失敗時回傳降級語句而非拋出例外（task 5.5）。

        回傳 (要送出的文字, 成功時的 LLMResponse 或 None)。
        呼叫端能從第二個元素分辨這次是真的生成還是降級——兩者都要送出，
        但只有前者值得記錄 token 用量、也只有後者需要在觀測資料裡被看見。

        規格要求「不讓使用者面對系統錯誤訊息或無限等待」，所以這裡
        **不再往上拋任何例外**：模型壞掉是我們的問題，不是使用者的。
        """
        try:
            response = await self.generate(
                path,
                system_prompt=system_prompt,
                messages=messages,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )
        except LLMError as exc:
            text = pick_fallback(rng)
            log.warning(
                "對話生成失敗，改送降級回應",
                extra={
                    "path": path,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "fallback": text,
                },
            )
            return text, None

        return response.text, response

    # ── 5.6 呼叫記錄 ──────────────────────────────────

    @staticmethod
    def _log_success(path: str, response: LLMResponse) -> None:
        log.info(
            "LLM 呼叫完成",
            extra={
                "path": path,
                "provider": response.provider,
                "model": response.model,
                "elapsed_ms": response.elapsed_ms,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "total_tokens": response.total_tokens,
                "attempts": response.attempts,
            },
        )

    def _log_failure(
        self, path: str, provider: LLMProvider, error: LLMError, started: float
    ) -> None:
        log.error(
            "LLM 呼叫失敗，重試已用盡",
            extra={
                "path": path,
                "provider": provider.name,
                "model": provider.model,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "attempts": self._max_retries + 1,
                "error_type": type(error).__name__,
                "error": str(error),
                # 被擋下與速率限制的處置完全不同，記錄時就分開，
                # 免得日後翻日誌還要靠訊息字串猜
                "blocked": isinstance(error, LLMBlocked),
                "rate_limited": isinstance(error, LLMRateLimited),
                "timed_out": isinstance(error, LLMTimeout),
            },
        )


def build_router(
    path_configs: dict[str, PathConfig],
    *,
    api_keys: dict[str, str],
    timeout: float,
    max_retries: int = 2,
    retry_base_seconds: float = 1.0,
    fallback_path_configs: dict[str, PathConfig] | None = None,
) -> LLMRouter:
    """依三條路徑的設定建出 router。

    任何一條路徑設定有問題就整個建構失敗（LLMConfigError），
    不會「壞掉的那條靜靜不能用、其他照常」——那種狀態只會在
    使用者發訊時才暴露，而 5.2 要求設定錯誤在啟動時就報出來。

    fallback_path_configs 是主要模型重試用盡後的備援（見 config.py 的
    dialog_fallback_model），只有設定了備援的路徑才會出現在這個字典裡；
    同一驗證規則（不可用的供應商就整個建構失敗）也適用於備援設定。
    """
    from app.llm.registry import build_provider

    providers: dict[str, LLMProvider] = {}
    for name, config in path_configs.items():
        providers[name] = build_provider(
            config, api_keys.get(config.provider, ""), timeout
        )

    fallback_providers: dict[str, LLMProvider] = {}
    for name, config in (fallback_path_configs or {}).items():
        fallback_providers[name] = build_provider(
            config, api_keys.get(config.provider, ""), timeout
        )

    return LLMRouter(
        providers,
        fallback_providers=fallback_providers,
        max_retries=max_retries,
        retry_base_seconds=retry_base_seconds,
    )
