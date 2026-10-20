"""user persona versions and attribution

add-persona-authoring-console 任務 3.6～3.10，依 design.md 決策 12（多個客戶共用
同一個 IG 帳號）與決策 13（自助頁面的使用者是終端使用者）調整 0002 建立的結構：
- 3.6 新增 user_persona_versions：使用者針對共用角色保存的版本，以
      user_persona_links（使用者 × 共用角色）為單位
- 3.7 persona_coaching 改掛在使用者版本上（persona_id → version_id）
- 3.8 user_persona_links 新增使用者歸屬；tenants 新增專屬代碼
- 3.9 移除 personas.tenant_id（共用角色屬於平台）

0002 套用後 tenants 與 persona_coaching 皆無資料（沒有任何程式寫入這兩張表），
因此 persona_coaching 直接重建、tenants.ref_code 直接加為 NOT NULL。若日後在
有資料的環境執行，這兩步會失敗而不會默默產生錯誤資料。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CARD_COMMENT_0002 = '整份人設卡（結構同 app/personas/schema.py 的 PersonaCard，不含 slug）。內建角色為 NULL，內容以 YAML 檔為準'
_CARD_COMMENT_0003 = '平台預設版本的整份人設卡（結構同 app/personas/schema.py 的 PersonaCard，不含 slug）。內建角色為 NULL，內容以 YAML 檔為準'


def _create_coaching_table(owner_column: str, owner_table: str) -> None:
    op.create_table('persona_coaching',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column(owner_column, sa.Integer(), nullable=False),
    sa.Column('disabled_rule_ids', sa.JSON(), nullable=True),
    sa.Column('custom_rules', sa.JSON(), nullable=True),
    sa.Column('report_tone', sa.Text(), nullable=True),
    sa.Column('report_length', sa.Text(), nullable=True),
    sa.Column('report_sections', sa.JSON(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint([owner_column], [f'{owner_table}.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f(f'ix_persona_coaching_{owner_column}'), 'persona_coaching', [owner_column], unique=True)


def _drop_coaching_table(owner_column: str) -> None:
    op.drop_index(op.f(f'ix_persona_coaching_{owner_column}'), table_name='persona_coaching')
    op.drop_table('persona_coaching')


def upgrade() -> None:
    # 3.8 tenants 專屬代碼
    op.add_column('tenants', sa.Column('ref_code', sa.String(length=64), nullable=False, comment='專屬私訊連結的代碼（ig.me/m/<帳號>?ref=<代碼>）。平台限英數與 - _ ='))
    op.create_unique_constraint('uq_tenants_ref_code', 'tenants', ['ref_code'])

    # 3.6 使用者版本
    op.create_table('user_persona_versions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('link_id', sa.Integer(), nullable=False),
    sa.Column('card', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['link_id'], ['user_persona_links.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_user_persona_versions_link_id'), 'user_persona_versions', ['link_id'], unique=True)

    # 3.7 教練設定改掛在使用者版本上
    _drop_coaching_table('persona_id')
    _create_coaching_table('version_id', 'user_persona_versions')

    # 3.8 使用者歸屬
    op.add_column('user_persona_links', sa.Column('tenant_id', sa.Integer(), nullable=True, comment='歸屬的客戶。NULL＝未歸屬'))
    op.add_column('user_persona_links', sa.Column('attribution_ref', sa.String(length=2083), nullable=True, comment='建立歸屬時平台傳來的代碼原文（上限依平台規格 2,083 字元）'))
    op.add_column('user_persona_links', sa.Column('attributed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_user_persona_links_tenant_id'), 'user_persona_links', ['tenant_id'], unique=False)
    op.create_foreign_key('fk_user_persona_links_tenant_id_tenants', 'user_persona_links', 'tenants', ['tenant_id'], ['id'])

    # 3.9 共用角色不屬於客戶
    op.drop_constraint('fk_personas_tenant_id_tenants', 'personas', type_='foreignkey')
    op.drop_index(op.f('ix_personas_tenant_id'), table_name='personas')
    op.drop_column('personas', 'tenant_id')
    op.alter_column('personas', 'card', existing_type=sa.JSON(), comment=_CARD_COMMENT_0003, existing_comment=_CARD_COMMENT_0002, existing_nullable=True)


def downgrade() -> None:
    op.alter_column('personas', 'card', existing_type=sa.JSON(), comment=_CARD_COMMENT_0002, existing_comment=_CARD_COMMENT_0003, existing_nullable=True)
    op.add_column('personas', sa.Column('tenant_id', sa.Integer(), nullable=True, comment='擁有者。NULL 表示系統內建角色（來自 YAML 檔）'))
    op.create_index(op.f('ix_personas_tenant_id'), 'personas', ['tenant_id'], unique=False)
    op.create_foreign_key('fk_personas_tenant_id_tenants', 'personas', 'tenants', ['tenant_id'], ['id'])

    op.drop_constraint('fk_user_persona_links_tenant_id_tenants', 'user_persona_links', type_='foreignkey')
    op.drop_index(op.f('ix_user_persona_links_tenant_id'), table_name='user_persona_links')
    op.drop_column('user_persona_links', 'attributed_at')
    op.drop_column('user_persona_links', 'attribution_ref')
    op.drop_column('user_persona_links', 'tenant_id')

    # 降級會丟失使用者版本與教練設定——兩者在 0002 的結構中沒有對應位置
    _drop_coaching_table('version_id')
    _create_coaching_table('persona_id', 'personas')

    op.drop_index(op.f('ix_user_persona_versions_link_id'), table_name='user_persona_versions')
    op.drop_table('user_persona_versions')

    op.drop_constraint('uq_tenants_ref_code', 'tenants', type_='unique')
    op.drop_column('tenants', 'ref_code')
