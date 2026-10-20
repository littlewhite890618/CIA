"""LLM 供應層測試（tasks 5.1–5.6）。

對應 openspec/changes/add-ig-persona-chat-mvp/specs/llm-provider/spec.md
的四個 Requirement：分路徑設定、供應商抽換、失敗降級、呼叫可觀測性。

不連外網：Gemini 以 httpx.MockTransport 攔下請求；退避等待注入假 sleep，
因此測試是毫秒級的。
"""

from __future__ import annotations

import asyncio
import json
import random

import httpx
import pytest

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
from app.llm.gemini import GeminiProvider
from app.llm.registry import PROVIDERS, build_provider, supported_providers
from app.llm.router import LLMRouter, build_router

SYSTEM = "你是晴晴，26 歲幼兒園老師。"
MESSAGES = [LLMMessage(role="user", content="今天過得還好嗎")]


# ═══════════════════════════════════════════════════════════
#  假的 Gemini 回應
# ═══════════════════════════════════════════════════════════

def _ok_body(text: str = "還不錯呀，你呢？", prompt_tokens=120, output_tokens=15) -> dict:
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": text}], "role": "model"},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": output_tokens,
            "totalTokenCount": prompt_tokens + output_tokens,
        },
        "modelVersion": "gemini-2.0-flash-001",
    }


def _provider(handler, model="gemini-2.0-flash") -> GeminiProvider:
    transport = httpx.MockTransport(handler)
    return GeminiProvider(
        "fake-key", model, client=httpx.AsyncClient(transport=transport)
    )


def _always(body: dict, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return handler


# ═══════════════════════════════════════════════════════════
#  5.3 供應商實作
# ═══════════════════════════════════════════════════════════

def test_成功呼叫_取得文字與token用量():
    provider = _provider(_always(_ok_body()))
    resp = asyncio.run(provider.generate(system_prompt=SYSTEM, messages=MESSAGES))

    assert resp.text == "還不錯呀，你呢？"
    assert resp.provider == "gemini"
    assert resp.prompt_tokens == 120
    assert resp.completion_tokens == 15
    assert resp.total_tokens == 135
    assert resp.elapsed_ms >= 0


def test_回報實際服務的模型版本而非設定值():
    """5.6 要記錄的是「實際用了哪個」，Gemini 會回更精確的版本號。"""
    provider = _provider(_always(_ok_body()), model="gemini-2.0-flash")
    resp = asyncio.run(provider.generate(system_prompt=SYSTEM, messages=MESSAGES))
    assert resp.model == "gemini-2.0-flash-001"


def test_system_prompt走systemInstruction而非混進對話():
    """混進 contents 的話，模型可能回頭複述人設內容。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_ok_body())

    asyncio.run(_provider(handler).generate(system_prompt=SYSTEM, messages=MESSAGES))

    assert captured["systemInstruction"]["parts"][0]["text"] == SYSTEM
    assert len(captured["contents"]) == 1
    assert SYSTEM not in json.dumps(captured["contents"], ensure_ascii=False)


def test_金鑰以標頭送出不放在網址():
    """放在 query string 會被寫進各種存取日誌。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers.get("x-goog-api-key")
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_ok_body())

    asyncio.run(_provider(handler).generate(system_prompt=SYSTEM, messages=MESSAGES))
    assert captured["key"] == "fake-key"
    assert "fake-key" not in captured["url"]


# ═══════════════════════════════════════════════════════════
#  5.4 失敗分類
# ═══════════════════════════════════════════════════════════

def test_429歸為速率限制():
    provider = _provider(_always({"error": "quota"}, status=429))
    with pytest.raises(LLMRateLimited):
        asyncio.run(provider.generate(system_prompt=SYSTEM, messages=MESSAGES))


def test_5xx歸為可重試():
    """供應商暫時性問題，與速率限制走同一條退避路徑。"""
    provider = _provider(_always({"error": "boom"}, status=503))
    with pytest.raises(LLMRateLimited):
        asyncio.run(provider.generate(system_prompt=SYSTEM, messages=MESSAGES))


def test_4xx歸為一般失敗不重試():
    provider = _provider(_always({"error": "bad key"}, status=401))
    with pytest.raises(LLMError) as exc:
        asyncio.run(provider.generate(system_prompt=SYSTEM, messages=MESSAGES))
    assert not isinstance(exc.value, LLMRateLimited)


def test_逾時中止():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    with pytest.raises(LLMTimeout):
        asyncio.run(_provider(handler).generate(system_prompt=SYSTEM, messages=MESSAGES))


def test_提示被安全機制擋下():
    body = {"promptFeedback": {"blockReason": "SAFETY"}}
    with pytest.raises(LLMBlocked):
        asyncio.run(_provider(_always(body)).generate(system_prompt=SYSTEM, messages=MESSAGES))


def test_回應被安全機制擋下():
    body = {"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}
    with pytest.raises(LLMBlocked):
        asyncio.run(_provider(_always(body)).generate(system_prompt=SYSTEM, messages=MESSAGES))


def test_空文字回應視為失敗():
    """送一則空訊息給使用者比重試更糟。"""
    body = {"candidates": [{"content": {"parts": [{"text": "  "}]}, "finishReason": "STOP"}]}
    with pytest.raises(LLMError):
        asyncio.run(_provider(_always(body)).generate(system_prompt=SYSTEM, messages=MESSAGES))


# ═══════════════════════════════════════════════════════════
#  5.1 分路徑設定 ／ 5.2 設定驗證
# ═══════════════════════════════════════════════════════════

class FakeProvider(LLMProvider):
    """測試用供應商。它的存在本身就是 5.3「抽換不需改業務邏輯」的證明——
    註冊進 PROVIDERS 後，router 與呼叫端一行都不必改。"""

    def __init__(self, api_key="k", model="fake-1", *, timeout=30.0, script=None):
        self._model = model
        self.calls = 0
        # script：依序拋出的例外，None 代表成功
        self._script = list(script or [])

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return self._model

    async def generate(self, *, system_prompt, messages, temperature=0.9, max_output_tokens=1024):
        self.calls += 1
        if self._script:
            item = self._script.pop(0)
            if item is not None:
                raise item
        return LLMResponse(
            text="假回應", provider="fake", model=self._model, elapsed_ms=1,
            prompt_tokens=10, completion_tokens=2,
        )


def test_三條路徑可各自指定不同供應商與模型():
    router = LLMRouter({
        "dialog": FakeProvider(model="fast-model"),
        "analysis": FakeProvider(model="cheap-model"),
        "coaching": FakeProvider(model="smart-model"),
    })
    assert router.paths == ["analysis", "coaching", "dialog"]
    assert router.provider_for("dialog").model == "fast-model"
    assert router.provider_for("coaching").model == "smart-model"


def test_變更單一路徑不影響其他路徑():
    dialog, analysis = FakeProvider(model="v2"), FakeProvider(model="v1")
    router = LLMRouter({"dialog": dialog, "analysis": analysis})
    asyncio.run(router.generate("dialog", system_prompt=SYSTEM, messages=MESSAGES))
    assert dialog.calls == 1
    assert analysis.calls == 0


def test_未實作的供應商在建構時報錯並指出環境變數():
    config = PathConfig(path="dialog", provider="openai", model="whatever")
    with pytest.raises(LLMConfigError) as exc:
        build_provider(config, "key", 30.0)
    message = str(exc.value)
    assert "openai" in message
    assert "DIALOG_PROVIDER" in message  # 要說該改哪裡，不只說「不支援」


def test_缺金鑰時報錯並指出要填哪個變數():
    config = PathConfig(path="dialog", provider="gemini", model="gemini-2.0-flash")
    with pytest.raises(LLMConfigError) as exc:
        build_provider(config, "", 30.0)
    assert "GEMINI_API_KEY" in str(exc.value)


def test_未設定的路徑名稱會報錯():
    router = LLMRouter({"dialog": FakeProvider()})
    with pytest.raises(LLMConfigError):
        router.provider_for("coaching")


def test_抽換供應商不需修改呼叫端(monkeypatch):
    """5.3：把 dialog 從 gemini 換成 fake，只動設定與註冊表。"""
    monkeypatch.setitem(PROVIDERS, "fake", FakeProvider)
    assert "fake" in supported_providers()

    router = build_router(
        {"dialog": PathConfig(path="dialog", provider="fake", model="fake-1")},
        api_keys={"fake": "key"},
        timeout=30.0,
    )
    text, resp = asyncio.run(
        router.generate_or_fallback("dialog", system_prompt=SYSTEM, messages=MESSAGES)
    )
    assert text == "假回應"
    assert resp is not None and resp.provider == "fake"


def test_build_router接受備援路徑設定(monkeypatch):
    monkeypatch.setitem(PROVIDERS, "fake", FakeProvider)

    router = build_router(
        {"dialog": PathConfig(path="dialog", provider="fake", model="fake-primary")},
        fallback_path_configs={
            "dialog": PathConfig(path="dialog", provider="fake", model="fake-fallback")
        },
        api_keys={"fake": "key"},
        timeout=30.0,
    )

    text, resp = asyncio.run(
        router.generate_or_fallback("dialog", system_prompt=SYSTEM, messages=MESSAGES)
    )
    assert text == "假回應"
    assert resp is not None and resp.provider == "fake"


def test_任一路徑設定壞掉就整個建構失敗():
    """不要「壞掉那條靜靜不能用」——那會等到使用者發訊才暴露。"""
    with pytest.raises(LLMConfigError):
        build_router(
            {
                "dialog": PathConfig(path="dialog", provider="gemini", model="m"),
                "analysis": PathConfig(path="analysis", provider="不存在", model="m"),
            },
            api_keys={"gemini": "key"},
            timeout=30.0,
        )


# ═══════════════════════════════════════════════════════════
#  5.4 退避重試
# ═══════════════════════════════════════════════════════════

class SleepRecorder:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def _router(script, *, max_retries=2, base=1.0):
    provider = FakeProvider(script=script)
    rec = SleepRecorder()
    router = LLMRouter(
        {"dialog": provider}, max_retries=max_retries,
        retry_base_seconds=base, sleep=rec,
    )
    return router, provider, rec


def test_速率限制退避後重試成功():
    router, provider, rec = _router([LLMRateLimited("429"), None])
    resp = asyncio.run(router.generate("dialog", system_prompt=SYSTEM, messages=MESSAGES))
    assert provider.calls == 2
    assert resp.attempts == 2
    assert rec.waits == [1.0]


def test_退避時間成指數增長():
    router, _p, rec = _router([LLMRateLimited("1"), LLMRateLimited("2"), None], base=2.0)
    asyncio.run(router.generate("dialog", system_prompt=SYSTEM, messages=MESSAGES))
    assert rec.waits == [2.0, 4.0]


def test_逾時同樣會重試():
    router, provider, _rec = _router([LLMTimeout("slow"), None])
    asyncio.run(router.generate("dialog", system_prompt=SYSTEM, messages=MESSAGES))
    assert provider.calls == 2


def test_被擋下不重試():
    """同樣的輸入必然再被擋，重試只是白等。"""
    router, provider, rec = _router([LLMBlocked("SAFETY")])
    with pytest.raises(LLMBlocked):
        asyncio.run(router.generate("dialog", system_prompt=SYSTEM, messages=MESSAGES))
    assert provider.calls == 1
    assert rec.waits == []


def test_重試用盡後拋出最後的例外():
    script = [LLMRateLimited("1"), LLMRateLimited("2"), LLMRateLimited("3")]
    router, provider, _rec = _router(script, max_retries=2)
    with pytest.raises(LLMRateLimited):
        asyncio.run(router.generate("dialog", system_prompt=SYSTEM, messages=MESSAGES))
    assert provider.calls == 3  # 首次 + 2 次重試


# ═══════════════════════════════════════════════════════════
#  備援模型：主要模型重試用盡後改試另一個模型
#
#  額度緊繃（免費層 RPD 20 次／天）時，重試同一個模型只是白等，
#  換一個模型才有機會成功。只接在有設定備援的路徑，見
#  config.py 的 dialog_fallback_model。
# ═══════════════════════════════════════════════════════════

def _router_with_fallback(primary_script, fallback_script, *, max_retries=2):
    primary = FakeProvider(model="primary-1", script=primary_script)
    fallback = FakeProvider(model="fallback-1", script=fallback_script)
    rec = SleepRecorder()
    router = LLMRouter(
        {"dialog": primary},
        fallback_providers={"dialog": fallback},
        max_retries=max_retries,
        sleep=rec,
    )
    return router, primary, fallback, rec


def test_主要模型重試用盡後改試備援並成功():
    primary_fails = [LLMRateLimited("1"), LLMRateLimited("2"), LLMRateLimited("3")]
    router, primary, fallback, _rec = _router_with_fallback(primary_fails, [None])

    resp = asyncio.run(router.generate("dialog", system_prompt=SYSTEM, messages=MESSAGES))

    assert resp.model == "fallback-1"
    assert primary.calls == 3  # 首次 + 2 次重試，全部用盡才換
    assert fallback.calls == 1


def test_備援也失敗時拋出主要模型的錯誤而非備援的():
    """降級語句該對應根本原因（主要模型為何不能用），備援只是次要嘗試。"""
    primary_fails = [LLMRateLimited("primary 429")]
    fallback_fails = [LLMRateLimited("fallback 429")]
    router, _primary, _fallback, _rec = _router_with_fallback(
        primary_fails, fallback_fails, max_retries=0
    )

    with pytest.raises(LLMRateLimited, match="primary 429"):
        asyncio.run(router.generate("dialog", system_prompt=SYSTEM, messages=MESSAGES))


def test_沒有備援的路徑行為不受影響():
    """analysis／coaching 沒設備援，重試用盡就直接拋出，跟改動前一樣。"""
    script = [LLMRateLimited("1"), LLMRateLimited("2"), LLMRateLimited("3")]
    router, provider, _rec = _router(script, max_retries=2)  # 沒有 fallback_providers
    with pytest.raises(LLMRateLimited):
        asyncio.run(router.generate("dialog", system_prompt=SYSTEM, messages=MESSAGES))
    assert provider.calls == 3


def test_下一次呼叫仍優先嘗試主要模型():
    """不會因為上次切去用了備援，這次就跳過主要模型——每次呼叫獨立判斷，
    額度一旦恢復就自動用回主要模型，不需要人工切回或額外狀態。"""
    primary = FakeProvider(model="primary-1", script=[LLMRateLimited("x"), None])
    fallback = FakeProvider(model="fallback-1", script=[None])
    router = LLMRouter(
        {"dialog": primary},
        fallback_providers={"dialog": fallback},
        max_retries=2,
        sleep=SleepRecorder(),
    )

    first = asyncio.run(router.generate("dialog", system_prompt=SYSTEM, messages=MESSAGES))
    assert first.model == "primary-1"  # 第一次重試一次就成功，用不到備援
    assert fallback.calls == 0


def test_切換備援後generate_or_fallback回傳真正生成的內容而非降級語句():
    primary_fails = [LLMRateLimited("1"), LLMRateLimited("2"), LLMRateLimited("3")]
    router, _primary, _fallback, _rec = _router_with_fallback(primary_fails, [None])

    text, resp = asyncio.run(
        router.generate_or_fallback("dialog", system_prompt=SYSTEM, messages=MESSAGES)
    )
    assert text == "假回應"
    assert resp is not None and resp.model == "fallback-1"


# ═══════════════════════════════════════════════════════════
#  5.5 降級回應
# ═══════════════════════════════════════════════════════════

def test_失敗時回傳降級語句而非拋出例外():
    router, _p, _rec = _router([LLMRateLimited("x"), LLMRateLimited("x"), LLMRateLimited("x")])
    text, resp = asyncio.run(
        router.generate_or_fallback("dialog", system_prompt=SYSTEM, messages=MESSAGES)
    )
    assert text in DIALOG_FALLBACKS
    assert resp is None  # 呼叫端據此分辨這次是降級而非真的生成


def test_被擋下時也走降級():
    router, _p, _rec = _router([LLMBlocked("SAFETY")])
    text, resp = asyncio.run(
        router.generate_or_fallback("dialog", system_prompt=SYSTEM, messages=MESSAGES)
    )
    assert text in DIALOG_FALLBACKS
    assert resp is None


def test_成功時回傳模型的文字與用量():
    router, _p, _rec = _router([None])
    text, resp = asyncio.run(
        router.generate_or_fallback("dialog", system_prompt=SYSTEM, messages=MESSAGES)
    )
    assert text == "假回應"
    assert resp is not None and resp.total_tokens == 12


def test_降級語句不含系統錯誤字眼():
    """使用者不該看到「錯誤」「失敗」「系統」——那會直接拆穿角色。"""
    forbidden = ("錯誤", "失敗", "系統", "error", "Error", "逾時", "重試")
    for line in DIALOG_FALLBACKS:
        assert not any(word in line for word in forbidden), line


def test_降級語句會變換不會每次都同一句():
    """模型失敗常連續發生，每次回同一句馬上就被看出是罐頭訊息。"""
    picks = {pick_fallback(random.Random(seed)) for seed in range(30)}
    assert len(picks) > 1


# ═══════════════════════════════════════════════════════════
#  思考模式與截斷處理
#  （2026-09-15 實測 gemini-3.6-flash 發現，見 gemini.py 的註解）
# ═══════════════════════════════════════════════════════════

def _captured_config(provider: GeminiProvider) -> dict:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_ok_body())

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(provider.generate(system_prompt=SYSTEM, messages=MESSAGES))
    return captured["generationConfig"]


def test_預設思考等級為最低檔():
    """思考額度會計入 maxOutputTokens，開著會一邊變慢一邊把回覆擠掉。

    不送 thinkingBudget=0：gemini-3.5-flash-lite 不能完全關閉思考，
    收到 0 會回 400（2026-09-22 實測，見 gemini.py 註解）。
    """
    config = _captured_config(GeminiProvider("k", "gemini-3.5-flash-lite"))
    assert config["thinkingConfig"] == {"thinkingLevel": "minimal"}


def test_思考額度可調高供教練回饋路徑使用():
    config = _captured_config(GeminiProvider("k", "m", thinking_budget=1024))
    assert config["thinkingConfig"] == {"thinkingBudget": 1024}


def test_思考等級與額度皆為None時交給模型自行決定():
    config = _captured_config(GeminiProvider("k", "m", thinking_level=None, thinking_budget=None))
    assert "thinkingConfig" not in config


def test_註冊表extra設為auto時不送思考設定():
    config = PathConfig(
        path="coaching", provider="gemini", model="m",
        extra={"thinking_budget": "auto"},
    )
    provider = build_provider(config, "key", 30.0)
    assert provider._thinking_level is None
    assert provider._thinking_budget is None


def test_註冊表可透過extra覆寫思考額度():
    config = PathConfig(
        path="coaching", provider="gemini", model="m",
        extra={"thinking_budget": "2048"},
    )
    provider = build_provider(config, "key", 30.0)
    assert provider._thinking_budget == 2048


def test_觸及token上限時退回最後一個完整句子():
    """原樣送出「今天一定處理了很多繁」這種斷句比少講一句更破壞擬真感。"""
    body = {
        "candidates": [
            {
                "content": {"parts": [{"text": "辛苦你了。今天一定處理了很多繁"}]},
                "finishReason": "MAX_TOKENS",
            }
        ]
    }
    resp = asyncio.run(
        _provider(_always(body)).generate(system_prompt=SYSTEM, messages=MESSAGES)
    )
    assert resp.text == "辛苦你了。"


def test_截斷內容沒有任何句末標點時原樣保留():
    """砍掉等於什麼都不剩，寧可送略嫌突兀的一句也不要送空訊息。"""
    body = {
        "candidates": [
            {"content": {"parts": [{"text": "今天一定處理了很多繁"}]}, "finishReason": "MAX_TOKENS"}
        ]
    }
    resp = asyncio.run(
        _provider(_always(body)).generate(system_prompt=SYSTEM, messages=MESSAGES)
    )
    assert resp.text == "今天一定處理了很多繁"


def test_正常結束的回應不做截斷處理():
    body = _ok_body(text="還不錯呀。你呢")
    resp = asyncio.run(
        _provider(_always(body)).generate(system_prompt=SYSTEM, messages=MESSAGES)
    )
    assert resp.text == "還不錯呀。你呢"
