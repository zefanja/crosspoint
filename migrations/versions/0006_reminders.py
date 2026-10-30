"""daily reminders (web push)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-30 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('reminders',
    sa.Column('user_id', sa.String(length=26), nullable=False),
    sa.Column('time', sa.String(length=5), nullable=False),
    sa.Column('lang', sa.String(length=10), nullable=False),
    sa.Column('last_sent_on', sa.Date(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id')
    )
    op.create_table('push_subscriptions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=False),
    sa.Column('endpoint', sa.String(length=1000), nullable=False),
    sa.Column('p256dh', sa.String(length=200), nullable=False),
    sa.Column('auth', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('endpoint')
    )
    with op.batch_alter_table('push_subscriptions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_push_subscriptions_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('push_subscriptions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_push_subscriptions_user_id'))
    op.drop_table('push_subscriptions')
    op.drop_table('reminders')
