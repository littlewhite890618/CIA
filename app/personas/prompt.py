"""人設卡至 system prompt 的渲染（task 7.3）。

參數型別是 PersonaPromptFields，不是 PersonaCard 或 PersonaRuntimeParams——
呼叫端就算想傳 runtime 參數進來也會被型別檢查擋下。這是「程式參數不進入
提示」的具體實作，對應 persona-chat spec 的「程式參數不進入提示」情境。

輸出格式是給模型讀的自然語言段落，不是給人看的排版文件，所以用標題
分節但不做花俏的 Markdown——多數模型對簡單、一致的結構反應最穩定。
"""

from __future__ import annotations

from app.personas.schema import PersonaPromptFields


def render_system_prompt(
    fields: PersonaPromptFields,
    known_topics: list[str] | None = None,
    previous_conversation: list[str] | None = None,
    conversation_summary: str | None = None,
) -> str:
    """渲染 system prompt。

    known_topics（task 6.7）是使用者跨 session 已經聊過的話題摘要，
    不是人設卡內容——刻意用獨立參數而非塞進 PersonaPromptFields，
    因為那是每個使用者各自不同的動態資料，跟角色本身的靜態設定
    分開才不會混淆「這是誰的記憶」。
    """
    identity = fields.identity
    background = fields.background
    personality = fields.personality
    language_style = fields.language_style
    boundaries = fields.boundaries

    lines: list[str] = []

    location_part = f"，住在{identity.location}" if identity.location else ""
    lines.append(
        f"你是{identity.name}，{identity.age}歲，{identity.occupation}{location_part}。"
        "接下來你要以這個角色的身分和使用者對話，語氣與反應都要維持一致。"
        "稱呼對方一律用「你」，不要用「妳」——「妳」是用來稱呼女性對象的字，"
        "而你聊天的對象是男性使用者。"
    )

    lines += ["", "【近況】", background.recent_life]

    lines += ["", "【興趣】"]
    lines += [f"- {item}" for item in background.interests]

    lines += [
        "",
        "【個性】",
        f"核心特質：{personality.core_traits}",
        f"主動性：{personality.initiative}",
    ]
    if personality.questioning:
        lines.append(f"提問傾向：{personality.questioning}")
    if personality.emotional_range:
        lines.append(f"情緒幅度：{personality.emotional_range}")
    if personality.compliment_response:
        lines.append(f"對讚美的反應：{personality.compliment_response}")
    if personality.self_disclosure:
        lines.append(f"自我揭露程度：{personality.self_disclosure}")

    lines += [
        "",
        "【說話風格】",
        f"節奏：{language_style.pace}",
        f"訊息長度：{language_style.message_length}",
        # 2026-10-16：只列出語助詞時，模型會當成每則都要用（真機測試約一半的回覆
        # 都帶「齁」「欸」），所以明寫頻率。這是給所有使用者版本的固定說明，不是設定值
        f"常用語助詞：{'、'.join(language_style.particles)}"
        "（偶爾用，大約三四則用一次，不要每則都用）",
        f"emoji 使用：{language_style.emoji}",
    ]
    if language_style.message_splitting:
        lines.append(f"拆訊習慣：{language_style.message_splitting}")
    if language_style.punctuation:
        lines.append(f"標點習慣：{language_style.punctuation}")

    lines += [
        "",
        "【什麼話題會讓你更投入、更熱絡】",
    ]
    lines += [f"- {item}" for item in boundaries.warms_up_to]

    lines += [
        "",
        "【什麼話題會讓你變冷淡】",
        "遇到以下話題時，不要翻臉、不要說教、也不要直接拒絕——",
        "而是照你的方式退回去：",
        boundaries.cooling_behavior.strip(),
        "會觸發這種反應的話題：",
    ]
    lines += [f"- {item}" for item in boundaries.cools_down_on]

    # task 7.7：回暖是邊界反應的一部分（spec 的「使用者修正後回暖」情境），
    # 但原本的段落只交代怎麼變冷淡，沒交代怎麼恢復——模型讀不到「該回暖了」
    # 這件事，只會一路冷下去。補這段讓「變熱絡」清單同時承擔兩個角色：
    # 平時的加分話題，以及冷淡之後的回暖信號。
    lines += [
        "",
        "【回暖】",
        "如果你剛才因為上面的話題而變冷淡，後來使用者轉而談到"
        "「什麼話題會讓你更投入、更熱絡」列出的內容，你應該在接下來",
        "幾則訊息內自然恢復原本的互動品質——不要記仇、不要維持冷淡，",
        "也不要提起「你剛才怎樣怎樣」，就自然地回暖即可。",
    ]

    lines += [
        "",
        "【絕對不能做的事，任何情況下都不能配合】",
    ]
    lines += [f"- {item}" for item in boundaries.never_engages]

    if known_topics:
        # task 6.7：跨 session 話題記憶。放在最後一段——這是使用者
        # 專屬的動態資料，不是角色的固定設定，語意上是「補充資訊」
        # 而非「你是誰」，放最後不干擾前面對人設本身的描述。
        lines += [
            "",
            "【你們之前聊過的話題】",
            "使用者不是第一次找你聊天，之前談過以下內容——",
            "不要重複詢問這些已經問過的基本問題，但也不用刻意提起，",
            "自然地表現出「記得對方」的樣子即可：",
        ]
        lines += [f"- {item}" for item in known_topics]

    if previous_conversation:
        # 跨場次記憶的第二層：話題萃取只留 10 字內的關鍵字，細節（例如
        # 主管姓什麼）會丟失，這裡直接附上舊場次的最後幾則原文。
        lines += [
            "",
            "【上次聊天的最後幾則（是之前的場次，不是剛剛）】",
            "以下是你們之前聊到的內容。如果使用者提起或問到這些細節，",
            "請根據這裡回答，不要說不記得；沒被問到就不用主動重提：",
        ]
        lines += previous_conversation

    if conversation_summary:
        # 任務 8：同一場次較早內容的摘要。放在 system prompt 而不是接在對話
        # 輪次前面，理由同上一段：避免同角色連續輪次被 Gemini 拒收
        lines += [
            "",
            "【這次聊天稍早的內容（摘要）】",
            "這次聊天前段的重點整理如下，比較近的對話在訊息紀錄裡。",
            "使用者提起這些事時照這裡回答，不要說不記得，也不要說出跟這裡矛盾的話：",
            conversation_summary.strip(),
        ]

    return "\n".join(lines)
