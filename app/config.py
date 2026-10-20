"""設定載入。

依用途分為四組：平台憑證、資料庫、LLM 供應層、可調參數。

LLM 設定刻意「按用途分路徑」而非「按供應商」——
對話生成要快、即時分析要便宜、教練回饋要準，三者對延遲與成本的
要求不同，分開設定才能各自最佳化，且抽換供應商不需修改業務邏輯。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

LLMPath = Literal["dialog", "analysis", "coaching"]


class LLMPathConfig:
    """單一 LLM 路徑的設定：供應商 + 模型。"""

    def __init__(self, path: LLMPath, provider: str, model: str) -> None:
        # 「有哪些供應商」的真相在 app/llm/registry.py，不在這裡另存一份名單——
        # 兩份名單遲早會不一致，然後出現「設定驗證說可以、實際建構時說沒有」
        # 這種最難查的矛盾。延遲匯入以免設定模組在 import 期就相依於 llm 套件。
        from app.llm.registry import supported_providers

        supported = supported_providers()
        if provider not in supported:
            raise ValueError(
                f"LLM 路徑 '{path}' 指向未實作的供應商 '{provider}'。"
                f" 目前支援：{supported}。"
                f" 請修正環境變數 {path.upper()}_PROVIDER。"
            )
        self.path = path
        self.provider = provider
        self.model = model

    def __repr__(self) -> str:
        return f"LLMPathConfig(path={self.path!r}, provider={self.provider!r}, model={self.model!r})"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── 平台憑證 ──────────────────────────────────────
    # ⚠️ Meta 後台有「兩組」App 編號與密鑰，用途不同，2026-09-08 實測確認：
    #   - 應用程式編號／密鑰（META_APP_*）：Facebook 層級，用於 Graph API 的
    #     app access token 等一般用途
    #   - Instagram 應用程式編號／密鑰（IG_APP_*）：Instagram 產品專屬
    #
    # **webhook 的 X-Hub-Signature-256 是用 Instagram 專屬密鑰簽的**，不是
    # Facebook 的那組。Meta 文件未載明此事，用錯會導致每一筆真實 webhook
    # 都驗章失敗（而後台「測試」按鈕送的是假簽章，兩組密鑰都驗不過，
    # 無法用來分辨），極難從症狀反推。
    meta_app_secret: str = Field(default="", description="Facebook 層級 App 密鑰")
    meta_webhook_verify_token: str = Field(default="", description="webhook 訂閱驗證用")
    ig_app_id: str = Field(default="", description="Instagram 專屬 App 編號")
    ig_app_secret: str = Field(default="", description="Instagram 專屬密鑰，用於 webhook 驗章")
    ig_access_token: str = Field(default="", description="呼叫 Instagram 發訊 API")
    ig_account_id: str = Field(default="", description="角色的 IG 專業帳號 ID")
    ig_username: str = Field(
        default="",
        description="角色的 IG 帳號名稱（不含 @），設定頁「回到聊天」用 ig.me/m/<名稱> 開啟私訊。"
                    "未設定時改連到 Instagram 收件匣",
    )

    # ── 資料庫 ────────────────────────────────────────
    # POC 為 SQLite。Phase 1 改 Postgres 僅需替換此連線字串。
    database_url: str = "sqlite:///./data/poc.db"

    # ── LLM 供應層（三條路徑）─────────────────────────
    #
    # ⚠️ 2026-09-15 實測：原訂的 gemini-2.0-flash 與 gemini-2.5-flash **皆已退役**，
    # 呼叫回 HTTP 404 並在錯誤訊息中指名改用 gemini-3.6-flash。
    #
    # 同日以晴晴的人設卡實測三個候選模型的對話路徑延遲：
    #     gemini-3.6-flash   3.6 秒  ← 採用
    #     gemini-3.8-flash  17.5 秒  ← 太慢，會吃掉整個擬真延遲預算（8–20 秒）
    #     gemini-2.5-flash   404 已退役
    #
    # analysis 與 coaching 暫時同用 3.6-flash：POC 不跑這兩條路徑
    # （即時訊號屬 M11、教練報告屬 M5，皆不在範圍）。實際啟用時應依用途
    # 分開——analysis 改用 flash-lite 類的便宜模型，coaching 改用 pro 類。
    dialog_provider: str = "gemini"
    dialog_model: str = "gemini-3.6-flash"
    analysis_provider: str = "gemini"
    analysis_model: str = "gemini-3.6-flash"
    coaching_provider: str = "gemini"
    coaching_model: str = "gemini-3.6-flash"

    # 2026-09-17：dialog 路徑的免費額度緊繃（RPD 20 次／天，9.3／9.4
    # 測試很容易撞到），主要模型的重試全部用盡後改試這個模型，兩者都
    # 失敗才真正降級。這是使用者等待中的回覆，降級語句會讓人識破在跟
    # AI 講話，所以優先接了備援。
    #
    # 2026-09-18：原本認為 analysis 失敗只是「安靜回傳空結果，不影響
    # 體驗」，不值得接備援——但實測發現這個假設是錯的：analysis 路徑
    # 的失敗會讓 6.7 的話題萃取完全沒有機會成功執行（查資料庫證實
    # topic_memory 全部是空的），使用者感受到的是「記憶缺失」，並非
    # 真的無感。因此也接上備援。coaching 目前 POC 階段未啟用
    # （屬 M5，不在範圍），暫不接。
    #
    # 留空代表沒有備援，行為與現在完全相同。沿用同一個 provider
    # （gemini）：目前只有一家供應商，備援只是換個模型名稱，不需要
    # 額外的 XXX_FALLBACK_PROVIDER 環境變數。
    dialog_fallback_model: str = Field(
        default="", description="dialog 路徑的備援模型，主要模型重試用盡後改試這個"
    )
    analysis_fallback_model: str = Field(
        default="", description="analysis 路徑的備援模型，主要模型重試用盡後改試這個"
    )

    gemini_api_key: str = Field(default="", description="Google AI Studio 金鑰")

    # ── 可調參數 ──────────────────────────────────────
    silence_threshold_seconds: int = Field(
        default=1500, description="靜默多久判定對話結束，預設 25 分鐘"
    )
    silence_poll_interval_seconds: int = Field(
        default=60,
        description="task 6.4 背景輪詢的掃描間隔。repository.find_silent_sessions "
                    "的文件原本就假定「每分鐘呼叫」，此處對應到同一個假設",
    )
    send_rate_limit_per_hour: int = Field(
        default=200,
        description="每角色帳號每小時發訊上限。**這不是 Meta 的 API 限制**——"
                    "官方發訊上限是 100 calls/秒/帳號（2026-09-23 查證），遠高於此。"
                    "200 是刻意的保守配速，用來避開 Meta 的反垃圾訊息偵測："
                    "該偵測沒有公開門檻，觸發會被停權 24–48 小時，且本專案帳號"
                    "已有兩次被風控封鎖的紀錄。調高前須先實測確認不觸發偵測，"
                    "不可因為「官方允許更高」就放寬",
    )
    llm_timeout_seconds: int = 30
    llm_max_retries: int = Field(
        default=2,
        description="LLM 呼叫失敗後的重試次數。免費模型速率上限低，"
                    "失敗是常態；但重試會拖長使用者的等待，故不宜設高",
    )
    llm_retry_base_seconds: float = Field(
        default=1.0, description="LLM 退避重試的基數，實際等待為 base × 2^嘗試次數"
    )
    send_max_retries: int = Field(
        default=3, description="觸發平台速率限制後的重試次數"
    )
    send_retry_base_seconds: float = Field(
        default=2.0, description="退避重試的基數，實際等待為 base × 2^嘗試次數"
    )
    send_max_wait_seconds: float = Field(
        default=300.0,
        description="配額不足時的最長等待秒數。超過即放棄該則並記錄，"
                    "避免背景任務無限期卡住",
    )
    # 20 改 60（2026-09-17）：9.3 要測 30 則以上的連續對話，20 則窗口
    # 會讓後段對話忘記前面講過的事，讓測試敗在窗口太小而非人設或模型
    # 本身。Gemini Flash 的上下文窗遠遠塞得下 60 則短訊息，成本影響
    # 可忽略；真正的摘要壓縮見 add-persona-authoring-console 任務 8
    # （原 6.6，2026-10-12 移入），對話量遠超過此規模才需要。
    dialog_context_message_count: int = Field(
        default=60, description="送入模型的近期訊息則數"
    )
    # 任務 8（design.md 決策 10）：同一場次超過 summary_trigger_messages 則時，
    # 保留最近 summary_keep_recent 則原文，較早的寫成摘要；之後每再多出
    # summary_update_batch 則，以「舊摘要＋新超出的訊息」更新一次
    summary_trigger_messages: int = Field(
        default=60, ge=2, description="場次訊息超過幾則開始產生摘要"
    )
    summary_keep_recent: int = Field(
        default=40, ge=1, description="產生摘要後，保留幾則最近訊息的原文"
    )
    summary_update_batch: int = Field(
        default=20, ge=1, description="已有摘要時，再多出幾則才更新摘要"
    )
    signal_burst_window_seconds: int = Field(
        default=30, description="task 8.2 的 L10 訊息轟炸：多少秒內連發算轟炸"
    )
    signal_burst_min_messages: int = Field(
        default=3, description="task 8.2 的 L10 訊息轟炸：時間窗內連發幾則以上算轟炸"
    )
    previous_session_message_count: int = Field(
        default=10,
        description="新場次開頭帶入的前幾個場次的最後訊息則數，讓使用者隔一段時間"
                    "回來時，角色仍記得之前聊過的細節",
    )
    coach_report_min_user_messages: int = Field(
        default=4,
        description="M9：session 結束時使用者訊息數達此門檻才發送教練報告邀請。"
                    "design.md 決策 5：兩三則往返產不出有意義的回饋，硬送邀請"
                    "只會讓使用者點開發現內容空泛，損害信任感",
    )

    # ── 使用者設定頁（add-persona-authoring-console 任務 5.1）──
    # 使用者在私訊傳 settings_keyword，系統回一條帶簽章、有時效的設定頁連結。
    # 密鑰或對外網址任一未設定時，此功能關閉，關鍵字當一般訊息處理——
    # 不能在缺設定的情況下發出一條點了會壞掉的連結。
    settings_link_secret: str = Field(
        default="",
        description="設定頁連結的簽章密鑰，至少 32 字元。不可與其他密鑰共用",
    )
    public_base_url: str = Field(
        default="",
        description="服務的對外網址，例如 https://xxx.ngrok-free.dev，用於組設定頁連結",
    )
    settings_link_ttl_seconds: int = Field(
        default=1800, description="設定頁連結的有效秒數，逾時需重新在私訊傳關鍵字"
    )
    settings_keyword: str = Field(default="設定", description="取得設定頁連結的私訊關鍵字")

    # ── 後台登入（任務 5.2、5.3）──
    login_session_ttl_seconds: int = Field(
        default=43200, description="管理員與客戶登入後的有效秒數（預設 12 小時）"
    )
    login_max_failures: int = Field(
        default=5, description="同一帳號在 login_failure_window_seconds 內允許的登入失敗次數"
    )
    login_failure_window_seconds: int = Field(
        default=900, description="計算登入失敗次數的時間窗（秒），超過次數即暫時拒絕該帳號登入"
    )

    # ── 執行環境 ──────────────────────────────────────
    log_level: str = "INFO"
    environment: str = "development"

    # ── 除錯逃生門 ────────────────────────────────────
    # 2026-09-08：真實 Instagram webhook 的 X-Hub-Signature-256 與
    # META_APP_SECRET 算出的值對不上，原因尚未查明（密鑰本身已證實有效，
    # 可用 client_credentials 換到 app access token）。為了不讓簽章這一關
    # 擋住 POC 其餘管線的驗證，提供此開關暫時略過驗章。
    #
    # 僅在 environment == "development" 時生效——見 skip_signature_check，
    # 就算誤把此變數帶進正式環境也不會關掉驗章。
    webhook_skip_signature_check: bool = Field(
        default=False,
        description="開發用：略過 webhook 簽章驗證。正式環境無效。",
    )

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        return v.upper()

    @model_validator(mode="after")
    def _check_summary_window(self) -> "Settings":
        # 保留的原文必須少於觸發門檻，否則永遠沒有東西可摘要；也不能多於送進
        # 模型的近期則數，否則摘要與近期訊息之間會有一段誰都沒帶到的空白
        if self.summary_keep_recent >= self.summary_trigger_messages:
            raise ValueError("SUMMARY_KEEP_RECENT 必須小於 SUMMARY_TRIGGER_MESSAGES")
        if self.summary_keep_recent > self.dialog_context_message_count:
            raise ValueError("SUMMARY_KEEP_RECENT 不可大於 DIALOG_CONTEXT_MESSAGE_COUNT")
        return self

    # ── 衍生設定 ──────────────────────────────────────
    @property
    def settings_link_enabled(self) -> bool:
        """設定頁連結功能是否可用。密鑰太短視同未設定，見 app/auth/settings_link.py。"""
        from app.auth.settings_link import MIN_SECRET_LENGTH

        return len(self.settings_link_secret) >= MIN_SECRET_LENGTH and bool(self.public_base_url)

    @property
    def llm_paths(self) -> dict[LLMPath, LLMPathConfig]:
        """三條 LLM 路徑的設定。

        於此處建構，因此設定指向未實作的供應商時會在
        「第一次讀取設定」當下就報錯，而非執行期才失敗。
        """
        return {
            "dialog": LLMPathConfig("dialog", self.dialog_provider, self.dialog_model),
            "analysis": LLMPathConfig("analysis", self.analysis_provider, self.analysis_model),
            "coaching": LLMPathConfig("coaching", self.coaching_provider, self.coaching_model),
        }

    @property
    def llm_path_configs(self) -> dict[str, "PathConfig"]:
        """三條路徑的設定，轉成 llm 層的 PathConfig。

        config 層與 llm 層各有一個路徑設定型別，看似重複，但方向是對的：
        `LLMPathConfig` 綁在環境變數上（知道 DIALOG_PROVIDER 這種名字），
        `PathConfig` 是 llm 層的純資料，不該知道設定是從哪來的。
        """
        from app.llm.base import PathConfig

        return {
            name: PathConfig(path=name, provider=cfg.provider, model=cfg.model)
            for name, cfg in self.llm_paths.items()
        }

    @property
    def llm_fallback_path_configs(self) -> dict[str, "PathConfig"]:
        """有設定備援模型的路徑，轉成 llm 層的 PathConfig。

        目前 dialog／analysis 可能有值，coaching 尚未接（POC 階段未啟用，
        屬 M5 範圍）；留空代表該路徑沒有備援。沿用該路徑原本的
        provider——備援只是換模型，不是換供應商。
        """
        from app.llm.base import PathConfig

        configs: dict[str, PathConfig] = {}
        if self.dialog_fallback_model:
            configs["dialog"] = PathConfig(
                path="dialog", provider=self.dialog_provider, model=self.dialog_fallback_model
            )
        if self.analysis_fallback_model:
            configs["analysis"] = PathConfig(
                path="analysis",
                provider=self.analysis_provider,
                model=self.analysis_fallback_model,
            )
        return configs

    @property
    def llm_api_keys(self) -> dict[str, str]:
        """供應商名稱 -> 金鑰。新增供應商時在此加一行。"""
        return {"gemini": self.gemini_api_key}

    def missing_llm_keys(self) -> list[str]:
        """回傳有路徑在用、但金鑰未填的供應商對應環境變數名稱。

        與 missing_platform_credentials() 同樣不阻擋啟動：缺金鑰時
        webhook 與人設卡等其餘功能仍可測試，僅對話生成不能用。
        """
        keys = self.llm_api_keys
        used = {cfg.provider for cfg in self.llm_paths.values()}
        return sorted(
            f"{provider.upper()}_API_KEY"
            for provider in used
            if not keys.get(provider)
        )

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def database_url_for_log(self) -> str:
        """寫進 log 用的連線字串，密碼以 *** 取代。

        SQLite 時代連線字串沒有密碼，直接記錄無妨；換成 Postgres 後原樣
        記錄會把資料庫密碼寫進 log（2026-10-02 容器驗證時發現）。
        """
        return make_url(self.database_url).render_as_string(hide_password=True)

    @property
    def webhook_signing_secret(self) -> str:
        """驗證 webhook 簽章要用的密鑰。

        Instagram 專屬密鑰優先。留一條退回 META_APP_SECRET 的路，是為了
        相容尚未補上 IG_APP_SECRET 的既有 .env——但那條路必然驗章失敗，
        所以 main.py 在啟動時會明確警告，而不是靜靜地退回去。
        """
        return self.ig_app_secret or self.meta_app_secret

    @property
    def skip_signature_check(self) -> bool:
        """是否略過 webhook 簽章驗證。

        兩個條件都成立才略過：明確設定了旗標，且執行環境為 development。
        把「環境」這個條件寫死在這裡，而不是交給部署時的環境變數自律，
        是為了讓「正式環境不可能關掉驗章」成為程式保證而非人為約定。
        """
        return self.webhook_skip_signature_check and self.environment == "development"

    def missing_platform_credentials(self) -> list[str]:
        """回傳尚未填寫的平台憑證欄位名稱。

        POC 開發初期可在沒有憑證的情況下用假 payload 測試，
        因此缺憑證不阻擋啟動，僅由 main.py 記錄警告。
        """
        required = {
            "META_WEBHOOK_VERIFY_TOKEN": self.meta_webhook_verify_token,
            "IG_APP_SECRET": self.ig_app_secret,
            "IG_ACCESS_TOKEN": self.ig_access_token,
            "IG_ACCOUNT_ID": self.ig_account_id,
        }
        return [name for name, value in required.items() if not value]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """取得設定單例。

    以 lru_cache 快取，避免每次呼叫都重讀 .env。
    測試需要覆寫時呼叫 get_settings.cache_clear()。
    """
    settings = Settings()
    # 提早觸發供應商驗證，讓設定錯誤在啟動時就暴露
    _ = settings.llm_paths
    return settings
