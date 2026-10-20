"""資料存取層測試。

重點驗證兩個最容易出錯、且錯了會直接影響產品的行為：
  1. 跨角色歸戶 —— 錯了會讓使用者換角色即輪廓歸零
  2. 訊息去重   —— 錯了會讓角色對同一句話回覆兩次
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import repository as repo
from app.db.models import Base, SessionState


@pytest.fixture
def db():
    """每個測試一個乾淨的記憶體資料庫。"""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def sunny(db):
    return repo.upsert_persona(
        db, slug="sunny", name="晴晴", gender="female", ig_account_id="17841400000001"
    )


@pytest.fixture
def aze(db):
    """第二個角色，用於驗證跨角色歸戶。"""
    return repo.upsert_persona(
        db, slug="aze", name="阿哲", gender="male", ig_account_id="17841400000002"
    )


# ── 角色路由 ────────────────────────────────────────────────

def test_角色路由_依帳號ID找出角色(db, sunny):
    found = repo.get_persona_by_ig_account(db, "17841400000001")
    assert found is not None
    assert found.slug == "sunny"


def test_角色路由_未知帳號回傳None(db, sunny):
    assert repo.get_persona_by_ig_account(db, "99999999999") is None


# ── 使用者身分歸戶 ──────────────────────────────────────────

def test_首次互動_建立使用者與連結(db, sunny):
    user, link, is_new = repo.resolve_user(
        db, persona_id=sunny.id, igsid="IGSID_A", ig_username="ming0123"
    )
    assert is_new is True
    assert user.ig_username == "ming0123"
    assert user.identity_confirmed is True
    assert link.igsid == "IGSID_A"


def test_再次互動_取回同一使用者不重複建立(db, sunny):
    user1, _, _ = repo.resolve_user(
        db, persona_id=sunny.id, igsid="IGSID_A", ig_username="ming0123"
    )
    user2, _, is_new = repo.resolve_user(
        db, persona_id=sunny.id, igsid="IGSID_A", ig_username="ming0123"
    )
    assert is_new is False
    assert user1.id == user2.id


def test_跨角色歸戶_不同IGSID仍認出同一人(db, sunny, aze):
    """同一個人跟兩個角色聊，Instagram 給的編號完全不同，
    但 username 不變，因此應歸戶到同一個使用者。"""
    user_a, link_a, _ = repo.resolve_user(
        db, persona_id=sunny.id, igsid="IGSID_晴晴那邊", ig_username="ming0123"
    )
    user_b, link_b, is_new = repo.resolve_user(
        db, persona_id=aze.id, igsid="IGSID_阿哲那邊", ig_username="ming0123"
    )

    assert is_new is False, "第二次應該認出是同一人，不是新使用者"
    assert user_a.id == user_b.id, "兩個角色下應歸戶到同一個使用者"
    assert link_a.igsid != link_b.igsid, "但 IGSID 應該不同"
    assert link_a.persona_id != link_b.persona_id


def test_無法取得username_仍可運作但標記未確認(db, sunny):
    """使用者封鎖 App 或 API 失敗時，仍要能對話。"""
    user, link, _ = repo.resolve_user(
        db, persona_id=sunny.id, igsid="IGSID_X", ig_username=None
    )
    assert user.identity_confirmed is False
    assert user.ig_username is None
    assert link.igsid == "IGSID_X"


def test_話題記憶掛在連結上而非使用者(db, sunny, aze):
    """角色的記憶應各自獨立 —— 阿哲不該知道使用者跟晴晴聊過什麼。"""
    _, link_a, _ = repo.resolve_user(
        db, persona_id=sunny.id, igsid="IG_A", ig_username="ming0123"
    )
    _, link_b, _ = repo.resolve_user(
        db, persona_id=aze.id, igsid="IG_B", ig_username="ming0123"
    )
    link_a.topic_memory = ["喜歡爬山"]
    db.flush()

    assert link_b.topic_memory == [], "另一個角色不應看到這段記憶"


# ── 對話場次 ────────────────────────────────────────────────

def test_場次_建立與延續(db, sunny):
    user, _, _ = repo.resolve_user(db, persona_id=sunny.id, igsid="IG_A", ig_username="u1")

    s1, is_new1 = repo.get_or_create_session(db, user.id, sunny.id)
    s2, is_new2 = repo.get_or_create_session(db, user.id, sunny.id)

    assert is_new1 is True
    assert is_new2 is False
    assert s1.id == s2.id


def test_場次結束後再發訊_建立新場次(db, sunny):
    user, _, _ = repo.resolve_user(db, persona_id=sunny.id, igsid="IG_A", ig_username="u1")
    s1, _ = repo.get_or_create_session(db, user.id, sunny.id)
    repo.end_session(db, s1)

    s2, is_new = repo.get_or_create_session(db, user.id, sunny.id)
    assert is_new is True
    assert s1.id != s2.id
    assert s1.state == SessionState.ENDED


def test_視窗與靜默時間分開_點按鈕只重置視窗(db, sunny):
    """點按鈕會重置 24 小時視窗，但不算「說話」，
    靜默判定仍應從最後一則訊息起算。"""
    user, _, _ = repo.resolve_user(db, persona_id=sunny.id, igsid="IG_A", ig_username="u1")
    session, _ = repo.get_or_create_session(db, user.id, sunny.id)

    original_last_message = session.last_message_at
    original_window = session.window_expires_at

    repo.touch_window_only(db, session)

    assert session.last_message_at == original_last_message, "點按鈕不應更新最後活動時間"
    assert session.window_expires_at > original_window, "但應延長視窗"


def test_靜默掃描_找出逾門檻的場次(db, sunny):
    from datetime import timedelta

    from app.db.models import utcnow

    user, _, _ = repo.resolve_user(db, persona_id=sunny.id, igsid="IG_A", ig_username="u1")
    session, _ = repo.get_or_create_session(db, user.id, sunny.id)

    # 尚未靜默
    assert repo.find_silent_sessions(db, threshold_seconds=1500) == []

    # 把最後活動時間推回 30 分鐘前
    session.last_message_at = utcnow() - timedelta(minutes=30)
    db.flush()

    found = repo.find_silent_sessions(db, threshold_seconds=1500)
    assert len(found) == 1

    # 已發過邀請的不應再被掃到
    session.invite_sent = True
    db.flush()
    assert repo.find_silent_sessions(db, threshold_seconds=1500) == []


# ── 訊息與去重 ──────────────────────────────────────────────

def test_訊息去重_重送的相同事件被擋下(db, sunny):
    """Meta 未即時收到 200 會重送同一事件。
    沒有去重，使用者講一句話晴晴會回兩次。"""
    user, _, _ = repo.resolve_user(db, persona_id=sunny.id, igsid="IG_A", ig_username="u1")
    session, _ = repo.get_or_create_session(db, user.id, sunny.id)

    first = repo.save_inbound_message(
        db, session=session, platform_message_id="mid_abc123", content="嗨"
    )
    second = repo.save_inbound_message(
        db, session=session, platform_message_id="mid_abc123", content="嗨"
    )

    assert first is not None
    assert second is None, "重複事件應回傳 None"
    assert session.message_count == 1


def test_出站訊息不重置視窗(db, sunny):
    """只有使用者開口才會重置視窗，系統自己發訊不會。"""
    user, _, _ = repo.resolve_user(db, persona_id=sunny.id, igsid="IG_A", ig_username="u1")
    session, _ = repo.get_or_create_session(db, user.id, sunny.id)

    window_before = session.window_expires_at
    repo.save_outbound_message(db, session=session, content="嗨～今天過得還好嗎")

    assert session.window_expires_at == window_before


def test_近期訊息依時間正序回傳(db, sunny):
    user, _, _ = repo.resolve_user(db, persona_id=sunny.id, igsid="IG_A", ig_username="u1")
    session, _ = repo.get_or_create_session(db, user.id, sunny.id)

    for i in range(5):
        repo.save_inbound_message(
            db, session=session, platform_message_id=f"mid_{i}", content=f"訊息{i}"
        )

    recent = repo.recent_messages(db, session.id, limit=3)
    assert [m.content for m in recent] == ["訊息2", "訊息3", "訊息4"]


def test_未互動使用者_發訊前檢查(db, sunny):
    user, _, _ = repo.resolve_user(db, persona_id=sunny.id, igsid="IG_A", ig_username="u1")
    session, _ = repo.get_or_create_session(db, user.id, sunny.id)

    assert repo.has_any_inbound(db, session.id) is False

    repo.save_inbound_message(
        db, session=session, platform_message_id="mid_1", content="嗨"
    )
    assert repo.has_any_inbound(db, session.id) is True


def test_視窗檢查(db, sunny):
    from datetime import timedelta

    from app.db.models import utcnow

    user, _, _ = repo.resolve_user(db, persona_id=sunny.id, igsid="IG_A", ig_username="u1")
    session, _ = repo.get_or_create_session(db, user.id, sunny.id)

    assert repo.is_window_open(session) is True

    session.window_expires_at = utcnow() - timedelta(minutes=1)
    assert repo.is_window_open(session) is False


# ═══════════════════════════════════════════════════════════
#  6.7 話題記憶
# ═══════════════════════════════════════════════════════════

def test_依session找到對應連結(db, sunny):
    user, link, _ = repo.resolve_user(db, persona_id=sunny.id, igsid="IG_TOPIC", ig_username="u1")
    session, _ = repo.get_or_create_session(db, user.id, sunny.id)

    found = repo.get_link_by_session(db, session)
    assert found is not None
    assert found.id == link.id


def test_附加話題記憶_去重且新話題排在後面(db, sunny):
    _, link, _ = repo.resolve_user(db, persona_id=sunny.id, igsid="IG_TOPIC2", ig_username="u2")
    assert link.topic_memory == []

    repo.append_topic_memory(db, link, ["幼兒園老師", "河濱騎車"])
    assert link.topic_memory == ["幼兒園老師", "河濱騎車"]

    # 第二次萃取：重複的不增加，新的接在後面
    repo.append_topic_memory(db, link, ["河濱騎車", "被主管唸"])
    assert link.topic_memory == ["幼兒園老師", "河濱騎車", "被主管唸"]


def test_話題記憶超過上限時只保留最新30筆(db, sunny):
    _, link, _ = repo.resolve_user(db, persona_id=sunny.id, igsid="IG_TOPIC3", ig_username="u3")

    repo.append_topic_memory(db, link, [f"話題{i}" for i in range(40)])
    assert len(link.topic_memory) == 30
    assert link.topic_memory[-1] == "話題39"  # 最新的話題被保留，不是最舊的


def test_前場次訊息_只撈同一組使用者與角色且更早的場次(db, sunny, aze):
    user, _, _ = repo.resolve_user(db, persona_id=sunny.id, igsid="IG_PREV", ig_username="prev1")
    s1, _ = repo.get_or_create_session(db, user.id, sunny.id)
    repo.save_inbound_message(db, session=s1, platform_message_id="p1", content="我被主管罵，他姓白")
    repo.save_outbound_message(db, session=s1, content="辛苦了")
    repo.end_session(db, s1)

    # 另一個使用者的訊息不該混進來
    other, _, _ = repo.resolve_user(db, persona_id=sunny.id, igsid="IG_OTHER", ig_username="other1")
    so, _ = repo.get_or_create_session(db, other.id, sunny.id)
    repo.save_inbound_message(db, session=so, platform_message_id="o1", content="別人的話")

    s2, _ = repo.get_or_create_session(db, user.id, sunny.id)
    prev = repo.previous_sessions_messages(db, s2, limit=10)

    assert [m.content for m in prev] == ["我被主管罵，他姓白", "辛苦了"]
    assert repo.previous_sessions_messages(db, s1, limit=10) == []


# ── 即時訊號（task 8.3）────────────────────────────────────

def _session_with_message(db, sunny, text="加個 LINE 吧"):
    user, _link, _ = repo.resolve_user(
        db, persona_id=sunny.id, igsid="IGSID_SIG", ig_username="sig_user"
    )
    session, _ = repo.get_or_create_session(db, user.id, sunny.id)
    message = repo.save_inbound_message(
        db, session=session, platform_message_id=f"mid_{text}", content=text
    )
    return session, message


def test_訊號落庫_單則訊息可記多筆且欄位完整(db, sunny):
    session, message = _session_with_message(db, sunny)
    repo.replace_session_signals(db, session.id, [
        {"message_id": message.id, "signal_type": "landmine", "severity": "high",
         "dimension": "分寸與界線", "reason": "索取聯絡方式"},
        {"message_id": message.id, "signal_type": "killer", "severity": None,
         "dimension": "接話與延展", "reason": "沒接角色的問題"},
    ])

    saved = repo.signals_for_session(db, session.id)
    assert [(s.signal_type, s.severity, s.dimension) for s in saved] == [
        ("landmine", "high", "分寸與界線"),
        ("killer", None, "接話與延展"),
    ]
    assert all(s.message_id == message.id for s in saved)
    assert saved[0].reason == "索取聯絡方式"


def test_訊號落庫_重跑偵測時取代舊訊號不重複(db, sunny):
    session, message = _session_with_message(db, sunny)
    first = [{"message_id": message.id, "signal_type": "landmine", "dimension": "分寸與界線"}]
    repo.replace_session_signals(db, session.id, first)
    repo.replace_session_signals(db, session.id, first)
    assert len(repo.signals_for_session(db, session.id)) == 1

    repo.replace_session_signals(db, session.id, [])
    assert repo.signals_for_session(db, session.id) == []


def test_訊號可回溯至使用者原句(db, sunny):
    from app.db.models import Message

    session, message = _session_with_message(db, sunny, text="我可以跟妳要Line嗎?")
    repo.replace_session_signals(db, session.id, [
        {"message_id": message.id, "signal_type": "landmine", "dimension": "分寸與界線"},
    ])
    [signal] = repo.signals_for_session(db, session.id)
    assert db.get(Message, signal.message_id).content == "我可以跟妳要Line嗎?"
