"""擬真對話節奏的計算（task 7.8 的純函式部分）。

依 design.md 決策 4：指示模型「你要慢慢回」無效，**送出時機只有程式能控制**。
人設卡的 `runtime` 區塊（回覆延遲秒數）就是為此存在的。

本模組不做任何 I/O，也不碰時間——只負責「算出這次回覆該怎麼送」，
實際的等待與發送在 app/dialog/delivery.py。這樣拆是為了讓節奏規則能用
固定種子測到底，不必等真的 20 秒。

為什麼節奏不放在 app/channels/：channels 層管的是平台強制規則
（1000 bytes 上限、24 小時視窗、速率配額），對所有角色都一樣。
延遲幾秒是晴晴這個角色的個性，屬於對話層。

訊息拆分不再靠隨機機率決定：LLM 輸出本身幾乎都會自然分成幾個語意段落，
拆分與否、拆成幾則，直接依這個自然結構決定即可，不需要再另外擲骰、也
不需要在文字中點找句末標點硬切——那樣切出來的結果跟 LLM 原本的段落
邊界對不齊，反而會讓某一則裡殘留著沒被物理拆開的段落。

2026-09-18 實測修正：原本假設 LLM 用空行（`\n\n`）分段，但
`gemini-3.6-flash`／`gemini-3.7-flash` 在晴晴這個對話情境下實際輸出的
是單個換行（`\n`），沒有空行。原本的判斷條件找不到 `\n\n`，導致誤判成
「只有一段」而完全不拆，讓多段內容全部黏在同一則裡發出去。現在改成偵測
單個換行。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from app.personas.schema import PersonaRuntimeParams

# 則與則之間的間隔秒數區間。
#
# 刻意短於首則延遲（晴晴是 8–20 秒）：真人的節奏是「想一下才開口，
# 然後連著把話打完」，不是每一則都重新思考半分鐘。
_GAP_RANGE = (2.0, 6.0)

# 拆出來的某一則短於此長度就整段退回單則。避免 LLM 隨手留了一個空行，
# 切出「喔。」這種破碎的短泡泡——兩則各剩幾個字反而比一則更像機器人。
_MIN_SPLIT_CHARS = 10

# 最多拆幾則：真人連發三則以上多半是情緒激動，不符合晴晴「慢、收斂」
# 的設定。LLM 若自然產出更多段落，會被合併進這個上限。
_MAX_BUBBLES = 2


@dataclass
class ReplyPlan:
    """一次回覆的送出計畫。

    bubbles 的順序即送出順序；gap_delays[i] 是 bubbles[i] 與 bubbles[i+1]
    之間的等待秒數，因此長度恆為 len(bubbles) - 1。
    """

    bubbles: list[str]
    lead_delay: float
    gap_delays: list[float]

    @property
    def total_delay(self) -> float:
        return self.lead_delay + sum(self.gap_delays)


_SENTENCE_ENDS = "。！？!?"


def _is_trailing_symbol(ch: str) -> bool:
    """句末標點後面緊跟的 emoji，語意上屬於前一句（例如「？🥺」）。"""
    code = ord(ch)
    return ch == "️" or 0x2600 <= code <= 0x27BF or 0x1F300 <= code <= 0x1FAFF


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """依句號、驚嘆號、問號與換行切出各句的 (起, 迄) 位置。

    刪節號（……）不算句末：晴晴常用它當句中停頓，切在那裡會斷在半句。
    """
    spans: list[tuple[int, int]] = []
    start: int | None = None
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            if start is not None:
                spans.append((start, i))
                start = None
            i += 1
            continue
        if start is None:
            if ch.isspace():
                i += 1
                continue
            start = i
        if ch in _SENTENCE_ENDS:
            j = i + 1
            while j < n:
                if text[j] in _SENTENCE_ENDS or _is_trailing_symbol(text[j]):
                    j += 1
                elif text[j] == " " and j + 1 < n and _is_trailing_symbol(text[j + 1]):
                    j += 1
                else:
                    break
            spans.append((start, j))
            start = None
            i = j
            continue
        i += 1
    if start is not None:
        spans.append((start, n))
    return spans


def split_into_bubbles(text: str) -> list[str]:
    """在句號、驚嘆號、問號或換行處拆成多則。

    句數與則數的對應：
      0 句（空字串）  → []
      1 句            → [text]，維持一則
      2 句            → 各自一則
      3 句以上         → 只在最後一句前切，前面全部合併成第一則，
                        維持 _MAX_BUBBLES 上限（晴晴的反問通常在最後一句，
                        因此會獨立成一則）

    任一則短於 _MIN_SPLIT_CHARS 時，整段退回單則發送——寧可一則稍長，
    也不要出現破碎的短泡泡。
    """
    text = text.strip()
    if not text:
        return []

    spans = _sentence_spans(text)
    if len(spans) <= 1:
        return [text]

    cuts = [start for start, _ in spans[1:]][-(_MAX_BUBBLES - 1):]
    bubbles: list[str] = []
    prev = 0
    for cut in cuts:
        bubbles.append(text[prev:cut].strip())
        prev = cut
    bubbles.append(text[prev:].strip())
    bubbles = [b for b in bubbles if b]

    if len(bubbles) <= 1 or any(len(b) < _MIN_SPLIT_CHARS for b in bubbles):
        return [text]

    return bubbles


def plan_reply(
    text: str,
    runtime: PersonaRuntimeParams,
    rng: random.Random | None = None,
) -> ReplyPlan:
    """依人設卡的 runtime 參數，算出這次回覆的送出計畫。

    rng 由外部傳入而非使用全域 random，是為了測試能以固定種子重現結果，
    不必 monkeypatch 模組全域狀態。拆分則數已不再靠擲骰決定（見
    split_into_bubbles），rng 只用於 lead_delay／gap_delays 的隨機性。
    """
    rng = rng or random.Random()

    delay = runtime.reply_delay_seconds
    lead_delay = rng.uniform(delay.min, delay.max)

    bubbles = split_into_bubbles(text)

    gap_delays = [rng.uniform(*_GAP_RANGE) for _ in range(max(len(bubbles) - 1, 0))]

    return ReplyPlan(bubbles=bubbles, lead_delay=lead_delay, gap_delays=gap_delays)
