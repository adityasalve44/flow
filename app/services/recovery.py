"""
app/services/recovery.py — Outage recovery, checkpointing, and stranded message sweeper.

Responsibilities:
1. Detect active conversations stranded during an agent API blackout or network outage.
2. Check if a human admin or recruiter messaged personally while the system was down.
3. Classify candidate sentiment during blackout:
   - Cursing/Abuse: Flag candidate, escalate conversation, stop replying immediately (disengage_silent).
   - Frustrated: Apologize honestly for ghosting/connection drop, de-escalate, offer recruiter call.
   - Polite: Apologize warmly for the technical pause, thank for patience, continue intake.
4. Process all unreplied messages in chronological order and dispatch response.
"""

import logging
import re
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any
from uuid import UUID

from app.api.schemas import InboundEvent
from app.db.uow import UnitOfWork
from app.domain.moderation import check_abuse_lexicon, record_moderation_event
from app.models import Conversation, Message
from app.models.enums import ConversationStatusEnum, DirectionEnum
from app.services.turn import TurnResult, TurnService

logger = logging.getLogger(__name__)


class BlackoutSentiment(str, Enum):
    CURSING = "cursing"
    FRUSTRATED = "frustrated"
    POLITE = "polite"
    NORMAL = "normal"


FRUSTRATION_PATTERNS = [
    re.compile(r"\b(why\s*(are\s*you\s*not|aren'?t\s*you)\s*reply(ing)?)\b", re.IGNORECASE),
    re.compile(r"\b(why\s*did\s*you\s*stop\s*reply(ing)?)\b", re.IGNORECASE),
    re.compile(r"\b(ghost(ed|ing)?\s*me|did\s*you\s*ghost\s*me)\b", re.IGNORECASE),
    re.compile(r"\b(waste\s*of\s*time|wasting\s*my\s*time|scam|fraud)\b", re.IGNORECASE),
    re.compile(r"\b(anyone\s*alive|ridiculous|useless\s*(bot)?|fake)\b", re.IGNORECASE),
]

POLITE_CHECKIN_PATTERNS = [
    re.compile(r"\b(are\s*you\s*there|you\s*there)\b", re.IGNORECASE),
    re.compile(r"\b(please\s*reply|kindly\s*reply|waiting\s*for\s*(your\s*)?reply)\b", re.IGNORECASE),
    re.compile(r"\b(hi|hello|hey|priya)\s*\?+", re.IGNORECASE),
    re.compile(r"\b(still\s*there|any\s*update)\b", re.IGNORECASE),
]


def classify_blackout_sentiment(text: str | None) -> BlackoutSentiment:
    """Determine the candidate's tone during a delay or outage."""
    if not text:
        return BlackoutSentiment.NORMAL

    # 1. Severe cursing / hostile abuse
    if check_abuse_lexicon(text):
        return BlackoutSentiment.CURSING

    clean = text.strip()

    # 2. Frustrated / ghosting complaint
    if any(p.search(clean) for p in FRUSTRATION_PATTERNS):
        return BlackoutSentiment.FRUSTRATED

    # 3. Polite check-in
    if any(p.search(clean) for p in POLITE_CHECKIN_PATTERNS):
        return BlackoutSentiment.POLITE

    return BlackoutSentiment.NORMAL


def has_admin_intervened(uow: UnitOfWork, conversation_id: UUID) -> bool:
    """
    Check if a human admin or recruiter messaged the candidate personally
    or took over the conversation while the system was down.
    """
    conv = uow.conversations.get_by_id(conversation_id)
    if not conv:
        return False

    if conv.status in (ConversationStatusEnum.escalated, ConversationStatusEnum.closed):
        return True

    # Check the latest outbound message
    recent_msgs = uow.messages.get_recent(conversation_id, limit=3)
    for m in recent_msgs:
        if m.direction == DirectionEnum.outbound:
            # Check for admin signature/prefix
            cid = m.channel_message_id or ""
            if cid.startswith("admin-") or cid.startswith("recruiter-") or m.media_ref == "admin":
                return True
            break

    return False


def find_stranded_conversations(
    uow: UnitOfWork,
    min_idle_seconds: int = 15,
    now: datetime | None = None,
) -> list[Conversation]:
    """
    Find active conversations where candidate inbound messages arrived
    without an outbound response and have been idle for >= min_idle_seconds.
    """
    current_time = now or datetime.now(UTC)
    cutoff = current_time - timedelta(seconds=min_idle_seconds)

    # Active conversations where last_inbound_at > last_outbound_at
    active_convs = (
        uow.session.query(Conversation)
        .filter(
            Conversation.status == ConversationStatusEnum.active,
            Conversation.last_inbound_at.isnot(None),
            Conversation.last_inbound_at <= cutoff,
        )
        .all()
    )

    stranded: list[Conversation] = []
    for c in active_convs:
        if c.last_outbound_at is None or (c.last_inbound_at and c.last_inbound_at > c.last_outbound_at):
            stranded.append(c)

    return stranded


async def recover_stranded_conversation(
    uow: UnitOfWork,
    conversation: Conversation,
    turn_service: TurnService,
    channel_client: Any | None = None,
    now: datetime | None = None,
) -> TurnResult | None:
    """
    Recover a stranded conversation by bundling all unhandled inbound messages,
    evaluating sentiment, and generating an appropriate comeback.
    """
    current_time = now or datetime.now(UTC)

    # 1. Yield to admin if admin took over
    if has_admin_intervened(uow, conversation.id):
        logger.info("Admin has intervened on conversation %s; skipping bot recovery", conversation.id)
        return None

    # 2. Collect all inbound messages since last_outbound_at
    query = (
        uow.session.query(Message)
        .filter(
            Message.conversation_id == conversation.id,
            Message.direction == DirectionEnum.inbound,
        )
        .order_by(Message.created_at.asc())
    )
    if conversation.last_outbound_at:
        query = query.filter(Message.created_at > conversation.last_outbound_at)

    inbound_msgs = query.all()
    if not inbound_msgs:
        return None

    combined_text = "\n".join((m.body or "").strip() for m in inbound_msgs if (m.body or "").strip())
    candidate = uow.candidates.get_by_id(conversation.candidate_id)
    if not candidate:
        return None

    # 3. Classify sentiment
    sentiment = classify_blackout_sentiment(combined_text)

    # 4. Severe cursing / abuse: Flag candidate, escalate, stop replying immediately
    if sentiment == BlackoutSentiment.CURSING:
        logger.warning(
            "Candidate %s cursed during blackout; escalating conversation and stopping replies",
            candidate.id,
        )
        conversation.abuse_count += 2
        conversation.status = ConversationStatusEnum.escalated
        conversation.closed_at = current_time

        record_moderation_event(
            uow=uow,
            candidate_id=candidate.id,
            conversation_id=conversation.id,
            message_id=inbound_msgs[-1].id,
            kind="escalated",
            detail="Candidate abusive/cursing during outage; flagged for admin intervention",
        )
        uow.commit()

        return TurnResult(
            candidate_id=candidate.id,
            conversation_id=conversation.id,
            reply_text="",
            directive="disengage_silent",
            mode=conversation.mode.value if hasattr(conversation.mode, "value") else str(conversation.mode),
            is_closed=True,
            inbound_message_id=inbound_msgs[-1].id,
            outbound_message_id=None,
        )

    # 5. Process turn with comeback sentiment attached
    latest_msg = inbound_msgs[-1]
    event = InboundEvent(
        phone_number=candidate.phone_number,
        contact_name=candidate.display_name,
        channel_message_id=latest_msg.channel_message_id or f"recov-{latest_msg.id}",
        message=combined_text,
        timestamp=latest_msg.created_at,
        channel=conversation.channel,
        sender_phone=candidate.phone_number,
        body=combined_text,
        received_at=latest_msg.created_at,
    )

    # Run turn with blackout sentiment context
    result = await turn_service.run(
        event,
        now=current_time,
        blackout_sentiment=sentiment.value if sentiment in (BlackoutSentiment.POLITE, BlackoutSentiment.FRUSTRATED) else None,
    )

    # 6. Dispatch via channel client if available
    if channel_client and result.reply_text:
        try:
            await channel_client.send_text_message(
                to_phone=candidate.phone_number,
                text=result.reply_text,
                reply_to_message_id=latest_msg.channel_message_id,
                last_inbound_at=conversation.last_inbound_at,
            )
        except Exception as err:
            logger.error(
                "Failed to send recovered reply to %s: %s",
                candidate.phone_number,
                err,
            )

    return result
