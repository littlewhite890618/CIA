"""資料層測試（add-persona-authoring-console 第 3 群，design.md 決策 12、13）。

SQLite 部分驗證表結構可用；標記 postgres 的部分驗證 Alembic 遷移與 models.py
一致、且 0003 可升降級（需 TEST_DATABASE_URL，見 test_postgres_smoke.py）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base,
    Persona,
    PersonaCoaching,
    Tenant,
    TenantStatus,
    User,
    UserPersonaLink,
    UserPersonaVersion,
    utcnow,
)
from tests.conftest import ROOT, TEST_DATABASE_URL, requires_postgres


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        yield session


def _persona(slug: str, **kw) -> Persona:
    return Persona(slug=slug, name=slug, gender="female", **kw)


@pytest.fixture
def sunny_and_tenant(db):
    sunny = _persona("sunny", ig_account_id="17841400000001")
    tenant = Tenant(name="想想交友", ref_code="xiangxiang")
    db.add_all([sunny, tenant])
    db.flush()
    return sunny, tenant


# ── 客戶 ──────────────────────────────────────────────────

def test_客戶預設為啟用(db):
    tenant = Tenant(name="想想交友", ref_code="xiangxiang")
    db.add(tenant)
    db.commit()
    assert tenant.status == TenantStatus.ACTIVE


def test_客戶名稱不可重複(db):
    db.add_all([Tenant(name="想想交友", ref_code="a"), Tenant(name="想想交友", ref_code="b")])
    with pytest.raises(IntegrityError):
        db.commit()


def test_專屬代碼不可重複(db):
    """代碼重複會讓同一條連結對應到兩個客戶，歸屬無法判定。"""
    db.add_all([Tenant(name="甲", ref_code="same"), Tenant(name="乙", ref_code="same")])
    with pytest.raises(IntegrityError):
        db.commit()


# ── 共用角色 ──────────────────────────────────────────────

def test_共用角色不再有擁有者欄位():
    """決策 12：共用角色屬於平台，客戶的內容在角色版本。"""
    assert "tenant_id" not in Persona.__table__.columns


def test_新建共用角色可以還沒有IG帳號且可同時存在多個(db):
    """design.md 決策 5：IG 帳號識別碼在平台作業完成後才回填。"""
    db.add_all([_persona("draft_a"), _persona("draft_b")])
    db.commit()
    assert db.query(Persona).filter(Persona.ig_account_id.is_(None)).count() == 2


def test_IG帳號有值時仍不可重複(db):
    db.add_all([_persona("a", ig_account_id="1"), _persona("b", ig_account_id="1")])
    with pytest.raises(IntegrityError):
        db.commit()


# ── 使用者版本（決策 13）──────────────────────────────────

@pytest.fixture
def link(db, sunny_and_tenant):
    sunny, _ = sunny_and_tenant
    user = User(ig_username="u1")
    db.add(user)
    db.flush()
    link = UserPersonaLink(user_id=user.id, persona_id=sunny.id, igsid="IGSID_1")
    db.add(link)
    db.flush()
    return link


def _card() -> dict:
    return {"identity": {"age": 28, "occupation": "護理師"}, "background": {"recent_life": "在準備考試"}}


def test_使用者版本保存整份內容(db, link):
    version = UserPersonaVersion(link_id=link.id, card=_card())
    db.add(version)
    db.commit()
    assert db.get(UserPersonaVersion, version.id).card == _card()


def test_使用者版本內容必填(db, link):
    """設定頁預先帶入平台預設內容、保存時驗證，因此不存在空的版本。"""
    db.add(UserPersonaVersion(link_id=link.id, card=None))
    with pytest.raises(IntegrityError):
        db.commit()


def test_同一使用者對同一共用角色只有一份版本(db, link):
    db.add_all([UserPersonaVersion(link_id=link.id, card=_card()), UserPersonaVersion(link_id=link.id, card=_card())])
    with pytest.raises(IntegrityError):
        db.commit()


def test_不同使用者可以各自有同一共用角色的版本(db, sunny_and_tenant, link):
    sunny, _ = sunny_and_tenant
    other_user = User(ig_username="u2")
    db.add(other_user)
    db.flush()
    other_link = UserPersonaLink(user_id=other_user.id, persona_id=sunny.id, igsid="IGSID_2")
    db.add(other_link)
    db.flush()
    db.add_all([UserPersonaVersion(link_id=link.id, card=_card()), UserPersonaVersion(link_id=other_link.id, card=_card())])
    db.commit()
    assert db.query(UserPersonaVersion).count() == 2


def test_使用者版本沒有審核狀態():
    """決策 13：不做人工審核，保存通過驗證即生效。"""
    assert "status" not in UserPersonaVersion.__table__.columns


# ── 教練設定 ──────────────────────────────────────────────

def test_教練設定掛在使用者版本上且未填項目為NULL(db, link):
    version = UserPersonaVersion(link_id=link.id, card=_card())
    db.add(version)
    db.flush()
    coaching = PersonaCoaching(version_id=version.id, disabled_rule_ids=["L9"])
    db.add(coaching)
    db.commit()

    assert coaching.disabled_rule_ids == ["L9"]
    assert coaching.custom_rules is None
    assert coaching.report_tone is None
    assert coaching.report_sections is None


def test_一個版本只有一份教練設定(db, link):
    version = UserPersonaVersion(link_id=link.id, card=_card())
    db.add(version)
    db.flush()
    db.add_all([PersonaCoaching(version_id=version.id), PersonaCoaching(version_id=version.id)])
    with pytest.raises(IntegrityError):
        db.commit()


# ── 使用者歸屬 ────────────────────────────────────────────

def test_新連結預設未歸屬(db, sunny_and_tenant):
    sunny, _ = sunny_and_tenant
    user = User(ig_username="u1")
    db.add(user)
    db.flush()
    link = UserPersonaLink(user_id=user.id, persona_id=sunny.id, igsid="IGSID_1")
    db.add(link)
    db.commit()

    assert link.tenant_id is None
    assert link.attribution_ref is None
    assert link.attributed_at is None


def test_歸屬保存客戶代碼與時間(db, sunny_and_tenant):
    sunny, tenant = sunny_and_tenant
    user = User(ig_username="u1")
    db.add(user)
    db.flush()
    now = utcnow()
    link = UserPersonaLink(
        user_id=user.id, persona_id=sunny.id, igsid="IGSID_1",
        tenant_id=tenant.id, attribution_ref="xiangxiang", attributed_at=now,
    )
    db.add(link)
    db.commit()

    saved = db.get(UserPersonaLink, link.id)
    assert saved.tenant_id == tenant.id
    assert saved.attribution_ref == "xiangxiang"


# ═══════════════════════════════════════════════════════════
#  Postgres：遷移
# ═══════════════════════════════════════════════════════════

@pytest.mark.postgres
@requires_postgres
def test_遷移後的結構與models一致(pg):
    """遷移是手寫的，用 Alembic 的比對確認沒有漏欄位、漏索引或型別不符。"""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    engine = pg.kw["bind"]
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diffs == []


@pytest.mark.postgres
@requires_postgres
def test_遷移0003可降級再升級(pg):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    cfg = Config(str(ROOT / "alembic.ini"))
    engine = create_engine(TEST_DATABASE_URL)

    command.downgrade(cfg, "0002")
    insp = inspect(engine)
    assert "user_persona_versions" not in insp.get_table_names()
    assert "tenant_id" in {c["name"] for c in insp.get_columns("personas")}
    assert "persona_id" in {c["name"] for c in insp.get_columns("persona_coaching")}
    assert "tenant_id" not in {c["name"] for c in insp.get_columns("user_persona_links")}

    command.upgrade(cfg, "head")
    insp = inspect(engine)
    assert "user_persona_versions" in insp.get_table_names()
    assert "tenant_id" not in {c["name"] for c in insp.get_columns("personas")}
    assert "version_id" in {c["name"] for c in insp.get_columns("persona_coaching")}
    assert "tenant_id" in {c["name"] for c in insp.get_columns("user_persona_links")}
    engine.dispose()


@pytest.mark.postgres
@requires_postgres
def test_遷移0003在有既有對話資料時可套用(pg):
    """正式資料庫有 POC 期間的使用者與連結，新增歸屬欄位不可影響它們。"""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    cfg = Config(str(ROOT / "alembic.ini"))
    command.downgrade(cfg, "0002")
    # 以原始 SQL 寫入：models.py 已是 0003 的結構，用 ORM 會查不存在的欄位
    engine = pg.kw["bind"]
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO personas (slug, name, gender, ig_account_id, source, status, created_at) "
            "VALUES ('sunny', '晴晴', 'female', '17841400000001', 'human', 'online', now())"
        ))
        conn.execute(text(
            "INSERT INTO users (ig_username, identity_confirmed, created_at) VALUES ('pg_user', true, now())"
        ))
        conn.execute(text(
            "INSERT INTO user_persona_links (user_id, persona_id, igsid, created_at) "
            "SELECT u.id, p.id, 'IGSID_PG', now() FROM users u, personas p"
        ))
    Session = pg

    command.upgrade(cfg, "head")

    with Session() as db:
        link = db.query(UserPersonaLink).one()
        assert link.igsid == "IGSID_PG"
        assert link.tenant_id is None
