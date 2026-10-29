"""把模型輸出殘留的簡體字轉成繁體（daily-coach-report-link 任務 2.1，design.md 決策 7）。

提示裡已經要求模型用繁體中文；這裡是保險，用在晴晴的回覆、教練報告、對話摘要。
使用者傳來的訊息不經過這裡。

不能直接把整段丟給 OpenCC：它會改寫本來就正確的繁體字（「干擾」→「幹擾」、
「台灣」→「臺灣」），違反「原本就是繁體的內容不被改寫」。所以：

1. 先用 OpenCC（s2tw：只轉字形與台灣異體字，不換詞彙）算出整段的轉換結果，
   保留它依詞組判斷一對多的能力（「头发」→「頭髮」、「发了」→「發了」）
2. 只採用「繁體字集（Big5）裡沒有的字」的轉換結果——那些字一定是簡體
3. 簡體字旁邊緊鄰、被 OpenCC 一起改掉的字也採用：它們是同一個簡體詞的一部分
   （「没关系」的「系」、「我们周末」的「周」），單看字本身會被誤判成繁體
"""

from __future__ import annotations

from opencc import OpenCC

_converter = OpenCC("s2tw")

# 放進晴晴、教練報告、對話摘要提示中的語言要求（任務 2.2）
LANGUAGE_RULE = "一律使用繁體中文與台灣慣用的用語，不要使用簡體字。"


def _is_traditional_char(ch: str) -> bool:
    try:
        ch.encode("cp950")
    except UnicodeEncodeError:
        return False
    return True


def to_traditional(text: str) -> str:
    if not text:
        return text
    converted = _converter.convert(text)
    if len(converted) != len(text):
        # 逐字轉換以保持位置對齊（目前的字典不會發生，只是防範）
        converted = "".join(_converter.convert(ch) for ch in text)

    simplified = {
        i for i, (src, dst) in enumerate(zip(text, converted))
        if src != dst and not _is_traditional_char(src)
    }
    take = simplified | {
        j for i in simplified for j in (i - 1, i + 1)
        if 0 <= j < len(text) and text[j] != converted[j]
    }
    return "".join(converted[i] if i in take else ch for i, ch in enumerate(text))
