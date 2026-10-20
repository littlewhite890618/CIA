"""send errors

add-persona-authoring-console 第 9 群：每一次發訊失敗記一筆，含分類與 Meta 的
錯誤原文，供發訊配速的監測與事後追查。新表，不動既有資料。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('send_errors',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('persona_id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=True),
    sa.Column('category', sa.String(length=32), nullable=False,
              comment='rate_limited／policy_block／token_invalid／window_expired／other'),
    sa.Column('http_status', sa.Integer(), nullable=True, comment='連線失敗時為 NULL'),
    sa.Column('error_code', sa.Integer(), nullable=True, comment='Meta 的 error.code'),
    sa.Column('error_subcode', sa.Integer(), nullable=True, comment='Meta 的 error.error_subcode'),
    sa.Column('detail', sa.Text(), nullable=True, comment='錯誤原文，截前 500 字'),
    sa.ForeignKeyConstraint(['persona_id'], ['personas.id'], ),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_send_errors_occurred_at'), 'send_errors', ['occurred_at'], unique=False)
    op.create_index(op.f('ix_send_errors_persona_id'), 'send_errors', ['persona_id'], unique=False)
    op.create_index(op.f('ix_send_errors_category'), 'send_errors', ['category'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_send_errors_category'), table_name='send_errors')
    op.drop_index(op.f('ix_send_errors_persona_id'), table_name='send_errors')
    op.drop_index(op.f('ix_send_errors_occurred_at'), table_name='send_errors')
    op.drop_table('send_errors')
