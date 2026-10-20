"""訊號偵測（task 8.2）。

session 結束時，把整段對話一次送給 analysis 路徑，逐則標記使用者訊息的
地雷／句點／亮點訊號。判定準則見 docs/即時訊號判定準則.md，時機的取捨見
design.md 決策 6（2026-09-22 由逐則並行改為 session 結束後一次分析）。

分工：模型只回「哪一則訊息命中哪個項目代號」，類型、嚴重程度、維度一律
由 SIGNAL_ITEMS 查表填入——讓模型自己填這些欄位，同一種行為這次給 high、
下次給 medium，維度名稱也可能寫錯字，報告撈資料時就對不上。

不交給模型的部分：
- L10 訊息轟炸：看時間戳記就能算，模型只看文字反而判斷不準。
- L3 的「前三次對話」：模型只判斷「是不是在約見面」，第幾次由程式數。

失敗不拋出例外，而是回傳 ok=False 的 DetectionResult（task 8.4）：
呼叫端據此把場次標成 failed、不寫入訊號，事後用 scripts/rerun_signals.py
重跑。不能像 topics.py 那樣失敗就回傳空清單——那會讓「偵測失敗」與
「這場真的沒有訊號」在資料庫裡看起來一模一樣。落庫見 signal_job.py。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.db.models import Message, MessageDirection, Severity, SignalType
from app.llm import LLMError, LLMMessage
from app.llm.router import LLMRouter
from app.logging_config import get_logger
from app.personas.schema import Boundaries

log = get_logger(__name__)

# 六項評分維度，定義見 docs/技術可行性與架構規劃.md §3.4.5
DIMENSIONS = frozenset({"提問品質", "接話與延展", "情緒感知", "自我揭露平衡", "分寸與界線", "節奏"})


@dataclass(frozen=True)
class SignalItem:
    signal_type: str
    severity: str | None
    dimension: str
    description: str


SIGNAL_ITEMS: dict[str, SignalItem] = {
    "L1": SignalItem(SignalType.LANDMINE, Severity.HIGH, "分寸與界線", "索取電話、LINE 或其他聯絡方式"),
    "L2": SignalItem(SignalType.LANDMINE, Severity.HIGH, "分寸與界線", "性暗示、性要求、黃色笑話、過早涉及身體話題"),
    "L3": SignalItem(SignalType.LANDMINE, Severity.HIGH, "分寸與界線", "約對方見面"),
    "L4": SignalItem(SignalType.LANDMINE, Severity.MEDIUM, "分寸與界線", "評論對方的身材或外貌"),
    "L5": SignalItem(SignalType.LANDMINE, Severity.MEDIUM, "分寸與界線", "貶低型玩笑、貶低女性"),
    "L6": SignalItem(SignalType.LANDMINE, Severity.MEDIUM, "分寸與界線", "抱怨前任"),
    "L7": SignalItem(SignalType.LANDMINE, Severity.MEDIUM, "自我揭露平衡", "連續三則以上只講自己、完全沒問對方"),
    "L8": SignalItem(SignalType.LANDMINE, Severity.MEDIUM, "提問品質", "連續查戶口式提問（哪裡人、住哪、幾歲）"),
    "L9": SignalItem(SignalType.LANDMINE, Severity.LOW, "自我揭露平衡", "過度自誇"),
    "L10": SignalItem(SignalType.LANDMINE, Severity.LOW, "節奏", "短時間內連發多則訊息轟炸"),
    "K1": SignalItem(SignalType.CONVERSATION_KILLER, None, "接話與延展", "敷衍回應：只回「嗯／喔／哈哈」等，沒有實質內容"),
    "K2": SignalItem(SignalType.CONVERSATION_KILLER, None, "提問品質", "連發封閉式問題（只能回是／否的問題連續出現）"),
    "K3": SignalItem(SignalType.CONVERSATION_KILLER, None, "接話與延展", "完全未回應對方拋出的話題"),
    "H1": SignalItem(SignalType.HIGHLIGHT, None, "提問品質", "開放式提問（無法用是／否回答）"),
    "H2": SignalItem(SignalType.HIGHLIGHT, None, "接話與延展", "成功接住對方拋出的話題"),
    "H3": SignalItem(SignalType.HIGHLIGHT, None, "自我揭露平衡", "適當的自我揭露（與話題相關、份量適中，也有把話丟回對方）"),
    "H4": SignalItem(SignalType.HIGHLIGHT, None, "情緒感知", "讀懂並具體回應對方的情緒（不是泛泛的「加油」）"),
}

# 由程式判斷、不讓模型標記的項目
_PROGRAM_ONLY_ITEMS = frozenset({"L10"})

# 使用者自訂規則在提示裡的代號前綴（C1、C2…），與系統預設的 L／K／H 分開
CUSTOM_CODE_PREFIX = "C"

_TYPE_NAME = {
    SignalType.LANDMINE: "地雷",
    SignalType.CONVERSATION_KILLER: "句點",
    SignalType.HIGHLIGHT: "亮點",
}


def build_rule_set(coaching: Mapping[str, Any] | None) -> dict[str, SignalItem]:
    """依使用者版本的教練設定組出這場要偵測的項目（add-persona-authoring-console 任務 7.1）。

    - 沒有教練設定（未設定者、平台預設版本）：原封不動回傳 SIGNAL_ITEMS
    - 停用的預設規則：整條拿掉，模型看不到、程式也不判斷（含 L10）
    - 自訂規則：依序給代號 C1、C2…，類型、嚴重程度、維度取自設定，
      跟預設規則一樣由查表填入，不讓模型自己填

    設定在保存時已經驗證（coaching_settings.validate_coaching），這裡仍略過
    類型或維度不合法的自訂規則：資料庫裡的值不保證一定經過那道驗證。
    """
    if not coaching:
        return SIGNAL_ITEMS
    disabled = set(coaching.get("disabledRuleIds") or [])
    items = {code: spec for code, spec in SIGNAL_ITEMS.items() if code not in disabled}
    number = 0
    for rule in coaching.get("customRules") or []:
        if not isinstance(rule, Mapping) or rule.get("id") in disabled:
            continue
        signal_type = rule.get("type")
        dimension = rule.get("dimension")
        text = str(rule.get("text") or "").strip()
        if signal_type not in _TYPE_NAME or dimension not in DIMENSIONS or not text:
            log.warning("略過不合法的自訂規則", extra={"rule": dict(rule)})
            continue
        severity = rule.get("severity") if signal_type == SignalType.LANDMINE else None
        if signal_type == SignalType.LANDMINE and severity not in {Severity.HIGH, Severity.MEDIUM, Severity.LOW}:
            severity = Severity.MEDIUM
        number += 1
        items[f"{CUSTOM_CODE_PREFIX}{number}"] = SignalItem(signal_type, severity, dimension, text)
    return items

# L3 只在前幾次對話算地雷，對應人設卡「前三次對話就約見面」
EARLY_SESSION_LIMIT = 3


@dataclass(frozen=True)
class DetectedSignal:
    message_id: int
    item: str
    signal_type: str
    severity: str | None
    dimension: str
    reason: str


@dataclass(frozen=True)
class DetectionResult:
    ok: bool
    signals: list[DetectedSignal]
    error: str | None = None


class SignalParseError(ValueError):
    """模型輸出不是可解析的 JSON 陣列。與「合法的空陣列」分開，
    後者代表這場沒有訊號，前者代表偵測失敗。"""


def _make_signal(
    message_id: int, item: str, reason: str, items: Mapping[str, SignalItem] = SIGNAL_ITEMS
) -> DetectedSignal:
    spec = items[item]
    return DetectedSignal(
        message_id=message_id,
        item=item,
        signal_type=spec.signal_type,
        severity=spec.severity,
        dimension=spec.dimension,
        reason=reason,
    )


# ═══════════════════════════════════════════════════════════
#  提示組裝
# ═══════════════════════════════════════════════════════════

# 判定要點：(相關代號, 說明)。相關代號全被停用時不列出，免得模型去找清單上沒有的項目
_JUDGING_NOTES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"K1"}), "- K1：角色剛講完一段有內容的話、使用者只回「嗯」這類才算，單獨一次寒暄不算。"),
    (frozenset({"K2"}), "- K2：至少連續兩則封閉式問題才算。"),
    (frozenset({"K3"}), "- K3：角色的上一則有明確拋出話題或問題，使用者完全沒接。"),
    (frozenset({"L7", "L8"}), "- L7、L8：跨多則的模式，只標在達成條件的那一則，不回頭標前面的訊息。"),
    (frozenset({"H3"}), "- H3：一直講自己不算亮點，那是 L7。"),
    (frozenset({"L3"}), "- L3：只要是在約見面就標，不必考慮是第幾次對話。"),
)


def build_system_prompt(
    boundaries: Boundaries, items: Mapping[str, SignalItem] = SIGNAL_ITEMS
) -> str:
    """判定準則 + 當前角色的邊界。角色邊界是判斷參考，代號仍用固定清單，
    命中角色邊界時選最接近的代號（準則未定義「其他地雷」，見準則文件）。

    items 是這場要偵測的項目（build_rule_set）；預設為 SIGNAL_ITEMS，
    此時輸出與導入教練設定前逐字相同。"""
    has_custom = any(code.startswith(CUSTOM_CODE_PREFIX) for code in items)
    lines = "\n".join(
        f"{code}：{spec.description}"
        + (f"（{_TYPE_NAME[spec.signal_type]}）" if code.startswith(CUSTOM_CODE_PREFIX) else "")
        for code, spec in items.items()
        if code not in _PROGRAM_ONLY_ITEMS
    )
    header = "【項目】L 開頭是地雷、K 開頭是句點、H 開頭是亮點"
    if has_custom:
        header += "、C 開頭是使用者自訂的項目（類型標在括號內）"
    notes = "\n".join(text for codes, text in _JUDGING_NOTES if codes & set(items))
    cold = "\n".join(f"- {b}" for b in boundaries.cools_down_on)
    never = "\n".join(f"- {b}" for b in boundaries.never_engages)
    return (
        "你是對話教練的分析助手。以下是使用者與 AI 角色的一段私訊對話，"
        "使用者的每則訊息前面標有編號，例如 [#12]。\n"
        "請逐則檢查使用者的訊息，找出命中下列項目的訊息。只分析使用者的訊息，"
        "不分析角色的訊息；角色的訊息只用來判斷使用者有沒有接話、有沒有回應情緒。\n\n"
        f"{header}\n"
        f"{lines}\n\n"
        + (f"【判定要點】\n{notes}\n\n" if notes else "")
        +
        "【這個角色的邊界】命中時選最接近的代號\n"
        f"會讓她變冷淡：\n{cold}\n"
        f"她絕對不接：\n{never}\n\n"
        "【輸出格式】只輸出一個 JSON 陣列，不要任何說明文字：\n"
        '[{"message_id": 12, "item": "H1", "reason": "判定原因，引用使用者原句"}]\n'
        "一則訊息可以命中多個項目，各列一筆。沒有任何命中就輸出 []。"
    )


def format_transcript(messages: list[Message]) -> str:
    """把訊息紀錄組成逐字稿。

    角色的回覆常因擬真節奏（見 app/dialog/pacing.py 的 task 7.8）被拆成
    好幾個泡泡分開送出，這裡先把連續的角色訊息合併成一行——否則同一次
    回覆在逐字稿裡會變成好幾行分開的「角色：」，讓模型在判斷「使用者這句
    話是接在角色哪一句之後」時容易把順序搞混（2026-09-24 真機測試發現：
    使用者只回「嗯嗯」，模型誤判成回應的是後面才問的問題，而非緊接在前
    的那句，推測與這個現象有關）。

    使用者的訊息不合併：每一則都是獨立動作，L10 訊息轟炸偵測（見
    detect_bursts）也需要看到分開的則數與個別時間戳記，且每則都要保留
    自己的 [#id] 供訊號歸因。
    """
    lines: list[str] = []
    pending_outbound: list[str] = []

    def flush_outbound() -> None:
        if pending_outbound:
            lines.append(f"角色：{' '.join(pending_outbound)}")
            pending_outbound.clear()

    for m in messages:
        if not (m.content and m.content.strip()):
            continue
        if m.direction == MessageDirection.INBOUND:
            flush_outbound()
            lines.append(f"[#{m.id}] 使用者：{m.content.strip()}")
        else:
            pending_outbound.append(m.content.strip())
    flush_outbound()

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  解析與規則
# ═══════════════════════════════════════════════════════════

def parse_signals(
    text: str, valid_message_ids: set[int], items: Mapping[str, SignalItem] = SIGNAL_ITEMS
) -> list[DetectedSignal]:
    """把模型輸出轉成訊號。代號不存在、屬於程式判斷的項目、或 message_id
    不是這場的使用者訊息，都整筆丟掉；整份格式壞掉拋出 SignalParseError。"""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end < start:
        raise SignalParseError(f"輸出中找不到 JSON 陣列：{text[:200]}")
    try:
        raw = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SignalParseError(f"輸出不是合法 JSON：{text[:200]}") from exc
    if not isinstance(raw, list):
        raise SignalParseError(f"輸出不是 JSON 陣列：{text[:200]}")

    signals: list[DetectedSignal] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        item = str(entry.get("item", "")).strip().upper()
        message_id = entry.get("message_id")
        if isinstance(message_id, str) and message_id.strip().lstrip("#").isdigit():
            message_id = int(message_id.strip().lstrip("#"))
        if (
            item not in items
            or item in _PROGRAM_ONLY_ITEMS
            or not isinstance(message_id, int)
            or message_id not in valid_message_ids
        ):
            log.warning("丟棄不合法的訊號", extra={"entry": entry})
            continue
        signals.append(_make_signal(message_id, item, str(entry.get("reason", "")).strip(), items))
    return signals


def apply_session_rules(signals: list[DetectedSignal], session_number: int) -> list[DetectedSignal]:
    """L3 只在前 EARLY_SESSION_LIMIT 次對話算地雷。"""
    if session_number <= EARLY_SESSION_LIMIT:
        return signals
    return [s for s in signals if s.item != "L3"]


def detect_bursts(
    messages: list[Message], window_seconds: int, min_messages: int
) -> list[DetectedSignal]:
    """L10：角色沒插話的連續使用者訊息裡，window_seconds 內達 min_messages 則
    即命中，記在達標的那一則。同一段連發只記一次。"""
    window = timedelta(seconds=window_seconds)
    signals: list[DetectedSignal] = []
    run: list[Message] = []
    flagged = False

    for m in messages:
        if m.direction != MessageDirection.INBOUND:
            run, flagged = [], False
            continue
        run.append(m)
        if flagged:
            continue
        recent = [r for r in run if m.sent_at - r.sent_at <= window]
        if len(recent) >= min_messages:
            signals.append(
                _make_signal(m.id, "L10", f"{window_seconds} 秒內連發 {len(recent)} 則訊息")
            )
            flagged = True
    return signals


# ═══════════════════════════════════════════════════════════
#  進入點
# ═══════════════════════════════════════════════════════════

async def detect_signals(
    router: LLMRouter,
    messages: list[Message],
    boundaries: Boundaries,
    *,
    session_number: int,
    burst_window_seconds: int,
    burst_min_messages: int,
    coaching: Mapping[str, Any] | None = None,
) -> DetectionResult:
    """偵測一整場 session 的訊號。messages 需依時間正序。

    coaching 是該 session 所套用之使用者版本的教練設定；None 表示沒有設定，
    沿用系統預設規則（行為與導入教練設定前相同）。"""
    items = build_rule_set(coaching)
    inbound_ids = {
        m.id for m in messages
        if m.direction == MessageDirection.INBOUND and m.content and m.content.strip()
    }
    if not inbound_ids:
        return DetectionResult(ok=True, signals=[])

    bursts = (
        detect_bursts(messages, burst_window_seconds, burst_min_messages) if "L10" in items else []
    )

    # 用 generate 而非 generate_or_fallback：後者失敗時回傳對話用的降級
    # 語句並吞掉錯誤原因，這裡需要原因寫進 sessions.signal_error。
    # 整段對話包成單一使用者訊息：以模型輪次結尾會被 Gemini 400 拒收（見 topics.py）
    try:
        response = await router.generate(
            "analysis",
            system_prompt=build_system_prompt(boundaries, items),
            messages=[LLMMessage(role="user", content=format_transcript(messages))],
            temperature=0.2,  # 判定要穩定一致
            max_output_tokens=2048,
        )
        llm_signals = parse_signals(response.text, inbound_ids, items)
    except (LLMError, SignalParseError) as exc:
        log.warning("訊號偵測失敗", extra={"error_type": type(exc).__name__, "error": str(exc)[:500]})
        return DetectionResult(ok=False, signals=[], error=f"{type(exc).__name__}: {exc}"[:1000])
    except Exception as exc:
        log.exception("訊號偵測發生未預期例外")
        return DetectionResult(ok=False, signals=[], error=f"{type(exc).__name__}: {exc}"[:1000])

    return DetectionResult(ok=True, signals=apply_session_rules(llm_signals, session_number) + bursts)
