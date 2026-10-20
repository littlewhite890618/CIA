"""後台帳號：密碼、開通、登入狀態（add-persona-authoring-console 任務 5.2、5.3、5.6、5.7）。

管理員與客戶共用同一套帳號與登入，以角色區分。終端使用者不登入，
他們走私訊取得的設定頁連結（app/auth/settings_link.py）。

密碼：
- 以 Python 內建的 scrypt 雜湊，每組密碼各自的隨機鹽，不需額外套件
- 初始密碼由系統產生，只在開通或重設的當下顯示一次，資料庫只存雜湊

登入狀態：
- cookie 只存一組隨機代碼，資料庫存其 SHA-256
- 登出、重設密碼、停用帳號都會讓既有登入立即失效
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DBSession

from app.db.models import (
    Account,
    AccountRole,
    LoginSession,
    Tenant,
    TenantStatus,
    utcnow,
)

# scrypt 參數：N=2^14、r=8、p=1，約 16 MB 記憶體、單次數十毫秒，
# 對後台登入的頻率而言足夠慢，又不會拖垮單一 worker
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_LEN = 2**14, 8, 1, 32

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]{3,64}$")
# 平台規格：ig.me 連結的 ref 參數只接受英數與 - _ =
REF_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_=-]{1,64}$")


class AccountError(ValueError):
    """開通或修改帳號時的輸入錯誤。訊息可直接顯示給管理員。"""


# ── 密碼 ──────────────────────────────────────────────

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_LEN
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# 帳號不存在時也做一次同樣成本的驗證，讓「帳號不存在」與「密碼錯誤」
# 花的時間相同，無法從回應快慢猜出帳號是否存在
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def generate_password() -> str:
    """系統產生的初始密碼：約 16 個英數字元，夠長、也方便管理員轉交。"""
    return secrets.token_urlsafe(12)


# ── 開通與重設 ────────────────────────────────────────

def _check_username(db: DBSession, username: str) -> str:
    username = username.strip()
    if not USERNAME_PATTERN.match(username):
        raise AccountError("登入帳號需為 3～64 個英數字元，可含 _ . @ -")
    if db.scalar(select(Account.id).where(Account.username == username)) is not None:
        raise AccountError(f"登入帳號「{username}」已被使用")
    return username


def create_admin(db: DBSession, username: str) -> tuple[Account, str]:
    """建立管理員，回傳 (帳號, 初始密碼)。初始密碼只在此時取得。"""
    username = _check_username(db, username)
    password = generate_password()
    account = Account(
        username=username, password_hash=hash_password(password), role=AccountRole.ADMIN
    )
    db.add(account)
    db.flush()
    return account, password


def has_admin(db: DBSession) -> bool:
    return db.scalar(select(Account.id).where(Account.role == AccountRole.ADMIN).limit(1)) is not None


def create_tenant_with_client(
    db: DBSession, *, name: str, ref_code: str, username: str
) -> tuple[Tenant, Account, str]:
    """開通客戶：建立客戶與它的第一個登入帳號，回傳 (客戶, 帳號, 初始密碼)。"""
    name = name.strip()
    ref_code = ref_code.strip()
    if not name or len(name) > 128:
        raise AccountError("客戶名稱需為 1～128 個字")
    if db.scalar(select(Tenant.id).where(Tenant.name == name)) is not None:
        raise AccountError(f"客戶「{name}」已存在")
    if not REF_CODE_PATTERN.match(ref_code):
        raise AccountError("專屬代碼需為 1～64 個字元，只能用英數與 - _ =（Instagram 連結的限制）")
    if db.scalar(select(Tenant.id).where(Tenant.ref_code == ref_code)) is not None:
        raise AccountError(f"專屬代碼「{ref_code}」已被使用")
    username = _check_username(db, username)

    tenant = Tenant(name=name, ref_code=ref_code, status=TenantStatus.ACTIVE)
    db.add(tenant)
    db.flush()

    password = generate_password()
    account = Account(
        username=username,
        password_hash=hash_password(password),
        role=AccountRole.CLIENT,
        tenant_id=tenant.id,
    )
    db.add(account)
    db.flush()
    return tenant, account, password


def reset_password(db: DBSession, account: Account) -> str:
    """重設密碼並讓該帳號所有既有登入失效，回傳新密碼。"""
    password = generate_password()
    account.password_hash = hash_password(password)
    db.execute(delete(LoginSession).where(LoginSession.account_id == account.id))
    db.flush()
    return password


# ── 登入狀態 ──────────────────────────────────────────

def authenticate(db: DBSession, username: str, password: str) -> Account | None:
    account = db.scalar(select(Account).where(Account.username == username.strip()))
    if account is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, account.password_hash) or not account.is_active:
        return None
    return account


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_session(db: DBSession, account: Account, *, ttl_seconds: int) -> str:
    """建立登入狀態，回傳要放進 cookie 的隨機代碼。"""
    token = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(LoginSession(
        account_id=account.id,
        token_hash=_token_hash(token),
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    ))
    account.last_login_at = now
    db.flush()
    return token


def _as_aware(value: datetime) -> datetime:
    # SQLite 讀回的時間沒有時區資訊，視為 UTC（同 disclosure.should_disclose）
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def account_for_session(db: DBSession, token: str | None) -> Account | None:
    """以 cookie 的代碼取得登入中的帳號。過期、帳號停用、代碼不存在皆回傳 None。"""
    if not token:
        return None
    session = db.scalar(select(LoginSession).where(LoginSession.token_hash == _token_hash(token)))
    if session is None:
        return None
    if _as_aware(session.expires_at) <= utcnow():
        db.delete(session)
        db.flush()
        return None
    account = db.get(Account, session.account_id)
    if account is None or not account.is_active:
        return None
    return account


def end_session(db: DBSession, token: str | None) -> None:
    if token:
        db.execute(delete(LoginSession).where(LoginSession.token_hash == _token_hash(token)))
        db.flush()


# ── 登入失敗次數限制 ──────────────────────────────────

class LoginThrottle:
    """同一帳號在時間窗內失敗太多次就暫時拒絕登入，擋住線上猜密碼。

    記在行程記憶體：服務固定單一 worker（見 Dockerfile），重啟後歸零可接受——
    歸零只會讓攻擊者多猜幾次，而 scrypt 加上次數上限已讓線上猜測不切實際。
    以帳號為單位而非 IP：經過 ngrok 時看到的來源位址不可靠。
    """

    def __init__(self, max_failures: int, window_seconds: int, clock=time.monotonic) -> None:
        self._max = max_failures
        self._window = window_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[float]:
        q = self._failures[key]
        cutoff = self._clock() - self._window
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def is_blocked(self, username: str) -> bool:
        return len(self._prune(username.strip().lower())) >= self._max

    def record_failure(self, username: str) -> None:
        self._prune(username.strip().lower()).append(self._clock())

    def reset(self, username: str) -> None:
        self._failures.pop(username.strip().lower(), None)
