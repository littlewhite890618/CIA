"""使用者版本的驗證、保存與套用（add-persona-authoring-console 任務 4.1～4.5）。

單元層驗證合併與驗證規則；整合層走完整 webhook → 背景任務管線
（架構同 test_reply_generation.py），確認回覆與訊號偵測真的套用了
使用者的版本，且保存後「下一則訊息起生效」。
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import repository as repo
from app.db.models import Base, PersonaStatus, UserPersonaVersion
from app.personas.loader import CARDS_DIR, PersonaCardError, load_all_cards
from app.personas.user_versions import (
    merge_user_card,
    resolve_card,
    save_user_card,
)
from tests.test_reply_generation import (  # noqa: F401  (make_client 為 fixture)
    IG_ACCOUNT_ID,
    _backdate_session,
    _post,
    make_client,
)


@pytest.fixture(scope="module")
def default_card():
    return load_all_cards(CARDS_DIR)["sunny"]


def _user_card(default_card, **section_overrides) -> dict:
    """以平台預設版本為底，產生一份使用者版本（不含名字、slug、safety_note）。"""
    raw = default_card.model_dump(mode="json")
    raw.pop("slug")
    raw.pop("safety_note")
    raw["identity"].pop("name")
    raw["identity"].pop("ig_display_name")
    raw = copy.deepcopy(raw)
    for section, values in section_overrides.items():
        raw[section].update(values)
    return raw


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session


@pytest.fixture
def link(db):
    persona = repo.upsert_persona(
        db, slug="sunny", name="晴晴", gender="female", ig_account_id=IG_ACCOUNT_ID
    )
    _user, link, _ = repo.resolve_user(
        db, persona_id=persona.id, igsid="IGSID_V1", ig_username="v1"
    )
    db.commit()
    return link


def _second_link(db, persona_id: int, n: int):
    _user, link, _ = repo.resolve_user(
        db, persona_id=persona_id, igsid=f"IGSID_V{n}", ig_username=f"v{n}"
    )
    db.commit()
    return link


# ═══════════════════════════════════════════════════════════
#  合併與驗證
# ═══════════════════════════════════════════════════════════

def test_合併_名字與安全註記取自共用角色(default_card):
    user = _user_card(default_card, personality={"core_traits": "活潑外向、愛分享"})
    merged = merge_user_card(default_card, user)

    assert merged.personality.core_traits == "活潑外向、愛分享"
    assert merged.identity.name == default_card.identity.name
    assert merged.identity.ig_display_name == default_card.identity.ig_display_name
    assert merged.slug == default_card.slug
    assert merged.safety_note == default_card.safety_note


def test_合併_使用者版本不改動平台預設版本物件(default_card):
    before = default_card.model_dump()
    merge_user_card(default_card, _user_card(default_card, identity={"age": 40}))
    assert default_card.model_dump() == before


def test_驗證_修改名字被拒絕(default_card):
    user = _user_card(default_card)
    user["identity"]["name"] = "別人"
    with pytest.raises(PersonaCardError, match="identity.name"):
        merge_user_card(default_card, user)


def test_驗證_名字與共用角色相同時接受(default_card):
    user = _user_card(default_card)
    user["identity"]["name"] = default_card.identity.name
    assert merge_user_card(default_card, user).identity.name == default_card.identity.name


@pytest.mark.parametrize("field", ["slug", "safety_note", "meta"])
def test_驗證_不允許的頂層欄位被拒絕(default_card, field):
    """safety_note 屬於平台——使用者不可藉由自己的版本改寫它。"""
    user = _user_card(default_card)
    user[field] = "x"
    with pytest.raises(PersonaCardError, match=field):
        merge_user_card(default_card, user)


def test_驗證_缺少必要欄位時指出完整路徑(default_card):
    user = _user_card(default_card)
    del user["personality"]["core_traits"]
    with pytest.raises(PersonaCardError, match=r"personality\.core_traits"):
        merge_user_card(default_card, user)


def test_驗證_數值超出範圍時指出欄位(default_card):
    user = _user_card(default_card, language_style={"typo_rate": 5})
    with pytest.raises(PersonaCardError, match=r"language_style\.typo_rate"):
        merge_user_card(default_card, user)


def test_驗證_清單為空時拒絕(default_card):
    user = _user_card(default_card, background={"interests": []})
    with pytest.raises(PersonaCardError, match=r"background\.interests"):
        merge_user_card(default_card, user)


def test_驗證_內容不是對應表時拒絕(default_card):
    with pytest.raises(PersonaCardError):
        merge_user_card(default_card, ["不是", "對應表"])


# ═══════════════════════════════════════════════════════════
#  保存與選卡（4.1～4.4）
# ═══════════════════════════════════════════════════════════

def test_沒有版本時套用平台預設版本(db, link, default_card):
    assert resolve_card(db, default_card, link) is default_card


def test_沒有連結時套用平台預設版本(db, default_card):
    assert resolve_card(db, default_card, None) is default_card


def test_保存後立即套用使用者版本(db, link, default_card):
    user = _user_card(default_card, personality={"core_traits": "活潑外向、愛分享"})
    save_user_card(db, default_card, link, user)
    db.commit()

    card = resolve_card(db, default_card, link)
    assert card.personality.core_traits == "活潑外向、愛分享"
    assert card.identity.name == default_card.identity.name


def test_存進資料庫的內容不含名字(db, link, default_card):
    """共用角色改名時，使用者版本不需要跟著改。"""
    user = _user_card(default_card)
    user["identity"]["name"] = default_card.identity.name
    user["identity"]["ig_display_name"] = default_card.identity.ig_display_name
    save_user_card(db, default_card, link, user)
    db.commit()

    stored = repo.get_user_version(db, link.id).card
    assert "name" not in stored["identity"]
    assert "ig_display_name" not in stored["identity"]


def test_再次保存覆寫同一列(db, link, default_card):
    save_user_card(db, default_card, link, _user_card(default_card, identity={"age": 30}))
    save_user_card(db, default_card, link, _user_card(default_card, identity={"age": 31}))
    db.commit()

    assert db.query(UserPersonaVersion).count() == 1
    assert resolve_card(db, default_card, link).identity.age == 31


def test_保存失敗時原版本不變且不寫入(db, link, default_card):
    save_user_card(db, default_card, link, _user_card(default_card, identity={"age": 30}))
    db.commit()

    bad = _user_card(default_card, identity={"age": 99})
    del bad["personality"]["core_traits"]
    with pytest.raises(PersonaCardError):
        save_user_card(db, default_card, link, bad)
    db.rollback()

    assert resolve_card(db, default_card, link).identity.age == 30


def test_資料庫中的壞版本退回平台預設版本且不拋例外(db, link, default_card, caplog):
    """4.4：一份壞資料不能讓回覆中斷。"""
    repo.save_user_version(db, link, {"personality": {"initiative": "高"}})  # 缺一堆必要欄位
    db.commit()

    assert resolve_card(db, default_card, link) is default_card


def test_兩位使用者的版本互不影響(db, link, default_card):
    other = _second_link(db, link.persona_id, 2)
    save_user_card(db, default_card, link, _user_card(default_card, identity={"age": 30}))
    save_user_card(db, default_card, other, _user_card(default_card, identity={"age": 35}))
    db.commit()

    assert resolve_card(db, default_card, link).identity.age == 30
    assert resolve_card(db, default_card, other).identity.age == 35


def test_一位使用者保存不影響沒有版本的使用者(db, link, default_card):
    other = _second_link(db, link.persona_id, 2)
    save_user_card(db, default_card, link, _user_card(default_card, identity={"age": 30}))
    db.commit()

    assert resolve_card(db, default_card, other) is default_card


def test_非上線的共用角色不接收訊息(db, link):
    """4.3：路由層本來就只認上線狀態的角色（repository.get_persona_by_ig_account）。"""
    assert repo.get_persona_by_ig_account(db, IG_ACCOUNT_ID) is not None

    persona = repo.get_persona_by_ig_account(db, IG_ACCOUNT_ID)
    for status in (PersonaStatus.PAUSED, PersonaStatus.DISABLED, PersonaStatus.REVIEWING):
        persona.status = status
        db.commit()
        assert repo.get_persona_by_ig_account(db, IG_ACCOUNT_ID) is None


# ═══════════════════════════════════════════════════════════
#  整合：完整 webhook 管線
# ═══════════════════════════════════════════════════════════

def _link_for(igsid: str):
    from app.db.models import Persona, UserPersonaLink
    from app.db.session import db_session

    with db_session() as db:
        persona = db.query(Persona).filter(Persona.ig_account_id == IG_ACCOUNT_ID).one()
        link = (
            db.query(UserPersonaLink)
            .filter(UserPersonaLink.persona_id == persona.id, UserPersonaLink.igsid == igsid)
            .one()
        )
        return link.id


def _save_for(client, igsid: str, **overrides) -> None:
    """以該使用者的身分保存版本——走與設定頁相同的驗證與保存函式。"""
    from app.db.models import UserPersonaLink
    from app.db.session import db_session

    main = client._main_module
    default_card = main.persona_cards["sunny"]
    with db_session() as db:
        link = db.get(UserPersonaLink, _link_for(igsid))
        save_user_card(db, default_card, link, _user_card(default_card, **overrides))


def test_整合_保存後下一則訊息起回覆依使用者版本(make_client):
    client = make_client(replies=["第一句。", "第二句。"])
    igsid = "IGSID_E2E_1"
    _post(client, "mid_1", "嗨嗨", igsid)
    first_prompt = client._fake_provider.calls[0]["system_prompt"]
    assert "溫柔療癒" in first_prompt  # 平台預設版本

    _save_for(client, igsid, personality={"core_traits": "活潑外向、愛分享"})
    _post(client, "mid_2", "在嗎", igsid)

    second_prompt = client._fake_provider.calls[1]["system_prompt"]
    assert "活潑外向、愛分享" in second_prompt
    assert "溫柔療癒" not in second_prompt
    assert "晴晴" in second_prompt  # 名字仍是共用角色的


def test_整合_另一位使用者仍得到平台預設版本(make_client):
    client = make_client(replies=["回覆。"])
    _post(client, "mid_a", "嗨", "IGSID_E2E_A")
    _post(client, "mid_b", "嗨", "IGSID_E2E_B")
    _save_for(client, "IGSID_E2E_A", personality={"core_traits": "活潑外向、愛分享"})

    _post(client, "mid_b2", "在嗎", "IGSID_E2E_B")
    prompt_for_b = client._fake_provider.calls[-1]["system_prompt"]
    assert "溫柔療癒" in prompt_for_b
    assert "活潑外向、愛分享" not in prompt_for_b


def test_整合_壞掉的版本不影響回覆(make_client):
    from app.db.models import UserPersonaLink
    from app.db.session import db_session

    client = make_client(replies=["第一句。", "第二句。"])
    igsid = "IGSID_E2E_BAD"
    _post(client, "mid_1", "嗨嗨", igsid)

    with db_session() as db:
        link = db.get(UserPersonaLink, _link_for(igsid))
        repo.save_user_version(db, link, {"personality": {"initiative": "高"}})

    resp = _post(client, "mid_2", "在嗎", igsid)
    assert resp.status_code == 200
    assert "溫柔療癒" in client._fake_provider.calls[1]["system_prompt"]


def test_整合_訊號偵測使用使用者版本的邊界(make_client):
    client = make_client(replies=["嗨，你好呀。"])
    igsid = "IGSID_E2E_SIGNALS"
    _post(client, "mid_1", "你週末都去哪玩？", igsid)

    custom = "一直問她的收入"
    default_card = client._main_module.persona_cards["sunny"]
    boundaries = _user_card(default_card)["boundaries"]
    boundaries["cools_down_on"] = [*boundaries["cools_down_on"], custom]
    _save_for(client, igsid, boundaries=boundaries)

    _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    detection_prompt = client._fake_analysis_provider.calls[1]["system_prompt"]
    assert custom in detection_prompt


def test_整合_沒有版本的使用者訊號偵測沿用平台預設邊界(make_client):
    client = make_client(replies=["嗨，你好呀。"])
    igsid = "IGSID_E2E_SIGNALS_DEFAULT"
    _post(client, "mid_1", "你週末都去哪玩？", igsid)

    _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())

    detection_prompt = client._fake_analysis_provider.calls[1]["system_prompt"]
    assert "評論她的身材或外貌" in detection_prompt
