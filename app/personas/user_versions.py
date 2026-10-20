"""使用者版本的驗證、保存與套用（add-persona-authoring-console 任務 4.1～4.4）。

依 design.md 決策 13：使用者可以針對共用角色保存自己的人設，不經人工審核，
保存時驗證、通過即生效。共用角色的平台預設版本來自 YAML 檔（app/personas/
cards/），使用者版本存在資料庫（user_persona_versions），兩者經過同一個
PersonaCard 驗證。

合併規則——使用者版本只能決定「角色怎麼說話」，不能決定「角色是誰」：
- 名字、IG 顯示名稱：永遠取共用角色。所有使用者在 IG 上看到的是同一個帳號
- slug、safety_note：屬於平台，使用者版本不可帶入
- 其餘欄位：取使用者版本

資料庫裡的使用者版本若無法通過驗證，改套用平台預設版本並記錄，不讓
一份壞資料中斷服務——這與 YAML 檔「壞了就啟動失敗」刻意不同（loader.py）：
檔案由工程師維護、壞了必須被看見；使用者版本有成千上萬份，不能因為一份
壞掉就全體停擺。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session as DBSession

from app.db import repository as repo
from app.db.models import UserPersonaLink
from app.logging_config import get_logger
from app.personas.loader import PersonaCardError, format_validation_errors
from app.personas.schema import PersonaCard

log = get_logger(__name__)

# 使用者版本可以填寫的頂層段落。slug、safety_note 屬於平台，不在此列
USER_CARD_SECTIONS = (
    "identity",
    "background",
    "personality",
    "language_style",
    "boundaries",
    "runtime",
)

# 沿用共用角色、使用者不可修改的身分欄位
PLATFORM_IDENTITY_FIELDS = ("name", "ig_display_name")

# 「絕不接」的系統底線：每個使用者版本都必須包含，使用者不可移除（design.md 決策 13）。
# 文字與晴晴的平台預設版本（app/personas/cards/sunny.yaml）一致。
SYSTEM_BASELINE_NEVER_ENGAGES = (
    "索取電話 / LINE / 其他聯絡方式",
    "任何性方面的暗示或要求",
)


def _check_shape(default: PersonaCard, user_card: Any) -> None:
    """結構層級的檢查：型別、頂層欄位、不可修改的身分欄位。

    與 PersonaCard 的驗證分開，因為這些是「使用者版本」特有的規則，
    PersonaCard 對 YAML 檔並不適用（YAML 檔本來就要帶名字）。
    """
    if not isinstance(user_card, dict):
        raise PersonaCardError("使用者版本的內容必須是一個對應表")

    unknown = sorted(set(user_card) - set(USER_CARD_SECTIONS))
    if unknown:
        raise PersonaCardError(f"使用者版本含有不允許的欄位：{'、'.join(unknown)}")

    identity = user_card.get("identity")
    if identity is None:
        return
    if not isinstance(identity, dict):
        raise PersonaCardError("identity（內容必須是一個對應表）")

    for field in PLATFORM_IDENTITY_FIELDS:
        platform_value = getattr(default.identity, field)
        if field in identity and identity[field] != platform_value:
            raise PersonaCardError(
                f"identity.{field}（沿用共用角色，不可修改；共用角色為「{platform_value}」）",
                details=[(("identity", field), "platform_field", "沿用共用角色，不可修改")],
            )


def merge_user_card(default: PersonaCard, user_card: Any) -> PersonaCard:
    """把使用者版本與共用角色的平台欄位合併成一張完整的人設卡並驗證。

    任何不合法都拋 PersonaCardError，訊息指出具體欄位位置。
    """
    _check_shape(default, user_card)

    raw = dict(user_card)
    identity = dict(raw.get("identity") or {})
    for field in PLATFORM_IDENTITY_FIELDS:
        identity[field] = getattr(default.identity, field)
    raw["identity"] = identity

    try:
        return PersonaCard.model_validate(
            {**raw, "slug": default.slug, "safety_note": default.safety_note}
        )
    except ValidationError as exc:
        raise PersonaCardError(
            f"使用者版本缺少必要欄位或格式錯誤：{format_validation_errors(exc)}",
            details=[(tuple(err["loc"]), err["type"], err["msg"]) for err in exc.errors()],
        ) from exc


def delete_user_version(db: DBSession, link: UserPersonaLink) -> None:
    """恢復預設（任務 6.7）：刪除使用者版本與它的教練設定，之後套用平台預設版本。"""
    repo.delete_user_version(db, link.id)


def _strip_platform_fields(user_card: dict[str, Any]) -> dict[str, Any]:
    """存進資料庫的內容不含名字與 IG 顯示名稱——它們由共用角色補入，
    共用角色改名時使用者版本不需要跟著改。"""
    stored = dict(user_card)
    if isinstance(stored.get("identity"), dict):
        stored["identity"] = {
            k: v for k, v in stored["identity"].items() if k not in PLATFORM_IDENTITY_FIELDS
        }
    return stored


def save_user_card(
    db: DBSession,
    default: PersonaCard,
    link: UserPersonaLink,
    user_card: dict[str, Any],
) -> PersonaCard:
    """驗證並保存使用者版本，回傳合併後實際生效的人設卡（任務 4.2）。

    驗證在寫入之前：失敗就拋 PersonaCardError，資料庫完全沒有動，
    該使用者原本生效的版本維持不變（persona-authoring spec「保存時的驗證與生效」）。
    """
    merged = merge_user_card(default, user_card)
    missing = [b for b in SYSTEM_BASELINE_NEVER_ENGAGES if b not in merged.boundaries.never_engages]
    if missing:
        raise PersonaCardError(
            f"boundaries.never_engages（系統底線不能移除：{'、'.join(missing)}）",
            details=[(("boundaries", "never_engages"), "baseline", "系統底線不能移除")],
        )
    repo.save_user_version(db, link, _strip_platform_fields(user_card))
    return merged


def resolve_card(
    db: DBSession, default: PersonaCard, link: UserPersonaLink | None
) -> PersonaCard:
    """決定這位使用者這次回覆要套用的人設卡（任務 4.1、4.3、4.4）。

    有使用者版本且合法 → 使用者版本；沒有，或版本壞掉 → 平台預設版本。
    每次回覆都查，不快取：版本存在資料庫、隨時會變，spec 要求保存後
    「下一則訊息起生效」。
    """
    if link is None:
        return default

    version = repo.get_user_version(db, link.id)
    if version is None:
        return default

    try:
        return merge_user_card(default, version.card)
    except PersonaCardError as exc:
        log.error(
            "使用者版本無法通過驗證，改用平台預設版本",
            extra={"link_id": link.id, "persona_slug": default.slug, "error": str(exc)},
        )
        return default
