"""tenants and persona content

add-persona-authoring-console 第 3 群（資料層）：
- 3.1 新增 tenants 表
- 3.2 personas.tenant_id（擁有者，NULL＝系統內建角色）
- 3.3 personas.card（整份人設卡 JSON）
- 3.4 新增 persona_coaching 表（教練設定，一角色一列）
- personas.ig_account_id 改為可為 NULL：客戶角色核准並完成平台作業後才回填（design.md 決策 5）

全部為新增或放寬限制，既有資料不需轉換。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('tenants',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False, comment='客戶名稱，管理員開通時填寫'),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_index(op.f('ix_tenants_status'), 'tenants', ['status'], unique=False)

    op.add_column('personas', sa.Column('tenant_id', sa.Integer(), nullable=True, comment='擁有者。NULL 表示系統內建角色（來自 YAML 檔）'))
    op.add_column('personas', sa.Column('card', sa.JSON(), nullable=True, comment='整份人設卡（結構同 app/personas/schema.py 的 PersonaCard，不含 slug）。內建角色為 NULL，內容以 YAML 檔為準'))
    op.create_index(op.f('ix_personas_tenant_id'), 'personas', ['tenant_id'], unique=False)
    op.create_foreign_key('fk_personas_tenant_id_tenants', 'personas', 'tenants', ['tenant_id'], ['id'])
    op.alter_column('personas', 'ig_account_id', existing_type=sa.String(length=64), nullable=True)

    op.create_table('persona_coaching',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('persona_id', sa.Integer(), nullable=False),
    sa.Column('disabled_rule_ids', sa.JSON(), nullable=True),
    sa.Column('custom_rules', sa.JSON(), nullable=True),
    sa.Column('report_tone', sa.Text(), nullable=True),
    sa.Column('report_length', sa.Text(), nullable=True),
    sa.Column('report_sections', sa.JSON(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['persona_id'], ['personas.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_persona_coaching_persona_id'), 'persona_coaching', ['persona_id'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_persona_coaching_persona_id'), table_name='persona_coaching')
    op.drop_table('persona_coaching')

    # 降級前若已有 ig_account_id 為 NULL 的客戶角色，此步會失敗——
    # 刻意不自動刪資料，需先人工處理這些角色
    op.alter_column('personas', 'ig_account_id', existing_type=sa.String(length=64), nullable=False)
    op.drop_constraint('fk_personas_tenant_id_tenants', 'personas', type_='foreignkey')
    op.drop_index(op.f('ix_personas_tenant_id'), table_name='personas')
    op.drop_column('personas', 'card')
    op.drop_column('personas', 'tenant_id')

    op.drop_index(op.f('ix_tenants_status'), table_name='tenants')
    op.drop_table('tenants')
