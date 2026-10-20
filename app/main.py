"""FastAPI 應用入口。

POC 階段：健康檢查、webhook 接收（tasks 3.1–3.7）、使用者身分歸戶
（task 6.1）、AI 身分揭露（task 7.5）、角色回覆生成（task 7.6，含邊界
反應 7.7）、擬真節奏（task 7.8）、延遲期間插話的過時檢查（task 7.9）、
靜默結束的背景輪詢、話題萃取與訊號偵測（tasks 6.4、6.7、8.2）。

回覆生成串接：recent_messages（既有）→ build_context（7.6）→
render_system_prompt（既有，7.3；回暖段落為 7.7 補上；known_topics
參數為 6.7 補上）→ llm_router（既有，第 5 群）→ send_paced_reply
（既有，7.8，帶 7.9 的 is_stale）。

背景輪詢串接：_silence_poll_loop 定期呼叫 repository.find_silent_sessions
（既有）→ end_session（既有）→ app/dialog/topics.py 的 extract_topics
（6.7 新增，走 analysis 路徑）→ repository.append_topic_memory（6.7 新增）
→ app/dialog/signal_job.py 的 run_signal_detection（8.2–8.4：偵測、落庫、
記錄成功／失敗狀態；失敗的場次以 scripts/rerun_signals.py 手動重跑）。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.channels.instagram import (
    InstagramClient,
    parse_webhook_payload,
    verify_signature,
    verify_subscription,
)
from app.channels.rate_limit import RateLimiter
from app.channels.sender import MessageSender
from app.config import get_settings
from app.db import repository as repo
from app.db.models import MessageDirection, Persona
from app.db.models import Session as SessionModel
from app.auth.settings_link import build_settings_url, issue_token
from app.db.models import SessionState, SignalStatus, SystemMessageKind, UserPersonaLink, utcnow
from app.db.session import check_connection, db_session, init_db
from app.dialog.coach_report import (
    INVITE_BUTTON_TITLE,
    INVITE_MESSAGE_TEXT,
    build_invite_buttons,
    format_report_text,
    generate_report,
    parse_invite_payload,
)
from app.dialog.context import build_context, format_previous_conversation
from app.dialog.summary import update_session_summary
from app.dialog.delivery import send_paced_reply
from app.dialog.signal_job import run_signal_detection
from app.dialog.topics import extract_topics
from app.llm import LLMConfigError, LLMMessage, LLMRouter, build_router, pick_fallback
from app.logging_config import get_logger, setup_logging
from app.personas.coaching_settings import session_coaching
from app.personas.disclosure import compose_reply, should_disclose
from app.personas.loader import PersonaCardError, load_all_cards
from app.personas.prompt import render_system_prompt
from app.personas.schema import PersonaCard
from app.personas.user_versions import resolve_card
from app.web.backoffice import build_router as build_backoffice_router
from app.web.settings_api import FRONTEND_DIST as SETTINGS_FRONTEND_DIST
from app.web.settings_api import build_router as build_settings_router

settings = get_settings()
setup_logging(settings.log_level, settings.environment)
log = get_logger(__name__)

# 行程層級單例：配額必須跨請求累計，每次發訊各自建一個就等於沒有限制。
rate_limiter = RateLimiter(settings.send_rate_limit_per_hour)

# slug -> PersonaCard，於 lifespan 啟動時載入一次。
# 純檔案讀取，不依賴 Meta 憑證，因此不受 missing_platform_credentials 影響。
persona_cards: dict[str, PersonaCard] = {}

# LLM 路由器。行程層級單例——供應商實例持有 httpx 連線池，
# 每次呼叫各建一個等於放棄連線重用。缺金鑰時維持 None，
# 屆時對話生成走降級回應（見 llm/fallback.py），其餘功能不受影響。
llm_router: LLMRouter | None = None

# 靜默輪詢背景任務的 handle（task 6.4）。lifespan 啟動時建立、
# 關閉時取消——與 llm_router 一樣是行程層級單例。
silence_poll_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info(
        "服務啟動",
        extra={"environment": settings.environment, "database": settings.database_url_for_log},
    )

    init_db()

    # 7.2：人設卡載入失敗就讓服務啟動失敗，不悄悄跳過壞掉的角色
    # （見 app/personas/loader.py 的設計說明）。純檔案讀取，與 Meta
    # 憑證是否齊全無關，因此擺在憑證檢查之前。
    try:
        persona_cards.update(load_all_cards())
    except PersonaCardError as exc:
        log.error("人設卡載入失敗，服務無法啟動", extra={"error": str(exc)})
        raise
    log.info("人設卡已載入", extra={"slugs": sorted(persona_cards)})

    if settings.skip_signature_check:
        log.warning(
            "⚠⚠ webhook 簽章驗證已停用——任何知道此網址的人都能偽造訊息。"
            "僅限本機開發，正式環境請移除 WEBHOOK_SKIP_SIGNATURE_CHECK"
        )

    if not settings.settings_link_enabled:
        log.warning(
            "設定頁連結功能未啟用：SETTINGS_LINK_SECRET（至少 32 字元）或 PUBLIC_BASE_URL "
            "未設定，使用者傳設定關鍵字會被當成一般訊息回覆",
            extra={"keyword": settings.settings_keyword},
        )

    if not settings.ig_app_secret:
        # 退回 META_APP_SECRET 必然驗章失敗（見 config.webhook_signing_secret），
        # 明講出來，避免又花一輪時間追「設定看起來都對卻收不到訊息」。
        log.warning(
            "IG_APP_SECRET 未設定，將退回 META_APP_SECRET——webhook 驗章會失敗。"
            "請至 Meta 後台 Instagram 產品頁取得「Instagram 應用程式密鑰」"
        )

    missing = settings.missing_platform_credentials()
    if missing:
        log.warning(
            "平台憑證未填，僅能以假 payload 測試，無法接收真實 Instagram 流量",
            extra={"missing": missing, "guide": "docs/開發環境設定.md"},
        )
    else:
        # 憑證齊全時 seed 晴晴的角色資料，讓 3.4 角色路由找得到人。
        # 完整的人設卡 YAML 載入是 M2 的工作（見 app/personas/），
        # 此處僅寫入路由所需的最小欄位。
        with db_session() as db:
            persona = repo.upsert_persona(
                db,
                slug="sunny",
                name="晴晴",
                gender="female",
                ig_account_id=settings.ig_account_id,
            )
            log.info("角色已就緒", extra={"slug": persona.slug, "ig_account_id": persona.ig_account_id})

    # 5.2：供應商設定驗證。指向未實作的供應商會在 settings.llm_paths
    # 讀取時就拋錯（見 config.LLMPathConfig），走不到這裡；此處處理的是
    # 缺金鑰。缺金鑰不阻擋啟動——webhook、人設卡、發訊管線都還能測，
    # 只有對話生成不能用，那時會走降級回應而非拋錯給使用者。
    global llm_router

    missing_keys = settings.missing_llm_keys()
    if missing_keys:
        log.warning(
            "LLM 金鑰未填，對話生成將一律走降級回應",
            extra={"missing": missing_keys, "guide": "docs/開發環境設定.md"},
        )
    else:
        try:
            llm_router = build_router(
                settings.llm_path_configs,
                api_keys=settings.llm_api_keys,
                timeout=float(settings.llm_timeout_seconds),
                max_retries=settings.llm_max_retries,
                retry_base_seconds=settings.llm_retry_base_seconds,
                fallback_path_configs=settings.llm_fallback_path_configs,
            )
        except LLMConfigError as exc:
            # 金鑰齊全卻仍建不起來，代表設定真的有問題（而非只是還沒填）。
            # 這種狀況不該讓服務帶病啟動——錯誤訊息已指出該改哪個環境變數。
            log.error("LLM 供應層建構失敗，服務無法啟動", extra={"error": str(exc)})
            raise

    fallback_configs = settings.llm_fallback_path_configs
    for path, cfg in settings.llm_paths.items():
        fallback_cfg = fallback_configs.get(path)
        log.info(
            "LLM 路徑就緒",
            extra={
                "path": path,
                "provider": cfg.provider,
                "model": cfg.model,
                "fallback_model": fallback_cfg.model if fallback_cfg else None,
                "usable": llm_router is not None,
            },
        )

    # 6.4：靜默輪詢背景任務。與 llm_router 一樣是行程層級單例，
    # 用 asyncio.create_task 而非 BackgroundTasks——它不屬於任何一次
    # webhook 請求，是獨立於請求週期之外、持續運行到服務關閉的常駐任務。
    global silence_poll_task
    silence_poll_task = asyncio.create_task(_silence_poll_loop())

    yield

    # 先取消輪詢任務再關 llm_router：萃取話題的呼叫用的就是 llm_router，
    # 順序反過來的話，輪詢任務可能在供應商已關閉連線後才嘗試呼叫。
    silence_poll_task.cancel()
    try:
        await silence_poll_task
    except asyncio.CancelledError:
        pass

    if llm_router is not None:
        await llm_router.aclose()

    log.info("服務關閉")


app = FastAPI(
    title="IG AI 戀愛顧問",
    description="Instagram 私訊上的 AI 對話練習夥伴與教練",
    version="0.1.0-poc",
    lifespan=lifespan,
    # 關閉自動產生的 API 文件（/docs、/redoc、/openapi.json）：服務經 ngrok
    # 對外公開，文件會列出所有路徑，包括刻意對非管理員回 404 的管理員頁面
    # （add-persona-authoring-console 任務 5.3）
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# 任務 5.2、5.3、5.6：後台登入、管理員頁面、客戶頁面
app.include_router(build_backoffice_router())

# 第 6 群：使用者設定頁（API 與 React 打包後的頁面）
app.include_router(build_settings_router(lambda slug: persona_cards.get(slug)))
if (SETTINGS_FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=SETTINGS_FRONTEND_DIST / "assets"), name="assets")


@app.get("/health")
async def health() -> JSONResponse:
    """健康檢查。實際查詢一次資料庫，連不上回 503。

    compose 的 app 健康檢查用的就是這個端點——只回報設定值的話，
    資料庫掛掉時容器仍會顯示健康。
    """
    db_ok = check_connection()
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "status": "ok" if db_ok else "degraded",
            "environment": settings.environment,
            "database": "sqlite" if settings.is_sqlite else "postgres",
            "database_ok": db_ok,
            "platform_credentials_ready": not settings.missing_platform_credentials(),
        },
    )


# ═══════════════════════════════════════════════════════════
#  3.1 webhook 訂閱驗證
# ═══════════════════════════════════════════════════════════

@app.get("/webhook")
async def webhook_verify(request: Request) -> Response:
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge", "")

    if verify_subscription(mode, token, settings.meta_webhook_verify_token):
        log.info("webhook 訂閱驗證成功")
        return Response(content=challenge, media_type="text/plain")

    log.warning("webhook 訂閱驗證失敗", extra={"mode": mode})
    return Response(status_code=403)


# ═══════════════════════════════════════════════════════════
#  3.2–3.7 webhook 事件接收
# ═══════════════════════════════════════════════════════════

@app.post("/webhook")
async def webhook_receive(request: Request, background_tasks: BackgroundTasks) -> Response:
    raw_body = await request.body()

    # 3.2 簽章驗證：失敗回 403，不進行任何後續處理
    signature = request.headers.get("X-Hub-Signature-256")
    if settings.skip_signature_check:
        # 開發用逃生門（見 config.skip_signature_check）。每一筆都記錄，
        # 讓「驗章目前是關的」這件事無法被忽略。
        if not verify_signature(raw_body, signature, settings.webhook_signing_secret):
            log.warning("⚠ 簽章不符但已略過（WEBHOOK_SKIP_SIGNATURE_CHECK 開啟中）")
    elif not verify_signature(raw_body, signature, settings.webhook_signing_secret):
        log.warning("簽章驗證失敗，拒絕請求")
        return Response(status_code=403)

    body = await request.json()

    # 3.3 事件解析（3.5：單一請求含多筆事件，逐筆處理，任一筆失敗不影響其餘）
    events = parse_webhook_payload(body)

    for event in events:
        if event is None:
            continue  # 格式不符的單筆事件已在解析階段記錄警告，此處略過
        try:
            await _handle_event(event, background_tasks)
        except Exception:
            log.exception("處理單筆事件時發生例外，略過此筆", extra={"event": event})

    # 3.7：先回應 200，回覆在背景任務中處理
    return Response(status_code=200)


async def _handle_event(event: dict, background_tasks: BackgroundTasks) -> None:
    with db_session() as db:
        # 3.4 角色路由：未知帳號記錄警告並略過
        persona = repo.get_persona_by_ig_account(db, event["entry_id"])
        if persona is None:
            log.warning("未知角色帳號，略過此事件", extra={"entry_id": event["entry_id"]})
            return

        if event["kind"] == "message":
            await _handle_message(db, persona.id, persona.slug, event, background_tasks)
        elif event["kind"] == "postback":
            _handle_postback(db, persona.id, event, background_tasks)


async def _handle_message(
    db, persona_id: int, persona_slug: str, event: dict, background_tasks: BackgroundTasks
) -> None:
    igsid = event["igsid"]

    existing = repo.find_user_by_igsid(db, persona_id, igsid)
    if existing is not None:
        user, link = existing
    else:
        # 6.1：只有真正第一次見面的使用者才付這次網路延遲，已有連結的
        # 使用者不必每則訊息都打 API。best-effort：fetch_username 內部
        # 已把失敗吞掉回傳 None，resolve_user 支援 None 的降級路徑（6.2），
        # 不會讓一次查詢失敗擋住整個對話。
        #
        # 在既有的 db_session 交易內等待這次網路呼叫，理論上會讓交易
        # 稍微變長；但這只發生在「真正的新使用者」這個稀有事件上，
        # 而 db/session.py 已開啟 WAL 模式讓讀寫並行，可接受。
        client = InstagramClient(settings.ig_access_token, settings.ig_account_id)
        try:
            ig_username = await client.fetch_username(igsid)
        finally:
            await client.aclose()

        user, link, _is_new = repo.resolve_user(
            db, persona_id=persona_id, igsid=igsid, ig_username=ig_username
        )

    session, is_new_session = repo.get_or_create_session(db, user.id, persona_id)

    # 7.5：揭露判斷必須用「這次活動之前」的最後互動時間，所以要在
    # touch_session_by_user_activity 覆寫它之前先讀出來。新場次的
    # last_message_at 就是建立當下的時間，gap 必然是 0，但這裡仍靠
    # is_new_session 直接判定，不依賴時間精度。
    prior_last_message_at = session.last_message_at
    disclose = should_disclose(
        is_new_session=is_new_session,
        last_activity_at=prior_last_message_at,
        now=utcnow(),
        silence_threshold_seconds=settings.silence_threshold_seconds,
    )

    repo.touch_session_by_user_activity(db, session)

    # 任務 5.1：「設定」不是聊天內容。照常落庫（去重、視窗、互動紀錄都要），
    # 但標記為系統訊息，不進入對話上下文與訊號偵測（design.md 決策 14）。
    settings_request = _is_settings_request(event["text"])

    # 3.6 去重：重複的 platform_message_id 回傳 None，直接不處理
    message = repo.save_inbound_message(
        db,
        session=session,
        platform_message_id=event["platform_message_id"],
        content=event["text"],
        meta={"system": SystemMessageKind.SETTINGS_LINK} if settings_request else None,
    )
    if message is None:
        return  # 重複事件，已記錄於 repository 層

    if settings_request:
        log.info("收到設定頁請求", extra={"session_id": session.id, "link_id": link.id})
        background_tasks.add_task(
            _send_settings_link, session.id, persona_id, persona_slug, igsid, link.id, disclose
        )
        return

    log.info(
        "收到訊息",
        extra={
            "session_id": session.id,
            "igsid": igsid,
            "text_len": len(event["text"]),
            "disclose": disclose,
        },
    )

    # 3.7 背景處理回覆：不阻塞 webhook 的 200 回應
    # message.id 隨身攜帶，供 7.9 判斷「送出當下這則是否已經過時」。
    background_tasks.add_task(
        _send_reply, session.id, persona_id, persona_slug, igsid, disclose, message.id
    )


def _is_settings_request(text: str) -> bool:
    """這則訊息是否在要設定頁連結。功能未啟用時一律 False，當一般訊息處理。"""
    return settings.settings_link_enabled and text.strip() == settings.settings_keyword


def settings_hint_text() -> str:
    """任務 5.5：揭露時告訴使用者可以傳關鍵字調整角色（2026-10-14 確認文案）。"""
    return f"想調整我的個性，隨時傳「{settings.settings_keyword}」給我就好"


def settings_link_text(url: str) -> str:
    minutes = settings.settings_link_ttl_seconds // 60
    return (
        "這是你專屬的設定連結，可以調整我的個性，還有聊完之後的回饋方式：\n"
        f"{url}\n"
        f"（{minutes} 分鐘內有效，只能在你打開它的那個裝置上使用，請不要分享給別人）"
    )


async def _send_settings_link(
    session_id: int,
    persona_id: int,
    persona_slug: str,
    igsid: str,
    link_id: int,
    disclose: bool,
) -> None:
    """回覆使用者專屬、有時效的設定頁連結（任務 5.1，design.md 決策 13、14）。

    不經過 LLM、不走擬真節奏：這是系統功能的回覆，不是角色在聊天。
    新對話中「設定」可能就是第一則訊息，因此照樣附上 AI 揭露。
    """
    token = issue_token(
        link_id,
        secret=settings.settings_link_secret,
        ttl_seconds=settings.settings_link_ttl_seconds,
    )
    card = persona_cards.get(persona_slug)
    persona_name = card.identity.name if card else persona_slug
    text = compose_reply(
        settings_link_text(build_settings_url(settings.public_base_url, token)),
        disclose=disclose,
        persona_name=persona_name,
    )

    client = InstagramClient(settings.ig_access_token, settings.ig_account_id)
    sender = MessageSender(
        client,
        rate_limiter,
        max_retries=settings.send_max_retries,
        retry_base_seconds=settings.send_retry_base_seconds,
        max_wait_seconds=settings.send_max_wait_seconds,
    )
    try:
        outcome = await sender.send_reply(
            session_id=session_id,
            persona_id=persona_id,
            igsid=igsid,
            text=text,
            meta={"system": SystemMessageKind.SETTINGS_LINK},
        )
    finally:
        await client.aclose()

    # 不記錄連結本身：token 在有效期內就是憑證
    log.info(
        "設定頁連結處理完成",
        extra={"session_id": session_id, "sent": outcome.ok, "reason": outcome.blocked_reason},
    )


def _handle_postback(
    db, persona_id: int, event: dict, background_tasks: BackgroundTasks
) -> None:
    igsid = event["igsid"]
    existing = repo.find_user_by_igsid(db, persona_id, igsid)
    if existing is None:
        log.warning("postback 來自未知使用者，略過", extra={"igsid": igsid})
        return

    user, link = existing
    session, _ = repo.get_or_create_session(db, user.id, persona_id)
    repo.touch_window_only(db, session)  # 點按鈕只重置視窗，不算「說話」
    log.info("收到 postback", extra={"session_id": session.id, "payload": event["payload"]})

    # M9 task 4.1：教練報告邀請的按鈕是 Button Template（type="postback"），
    # 點擊觸發的正是這個 messaging_postbacks 事件——2026-09-23 真機驗證
    # 確認（見 app/channels/instagram.py 的 send_button_template 說明）。
    report_session_id = parse_invite_payload(event["payload"])
    if report_session_id is not None:
        log.info(
            "使用者點擊教練報告邀請",
            extra={"session_id": session.id, "report_session_id": report_session_id},
        )
        # 2026-09-23 真機測試發現的 bug：postback 從不落庫任何訊息，
        # 點擊建立的這個新 session 因此對 MessageSender 的「未互動使用者
        # 阻擋」（4.4）而言形同陌生人，報告會被判定 no_inbound_interaction
        # 而送不出去。補記一筆入站訊息，內容用按鈕文字——語意上使用者
        # 確實「說」了這句話，讓這個 session 符合既有的發訊前置條件，
        # 不需要放寬 MessageSender 的共用邏輯（那會影響其他所有發訊路徑）。
        repo.save_inbound_message(
            db,
            session=session,
            platform_message_id=None,
            content=INVITE_BUTTON_TITLE,
        )
        background_tasks.add_task(_handle_coach_report_click, report_session_id, session.id, igsid)


# ═══════════════════════════════════════════════════════════
#  6.4 靜默輪詢 ／ 6.7 話題萃取 ／ 8.2 訊號偵測
# ═══════════════════════════════════════════════════════════

async def _silence_poll_loop() -> None:
    """背景輪詢迴圈（task 6.4）。lifespan 啟動時建立、關閉時取消。

    先 sleep 再掃描，不是掃描再 sleep——服務剛啟動時不需要立刻掃一次
    （不可能有 session 已經靜默超過門檻），這樣寫也讓測試能直接呼叫
    `_run_silence_pass()` 驗證單次掃描的行為，不需要真的等一個間隔。
    """
    while True:
        await asyncio.sleep(settings.silence_poll_interval_seconds)
        try:
            await _run_silence_pass()
        except Exception:
            # 這是常駐背景任務，單次掃描出錯不該讓整個輪詢迴圈死掉——
            # 下一輪還是要繼續掃，否則往後所有 session 都不會被結束。
            log.exception("靜默輪詢單次掃描發生例外，等下一輪繼續")


async def _run_silence_pass() -> None:
    """單次靜默掃描：找出逾門檻的 session、標記結束、萃取話題。

    抽成獨立函式而非寫在迴圈裡，是為了讓測試能直接呼叫一次，
    不必真的等 `silence_poll_interval_seconds`。
    """
    with db_session() as db:
        silent = repo.find_silent_sessions(db, settings.silence_threshold_seconds)
        session_ids = [s.id for s in silent]

    for session_id in session_ids:
        # task 8.4：每場各自隔離，一場出錯不讓同一輪後面的場次被跳過
        try:
            await _close_session_and_analyze(session_id)
        except Exception:
            log.exception("結束場次時發生例外，繼續處理下一場", extra={"session_id": session_id})


async def _close_session_and_analyze(session_id: int) -> None:
    """結束單一 session，萃取話題存進對應的使用者連結（task 6.7），
    再對整段對話偵測訊號（task 8.2，時機見 design.md 決策 6）。

    分兩段各自開短生命週期的 db_session：中間夾著一次 LLM 呼叫，
    不該讓資料庫連線在等待網路 I/O 時一直被佔用著。
    """
    with db_session() as db:
        session = db.get(SessionModel, session_id)
        if session is None or session.state != SessionState.ACTIVE:
            # 已被處理過（理論上輪詢掃描不會重複挑到同一筆，但用
            # 這個檢查擋住任何競態，好過重複萃取或萃取到不存在的 session）
            return

        # 整段對話都要看，不能只用 dialog_context_message_count 的視窗——
        # 那是給「這次回覆要參考多少上下文」用的，話題萃取要看的是
        # 這個 session 從頭到尾聊了什麼。200 則遠超過 POC 展示規模，
        # 只是給個實務上限，不必無上限查詢。
        history = repo.recent_messages(db, session_id, 200)
        link = repo.get_link_by_session(db, session)
        persona_id = session.persona_id
        repo.end_session(db, session)

    if link is None or not history:
        return

    inbound_count = sum(1 for m in history if m.direction == MessageDirection.INBOUND)

    if llm_router is not None:
        context = build_context(history)
        topics = await extract_topics(llm_router, context)
        if topics:
            with db_session() as db:
                fresh_link = db.get(UserPersonaLink, link.id)
                if fresh_link is not None:
                    repo.append_topic_memory(db, fresh_link, topics)
                    log.info(
                        "話題萃取完成",
                        extra={"session_id": session_id, "link_id": link.id, "topics": topics},
                    )

        await run_signal_detection(
            llm_router,
            session_id,
            persona_cards,
            burst_window_seconds=settings.signal_burst_window_seconds,
            burst_min_messages=settings.signal_burst_min_messages,
        )

    # M9 tasks 2.2–2.4：話題萃取、訊號偵測都做完才發邀請——不是因為邀請
    # 依賴它們的結果，是讓這輪背景工作的順序單純（結束 → 分析 → 邀請），
    # 出錯時看 log 容易理解先後。
    await _send_coach_invite(session_id, persona_id, link.igsid, inbound_count)


async def _send_coach_invite(
    session_id: int, persona_id: int, igsid: str, inbound_count: int
) -> None:
    """靜默結束後發送教練報告邀請（M9 tasks 2.2–2.4）。

    互動過少不邀請（2.3）；視窗逾期不硬送（2.4，交給 MessageSender 的
    既有前置檢查處理，不在這裡重複判斷——design.md 決策 6）。
    """
    if inbound_count < settings.coach_report_min_user_messages:
        with db_session() as db:
            session = db.get(SessionModel, session_id)
            if session is not None:
                repo.mark_invite_skipped(db, session, reason="too_few_messages")
        log.info(
            "互動過少，不發送教練報告邀請",
            extra={"session_id": session_id, "inbound_count": inbound_count},
        )
        return

    client = InstagramClient(settings.ig_access_token, settings.ig_account_id)
    sender = MessageSender(
        client,
        rate_limiter,
        max_retries=settings.send_max_retries,
        retry_base_seconds=settings.send_retry_base_seconds,
        max_wait_seconds=settings.send_max_wait_seconds,
    )
    try:
        outcome = await sender.send_button_template(
            session_id=session_id,
            persona_id=persona_id,
            igsid=igsid,
            text=INVITE_MESSAGE_TEXT,
            buttons=build_invite_buttons(session_id),
        )
    finally:
        await client.aclose()

    with db_session() as db:
        session = db.get(SessionModel, session_id)
        if session is None:
            return
        if outcome.ok:
            session.invite_sent = True
            db.flush()
        else:
            repo.mark_invite_skipped(db, session, reason=outcome.blocked_reason or "send_failed")

    log.info(
        "教練報告邀請處理完成",
        extra={"session_id": session_id, "sent": outcome.ok, "reason": outcome.blocked_reason},
    )


async def _handle_coach_report_click(
    report_session_id: int, delivery_session_id: int, igsid: str
) -> None:
    """使用者點擊教練報告邀請後的處理（M9 tasks 4.1、4.2）。

    report_session_id 是邀請所屬的那場已結束對話（報告與訊號都掛在它上面）；
    delivery_session_id 是點擊當下的場次（get_or_create_session 在
    _handle_message 已建立或延續），送達要用它的視窗——report_session_id
    的視窗早在對話靜默時就過期了，那正是它會靜默結束、觸發邀請的原因，
    拿它做視窗檢查一定會誤判成逾期。
    """
    with db_session() as db:
        report_session = db.get(SessionModel, report_session_id)
    if report_session is None:
        log.warning("教練報告對應的 session 不存在", extra={"session_id": report_session_id})
        return

    with db_session() as db:
        report = repo.get_or_create_coach_report(db, report_session_id)
        already_generated = report.generated_at is not None

    if not already_generated:
        # task 3.4：訊號未就緒或偵測失敗，不以不完整的訊號生成。
        # 記一個原因方便 scripts/rerun_coach_reports.py 之後重試
        # （task 5.2），不需要使用者再點一次可能已經消失的按鈕。
        if report_session.signal_status != SignalStatus.OK:
            with db_session() as db:
                report = repo.get_or_create_coach_report(db, report_session_id)
                repo.mark_report_failed(db, report, "signals_not_ready")
            log.info(
                "訊號尚未就緒，暫不生成教練報告",
                extra={
                    "session_id": report_session_id,
                    "signal_status": report_session.signal_status,
                },
            )
            return

        if llm_router is None:
            log.warning("LLM 未設定，無法生成教練報告", extra={"session_id": report_session_id})
            return

        with db_session() as db:
            signals = repo.signals_for_session(db, report_session_id)
            persona = db.get(Persona, report_session.persona_id)
            persona_name = persona.name if persona else "角色"
            coaching = session_coaching(db, report_session)

        result = await generate_report(llm_router, signals, persona_name, coaching)
        with db_session() as db:
            report = repo.get_or_create_coach_report(db, report_session_id)
            if result.ok and result.content is not None:
                repo.mark_report_generated(db, report, result.content)
            else:
                repo.mark_report_failed(db, report, result.error or "unknown")
                log.warning(
                    "教練報告生成失敗",
                    extra={"session_id": report_session_id, "error": result.error},
                )
                return

    await _deliver_coach_report(report_session_id, delivery_session_id, igsid)


async def _deliver_coach_report(
    report_session_id: int, delivery_session_id: int, igsid: str
) -> None:
    """送出已生成的教練報告（task 4.3、4.4）。

    delivery_session_id 決定視窗與未互動檢查用哪個 session；報告內容與
    「是否已送達」的狀態則一律讀寫 report_session_id 底下那筆
    coach_reports——同一份報告不會因為從哪個場次觸發送達而變成兩筆。

    補救路徑（task 5.1）只由使用者再次點擊查看回饋觸發（見
    _handle_coach_report_click 的 already_generated 分支），不會在使用者
    傳送其他不相關訊息時自動補送——2026-09-24 產品決策：報告與當下對話
    無關時突然送出會讓使用者一頭霧水，不記得自己先前點過什麼。
    """
    with db_session() as db:
        report = repo.get_coach_report(db, report_session_id)
        delivery_session = db.get(SessionModel, delivery_session_id)

    if report is None or report.generated_at is None:
        return
    if report.delivered_at is not None:
        return  # 已送達。目前重複點擊直接不回應（見 4.2 的既有規則），維持原狀不動
    if delivery_session is None:
        return

    text = format_report_text(report.content or {})
    if not text:
        return

    client = InstagramClient(settings.ig_access_token, settings.ig_account_id)
    sender = MessageSender(
        client,
        rate_limiter,
        max_retries=settings.send_max_retries,
        retry_base_seconds=settings.send_retry_base_seconds,
        max_wait_seconds=settings.send_max_wait_seconds,
    )
    try:
        outcome = await sender.send_reply(
            session_id=delivery_session_id,
            persona_id=delivery_session.persona_id,
            igsid=igsid,
            text=text,
        )
    finally:
        await client.aclose()

    if outcome.ok:
        with db_session() as db:
            fresh_report = repo.get_coach_report(db, report_session_id)
            if fresh_report is not None:
                repo.mark_report_delivered(db, fresh_report)
        log.info("教練報告已送達", extra={"session_id": report_session_id})
    else:
        # 保留為「已生成未送達」，不動 content——使用者之後再次點擊查看
        # 回饋時會再嘗試送達，不重新呼叫模型（task 5.1）。
        log.info(
            "教練報告送達失敗，保留待補送",
            extra={"session_id": report_session_id, "reason": outcome.blocked_reason},
        )


def _is_stale(session_id: int, since_message_id: int) -> bool:
    """task 7.9：這則正在準備中的回覆，使用者是否已經又發了新訊息。

    獨立、短生命週期的 DB session——呼叫時機在 send_paced_reply 的
    delay 之後，此時原本 _handle_message 那個 db session 早已關閉。
    """
    with db_session() as db:
        return repo.has_newer_inbound(db, session_id, since_message_id)


async def _send_reply(
    session_id: int,
    persona_id: int,
    persona_slug: str,
    igsid: str,
    disclose: bool,
    message_id: int,
) -> None:
    """背景任務：生成角色回覆（7.6）並依擬真節奏送出（7.8），視需要
    附上揭露（7.5），送出前確認未被使用者插話的新訊息取代（7.9）。

    **必須是 async def，不能是「同步函式內部包一層 asyncio.run()」**：
    後者曾經是這裡的寫法，且對只用一次性資源的 InstagramClient 沒有問題
    （每次呼叫都在函式內全新建立、同一個事件迴圈內關閉）。但 llm_router
    的供應商實例是 lifespan 建立、跨請求存活的長壽物件，內部的 httpx
    client 採延遲建立（見 GeminiProvider._http）。若這裡仍用
    asyncio.run() 各開一個新事件迴圈，供應商的 httpx client 會綁死在
    第一次呼叫時的那個迴圈上，下一次背景任務（新的 asyncio.run()、新的
    迴圈）再用同一個供應商就會撞上「Event loop is closed」。改成 async
    def 後，FastAPI 的 BackgroundTasks 會直接把這個協程排進主事件迴圈
    （與 llm_router 建立時同一個），供應商的 httpx client 全程只活在
    一個迴圈裡，問題根本不會發生。

    平台規則全部交給 MessageSender、節奏交給 send_paced_reply——此處
    不需要知道 1000 bytes 上限、24 小時視窗、速率配額或延遲區間。
    """
    # 7.9 第一次檢查：生成前。若使用者已經又發了新訊息，這次生成
    # 從一開始就沒有意義——省下一次 LLM 呼叫（額度有限，見
    # verification-log 5.3 補充）比省下發訊動作更值得。
    if _is_stale(session_id, message_id):
        log.info(
            "生成前發現使用者已發送新訊息，放棄此次回覆",
            extra={"session_id": session_id},
        )
        return

    default_card = persona_cards.get(persona_slug)
    if default_card is None:
        # persona_cards 於啟動時載入，缺卡會讓服務起不來（見 lifespan），
        # 所以理論上走不到這裡。但節奏參數只存在於人設卡，沒有卡就沒有
        # 可用的預設值——與其瞎猜一個延遲區間，不如明確記錄並放棄這則。
        log.error(
            "找不到人設卡，無法決定回覆節奏，放棄此則",
            extra={"persona_slug": persona_slug, "session_id": session_id},
        )
        return

    # 7.6：組裝對話上下文。recent_messages 已包含觸發這次回覆的
    # 入站訊息本身（save_inbound_message 早於此背景任務落庫）。
    #
    # 6.7：一併載入這個使用者跨 session 的話題記憶。session 物件本身
    # 這裡用不到別的欄位，只是拿它的 user_id／persona_id 去查連結，
    # 不值得為此另開一個「依 session_id 查話題記憶」的 repo 函式。
    with db_session() as db:
        history = repo.recent_messages(
            db, session_id, settings.dialog_context_message_count
        )
        session_row = db.get(SessionModel, session_id)
        link = repo.get_link_by_session(db, session_row) if session_row is not None else None
        # design.md 決策 13：使用者有自己的版本就套用，沒有（或版本壞掉）則用
        # 平台預設版本。每次回覆都查——版本存在資料庫、隨時會變，
        # 保存後必須從下一則訊息起生效，不能像 YAML 那樣啟動時讀一次。
        card = resolve_card(db, default_card, link)
        known_topics = list(link.topic_memory) if link and link.topic_memory else None
        # 任務 8：已摘要的訊息不再以原文送入，只送摘要之後的部分
        summary = session_row.summary if session_row is not None else None
        summary_until = session_row.summary_until_message_id if session_row is not None else None
        if summary and summary_until is not None:
            history = [m for m in history if m.id > summary_until]
        previous = (
            format_previous_conversation(
                repo.previous_sessions_messages(
                    db, session_row, settings.previous_session_message_count
                )
            )
            if session_row is not None
            else []
        )
    context: list[LLMMessage] = build_context(history)
    system_prompt = render_system_prompt(
        card.prompt_fields,
        known_topics=known_topics,
        previous_conversation=previous or None,
        conversation_summary=summary if summary_until is not None else None,
    )

    if llm_router is not None:
        reply_text, llm_response = await llm_router.generate_or_fallback(
            "dialog", system_prompt=system_prompt, messages=context
        )
    else:
        # 金鑰未設定，llm_router 於 lifespan 中維持 None（見該處說明）。
        # 與呼叫失敗走同一句降級語句，使用者不會分辨出「沒接」與
        # 「接了但失敗」的差異——這差異只對開發者有意義。
        reply_text, llm_response = pick_fallback(), None

    # 揭露必須在 plan_reply 之前組好，否則拆分可能把揭露文字推到第二則，
    # 變成使用者先看到角色說話、才被告知對方是 AI——違反 7.5 的
    # 「先於或伴隨第一則對話內容出現」。
    text = compose_reply(
        reply_text,
        disclose=disclose,
        persona_name=card.identity.name,
        hint=settings_hint_text() if settings.settings_link_enabled else None,
    )

    client = InstagramClient(settings.ig_access_token, settings.ig_account_id)
    sender = MessageSender(
        client,
        rate_limiter,
        max_retries=settings.send_max_retries,
        retry_base_seconds=settings.send_retry_base_seconds,
        max_wait_seconds=settings.send_max_wait_seconds,
    )
    try:
        outcomes = await send_paced_reply(
            sender=sender,
            client=client,
            session_id=session_id,
            persona_id=persona_id,
            igsid=igsid,
            text=text,
            runtime=card.runtime,
            # 7.9 第二次檢查：擬真延遲跑完、真正要送出之前。
            is_stale=lambda: _is_stale(session_id, message_id),
        )
    finally:
        await client.aclose()

    log.info(
        "回覆處理完成",
        extra={
            "session_id": session_id,
            "bubbles": len(outcomes),
            "sent_parts": sum(len(o.delivered) for o in outcomes),
            "blocked_reason": next(
                (o.blocked_reason for o in outcomes if o.blocked_reason), None
            ),
            "rate_limited_hits": sum(o.rate_limited_hits for o in outcomes),
            "disclosed": disclose,
            "llm_used": llm_response is not None,
        },
    )

    # 任務 8：回覆送出之後才做，不延遲回覆。失敗時安靜放棄，下一則回覆後再試
    if llm_router is not None:
        await update_session_summary(
            llm_router,
            session_id,
            trigger=settings.summary_trigger_messages,
            keep_recent=settings.summary_keep_recent,
            update_batch=settings.summary_update_batch,
        )
