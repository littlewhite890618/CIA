"""使用者自訂文字的自動檢查（add-persona-authoring-console 任務 6.3，design.md 決策 13）。

使用者版本不經人工審核，改以範圍限制把關：系統底線（絕不接）不可移除，
使用者寫的文字保存前經這裡檢查。目標是擋下「讓角色配合」的內容——
要求性方面的互動、或讓角色索取／交出聯絡方式。

只檢查描述「角色會做什麼、喜歡什麼」的欄位。「會變冷淡」「絕不接」列的是
角色會拒絕的事，裡面出現「性暗示」「要 LINE」是正常的，不檢查。

這是規則比對，不是語意判斷：會漏掉換句話說的寫法，也可能誤擋。上線前的
安全護欄（M12）仍是必要條件；這裡只先擋掉最直接的寫法。
"""

from __future__ import annotations

import re

# 描述角色行為與喜好的欄位（設定頁的欄位路徑）
CHECKED_FIELDS = (
    "identity.occupation",
    "identity.location",
    "background.recent_life",
    "background.interests",
    "personality.core_traits",
    "personality.initiative",
    "personality.questioning",
    "personality.emotional_range",
    "personality.compliment_response",
    "personality.self_disclosure",
    "language_style.pace",
    "language_style.message_length",
    "language_style.message_splitting",
    "language_style.particles",
    "language_style.emoji",
    "language_style.punctuation",
    "boundaries.warms_up_to",
    "boundaries.cooling_behavior",
)

_PATTERNS = [
    # 性方面
    re.compile(r"(做愛|性愛|約炮|打炮|裸照|裸體|露點|色色|自慰|性交|口交|一夜情|上床|開房|性感照|脫衣|調情到|性幻想)"),
    re.compile(r"(主動|喜歡|願意|樂意|會).{0,6}(性暗示|黃色笑話|開黃腔|性話題|講色|聊色)"),
    # 聯絡方式：讓角色索取或給出
    re.compile(r"(主動|會|願意|樂意|喜歡).{0,6}(要|給|留|交換|索取|加).{0,6}(電話|手機|LINE|line|Line|IG|微信|WeChat|聯絡方式|地址)"),
    re.compile(r"(給|留|交換|加).{0,4}(她|我|角色)?的?(電話|手機|LINE|line|Line|微信|聯絡方式|地址)"),
]

MESSAGE = "這段內容不適合放進角色設定（例如讓角色配合性方面的內容，或索取、交出聯絡方式）。"


def find_problems(card: dict) -> dict[str, str]:
    """回傳 {欄位路徑: 錯誤訊息}。card 是設定頁格式（段落 → 欄位 → 字串或字串清單）。"""
    problems: dict[str, str] = {}
    for path in CHECKED_FIELDS:
        section, key = path.split(".")
        value = (card.get(section) or {}).get(key)
        texts = value if isinstance(value, list) else [value]
        if any(isinstance(t, str) and any(p.search(t) for p in _PATTERNS) for t in texts):
            problems[path] = MESSAGE
    return problems
