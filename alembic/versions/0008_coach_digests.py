"""coach digests

daily-coach-report-link 任務 3.1：教練回饋改為凌晨四點合併送出連結。
新增 coach_digests 表與 sessions.coach_digest_id。

既有資料：已結束、但從未送過按鈕邀請也沒有略過原因的舊場次（例如 M9 之前
結束的），標記為 before_daily_digest，避免第一次每日送出把幾週前的對話一起
寄給使用者。

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-21

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('coach_digests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('link_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True, comment='連結訊息實際送出的時刻；未送出為 NULL'),
    sa.Column('skip_reason', sa.String(length=64), nullable=True, comment='未能送出的原因（例如訊息視窗已逾期）'),
    sa.ForeignKeyConstraint(['link_id'], ['user_persona_links.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_coach_digests_link_id'), 'coach_digests', ['link_id'], unique=False)
    with op.batch_alter_table('sessions') as batch:
        batch.add_column(sa.Column('coach_digest_id', sa.Integer(), nullable=True, comment='併入的每日回饋；NULL 表示尚未送出'))
        batch.create_index('ix_sessions_coach_digest_id', ['coach_digest_id'], unique=False)
        batch.create_foreign_key('fk_sessions_coach_digest_id', 'coach_digests', ['coach_digest_id'], ['id'])
    op.execute(
        "UPDATE sessions SET invite_skip_reason = 'before_daily_digest' "
        "WHERE state = 'ended' AND invite_sent = false AND invite_skip_reason IS NULL"
    )


def downgrade() -> None:
    op.execute("UPDATE sessions SET invite_skip_reason = NULL WHERE invite_skip_reason = 'before_daily_digest'")
    with op.batch_alter_table('sessions') as batch:
        batch.drop_constraint('fk_sessions_coach_digest_id', type_='foreignkey')
        batch.drop_index('ix_sessions_coach_digest_id')
        batch.drop_column('coach_digest_id')
    op.drop_index(op.f('ix_coach_digests_link_id'), table_name='coach_digests')
    op.drop_table('coach_digests')
