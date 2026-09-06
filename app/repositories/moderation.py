"""
app/repositories/moderation.py — Moderation events data access (FLOW-029).

Repositories only read and stage (flush) — transactions are owned by UnitOfWork.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.moderation import ModerationEvent


class ModerationEventRepository:
    """Repository for ModerationEvent audit records."""

    def __init__(self, session: Session):
        self.session = session

    def add(self, event: ModerationEvent) -> ModerationEvent:
        self.session.add(event)
        self.session.flush()
        return event

    def create(
        self,
        candidate_id: UUID | str,
        kind: str,
        detail: str | None = None,
        conversation_id: UUID | str | None = None,
        message_id: UUID | str | None = None,
    ) -> ModerationEvent:
        event = ModerationEvent(
            candidate_id=candidate_id,
            conversation_id=conversation_id,
            message_id=message_id,
            kind=kind,
            detail=detail,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def get_by_candidate(
        self,
        candidate_id: UUID | str,
        limit: int = 20,
    ) -> list[ModerationEvent]:
        stmt = (
            select(ModerationEvent)
            .where(ModerationEvent.candidate_id == candidate_id)
            .order_by(ModerationEvent.created_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt).all())
