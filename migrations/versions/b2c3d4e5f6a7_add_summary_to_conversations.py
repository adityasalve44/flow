"""add_summary_to_conversations

Revision ID: b2c3d4e5f6a7
Revises: f6b9affc97e7
Create Date: 2026-09-06 17:15:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: str | Sequence[str] | None = 'f6b9affc97e7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'conversations',
        sa.Column('summary', sa.Text(), nullable=True),
        schema='flow',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('conversations', 'summary', schema='flow')
