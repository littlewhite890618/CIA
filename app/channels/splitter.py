"""訊息切分（task 4.5）。

Meta 官方限制（2026-09-09 查證）：
    "Message text must be UTF-8 and be a 1000 bytes or less."
    https://developers.facebook.com/docs/instagram-platform/
    instagram-api-with-instagram-login/messaging-api/

**單位是 bytes，不是字元。** 中文一字 3 bytes、emoji 通常 4 bytes，
所以 1000 bytes 僅約 330 個中文字。以字元數估算會嚴重超標。

切分優先序（依 design.md 決策 8）：段落 → 句末標點 → 逗號 → 硬切。
機械式硬切會把句子從中間斬斷，嚴重破壞擬真感，因此硬切僅作保底。
"""

from __future__ import annotations

MAX_BYTES = 1000

# 切分點的優先序。愈前面愈自然。
_PARAGRAPH = ("\n\n",)

SENTENCE_ENDINGS = ("。", "！", "？", "…", "\n", "! ", "? ", ". ")

_COMMA = ("，", "、", "；", "：", ", ", "; ", ": ")

# 切分點至少要落在可用長度的這個比例之後。
#
# 沒有這個下限的話，開頭附近一個句號就會讓整段被切成「兩個字」加「一大段」，
# 反而更破碎。寧可讓某一則稍微短一點，也不要產生過度零碎的訊息。
_MIN_FILL_RATIO = 0.5


def _fit_chars(text: str, limit: int) -> int:
    """回傳 text 前綴中，UTF-8 編碼不超過 limit bytes 的最大字元數。

    先在 bytes 層硬切，再往前退到合法的字元邊界——這樣不論多位元組
    字元佔幾個 byte 都能正確處理，不需要逐字元累加長度。
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return len(text)

    cut = encoded[:limit]
    while cut:
        try:
            return len(cut.decode("utf-8"))
        except UnicodeDecodeError as exc:
            # 切點落在某個字元的位元組中間，退到該字元起始處
            cut = cut[: exc.start]
    return 0


def _find_boundary(text: str, upto: int, separators: tuple[str, ...]) -> int | None:
    """在 text[:upto] 中找最後一個分隔符，回傳「分隔符之後」的位置。

    找不到、或位置太靠前（未達 _MIN_FILL_RATIO）時回傳 None，
    由呼叫端往下一個優先序嘗試。
    """
    floor = int(upto * _MIN_FILL_RATIO)
    best: int | None = None

    for sep in separators:
        idx = text.rfind(sep, 0, upto)
        if idx == -1:
            continue
        end = idx + len(sep)
        if end >= floor and (best is None or end > best):
            best = end

    return best


def split_message(text: str, max_bytes: int = MAX_BYTES) -> list[str]:
    """將文字切分為每則不超過 max_bytes 的多則訊息。

    保證：
      1. 每一則的 UTF-8 編碼長度 <= max_bytes
      2. 每一則都是合法 UTF-8，不會有半個字元造成的亂碼
      3. 不遺失也不重複任何非空白字元
      4. 順序與原文一致

    切分點兩側的空白會被去除（第 3 點的保證針對非空白字元）——
    否則後續訊息會以換行開頭，在 Instagram 上顯示成突兀的空行。

    空字串或純空白回傳空清單：沒有內容就不該送出任何訊息。
    """
    text = text.strip()
    if not text:
        return []

    chunks: list[str] = []
    remaining = text

    while remaining:
        fit = _fit_chars(remaining, max_bytes)

        if fit >= len(remaining):
            chunks.append(remaining)
            break

        if fit == 0:
            # 單一字元就超過上限。實務上不會發生（上限 1000 bytes，
            # 最長的字元也只有 4 bytes），但不處理會變成無窮迴圈。
            raise ValueError(f"單一字元超過 {max_bytes} bytes 上限，無法切分")

        cut = (
            _find_boundary(remaining, fit, _PARAGRAPH)
            or _find_boundary(remaining, fit, SENTENCE_ENDINGS)
            or _find_boundary(remaining, fit, _COMMA)
            or fit  # 都找不到自然邊界時才硬切，位置已是合法字元邊界
        )

        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].strip()

    return chunks
