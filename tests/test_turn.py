"""
tests/test_turn.py — single-turn orchestration engine tests (FLOW-021).

Tests:
1. Full simulated WhatsApp turn:
   - Inbound: "I am a frontend developer with 4 years experience"
   - Extractor -> Policy -> Replier pipeline executes.
   - Updates candidate_profiles projection in DB.
   - Saves both inbound and outbound messages in DB.
2. Consent gate integration:
   - Inbound with pending consent -> ask_consent reply, zero attributes in DB.
3. Safe error degradation:
   - Exception during pipeline execution degrades gracefully to SAFE_FALLBACK_REPLY.
   - Never raises an unhandled exception or drops the conversation.
"""

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from google.adk.agents import BaseAgent
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
import pytest

from app.agents.policy import PolicyAgent
from app.agents.root import create_flow_app
from app.agents.schemas import (
    ExtractedFact,
    ExtractionConfidenceEnum,
    IntentEnum,
    TurnExtraction,
)
from app.channel.inbound import InboundEvent
from app.db.uow import UnitOfWork
from app.models.enums import ChannelEnum, ConsentStatusEnum
from app.services.turn import SAFE_FALLBACK_REPLY, TurnService


from pydantic import Field


# ---------------------------------------------------------------------------
# Test Agent Mocks for Fast, Deterministic Unit Runs
# ---------------------------------------------------------------------------

class MockExtractorAgent(BaseAgent):
    """Mock extractor that emits a fixed TurnExtraction for testing."""

    extraction: Any = Field(default=None)

    def __init__(self, extraction: TurnExtraction, name: str = "extractor", **kwargs):
        super().__init__(name=name, extraction=extraction, **kwargs)

    async def _run_async_impl(self, ctx) -> AsyncGenerator[Event, None]:
        ctx.session.state["temp:extraction"] = self.extraction.model_dump()
        yield Event(
            author=self.name,
            actions=EventActions(state_delta={"temp:extraction": self.extraction.model_dump()}),
        )


class MockReplierAgent(BaseAgent):
    """Mock replier that emits a fixed text reply based on directive."""

    reply_text: str = Field(default="")

    def __init__(self, reply_text: str, name: str = "replier", **kwargs):
        super().__init__(name=name, reply_text=reply_text, **kwargs)

    async def _run_async_impl(self, ctx) -> AsyncGenerator[Event, None]:
        content = types.Content(
            role="model",
            parts=[types.Part.from_text(text=self.reply_text)],
        )
        yield Event(author=self.name, content=content)


class FailingAgent(BaseAgent):
    """Mock agent that raises an error to test degradation."""

    async def _run_async_impl(self, ctx) -> AsyncGenerator[Event, None]:
        raise RuntimeError("Simulated Gemini API 503 Service Unavailable")
        yield


# ---------------------------------------------------------------------------
# Acceptance Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_turn_intake_flow(db):
    """
    Acceptance test (§8, FLOW-021):
    Inbound: "I am a frontend developer with 4 years experience"
    - Runs SequentialAgent (extractor -> policy -> replier).
    - Updates candidate_profiles with experience_years=4.
    - Saves both inbound and outbound messages in DB.
    """
    phone = f"+9191{uuid4().int % 100000000:08d}"
    uow = UnitOfWork(session=db)
    t0 = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Pre-seed candidate with granted consent
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        cand.consent_at = t0
        uow.candidates.add(cand)

    # 2. Build test pipeline
    extraction = TurnExtraction(
        intent=IntentEnum.provide_info,
        facts=[
            ExtractedFact(
                key="desired_role",
                value="Frontend Developer",
                raw_text="frontend developer",
                confidence=ExtractionConfidenceEnum.confirmed,
            ),
            ExtractedFact(
                key="experience_years",
                value=4,
                raw_text="4 years experience",
                confidence=ExtractionConfidenceEnum.confirmed,
            ),
        ],
    )
    mock_extractor = MockExtractorAgent(extraction=extraction)
    mock_policy = PolicyAgent(session_factory=lambda: db)
    mock_replier = MockReplierAgent(
        reply_text="Great to connect! What is your expected CTC and preferred work location?"
    )

    app = create_flow_app(
        custom_extractor=mock_extractor,
        custom_policy=mock_policy,
        custom_replier=mock_replier,
    )
    session_service = InMemorySessionService()
    turn_service = TurnService(
        uow=uow,
        session_service=session_service,
        app=app,
    )

    inbound = InboundEvent(
        channel=ChannelEnum.simulator,
        phone_number=phone,
        channel_message_id=f"msg-{uuid4()}",
        message="I am a frontend developer with 4 years experience",
        timestamp=t0,
    )

    # 3. Execute turn
    result = await turn_service.run(inbound)

    assert result.reply_text == "Great to connect! What is your expected CTC and preferred work location?"
    assert result.directive == "ask_next"
    assert result.is_closed is False

    # 4. Assert DB state
    with uow:
        # Candidate profile updated
        profile = uow.profiles.get_by_candidate_id(result.candidate_id)
        assert profile is not None
        assert profile.experience_years == 4.0

        # Inbound and outbound messages persisted
        messages = uow.messages.get_recent_by_conversation(result.conversation_id)
        assert len(messages) == 2
        directions = {m.direction.value for m in messages}
        assert directions == {"inbound", "outbound"}

        # Conversation timestamps updated
        conv = uow.conversations.get_by_id(result.conversation_id)
        assert conv.last_inbound_at == t0
        assert conv.last_outbound_at is not None


@pytest.mark.asyncio
async def test_consent_gate_turn_handling(db):
    """
    Acceptance test (Q4, FLOW-045, FLOW-021):
    First message from pending candidate is handled by the consent gate.
    - Does NOT execute extractor.
    - Returns WhatsApp consent notice.
    - Zero candidate_attributes persisted in DB.
    """
    phone = f"+9191{uuid4().int % 100000000:08d}"
    uow = UnitOfWork(session=db)
    t0 = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)

    session_service = InMemorySessionService()
    turn_service = TurnService(
        uow=uow,
        session_service=session_service,
    )

    inbound = InboundEvent(
        channel=ChannelEnum.simulator,
        phone_number=phone,
        channel_message_id=f"msg-{uuid4()}",
        message="Hi, I am looking for a Python developer job in Berlin",
        timestamp=t0,
    )

    result = await turn_service.run(inbound)

    assert result.directive == "ask_consent"
    assert "consent" in result.reply_text.lower()

    # DB assertions
    with uow:
        # Zero attributes written (Q4 invariant)
        attrs = uow.attributes.get_all_for_candidate(result.candidate_id)
        assert len(attrs) == 0

        # Both messages persisted
        messages = uow.messages.get_recent_by_conversation(result.conversation_id)
        assert len(messages) == 2


@pytest.mark.asyncio
async def test_turn_error_degradation(db):
    """
    Acceptance test: If model/pipeline raises an exception, TurnService degrades
    to SAFE_FALLBACK_REPLY and persists outbound message without crashing.
    """
    phone = f"+9191{uuid4().int % 100000000:08d}"
    uow = UnitOfWork(session=db)
    t0 = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        cand.consent_at = t0
        uow.candidates.add(cand)

    app = create_flow_app(
        custom_extractor=FailingAgent(name="extractor"),
    )
    session_service = InMemorySessionService()
    turn_service = TurnService(
        uow=uow,
        session_service=session_service,
        app=app,
    )

    inbound = InboundEvent(
        channel=ChannelEnum.simulator,
        phone_number=phone,
        channel_message_id=f"msg-{uuid4()}",
        message="Hi there",
        timestamp=t0,
    )

    # Must NOT raise unhandled exception!
    result = await turn_service.run(inbound)

    assert result.reply_text == SAFE_FALLBACK_REPLY
    assert result.directive == "error_fallback"

    # Outbound message is saved in DB
    with uow:
        messages = uow.messages.get_recent_by_conversation(result.conversation_id)
        assert len(messages) == 2
        assert any(m.body == SAFE_FALLBACK_REPLY for m in messages)
