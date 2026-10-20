"""webhook 端點測試（tasks 3.1–3.7）。

全部使用假 payload，不需要真的 Meta 帳號——3.1 到 3.7 這幾項
本來就可以在沒有 Meta 憑證的情況下先寫先測。
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import repository as repo
from app.db.models import Base

APP_SECRET = "test_app_secret"
VERIFY_TOKEN = "test_verify_token"
IG_ACCOUNT_ID = "17841400000001"


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture
def client(monkeypatch, tmp_path):
    """每個測試獨立的 SQLite 檔案 + 已 seed 好晴晴角色的 TestClient。

    注意重載順序：app.db.session 在模組層級建立 engine（讀取當時的
    DATABASE_URL），app.main 又在模組層級 import db_session/init_db。
    只重載 app.main 的話，engine 仍是舊的（可能指到正式的 data/poc.db）。
    必須先重載 app.db.session，讓 app.main 重載時抓到綁定新 engine 的版本。
    """
    import importlib

    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    # 不受本機 .env 影響：設定頁功能（M13 任務 5.1）關閉
    monkeypatch.setenv("SETTINGS_LINK_SECRET", "")
    monkeypatch.setenv("PUBLIC_BASE_URL", "")
    # 驗章用的是 Instagram 專屬密鑰而非 META_APP_SECRET，
    # 兩者在 Meta 後台是不同欄位（見 config.webhook_signing_secret）。
    monkeypatch.setenv("IG_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("META_APP_SECRET", "不該被用來驗章的密鑰")
    monkeypatch.setenv("META_WEBHOOK_VERIFY_TOKEN", VERIFY_TOKEN)
    monkeypatch.setenv("IG_ACCESS_TOKEN", "fake_token")
    monkeypatch.setenv("IG_ACCOUNT_ID", IG_ACCOUNT_ID)
    # 明確清空——開發者本機 .env 若有真實 Gemini 金鑰，不覆寫的話會讓
    # llm_router 在測試中連上真實 API：真的燒額度、測試變慢又依賴網路，
    # 而這個檔案測的是任務群 3（webhook），不是任務群 5（LLM 供應層，
    # 見 tests/test_llm.py）或任務群 7（回覆生成，見 tests/test_reply_generation.py）。
    monkeypatch.setenv("GEMINI_API_KEY", "")
    # 明確關閉開發用逃生門。開發者本機的 .env 可能把它打開著，
    # 若不覆寫，驗章相關的測試會靜靜地失去意義（全部變成放行）。
    monkeypatch.setenv("WEBHOOK_SKIP_SIGNATURE_CHECK", "false")

    from app.config import get_settings

    get_settings.cache_clear()

    import app.db.session as session_module

    importlib.reload(session_module)

    import app.main as main_module

    importlib.reload(main_module)

    # 背景任務會真的呼叫 InstagramClient.send_text，測試環境沒有真的
    # access token，直接打 API 只會逾時或收到 401。這裡測的是任務群 3
    # （webhook 接收/驗證/解析/去重/路由），不是任務群 4 的真實發訊，
    # 所以把送出動作替換成假的，避免測試變慢或依賴網路狀態。
    _fake_id_counter = iter(range(1, 100_000))

    async def _fake_send_text(self, igsid, text):
        # 每次回傳不同的假訊息 ID——真實 Meta API 保證每次回應的
        # message_id 唯一，固定回傳同一個字串會撞到 platform_message_id
        # 的唯一約束（出站訊息也適用同一條去重規則）。
        return f"fake_message_id_{next(_fake_id_counter)}"

    async def _fake_sender_action(self, igsid, action):
        # 輸入狀態提示同樣是真實的 API 呼叫，測試環境要一併攔掉。
        return None

    async def _fake_fetch_username(self, igsid):
        # task 6.1：新使用者的第一則訊息會觸發這個呼叫。回傳 None 等同
        # 「查不到」，走 6.2 的降級路徑——與這裡多數測試原本的假設一致，
        # 不必額外調整既有斷言。
        return None

    monkeypatch.setattr(main_module.InstagramClient, "send_text", _fake_send_text)
    monkeypatch.setattr(main_module.InstagramClient, "send_sender_action", _fake_sender_action)
    monkeypatch.setattr(main_module.InstagramClient, "fetch_username", _fake_fetch_username)

    # 同理，背景任務會依人設卡的擬真節奏（task 7.8）等 8–40 秒才送出。
    # 這裡測的是任務群 3，不是節奏本身（那在 tests/test_pacing.py），
    # 讓每一筆假 payload 真的睡滿會把整個套件從數秒拖成數分鐘。
    import app.dialog.delivery as delivery_module

    async def _no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(delivery_module, "DEFAULT_SLEEP", _no_sleep)

    with TestClient(main_module.app) as c:
        # 拆分（task 7.8）現在完全依 LLM 輸出本身有沒有 \n\n 自然段落
        # 決定，不再是隨機機率。這裡走的是 GEMINI_API_KEY 清空後的
        # pick_fallback() 降級語句（app/llm/fallback.py），內容本來就
        # 是不含 \n\n 的短句，天然保持單則，不需要另外鎖定任何參數。
        yield c

    get_settings.cache_clear()


def _message_payload(mid: str = "mid_001", text: str = "嗨", igsid: str = "IGSID_A") -> dict:
    return {
        "object": "instagram",
        "entry": [
            {
                "id": IG_ACCOUNT_ID,
                "time": 1234567890,
                "messaging": [
                    {
                        "sender": {"id": igsid},
                        "recipient": {"id": IG_ACCOUNT_ID},
                        "timestamp": 1234567890,
                        "message": {"mid": mid, "text": text},
                    }
                ],
            }
        ],
    }


# ── 3.1 訂閱驗證 ──────────────────────────────────────────

def test_訂閱驗證_token相符時回傳challenge(client):
    resp = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "abc123"},
    )
    assert resp.status_code == 200
    assert resp.text == "abc123"


def test_訂閱驗證_token不符時拒絕(client):
    resp = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong_token", "hub.challenge": "abc123"},
    )
    assert resp.status_code == 403


def test_訂閱驗證_mode不是subscribe時拒絕(client):
    resp = client.get(
        "/webhook",
        params={"hub.mode": "unsubscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "abc123"},
    )
    assert resp.status_code == 403


# ── 3.2 簽章驗證 ──────────────────────────────────────────

def test_簽章正確時接受(client):
    body = json.dumps(_message_payload()).encode()
    resp = client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"}
    )
    assert resp.status_code == 200


def test_簽章錯誤時拒絕且不落庫(client):
    body = json.dumps(_message_payload(mid="mid_reject")).encode()
    wrong_sig = sign(body, secret="wrong_secret")

    resp = client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": wrong_sig, "Content-Type": "application/json"}
    )
    assert resp.status_code == 403

    from app.db.session import db_session
    with db_session() as db:
        from app.db.models import Message
        found = db.query(Message).filter(Message.platform_message_id == "mid_reject").first()
        assert found is None, "簽章錯誤的請求不應該有任何資料被寫入"


def test_缺少簽章標頭時拒絕(client):
    body = json.dumps(_message_payload()).encode()
    resp = client.post("/webhook", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 403


# ── 3.4 角色路由 ──────────────────────────────────────────

def test_未知帳號_略過但仍回200(client):
    payload = _message_payload()
    payload["entry"][0]["id"] = "99999999999"  # 不存在的角色帳號
    body = json.dumps(payload).encode()

    resp = client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"}
    )
    assert resp.status_code == 200  # 未知帳號略過，但整體請求仍算成功處理


# ── 3.5 單一請求多筆事件 ────────────────────────────────────

def test_單一請求含多筆事件_逐筆處理(client):
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": IG_ACCOUNT_ID,
                "messaging": [
                    {"sender": {"id": "IGSID_A"}, "message": {"mid": "mid_multi_1", "text": "第一則"}},
                    {"sender": {"id": "IGSID_B"}, "message": {"mid": "mid_multi_2", "text": "第二則"}},
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()
    resp = client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"}
    )
    assert resp.status_code == 200

    from app.db.session import db_session
    from app.db.models import Message
    with db_session() as db:
        assert db.query(Message).filter(Message.platform_message_id == "mid_multi_1").first() is not None
        assert db.query(Message).filter(Message.platform_message_id == "mid_multi_2").first() is not None


# ── 回音事件過濾 ────────────────────────────────────────────

def test_回音事件_不落庫也不觸發回覆(client):
    """角色帳號自己發出的訊息會被 Meta 原樣回送（is_echo）。

    若不過濾，sender.id 會是角色帳號本身，程式會把晴晴當成使用者建檔、
    開場次、再回一則訊息給自己——那則回覆又產生新的回音，形成無窮迴圈。
    """
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": IG_ACCOUNT_ID,
                "messaging": [
                    {
                        "sender": {"id": IG_ACCOUNT_ID},      # 發送者是角色帳號自己
                        "recipient": {"id": "IGSID_HUMAN"},
                        "message": {"mid": "mid_echo", "text": "嗨", "is_echo": True},
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()
    resp = client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"}
    )
    assert resp.status_code == 200

    from app.db.session import db_session
    from app.db.models import Message, User
    with db_session() as db:
        assert db.query(Message).filter(Message.platform_message_id == "mid_echo").first() is None
        assert db.query(User).count() == 0, "回音事件不應該建立任何使用者"


# ── 3.6 事件去重 ──────────────────────────────────────────

def test_重複事件_第二次不重複建立訊息(client):
    payload = _message_payload(mid="mid_dup", igsid="IGSID_DUP")
    body = json.dumps(payload).encode()
    headers = {"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"}

    resp1 = client.post("/webhook", content=body, headers=headers)
    resp2 = client.post("/webhook", content=body, headers=headers)

    assert resp1.status_code == 200
    assert resp2.status_code == 200  # 重複事件仍回 200（Meta 認為送達即可），只是不重複處理

    from app.db.session import db_session
    from app.db.models import Message
    with db_session() as db:
        count = db.query(Message).filter(Message.platform_message_id == "mid_dup").count()
        assert count == 1, "重複的 platform_message_id 應該只存一筆"


# ── 3.7 落庫與 postback ─────────────────────────────────────

def test_postback_重置視窗但不算入訊息(client):
    # 先讓使用者傳一則訊息建立 session 與連結
    msg_body = json.dumps(_message_payload(mid="mid_pre", igsid="IGSID_PB")).encode()
    client.post(
        "/webhook", content=msg_body,
        headers={"X-Hub-Signature-256": sign(msg_body), "Content-Type": "application/json"},
    )

    postback_payload = {
        "object": "instagram",
        "entry": [
            {
                "id": IG_ACCOUNT_ID,
                "messaging": [
                    {"sender": {"id": "IGSID_PB"}, "postback": {"payload": "SHOW_REPORT"}},
                ],
            }
        ],
    }
    body = json.dumps(postback_payload).encode()
    resp = client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"}
    )
    assert resp.status_code == 200


# ── 7.5 AI 身分揭露（整合進實際發訊管線）─────────────────────
#
# app/tests/test_personas.py 已經單元測試過 should_disclose／compose_reply
# 本身的判斷邏輯；這裡驗證的是「main.py 真的有把它接進 webhook → 發訊
# 這條管線」，兩層測試涵蓋的問題不同——單元測試測不出「忘了接線」。

def _outbound_messages(igsid: str) -> list:
    """取出某 IGSID 對應場次的所有出站訊息，依時間排序。"""
    from app.db.session import db_session
    from app.db.models import Message, MessageDirection, Persona
    from app.db.models import Session as SessionModel
    from app.db.models import UserPersonaLink

    with db_session() as db:
        persona = db.query(Persona).filter(Persona.ig_account_id == IG_ACCOUNT_ID).one()
        link = (
            db.query(UserPersonaLink)
            .filter(UserPersonaLink.persona_id == persona.id, UserPersonaLink.igsid == igsid)
            .one()
        )
        session = (
            db.query(SessionModel)
            .filter(SessionModel.user_id == link.user_id, SessionModel.persona_id == persona.id)
            .one()
        )
        rows = (
            db.query(Message)
            .filter(Message.session_id == session.id, Message.direction == MessageDirection.OUTBOUND)
            .order_by(Message.id)
            .all()
        )
        return [m.content for m in rows]


def test_揭露_首次對話outbound內容包含揭露文字(client):
    import app.main as main_module
    from app.personas.disclosure import disclosure_text

    igsid = "IGSID_DISCLOSE_1"
    body = json.dumps(_message_payload(mid="mid_disclose_1", igsid=igsid)).encode()
    resp = client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"}
    )
    assert resp.status_code == 200

    persona_name = main_module.persona_cards["sunny"].identity.name
    outbound = _outbound_messages(igsid)

    # 揭露與回覆內容各自是一句，會被拆成兩則送出；揭露必須在第一則
    assert 1 <= len(outbound) <= 2
    assert outbound[0].startswith(disclosure_text(persona_name))


def test_揭露_短時間內第二則不重複揭露(client):
    igsid = "IGSID_DISCLOSE_2"

    first = json.dumps(_message_payload(mid="mid_disclose_2a", igsid=igsid)).encode()
    client.post(
        "/webhook", content=first,
        headers={"X-Hub-Signature-256": sign(first), "Content-Type": "application/json"},
    )

    second = json.dumps(_message_payload(mid="mid_disclose_2b", igsid=igsid, text="第二句")).encode()
    resp = client.post(
        "/webhook", content=second,
        headers={"X-Hub-Signature-256": sign(second), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200

    import app.main as main_module
    from app.personas.disclosure import disclosure_text

    persona_name = main_module.persona_cards["sunny"].identity.name
    outbound = _outbound_messages(igsid)

    assert len(outbound) >= 2
    assert outbound[0].startswith(disclosure_text(persona_name)), "第一則必須揭露"
    assert not any(
        m.startswith(disclosure_text(persona_name)) for m in outbound[1:]
    ), "短時間內的第二則不該重複揭露"


# ── 6.1 使用者身分歸戶 ────────────────────────────────────

def test_首次見面時查詢username並標記身分已確認(client, monkeypatch):
    """接線驗證：fetch_username() 的結果要真的流進 resolve_user()。

    這正是 2026-09-15 查證發現的缺口——零件都在但沒接起來，導致線上
    每一位使用者都是 identity_confirmed=False。這裡驗證接上之後的行為。
    """
    import app.main as main_module

    async def _fake_fetch_username(self, igsid):
        return "test_username_123"

    monkeypatch.setattr(main_module.InstagramClient, "fetch_username", _fake_fetch_username)

    body = json.dumps(_message_payload(mid="mid_username", igsid="IGSID_NEW_USER")).encode()
    resp = client.post(
        "/webhook", content=body,
        headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200

    from app.db.session import db_session
    from app.db.models import User

    with db_session() as db:
        user = db.query(User).filter(User.ig_username == "test_username_123").one()
        assert user.identity_confirmed is True


def test_查詢username失敗時標記身分未確認(client):
    """預設的假物件回傳 None（見 client fixture），對應使用者封鎖 App
    或 API 失敗的情境——仍要能建立可運作的紀錄（6.2），但不歸戶。
    """
    body = json.dumps(_message_payload(mid="mid_no_username", igsid="IGSID_NO_USERNAME")).encode()
    resp = client.post(
        "/webhook", content=body,
        headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200

    from app.db.session import db_session
    from app.db.models import User, UserPersonaLink

    with db_session() as db:
        link = db.query(UserPersonaLink).filter(UserPersonaLink.igsid == "IGSID_NO_USERNAME").one()
        user = db.query(User).filter(User.id == link.user_id).one()
        assert user.ig_username is None
        assert user.identity_confirmed is False


def test_已有連結的使用者不重複查詢username(client, monkeypatch):
    """已見過面的使用者，每則訊息都打一次 API 沒有意義——見
    _handle_message 的既有註解「首次見面才需要查 username」。
    """
    import app.main as main_module

    call_count = 0

    async def _counting_fetch_username(self, igsid):
        nonlocal call_count
        call_count += 1
        return "should_only_see_once"

    monkeypatch.setattr(main_module.InstagramClient, "fetch_username", _counting_fetch_username)

    igsid = "IGSID_REPEAT"
    for mid in ("mid_repeat_1", "mid_repeat_2", "mid_repeat_3"):
        body = json.dumps(_message_payload(mid=mid, igsid=igsid)).encode()
        client.post(
            "/webhook", content=body,
            headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"},
        )

    assert call_count == 1, "三則訊息只應該在第一次觸發 username 查詢"


# ═══════════════════════════════════════════════════════════
#  /health（migrate-to-postgres-and-containerize 決策 9）
# ═══════════════════════════════════════════════════════════

def test_health_資料庫正常時回200(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["database"] == "sqlite"
    assert body["database_ok"] is True


def test_health_資料庫連不上時回503(client, monkeypatch):
    """compose 的健康檢查靠這個狀態碼判斷 app 是否健康，不能永遠回 200。"""
    import app.main as main_module

    monkeypatch.setattr(main_module, "check_connection", lambda: False)
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["database_ok"] is False