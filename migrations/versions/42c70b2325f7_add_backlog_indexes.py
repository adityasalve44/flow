"""add backlog indexes

Revision ID: 42c70b2325f7
Revises: 6253ae847a58
Create Date: 2026-09-07 01:03:41.598921

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '42c70b2325f7'
down_revision: str | Sequence[str] | None = '6253ae847a58'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(op.f('ix_flow_candidates_lifecycle_status'), 'candidates', ['lifecycle_status'], unique=False, schema='flow')
    op.create_index(op.f('ix_flow_candidate_profiles_completeness'), 'candidate_profiles', ['completeness'], unique=False, schema='flow')
    op.create_index(op.f('ix_flow_conversations_last_inbound_at'), 'conversations', ['last_inbound_at'], unique=False, schema='flow')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_flow_conversations_last_inbound_at'), table_name='conversations', schema='flow')
    op.drop_index(op.f('ix_flow_candidate_profiles_completeness'), table_name='candidate_profiles', schema='flow')
    op.drop_index(op.f('ix_flow_candidates_lifecycle_status'), table_name='candidates', schema='flow')
