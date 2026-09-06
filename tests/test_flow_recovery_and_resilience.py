"""
tests/test_flow_recovery_and_resilience.py — Tests for outage recovery, debouncing,
sentiment-aware blackout comebacks, admin intervention guard, and multi-industry intake.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.api.schemas import InboundEvent
from app.channel.debounce import MessageDebouncer
from app.db.uow import UnitOfWork
from app.domain.completeness import get_missing_blocking_fields, is_profile_ready
from app.domain.merge import Fact
from app.models import Message
from app.models.enums import (
    AttributeStatusEnum,
    ChannelEnum,
    ConfidenceEnum,
    ConversationModeEnum,
    ConversationStatusEnum,
    DirectionEnum,
)
from app.services.recovery import (
    BlackoutSentiment,
    classify_blackout_sentiment,
    find_stranded_conversations,
    has_admin_intervened,
    recover_stranded_conversation,
)
from app.services.turn import TurnResult


@pytest.mark.asyncio
async def test_debouncer_merges_rapid_messages():
    """Verify rapid consecutive messages from the same sender are merged into one."""
    dispatched_events: list[InboundEvent] = []

    async def on_dispatch(event: InboundEvent) -> None:
        dispatched_events.append(event)

    debouncer = MessageDebouncer(on_dispatch=on_dispatch, window_seconds=0.1)

    phone = "+919876543210"
    event1 = InboundEvent(
        phone_number=phone,
        message="Hey Priya",
        channel_message_id="msg-1",
    )
    event2 = InboundEvent(
        phone_number=phone,
        message="I have 5 years exp in B2B Sales",
        channel_message_id="msg-2",
    )
    event3 = InboundEvent(
        phone_number=phone,
        message="Looking for 15 LPA in Mumbai, 30 days notice",
        channel_message_id="msg-3",
    )

    await debouncer.enqueue(event1)
    await debouncer.enqueue(event2)
    await debouncer.enqueue(event3)

    # Wait for debounce window to expire
    await asyncio.sleep(0.2)

    assert len(dispatched_events) == 1
    merged = dispatched_events[0]
    assert merged.phone_number == phone
    assert "Hey Priya" in merged.message
    assert "B2B Sales" in merged.message
    assert "15 LPA" in merged.message
    assert merged.channel_message_id == "msg-3"


def test_classify_blackout_sentiment():
    """Verify sentiment classification for candidate messages during delays."""
    # Polite check-ins
    assert classify_blackout_sentiment("Are you there?") == BlackoutSentiment.POLITE
    assert classify_blackout_sentiment("please reply") == BlackoutSentiment.POLITE
    assert classify_blackout_sentiment("Hello???") == BlackoutSentiment.POLITE
    assert classify_blackout_sentiment("hey priya?") == BlackoutSentiment.POLITE

    # Frustrated complaints (ghosting, delay)
    assert classify_blackout_sentiment("why did you stop replying?") == BlackoutSentiment.FRUSTRATED
    assert classify_blackout_sentiment("did you ghost me?") == BlackoutSentiment.FRUSTRATED
    assert classify_blackout_sentiment("stop wasting my time") == BlackoutSentiment.FRUSTRATED
    assert classify_blackout_sentiment("anyone alive? what a useless bot") == BlackoutSentiment.FRUSTRATED

    # Severe cursing / abuse
    assert classify_blackout_sentiment("fuck you reply now") == BlackoutSentiment.CURSING
    assert classify_blackout_sentiment("shut up you idiot") == BlackoutSentiment.CURSING

    # Normal message
    assert classify_blackout_sentiment("I am a sales executive with 3 years exp") == BlackoutSentiment.NORMAL


def test_find_stranded_conversations(db):
    """Verify detection of conversations where candidate messages had no outbound reply."""
    uow = UnitOfWork(session=db)
    phone = f"+9198{uuid4().int % 100000000:08d}"
    t0 = datetime.now(UTC) - timedelta(minutes=5)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        conv = uow.conversations.create(
            candidate_id=cand.id,
            channel=ChannelEnum.simulator,
            mode=ConversationModeEnum.intake,
        )
        conv.last_inbound_at = t0
        conv.last_outbound_at = None
        uow.conversations.add(conv)
        uow.commit()

        stranded = find_stranded_conversations(uow, min_idle_seconds=10)
        assert any(c.id == conv.id for c in stranded)


def test_has_admin_intervened(db):
    """Verify automated system yields when an admin or human recruiter has stepped in."""
    uow = UnitOfWork(session=db)
    phone = f"+9198{uuid4().int % 100000000:08d}"

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        conv = uow.conversations.create(
            candidate_id=cand.id,
            channel=ChannelEnum.simulator,
            mode=ConversationModeEnum.intake,
        )
        uow.commit()

        # No admin message yet
        assert has_admin_intervened(uow, conv.id) is False

        # Admin sends an outbound message
        admin_msg = Message(
            conversation_id=conv.id,
            candidate_id=cand.id,
            direction=DirectionEnum.outbound,
            channel_message_id=f"admin-{uuid4()}",
            body="Hello, I am recruiter Rohit stepping in to help.",
            created_at=datetime.now(UTC),
        )
        uow.messages.add(admin_msg)
        uow.commit()

        # Admin intervention detected
        assert has_admin_intervened(uow, conv.id) is True


@pytest.mark.asyncio
async def test_recover_stranded_conversation_with_polite_apology(db):
    """Verify recovery sweeper bundles stranded messages and triggers polite comeback."""
    uow = UnitOfWork(session=db)
    phone = f"+9198{uuid4().int % 100000000:08d}"
    t0 = datetime.now(UTC) - timedelta(minutes=2)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        conv = uow.conversations.create(
            candidate_id=cand.id,
            channel=ChannelEnum.simulator,
            mode=ConversationModeEnum.intake,
        )
        conv.last_inbound_at = t0
        uow.conversations.add(conv)

        # Inbound messages sent during blackout
        msg1 = Message(
            conversation_id=conv.id,
            candidate_id=cand.id,
            direction=DirectionEnum.inbound,
            channel_message_id=f"msg-{uuid4()}",
            body="I am an HR Manager with 6 years experience",
            created_at=t0,
        )
        msg2 = Message(
            conversation_id=conv.id,
            candidate_id=cand.id,
            direction=DirectionEnum.inbound,
            channel_message_id=f"msg-{uuid4()}",
            body="Are you there? Please reply",
            created_at=t0 + timedelta(seconds=10),
        )
        uow.messages.add(msg1)
        uow.messages.add(msg2)
        uow.commit()

    mock_turn_service = MagicMock()
    mock_turn_service.run = AsyncMock(
        return_value=TurnResult(
            candidate_id=cand.id,
            conversation_id=conv.id,
            reply_text="Hey! Really sorry for the pause there, had a brief glitch on my side. Thanks for your patience! Could you share your expected CTC and notice period?",
            directive="blackout_apology_polite",
            mode="intake",
        )
    )

    result = await recover_stranded_conversation(uow, conv, mock_turn_service)
    assert result is not None
    assert result.directive == "blackout_apology_polite"
    mock_turn_service.run.assert_called_once()
    call_kwargs = mock_turn_service.run.call_args.kwargs
    assert call_kwargs["blackout_sentiment"] == "polite"


@pytest.mark.asyncio
async def test_recover_stranded_conversation_with_cursing_escalation(db):
    """Verify that severe cursing during outage flags the candidate, escalates, and stops replies."""
    uow = UnitOfWork(session=db)
    phone = f"+9198{uuid4().int % 100000000:08d}"
    t0 = datetime.now(UTC) - timedelta(minutes=2)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        conv = uow.conversations.create(
            candidate_id=cand.id,
            channel=ChannelEnum.simulator,
            mode=ConversationModeEnum.intake,
        )
        conv.last_inbound_at = t0
        uow.conversations.add(conv)

        msg = Message(
            conversation_id=conv.id,
            candidate_id=cand.id,
            direction=DirectionEnum.inbound,
            channel_message_id=f"msg-{uuid4()}",
            body="fuck you reply right now",
            created_at=t0,
        )
        uow.messages.add(msg)
        uow.commit()

    mock_turn_service = MagicMock()
    result = await recover_stranded_conversation(uow, conv, mock_turn_service)

    assert result is not None
    assert result.directive == "disengage_silent"
    assert result.reply_text == ""
    assert result.is_closed is True
    # Zero model calls were made
    mock_turn_service.run.assert_not_called()

    # DB state: conversation escalated
    with uow:
        updated_conv = uow.conversations.get_by_id(conv.id)
        assert updated_conv.status == ConversationStatusEnum.escalated
        assert updated_conv.abuse_count >= 2


def test_multi_industry_profile_completion():
    """Verify that non-tech profiles (e.g. Sales Manager, Nurse, Operations) reach completeness."""
    sales_facts = [
        Fact(
            id=uuid4(),
            candidate_id=uuid4(),
            key="desired_role",
            value="Regional Sales Manager",
            raw_text="Regional Sales Manager",
            source="candidate_stated",
            confidence=ConfidenceEnum.confirmed.value,
            status=AttributeStatusEnum.current.value,
            data_class="professional",
        ),
        Fact(
            id=uuid4(),
            candidate_id=uuid4(),
            key="experience_years",
            value=7,
            raw_text="7 years",
            source="candidate_stated",
            confidence=ConfidenceEnum.confirmed.value,
            status=AttributeStatusEnum.current.value,
            data_class="professional",
        ),
        Fact(
            id=uuid4(),
            candidate_id=uuid4(),
            key="skills",
            value=["b2b_sales", "lead_generation", "crm", "team_management"],
            raw_text="B2B sales, lead gen, CRM",
            source="candidate_stated",
            confidence=ConfidenceEnum.confirmed.value,
            status=AttributeStatusEnum.current.value,
            data_class="professional",
        ),
        Fact(
            id=uuid4(),
            candidate_id=uuid4(),
            key="location_preference",
            value=["mumbai", "pune"],
            raw_text="Mumbai or Pune",
            source="candidate_stated",
            confidence=ConfidenceEnum.confirmed.value,
            status=AttributeStatusEnum.current.value,
            data_class="personal",
        ),
        Fact(
            id=uuid4(),
            candidate_id=uuid4(),
            key="expected_ctc",
            value={"amount": 1800000, "currency": "INR"},
            raw_text="18 LPA",
            source="candidate_stated",
            confidence=ConfidenceEnum.confirmed.value,
            status=AttributeStatusEnum.current.value,
            data_class="compensation",
        ),
        Fact(
            id=uuid4(),
            candidate_id=uuid4(),
            key="notice_period",
            value=30,
            raw_text="30 days",
            source="candidate_stated",
            confidence=ConfidenceEnum.confirmed.value,
            status=AttributeStatusEnum.current.value,
            data_class="operational",
        ),
    ]

    assert is_profile_ready(sales_facts) is True
    missing = get_missing_blocking_fields(sales_facts)
    assert len(missing) == 0
