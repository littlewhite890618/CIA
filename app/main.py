"""FastAPI 應用入口。

POC 階段：健康檢查、webhook 接收（tasks 3.1–3.7）、使用者身分歸戶
（task 6.1）、開場問候（原 task 7.5 的 AI 揭露）、角色回覆生成（task 7.6，含邊界
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
from datetime import datetime

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
from app.channels.sender import MessageSender, SendOutcome
from app.config import get_settings
from app.db import repository as repo
from app.db.models import Session as SessionModel
from app.auth.settings_link import REPORT_PURPOSE, build_report_url, build_settings_url, issue_token
from app.db.models import SessionState, SystemMessageKind, UserPersonaLink, utcnow
from app.db.session import check_connection, db_session, init_db
from app.dialog.coach_report import INVITE_BUTTON_TITLE, parse_invite_payload
from app.dialog.digest_job import digest_message_text, last_run_at, next_run_at, run_digest
from app.dialog.context import build_context, format_previous_conversation
from app.dialog.summary import update_session_summary
from app.dialog.delivery import send_paced_reply
from app.dialog.signal_job import run_signal_detection
from app.dialog.topics import extract_topics
from app.dialog.traditional import to_traditional
from app.llm import LLMConfigError, LLMMessage, LLMRouter, build_router, pick_fallback
from app.logging_config import get_logger, setup_logging
from app.personas.disclosure import compose_reply, should_disclose
from app.personas.loader import PersonaCardError, load_all_cards
from app.personas.prompt import render_system_prompt
from app.personas.schema import PersonaCard
from app.personas.user_versions import resolve_card
from app.web.backoffice import build_router as build_backoffice_router
from app.web.settings_api import FRONTEND_DIST as SETTINGS_FRONTEND_DIST
from app.web.report_api import build_router as build_report_router
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
digest_task: asyncio.Task | None = None


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
    global silence_poll_task, digest_task
    silence_poll_task = asyncio.create_task(_silence_poll_loop())

    # daily-coach-report-link 任務 5.2：每日教練回饋。連結與設定頁共用密鑰與對外網址，
    # 沒設定就無法產生可用的連結，乾脆不啟動
    if settings.settings_link_enabled:
        digest_task = asyncio.create_task(_daily_digest_loop())
    else:
        log.warning("教練回饋連結未啟用：SETTINGS_LINK_SECRET 或 PUBLIC_BASE_URL 未設定，不會送出每日回饋")

    yield

    if digest_task is not None:
        digest_task.cancel()
        try:
            await digest_task
        except asyncio.CancelledError:
            pass

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
# daily-coach-report-link 第 6 群：教練回饋網頁。llm_router 在啟動後才建立，所以傳取用函式
app.include_router(build_report_router(lambda: llm_router))
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

    # 7.5：要不要加開場問候，必須用「這次活動之前」的最後互動時間，所以要在
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
    新對話中關鍵字可能就是第一則訊息，因此照樣加上開場問候。
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
        template=settings.opening_greeting,
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
            meta={"system": SystemMessageKind.COACH_DIGEST},  # 不是聊天內容，不進對話上下文
        )
        background_tasks.add_task(_handle_old_invite_click, report_session_id, session.id, igsid)


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
        repo.end_session(db, session)

    if link is None or not history:
        return

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

    # daily-coach-report-link 任務 5.1：不再在對話結束當下發邀請，
    # 改由每日排程在凌晨把結束的對話合併成一則回饋連結（_daily_digest_loop）


async def _send_system_text(
    session_id: int, persona_id: int, igsid: str, text: str, meta: dict
) -> SendOutcome:
    """以系統訊息送出一則文字（每日回饋連結）。不顯示輸入中、不走擬真節奏。"""
    client = InstagramClient(settings.ig_access_token, settings.ig_account_id)
    sender = MessageSender(
        client,
        rate_limiter,
        max_retries=settings.send_max_retries,
        retry_base_seconds=settings.send_retry_base_seconds,
        max_wait_seconds=settings.send_max_wait_seconds,
    )
    try:
        return await sender.send_reply(
            session_id=session_id, persona_id=persona_id, igsid=igsid, text=text,
            typing=False, meta=meta,
        )
    finally:
        await client.aclose()


def _report_url(digest_id: int) -> str:
    token = issue_token(
        digest_id,
        secret=settings.settings_link_secret,
        ttl_seconds=settings.coach_report_link_ttl_days * 86400,
        purpose=REPORT_PURPOSE,
    )
    return build_report_url(settings.public_base_url, token)


async def _run_digest(before: datetime) -> None:
    await run_digest(
        before,
        send=_send_system_text,
        url_for=_report_url,
        min_user_messages=settings.coach_report_min_user_messages,
        ttl_days=settings.coach_report_link_ttl_days,
    )


async def _daily_digest_loop() -> None:
    """每日教練回饋（daily-coach-report-link 任務 5.2，design.md 決策 2）。

    啟動時先以「上一個排定時刻」為界補跑：只會撈到那之前就結束、卻沒被處理的
    對話（例如服務在 04:00 剛好停機），之後才結束的照常等下一個排定時刻。
    """
    at, tz = settings.coach_digest_time, settings.coach_digest_timezone
    try:
        await _run_digest(last_run_at(utcnow(), at=at, tz_name=tz))
    except Exception:
        log.exception("每日教練回饋補跑發生例外")
    while True:
        run_at = next_run_at(utcnow(), at=at, tz_name=tz)
        await asyncio.sleep(max((run_at - utcnow()).total_seconds(), 0))
        try:
            await _run_digest(run_at)
        except Exception:
            # 常駐任務：單次出錯不能讓迴圈死掉，否則之後每天都不會再送
            log.exception("每日教練回饋送出發生例外，明天同一時刻再試")


async def _handle_old_invite_click(
    report_session_id: int, delivery_session_id: int, igsid: str
) -> None:
    """改版前送出的「看這次的回饋」按鈕被點擊（任務 5.4）：回覆該段對話的回饋連結。

    已經併入每日回饋的就沿用那一則，沒有就為這段對話單獨建一則。連結送在
    點擊當下的場次上（點擊會重置 24 小時視窗）。不在私訊中送報告內容。
    """
    with db_session() as db:
        report_session = db.get(SessionModel, report_session_id)
        if report_session is None:
            log.warning("教練報告對應的 session 不存在", extra={"session_id": report_session_id})
            return
        digest_id = report_session.coach_digest_id
        if digest_id is None:
            link = repo.get_link_by_session(db, report_session)
            if link is None:
                return
            digest_id = repo.create_digest(db, link, [report_session]).id
        persona_id = report_session.persona_id

    if not settings.settings_link_enabled:
        log.warning("回饋連結功能未啟用，無法回覆舊按鈕", extra={"session_id": report_session_id})
        return

    outcome = await _send_system_text(
        delivery_session_id,
        persona_id,
        igsid,
        digest_message_text(_report_url(digest_id), settings.coach_report_link_ttl_days),
        {"system": SystemMessageKind.COACH_DIGEST},
    )
    with db_session() as db:
        digest = repo.get_digest(db, digest_id)
        if outcome.ok:
            repo.mark_digest_sent(db, digest)
        else:
            repo.mark_digest_skipped(db, digest, outcome.blocked_reason or "send_failed")
    log.info(
        "舊按鈕已回覆回饋連結",
        extra={"session_id": report_session_id, "digest_id": digest_id, "sent": outcome.ok},
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
    加上開場問候（7.5），送出前確認未被使用者插話的新訊息取代（7.9）。

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

    # daily-coach-report-link 任務 2.3：模型偶爾夾雜簡體字，送出前轉成繁體
    reply_text = to_traditional(reply_text)

    # 開場問候必須在 plan_reply 之前組好，否則拆分可能把它推到第二則，
    # 變成使用者先看到角色說話、才看到問候。
    text = compose_reply(
        reply_text,
        disclose=disclose,
        persona_name=card.identity.name,
        template=settings.opening_greeting,
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
