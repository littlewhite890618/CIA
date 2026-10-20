"""後台網頁：登入、管理員頁面、客戶頁面（add-persona-authoring-console 任務 5.2、5.3、5.6）。

存取規則：
- 管理員頁面（/admin 開頭）：非管理員一律回 404，包含未登入——不透露這些頁面存在
- 客戶頁面（/client）：未登入導向登入頁；管理員進來也回 404，兩種後台不混用
- 登入失敗只說「帳號或密碼錯誤」，不區分帳號不存在或密碼錯誤

以 build_router() 建立而非模組層級的單例：測試會重新載入 app.main，
每次重建才能拿到乾淨的登入失敗計數。資料庫與設定一律在請求當下取用
（session_module.db_session、get_settings），不在匯入時綁定。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.auth import accounts
from app.config import get_settings
from app.db import session as session_module
from app.db.models import Account, AccountRole, Tenant
from app.logging_config import get_logger

log = get_logger(__name__)

SESSION_COOKIE = "cia_session"
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_NO_STORE = {"Cache-Control": "no-store"}  # 頁面上可能有剛產生的密碼，不可被快取


def _not_found() -> HTMLResponse:
    return HTMLResponse("Not Found", status_code=404)


def _render(request: Request, name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request, name, context, status_code=status_code, headers=_NO_STORE
    )


def _current_account(request: Request, db) -> Account | None:
    return accounts.account_for_session(db, request.cookies.get(SESSION_COOKIE))


def _admin_page_context(db, account: Account, **extra: Any) -> dict[str, Any]:
    tenants = list(db.scalars(select(Tenant).order_by(Tenant.id)))
    all_accounts = list(db.scalars(select(Account).order_by(Account.id)))
    return {
        "me": account,
        "tenants": [
            {"tenant": t, "accounts": [a for a in all_accounts if a.tenant_id == t.id]}
            for t in tenants
        ],
        "admins": [a for a in all_accounts if a.role == AccountRole.ADMIN],
        **extra,
    }


def build_router() -> APIRouter:
    router = APIRouter()
    settings = get_settings()
    throttle = accounts.LoginThrottle(
        settings.login_max_failures, settings.login_failure_window_seconds
    )

    # ── 登入與登出 ────────────────────────────────────

    @router.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return _render(request, "login.html", {"error": None})

    @router.post("/login")
    def login(request: Request, username: str = Form(""), password: str = Form("")):
        if throttle.is_blocked(username):
            log.warning("登入失敗次數過多，暫時拒絕", extra={"username": username})
            return _render(
                request, "login.html",
                {"error": "嘗試次數過多，請稍後再試"}, status_code=429,
            )

        with session_module.db_session() as db:
            account = accounts.authenticate(db, username, password)
            if account is None:
                throttle.record_failure(username)
                log.info("登入失敗", extra={"username": username})
                return _render(
                    request, "login.html", {"error": "帳號或密碼錯誤"}, status_code=401
                )
            throttle.reset(username)
            token = accounts.start_session(
                db, account, ttl_seconds=get_settings().login_session_ttl_seconds
            )
            target = "/admin" if account.role == AccountRole.ADMIN else "/client"
            log.info("登入成功", extra={"account_id": account.id, "role": account.role})

        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=get_settings().login_session_ttl_seconds,
            httponly=True,
            # Lax：其他網站發來的表單 POST 不會帶上這個 cookie，擋掉跨站偽造請求
            samesite="lax",
            secure=get_settings().public_base_url.startswith("https://"),
        )
        return response

    @router.post("/logout")
    def logout(request: Request):
        with session_module.db_session() as db:
            accounts.end_session(db, request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    # ── 管理員頁面（任務 5.3、5.6）───────────────────

    @router.get("/admin", response_class=HTMLResponse)
    def admin_home(request: Request):
        with session_module.db_session() as db:
            me = _current_account(request, db)
            if me is None or me.role != AccountRole.ADMIN:
                return _not_found()
            return _render(request, "admin.html", _admin_page_context(db, me))

    @router.post("/admin/tenants", response_class=HTMLResponse)
    def admin_create_tenant(
        request: Request,
        name: str = Form(""),
        ref_code: str = Form(""),
        username: str = Form(""),
    ):
        with session_module.db_session() as db:
            me = _current_account(request, db)
            if me is None or me.role != AccountRole.ADMIN:
                return _not_found()
            try:
                tenant, account, password = accounts.create_tenant_with_client(
                    db, name=name, ref_code=ref_code, username=username
                )
            except accounts.AccountError as exc:
                return _render(
                    request, "admin.html",
                    _admin_page_context(db, me, error=str(exc), form={"name": name, "ref_code": ref_code, "username": username}),
                    status_code=400,
                )
            log.info("開通客戶", extra={"by": me.id, "tenant_id": tenant.id, "account_id": account.id})
            notice = {
                "title": f"已開通客戶「{tenant.name}」",
                "username": account.username,
                "password": password,
            }
            return _render(request, "admin.html", _admin_page_context(db, me, notice=notice))

    @router.post("/admin/admins", response_class=HTMLResponse)
    def admin_create_admin(request: Request, username: str = Form("")):
        with session_module.db_session() as db:
            me = _current_account(request, db)
            if me is None or me.role != AccountRole.ADMIN:
                return _not_found()
            try:
                account, password = accounts.create_admin(db, username)
            except accounts.AccountError as exc:
                return _render(
                    request, "admin.html", _admin_page_context(db, me, error=str(exc)),
                    status_code=400,
                )
            log.info("新增管理員", extra={"by": me.id, "account_id": account.id})
            notice = {"title": "已新增管理員", "username": account.username, "password": password}
            return _render(request, "admin.html", _admin_page_context(db, me, notice=notice))

    @router.post("/admin/accounts/{account_id}/reset-password", response_class=HTMLResponse)
    def admin_reset_password(request: Request, account_id: int):
        with session_module.db_session() as db:
            me = _current_account(request, db)
            if me is None or me.role != AccountRole.ADMIN:
                return _not_found()
            target = db.get(Account, account_id)
            if target is None:
                return _not_found()
            password = accounts.reset_password(db, target)
            log.info("重設密碼", extra={"by": me.id, "account_id": target.id})
            # 重設自己的密碼也照樣顯示這一頁：直接導回登入頁會讓新密碼看不到。
            # 自己的登入已失效，下一次操作才會被要求重新登入
            notice = {
                "title": f"已重設「{target.username}」的密碼，原本的登入已全部失效",
                "username": target.username,
                "password": password,
            }
            return _render(request, "admin.html", _admin_page_context(db, me, notice=notice))

    # ── 客戶頁面（統計頁屬第 7 部分）─────────────────

    @router.get("/client", response_class=HTMLResponse)
    def client_home(request: Request):
        with session_module.db_session() as db:
            me = _current_account(request, db)
            if me is None:
                return RedirectResponse("/login", status_code=303)
            if me.role != AccountRole.CLIENT:
                return _not_found()
            tenant = db.get(Tenant, me.tenant_id)
            return _render(request, "client.html", {"me": me, "tenant": tenant})

    return router
