"""使用者設定頁的 API 與頁面（add-persona-authoring-console 第 6 群）。

    GET    /api/settings?t=<token>   讀取：平台預設版本、目前生效的版本、教練設定
    POST   /api/settings?t=<token>   保存：驗證通過即生效，下一則訊息起套用
    DELETE /api/settings?t=<token>   恢復預設：刪除使用者版本
    GET    /settings                  React 打包後的頁面（frontend/dist）

存取：token 就是憑證（app/auth/settings_link.py），綁定「使用者 × 共用角色」。
過期回 401 {"error": "expired"}；竄改、格式錯誤、對應不到使用者一律回
401 {"error": "invalid"}，不透露該使用者是否存在。過期與「已在別處打開」
另帶 keyword：簽章已驗證過，持有者本來就拿過連結、知道關鍵字；無效的連結
不帶，免得任何人亂打一串就能問出關鍵字。

設定頁的資料格式與後端人設卡幾乎相同，差在：值一律是字串（年齡、錯字率、秒數、
布林值在這裡轉換），回覆延遲在畫面上是 runtime.delay_min／delay_max 兩欄。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Cookie, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.auth.settings_claim import COOKIE_NAME, check_or_claim
from app.auth.settings_link import SettingsLinkExpired, SettingsLinkInvalid, verify_token_with_expiry
from app.config import get_settings
from app.db import repository as repo
from app.db import session as session_module
from app.db.models import Persona, PersonaStatus, UserPersonaLink
from app.logging_config import get_logger
from app.personas import user_versions
from app.personas.coaching_settings import (
    coaching_from_columns,
    coaching_to_columns,
    default_coaching,
    validate_coaching,
)
from app.personas.content_check import find_problems
from app.personas.loader import PersonaCardError
from app.personas.schema import PersonaCard

log = get_logger(__name__)

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

# 網址裡有 token：不能讓瀏覽器把它當 Referer 送給字型服務或 Instagram
_PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _error(status: int, body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=_PRIVATE_HEADERS)


# ── 人設卡 ⇄ 設定頁格式 ───────────────────────────

def _str(value: Any) -> str:
    # 去頭尾空白：YAML 的多行文字（|）會帶結尾換行，保存時又會被去掉，
    # 不在這裡先統一，原樣存回後就會被當成「跟預設不同」
    return "" if value is None else str(value).strip()


def card_to_ui(card: PersonaCard) -> dict[str, dict[str, Any]]:
    data = card.model_dump(mode="json")
    identity, background = data["identity"], data["background"]
    personality, style = data["personality"], data["language_style"]
    delay = data["runtime"]["reply_delay_seconds"]
    return {
        "identity": {
            "name": identity["name"],
            "ig_display_name": identity["ig_display_name"],
            "age": _str(identity["age"]),
            "occupation": _str(identity["occupation"]),
            "location": _str(identity["location"]),
        },
        "background": {
            "recent_life": _str(background["recent_life"]),
            "interests": [_str(i) for i in background["interests"]],
        },
        "personality": {key: _str(personality[key]) for key in (
            "core_traits", "initiative", "questioning", "emotional_range",
            "compliment_response", "self_disclosure",
        )},
        "language_style": {
            "pace": _str(style["pace"]),
            "message_length": _str(style["message_length"]),
            "message_splitting": _str(style["message_splitting"]),
            "particles": [_str(p) for p in style["particles"]],
            "emoji": _str(style["emoji"]),
            "punctuation": _str(style["punctuation"]),
            "typo_rate": _str(style["typo_rate"]),
        },
        "boundaries": {
            key: [_str(i) for i in data["boundaries"][key]]
            for key in ("warms_up_to", "cools_down_on", "never_engages")
        } | {"cooling_behavior": _str(data["boundaries"]["cooling_behavior"])},
        "runtime": {
            "delay_min": _str(delay["min"]),
            "delay_max": _str(delay["max"]),
            "typing_indicator": "true" if data["runtime"]["typing_indicator"] else "false",
        },
    }


_UI_FIELDS: dict[str, tuple[str, ...]] = {
    "identity": ("age", "occupation", "location"),
    "background": ("recent_life", "interests"),
    "personality": ("core_traits", "initiative", "questioning", "emotional_range",
                    "compliment_response", "self_disclosure"),
    "language_style": ("pace", "message_length", "message_splitting", "particles",
                       "emoji", "punctuation", "typo_rate"),
    "boundaries": ("warms_up_to", "cools_down_on", "never_engages", "cooling_behavior"),
    "runtime": ("delay_min", "delay_max", "typing_indicator"),
}
_LIST_FIELDS = {"interests", "particles", "warms_up_to", "cools_down_on", "never_engages"}


def ui_to_user_card(ui: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """設定頁格式 → 使用者版本（不含名字、IG 顯示名稱）。回傳 (人設卡, 轉換錯誤)。"""
    errors: dict[str, str] = {}
    if not isinstance(ui, dict):
        return {}, {"card": "設定的格式不正確。"}

    def text(section: str, key: str) -> str:
        # 設定頁的每個文字欄位都必填（與前端驗證一致）：人設卡結構允許空字串，
        # 但空白的「近況」「核心特質」會讓角色沒有素材可用
        value = (ui.get(section) or {}).get(key, "")
        if not isinstance(value, str):
            errors[f"{section}.{key}"] = "格式不正確。"
            return ""
        if not value.strip():
            errors[f"{section}.{key}"] = "這一欄要填。"
        return value.strip()

    def items(section: str, key: str) -> list[str]:
        value = (ui.get(section) or {}).get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            errors[f"{section}.{key}"] = "格式不正確。"
            return []
        return [v.strip() for v in value if v.strip()]

    def number(section: str, key: str, cast: Callable[[str], Any], message: str) -> Any:
        raw = text(section, key)
        try:
            return cast(raw)
        except ValueError:
            errors[f"{section}.{key}"] = message
            return None

    card: dict[str, Any] = {}
    for section, keys in _UI_FIELDS.items():
        if section == "runtime":
            continue
        card[section] = {}
        for key in keys:
            if key in _LIST_FIELDS:
                card[section][key] = items(section, key)
            elif (section, key) == ("identity", "age"):
                card[section][key] = number(section, key, int, "寫一個數字。")
            elif (section, key) == ("language_style", "typo_rate"):
                card[section][key] = number(section, key, float, "寫一個 0 到 1 之間的數字，例如 0.03。")
            else:
                card[section][key] = text(section, key)

    card["runtime"] = {
        "reply_delay_seconds": {
            "min": number("runtime", "delay_min", int, "寫一個整數秒數。"),
            "max": number("runtime", "delay_max", int, "寫一個整數秒數。"),
        },
        "typing_indicator": text("runtime", "typing_indicator") != "false",
    }
    return card, errors


_TYPE_MESSAGES = {
    "missing": "這一欄要填。",
    "too_short": "至少留一項。",
    "string_too_short": "這一欄要填。",
}


def details_to_errors(details: list[tuple[tuple, str, str]]) -> dict[str, str]:
    """PersonaCardError.details → {設定頁欄位路徑: 訊息}。"""
    errors: dict[str, str] = {}
    for loc, err_type, msg in details:
        if len(loc) >= 2 and loc[0] == "runtime" and loc[1] == "reply_delay_seconds":
            errors["runtime.delay_min"] = "最快不能比最慢還慢。" if err_type == "value_error" else "寫一個大於 0 的秒數。"
            continue
        path = ".".join(str(p) for p in loc[:2]) if len(loc) >= 2 else "card"
        if err_type in ("platform_field", "baseline"):
            errors[path] = msg + "。"
        elif path == "identity.age":
            errors[path] = "寫一個數字。"
        elif path == "language_style.typo_rate":
            errors[path] = "寫一個 0 到 1 之間的數字，例如 0.03。"
        else:
            errors[path] = _TYPE_MESSAGES.get(err_type, "格式不正確。")
    return errors


# ── 路由 ──────────────────────────────────────────

def build_router(default_card_for: Callable[[str], PersonaCard | None]) -> APIRouter:
    """default_card_for(slug) 取共用角色的平台預設版本（app.main 的 persona_cards）。"""
    router = APIRouter()

    def resolve(token: str | None):
        """驗證 token 並取出連結與共用角色。失敗時回傳 (None, 錯誤回應)。"""
        settings = get_settings()
        if not settings.settings_link_enabled:
            return None, _error(404, {"error": "unavailable"})
        try:
            link_id, expires_at = verify_token_with_expiry(token or "", secret=settings.settings_link_secret)
        except SettingsLinkExpired:
            return None, _error(401, {"error": "expired", "keyword": settings.settings_keyword})
        except SettingsLinkInvalid:
            return None, _error(401, {"error": "invalid"})
        return (link_id, expires_at), None

    def claim(t: str | None, code: str | None, expires_at: int):
        """連結只能在第一個打開它的瀏覽器上使用（任務 5.8）。"""
        with session_module.db_session() as db:
            return check_or_claim(db, t or "", code, expires_at)

    def finish(response: JSONResponse, claimed, expires_at: int) -> JSONResponse:
        """第一次打開時，把代碼交給這個瀏覽器。cookie 只送往 /api/settings，壽命到連結到期為止。"""
        if claimed.new_code:
            response.set_cookie(
                COOKIE_NAME,
                claimed.new_code,
                max_age=max(int(expires_at - datetime.now(timezone.utc).timestamp()), 1),
                path="/api/settings",
                httponly=True,
                samesite="lax",
                secure=get_settings().public_base_url.startswith("https://"),
            )
        return response

    def load(db, link_id: int):
        link = db.get(UserPersonaLink, link_id)
        persona = db.get(Persona, link.persona_id) if link else None
        if link is None or persona is None or persona.status != PersonaStatus.ONLINE:
            return None, None, None
        return link, persona, default_card_for(persona.slug)

    @router.get("/api/settings")
    def read_settings(t: str | None = Query(None), code: str | None = Cookie(None, alias=COOKIE_NAME)):
        resolved, failure = resolve(t)
        if failure:
            return failure
        link_id, expires_at = resolved
        claimed = claim(t, code, expires_at)
        if not claimed.allowed:
            return _error(401, {"error": "used", "keyword": get_settings().settings_keyword})
        settings = get_settings()
        with session_module.db_session() as db:
            link, persona, default = load(db, link_id)
            if link is None or default is None:
                return _error(401, {"error": "invalid"})
            current = user_versions.resolve_card(db, default, link)
            version = repo.get_user_version(db, link.id)
            # 版本壞掉時 resolve_card 已退回預設；教練設定也一併用預設，避免兩者不一致
            coaching = (
                coaching_from_columns(repo.get_coaching(db, version.id))
                if version is not None and current is not default
                else default_coaching()
            )
            body = {
                "personaName": default.identity.name,
                "igDisplayName": default.identity.ig_display_name,
                "expiresAt": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
                "chatUrl": (
                    f"https://ig.me/m/{settings.ig_username}"
                    if settings.ig_username
                    else "https://www.instagram.com/direct/inbox/"
                ),
                "defaults": {"card": card_to_ui(default), "coaching": default_coaching()},
                "current": {"card": card_to_ui(current), "coaching": coaching},
                "baselineNeverEngages": list(user_versions.SYSTEM_BASELINE_NEVER_ENGAGES),
                "keyword": settings.settings_keyword,
            }
        return finish(JSONResponse(body, headers=_PRIVATE_HEADERS), claimed, expires_at)

    @router.post("/api/settings")
    def save_settings(
        t: str | None = Query(None),
        payload: Any = Body(None),
        code: str | None = Cookie(None, alias=COOKIE_NAME),
    ):
        resolved, failure = resolve(t)
        if failure:
            return failure
        link_id, expires_at = resolved
        claimed = claim(t, code, expires_at)
        if not claimed.allowed:
            return _error(401, {"error": "used", "keyword": get_settings().settings_keyword})
        if not isinstance(payload, dict):
            return _error(422, {"errors": {"card": "設定的格式不正確。"}})

        user_card, errors = ui_to_user_card(payload.get("card"))
        errors.update(find_problems(payload.get("card") or {}))
        coaching, coaching_errors = validate_coaching(payload.get("coaching"))
        errors.update(coaching_errors)
        if errors:
            return _error(422, {"errors": errors})

        with session_module.db_session() as db:
            link, _persona, default = load(db, link_id)
            if link is None or default is None:
                return _error(401, {"error": "invalid"})
            try:
                user_versions.save_user_card(db, default, link, user_card)
            except PersonaCardError as exc:
                db.rollback()
                return _error(422, {"errors": details_to_errors(exc.details) or {"card": str(exc)}})
            version = repo.get_user_version(db, link.id)
            repo.save_coaching(db, version, coaching_to_columns(coaching))
            log.info("使用者版本已保存", extra={"link_id": link.id})
        return finish(JSONResponse({"ok": True}, headers=_PRIVATE_HEADERS), claimed, expires_at)

    @router.delete("/api/settings")
    def reset_settings(t: str | None = Query(None), code: str | None = Cookie(None, alias=COOKIE_NAME)):
        resolved, failure = resolve(t)
        if failure:
            return failure
        link_id, expires_at = resolved
        claimed = claim(t, code, expires_at)
        if not claimed.allowed:
            return _error(401, {"error": "used", "keyword": get_settings().settings_keyword})
        with session_module.db_session() as db:
            link, _persona, default = load(db, link_id)
            if link is None or default is None:
                return _error(401, {"error": "invalid"})
            user_versions.delete_user_version(db, link)
            log.info("使用者版本已恢復預設", extra={"link_id": link.id})
        return finish(JSONResponse({"ok": True}, headers=_PRIVATE_HEADERS), claimed, expires_at)

    @router.get("/settings")
    def settings_page():
        index = FRONTEND_DIST / "index.html"
        if not index.exists():
            return Response("設定頁尚未打包（frontend/dist 不存在）", status_code=503)
        # 連結預覽的圖片要完整網址（Instagram 抓預覽時不會解析相對路徑），
        # 打包時還不知道對外網址，在這裡填入
        html = index.read_text(encoding="utf-8").replace(
            "__PUBLIC_BASE_URL__", get_settings().public_base_url.rstrip("/")
        )
        return HTMLResponse(html, headers=_PRIVATE_HEADERS)

    return router
