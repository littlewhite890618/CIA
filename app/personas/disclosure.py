"""AI 身分揭露（task 7.5）。

Meta 平台政策要求：每一段新對話開頭、以及長時間中斷後重新開啟對話，
都必須讓使用者知道自己在跟自動化服務互動。這不是可調整的體驗選項，
是 persona-chat spec 明文要求「MUST NOT 可被設定關閉」的政策紅線。

因此本模組刻意不提供任何停用開關——should_disclose() 與 compose_reply()
都沒有「是否揭露」的參數。這是結構上的保證：往後有人想加一個
「AI_DISCLOSURE_ENABLED」設定並接進來，必須主動修改這裡的函式簽章，
不會是「不小心接對了一個既有參數」這種容易被忽略的一行改動。
"""

from __future__ import annotations

from datetime import datetime, timezone


def disclosure_text(persona_name: str) -> str:
    return f"嗨，我是 {persona_name}，我是 AI 戀愛顧問，不是真人唷 🌿"


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


def compose_reply(
    base_text: str, *, disclose: bool, persona_name: str, hint: str | None = None
) -> str:
    """視需要在回覆前面加上 AI 身分揭露。

    用單個換行銜接——app/dialog/pacing.py 的 split_into_bubbles() 依
    單個換行判斷段落邊界（2026-09-18 實測 LLM 輸出習慣後修正），這裡
    保持一致，讓揭露文字能被正確辨識成獨立一段。

    hint 是揭露之後的功能提示（add-persona-authoring-console 任務 5.5：
    告訴新使用者可以傳「設定」），只在揭露時一併出現。它不影響揭露本身——
    沒有 hint 也照樣揭露。
    """
    if not disclose:
        return base_text
    lines = [disclosure_text(persona_name)]
    if hint:
        lines.append(hint)
    lines.append(base_text)
    return "\n".join(lines)
