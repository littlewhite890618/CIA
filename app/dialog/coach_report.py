"""教練報告：邀請文案／payload、生成、送達文字格式化（M5＋M9）。

生成以該 session 已落庫的行為訊號（app/dialog/signals.py 偵測、
app/dialog/signal_job.py 落庫）為**唯一**資料來源，不重新讀取原始對話——
訊號的 reason 欄位在偵測當下就已引用使用者原句（見 signals.py 的輸出格式
要求：`"reason": "判定原因，引用使用者原句"`），報告不需要再看一次逐則訊息，
也對應 design.md「唯一資料來源」的定位（省一次重複的模型呼叫）。

走 coaching 路徑而非 dialog：不在使用者等待的即時路徑上，對延遲、語氣
沒有要求，可用比對話更強、更貴的模型（design.md 決策 4）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.db.models import RealtimeSignal, SignalType
from app.llm import LLMError, LLMMessage
from app.dialog.traditional import LANGUAGE_RULE, to_traditional
from app.llm.router import LLMRouter
from app.logging_config import get_logger
from app.personas.coaching_settings import DEFAULT_LENGTH, DEFAULT_TONE, default_coaching

log = get_logger(__name__)

REPORT_SECTIONS = ("摘要", "做得好", "可調整", "改寫示範", "下次可試")

_TYPE_LABEL = {
    SignalType.LANDMINE: "地雷",
    SignalType.CONVERSATION_KILLER: "句點",
    SignalType.HIGHLIGHT: "亮點",
}

_SECTION_DISPLAY_LABEL = {
    "摘要": "這次聊起來的感覺",
    "做得好": "做得好的地方",
    "可調整": "可以再調整的地方",
    "改寫示範": "換個說法試試",
    "下次可試": "下次可以試試",
}

# ═══════════════════════════════════════════════════════════
#  邀請：Button Template payload（task 2.1）
# ═══════════════════════════════════════════════════════════
#
# 原本選 Quick Reply 是因為 docs/技術可行性與架構規劃.md §1.1／§1.2
# 判斷它「手機＋桌面皆可觸發」，Persistent Menu／Ice Breaker 僅手機版顯示。
# 2026-09-23 真機實測推翻了這個判斷：Quick Reply 在 Instagram Login API
# 上完全不會渲染任何按鈕（API 回 200、拿得到 message_id，但畫面上什麼都
# 沒有——Graph API 對不支援的欄位是默默忽略，不是報錯，難以從症狀察覺）。
# 改用 Button Template 後真機驗證按鈕正常顯示，點擊後觸發的是
# messaging_postbacks（見 app/channels/instagram.py 的 _parse_one 既有的
# postback 分支），不是原先假設的一般 message 事件。
#
# 該研究文件的判斷應是把 Messenger 平台的 Quick Reply（確實支援桌面）與
# Instagram 平台的搞混——兩者 API 形狀相似但實際支援的元件不同。

# daily-coach-report-link：不再送出按鈕邀請（改為凌晨送出回饋連結），但使用者手上
# 仍有先前送出的按鈕，點擊時要認得它的 payload，改回覆連結（app/main.py
# 的 _handle_old_invite_click）。按鈕文字是點擊時補記的那則系統訊息內容。
INVITE_PAYLOAD_PREFIX = "COACH_REPORT_VIEW:"
INVITE_BUTTON_TITLE = "看這次的回饋"


def parse_invite_payload(payload: str | None) -> int | None:
    """從 postback payload 解出邀請所屬的 session_id，格式不符回傳 None。"""
    if not payload or not payload.startswith(INVITE_PAYLOAD_PREFIX):
        return None
    raw = payload[len(INVITE_PAYLOAD_PREFIX) :]
    return int(raw) if raw.isdigit() else None


# ═══════════════════════════════════════════════════════════
#  生成（tasks 3.1–3.5）
# ═══════════════════════════════════════════════════════════


class ReportParseError(ValueError):
    """模型輸出不是可解析的段落結構 JSON。"""


@dataclass(frozen=True)
class ReportSection:
    """報告的一個段落。key 是模型輸出 JSON 的鍵，也是存進 coach_reports.content 的鍵。"""

    key: str
    description: str


def report_sections(coaching: Mapping[str, Any] | None) -> list[ReportSection]:
    """依教練設定決定報告要寫哪些段落（add-persona-authoring-console 任務 7.3）。

    預設段落沿用原本的鍵（摘要、做得好…），顯示時由 _SECTION_DISPLAY_LABEL
    換成標題，舊報告與新報告格式一致。自訂段落以標題當鍵：
    display_sections 只拿得到報告內容，鍵本身就得是能直接顯示的標題。
    """
    raw = (coaching or {}).get("reportSections") or []
    sections: list[ReportSection] = []
    used: set[str] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        key = str(entry.get("key") or "")
        is_custom = entry.get("custom") is True or key not in _SECTION_DISPLAY_LABEL
        if is_custom:
            key = str(entry.get("title") or "").strip()
        if not key:
            continue
        # 自訂段落的標題可能與其他段落重複，或剛好等於預設段落的鍵（會被顯示成
        # 那個預設段落的標題）：加編號，免得兩段內容互相覆蓋或標題錯置
        reserved = used | set(_SECTION_DISPLAY_LABEL) if is_custom else used
        base, n = key, 2
        while key in reserved:
            key = f"{base}（{n}）"
            n += 1
        used.add(key)
        sections.append(ReportSection(key, str(entry.get("description") or "").strip()))
    return sections


def _is_default_coaching(coaching: Mapping[str, Any] | None) -> bool:
    """沒有設定，或報告相關設定都與預設相同：沿用原本的提示，行為與導入前一致。"""
    if not coaching:
        return True
    defaults = default_coaching()
    return all(
        coaching.get(k) == defaults[k] for k in ("reportTone", "reportLength", "reportSections")
    )


def _signal_lines(signals: list[RealtimeSignal]) -> str:
    if not signals:
        return "（這場對話沒有偵測到特別的地雷或句點，也沒有特別突出的亮點）"
    return "\n".join(
        f"- [{_TYPE_LABEL.get(s.signal_type, s.signal_type)}"
        f"{'／' + s.severity if s.severity else ''}／{s.dimension or '未分類'}] "
        f"{s.reason or ''}"
        for s in signals
    )


def build_system_prompt(
    signals: list[RealtimeSignal],
    persona_name: str,
    coaching: Mapping[str, Any] | None = None,
) -> str:
    """coaching 是該 session 所套用之使用者版本的教練設定；None 或與預設相同時
    輸出原本的五段提示，與導入教練設定前逐字相同。"""
    if not _is_default_coaching(coaching):
        return _build_custom_prompt(signals, persona_name, coaching)
    return _build_default_prompt(signals, persona_name)


def _build_custom_prompt(
    signals: list[RealtimeSignal], persona_name: str, coaching: Mapping[str, Any]
) -> str:
    sections = report_sections(coaching)
    tone = str(coaching.get("reportTone") or DEFAULT_TONE).strip()
    length = str(coaching.get("reportLength") or DEFAULT_LENGTH).strip()
    numbered = "\n".join(
        f"{i}. {s.key}：{s.description}" if s.description else f"{i}. {s.key}"
        for i, s in enumerate(sections, 1)
    )
    example = "{" + ", ".join(f'"{s.key}": "..."' for s in sections) + "}"
    return (
        f"你是戀愛對話教練，要根據使用者剛才與 AI 角色「{persona_name}」的一段對話"
        "分析結果，寫一份給使用者看的回饋。以下是這場對話偵測到的溝通行為訊號"
        "——**不是原始對話逐字稿，只有這些訊號可用，不要杜撰訊號中沒有的細節**：\n\n"
        f"{_signal_lines(signals)}\n\n"
        "使用者自己設定了想要的回饋方式，請照著寫：\n"
        f"- 語氣：{tone}\n"
        f"- 篇幅：全部段落加起來{length}\n\n"
        f"只寫下面這 {len(sections)} 段，不要多寫其他段落：\n"
        f"{numbered}\n\n"
        f"{LANGUAGE_RULE}\n"
        "需要引用時，引用上面訊號裡的原句。"
        "沒有明顯地雷或句點時，不要硬找問題。\n"
        "只輸出一個 JSON 物件，不要任何說明文字，鍵名照上面的段落名稱：\n"
        f"{example}"
    )


def _build_default_prompt(signals: list[RealtimeSignal], persona_name: str) -> str:
    if signals:
        lines = "\n".join(
            f"- [{_TYPE_LABEL.get(s.signal_type, s.signal_type)}"
            f"{'／' + s.severity if s.severity else ''}／{s.dimension or '未分類'}] "
            f"{s.reason or ''}"
            for s in signals
        )
    else:
        lines = "（這場對話沒有偵測到特別的地雷或句點，也沒有特別突出的亮點）"

    return (
        f"你是戀愛對話教練，要根據使用者剛才與 AI 角色「{persona_name}」的一段對話"
        "分析結果，寫一份給使用者看的簡短回饋。以下是這場對話偵測到的溝通行為訊號"
        "——**不是原始對話逐字稿，只有這些訊號可用，不要杜撰訊號中沒有的細節**：\n\n"
        f"{lines}\n\n"
        "請寫五段內容，語氣像朋友給建議，不要說教、不要用「你應該」這種命令句：\n"
        "1. 摘要：這次對話整體表現的一句話總結\n"
        "2. 做得好：具體做得好的地方，引用上面訊號裡的原句佐證\n"
        "3. 可調整：可以改進的地方，同樣引用訊號原句，語氣委婉\n"
        "4. 改寫示範：針對「可調整」提到的情境，示範一句更好的說法\n"
        "5. 下次可試：下次對話可以嘗試的一個具體方向\n\n"
        "只輸出一個 JSON 物件，不要任何說明文字，鍵名固定為：\n"
        '{"摘要": "...", "做得好": "...", "可調整": "...", '
        '"改寫示範": "...", "下次可試": "..."}\n'
        "沒有明顯地雷或句點時，「可調整」可以寫鼓勵繼續保持的話，不要硬找問題。\n"
        f"{LANGUAGE_RULE}"
    )


def parse_report(text: str, keys: Sequence[str] = REPORT_SECTIONS) -> dict[str, str]:
    """把模型輸出轉成段落結構，依 keys 的順序。缺任一段落即視為壞掉，整份拋出，
    不留半成品——半份報告比不生成更糟，使用者看到的會是內容不完整卻像正式產出的東西。
    模型多寫的段落直接丟掉：使用者取消的段落不該因為模型自作主張又出現。
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ReportParseError(f"輸出中找不到 JSON 物件：{text[:200]}")
    try:
        raw = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ReportParseError(f"輸出不是合法 JSON：{text[:200]}") from exc
    if not isinstance(raw, dict):
        raise ReportParseError(f"輸出不是 JSON 物件：{text[:200]}")

    missing = [k for k in keys if not str(raw.get(k, "")).strip()]
    if missing:
        raise ReportParseError(f"缺少必要段落：{missing}")

    return {k: str(raw[k]).strip() for k in keys}


@dataclass(frozen=True)
class ReportResult:
    ok: bool
    content: dict[str, str] | None
    error: str | None = None


async def generate_report(
    router: LLMRouter,
    signals: list[RealtimeSignal],
    persona_name: str,
    coaching: Mapping[str, Any] | None = None,
) -> ReportResult:
    """生成一份教練報告。失敗（含解析失敗）回傳 ok=False 並帶上原因，
    不拋出例外——呼叫端要能把原因寫進 coach_reports.failure_reason。

    用 router.generate 而非 generate_or_fallback：後者失敗時回傳對話用的
    降級語句並吞掉錯誤原因，這裡需要保留真正的失敗原因（同 signals.py 的選擇）。

    coaching 是報告所屬 session 套用之使用者版本的教練設定（任務 7.3）；
    None 或與預設相同時沿用原本的五段與預設語氣。
    """
    if _is_default_coaching(coaching):
        keys: Sequence[str] = REPORT_SECTIONS
        max_tokens = 1024
    else:
        keys = [s.key for s in report_sections(coaching)]
        # 使用者可選到約 800 字的篇幅，加上 JSON 外殼，1024 不一定夠
        max_tokens = 2048
    try:
        response = await router.generate(
            "coaching",
            system_prompt=build_system_prompt(signals, persona_name, coaching),
            messages=[LLMMessage(role="user", content="請產出這次對話的教練報告。")],
            temperature=0.7,
            max_output_tokens=max_tokens,
        )
        # 任務 2.3：段落內容轉繁體；鍵是段落名稱（自訂段落是使用者寫的標題），不動
        content = {k: to_traditional(v) for k, v in parse_report(response.text, keys).items()}
    except (LLMError, ReportParseError) as exc:
        log.warning(
            "教練報告生成失敗",
            extra={"error_type": type(exc).__name__, "error": str(exc)[:500]},
        )
        return ReportResult(ok=False, content=None, error=f"{type(exc).__name__}: {exc}"[:1000])
    except Exception as exc:
        log.exception("教練報告生成發生未預期例外")
        return ReportResult(ok=False, content=None, error=f"{type(exc).__name__}: {exc}"[:1000])

    return ReportResult(ok=True, content=content)


# ═══════════════════════════════════════════════════════════
#  網頁上的段落（daily-coach-report-link 任務 6.1）
# ═══════════════════════════════════════════════════════════


def display_sections(content: dict[str, str] | None) -> list[dict[str, str]]:
    """把報告內容轉成網頁要顯示的段落：[{"title", "body"}]，依內容裡的段落順序。

    段落由生成時的教練設定決定（parse_report 已依設定排好順序）：使用者取消的
    段落本來就不在內容裡。預設段落的鍵換成標題，自訂段落的鍵本身就是標題。
    """
    sections = []
    for key, raw in (content or {}).items():
        body = raw.strip() if isinstance(raw, str) else ""
        if body:
            sections.append({"title": _SECTION_DISPLAY_LABEL.get(key, key), "body": body})
    return sections
