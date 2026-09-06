from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Conversation


def save_message(
    db: Session,
    candidate_id: int,
    message: str,
    direction: str,
) -> Conversation:

    conversation = Conversation(
        candidate_id=candidate_id,
        message=message,
        direction=direction,
    )

    db.add(conversation)
    db.commit()
    db.refresh(conversation)

    return conversation


def get_recent_messages(
    db: Session,
    candidate_id: int,
    limit: int = 20,
) -> list[Conversation]:

    statement = (
        select(Conversation)
        .where(Conversation.candidate_id == candidate_id)
        .order_by(Conversation.created_at.desc())
        .limit(limit)
    )

    return list(
        reversed(db.scalars(statement).all())
    )
