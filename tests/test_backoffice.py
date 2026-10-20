"""後台帳號與登入（add-persona-authoring-console 任務 5.2～5.7）。

單元層驗證密碼雜湊、開通規則、登入狀態與失敗次數限制；整合層走真的
HTTP 請求，驗證存取規則（非管理員看不到管理員頁面）、開通客戶的完整
流程、重設密碼與登出讓舊登入失效。
"""

from __future__ import annotations

import importlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.auth import accounts
from app.db.models import Account, AccountRole, Base, LoginSession, Tenant, utcnow
from tests.conftest import requires_postgres
from tests.test_reply_generation import make_client  # noqa: F401  (fixture)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session


# ═══════════════════════════════════════════════════════════
#  密碼
# ═══════════════════════════════════════════════════════════

def test_密碼雜湊可驗證():
    stored = accounts.hash_password("correct horse")
    assert accounts.verify_password("correct horse", stored)
    assert not accounts.verify_password("wrong horse", stored)


def test_同一密碼每次雜湊結果不同():
    """每組密碼各自的隨機鹽：資料庫外洩時無法一眼看出誰的密碼相同。"""
    assert accounts.hash_password("same") != accounts.hash_password("same")


def test_不存明文():
    stored = accounts.hash_password("plain-text-password")
    assert "plain-text-password" not in stored
    assert stored.startswith("scrypt$")


@pytest.mark.parametrize("stored", ["", "garbage", "bcrypt$1$2$3$4$5", "scrypt$x$8$1$AA$AA"])
def test_格式錯誤的雜湊一律驗證失敗(stored):
    assert not accounts.verify_password("anything", stored)


# ═══════════════════════════════════════════════════════════
#  開通
# ═══════════════════════════════════════════════════════════

def test_建立管理員並可登入(db):
    account, password = accounts.create_admin(db, "root")
    assert account.role == AccountRole.ADMIN
    assert account.tenant_id is None
    assert accounts.authenticate(db, "root", password).id == account.id
    assert accounts.has_admin(db)


def test_沒有管理員時has_admin為否(db):
    assert not accounts.has_admin(db)


def test_開通客戶建立客戶與帳號(db):
    tenant, account, password = accounts.create_tenant_with_client(
        db, name="想想交友", ref_code="xiangxiang", username="xx_staff"
    )
    assert account.role == AccountRole.CLIENT
    assert account.tenant_id == tenant.id
    assert accounts.authenticate(db, "xx_staff", password).id == account.id


@pytest.mark.parametrize("ref_code", ["a b", "中文", "a.b", "a/b", "", "x" * 65])
def test_專屬代碼不符平台限制時拒絕(db, ref_code):
    with pytest.raises(accounts.AccountError, match="專屬代碼"):
        accounts.create_tenant_with_client(db, name="甲", ref_code=ref_code, username="user1")


@pytest.mark.parametrize("username", ["ab", "有中文", "has space", "x" * 65])
def test_登入帳號格式不符時拒絕(db, username):
    with pytest.raises(accounts.AccountError, match="登入帳號"):
        accounts.create_admin(db, username)


def test_重複的客戶名稱代碼或帳號皆拒絕且不留下資料(db):
    accounts.create_tenant_with_client(db, name="甲", ref_code="code_a", username="user_a")
    db.commit()
    for kwargs, match in [
        ({"name": "甲", "ref_code": "code_b", "username": "user_b"}, "客戶"),
        ({"name": "乙", "ref_code": "code_a", "username": "user_b"}, "專屬代碼"),
        ({"name": "乙", "ref_code": "code_b", "username": "user_a"}, "登入帳號"),
    ]:
        with pytest.raises(accounts.AccountError, match=match):
            accounts.create_tenant_with_client(db, **kwargs)
    db.commit()
    assert db.query(Tenant).count() == 1
    assert db.query(Account).count() == 1


def test_資料庫約束_客戶帳號必屬於客戶_管理員不屬於客戶(db):
    db.add(Account(username="orphan", password_hash="x", role=AccountRole.CLIENT))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    tenant = Tenant(name="甲", ref_code="a")
    db.add(tenant)
    db.flush()
    db.add(Account(username="odd_admin", password_hash="x", role=AccountRole.ADMIN, tenant_id=tenant.id))
    with pytest.raises(IntegrityError):
        db.commit()


# ═══════════════════════════════════════════════════════════
#  登入狀態
# ═══════════════════════════════════════════════════════════

def test_帳號不存在與密碼錯誤都回傳None(db):
    accounts.create_admin(db, "root")
    assert accounts.authenticate(db, "nobody", "x") is None
    assert accounts.authenticate(db, "root", "wrong") is None


def test_停用的帳號無法登入且既有登入失效(db):
    account, password = accounts.create_admin(db, "root")
    token = accounts.start_session(db, account, ttl_seconds=3600)
    account.is_active = False
    db.flush()
    assert accounts.authenticate(db, "root", password) is None
    assert accounts.account_for_session(db, token) is None


def test_登入狀態可取回帳號且資料庫不存原代碼(db):
    account, _ = accounts.create_admin(db, "root")
    token = accounts.start_session(db, account, ttl_seconds=3600)

    assert accounts.account_for_session(db, token).id == account.id
    assert db.query(LoginSession).one().token_hash != token
    assert account.last_login_at is not None


def test_登入狀態過期後失效(db):
    account, _ = accounts.create_admin(db, "root")
    token = accounts.start_session(db, account, ttl_seconds=3600)
    db.query(LoginSession).one().expires_at = utcnow() - timedelta(seconds=1)
    db.flush()
    assert accounts.account_for_session(db, token) is None


def test_登出後失效(db):
    account, _ = accounts.create_admin(db, "root")
    token = accounts.start_session(db, account, ttl_seconds=3600)
    accounts.end_session(db, token)
    assert accounts.account_for_session(db, token) is None


def test_重設密碼後舊密碼與所有舊登入失效(db):
    account, old_password = accounts.create_admin(db, "root")
    tokens = [accounts.start_session(db, account, ttl_seconds=3600) for _ in range(2)]

    new_password = accounts.reset_password(db, account)

    assert accounts.authenticate(db, "root", old_password) is None
    assert accounts.authenticate(db, "root", new_password).id == account.id
    assert all(accounts.account_for_session(db, t) is None for t in tokens)


@pytest.mark.parametrize("token", [None, "", "not-a-real-token"])
def test_沒有或錯誤的代碼回傳None(db, token):
    assert accounts.account_for_session(db, token) is None


# ═══════════════════════════════════════════════════════════
#  登入失敗次數限制
# ═══════════════════════════════════════════════════════════

class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_失敗達上限後暫時拒絕_時間窗過後恢復():
    clock = _Clock()
    throttle = accounts.LoginThrottle(max_failures=3, window_seconds=60, clock=clock)
    for _ in range(3):
        assert not throttle.is_blocked("root")
        throttle.record_failure("root")
    assert throttle.is_blocked("root")
    assert throttle.is_blocked("ROOT")  # 不分大小寫，換大小寫繞不過去

    clock.now += 61
    assert not throttle.is_blocked("root")


def test_成功登入後清除失敗紀錄():
    throttle = accounts.LoginThrottle(max_failures=2, window_seconds=60, clock=_Clock())
    throttle.record_failure("root")
    throttle.reset("root")
    throttle.record_failure("root")
    assert not throttle.is_blocked("root")


def test_各帳號分開計算():
    throttle = accounts.LoginThrottle(max_failures=1, window_seconds=60, clock=_Clock())
    throttle.record_failure("root")
    assert throttle.is_blocked("root")
    assert not throttle.is_blocked("other")


# ═══════════════════════════════════════════════════════════
#  整合：HTTP
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def web(make_client):
    """回傳 (TestClient, 建立帳號的函式)。每個使用者用自己的 TestClient，cookie 不混用。"""
    client = make_client()
    main = client._main_module

    def new_browser() -> TestClient:
        return TestClient(main.app, follow_redirects=False)

    return main, new_browser


def _db():
    from app.db import session as session_module

    return session_module.db_session()


def _create_admin(username: str = "root") -> str:
    with _db() as db:
        _account, password = accounts.create_admin(db, username)
    return password


def _login(browser: TestClient, username: str, password: str):
    return browser.post("/login", data={"username": username, "password": password})


def test_登入頁可開啟(web):
    _main, new_browser = web
    resp = new_browser().get("/login")
    assert resp.status_code == 200
    assert "後台登入" in resp.text


def test_管理員登入後導向管理員頁面_cookie設定安全旗標(web):
    _main, new_browser = web
    browser = new_browser()
    password = _create_admin()

    resp = _login(browser, "root", password)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie

    page = browser.get("/admin")
    assert page.status_code == 200
    assert "開通客戶" in page.text
    assert page.headers["cache-control"] == "no-store"


def test_登入失敗訊息不區分帳號不存在或密碼錯誤(web):
    _main, new_browser = web
    _create_admin()
    wrong_password = _login(new_browser(), "root", "wrong")
    no_such_user = _login(new_browser(), "nobody", "wrong")

    assert wrong_password.status_code == no_such_user.status_code == 401
    assert "帳號或密碼錯誤" in wrong_password.text
    assert "帳號或密碼錯誤" in no_such_user.text


def test_連續失敗後即使密碼正確也暫時拒絕(web):
    _main, new_browser = web
    password = _create_admin()
    browser = new_browser()
    for _ in range(5):
        assert _login(browser, "root", "wrong").status_code == 401

    resp = _login(browser, "root", password)
    assert resp.status_code == 429
    assert "set-cookie" not in resp.headers


@pytest.mark.parametrize("path", ["/admin"])
def test_未登入存取管理員頁面回404(web, path):
    _main, new_browser = web
    assert new_browser().get(path).status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/admin/tenants"),
        ("post", "/admin/admins"),
        ("post", "/admin/accounts/1/reset-password"),
    ],
)
def test_未登入的管理員操作回404且不執行(web, method, path):
    _main, new_browser = web
    _create_admin()
    resp = getattr(new_browser(), method)(
        path, data={"name": "甲", "ref_code": "a", "username": "attacker"}
    )
    assert resp.status_code == 404
    with _db() as db:
        assert db.query(Tenant).count() == 0
        assert db.query(Account).count() == 1  # 只有原本的管理員


def test_自動產生的API文件已關閉(web):
    """文件會列出所有路徑，包括刻意對非管理員回 404 的管理員頁面。"""
    _main, new_browser = web
    browser = new_browser()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert browser.get(path).status_code == 404


def test_開通客戶完整流程(web):
    _main, new_browser = web
    admin = new_browser()
    _login(admin, "root", _create_admin())

    resp = admin.post(
        "/admin/tenants",
        data={"name": "想想交友", "ref_code": "xiangxiang", "username": "xx_staff"},
    )
    assert resp.status_code == 200
    assert "已開通客戶「想想交友」" in resp.text
    password = resp.text.split('class="password">')[1].split("<")[0]

    client = new_browser()
    login = _login(client, "xx_staff", password)
    assert login.status_code == 303
    assert login.headers["location"] == "/client"

    page = client.get("/client")
    assert page.status_code == 200
    assert "想想交友" in page.text
    assert "xiangxiang" in page.text


def test_開通時輸入錯誤顯示原因且保留已填內容(web):
    _main, new_browser = web
    admin = new_browser()
    _login(admin, "root", _create_admin())

    resp = admin.post(
        "/admin/tenants", data={"name": "想想交友", "ref_code": "有中文", "username": "xx_staff"}
    )
    assert resp.status_code == 400
    assert "專屬代碼" in resp.text
    assert 'value="想想交友"' in resp.text
    with _db() as db:
        assert db.query(Tenant).count() == 0


def test_頁面內容會跳脫HTML(web):
    """客戶名稱由管理員輸入，顯示時必須跳脫，不能變成可執行的腳本。"""
    _main, new_browser = web
    admin = new_browser()
    _login(admin, "root", _create_admin())
    resp = admin.post(
        "/admin/tenants",
        data={"name": "<script>alert(1)</script>", "ref_code": "xss", "username": "xss_user"},
    )
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_客戶看不到管理員頁面也無法執行管理員操作(web):
    _main, new_browser = web
    with _db() as db:
        _t, _a, password = accounts.create_tenant_with_client(
            db, name="甲", ref_code="a", username="client_a"
        )
    client = new_browser()
    _login(client, "client_a", password)

    assert client.get("/admin").status_code == 404
    resp = client.post("/admin/admins", data={"username": "sneaky_admin"})
    assert resp.status_code == 404
    with _db() as db:
        assert not accounts.has_admin(db)


def test_管理員進客戶頁面回404(web):
    _main, new_browser = web
    admin = new_browser()
    _login(admin, "root", _create_admin())
    assert admin.get("/client").status_code == 404


def test_未登入進客戶頁面導向登入頁(web):
    _main, new_browser = web
    resp = new_browser().get("/client")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_新增其他管理員(web):
    _main, new_browser = web
    admin = new_browser()
    _login(admin, "root", _create_admin())

    resp = admin.post("/admin/admins", data={"username": "second"})
    assert resp.status_code == 200
    password = resp.text.split('class="password">')[1].split("<")[0]
    assert _login(new_browser(), "second", password).headers["location"] == "/admin"


def test_重設密碼後客戶舊密碼與舊登入失效(web):
    _main, new_browser = web
    with _db() as db:
        _t, account, old_password = accounts.create_tenant_with_client(
            db, name="甲", ref_code="a", username="client_a"
        )
        account_id = account.id
    client = new_browser()
    _login(client, "client_a", old_password)
    assert client.get("/client").status_code == 200

    admin = new_browser()
    _login(admin, "root", _create_admin())
    resp = admin.post(f"/admin/accounts/{account_id}/reset-password")
    assert resp.status_code == 200
    new_password = resp.text.split('class="password">')[1].split("<")[0]

    assert client.get("/client").status_code == 303  # 舊登入已失效，被導回登入頁
    assert _login(new_browser(), "client_a", old_password).status_code == 401
    assert _login(new_browser(), "client_a", new_password).status_code == 303


def test_重設不存在的帳號回404(web):
    _main, new_browser = web
    admin = new_browser()
    _login(admin, "root", _create_admin())
    assert admin.post("/admin/accounts/9999/reset-password").status_code == 404


def test_登出後舊cookie失效(web):
    _main, new_browser = web
    browser = new_browser()
    _login(browser, "root", _create_admin())
    cookie = browser.cookies.get("cia_session")

    browser.post("/logout")

    stolen = new_browser()
    stolen.cookies.set("cia_session", cookie)
    assert stolen.get("/admin").status_code == 404


@pytest.mark.postgres
@requires_postgres
def test_遷移建立的資料庫約束_客戶帳號必屬於客戶(pg):
    """Alembic 的結構比對不檢查 CHECK 約束，這裡直接在遷移後的 Postgres 上驗證。"""
    Session = pg
    with Session() as db:
        db.add(Account(username="orphan", password_hash="x", role=AccountRole.CLIENT))
        with pytest.raises(IntegrityError):
            db.commit()


# ═══════════════════════════════════════════════════════════
#  第一個管理員的指令（任務 5.7）
# ═══════════════════════════════════════════════════════════

def test_指令建立第一個管理員_已有管理員時拒絕(web, capsys):
    import scripts.create_admin as create_admin_script

    importlib.reload(create_admin_script)  # 綁定測試用的資料庫（make_client 已重載 session 模組）

    assert create_admin_script.main(["first_admin"]) == 0
    out = capsys.readouterr().out
    assert "已建立管理員：first_admin" in out
    password = out.split("密碼：")[1].splitlines()[0].strip()

    _main, new_browser = web
    assert _login(new_browser(), "first_admin", password).headers["location"] == "/admin"

    assert create_admin_script.main(["second_admin"]) == 1
    assert "已經有管理員了" in capsys.readouterr().err
