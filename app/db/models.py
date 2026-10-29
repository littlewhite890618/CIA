"""資料表定義。

表結構完全依照 openspec/changes/add-ig-persona-chat-mvp 的 spec 設計。

POC 使用 SQLite，Phase 1 改用 Postgres 16。為了讓遷移只需替換連線字串，
此處一律使用 SQLAlchemy 的泛型型別，不使用任何資料庫專屬型別。
（Phase 1 可將 JSON 改為 postgresql.JSONB 以取得索引能力，但非必要。）

狀態與列舉值以字串常數表示，不使用資料庫原生 enum ——
原生 enum 在跨資料庫遷移與後續新增值時都很麻煩。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ═══════════════════════════════════════════════════════════
#  常數
# ═══════════════════════════════════════════════════════════

class PersonaStatus:
    """角色生命週期狀態。

    POC 與 Phase 1 僅使用 ONLINE / DISABLED。
    DRAFT 與 REVIEWING 於 v3 隨角色克隆（M10）啟用 —— 屆時才需要審核關卡。
    """
    DRAFT = "draft"
    REVIEWING = "reviewing"
    ONLINE = "online"
    PAUSED = "paused"
    DISABLED = "disabled"


class TenantStatus:
    """客戶（租戶）帳號狀態。帳號由管理員開通，不提供自助註冊。"""
    ACTIVE = "active"
    DISABLED = "disabled"


class AccountRole:
    """後台登入帳號的角色（任務 5.2、5.3）。終端使用者不登入，走設定頁連結。"""
    ADMIN = "admin"    # 平台管理員：開通客戶、管理共用角色
    CLIENT = "client"  # 客戶（交友公司、KOL）：只看自己帶來的使用者的統計


class PersonaSource:
    HUMAN = "human"      # 人工撰寫
    CLONED = "cloned"    # M10 從使用者風格萃取


class MessageDirection:
    INBOUND = "inbound"    # 使用者 -> 系統
    OUTBOUND = "outbound"  # 系統 -> 使用者


class SystemMessageKind:
    """不屬於聊天內容的訊息，記在 messages.meta["system"]。

    這類訊息照常落庫（入站的仍需去重、重置 24 小時視窗、讓發訊前的
    「是否互動過」檢查通過），但不進入對話上下文、話題萃取、訊號偵測
    與插話判定——見 repository.recent_messages 等函式與 design.md 決策 14。
    """
    SETTINGS_LINK = "settings_link"  # 使用者傳關鍵字與系統回覆的設定頁連結
    COACH_DIGEST = "coach_digest"  # 凌晨送出的教練回饋連結（daily-coach-report-link）


class SessionState:
    ACTIVE = "active"
    ENDED = "ended"


class SignalType:
    LANDMINE = "landmine"           # 地雷
    CONVERSATION_KILLER = "killer"  # 句點
    HIGHLIGHT = "highlight"         # 亮點


class SignalStatus:
    OK = "ok"          # 偵測成功（含「沒有任何訊號」）
    FAILED = "failed"  # 模型失敗或輸出壞掉，待 scripts/rerun_signals.py 重跑


class Severity:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ═══════════════════════════════════════════════════════════
#  客戶群組（add-persona-authoring-console 第 3 群）
# ═══════════════════════════════════════════════════════════

class Tenant(Base):
    """客戶（租戶）：在平台上建立角色的交友公司或網紅。

    登入帳號與權限屬於任務 5.x，不在此表；此表只代表「一個客戶」這個
    單位，供使用者歸屬指向。客戶只負責帶使用者進來並看彙總統計，
    不設定角色（design.md 決策 13）。
    """

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, comment="客戶名稱，管理員開通時填寫")
    ref_code: Mapped[str] = mapped_column(
        String(64), unique=True,
        comment="專屬私訊連結的代碼（ig.me/m/<帳號>?ref=<代碼>）。平台限英數與 - _ =",
    )
    status: Mapped[str] = mapped_column(String(16), default=TenantStatus.ACTIVE, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Account(Base):
    """後台登入帳號：管理員或客戶（任務 5.2、5.3）。

    管理員不屬於任何客戶；客戶帳號必屬於一個客戶——由資料庫的 CHECK
    約束保證，不只靠程式記得。一個客戶可以有多個帳號。
    密碼只存 scrypt 雜湊（app/auth/accounts.py），不存明文。
    """

    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint(
            "(role = 'admin' AND tenant_id IS NULL) OR (role = 'client' AND tenant_id IS NOT NULL)",
            name="ck_accounts_role_tenant",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, comment="登入帳號")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginSession(Base):
    """後台的登入狀態（任務 5.2）。

    瀏覽器的 cookie 只存一組隨機代碼，這裡存它的 SHA-256——資料庫外洩
    也拿不到能直接登入的值。存在伺服器端而非簽章 cookie，是為了登出、
    重設密碼時能立即讓既有登入失效。
    """

    __tablename__ = "login_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ═══════════════════════════════════════════════════════════
#  身分群組
# ═══════════════════════════════════════════════════════════

class Persona(Base):
    """共用角色：一個 IG 帳號，由平台管理（design.md 決策 12）。

    名字與 IG 帳號屬於共用角色，所有客戶的使用者看到的是同一個帳號。
    角色的平台預設版本有兩種來源（決策 3）：
    - 內建角色（晴晴）：來自 YAML 檔（app/personas/cards/），card 為 NULL
    - 管理員自資料庫建立的共用角色：人設卡整份存在 card（決策 1）
    使用者自訂的內容不在此表，而是 UserPersonaVersion（決策 13）。

    personality_tags／language_style／boundaries／runtime_params 是 POC 時期
    預留的欄位，實際從未寫入；人設內容一律存在 card 或 YAML 檔。
    """

    __tablename__ = "personas"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, comment="對應人設卡檔名，如 sunny")
    name: Mapped[str] = mapped_column(String(64), comment="角色顯示名稱")
    gender: Mapped[str] = mapped_column(String(16))

    # webhook payload 的 entry[].id，用於角色路由。
    # 可為 NULL：管理員新建的共用角色在平台作業完成前還沒有 IG 帳號，
    # 回填才是上線閘門（design.md 決策 5）。唯一索引允許多個 NULL。
    ig_account_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)

    card: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        comment="平台預設版本的整份人設卡（結構同 app/personas/schema.py 的 PersonaCard，"
                "不含 slug）。內建角色為 NULL，內容以 YAML 檔為準",
    )

    personality_tags: Mapped[list[Any] | None] = mapped_column(JSON, default=list)
    language_style: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
    boundaries: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=dict, comment="變熱絡／變冷淡／絕不接。教學機制的核心"
    )
    runtime_params: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=dict, comment="回覆延遲、拆分機率。由程式執行，不進入提示"
    )

    source: Mapped[str] = mapped_column(String(16), default=PersonaSource.HUMAN)
    status: Mapped[str] = mapped_column(String(16), default=PersonaStatus.ONLINE, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserPersonaVersion(Base):
    """使用者版本：某位使用者針對某個共用角色保存的人設（design.md 決策 13）。

    以 user_persona_links（使用者 × 共用角色）為單位，一個連結至多一份；
    沒有這一列的使用者套用共用角色的平台預設版本。

    card 是人設卡扣掉 identity.name 與 identity.ig_display_name——這兩項沿用
    共用角色，載入時由共用角色補入後再以 PersonaCard 驗證（任務 4.1）。
    設定頁第一次開啟時預先帶入平台預設內容、保存時才驗證，因此不存在
    不完整的草稿，card 必填。不經人工審核，沒有審核狀態。
    """

    __tablename__ = "user_persona_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    link_id: Mapped[int] = mapped_column(ForeignKey("user_persona_links.id"), unique=True, index=True)
    # none_as_null：預設 JSON 欄位會把 None 存成 JSON 的 null，NOT NULL 擋不住
    card: Mapped[dict[str, Any]] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PersonaCoaching(Base):
    """使用者版本的教練設定（persona-authoring spec「教練設定」，一版本一列）。

    平台預設版本沒有教練設定，一律沿用系統預設。每個欄位為 NULL 也代表
    「沿用系統預設」，使未設定的版本維持既有行為
    （realtime-signals spec「角色未設定教練內容」）：
    - disabled_rule_ids：停用的系統預設規則代號，如 ["L9", "K2"]
    - custom_rules：自訂規則，每筆含 text／type／dimension／severity（僅地雷）
    - report_tone／report_length：報告語氣與篇幅，選單值或客戶自訂文字
    - report_sections：啟用的報告段落，依序排列，每筆含 key／title／description
    評分維度固定六項，不存於此（design.md 決策 8）。
    """

    __tablename__ = "persona_coaching"

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("user_persona_versions.id"), unique=True, index=True)

    disabled_rule_ids: Mapped[list[Any] | None] = mapped_column(JSON)
    custom_rules: Mapped[list[Any] | None] = mapped_column(JSON)
    report_tone: Mapped[str | None] = mapped_column(Text)
    report_length: Mapped[str | None] = mapped_column(Text)
    report_sections: Mapped[list[Any] | None] = mapped_column(JSON)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class User(Base):
    """使用者。

    以 Instagram username 為跨角色識別鍵 —— IGSID 的範圍是單一專業帳號，
    同一人與不同角色互動會得到不同 ID，直接用 IGSID 當主鍵會讓
    使用者換角色即等同輪廓歸零。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    ig_username: Mapped[str | None] = mapped_column(
        String(128), unique=True, index=True,
        comment="跨角色識別鍵。取得失敗時為 NULL，該使用者標記為身分未確認",
    )
    identity_confirmed: Mapped[bool] = mapped_column(
        Boolean, default=False,
        comment="False 表示未能取得 username，僅能以 IGSID 運作",
    )
    external_member_id: Mapped[str | None] = mapped_column(
        String(64), index=True, comment="想想交友會員 ID，可為空"
    )
    gender_preference: Mapped[str | None] = mapped_column(
        String(16), comment="由行為推斷：使用者私訊哪個性別的角色"
    )

    # ── 合規欄位。POC 不使用，但必須從第一天就存 ──
    # 同意須在資料蒐集當下取得，不可事後補。若 v3 才建，
    # 先前累積的對話將無法用於角色克隆。
    #
    # 年齡驗證不在此表另存時間戳：晴晴的 IG 專業帳號已於平台端
    # 設定 18 歲以上限制，把關責任交給 Instagram，系統不重複記錄。
    # 若未來平台限制不足以滿足法遵要求（例如需舉證「何時、如何驗證」），
    # 屆時再補一個時間戳欄位，成本很低。
    consent: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=dict,
        comment="分項同意記錄，含時間與條款版本。必須可撤回",
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    links: Mapped[list[UserPersonaLink]] = relationship(back_populates="user")


class UserPersonaLink(Base):
    """使用者與角色的對應，保存該角色帳號範圍內的 IGSID。

    這張表的存在理由：Instagram 給的編號是「每個專業帳號各自一套」，
    同一個人跟不同角色聊會拿到不同編號且無法互推。
    """

    __tablename__ = "user_persona_links"
    __table_args__ = (
        UniqueConstraint("persona_id", "igsid", name="uq_persona_igsid"),
        Index("ix_link_user_persona", "user_id", "persona_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    persona_id: Mapped[int] = mapped_column(ForeignKey("personas.id"), index=True)
    igsid: Mapped[str] = mapped_column(String(64), index=True)

    # 話題記憶掛在此表而非 users —— 角色的記憶應各自獨立。
    # 阿哲不該知道使用者跟晴晴聊過什麼，那會有被監控的感覺。
    topic_memory: Mapped[list[Any] | None] = mapped_column(JSON, default=list)

    # 使用者歸屬（design.md 決策 12）：以「使用者 × 共用角色」為單位，正好是這張表。
    # 第一次透過專屬連結進入時寫入，之後不再變更；NULL 表示未歸屬。
    # 歸屬只用於客戶統計，不影響套用的人設（決策 13）。
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id"), index=True, comment="歸屬的客戶。NULL＝未歸屬",
    )
    attribution_ref: Mapped[str | None] = mapped_column(
        String(2083), comment="建立歸屬時平台傳來的代碼原文（上限依平台規格 2,083 字元）",
    )
    attributed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="links")


# ═══════════════════════════════════════════════════════════
#  對話群組
# ═══════════════════════════════════════════════════════════

class Session(Base):
    """一段連續互動。

    靜默達門檻即標記結束。教練報告的範圍、24 小時視窗、邀請去重
    三者皆掛在此表上。
    """

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_session_silence_scan", "state", "invite_sent", "last_message_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    persona_id: Mapped[int] = mapped_column(ForeignKey("personas.id"), index=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow,
        comment="使用者最後一則訊息的時間。用於靜默判定，判斷對方是否已離開",
    )
    window_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="24 小時視窗到期時間。與 last_message_at 分開儲存，"
                "因為點擊按鈕會重置視窗但不產生訊息，兩者會分岔",
    )

    invite_sent: Mapped[bool] = mapped_column(
        Boolean, default=False,
        comment="教練報告邀請是否已發送。防止背景輪詢每分鐘重複發送",
    )
    invite_skip_reason: Mapped[str | None] = mapped_column(
        String(32),
        comment="task M9：未發送邀請的原因（too_few_messages／視窗逾期等）。"
                "invite_sent 維持 False，此欄位僅供觀測，不影響輪詢邏輯——"
                "session 已 ENDED，find_silent_sessions 本就不會再選到它",
    )
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(16), default=SessionState.ACTIVE, index=True)

    signal_status: Mapped[str | None] = mapped_column(
        String(16),
        comment="task 8.4 訊號偵測狀態：ok／failed，尚未偵測為 NULL。"
                "用來分辨「沒有訊號」與「偵測失敗」",
    )
    signal_error: Mapped[str | None] = mapped_column(Text, comment="偵測失敗的原因")

    # add-persona-authoring-console 任務 8（design.md 決策 10）：長對話摘要
    summary: Mapped[str | None] = mapped_column(
        Text, comment="這個場次較早訊息的摘要；未超過門檻或尚未產生為 NULL"
    )
    summary_until_message_id: Mapped[int | None] = mapped_column(
        Integer,
        comment="摘要涵蓋到哪一則訊息（含）。之後的訊息以原文放進上下文",
    )

    # daily-coach-report-link 任務 3.1：這段對話被併入哪一則每日回饋。
    # 有值就代表已處理過，之後的每日送出不再撈它
    coach_digest_id: Mapped[int | None] = mapped_column(
        ForeignKey("coach_digests.id"), index=True,
        comment="併入的每日回饋；NULL 表示尚未送出",
    )


class Message(Base):
    """單則訊息。入站與出站共用此表，以 direction 區分。"""

    __tablename__ = "messages"
    __table_args__ = (
        # 事件去重：Meta 未即時收到 200 會重送，重送帶相同 ID。
        # 於資料庫層擋下，程式碼不需自行判斷，也不會有漏判。
        UniqueConstraint("platform_message_id", name="uq_platform_message_id"),
        Index("ix_message_session_time", "session_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    direction: Mapped[str] = mapped_column(String(16))

    platform_message_id: Mapped[str | None] = mapped_column(
        String(255),
        comment="Instagram 給的訊息編號。出站訊息在送出前尚無此值，故可為 NULL",
    )
    content: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)


class RealtimeSignal(Base):
    """M11 的偵測結果。教練報告的唯一資料來源。

    在對話當下標記，而非事後從純文字重新推斷 ——
    事後分析較貴、較不準，且無法引用使用者原句。
    """

    __tablename__ = "realtime_signals"
    __table_args__ = (Index("ix_signal_session_type", "session_id", "signal_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id"), index=True,
        comment="關聯至產生此訊號的具體訊息，使報告能引用原句",
    )
    signal_type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str | None] = mapped_column(String(16))
    dimension: Mapped[str | None] = mapped_column(
        String(32), comment="六項評分維度之一，見 docs/技術可行性與架構規劃.md §3.4.5",
    )
    reason: Mapped[str | None] = mapped_column(Text, comment="判定原因")
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ═══════════════════════════════════════════════════════════
#  產出群組（POC 不使用，先建表以避免日後遷移）
# ═══════════════════════════════════════════════════════════

class CoachReport(Base):
    """M5 的產出。一場對話一份，非一天一份。"""

    __tablename__ = "coach_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)

    generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="內容產生完成的時刻"
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="實際送達的時刻。NULL 表示已生成但未送達，可據以補送與追成本",
    )
    content: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=dict, comment="五段結構：摘要／做得好／可調整／改寫示範／下次可試"
    )
    failure_reason: Mapped[str | None] = mapped_column(
        Text,
        comment="生成失敗的原因。generated_at 為 NULL 且此欄位有值 = 生成失敗，"
                "可重新生成；generated_at 有值則此欄位應為 NULL（見 repository."
                "mark_report_generated 會清空它）",
    )


class SafetyEvent(Base):
    """M12 的偵測結果。"""

    __tablename__ = "safety_events"
    __table_args__ = (Index("ix_safety_pending", "severity", "handled_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id"), index=True)

    category: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16), index=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)

    handled_by: Mapped[str | None] = mapped_column(String(64))
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RateBudget(Base):
    """發訊速率觀測。

    POC 不做配額控制，但保留此表以蒐集實際觸發速率限制的資料 ——
    用於驗證「200 則／小時／帳號」這個未經官方證實的假設。
    """

    __tablename__ = "rate_budget"
    __table_args__ = (
        UniqueConstraint("persona_id", "window_start", name="uq_rate_window"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    persona_id: Mapped[int] = mapped_column(ForeignKey("personas.id"), index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    rate_limited_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="平台回傳速率限制錯誤的次數"
    )


class SendError(Base):
    """每一次發訊失敗（add-persona-authoring-console 第 9 群：發訊配速的監測）。

    分類見 app/channels/send_errors.py。存原始的錯誤代碼與說明，而不只是
    rate_budget 那樣的計數：被判違規時要能回頭查 Meta 說了什麼、發生在哪一刻。
    失敗很少發生，這張表不會長大。
    """

    __tablename__ = "send_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    persona_id: Mapped[int] = mapped_column(ForeignKey("personas.id"), index=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id"))
    category: Mapped[str] = mapped_column(
        String(32), index=True,
        comment="rate_limited／policy_block／token_invalid／window_expired／other",
    )
    http_status: Mapped[int | None] = mapped_column(Integer, comment="連線失敗時為 NULL")
    error_code: Mapped[int | None] = mapped_column(Integer, comment="Meta 的 error.code")
    error_subcode: Mapped[int | None] = mapped_column(Integer, comment="Meta 的 error.error_subcode")
    detail: Mapped[str | None] = mapped_column(Text, comment="錯誤原文，截前 500 字")


class SettingsLinkClaim(Base):
    """設定連結的「已被打開」紀錄（add-persona-authoring-console 任務 5.8）。

    連結第一次被使用時記下來，並給打開的那個瀏覽器一組隨機代碼（cookie）；
    之後只有帶著這組代碼的瀏覽器能繼續使用這條連結，轉貼給別人的連結
    在別的裝置上會被拒絕。只存雜湊：資料庫外洩時，無法拿來還原連結或代碼。
    """

    __tablename__ = "settings_link_claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, comment="設定連結 token 的 SHA-256"
    )
    claim_hash: Mapped[str] = mapped_column(
        String(64), comment="打開連結的瀏覽器所持代碼的 SHA-256"
    )
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, comment="連結到期時間，過期一陣子後清除"
    )


class CoachDigest(Base):
    """一則每日回饋訊息（daily-coach-report-link 任務 3.1，design.md 決策 1）。

    凌晨送出時，同一位使用者在這之前結束、尚未送過的對話合併成一列；
    場次以 sessions.coach_digest_id 指向它。報告本身仍是每段對話一列，
    存在 coach_reports，要等使用者打開連結才產生。
    """

    __tablename__ = "coach_digests"

    id: Mapped[int] = mapped_column(primary_key=True)
    link_id: Mapped[int] = mapped_column(ForeignKey("user_persona_links.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="連結訊息實際送出的時刻；未送出為 NULL"
    )
    skip_reason: Mapped[str | None] = mapped_column(
        String(64), comment="未能送出的原因（例如訊息視窗已逾期）"
    )
