"""
app/repositories/audit.py — AuditRepository: database operations for AuditEvent.

Key invariants:
- Repositories only read/stage changes against the Session; they never commit.
- Transaction boundaries belong strictly to the UnitOfWork.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent


class AuditRepository:
    """Repository for AuditEvent entity (FLOW-035, FLOW-037)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, event: AuditEvent) -> AuditEvent:
        """Stage a new AuditEvent record."""
        self.session.add(event)
        return event

    def get_by_entity(self, entity_type: str, entity_id: str | UUID) -> list[AuditEvent]:
        """Fetch all audit events for a specific entity, newest first."""
        return list(
            self.session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.entity_type == entity_type,
                    AuditEvent.entity_id == str(entity_id),
                )
                .order_by(AuditEvent.created_at.desc())
            ).scalars().all()
        )

    def get_by_actor(self, actor_type: str, actor_id: str | None = None) -> list[AuditEvent]:
        """Fetch audit events by actor, newest first."""
        query = select(AuditEvent).where(AuditEvent.actor_type == actor_type)
        if actor_id is not None:
            query = query.where(AuditEvent.actor_id == actor_id)
        return list(
            self.session.execute(
                query.order_by(AuditEvent.created_at.desc())
            ).scalars().all()
        )
