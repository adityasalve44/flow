"""
app/models/recruiter.py — Recruiter, RecruiterNote (FLOW-038).

Recruiter accounts and their private notes on candidates.

Key invariants:
- api_key_hash stores only a SHA-256 hash — the plaintext key is shown to
  the recruiter exactly once, at creation, and never persisted or
  retrievable again. See app/api/auth.py for generation/verification.
- RecruiterNote is never read by any candidate-facing code path. There is
  no candidate-facing API in Flow at all today, and no tool
  (app/tools/*.py) queries this table — the isolation is structural, not a
  convention that has to be remembered per call site.
"""

import uuid

from sqlalchemy import (
    Boolean,
    Enum,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import RecruiterRoleEnum


class Recruiter(Base, TimestampMixin):
    """A recruiter account. Auth today is a hashed API key (FLOW-038);
    designed so JWT can replace the credential check in app/api/auth.py
    without touching this model or any route."""

    __tablename__ = "recruiters"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[RecruiterRoleEnum] = mapped_column(
        Enum(RecruiterRoleEnum, name="recruiter_role_enum", schema="flow"),
        nullable=False,
        default=RecruiterRoleEnum.recruiter,
        server_default=RecruiterRoleEnum.recruiter.value,
    )
    # SHA-256 hex digest of the API key. Unique so a lookup by hash is O(1)
    # via the index, and so two recruiters can never collide on one key.
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    def __repr__(self) -> str:
        return f"<Recruiter id={self.id} email={self.email} role={self.role}>"


class RecruiterNote(Base, TimestampMixin):
    """A private note a recruiter leaves on a candidate. Never candidate-
    visible — see the module docstring."""

    __tablename__ = "recruiter_notes"

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
    recruiter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow.recruiters.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    note: Mapped[str] = mapped_column(Text, nullable=False)

    recruiter: Mapped[Recruiter | None] = relationship("Recruiter")

    def __repr__(self) -> str:
        return f"<RecruiterNote id={self.id} candidate_id={self.candidate_id}>"
