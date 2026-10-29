"""教練設定的套用（add-persona-authoring-console 第 7 群）。

7.1 訊號偵測依使用者版本的規則設定：停用的預設規則不偵測、自訂規則加入並帶上維度
7.2 沒有使用者版本、或沒有教練設定時，行為與導入前一致（提示逐字相同）
7.3 報告的語氣、篇幅、段落依使用者設定；未設定者沿用原本五段

不連外網：LLMRouter 換成假的 provider，只檢查送進模型的提示與解析結果。
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import repository as repo
from app.db.models import Base, Message, MessageDirection, Severity, SignalType
from app.dialog.coach_report import (
    REPORT_SECTIONS,
    build_system_prompt as build_report_prompt,
    display_sections,
    generate_report,
    parse_report,
    report_sections,
)
from app.dialog.signals import (
    SIGNAL_ITEMS,
    build_rule_set,
    build_system_prompt as build_signal_prompt,
    detect_signals,
    parse_signals,
)
from app.llm.base import LLMProvider, LLMResponse
from app.llm.router import LLMRouter
from app.personas.coaching_settings import (
    coaching_to_columns,
    default_coaching,
    session_coaching,
)
from tests.test_signals import BOUNDARIES, T0, _user


class _Recorder(LLMProvider):
    """記下送進模型的提示與 max_output_tokens，回固定文字。"""

    def __init__(self, text: str) -> None:
        self.calls: list[dict] = []
        self._text = text

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-1"

    async def generate(self, *, system_prompt, messages, temperature=0.9, max_output_tokens=1024):
        self.calls.append({"system_prompt": system_prompt, "max_output_tokens": max_output_tokens})
        return LLMResponse(text=self._text, provider="fake", model=self.model, elapsed_ms=1)


def _coaching(**changes) -> dict:
    coaching = default_coaching()
    coaching.update(copy.deepcopy(changes))
    return coaching


CUSTOM_LANDMINE = {
    "id": "custom-1",
    "text": "一直提到自己的薪水",
    "type": "landmine",
    "dimension": "自我揭露平衡",
    "severity": "high",
    "custom": True,
}
CUSTOM_HIGHLIGHT = {
    "id": "custom-2",
    "text": "記得對方上次提過的事",
    "type": "highlight",
    "dimension": "接話與延展",
    "severity": None,
    "custom": True,
}


# ═══════════════════════════════════════════════════════════
#  7.1／7.2 訊號偵測
# ═══════════════════════════════════════════════════════════


def test_沒有教練設定時沿用系統預設規則():
    assert build_rule_set(None) is SIGNAL_ITEMS


def test_教練設定與預設相同時提示與導入前逐字相同():
    """7.2：存過設定但沒改規則的使用者，偵測行為不能有任何差異。"""
    items = build_rule_set(default_coaching())
    assert items == SIGNAL_ITEMS
    assert build_signal_prompt(BOUNDARIES, items) == build_signal_prompt(BOUNDARIES)


def test_停用的預設規則不出現在提示也不被採用():
    items = build_rule_set(_coaching(disabledRuleIds=["L4", "K1"]))
    prompt = build_signal_prompt(BOUNDARIES, items)

    assert "L4：" not in prompt
    assert "K1：" not in prompt
    # K1 的判定要點跟著拿掉，免得模型去找清單上沒有的項目
    assert "- K1：" not in prompt
    assert "L5：" in prompt

    output = json.dumps([
        {"message_id": 1, "item": "L4", "reason": "評論外貌"},
        {"message_id": 1, "item": "L5", "reason": "貶低型玩笑"},
    ])
    signals = parse_signals(output, {1}, items)
    assert [s.item for s in signals] == ["L5"]


def test_自訂規則以C代號加入並由設定填入類型嚴重程度與維度():
    items = build_rule_set(_coaching(customRules=[CUSTOM_LANDMINE, CUSTOM_HIGHLIGHT]))
    prompt = build_signal_prompt(BOUNDARIES, items)

    assert "C1：一直提到自己的薪水（地雷）" in prompt
    assert "C2：記得對方上次提過的事（亮點）" in prompt
    assert "C 開頭是使用者自訂的項目" in prompt

    output = json.dumps([
        {"message_id": 1, "item": "C1", "reason": "「我月薪十萬」"},
        {"message_id": 2, "item": "c2", "reason": "「你上次說的貓還好嗎」"},
        {"message_id": 2, "item": "C9", "reason": "不存在的代號"},
    ])
    signals = parse_signals(output, {1, 2}, items)

    assert len(signals) == 2
    landmine, highlight = signals
    assert (landmine.signal_type, landmine.severity, landmine.dimension) == (
        SignalType.LANDMINE, Severity.HIGH, "自我揭露平衡"
    )
    assert (highlight.signal_type, highlight.severity, highlight.dimension) == (
        SignalType.HIGHLIGHT, None, "接話與延展"
    )


def test_停用的自訂規則不偵測():
    items = build_rule_set(
        _coaching(customRules=[CUSTOM_LANDMINE, CUSTOM_HIGHLIGHT], disabledRuleIds=["custom-1"])
    )
    assert [code for code in items if code.startswith("C")] == ["C1"]
    assert items["C1"].description == "記得對方上次提過的事"


def test_資料庫裡不合法的自訂規則被略過():
    bad = dict(CUSTOM_LANDMINE, dimension="不存在的維度")
    items = build_rule_set(_coaching(customRules=[bad]))
    assert not any(code.startswith("C") for code in items)


def test_停用L10時不判斷訊息轟炸():
    burst = [_user(1, "欸", 0), _user(2, "在嗎", 5), _user(3, "？", 10)]
    router = LLMRouter({"analysis": _Recorder("[]")})
    common = dict(session_number=1, burst_window_seconds=60, burst_min_messages=3)

    on = asyncio.run(detect_signals(router, burst, BOUNDARIES, **common))
    off = asyncio.run(
        detect_signals(router, burst, BOUNDARIES, coaching=_coaching(disabledRuleIds=["L10"]), **common)
    )

    assert [s.item for s in on.signals] == ["L10"]
    assert off.ok and off.signals == []


def test_偵測時自訂規則的訊號帶著設定的維度():
    messages = [
        Message(id=1, session_id=1, direction=MessageDirection.INBOUND, content="我月薪十萬", sent_at=T0),
    ]
    provider = _Recorder(json.dumps([{"message_id": 1, "item": "C1", "reason": "「我月薪十萬」"}]))
    result = asyncio.run(
        detect_signals(
            LLMRouter({"analysis": provider}),
            messages,
            BOUNDARIES,
            session_number=1,
            burst_window_seconds=60,
            burst_min_messages=3,
            coaching=_coaching(customRules=[CUSTOM_LANDMINE]),
        )
    )
    assert result.ok
    assert [(s.signal_type, s.dimension) for s in result.signals] == [
        (SignalType.LANDMINE, "自我揭露平衡")
    ]
    assert "C1：一直提到自己的薪水" in provider.calls[0]["system_prompt"]


# ═══════════════════════════════════════════════════════════
#  7.3 教練報告
# ═══════════════════════════════════════════════════════════

FIVE = {k: f"{k}的內容" for k in REPORT_SECTIONS}


def _without(*keys: str) -> list[dict]:
    return [s for s in default_coaching()["reportSections"] if s["key"] not in keys]


def test_沒有設定或與預設相同時報告提示與導入前逐字相同():
    assert build_report_prompt([], "晴晴", default_coaching()) == build_report_prompt([], "晴晴")


def test_取消的段落不寫也不送出():
    """使用者取消「下次可以試試」，模型就算多寫了，也不能出現在送出的報告裡。"""
    coaching = _coaching(reportSections=_without("下次可試"))
    prompt = build_report_prompt([], "晴晴", coaching)
    assert "下次可試" not in prompt
    assert "只寫下面這 4 段" in prompt

    keys = [s.key for s in report_sections(coaching)]
    content = parse_report(json.dumps(FIVE, ensure_ascii=False), keys)
    titles = [s["title"] for s in display_sections(content)]

    assert list(content) == ["摘要", "做得好", "可調整", "改寫示範"]
    assert "下次可以試試" not in titles
    assert titles[0] == "這次聊起來的感覺"


def test_自訂段落以標題為鍵並照設定順序顯示():
    custom = {"key": "custom-1", "title": "她可能在想什麼", "description": "從她的角度看這段對話", "custom": True}
    coaching = _coaching(reportSections=_without("改寫示範", "下次可試") + [custom])
    keys = [s.key for s in report_sections(coaching)]
    assert keys == ["摘要", "做得好", "可調整", "她可能在想什麼"]

    prompt = build_report_prompt([], "晴晴", coaching)
    assert "4. 她可能在想什麼：從她的角度看這段對話" in prompt

    content = parse_report(json.dumps({k: f"{k}…" for k in keys}, ensure_ascii=False), keys)
    titles = [s["title"] for s in display_sections(content)]
    assert titles.index("可以再調整的地方") < titles.index("她可能在想什麼")


def test_自訂段落標題與預設段落的鍵相同時加上編號():
    """標題剛好是「摘要」：不加編號會覆蓋預設段落的內容，顯示時也會被換成預設標題。"""
    custom = {"key": "custom-1", "title": "摘要", "description": "", "custom": True}
    keys = [s.key for s in report_sections(_coaching(reportSections=_without() + [custom]))]
    assert keys[:5] == list(REPORT_SECTIONS)
    assert keys[5] == "摘要（2）"


def test_語氣與篇幅寫進報告提示():
    coaching = _coaching(reportTone="直接具體：重點先講，不繞圈子", reportLength="1 則，約 300 字")
    prompt = build_report_prompt([], "晴晴", coaching)
    assert "語氣：直接具體：重點先講，不繞圈子" in prompt
    assert "約 300 字" in prompt


def test_generate_report依設定生成且只有預設時維持原本的輸出上限():
    four = {k: f"{k}…" for k in REPORT_SECTIONS if k != "下次可試"}

    provider = _Recorder(json.dumps(four, ensure_ascii=False))
    coaching = _coaching(reportSections=_without("下次可試"))
    result = asyncio.run(generate_report(LLMRouter({"coaching": provider}), [], "晴晴", coaching))
    assert result.ok and list(result.content) == list(four)
    assert provider.calls[0]["max_output_tokens"] == 2048

    provider = _Recorder(json.dumps(FIVE, ensure_ascii=False))
    result = asyncio.run(generate_report(LLMRouter({"coaching": provider}), [], "晴晴"))
    assert result.ok and list(result.content) == list(REPORT_SECTIONS)
    assert provider.calls[0]["max_output_tokens"] == 1024


def test_舊報告的內容格式照常顯示():
    """導入前存下的報告是固定五段的鍵，網頁上照樣換成中文標題。"""
    sections = display_sections(FIVE)
    assert sections[0] == {"title": "這次聊起來的感覺", "body": "摘要的內容"}
    assert sections[-1]["title"] == "下次可以試試"


# ═══════════════════════════════════════════════════════════
#  依 session 取用使用者版本的教練設定
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session


def _session_for(db, igsid: str):
    persona = repo.upsert_persona(db, slug="sunny", name="晴晴", gender="female", ig_account_id="IGA")
    user, link, _ = repo.resolve_user(db, persona_id=persona.id, igsid=igsid, ig_username=igsid)
    session, _ = repo.get_or_create_session(db, user.id, persona.id)
    db.commit()
    return session, link


def test_沒有使用者版本時取不到教練設定(db):
    session, _link = _session_for(db, "U1")
    assert session_coaching(db, session) is None


def test_有版本但沒有教練設定時取不到教練設定(db):
    session, link = _session_for(db, "U2")
    repo.save_user_version(db, link, {"identity": {"age": 26}})
    db.commit()
    assert session_coaching(db, session) is None


def test_取得該使用者版本的教練設定(db):
    session, link = _session_for(db, "U3")
    version = repo.save_user_version(db, link, {"identity": {"age": 26}})
    coaching = _coaching(disabledRuleIds=["L4"], reportSections=_without("下次可試"))
    repo.save_coaching(db, version, coaching_to_columns(coaching))
    db.commit()

    got = session_coaching(db, session)
    assert got["disabledRuleIds"] == ["L4"]
    assert [s["key"] for s in got["reportSections"]] == ["摘要", "做得好", "可調整", "改寫示範"]


def test_教練設定只屬於該使用者(db):
    session_a, link_a = _session_for(db, "UA")
    session_b, _link_b = _session_for(db, "UB")
    version = repo.save_user_version(db, link_a, {"identity": {"age": 26}})
    repo.save_coaching(db, version, coaching_to_columns(_coaching(disabledRuleIds=["L4"])))
    db.commit()

    assert session_coaching(db, session_a)["disabledRuleIds"] == ["L4"]
    assert session_coaching(db, session_b) is None


# ═══════════════════════════════════════════════════════════
#  整合：私訊 → 靜默結束 → 產生報告（打開回饋連結時）→ 報告依使用者設定
# ═══════════════════════════════════════════════════════════

from tests.test_coach_report import (  # noqa: E402,F401  (make_client 為 fixture)
    IG_ACCOUNT_ID as REPORT_IG_ACCOUNT_ID,
    _backdate_session,
    _get_coach_report,
    _post as _post_message,
    make_client,
)


def test_取消段落後產生的報告沒有該段落(make_client):
    """真機回報的問題：設定頁取消「下次可以試試」，報告仍出現這一段。
    假模型照樣回五段，送出的報告也只能有四段。同時停用 L4，確認 session
    結束時的訊號偵測也讀到了同一份設定。"""
    from app.db.models import Persona, UserPersonaLink
    from app.db.session import db_session
    from app.dialog.report_service import READY, generate_session_report

    client = make_client()
    igsid = "IGSID_COACHING_1"
    _post_message(client, "mid_1", "我今天心情不太好", igsid)
    _post_message(client, "mid_2", "想找人聊聊", igsid)

    with db_session() as db:
        persona = db.query(Persona).filter(Persona.ig_account_id == REPORT_IG_ACCOUNT_ID).one()
        link = (
            db.query(UserPersonaLink)
            .filter(UserPersonaLink.persona_id == persona.id, UserPersonaLink.igsid == igsid)
            .one()
        )
        version = repo.save_user_version(db, link, {"identity": {"age": 26}})
        repo.save_coaching(
            db, version, coaching_to_columns(
                _coaching(disabledRuleIds=["L4"], reportSections=_without("下次可試"))
            )
        )

    session_id = _backdate_session(client._main_module, igsid, minutes=30)
    asyncio.run(client._main_module._run_silence_pass())
    # 回饋網頁打開時呼叫的就是這個（app/web/report_api.py）
    result = asyncio.run(generate_session_report(client._main_module.llm_router, session_id))

    assert result.status == READY
    report = _get_coach_report(client._main_module, session_id)
    assert list(report.content) == ["摘要", "做得好", "可調整", "改寫示範"]
    assert "下次可以試試" not in [s["title"] for s in display_sections(report.content)]
    assert "下次可試" not in client._fake_coaching.calls[0]["system_prompt"]

    signal_prompt = client._main_module.llm_router.provider_for("analysis").calls[-1]["system_prompt"]
    assert "L5：" in signal_prompt
    assert "L4：" not in signal_prompt
