"""
tests/test_summaries.py — Conversation summarisation tests (FLOW-031).

Tests:
1. Deterministic conversation summary formatting.
2. Conversation close and summary persistence in database.
3. LLM client integration and fallback handling.
4. Summary captures deflections, abuse, mode, and final status.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4
import pytest

from app.agents.summariser import (
    build_deterministic_summary,
    close_and_summarise_conversation,
    format_transcript,
    summarise_conversation,
    summarise_messages,
)
from app.db.uow import UnitOfWork
from app.models.candidate import Conversation, Message
from app.models.enums import (
    ChannelEnum,
    ConsentStatusEnum,
    ConversationModeEnum,
    ConversationStatusEnum,
    DirectionEnum,
)


def test_transcript_formatting():
    """Format messages into candidate / Flow dialogue transcript."""
    cand_id = uuid4()
    conv_id = uuid4()
    messages = [
        Message(
            conversation_id=conv_id,
            candidate_id=cand_id,
            direction=DirectionEnum.inbound,
            body="Hello, I am looking for a Python developer job.",
        ),
        Message(
            conversation_id=conv_id,
            candidate_id=cand_id,
            direction=DirectionEnum.outbound,
            body="Welcome! How many years of experience do you have?",
        ),
        Message(
            conversation_id=conv_id,
            candidate_id=cand_id,
            direction=DirectionEnum.inbound,
            body="I have 5 years of backend experience.",
        ),
    ]

    transcript = format_transcript(messages)
    assert "Candidate: Hello, I am looking for a Python developer job." in transcript
    assert "Flow: Welcome! How many years of experience do you have?" in transcript
    assert "Candidate: I have 5 years of backend experience." in transcript


def test_deterministic_summary_captures_counters_and_status():
    """Deterministic summary captures deflections, abuse counts, and final status."""
    conv = Conversation(
        channel=ChannelEnum.simulator,
        mode=ConversationModeEnum.intake,
        status=ConversationStatusEnum.closed,
        started_at=datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc),
        deflection_count=2,
        abuse_count=1,
    )
    messages = [
        Message(
            direction=DirectionEnum.inbound,
            body="I prefer not to say my current salary.",
        ),
        Message(
            direction=DirectionEnum.outbound,
            body="No problem. Would you like to speak with a human recruiter?",
        ),
    ]

    summary = build_deterministic_summary(conv, messages)
    assert "mode 'intake'" in summary
    assert "Recorded 2 deflections/refusals." in summary
    assert "Recorded 1 moderation/abuse warnings." in summary
    assert "Final status: closed." in summary
    assert "Candidate inputs: I prefer not to say my current salary." in summary


def test_close_and_summarise_conversation(db):
    """Closing conversation persists summary and sets closed_at."""
    uow = UnitOfWork(session=db)
    phone = f"+91{uuid4().int % 10000000000:010d}"

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        conv = uow.conversations.create(
            candidate_id=cand.id,
            mode=ConversationModeEnum.intake,
        )

        uow.messages.create(
            conversation_id=conv.id,
            candidate_id=cand.id,
            direction=DirectionEnum.inbound,
            body="I want a Data Engineering role in Bengaluru.",
        )
        uow.messages.create(
            conversation_id=conv.id,
            candidate_id=cand.id,
            direction=DirectionEnum.outbound,
            body="What is your expected CTC?",
        )
        uow.messages.create(
            conversation_id=conv.id,
            candidate_id=cand.id,
            direction=DirectionEnum.inbound,
            body="25 LPA.",
        )
        uow.commit()

    with uow:
        conv = uow.conversations.get_by_id(conv.id)
        assert conv.status == ConversationStatusEnum.active
        assert conv.summary is None

        summary = close_and_summarise_conversation(uow, conv)
        uow.commit()

    with uow:
        refreshed = uow.conversations.get_by_id(conv.id)
        assert refreshed.status == ConversationStatusEnum.closed
        assert refreshed.closed_at is not None
        assert refreshed.summary is not None
        assert "Data Engineering" in refreshed.summary
        assert "25 LPA" in refreshed.summary


def test_summarise_messages_with_mock_llm():
    """Mock LLM response is returned when LLM client succeeds."""
    conv = Conversation(mode=ConversationModeEnum.intake, status=ConversationStatusEnum.active)
    messages = [
        Message(direction=DirectionEnum.inbound, body="I am a Senior DevOps Engineer with 7 years experience."),
    ]

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value.text = (
        "Candidate is a Senior DevOps Engineer with 7 years of experience seeking new opportunities."
    )

    summary = summarise_messages(messages, conv, client=mock_client)
    assert "Senior DevOps Engineer" in summary
    assert mock_client.models.generate_content.called


def test_summarise_messages_llm_failure_falls_back():
    """When LLM generation fails, cleanly falls back to deterministic summary."""
    conv = Conversation(
        mode=ConversationModeEnum.intake,
        status=ConversationStatusEnum.active,
        started_at=datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc),
    )
    messages = [
        Message(direction=DirectionEnum.inbound, body="I am looking for Java roles in Pune."),
    ]

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError("API quota exceeded")

    summary = summarise_messages(messages, conv, client=mock_client)
    assert "Conversation started" in summary
    assert "Java roles in Pune" in summary
