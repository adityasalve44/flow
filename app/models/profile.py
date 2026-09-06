"""
app/models/profile.py — candidate_profiles (projection) and preference tables.

``CandidateProfile`` is a derived projection of confirmed, current, operational
facts.  It is never written by hand — only by ``rebuild_projection()`` in
app/domain/projection.py.

The preference tables (skills, role preferences, location preferences) are also
derived and part of the fast-query surface for the future matching system.

Money is stored as NUMERIC via Python's Decimal — never Float.
Currency is always explicit.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class CandidateSkill(Base, TimestampMixin):
    """Normalised skills, deduped per candidate."""
    __tablename__ = "candidate_skills"

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
    skill_raw: Mapped[str] = mapped_column(String(255), nullable=False)
    skill_norm: Mapped[str] = mapped_column(String(255), nullable=False)
    years: Mapped[float | None] = mapped_column()
    source: Mapped[str | None] = mapped_column(String(50))
    confidence: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="current", server_default="current"
    )

    __table_args__ = (
        UniqueConstraint("candidate_id", "skill_norm", name="uq_candidate_skill_norm"),
    )


class CandidateRolePref(Base, TimestampMixin):
    """Current vs desired role preferences."""
    __tablename__ = "candidate_role_prefs"

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
    role_raw: Mapped[str] = mapped_column(String(255), nullable=False)
    role_norm: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # "current" or "desired"
    strength: Mapped[float] = mapped_column(
        nullable=False, default=1.0, server_default="1.0"
    )


class CandidateLocationPref(Base, TimestampMixin):
    """Where the candidate is willing to actually work.

    strength differentiates:
      preferred   — first choice
      acceptable  — would take it
      excluded    — hard no
    """
    __tablename__ = "candidate_location_prefs"

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
    location_raw: Mapped[str] = mapped_column(String(255), nullable=False)
    location_norm: Mapped[str] = mapped_column(String(255), nullable=False)
    strength: Mapped[str] = mapped_column(
        String(20), nullable=False, default="preferred", server_default="preferred"
    )  # preferred | acceptable | excluded
    work_mode: Mapped[str | None] = mapped_column(String(50))
    is_hard_requirement: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
