"""
app/repositories/conversation.py — read-only conversation/message data access.

IMPORTANT: No commit() calls here. The service layer (UnitOfWork) owns
all transactions.  These functions only read and stage (flush) — they
never commit.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Conversation, Message


def get_active_conversation(
    db: Session,
    candidate_id,
) -> Conversation | None:
    """Return the most recent active conversation for a candidate."""
    from app.models.enums import ConversationStatusEnum
    statement = (
        select(Conversation)
        .where(
            Conversation.candidate_id == candidate_id,
            Conversation.status == ConversationStatusEnum.active,
        )
        .order_by(Conversation.started_at.desc())
        .limit(1)
    )
    return db.scalar(statement)


def get_recent_messages(
    db: Session,
    conversation_id,
    limit: int = 20,
) -> list[Message]:
    statement = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    return list(reversed(db.scalars(statement).all()))
