"""資料存取層。

將資料庫操作集中於此，上層模組（M1 通路、M2 人設、M3 對話）
不直接組 SQL，也不需要知道表結構。

POC 僅實作已用得到的操作。Phase 1 補功能時在此擴充，不改上層。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from app.db.models import (
    SendError,
    CoachReport,
    Message,
    MessageDirection,
    Persona,
    PersonaCoaching,
    PersonaStatus,
    RateBudget,
    RealtimeSignal,
    Session,
    SessionState,
    SignalStatus,
    User,
    UserPersonaLink,
    UserPersonaVersion,
    utcnow,
)
from app.logging_config import get_logger

log = get_logger(__name__)

# Meta 平台規則：使用者最後一次互動起算 24 小時內才能主動發訊
MESSAGING_WINDOW = timedelta(hours=24)


# ═══════════════════════════════════════════════════════════
#  角色
# ═══════════════════════════════════════════════════════════

def get_persona_by_ig_account(db: DBSession, ig_account_id: str) -> Persona | None:
    """依 webhook payload 的 entry[].id 找出對應角色。

    所有角色帳號的訊息都送到同一個 webhook 網址，因此必須由此分流。
    即使 POC 只有一個角色，仍需此步驟取出正確的 access token。
    """
    return db.scalar(
        select(Persona).where(
            Persona.ig_account_id == ig_account_id,
            Persona.status == PersonaStatus.ONLINE,
        )
    )


def upsert_persona(
    db: DBSession,
    *,
    slug: str,
    name: str,
    gender: str,
    ig_account_id: str,
    personality_tags: list[Any] | None = None,
    language_style: dict[str, Any] | None = None,
    boundaries: dict[str, Any] | None = None,
    runtime_params: dict[str, Any] | None = None,
) -> Persona:
    """建立或更新角色。POC 於啟動時由人設卡同步至此表。"""
    persona = db.scalar(select(Persona).where(Persona.slug == slug))
    if persona is None:
        persona = Persona(slug=slug)
        db.add(persona)

    persona.name = name
    persona.gender = gender
    persona.ig_account_id = ig_account_id
    persona.personality_tags = personality_tags or []
    persona.language_style = language_style or {}
    persona.boundaries = boundaries or {}
    persona.runtime_params = runtime_params or {}
    persona.status = PersonaStatus.ONLINE

    db.flush()
    return persona


# ═══════════════════════════════════════════════════════════
#  使用者身分歸戶
# ═══════════════════════════════════════════════════════════

def find_user_by_igsid(
    db: DBSession, persona_id: int, igsid: str
) -> tuple[User, UserPersonaLink] | None:
    """以 IGSID 查出使用者。找不到回傳 None。"""
    link = db.scalar(
        select(UserPersonaLink).where(
            UserPersonaLink.persona_id == persona_id,
            UserPersonaLink.igsid == igsid,
        )
    )
    if link is None:
        return None
    return link.user, link


def resolve_user(
    db: DBSession,
    *,
    persona_id: int,
    igsid: str,
    ig_username: str | None,
) -> tuple[User, UserPersonaLink, bool]:
    """依 IGSID 取得使用者，必要時建立。

    Instagram 給的編號是「每個專業帳號各自一套」，同一個人跟不同角色
    聊會拿到不同編號且無法互推。因此流程是：

        IGSID 查連結表
          ├─ 找到 -> 直接取得使用者
          └─ 找不到 -> 取 username
                        ├─ username 已有使用者 -> 建新連結（跨角色歸戶）
                        └─ 全新 -> 建使用者 + 連結

    ig_username 為 None 時（使用者封鎖了 App 或 API 失敗），
    仍建立可運作的紀錄，但標記 identity_confirmed=False。
    此類使用者無法跨角色歸戶，換角色即視為新人。

    回傳 (user, link, is_new_user)
    """
    existing = find_user_by_igsid(db, persona_id, igsid)
    if existing is not None:
        user, link = existing
        return user, link, False

    user: User | None = None
    if ig_username:
        user = db.scalar(select(User).where(User.ig_username == ig_username))

    is_new_user = user is None
    if user is None:
        user = User(
            ig_username=ig_username,
            identity_confirmed=bool(ig_username),
        )
        db.add(user)
        db.flush()
    elif ig_username:
        log.info(
            "跨角色歸戶：同一使用者在新角色下建立連結",
            extra={"user_id": user.id, "username": ig_username, "persona_id": persona_id},
        )

    link = UserPersonaLink(
        user_id=user.id,
        persona_id=persona_id,
        igsid=igsid,
        topic_memory=[],
    )
    db.add(link)
    db.flush()
    return user, link, is_new_user


# ═══════════════════════════════════════════════════════════
#  對話場次
# ═══════════════════════════════════════════════════════════

def get_active_session(db: DBSession, user_id: int, persona_id: int) -> Session | None:
    return db.scalar(
        select(Session).where(
            Session.user_id == user_id,
            Session.persona_id == persona_id,
            Session.state == SessionState.ACTIVE,
        )
    )


def get_or_create_session(db: DBSession, user_id: int, persona_id: int) -> tuple[Session, bool]:
    """取得進行中的 session，沒有就建立。回傳 (session, is_new)。"""
    session = get_active_session(db, user_id, persona_id)
    if session is not None:
        return session, False

    now = utcnow()
    session = Session(
        user_id=user_id,
        persona_id=persona_id,
        started_at=now,
        last_message_at=now,
        window_expires_at=now + MESSAGING_WINDOW,
        state=SessionState.ACTIVE,
    )
    db.add(session)
    db.flush()
    return session, True


def touch_session_by_user_activity(db: DBSession, session: Session) -> None:
    """使用者開口（發訊或點按鈕）時呼叫。

    同時更新兩個時間，但它們的用途不同，之後會分岔：
      last_message_at   -> 判斷「他還在嗎」，用於靜默判定
      window_expires_at -> 判斷「還能發訊嗎」，用於平台規則

    點擊按鈕只會更新 window_expires_at（見 touch_window_only）。
    """
    now = utcnow()
    session.last_message_at = now
    session.window_expires_at = now + MESSAGING_WINDOW
    db.flush()


def touch_window_only(db: DBSession, session: Session) -> None:
    """使用者點擊按鈕（postback）時呼叫。

    重置 24 小時視窗，但不更新 last_message_at ——
    點按鈕不算「說話」，靜默判定仍應從最後一則訊息起算。
    """
    session.window_expires_at = utcnow() + MESSAGING_WINDOW
    db.flush()


def is_window_open(session: Session) -> bool:
    """檢查是否仍在 24 小時視窗內。任何主動發訊前都必須呼叫。"""
    if session.window_expires_at is None:
        return False
    expires = session.window_expires_at
    if expires.tzinfo is None:  # SQLite 讀回可能沒有時區資訊
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > utcnow()


def mark_invite_skipped(db: DBSession, session: Session, reason: str) -> None:
    """記錄未發送教練報告邀請的原因（M9 task 2.3／2.4）。

    不把 invite_sent 設為 True——那個欄位的語意是「已送出」，
    略過不等於送出。session 已是 ENDED，find_silent_sessions 只挑
    ACTIVE 的場次，不會因為沒設 invite_sent 而被重複掃到。
    """
    session.invite_skip_reason = reason
    db.flush()


def find_silent_sessions(db: DBSession, threshold_seconds: int) -> list[Session]:
    """找出靜默逾門檻且尚未發送邀請的場次。

    由背景輪詢每分鐘呼叫。「靜默」是沒有事情發生，不會觸發任何通知，
    只能定期主動查詢。
    """
    cutoff = utcnow() - timedelta(seconds=threshold_seconds)
    return list(
        db.scalars(
            select(Session).where(
                Session.state == SessionState.ACTIVE,
                Session.invite_sent.is_(False),
                Session.last_message_at < cutoff,
            )
        )
    )


def end_session(db: DBSession, session: Session) -> None:
    session.state = SessionState.ENDED
    session.ended_at = utcnow()
    db.flush()


def get_link_by_session(db: DBSession, session: Session) -> UserPersonaLink | None:
    """依 session 找出對應的使用者與角色連結（task 6.7 用於話題萃取）。

    Session 只存 user_id／persona_id，不存 igsid；一組 user+persona
    對應恰好一筆連結，直接查 (user_id, persona_id) 即可，
    `ix_link_user_persona` 索引已為此建立。
    """
    return db.scalar(
        select(UserPersonaLink).where(
            UserPersonaLink.user_id == session.user_id,
            UserPersonaLink.persona_id == session.persona_id,
        )
    )


def get_user_version(db: DBSession, link_id: int) -> UserPersonaVersion | None:
    """使用者對某個共用角色保存的版本（design.md 決策 13）。沒有則回傳 None。"""
    return db.scalar(
        select(UserPersonaVersion).where(UserPersonaVersion.link_id == link_id)
    )


def save_user_version(db: DBSession, link: UserPersonaLink, card: dict[str, Any]) -> UserPersonaVersion:
    """建立或覆寫使用者版本。只負責寫入，驗證由 app/personas/user_versions.py 做。

    一個連結至多一份：已有就覆寫同一列，不另開新列，
    使「保存後下一則訊息生效」不必處理新舊版本並存的問題。
    """
    version = get_user_version(db, link.id)
    if version is None:
        version = UserPersonaVersion(link_id=link.id, card=card)
        db.add(version)
    else:
        version.card = card
        version.updated_at = utcnow()
    db.flush()
    return version


def get_coaching(db: DBSession, version_id: int) -> PersonaCoaching | None:
    return db.scalar(select(PersonaCoaching).where(PersonaCoaching.version_id == version_id))


def save_coaching(db: DBSession, version: UserPersonaVersion, values: dict[str, Any]) -> PersonaCoaching:
    """建立或覆寫使用者版本的教練設定。values 的鍵對應 PersonaCoaching 的欄位。"""
    coaching = get_coaching(db, version.id)
    if coaching is None:
        coaching = PersonaCoaching(version_id=version.id)
        db.add(coaching)
    for key, value in values.items():
        setattr(coaching, key, value)
    coaching.updated_at = utcnow()
    db.flush()
    return coaching


def delete_user_version(db: DBSession, link_id: int) -> None:
    """刪除使用者版本與其教練設定（先刪教練設定：它以外鍵指向版本）。"""
    version = get_user_version(db, link_id)
    if version is None:
        return
    db.execute(delete(PersonaCoaching).where(PersonaCoaching.version_id == version.id))
    db.delete(version)
    db.flush()


def append_topic_memory(db: DBSession, link: UserPersonaLink, new_topics: list[str]) -> None:
    """把新萃取的話題併入既有記憶，去重且不無限成長（task 6.7）。

    session 結束時呼叫。上限 30 筆——POC 展示規模用不到這麼多，
    設個上限只是避免長期掛著的帳號無限累積。新話題排在後面，
    render_system_prompt 讀取時視為「最近聊過的」優先於舊的。
    """
    existing = list(link.topic_memory or [])
    for topic in new_topics:
        if topic not in existing:
            existing.append(topic)
    link.topic_memory = existing[-30:]
    db.flush()


# ═══════════════════════════════════════════════════════════
#  訊息
# ═══════════════════════════════════════════════════════════

def save_inbound_message(
    db: DBSession,
    *,
    session: Session,
    platform_message_id: str | None,
    content: str,
    meta: dict[str, Any] | None = None,
) -> Message | None:
    """保存使用者訊息。重複事件回傳 None。

    Meta 未即時收到 200 時會重送同一事件，重送帶相同的
    platform_message_id。去重靠資料庫的唯一約束，
    而非程式自行判斷 —— 那樣不會有漏判，也不會有競態。

    platform_message_id 可為 None——postback 事件（M9 教練報告按鈕點擊）
    沒有 Meta 給的訊息識別碼這個概念，此時放棄去重保證（SQLite 的唯一
    約束視多個 NULL 為互不相同，重送的 postback 因此不會被擋下，
    與 _handle_postback 本身自 M1 起就沒有任何去重機制一致，非本次新增風險）。
    """
    message = Message(
        session_id=session.id,
        direction=MessageDirection.INBOUND,
        platform_message_id=platform_message_id,
        content=content,
        meta=meta or {},
    )
    db.add(message)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        log.info(
            "重複事件已擋下，不重複處理",
            extra={"platform_message_id": platform_message_id},
        )
        return None

    session.message_count += 1
    db.flush()
    return message


def save_outbound_message(
    db: DBSession,
    *,
    session: Session,
    content: str,
    platform_message_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Message:
    """保存角色訊息。

    注意：不更新 window_expires_at —— 只有使用者開口才會重置視窗，
    系統自己發訊不會延長時間。
    """
    message = Message(
        session_id=session.id,
        direction=MessageDirection.OUTBOUND,
        platform_message_id=platform_message_id,
        content=content,
        meta=meta or {},
    )
    db.add(message)
    session.message_count += 1
    db.flush()
    return message


def save_undelivered_message(
    db: DBSession,
    *,
    session: Session,
    content: str,
    reason: str,
) -> Message:
    """保存一則「產生了但沒送出去」的角色訊息（tasks 4.3、4.4）。

    仍然落庫而不是直接丟棄，理由有二：
      1. 內容多半是 LLM 生成的，丟掉等於白花一次呼叫成本
      2. 未送達的原因本身就是要觀測的資料——視窗過期了幾次、
         擋掉了幾次主動發訊，是驗證平台規則假設的依據

    以 platform_message_id 為 NULL 且 meta.delivered 為 False 標示。
    不增加 session.message_count：沒送出去的訊息不算在對話則數裡。
    """
    message = Message(
        session_id=session.id,
        direction=MessageDirection.OUTBOUND,
        platform_message_id=None,
        content=content,
        meta={"delivered": False, "reason": reason},
    )
    db.add(message)
    db.flush()
    log.warning("訊息未送達", extra={"session_id": session.id, "reason": reason})
    return message


def record_send_error(
    db: DBSession,
    *,
    persona_id: int,
    session_id: int | None,
    category: str,
    http_status: int | None,
    error_code: int | None,
    error_subcode: int | None,
    detail: str | None,
) -> SendError:
    """記下一次發訊失敗（第 9 群監測）。分類由呼叫端決定（app/channels/send_errors.py）。"""
    row = SendError(
        persona_id=persona_id,
        session_id=session_id,
        category=category,
        http_status=http_status,
        error_code=error_code,
        error_subcode=error_subcode,
        detail=(detail or "")[:500] or None,
    )
    db.add(row)
    db.flush()
    return row


def send_activity_since(db: DBSession, since: datetime) -> dict[datetime, dict[str, int]]:
    """每個整點的發訊量與各類發訊錯誤次數（第 9 群監測），鍵為 UTC 整點。

    發訊量來自 rate_budget，錯誤來自 send_errors；分桶在 Python 端做，
    不依賴 SQLite 與 Postgres 不同的日期函式。所有角色合計。
    """
    hours: dict[datetime, dict[str, int]] = {}

    def bucket(at: datetime) -> dict[str, int]:
        if at.tzinfo is None:  # SQLite 讀回來不帶時區
            at = at.replace(tzinfo=timezone.utc)
        key = at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        return hours.setdefault(key, {"sent": 0})

    for row in db.scalars(select(RateBudget).where(RateBudget.window_start >= since)):
        bucket(row.window_start)["sent"] += row.sent_count
    for row in db.scalars(select(SendError).where(SendError.occurred_at >= since)):
        counts = bucket(row.occurred_at)
        counts[row.category] = counts.get(row.category, 0) + 1
    return dict(sorted(hours.items()))


def recent_send_errors(db: DBSession, limit: int) -> list[SendError]:
    return list(db.scalars(select(SendError).order_by(SendError.id.desc()).limit(limit)))


def bump_rate_budget(
    db: DBSession,
    *,
    persona_id: int,
    sent: int = 0,
    rate_limited: int = 0,
) -> RateBudget:
    """累計發訊量與觸發速率限制的次數（task 4.8）。

    以「整點」為時間窗起點分桶。這是給人看的觀測資料，不是配額判斷的
    依據——實際的配額控制在 app/channels/rate_limit.py 以滾動窗進行，
    兩者刻意分開：整點分桶易讀易查，滾動窗才不會在交界處失準。
    """
    window_start = utcnow().replace(minute=0, second=0, microsecond=0)
    budget = db.scalar(
        select(RateBudget).where(
            RateBudget.persona_id == persona_id,
            RateBudget.window_start == window_start,
        )
    )
    if budget is None:
        budget = RateBudget(persona_id=persona_id, window_start=window_start,
                            sent_count=0, rate_limited_count=0)
        db.add(budget)

    budget.sent_count += sent
    budget.rate_limited_count += rate_limited
    db.flush()
    return budget


def is_system_message(message: Message) -> bool:
    """是否為不屬於聊天內容的系統訊息（見 models.SystemMessageKind）。"""
    return bool((message.meta or {}).get("system"))


def recent_messages(db: DBSession, session_id: int, limit: int) -> list[Message]:
    """取最近 N 則聊天訊息，依時間正序回傳，供組裝對話上下文使用。

    排除系統訊息（「設定」與設定頁連結）：它們不是聊天內容，混進來會讓
    角色、話題萃取與訊號偵測都誤讀。在 Python 端過濾而非 SQL——
    meta 是 JSON 欄位，SQLite 與 Postgres 的 JSON 查詢語法不同；代價是
    回傳則數可能略少於 limit，對上下文視窗無實質影響。
    """
    rows = list(
        db.scalars(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.sent_at.desc(), Message.id.desc())
            .limit(limit)
        )
    )
    return [m for m in reversed(rows) if not is_system_message(m)]


def session_messages_after(
    db: DBSession, session_id: int, after_message_id: int | None
) -> list[Message]:
    """場次中 id 大於 after_message_id 的聊天訊息（不含系統訊息），依 id 正序。

    供長對話摘要（任務 8）使用：摘要以訊息 id 標記涵蓋範圍，這裡也依 id 排序，
    兩者用同一把尺，不會因為兩則訊息時間相同而算錯邊界。
    """
    query = select(Message).where(Message.session_id == session_id)
    if after_message_id is not None:
        query = query.where(Message.id > after_message_id)
    rows = db.scalars(query.order_by(Message.id))
    return [m for m in rows if not is_system_message(m)]


def save_session_summary(
    db: DBSession,
    session_id: int,
    summary: str,
    until_message_id: int,
    expected_until: int | None,
) -> bool:
    """寫入摘要。只有涵蓋範圍仍是 expected_until 時才寫——兩個背景任務同時
    算出摘要時，後到的那個不覆蓋先寫入的（它是以舊的範圍算的）。回傳是否寫入。"""
    session = db.get(Session, session_id)
    if session is None or session.summary_until_message_id != expected_until:
        return False
    session.summary = summary
    session.summary_until_message_id = until_message_id
    db.flush()
    return True


def previous_sessions_messages(db: DBSession, session: Session, limit: int) -> list[Message]:
    """同一組使用者＋角色、比目前場次更早的最後 N 則訊息，依時間正序回傳。

    跨場次記憶的第二層：話題萃取（6.7）只留 10 字內的話題關鍵字，
    「主管姓白」這類細節會丟失。新場次的對話上下文又只讀自己這個場次，
    使用者隔一段時間回來就等於失憶。這裡直接撈舊訊息補上，不經過 LLM。
    """
    rows = list(
        db.scalars(
            select(Message)
            .join(Session, Message.session_id == Session.id)
            .where(
                Session.user_id == session.user_id,
                Session.persona_id == session.persona_id,
                Session.id < session.id,
                Message.content != "",
            )
            .order_by(Message.sent_at.desc(), Message.id.desc())
            .limit(limit)
        )
    )
    return [m for m in reversed(rows) if not is_system_message(m)]


def session_number(db: DBSession, session: Session) -> int:
    """這是同一組使用者＋角色的第幾次對話（從 1 起算）。

    task 8.2 的 L3「前三次對話就約見面」用這個判斷時機。
    """
    return db.scalar(
        select(func.count(Session.id)).where(
            Session.user_id == session.user_id,
            Session.persona_id == session.persona_id,
            Session.id <= session.id,
        )
    ) or 1


def has_newer_inbound(db: DBSession, session_id: int, since_message_id: int) -> bool:
    """該場次是否存在 id 大於 since_message_id 的入站訊息。

    task 7.9：使用者在擬真延遲等待期間又發訊，代表準備中的回覆已經過時。
    用資料庫查詢判斷「是否過時」，而非用 asyncio.Task.cancel() 真的中止
    背景任務——後者需要能跨請求取消任務的排程機制，FastAPI 的
    BackgroundTasks 並不提供，換掉整個排程方式牽動既有測試依賴的
    「背景任務在回應前跑完」特性，代價過高。這裡改成「送出前檢查一次
    是否已過時」，新訊息自己觸發的下一次回覆會經由 recent_messages()
    自然帶入這則被跳過的訊息，等同把兩則話合併進同一次理解，不需要
    真的合併文字內容。

    系統訊息不算：使用者在晴晴「輸入中」時傳「設定」，不該讓準備中的
    回覆被丟棄——「設定」本身不會觸發一般回覆，丟掉就再也沒人回他上一句。
    """
    newer = db.scalars(
        select(Message).where(
            Message.session_id == session_id,
            Message.direction == MessageDirection.INBOUND,
            Message.id > since_message_id,
        )
    )
    return any(not is_system_message(m) for m in newer)


def has_any_inbound(db: DBSession, session_id: int) -> bool:
    """該場次是否有過使用者訊息。

    Meta 禁止對從未發訊的使用者主動發訊，發訊前需檢查。
    """
    return db.scalar(
        select(Message.id).where(
            Message.session_id == session_id,
            Message.direction == MessageDirection.INBOUND,
        ).limit(1)
    ) is not None


# ═══════════════════════════════════════════════════════════
#  即時訊號（task 8.3）
# ═══════════════════════════════════════════════════════════

def save_signal(
    db: DBSession,
    *,
    session_id: int,
    message_id: int,
    signal_type: str,
    severity: str | None = None,
    dimension: str | None = None,
    reason: str | None = None,
) -> RealtimeSignal:
    signal = RealtimeSignal(
        session_id=session_id,
        message_id=message_id,
        signal_type=signal_type,
        severity=severity,
        dimension=dimension,
        reason=reason,
    )
    db.add(signal)
    db.flush()
    return signal


def replace_session_signals(
    db: DBSession, session_id: int, signals: list[dict[str, Any]]
) -> list[RealtimeSignal]:
    """以這次偵測結果取代該場次的全部訊號。

    先刪後寫：同一場次重跑偵測（task 8.4 的事後重跑）時訊號不會重複。
    signals 每筆需有 message_id、signal_type，可選 severity、dimension、reason。
    """
    db.execute(delete(RealtimeSignal).where(RealtimeSignal.session_id == session_id))
    return [save_signal(db, session_id=session_id, **s) for s in signals]


def signals_for_session(db: DBSession, session_id: int) -> list[RealtimeSignal]:
    """取出某場次的所有訊號，供 M5 生成教練報告。"""
    return list(
        db.scalars(
            select(RealtimeSignal)
            .where(RealtimeSignal.session_id == session_id)
            .order_by(RealtimeSignal.detected_at)
        )
    )


def set_signal_status(
    db: DBSession, session_id: int, status: str, error: str | None = None
) -> None:
    """記錄某場次的訊號偵測結果（task 8.4）。成功時清掉舊的錯誤原因。"""
    session = db.get(Session, session_id)
    if session is None:
        return
    session.signal_status = status
    session.signal_error = error if status == SignalStatus.FAILED else None
    db.flush()


def failed_signal_session_ids(db: DBSession) -> list[int]:
    """偵測失敗、待重跑的場次，供 scripts/rerun_signals.py 使用。"""
    return list(
        db.scalars(
            select(Session.id)
            .where(Session.signal_status == SignalStatus.FAILED)
            .order_by(Session.id)
        )
    )


# ═══════════════════════════════════════════════════════════
#  教練報告（M5／M9，tasks 1.2、1.3）
# ═══════════════════════════════════════════════════════════

def get_coach_report(db: DBSession, session_id: int) -> CoachReport | None:
    return db.scalar(select(CoachReport).where(CoachReport.session_id == session_id))


def get_or_create_coach_report(db: DBSession, session_id: int) -> CoachReport:
    """取得該 session 的報告列，沒有就建立空白的一筆。

    「一場對話一份報告」不是靠資料庫唯一約束保證，而是靠呼叫端一律
    先查後建（同一模式見 get_or_create_session）——這個產品是私訊場景，
    非高併發，與其他既有的 get_or_create 系列函式一致不額外加鎖。
    """
    report = get_coach_report(db, session_id)
    if report is None:
        report = CoachReport(session_id=session_id)
        db.add(report)
        db.flush()
    return report


def mark_report_generated(db: DBSession, report: CoachReport, content: dict[str, Any]) -> None:
    """生成成功：寫入內容並清空失敗原因（若有）。"""
    report.content = content
    report.generated_at = utcnow()
    report.failure_reason = None
    db.flush()


def mark_report_delivered(db: DBSession, report: CoachReport) -> None:
    report.delivered_at = utcnow()
    db.flush()


def mark_report_failed(db: DBSession, report: CoachReport, reason: str) -> None:
    """生成失敗（或訊號未就緒）：只記原因，不動 content——維持「不留半成品」。"""
    report.failure_reason = reason
    db.flush()


def failed_coach_report_session_ids(db: DBSession) -> list[int]:
    """生成失敗（含訊號未就緒）、待重跑的場次，供 scripts/rerun_coach_reports.py 使用。

    與 failed_signal_session_ids 同構：generated_at 為 NULL 且曾記錄過
    失敗原因，代表這筆報告從未成功生成過。
    """
    return list(
        db.scalars(
            select(CoachReport.session_id)
            .where(
                CoachReport.generated_at.is_(None),
                CoachReport.failure_reason.is_not(None),
            )
            .order_by(CoachReport.session_id)
        )
    )
