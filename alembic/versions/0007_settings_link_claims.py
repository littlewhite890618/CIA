"""settings link claims

add-persona-authoring-console 任務 5.8：設定連結只能在打開它的那個瀏覽器上使用。
新表，不動既有資料。

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('settings_link_claims',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False, comment='設定連結 token 的 SHA-256'),
    sa.Column('claim_hash', sa.String(length=64), nullable=False, comment='打開連結的瀏覽器所持代碼的 SHA-256'),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False, comment='連結到期時間，過期一陣子後清除'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_settings_link_claims_expires_at'), 'settings_link_claims', ['expires_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_settings_link_claims_expires_at'), table_name='settings_link_claims')
    op.drop_table('settings_link_claims')
