"""group deck edit log

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-28 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('group_deck_edits',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('note_id', sa.Integer(), nullable=False),
    sa.Column('deck_id', sa.Integer(), nullable=False),
    sa.Column('action', sa.String(length=10), nullable=False),
    sa.Column('summary', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('group_deck_edits', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_group_deck_edits_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_group_deck_edits_note_id'), ['note_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_group_deck_edits_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('group_deck_edits', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_group_deck_edits_user_id'))
        batch_op.drop_index(batch_op.f('ix_group_deck_edits_note_id'))
        batch_op.drop_index(batch_op.f('ix_group_deck_edits_created_at'))

    op.drop_table('group_deck_edits')
