"""
app/models/moderation.py — Moderation events and audit trails (FLOW-029).

Captures abuse, escalation, and block events (§3, §8, FLOW-029 of REVIEW_AND_PLAN.md).
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ModerationEvent(Base):
    """Event recorded when a candidate message triggers moderation or escalation."""

    __tablename__ = "moderation_events"
    __table_args__ = {"schema": "flow"}

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("flow.candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("flow.conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("flow.messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    candidate: Mapped["Candidate"] = relationship("Candidate")
    conversation: Mapped["Conversation | None"] = relationship("Conversation")
    message: Mapped["Message | None"] = relationship("Message")

    def __repr__(self) -> str:
        return (
            f"<ModerationEvent id={self.id} candidate_id={self.candidate_id} "
            f"kind={self.kind} created_at={self.created_at}>"
        )
