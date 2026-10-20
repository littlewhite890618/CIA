"""人設卡的資料結構（task 7.1）。

依 design.md 決策 4，人設卡明確分成兩組，這個切分本身用型別表達，
不只是文件上的約定：

    PersonaPromptFields   會被渲染進 system prompt，送給模型看
    PersonaRuntimeParams  由程式讀取（回覆延遲…），不進提示

app/personas/prompt.py 的 render_system_prompt() 只接受
PersonaPromptFields 型別，不接受完整的 PersonaCard 或 RuntimeParams——
「runtime 參數不能流入提示」因此是簽章層級就擋住的事，不是要求人工記得
別多傳一個欄位。

欄位名稱為英文（add-persona-authoring-console 任務 2.1）。原本保留中文
是因為產品端會直接編輯 YAML；客戶改由前端表單填寫後，此理由消失，
客戶看到的中文改由前端標籤提供。欄位名稱不進入模型提示——prompt.py
自行輸出中文段落標題，故改名對模型行為零影響。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Identity(BaseModel):
    """角色的基本身分。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    ig_display_name: str
    age: int
    occupation: str
    location: str | None = None


class Background(BaseModel):
    """背景故事。

    原有的「自己的煩惱」欄位已於 2026-10-12 移除（add-persona-authoring-console
    design.md 決策 9）：客戶介面不提供此欄位，結構一併拿掉，避免內建角色與
    客戶角色的欄位不一致。
    """

    model_config = ConfigDict(extra="forbid")

    recent_life: str                                  # 近況
    interests: list[str] = Field(min_length=1)        # 興趣


class Personality(BaseModel):
    model_config = ConfigDict(extra="forbid")

    core_traits: str                                  # 核心特質
    initiative: str                                   # 主動性
    questioning: str | None = None                    # 提問傾向
    emotional_range: str | None = None                # 情緒幅度
    compliment_response: str | None = None            # 對讚美的反應
    self_disclosure: str | None = None                # 自我揭露


class LanguageStyle(BaseModel):
    """語言風格。

    spec 的驗收情境明確要求「不同角色在訊息長度、語助詞與 emoji 使用上
    呈現可辨識的差異」，因此這三項設為必要欄位。
    """

    model_config = ConfigDict(extra="forbid")

    pace: str                                         # 節奏
    message_length: str                               # 訊息長度
    particles: list[str] = Field(min_length=1)        # 語助詞
    emoji: str
    message_splitting: str | None = None              # 拆訊習慣
    punctuation: str | None = None                    # 標點
    typo_rate: float = Field(default=0.0, ge=0.0, le=1.0)  # 錯字率


class Boundaries(BaseModel):
    """邊界反應——依 design.md「產品的核心教學機制」，三個清單缺一不可。"""

    model_config = ConfigDict(extra="forbid")

    warms_up_to: list[str] = Field(min_length=1)      # 變熱絡
    cools_down_on: list[str] = Field(min_length=1)    # 變冷淡
    never_engages: list[str] = Field(min_length=1)    # 絕不接
    cooling_behavior: str                             # 冷淡的表現方式


class PersonaPromptFields(BaseModel):
    """會被渲染進模型提示的全部內容。"""

    model_config = ConfigDict(extra="forbid")

    identity: Identity
    background: Background
    personality: Personality
    language_style: LanguageStyle
    boundaries: Boundaries


class DelaySeconds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: int = Field(gt=0)
    max: int = Field(gt=0)

    @model_validator(mode="after")
    def _min_not_greater_than_max(self) -> DelaySeconds:
        if self.min > self.max:
            raise ValueError(f"回覆延遲秒數的 min（{self.min}）不可大於 max（{self.max}）")
        return self


class PersonaRuntimeParams(BaseModel):
    """程式執行參數。MUST NOT 進入提示——見本檔案開頭說明。"""

    model_config = ConfigDict(extra="forbid")

    reply_delay_seconds: DelaySeconds                 # 回覆延遲秒數
    typing_indicator: bool = True


class PersonaCard(BaseModel):
    """一張完整的人設卡，對應一個 YAML 檔。

    slug 不是 YAML 內容的一部分，而是由載入端依檔名決定
    （見 loader.py）——這樣角色改名不需要同步改檔名，
    且與 `personas` 表的 slug 欄位對應關係一目了然。
    """

    model_config = ConfigDict(extra="ignore")  # meta 等附加資訊允許存在但不強制定義結構

    slug: str
    identity: Identity
    background: Background
    personality: Personality
    language_style: LanguageStyle
    boundaries: Boundaries
    runtime: PersonaRuntimeParams
    safety_note: str | None = None

    @property
    def prompt_fields(self) -> PersonaPromptFields:
        """萃取出僅供渲染提示使用的子集，不含 runtime。"""
        return PersonaPromptFields(
            identity=self.identity,
            background=self.background,
            personality=self.personality,
            language_style=self.language_style,
            boundaries=self.boundaries,
        )
