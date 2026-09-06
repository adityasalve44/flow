"""
tests/test_ingress.py — tests for channel ingress defense.

Tests:
1. Malformed phone number rejected by InboundEvent validator.
2. Oversized message (>4096 chars) rejected by InboundEvent validator.
3. Successful E.164 normalization on valid input.
4. Valid ingress returns IngressDecision.PROCEED.
5. Replay of existing channel_message_id returns IngressDecision.REPLAY.
6. Blocked candidate (blocked_at set) returns IngressDecision.BLOCKED.
7. Rate limit exceeded (counted directly in Postgres) returns IngressDecision.RATE_LIMITED.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pydantic
import pytest

from app.api.schemas import InboundEvent
from app.channel.inbound import IngressDecision, process_ingress
from app.db.uow import UnitOfWork
from app.models.enums import DirectionEnum


def test_malformed_phone_number_rejected():
    """InboundEvent rejects invalid phone numbers (producing HTTP 422)."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        InboundEvent(
            phone_number="not-a-number",
            channel_message_id="msg_001",
            message="Hello",
        )
    assert "Invalid phone number" in str(exc_info.value)


def test_oversized_message_rejected():
    """InboundEvent rejects messages exceeding 4096 characters (HTTP 422)."""
    oversized = "a" * 4097
    with pytest.raises(pydantic.ValidationError) as exc_info:
        InboundEvent(
            phone_number="+919876543210",
            channel_message_id="msg_002",
            message=oversized,
        )
    assert "Message exceeds maximum allowed length" in str(exc_info.value)


def test_e164_normalization_on_ingress():
    """Valid Indian 10-digit number is cleanly normalized to E.164."""
    event = InboundEvent(
        phone_number="9876543210",
        channel_message_id="msg_003",
        message="Hi",
    )
    assert event.phone_number == "+919876543210"


def test_valid_ingress_proceeds(db):
    """Clean inbound event passes boundary defenses and returns PROCEED."""
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    event = InboundEvent(
        phone_number=phone,
        contact_name="Aditya Flow",
        message="Hello! Looking for a role.",
        channel_message_id=f"ext_{uuid4().hex}",
    )

    with uow:
        result = process_ingress(uow, event)
        assert result.decision == IngressDecision.PROCEED
        assert result.candidate is not None
        assert result.candidate.phone_number == phone
        assert result.candidate.display_name == "Aditya Flow"


def test_idempotent_replay_is_noop(db):
    """
    Replaying an already-processed channel_message_id yields IngressDecision.REPLAY
    and points to the existing message without side effects.
    """
    uow = UnitOfWork(session=db)
    phone = f"+9192{uuid4().int % 100000000:08d}"
    channel_msg_id = f"ext_unique_{uuid4().hex}"

    with uow:
        # Simulate an already persisted message in the database
        candidate = uow.candidates.get_or_create_by_phone(phone)
        conv = uow.conversations.create(candidate.id)
        msg = uow.messages.create(
            conversation_id=conv.id,
            candidate_id=candidate.id,
            direction=DirectionEnum.inbound,
            body="First time message",
            channel_message_id=channel_msg_id,
        )

        # Incoming event with the exact same channel_message_id
        event = InboundEvent(
            phone_number=phone,
            message="First time message",
            channel_message_id=channel_msg_id,
        )

        result = process_ingress(uow, event)
        assert result.decision == IngressDecision.REPLAY
        assert result.existing_message is not None
        assert result.existing_message.id == msg.id


def test_blocked_candidate_is_rejected(db):
    """
    A candidate with blocked_at set is caught at the block gate
    and returns IngressDecision.BLOCKED (never processed).
    """
    uow = UnitOfWork(session=db)
    phone = f"+9193{uuid4().int % 100000000:08d}"

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.blocked_at = datetime.now(timezone.utc)
        uow.candidates.add(cand)

        event = InboundEvent(
            phone_number=phone,
            message="Trying to spam again",
            channel_message_id=f"ext_blocked_{uuid4().hex}",
        )

        result = process_ingress(uow, event)
        assert result.decision == IngressDecision.BLOCKED
        assert result.candidate.id == cand.id


def test_postgres_rate_limiting(db):
    """
    Inbound messages exceeding threshold in the last 60 seconds are RATE_LIMITED.
    Counted directly in Postgres without Redis.
    """
    uow = UnitOfWork(session=db)
    phone = f"+9194{uuid4().int % 100000000:08d}"
    now = datetime.now(timezone.utc)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        conv = uow.conversations.create(cand.id)

        # Pre-seed 5 inbound messages within the 60-second window
        for i in range(5):
            uow.messages.create(
                conversation_id=conv.id,
                candidate_id=cand.id,
                direction=DirectionEnum.inbound,
                body=f"Spam message {i}",
                channel_message_id=f"spam_{i}_{uuid4().hex}",
            )

        # Rate limit threshold set to 5 messages per minute
        event = InboundEvent(
            phone_number=phone,
            message="6th message exceeds limit",
            channel_message_id=f"spam_6_{uuid4().hex}",
        )

        # Check with threshold = 5
        result = process_ingress(uow, event, rate_limit_per_minute=5, now=now)
        assert result.decision == IngressDecision.RATE_LIMITED

        # With a higher threshold of 10, it should PROCEED
        result_ok = process_ingress(uow, event, rate_limit_per_minute=10, now=now)
        assert result_ok.decision == IngressDecision.PROCEED
