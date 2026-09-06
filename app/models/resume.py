"""
app/models/resume.py — Resume: versioned file references for candidate resumes.

Key invariants (§6, §8, FLOW-035 of REVIEW_AND_PLAN.md):
- A partial unique index enforces at most one `is_current=True` row per candidate_id.
- Multiple versions (1, 2, 3...) can exist for a candidate, but exactly one is current.
- Checksum deduplication: identical checksum confirms the existing version instead of inserting a duplicate.
- Binary data is never stored in the database — only storage pointers (bucket, object_key).
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import SourceEnum

if TYPE_CHECKING:
    from app.models.candidate import Candidate


class Resume(Base):
    """Versioned resume references for a candidate."""

    __tablename__ = "resumes"
    __table_args__ = (
        Index(
            "ix_flow_resumes_candidate_current",
            "candidate_id",
            unique=True,
            postgresql_where="is_current = true",
        ),
        Index(
            "ix_flow_resumes_candidate_checksum",
            "candidate_id",
            "checksum",
        ),
        Index(
            "ix_flow_resumes_candidate_version",
            "candidate_id",
            "version",
        ),
        {"schema": "flow"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow.candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    is_current: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    source: Mapped[SourceEnum] = mapped_column(
        Enum(SourceEnum, name="source_enum", schema="flow"),
        nullable=False,
        default=SourceEnum.candidate_stated,
        server_default=SourceEnum.candidate_stated.value,
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    parse_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default="pending",
    )

    # Relationships
    candidate: Mapped["Candidate"] = relationship(  # noqa: UP037  (TYPE_CHECKING-only name)
        "Candidate",
        back_populates="resumes",
    )

    def __repr__(self) -> str:
        return (
            f"<Resume id={self.id} candidate_id={self.candidate_id} "
            f"version={self.version} is_current={self.is_current} "
            f"filename={self.filename!r}>"
        )
