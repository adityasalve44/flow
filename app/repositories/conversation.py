"""
app/repositories/conversation.py — conversation and message data access.

IMPORTANT: No commit() calls here. The service layer (UnitOfWork) owns
all transactions. These functions only read and stage (flush) — they
never commit.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Conversation, Message
from app.models.enums import (
    ChannelEnum,
    ConversationModeEnum,
    ConversationStatusEnum,
    DirectionEnum,
)


class ConversationRepository:
    """Repository for Conversation entity."""

    def __init__(self, session: Session):
        self.session = session

    def get_by_id(self, conversation_id: UUID | str) -> Conversation | None:
        statement = select(Conversation).where(Conversation.id == conversation_id)
        return self.session.scalar(statement)

    def get_active(self, candidate_id: UUID | str) -> Conversation | None:
        """Return the most recent active conversation for a candidate."""
        statement = (
            select(Conversation)
            .where(
                Conversation.candidate_id == candidate_id,
                Conversation.status == ConversationStatusEnum.active,
            )
            .order_by(Conversation.started_at.desc())
            .limit(1)
        )
        return self.session.scalar(statement)

    def get_latest(self, candidate_id: UUID | str) -> Conversation | None:
        """Return the most recent conversation for a candidate regardless of status."""
        statement = (
            select(Conversation)
            .where(Conversation.candidate_id == candidate_id)
            .order_by(Conversation.started_at.desc())
            .limit(1)
        )
        return self.session.scalar(statement)

    def create(
        self,
        candidate_id: UUID | str,
        channel: ChannelEnum = ChannelEnum.simulator,
        mode: ConversationModeEnum = ConversationModeEnum.consent,
        adk_session_id: str | None = None,
    ) -> Conversation:
        conversation = Conversation(
            candidate_id=candidate_id,
            channel=channel,
            mode=mode,
            adk_session_id=adk_session_id,
            status=ConversationStatusEnum.active,
        )
        self.session.add(conversation)
        self.session.flush()
        return conversation

    def add(self, conversation: Conversation) -> Conversation:
        self.session.add(conversation)
        self.session.flush()
        return conversation


class MessageRepository:
    """Repository for Message entity."""

    def __init__(self, session: Session):
        self.session = session

    def get_by_id(self, message_id: UUID | str) -> Message | None:
        statement = select(Message).where(Message.id == message_id)
        return self.session.scalar(statement)

    def get_by_channel_message_id(self, channel_message_id: str) -> Message | None:
        """Fetch a message by external BSP message ID (for idempotency check)."""
        statement = select(Message).where(
            Message.channel_message_id == channel_message_id
        )
        return self.session.scalar(statement)

    def get_recent(
        self, conversation_id: UUID | str, limit: int = 20
    ) -> list[Message]:
        statement = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        return list(reversed(self.session.scalars(statement).all()))

    # Alias for readability
    get_recent_by_conversation = get_recent

    def create(
        self,
        conversation_id: UUID | str,
        candidate_id: UUID | str,
        direction: DirectionEnum,
        body: str | None = None,
        channel_message_id: str | None = None,
        media_ref: str | None = None,
    ) -> Message:
        msg = Message(
            conversation_id=conversation_id,
            candidate_id=candidate_id,
            direction=direction,
            body=body,
            channel_message_id=channel_message_id,
            media_ref=media_ref,
        )
        self.session.add(msg)
        self.session.flush()
        return msg

    def add(self, message: Message) -> Message:
        self.session.add(message)
        self.session.flush()
        return message


# Backward-compatible functional interface
def get_active_conversation(
    db: Session,
    candidate_id: UUID | str,
) -> Conversation | None:
    return ConversationRepository(db).get_active(candidate_id)


def get_recent_messages(
    db: Session,
    conversation_id: UUID | str,
    limit: int = 20,
) -> list[Message]:
    return MessageRepository(db).get_recent(conversation_id, limit=limit)
