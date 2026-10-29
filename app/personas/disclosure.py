"""對話開場問候（原 task 7.5 的 AI 身分揭露；daily-coach-report-link 任務 8.1）。

每一段新對話開頭、以及長時間中斷後重新開啟對話，第一則回覆前加一行開場問候。

2026-10-21 使用者決定：開場改為「嗨，我是晴晴，戀愛顧問🌿」，不再提「AI」與
「不是真人」，也拿掉「設定」提示。這與 Meta 平台政策「自動化對話須在開頭揭露」
不符，使用者已知情（daily-coach-report-link design.md 決策 10）；目前剩下的揭露
是 IG 帳號的「AI 生成的個人檔案」標籤。開場文字是設定值（OPENING_GREETING），日後要恢復
揭露只需改設定。出現時機（should_disclose）不變。
"""

from __future__ import annotations

from datetime import datetime, timezone


def disclosure_text(persona_name: str, template: str) -> str:
    """開場問候。template 中的 {name} 換成角色名字。"""
    return template.replace("{name}", persona_name)


def should_disclose(
    *,
    is_new_session: bool,
    last_activity_at: datetime,
    now: datetime,
    silence_threshold_seconds: int,
) -> bool:
    """判斷這次回覆是否需要附上 AI 身分揭露。

    對應 spec 的兩個觸發情境：
      - 新對話開頭 → is_new_session 為 True
      - 長時間中斷後重新開啟 → 距上次互動的間隔達到靜默門檻

    「長時間中斷」沿用與 session 靜默判定相同的門檻值（見
    conversation-session spec 的 silence_threshold_seconds）——概念上
    這本來就是同一件事：間隔久到系統會把對話判定為已結束，就久到
    需要向使用者重新揭露身分，不需要另外引入一個獨立的設定值。

    last_activity_at 若不帶時區資訊會被視為 UTC（SQLite 讀回的
    datetime 常是如此）。
    """
    if is_new_session:
        return True

    if last_activity_at.tzinfo is None:
        last_activity_at = last_activity_at.replace(tzinfo=timezone.utc)

    gap_seconds = (now - last_activity_at).total_seconds()
    return gap_seconds >= silence_threshold_seconds


def compose_reply(base_text: str, *, disclose: bool, persona_name: str, template: str) -> str:
    """視需要在回覆前面加上開場問候。

    用單個換行銜接——app/dialog/pacing.py 的 split_into_bubbles() 依
    單個換行判斷段落邊界（2026-09-18 實測 LLM 輸出習慣後修正），這裡
    保持一致，讓開場問候能被正確辨識成獨立一段。
    """
    if not disclose:
        return base_text
    return f"{disclosure_text(persona_name, template)}\n{base_text}"
