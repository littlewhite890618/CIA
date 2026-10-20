"""人設卡測試（tasks 7.1–7.5）。

對應 openspec/changes/add-ig-persona-chat-mvp/specs/persona-chat/spec.md
的三個 Requirement：人設卡結構、AI 身分揭露、（角色回覆生成留待 7.6）。
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.personas.disclosure import compose_reply, disclosure_text, should_disclose
from app.personas.loader import CARDS_DIR, PersonaCardError, load_all_cards, load_persona_card
from app.personas.prompt import render_system_prompt
from app.personas.schema import (
    Background,
    Boundaries,
    DelaySeconds,
    Identity,
    LanguageStyle,
    Personality,
    PersonaCard,
    PersonaPromptFields,
    PersonaRuntimeParams,
)

MIN_CARD = {
    "identity": {"name": "測試", "ig_display_name": "測試 Test", "age": 20, "occupation": "學生"},
    "background": {"recent_life": "在忙期末考", "interests": ["打球"]},
    "personality": {"core_traits": "開朗", "initiative": "高"},
    "language_style": {"pace": "快", "message_length": "短", "particles": ["喔"], "emoji": "高頻"},
    "boundaries": {
        "warms_up_to": ["聊運動"],
        "cools_down_on": ["人身攻擊"],
        "never_engages": ["要聯絡方式"],
        "cooling_behavior": "回應變短",
    },
    "runtime": {"reply_delay_seconds": {"min": 1, "max": 5}},
}


def _write_card(tmp_path: Path, filename: str, content: dict) -> Path:
    path = tmp_path / filename
    path.write_text(yaml.safe_dump(content, allow_unicode=True), encoding="utf-8")
    return path


# ═══════════════════════════════════════════════════════════
#  7.1 人設卡結構
# ═══════════════════════════════════════════════════════════

def test_結構_最小合法卡片可以建構():
    card = PersonaCard.model_validate({**MIN_CARD, "slug": "test"})
    assert card.identity.name == "測試"
    assert card.runtime.reply_delay_seconds.min == 1


def test_結構_prompt_fields與runtime明確分離():
    """型別本身就是分離的保證：prompt_fields 回傳的物件完全不含 runtime 屬性。"""
    card = PersonaCard.model_validate({**MIN_CARD, "slug": "test"})
    fields = card.prompt_fields

    assert isinstance(fields, PersonaPromptFields)
    assert not hasattr(fields, "runtime")
    assert not hasattr(fields, "reply_delay_seconds")


def test_結構_延遲秒數min大於max時拒絕():
    bad = {**MIN_CARD, "runtime": {"reply_delay_seconds": {"min": 100, "max": 5}}}
    with pytest.raises(ValidationError):
        PersonaCard.model_validate({**bad, "slug": "test"})


def test_結構_興趣清單為空時拒絕():
    """興趣是話題延伸的來源，空清單等於角色沒有任何素材可聊。"""
    bad = {**MIN_CARD, "background": {**MIN_CARD["background"], "interests": []}}
    with pytest.raises(ValidationError):
        PersonaCard.model_validate({**bad, "slug": "test"})


def test_結構_邊界三清單缺一則拒絕():
    """依 design.md「產品的核心教學機制」，變熱絡/變冷淡/絕不接缺一不可。"""
    for missing in ("warms_up_to", "cools_down_on", "never_engages"):
        bad_boundaries = {k: v for k, v in MIN_CARD["boundaries"].items() if k != missing}
        with pytest.raises(ValidationError):
            PersonaCard.model_validate({**MIN_CARD, "boundaries": bad_boundaries, "slug": "test"})


# ═══════════════════════════════════════════════════════════
#  7.2 人設卡載入與驗證
# ═══════════════════════════════════════════════════════════

def test_載入_合法檔案成功(tmp_path):
    path = _write_card(tmp_path, "test.yaml", MIN_CARD)
    card = load_persona_card(path)
    assert card.slug == "test"  # 取自檔名，不是 YAML 內容
    assert card.identity.name == "測試"


def test_載入_缺少必要欄位時報錯且指出欄位名(tmp_path):
    incomplete = {k: v for k, v in MIN_CARD.items() if k != "background"}
    path = _write_card(tmp_path, "broken.yaml", incomplete)

    with pytest.raises(PersonaCardError) as exc_info:
        load_persona_card(path)

    message = str(exc_info.value)
    assert "background" in message
    assert "broken.yaml" in message


def test_載入_巢狀欄位缺漏時指出完整路徑(tmp_path):
    """要指到 background.interests，不能只說「background 有問題」。"""
    incomplete = {
        **MIN_CARD,
        "background": {"recent_life": "x"},  # 缺 interests
    }
    path = _write_card(tmp_path, "broken.yaml", incomplete)

    with pytest.raises(PersonaCardError) as exc_info:
        load_persona_card(path)

    assert "background.interests" in str(exc_info.value)


def test_載入_不合法的yaml語法時報錯(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text("identity: [這不是合法的\n  縮排結構", encoding="utf-8")

    with pytest.raises(PersonaCardError):
        load_persona_card(path)


def test_載入_內容非對應表時報錯(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- 這是一個清單\n- 不是 mapping", encoding="utf-8")

    with pytest.raises(PersonaCardError):
        load_persona_card(path)


def test_載入_目錄下多張卡全部成功才回傳(tmp_path):
    _write_card(tmp_path, "a.yaml", MIN_CARD)
    _write_card(tmp_path, "b.yaml", MIN_CARD)

    cards = load_all_cards(tmp_path)

    assert set(cards) == {"a", "b"}


def test_載入_任一張卡壞掉整批失敗(tmp_path):
    """不可略過壞掉的卡繼續啟動——那會讓某個角色悄悄停擺沒人注意。"""
    _write_card(tmp_path, "good.yaml", MIN_CARD)
    incomplete = {k: v for k, v in MIN_CARD.items() if k != "personality"}
    _write_card(tmp_path, "bad.yaml", incomplete)

    with pytest.raises(PersonaCardError):
        load_all_cards(tmp_path)


def test_載入_真實的晴晴卡片可以成功載入():
    """回歸測試：確保 app/personas/cards/sunny.yaml（task 7.4）本身合法。"""
    cards = load_all_cards(CARDS_DIR)
    assert "sunny" in cards
    sunny = cards["sunny"]
    assert sunny.identity.name == "晴晴"
    assert sunny.runtime.reply_delay_seconds.min == 8
    assert sunny.runtime.reply_delay_seconds.max == 20


def test_載入_晴晴卡片slug與main模組seed的slug一致():
    """main.py 啟動時以 slug="sunny" seed 資料庫角色列，兩邊對不上
    角色路由會失效。這個測試把兩者的耦合關係明確地釘住。
    """
    import app.main as main_module

    cards = load_all_cards(CARDS_DIR)
    assert "sunny" in cards
    assert "sunny" in inspect.getsource(main_module.lifespan)


# ═══════════════════════════════════════════════════════════
#  7.3 人設卡至 system prompt 的渲染
# ═══════════════════════════════════════════════════════════

def _prompt_fields(**overrides) -> PersonaPromptFields:
    data = {
        "identity": Identity(name="小美", ig_display_name="小美", age=25, occupation="設計師"),
        "background": Background(recent_life="在趕案子", interests=["畫畫"]),
        "personality": Personality(core_traits="活潑", initiative="高"),
        "language_style": LanguageStyle(pace="快", message_length="短", particles=["耶"], emoji="高頻"),
        "boundaries": Boundaries(
            warms_up_to=["聊設計"], cools_down_on=["酸言酸語"], never_engages=["約見面"], cooling_behavior="變得敷衍"
        ),
    }
    data.update(overrides)
    return PersonaPromptFields(**data)


def test_渲染_只接受PromptFields型別():
    """簽章層級的保證：想傳完整 PersonaCard 進去會直接被型別檢查擋下。

    模組有 `from __future__ import annotations`，簽章上的型別在執行期
    只是字串，所以用 typing.get_type_hints 解析成真正的型別物件再比對，
    而不是直接比對 Parameter.annotation（那會比到字串）。

    known_topics（task 6.7）是合法的新參數——它是使用者跨 session 的
    動態資料，不是人設卡內容，這裡只需確認它沒有把 PersonaCard 或
    PersonaRuntimeParams 夾帶進來；`fields` 仍鎖死是 PersonaPromptFields
    才是這項測試真正要守住的事。
    """
    import typing

    params = inspect.signature(render_system_prompt).parameters
    assert list(params) == [
        "fields", "known_topics", "previous_conversation", "conversation_summary"
    ]

    hints = typing.get_type_hints(render_system_prompt)
    assert hints["fields"] is PersonaPromptFields
    assert hints["known_topics"] == (list[str] | None)
    assert hints["previous_conversation"] == (list[str] | None)
    assert hints["conversation_summary"] == (str | None)  # 任務 8：場次摘要，動態資料


def test_渲染_runtime欄位標籤不會出現在輸出中(tmp_path):
    card = PersonaCard.model_validate({**MIN_CARD, "slug": "test"})
    prompt = render_system_prompt(card.prompt_fields)

    for leaked_label in ("reply_delay_seconds", "回覆延遲秒數", "typing_indicator"):
        assert leaked_label not in prompt


def test_渲染_內容包含身分與背景():
    fields = _prompt_fields()
    prompt = render_system_prompt(fields)

    assert "小美" in prompt
    assert "設計師" in prompt
    assert "在趕案子" in prompt
    assert "畫畫" in prompt


def test_渲染_邊界反應三類都出現():
    fields = _prompt_fields()
    prompt = render_system_prompt(fields)

    assert "聊設計" in prompt
    assert "酸言酸語" in prompt
    assert "約見面" in prompt


def test_渲染_語助詞附上使用頻率():
    """只列語助詞時模型會每則都用（2026-10-16 真機測試），要明寫偶爾用。"""
    prompt = render_system_prompt(_prompt_fields())
    line = next(l for l in prompt.splitlines() if l.startswith("常用語助詞："))
    assert line.endswith("（偶爾用，大約三四則用一次，不要每則都用）")


def test_載入_晴晴的熱絡清單包含道歉():
    """使用者道歉時要能回暖（2026-10-16 真機測試道歉兩次仍維持冷淡）。"""
    card = load_all_cards(CARDS_DIR)["sunny"]
    assert any("道歉" in item for item in card.boundaries.warms_up_to)


def test_渲染_不同語言風格產生可辨識的差異():
    """spec 情境：訊息長度、語助詞、emoji 使用要呈現可辨識差異。"""
    talkative = _prompt_fields(
        language_style=LanguageStyle(pace="快", message_length="長，常常一次講很多", particles=["哈哈", "欸"], emoji="超高頻")
    )
    reserved = _prompt_fields(
        language_style=LanguageStyle(pace="慢", message_length="短，惜字如金", particles=["嗯"], emoji="幾乎不用")
    )

    p1 = render_system_prompt(talkative)
    p2 = render_system_prompt(reserved)

    assert p1 != p2
    assert "哈哈" in p1 and "哈哈" not in p2
    assert "惜字如金" in p2 and "惜字如金" not in p1


# ═══════════════════════════════════════════════════════════
#  7.5 AI 身分揭露
# ═══════════════════════════════════════════════════════════

def test_揭露_新場次必定揭露():
    assert should_disclose(
        is_new_session=True,
        last_activity_at=datetime.now(timezone.utc),
        now=datetime.now(timezone.utc),
        silence_threshold_seconds=1500,
    )


def test_揭露_間隔超過門檻時揭露():
    now = datetime.now(timezone.utc)
    last = now - timedelta(seconds=1600)

    assert should_disclose(
        is_new_session=False, last_activity_at=last, now=now, silence_threshold_seconds=1500
    )


def test_揭露_間隔未達門檻時不揭露():
    now = datetime.now(timezone.utc)
    last = now - timedelta(seconds=100)

    assert not should_disclose(
        is_new_session=False, last_activity_at=last, now=now, silence_threshold_seconds=1500
    )


def test_揭露_剛好等於門檻時揭露():
    """邊界值：spec 用詞是「達」設定時長，等於也算數，不是必須超過。"""
    now = datetime.now(timezone.utc)
    last = now - timedelta(seconds=1500)

    assert should_disclose(
        is_new_session=False, last_activity_at=last, now=now, silence_threshold_seconds=1500
    )


def test_揭露_無時區資訊的時間視為UTC():
    """SQLite 讀回的 datetime 常常沒有 tzinfo，不能因此整個炸掉或算錯。"""
    now = datetime.now(timezone.utc)
    naive_last = (now - timedelta(seconds=1600)).replace(tzinfo=None)

    assert should_disclose(
        is_new_session=False,
        last_activity_at=naive_last,
        now=now,
        silence_threshold_seconds=1500,
    )


def test_組合回覆_需要揭露時前綴文字並保留原內容():
    text = compose_reply("今天過得如何？", disclose=True, persona_name="晴晴")

    assert text.startswith(disclosure_text("晴晴"))
    assert "今天過得如何？" in text
    assert "晴晴" in text


def test_組合回覆_不需要揭露時原樣回傳():
    text = compose_reply("今天過得如何？", disclose=False, persona_name="晴晴")
    assert text == "今天過得如何？"


def test_揭露_無法被設定關閉_結構保證():
    """persona-chat spec：「嘗試停用揭露 → 系統忽略該設定仍執行揭露」。

    這裡驗證的是比「忽略某個設定值」更強的保證：should_disclose 與
    compose_reply 的函式簽章裡根本沒有任何用來關閉揭露的參數存在，
    未來要繞過去就必須主動修改這裡的簽章，不會是不小心接對一個
    既有參數就達成的事。
    """
    disclose_params = set(inspect.signature(should_disclose).parameters)
    compose_params = set(inspect.signature(compose_reply).parameters)

    disable_like_names = {"enabled", "disabled", "disable", "skip", "off", "force_disable"}
    assert disclose_params.isdisjoint(disable_like_names)
    assert compose_params.isdisjoint(disable_like_names)


def test_揭露文字明確表明AI身分():
    text = disclosure_text("晴晴")
    assert "AI" in text
    assert "晴晴" in text


def test_渲染_有前場次對話時附上原文():
    prompt = render_system_prompt(
        _prompt_fields(), previous_conversation=["使用者：我被主管罵，他姓白", "你：辛苦了"]
    )
    assert "上次聊天的最後幾則" in prompt
    assert "使用者：我被主管罵，他姓白" in prompt


def test_渲染_沒有前場次對話時不出現該段落():
    assert "上次聊天的最後幾則" not in render_system_prompt(_prompt_fields())
