"""教練回饋網頁的 API（daily-coach-report-link 第 6 群，design.md 決策 4、5）。

凌晨送出的連結 `<對外網址>/report?t=<token>` 打開後，前端：

1. GET /api/report?t=：拿到這則回饋涵蓋的對話清單與各段狀態。只讀，不呼叫模型
2. 對狀態為「尚未產生」的段落，依序 POST /api/report/sessions/{id}/generate?t=
   產生並保存（打開連結＝使用者表示要看，spec「打開連結才產生報告」）

連結的簽章與設定連結同一套（用途前綴不同，app/auth/settings_link.py），也同樣
「第一個打開的瀏覽器才算數」：登記表共用 settings_link_claims，cookie 另取名、
路徑限定 /api/report。頁面本身（/report）不登記，避免被 IG 的預覽機器人用掉。

錯誤：過期回 401 {"error": "expired"}；已在別的瀏覽器打開回 401 {"error": "used"}；
竄改、格式錯誤、對應不到一律 401 {"error": "invalid"}，不透露該回饋是否存在。
回饋連結不像設定連結能靠關鍵字重新要，所以錯誤不帶關鍵字。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Cookie, Path, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.auth.settings_claim import REPORT_COOKIE_NAME, check_or_claim
from app.auth.settings_link import (
    REPORT_PURPOSE,
    SettingsLinkExpired,
    SettingsLinkInvalid,
    verify_token_with_expiry,
)
from app.config import get_settings
from app.db import repository as repo
from app.db import session as session_module
from app.db.models import Persona
from app.db.models import Session as SessionModel
from app.dialog.coach_report import display_sections
from app.dialog.report_service import READY, generate_session_report, report_status
from app.llm.router import LLMRouter
from app.logging_config import get_logger
from app.web.settings_api import FRONTEND_DIST, _PRIVATE_HEADERS

log = get_logger(__name__)

_COOKIE_PATH = "/api/report"


def _error(status: int, body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=_PRIVATE_HEADERS)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite 讀回來不帶時區
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def build_router(router_getter: Callable[[], LLMRouter | None]) -> APIRouter:
    """router_getter 取得目前的 LLM 路由（app.main 的 llm_router，啟動後才建立）。"""
    router = APIRouter()

    def authorize(t: str | None, code: str | None):
        """驗證連結與瀏覽器登記。回傳 (digest_id, 到期時間, 登記結果) 或 (None, 錯誤回應)。"""
        settings = get_settings()
        if not settings.settings_link_enabled:
            return None, _error(404, {"error": "unavailable"})
        try:
            digest_id, expires_at = verify_token_with_expiry(
                t or "", secret=settings.settings_link_secret, purpose=REPORT_PURPOSE
            )
        except SettingsLinkExpired:
            return None, _error(401, {"error": "expired"})
        except SettingsLinkInvalid:
            return None, _error(401, {"error": "invalid"})
        with session_module.db_session() as db:
            claimed = check_or_claim(db, t or "", code, expires_at)
        if not claimed.allowed:
            return None, _error(401, {"error": "used"})
        return (digest_id, expires_at, claimed), None

    def finish(response: JSONResponse, claimed, expires_at: int) -> JSONResponse:
        if claimed.new_code:
            response.set_cookie(
                REPORT_COOKIE_NAME,
                claimed.new_code,
                max_age=max(int(expires_at - datetime.now(timezone.utc).timestamp()), 1),
                path=_COOKIE_PATH,
                httponly=True,
                samesite="lax",
                secure=get_settings().public_base_url.startswith("https://"),
            )
        return response

    def mark_seen(db, session_id: int) -> None:
        """報告第一次顯示在使用者頁面上的時刻（spec「記錄被看到的時刻」），之後不覆蓋。"""
        report = repo.get_coach_report(db, session_id)
        if report is not None and report.generated_at is not None and report.delivered_at is None:
            repo.mark_report_delivered(db, report)

    @router.get("/api/report")
    def read_report(t: str | None = Query(None), code: str | None = Cookie(None, alias=REPORT_COOKIE_NAME)):
        authorized, failure = authorize(t, code)
        if failure:
            return failure
        digest_id, expires_at, claimed = authorized
        settings = get_settings()
        with session_module.db_session() as db:
            digest = repo.get_digest(db, digest_id)
            sessions = repo.digest_sessions(db, digest_id) if digest else []
            if digest is None or not sessions:
                return _error(401, {"error": "invalid"})
            persona = db.get(Persona, sessions[0].persona_id)
            items = []
            for session in sessions:
                report = repo.get_coach_report(db, session.id)
                status = report_status(session, report)
                if status == READY:
                    mark_seen(db, session.id)
                items.append({
                    "id": session.id,
                    "startedAt": _iso(session.started_at),
                    "endedAt": _iso(session.ended_at),
                    "status": status,
                    "sections": display_sections(report.content) if status == READY else [],
                })
            body = {
                "personaName": persona.name if persona else "晴晴",
                "expiresAt": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
                "chatUrl": (
                    f"https://ig.me/m/{settings.ig_username}"
                    if settings.ig_username
                    else "https://www.instagram.com/direct/inbox/"
                ),
                "sessions": items,
            }
        return finish(JSONResponse(body, headers=_PRIVATE_HEADERS), claimed, expires_at)

    @router.post("/api/report/sessions/{session_id}/generate")
    async def generate(
        session_id: int = Path(...),
        t: str | None = Query(None),
        code: str | None = Cookie(None, alias=REPORT_COOKIE_NAME),
    ):
        authorized, failure = authorize(t, code)
        if failure:
            return failure
        digest_id, expires_at, claimed = authorized
        with session_module.db_session() as db:
            session = db.get(SessionModel, session_id)
            # 只能產生這則回饋裡的對話；別人的對話一律當無效，不透露它是否存在
            if session is None or session.coach_digest_id != digest_id:
                return _error(401, {"error": "invalid"})

        result = await generate_session_report(router_getter(), session_id)

        if result.status == READY:
            with session_module.db_session() as db:
                mark_seen(db, session_id)
        body = {
            "id": session_id,
            "status": result.status,
            "sections": display_sections(result.content) if result.status == READY else [],
        }
        return finish(JSONResponse(body, headers=_PRIVATE_HEADERS), claimed, expires_at)

    @router.get("/report")
    def report_page():
        page = FRONTEND_DIST / "report.html"
        if not page.exists():
            return Response("回饋頁尚未打包（frontend/dist/report.html 不存在）", status_code=503)
        html = page.read_text(encoding="utf-8").replace(
            "__PUBLIC_BASE_URL__", get_settings().public_base_url.rstrip("/")
        )
        return HTMLResponse(html, headers=_PRIVATE_HEADERS)

    return router
