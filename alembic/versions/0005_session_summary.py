"""session summary

add-persona-authoring-console 任務 8.1（design.md 決策 10）：長對話的較早內容
壓縮成摘要，存在 sessions 表。兩個欄位都可為 NULL，既有場次不受影響。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sessions', sa.Column(
        'summary', sa.Text(), nullable=True,
        comment='這個場次較早訊息的摘要；未超過門檻或尚未產生為 NULL',
    ))
    op.add_column('sessions', sa.Column(
        'summary_until_message_id', sa.Integer(), nullable=True,
        comment='摘要涵蓋到哪一則訊息（含）。之後的訊息以原文放進上下文',
    ))


def downgrade() -> None:
    op.drop_column('sessions', 'summary_until_message_id')
    op.drop_column('sessions', 'summary')
