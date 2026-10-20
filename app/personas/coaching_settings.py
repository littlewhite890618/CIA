"""使用者版本的教練設定：預設值與驗證（add-persona-authoring-console 任務 6.4、6.5）。

persona-authoring spec「教練設定」：
- 評分維度固定六項（signals.DIMENSIONS），使用者不可增減
- 系統預設規則（signals.SIGNAL_ITEMS，L1～H4）預設全部啟用，可逐條停用
- 自訂規則要有描述、類型、歸屬維度；地雷另需嚴重程度
- 報告語氣與篇幅為下拉選單（可自訂）；報告段落至少一段

預設值以這裡為準，設定頁從 API 取得（GET /api/settings 的 defaults.coaching），
前端的選項清單第一項需與這裡一致（frontend/src/settings/schema.ts）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session as DBSession

from app.db import repository as repo
from app.db.models import Session
from app.dialog.signals import DIMENSIONS, SIGNAL_ITEMS

DEFAULT_TONE = "溫和鼓勵：先肯定再建議，語氣委婉"
DEFAULT_LENGTH = "1～2 則，約 500 字"

# 鍵與 app/dialog/coach_report.py 的 REPORT_SECTIONS 一致
DEFAULT_SECTIONS: list[dict[str, Any]] = [
    {"key": "摘要", "title": "這次聊起來的感覺", "description": "整體表現的一句話總結"},
    {"key": "做得好", "title": "做得好的地方", "description": "引用你的原句，說明哪裡做得好"},
    {"key": "可調整", "title": "可以再調整的地方", "description": "引用你的原句，語氣委婉"},
    {"key": "改寫示範", "title": "換個說法試試", "description": "針對可以調整的地方，示範一句更好的說法"},
    {"key": "下次可試", "title": "下次可以試試", "description": "下次聊天可以嘗試的一個具體方向"},
]

SIGNAL_TYPES = {"landmine", "killer", "highlight"}
SEVERITIES = {"high", "medium", "low"}
MAX_TEXT = 200
MAX_CUSTOM_RULES = 20
MAX_SECTIONS = 10


def default_coaching() -> dict[str, Any]:
    return {
        "disabledRuleIds": [],
        "customRules": [],
        "reportTone": DEFAULT_TONE,
        "reportLength": DEFAULT_LENGTH,
        "reportSections": [dict(s) for s in DEFAULT_SECTIONS],
    }


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def validate_coaching(raw: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """驗證並整理設定頁送來的教練設定，回傳 (整理後的值, 錯誤)。錯誤的鍵是欄位路徑。"""
    errors: dict[str, str] = {}
    if not isinstance(raw, dict):
        return default_coaching(), {"coaching": "教練設定的格式不正確。"}

    custom_rules: list[dict[str, Any]] = []
    raw_rules = raw.get("customRules") or []
    if not isinstance(raw_rules, list) or len(raw_rules) > MAX_CUSTOM_RULES:
        errors["coaching.customRules"] = f"自訂規則最多 {MAX_CUSTOM_RULES} 條。"
        raw_rules = []
    for rule in raw_rules:
        if not isinstance(rule, dict):
            errors["coaching.customRules"] = "自訂規則的格式不正確。"
            continue
        text = _text(rule.get("text"))
        rule_type = rule.get("type")
        dimension = rule.get("dimension")
        severity = rule.get("severity")
        if not text or len(text) > MAX_TEXT:
            errors["coaching.customRules"] = "自訂規則要寫要看的行為，最多 200 字。"
        elif rule_type not in SIGNAL_TYPES:
            errors["coaching.customRules"] = "自訂規則的類型不正確。"
        elif dimension not in DIMENSIONS:
            errors["coaching.customRules"] = "自訂規則要歸在六項之一。"
        elif rule_type == "landmine" and severity not in SEVERITIES:
            errors["coaching.customRules"] = "地雷要選嚴重程度。"
        else:
            custom_rules.append({
                "id": str(rule.get("id") or f"custom-{len(custom_rules) + 1}")[:64],
                "text": text,
                "type": rule_type,
                "dimension": dimension,
                "severity": severity if rule_type == "landmine" else None,
                "custom": True,
            })

    known_ids = set(SIGNAL_ITEMS) | {r["id"] for r in custom_rules}
    disabled = raw.get("disabledRuleIds") or []
    if not isinstance(disabled, list):
        disabled = []
    disabled_ids = sorted({str(i) for i in disabled if str(i) in known_ids})

    tone = _text(raw.get("reportTone"))
    length = _text(raw.get("reportLength"))
    if not tone or len(tone) > MAX_TEXT:
        errors["coaching.reportTone"] = "這一欄要填，最多 200 字。"
    if not length or len(length) > MAX_TEXT:
        errors["coaching.reportLength"] = "這一欄要填，最多 200 字。"

    sections: list[dict[str, Any]] = []
    raw_sections = raw.get("reportSections") or []
    if not isinstance(raw_sections, list):
        raw_sections = []
    for section in raw_sections[:MAX_SECTIONS]:
        if not isinstance(section, dict):
            continue
        title = _text(section.get("title"))
        if not title or len(title) > 40:
            errors["coaching.reportSections"] = "段落要有標題，最多 40 字。"
            continue
        entry = {
            "key": str(section.get("key") or title)[:64],
            "title": title,
            "description": _text(section.get("description"))[:MAX_TEXT] or "（沒有說明）",
        }
        # 只有自訂段落帶 custom：預設段落的形狀要與 DEFAULT_SECTIONS 完全相同，
        # 設定頁才不會把沒改過的段落判定成「跟預設不同」
        if section.get("custom"):
            entry["custom"] = True
        sections.append(entry)
    if not sections and "coaching.reportSections" not in errors:
        errors["coaching.reportSections"] = "至少留一個段落。"

    return {
        "disabledRuleIds": disabled_ids,
        "customRules": custom_rules,
        "reportTone": tone or DEFAULT_TONE,
        "reportLength": length or DEFAULT_LENGTH,
        "reportSections": sections,
    }, errors


def coaching_to_columns(coaching: dict[str, Any]) -> dict[str, Any]:
    """設定頁的教練設定 → PersonaCoaching 的欄位。"""
    return {
        "disabled_rule_ids": coaching["disabledRuleIds"],
        "custom_rules": coaching["customRules"],
        "report_tone": coaching["reportTone"],
        "report_length": coaching["reportLength"],
        "report_sections": coaching["reportSections"],
    }


def coaching_from_columns(row: Any | None) -> dict[str, Any]:
    """PersonaCoaching → 設定頁的教練設定；欄位為 NULL 時用預設值。"""
    defaults = default_coaching()
    if row is None:
        return defaults
    return {
        "disabledRuleIds": row.disabled_rule_ids if row.disabled_rule_ids is not None else defaults["disabledRuleIds"],
        "customRules": row.custom_rules if row.custom_rules is not None else defaults["customRules"],
        "reportTone": row.report_tone or defaults["reportTone"],
        "reportLength": row.report_length or defaults["reportLength"],
        "reportSections": row.report_sections or defaults["reportSections"],
    }


def session_coaching(db: DBSession, session: Session) -> dict[str, Any] | None:
    """該 session 所套用之使用者版本的教練設定（任務 7.1、7.3）。

    使用者沒有版本、或版本沒有教練設定時回傳 None，呼叫端沿用系統預設。
    讀的是當下的設定，不是對話開始時的快照：設定在 session 結束後才用得到
    （訊號偵測、教練報告），使用者在對話中途改設定，以改後的為準。
    """
    link = repo.get_link_by_session(db, session)
    if link is None:
        return None
    version = repo.get_user_version(db, link.id)
    if version is None:
        return None
    row = repo.get_coaching(db, version.id)
    if row is None:
        return None
    return coaching_from_columns(row)
