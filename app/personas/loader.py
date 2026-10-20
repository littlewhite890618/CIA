"""人設卡載入與結構驗證（task 7.2）。

依 design.md 決策 4：缺漏必要欄位時啟動失敗，不可降級為警告——
這是維持多角色品質下限的機制。缺一個欄位就整個角色不能上線，
比「用不完整的人設硬跑」安全得多：後者的代價是使用者真的聊到那個
沒定義的邊界時，模型只能亂猜，而亂猜正是這個產品最想避免的事。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from app.personas.schema import PersonaCard

CARDS_DIR = Path(__file__).parent / "cards"


class PersonaCardError(Exception):
    """人設卡格式錯誤。訊息會指出具體缺漏或錯誤的欄位，不是籠統的『格式錯誤』。

    details 是結構化的錯誤清單 [(欄位路徑, 錯誤類型, 原始訊息)]，供設定頁把錯誤
    標在對應欄位上（app/web/settings_api.py）；檔案載入的路徑不需要它，可為空。
    """

    def __init__(self, message: str, details: list[tuple[tuple, str, str]] | None = None) -> None:
        super().__init__(message)
        self.details = details or []


def load_persona_card(path: Path) -> PersonaCard:
    """載入單一人設卡檔案。

    slug 取自檔名（不含副檔名），例如 cards/sunny.yaml → slug="sunny"，
    需與 `personas` 表的 slug 欄位一致，角色路由才能對得上
    （見 app/main.py 啟動時 upsert_persona 的呼叫）。
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PersonaCardError(f"人設卡 {path.name} 不是合法的 YAML：{exc}") from exc

    if not isinstance(raw, dict):
        raise PersonaCardError(f"人設卡 {path.name} 的內容必須是一個對應表（YAML mapping）")

    try:
        return PersonaCard.model_validate({**raw, "slug": path.stem})
    except ValidationError as exc:
        raise PersonaCardError(
            f"人設卡 {path.name} 缺少必要欄位或格式錯誤：{format_validation_errors(exc)}\n"
            "系統不會以不完整的人設啟用此角色。"
        ) from exc


def format_validation_errors(exc: ValidationError) -> str:
    """把驗證錯誤整理成「欄位路徑（原因）」清單。

    檔案載入與使用者版本保存（app/personas/user_versions.py）共用，
    兩邊都必須指出具體欄位位置，不能只說「格式錯誤」。
    """
    parts = []
    for err in exc.errors():
        # loc 是 ('background', 'interests') 這種路徑，"." 串起來方便閱讀
        loc = ".".join(str(x) for x in err["loc"]) or "(頂層)"
        parts.append(f"{loc}（{err['msg']}）")
    return "；".join(parts)


def load_all_cards(cards_dir: Path = CARDS_DIR) -> dict[str, PersonaCard]:
    """載入目錄下所有人設卡，供服務啟動時呼叫。

    任一張卡有問題就整批失敗，不略過、不忽略——多角色上線後，若允許
    「壞掉的卡先跳過、其他照常啟動」，壞掉的角色會在沒人注意的情況下
    悄悄停擺。啟動失敗雖然粗暴，但保證問題一定會被看到。
    """
    cards: dict[str, PersonaCard] = {}
    for path in sorted(cards_dir.glob("*.yaml")):
        card = load_persona_card(path)
        cards[card.slug] = card
    return cards
