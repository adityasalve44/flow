"""
app/models/audit.py — Audit events for mutations, recruiter access, and lifecycle changes.

Matches §6 of REVIEW_AND_PLAN.md:
| audit_events | Who changed what | actor_type (system/agent/recruiter) · actor_id · entity_type · entity_id · action · before jsonb · after jsonb · created_at |
"""

from datetime import datetime
import uuid

from sqlalchemy import (
    DateTime,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AuditEvent(Base):
    """Immutable record of changes, accesses, and lifecycle events."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index(
            "ix_flow_audit_events_entity",
            "entity_type",
            "entity_id",
        ),
        Index(
            "ix_flow_audit_events_created_at",
            "created_at",
        ),
        {"schema": "flow"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    actor_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    before: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<AuditEvent id={self.id} actor={self.actor_type}:{self.actor_id} "
            f"action={self.action} entity={self.entity_type}:{self.entity_id}>"
        )
