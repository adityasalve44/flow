"""
app/models/attribute.py — CandidateAttribute: the fact store with provenance.

Every claim a candidate makes (or that any source produces) is an immutable
append to this table.  Facts are never edited in place — they are superseded,
creating an auditable chain.

Key invariants:
- A partial unique index enforces at most one ``status=current`` row per
  (candidate_id, key) pair at the database level.
- Every attribute points at the conversation and message that produced it,
  so every fact is traceable to its sentence.
- ``data_class`` is assigned from the key registry — never guessed at runtime.
  Unknown keys default to ``personal`` (fail closed).
- Sensitive facts (personal/protected) have full provenance and are stored,
  but ``rebuild_projection`` filters them out so they never reach the
  ``candidate_profiles`` table or any recruiter payload.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import (
    AttributeStatusEnum,
    ConfidenceEnum,
    DataClassEnum,
    SourceEnum,
)


class CandidateAttribute(Base):
    """The fact store — every claim ever made, with full provenance."""
    __tablename__ = "candidate_attributes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    # Structured value — can hold numeric, string, or complex structured data
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # The raw text from which this fact was extracted, for audit
    raw_text: Mapped[str | None] = mapped_column(Text)

    source: Mapped[SourceEnum] = mapped_column(
        Enum(SourceEnum, name="source_enum", schema="flow"),
        nullable=False,
    )
    confidence: Mapped[ConfidenceEnum] = mapped_column(
        Enum(ConfidenceEnum, name="confidence_enum", schema="flow"),
        nullable=False,
    )
    status: Mapped[AttributeStatusEnum] = mapped_column(
        Enum(AttributeStatusEnum, name="attribute_status_enum", schema="flow"),
        nullable=False,
        default=AttributeStatusEnum.current,
        server_default=AttributeStatusEnum.current.value,
    )
    data_class: Mapped[DataClassEnum] = mapped_column(
        Enum(DataClassEnum, name="data_class_enum", schema="flow"),
        nullable=False,
    )

    # Traceability — every fact points at the turn that produced it
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
    )
    message_id: Mapped[str | None] = mapped_column(String(255))

    # Supersession chain — allows tracing history
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("candidate_attributes.id", ondelete="SET NULL"),
    )

    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Indexes
    __table_args__ = (
        # Partial unique: at most one current value per (candidate, key)
        Index(
            "ix_candidate_attributes_current",
            "candidate_id",
            "key",
            unique=True,
            postgresql_where="status = 'current'",
        ),
        # Fast retrieval by candidate + status
        Index(
            "ix_candidate_attributes_candidate_status",
            "candidate_id",
            "status",
        ),
        # Fast retrieval by candidate + data_class (for projection filtering)
        Index(
            "ix_candidate_attributes_candidate_class",
            "candidate_id",
            "data_class",
        ),
    )
