"""add_resumes_and_audit_events

Revision ID: 668bda89b6be
Revises: b2c3d4e5f6a7
Create Date: 2026-09-06 17:29:29.948709

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '668bda89b6be'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. flow.resumes
    op.create_table(
        'resumes',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('candidate_id', sa.UUID(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('bucket', sa.String(length=128), nullable=False),
        sa.Column('object_key', sa.String(length=512), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=128), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.Column('is_current', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column(
            'source',
            postgresql.ENUM(
                'candidate_stated',
                'candidate_confirmed',
                'resume',
                'llm_inferred',
                'system_calculated',
                'recruiter_verified',
                'channel_metadata',
                name='source_enum',
                schema='flow',
                create_type=False,
            ),
            server_default='candidate_stated',
            nullable=False,
        ),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('parse_status', sa.String(length=32), server_default='pending', nullable=False),
        sa.ForeignKeyConstraint(['candidate_id'], ['flow.candidates.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        schema='flow',
    )
    op.create_index(
        op.f('ix_flow_resumes_candidate_id'),
        'resumes',
        ['candidate_id'],
        unique=False,
        schema='flow',
    )
    op.create_index(
        'ix_flow_resumes_candidate_current',
        'resumes',
        ['candidate_id'],
        unique=True,
        postgresql_where=sa.text('is_current = true'),
        schema='flow',
    )
    op.create_index(
        'ix_flow_resumes_candidate_checksum',
        'resumes',
        ['candidate_id', 'checksum'],
        unique=False,
        schema='flow',
    )
    op.create_index(
        'ix_flow_resumes_candidate_version',
        'resumes',
        ['candidate_id', 'version'],
        unique=False,
        schema='flow',
    )

    # 2. flow.audit_events
    op.create_table(
        'audit_events',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('actor_type', sa.String(length=64), nullable=False),
        sa.Column('actor_id', sa.String(length=255), nullable=True),
        sa.Column('entity_type', sa.String(length=64), nullable=False),
        sa.Column('entity_id', sa.String(length=255), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('before', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('after', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        schema='flow',
    )
    op.create_index(
        'ix_flow_audit_events_entity',
        'audit_events',
        ['entity_type', 'entity_id'],
        unique=False,
        schema='flow',
    )
    op.create_index(
        'ix_flow_audit_events_created_at',
        'audit_events',
        ['created_at'],
        unique=False,
        schema='flow',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_flow_audit_events_created_at', table_name='audit_events', schema='flow')
    op.drop_index('ix_flow_audit_events_entity', table_name='audit_events', schema='flow')
    op.drop_table('audit_events', schema='flow')

    op.drop_index('ix_flow_resumes_candidate_version', table_name='resumes', schema='flow')
    op.drop_index('ix_flow_resumes_candidate_checksum', table_name='resumes', schema='flow')
    op.drop_index('ix_flow_resumes_candidate_current', table_name='resumes', schema='flow')
    op.drop_index(op.f('ix_flow_resumes_candidate_id'), table_name='resumes', schema='flow')
    op.drop_table('resumes', schema='flow')
