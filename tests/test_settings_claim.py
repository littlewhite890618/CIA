"""設定連結只能在一個瀏覽器上使用（add-persona-authoring-console 任務 5.8）。

走真的 HTTP 請求：第一個打開連結的瀏覽器拿到代碼（cookie），之後同一個瀏覽器可以
重新整理、保存、恢復預設；把連結轉貼到別的瀏覽器（沒有代碼）一律被拒絕，
而且拒絕的請求不會改到任何設定。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.settings_claim import COOKIE_NAME, check_or_claim
from app.auth.settings_link import issue_token
from app.db.models import Base, SettingsLinkClaim
from tests.test_settings_api import (  # noqa: F401  (api、make_client 為 fixture)
    SECRET,
    _get,
    _link_id,
    _payload,
    _save,
    api,
    make_client,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _other_browser(client) -> None:
    """換一支手機：沒有任何 cookie。"""
    client.cookies.clear()


def _is_used(resp) -> bool:
    return resp.status_code == 401 and resp.json() == {"error": "used", "keyword": "芝麻開門"}


# ═══════════════════════════════════════════════════════════
#  HTTP
# ═══════════════════════════════════════════════════════════


def test_第一次打開時發代碼且只送往設定的API(api):
    client, token = api
    resp = _get(client, token)
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    for part in ("HttpOnly", "Secure", "SameSite=lax", "Path=/api/settings"):
        assert part.lower() in cookie.lower()


def test_同一個瀏覽器可以重新整理_保存_恢復預設(api):
    client, token = api
    assert _get(client, token).status_code == 200
    assert _get(client, token).status_code == 200  # 重新整理
    assert _save(client, token, _payload(client, token, personality__core_traits="活潑外向、愛分享")).status_code == 200
    assert client.delete("/api/settings", params={"t": token}).status_code == 200


def test_轉貼到別的瀏覽器一律被拒絕(api):
    client, token = api
    _get(client, token)  # 本人先打開

    _other_browser(client)
    assert _is_used(_get(client, token))
    assert _is_used(_save(client, token, {"card": {}, "coaching": {}}))
    assert _is_used(client.delete("/api/settings", params={"t": token}))


def test_被拒絕的請求不會改到設定(api):
    client, token = api
    payload = _payload(client, token, personality__core_traits="活潑外向、愛分享")  # 本人載入後備好
    _other_browser(client)
    assert _is_used(_save(client, token, payload))

    # 本人（重新拿回自己的代碼是不可能的，改用新連結）確認沒有被存進去
    new_token = issue_token(_link_id("IGSID_API"), secret=SECRET, ttl_seconds=900)
    current = _get(client, new_token).json()["current"]["card"]
    assert current["personality"]["core_traits"] != "活潑外向、愛分享"


def test_代碼被竄改或換成別條連結的代碼都不行(api):
    client, token = api
    _get(client, token)
    client.cookies.set(COOKIE_NAME, "x" * 43, path="/api/settings")
    assert _is_used(_get(client, token))


def test_重新傳設定拿到的新連結在新裝置上可用(api):
    client, old_token = api
    _get(client, old_token)

    _other_browser(client)
    new_token = issue_token(_link_id("IGSID_API"), secret=SECRET, ttl_seconds=900)
    assert new_token != old_token
    assert _get(client, new_token).status_code == 200
    # 舊連結在新裝置上仍然不能用
    client.cookies.clear()
    assert _is_used(_get(client, old_token))


def test_頁面本身不會登記連結(api, tmp_path, monkeypatch):
    """IG 為了產生預覽卡片會抓網址。若頁面也登記，使用者還沒點開就被預覽機器人用掉了。"""
    import app.web.settings_api as settings_api

    (tmp_path / "index.html").write_text("<!doctype html><title>晴晴的設定</title>", encoding="utf-8")
    monkeypatch.setattr(settings_api, "FRONTEND_DIST", tmp_path)
    client, token = api

    assert client.get("/settings", params={"t": token}).status_code == 200  # 預覽機器人
    _other_browser(client)
    assert _get(client, token).status_code == 200  # 使用者本人仍能打開


def test_過期或無效的連結不會登記(api):
    client, _ = api
    link_id = _link_id("IGSID_API")
    expired = issue_token(link_id, secret=SECRET, ttl_seconds=-10)
    assert _get(client, expired).json() == {"error": "expired", "keyword": "芝麻開門"}
    assert _get(client, "garbage.token").json() == {"error": "invalid"}

    from app.db.session import db_session

    with db_session() as db:
        assert db.query(SettingsLinkClaim).count() == 0


# ═══════════════════════════════════════════════════════════
#  登記邏輯
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session


def _expires(minutes: int = 30) -> int:
    return int((NOW + timedelta(minutes=minutes)).timestamp())


def test_第一次登記給請求者之後要帶代碼(db):
    first = check_or_claim(db, "tok", None, _expires(), now=NOW)
    assert first.allowed and first.new_code

    again = check_or_claim(db, "tok", first.new_code, _expires(), now=NOW)
    assert again.allowed and again.new_code is None  # 已登記，不再發新的

    assert not check_or_claim(db, "tok", None, _expires(), now=NOW).allowed
    assert not check_or_claim(db, "tok", "wrong", _expires(), now=NOW).allowed


def test_不同連結的代碼互不通用(db):
    a = check_or_claim(db, "tok-a", None, _expires(), now=NOW)
    check_or_claim(db, "tok-b", None, _expires(), now=NOW)
    assert not check_or_claim(db, "tok-b", a.new_code, _expires(), now=NOW).allowed


def test_資料庫只存雜湊不存連結與代碼(db):
    result = check_or_claim(db, "tok-secret", None, _expires(), now=NOW)
    row = db.query(SettingsLinkClaim).one()
    assert "tok-secret" not in (row.token_hash, row.claim_hash)
    assert result.new_code not in (row.token_hash, row.claim_hash)
    assert len(row.token_hash) == len(row.claim_hash) == 64


def test_過期很久的登記在下次登記時清掉(db):
    check_or_claim(db, "old", None, int((NOW - timedelta(days=3)).timestamp()), now=NOW)
    check_or_claim(db, "new", None, _expires(), now=NOW)
    assert [r.expires_at.replace(tzinfo=timezone.utc) for r in db.query(SettingsLinkClaim)] == [
        datetime.fromtimestamp(_expires(), timezone.utc)
    ]
