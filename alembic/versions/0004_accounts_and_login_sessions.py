"""accounts and login sessions

add-persona-authoring-console 任務 5.2、5.3：後台登入帳號（管理員與客戶）
與伺服器端的登入狀態。兩張皆為新表，不動既有資料。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('accounts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False, comment='登入帳號'),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(role = 'admin' AND tenant_id IS NULL) OR (role = 'client' AND tenant_id IS NOT NULL)", name='ck_accounts_role_tenant'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('username')
    )
    op.create_index(op.f('ix_accounts_tenant_id'), 'accounts', ['tenant_id'], unique=False)

    op.create_table('login_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_login_sessions_account_id'), 'login_sessions', ['account_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_login_sessions_account_id'), table_name='login_sessions')
    op.drop_table('login_sessions')
    op.drop_index(op.f('ix_accounts_tenant_id'), table_name='accounts')
    op.drop_table('accounts')
