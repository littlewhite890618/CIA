"""Google Gemini 供應商（task 5.3）。

選它的理由：AI Studio 提供免費額度，POC 不必先處理付費帳務。
design.md 已載明 MVP 使用免費模型，且供應層必須把失敗視為常態。

直接打 REST API 而不裝 google-genai SDK，與 app/channels/instagram.py
的作法一致：多一個 SDK 就多一組版本相依與一層自己的重試邏輯，
而我們只需要 generateContent 這一個端點。

API 形狀（v1beta）：
    POST {BASE}/models/{model}:generateContent
    header  x-goog-api-key: <金鑰>
    body    {systemInstruction, contents[], generationConfig}
    回應    {candidates[].content.parts[].text, usageMetadata}
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.channels.splitter import SENTENCE_ENDINGS
from app.llm.base import (
    LLMBlocked,
    LLMError,
    LLMMessage,
    LLMProvider,
    LLMRateLimited,
    LLMResponse,
    LLMTimeout,
)
from app.logging_config import get_logger

log = get_logger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# 供應商判定「不予回應」的完成原因。這些重試也不會變好。
# STOP 是正常結束；MAX_TOKENS 有內容只是被截斷，不算被擋。
_BLOCKED_FINISH_REASONS = frozenset(
    {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}
)


def _trim_to_last_sentence(text: str) -> str:
    """砍掉被截斷的最後半句。找不到完整句子時原樣回傳。

    標點清單沿用 app/channels/splitter.py 的 SENTENCE_ENDINGS，不另存一份。
    """
    # 必須先濾掉 rfind == -1 的分隔符再加長度：`"! "` 這種兩字元的分隔符
    # 找不到時會算出 -1 + 2 = 1，讓整段被砍到只剩第一個字。
    positions = [
        idx + len(sep)
        for sep in SENTENCE_ENDINGS
        if (idx := text.rfind(sep)) != -1
    ]
    best = max(positions, default=0)
    if best <= 0:
        # 整段連一個句末標點都沒有。此時砍掉等於什麼都不剩，
        # 寧可送出略嫌突兀的一句，也不要送空訊息。
        return text
    return text[:best].strip()


class GeminiProvider(LLMProvider):
    """Gemini 的 generateContent 端點。

    一個實例對應一條路徑的設定（一個模型）。三條路徑若用不同模型，
    就會有三個實例，各自持有自己的 httpx 客戶端。
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = 30.0,
        thinking_level: str | None = "minimal",
        thinking_budget: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        # 預設關閉「思考」。2026-09-15 以 gemini-3.6-flash 實測：
        #     思考開啟（預設）  4.0 秒，thoughts 吃掉 244/256 額度，
        #                       回覆只剩 8 token 且被 MAX_TOKENS 截在句中
        #     thinkingBudget=0  2.1 秒，完整回覆 41 token
        # 對話路徑要的是快，而閒聊不需要推理；思考額度**會計入
        # maxOutputTokens**，開著等於一邊變慢一邊把回覆擠掉。
        # 教練回饋路徑（M5）需要推理時，再於該路徑設定為 None 開啟。
        #
        # 2026-09-22 改送 thinkingLevel="minimal" 而非 thinkingBudget=0：
        # gemini-3.5-flash-lite 不能完全關閉思考（官方文件列的等級只有
        # minimal／low／medium／high），收到 thinkingBudget=0 一律回
        # HTTP 400 INVALID_ARGUMENT。備援機制把這個錯誤藏了起來——
        # dialog／analysis 兩條路徑的主模型每次都失敗、實際全由備援
        # 3.1-flash-lite 回覆，只在日誌看得到。實測兩個模型都接受 minimal：
        #     3.5-flash-lite  thinkingBudget=0 → 400；minimal → 1.3 秒，思考 0 token
        #     3.1-flash-lite  thinkingBudget=0 → OK； minimal → 2.5 秒，思考 0 token
        # thinking_budget 有值時優先送 thinkingBudget（供需要精確額度的路徑用）；
        # 兩者皆 None 代表交給模型自行決定，不送 thinkingConfig。
        self._thinking_level = thinking_level
        self._thinking_budget = thinking_budget
        # client 可注入，讓測試以 httpx.MockTransport 攔截請求，
        # 不必 monkeypatch 模組全域，也不必真的連外網。
        #
        # 未注入時**延遲到第一次呼叫才建立**：httpx.AsyncClient 的建構會
        # 載入 CA 憑證並建出 SSL context，約需 0.3 秒。三條路徑就是 1 秒，
        # 而且是在服務啟動的關鍵路徑上——實測讓每個 webhook 測試的 fixture
        # 都多花 1.1 秒。延遲建立後，不呼叫模型的流程完全不必付這個成本。
        self._client = client

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def model(self) -> str:
        return self._model

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        # 從未呼叫過就沒有客戶端可關，不必為了關閉而先建一個出來
        if self._client is not None:
            await self._client.aclose()

    # ── 請求組裝 ──────────────────────────────────────

    def _build_payload(
        self,
        system_prompt: str,
        messages: list[LLMMessage],
        temperature: float,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        generation_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
        }
        # 兩者皆 None 代表交給模型自行決定（不送此欄位），見 __init__ 註解
        if self._thinking_budget is not None:
            generation_config["thinkingConfig"] = {
                "thinkingBudget": self._thinking_budget
            }
        elif self._thinking_level is not None:
            generation_config["thinkingConfig"] = {
                "thinkingLevel": self._thinking_level
            }

        payload: dict[str, Any] = {
            "contents": [
                {"role": m.role, "parts": [{"text": m.content}]} for m in messages
            ],
            "generationConfig": generation_config,
        }
        # 人設卡渲染出來的 system prompt 走 systemInstruction，不混進 contents。
        # 混進去的話模型會把它當成對話的一部分，可能回頭「複述」人設內容。
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        return payload

    # ── 呼叫 ──────────────────────────────────────────

    async def generate(
        self,
        *,
        system_prompt: str,
        messages: list[LLMMessage],
        temperature: float = 0.9,
        max_output_tokens: int = 1024,
    ) -> LLMResponse:
        url = f"{API_BASE}/models/{self._model}:generateContent"
        payload = self._build_payload(
            system_prompt, messages, temperature, max_output_tokens
        )

        started = time.perf_counter()
        try:
            resp = await self._http.post(
                url,
                headers={
                    "x-goog-api-key": self._api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.TimeoutException as exc:
            # 5.4：逾時即中止，不讓使用者無限等待
            raise LLMTimeout(f"呼叫逾時：{exc}") from exc
        except httpx.RequestError as exc:
            raise LLMError(f"連線失敗：{exc}") from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)

        self._raise_for_status(resp)
        return self._parse(resp, elapsed_ms, self._model)

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code == 429:
            # 原本只丟 "HTTP 429"，看不出撞到的是 RPM／TPM／RPD 哪一種
            # 配額，也看不到 Google 建議的重試等待秒數。Gemini 的 429
            # body 通常帶 error.status（多半是 RESOURCE_EXHAUSTED）、
            # error.message，以及 details 裡的 QuotaFailure／RetryInfo——
            # 把這些原樣記下來，才不用每次都靠猜的。
            raise LLMRateLimited(f"HTTP 429: {resp.text[:500]}")

        if resp.is_error:
            detail = resp.text[:200]
            # 5xx 多半是供應商暫時性問題，與速率限制一樣值得重試，
            # 因此歸為 LLMRateLimited 讓上層走同一條退避路徑。
            if resp.status_code >= 500:
                raise LLMRateLimited(f"HTTP {resp.status_code}: {detail}")
            raise LLMError(f"HTTP {resp.status_code}: {detail}")

    @staticmethod
    def _parse(resp: httpx.Response, elapsed_ms: int, configured_model: str) -> LLMResponse:
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError("回應不是合法 JSON") from exc

        # 整個提示被擋下時連 candidates 都不會有
        block_reason = (data.get("promptFeedback") or {}).get("blockReason")
        if block_reason:
            raise LLMBlocked(f"提示被擋下：{block_reason}")

        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError("回應不含任何 candidate")

        candidate = candidates[0]
        finish = candidate.get("finishReason")
        if finish in _BLOCKED_FINISH_REASONS:
            raise LLMBlocked(f"回應被擋下：{finish}")

        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            # 有 candidate 但沒有文字。多半是被截斷在開頭，或安全機制的
            # 邊界案例。當作失敗讓上層重試，好過送一則空訊息給使用者。
            raise LLMError(f"回應沒有文字內容（finishReason={finish}）")

        if finish == "MAX_TOKENS":
            # 額度用盡，最後一句被切在半途。原樣送出會是「今天一定處理了很多繁」
            # 這種斷句，比少講一句話更破壞擬真感，所以退回最後一個完整句子。
            text = _trim_to_last_sentence(text)
            log.warning(
                "回應觸及 maxOutputTokens，已退回最後一個完整句子",
                extra={"model": configured_model, "trimmed_to": len(text)},
            )

        usage = data.get("usageMetadata") or {}
        return LLMResponse(
            text=text,
            provider="gemini",
            # Gemini 會回報實際服務的版本（可能比設定值更精確，
            # 如 gemini-2.0-flash -> gemini-2.0-flash-001）。取得到就用它，
            # 這是 5.6 要記錄的「實際用了哪個模型」而非「我們要求哪個」。
            model=data.get("modelVersion") or configured_model,
            elapsed_ms=elapsed_ms,
            prompt_tokens=usage.get("promptTokenCount"),
            completion_tokens=usage.get("candidatesTokenCount"),
        )
