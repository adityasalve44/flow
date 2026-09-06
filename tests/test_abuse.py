"""
tests/test_abuse.py — Abuse handling, escalation, and admin blocking tests (FLOW-029).

Tests:
1. Deterministic lexicon check catches abusive terms before model invocation.
2. First abuse gets one calm warning; reply never mentions moderation or internal rules.
3. Second abuse stops conversation, flags conversation as escalated, and records ModerationEvent.
4. Extractor abuse_signal independently triggers the two-strike moderation policy.
5. Blocked candidate (candidates.blocked_at) short-circuits at the very first gate.
6. Safe moderation replies never leak moderation jargon (strike, blacklist, banned, etc.).
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from google.adk.agents import BaseAgent
from google.adk.events import Event, EventActions
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field

from app.agents.policy import PolicyAgent
from app.agents.root import create_flow_app
from app.agents.schemas import IntentEnum, TurnExtraction
from app.channel.inbound import InboundEvent
from app.db.uow import UnitOfWork
from app.domain.moderation import (
    block_candidate,
    check_abuse_lexicon,
    is_safe_moderation_reply,
)
from app.models.enums import (
    ChannelEnum,
    ConsentStatusEnum,
    ConversationStatusEnum,
)
from app.services.turn import TurnService


class MockExtractorAgent(BaseAgent):
    """Mock extractor for testing abuse_signal flag from model."""
    extraction: Any = Field(default=None)

    def __init__(self, extraction: TurnExtraction, name: str = "extractor", **kwargs):
        super().__init__(name=name, extraction=extraction, **kwargs)

    async def _run_async_impl(self, ctx) -> AsyncGenerator[Event]:
        ctx.session.state["temp:extraction"] = self.extraction.model_dump()
        yield Event(
            author=self.name,
            actions=EventActions(state_delta={"temp:extraction": self.extraction.model_dump()}),
        )


class MockReplierAgent(BaseAgent):
    """Mock replier that records calls."""
    call_count: int = Field(default=0)

    async def _run_async_impl(self, ctx) -> AsyncGenerator[Event]:
        directive = ctx.session.state.get("temp:directive", {})
        dir_name = directive.get("name", "ask_next")
        if dir_name == "disengage_silent":
            return
        self.call_count += 1
        text = f"Reply for {dir_name}"
        content = types.Content(role="model", parts=[types.Part.from_text(text=text)])
        yield Event(author=self.name, content=content)


def test_abuse_lexicon_deterministic():
    """Verify deterministic boundary check at ingress."""
    assert check_abuse_lexicon("fuck off") is True
    assert check_abuse_lexicon("You idiot asshole") is True
    assert check_abuse_lexicon("shut up bastard") is True
    assert check_abuse_lexicon("Go to hell and die") is True
    assert check_abuse_lexicon("f*ck you") is True

    # Safe professional messages must not trigger false positives
    assert check_abuse_lexicon("I have 4 years experience") is False
    assert check_abuse_lexicon("My desired role is software engineer") is False
    assert check_abuse_lexicon("No, I would rather not share that") is False
    assert check_abuse_lexicon("Is this position remote or on-site?") is False
    assert check_abuse_lexicon("") is False
    assert check_abuse_lexicon(None) is False


def test_safe_moderation_reply_invariant():
    """Ensure candidate messages never leak moderation, policy, or penalty jargon."""
    assert is_safe_moderation_reply("Please keep our conversation respectful. I am here to help.") is True
    assert is_safe_moderation_reply("How would you like to proceed with your job search?") is True

    # Forbidden leak phrases
    assert is_safe_moderation_reply("You received a moderation strike.") is False
    assert is_safe_moderation_reply("Your account was flagged for policy violation.") is False
    assert is_safe_moderation_reply("Our rules state you will be banned.") is False
    assert is_safe_moderation_reply("You are blacklisted by the system.") is False


@pytest.mark.asyncio
async def test_first_abuse_gets_calm_warning_and_records_event(db):
    """
    FLOW-029 Acceptance test:
    First abuse gets one calm warning; second stops the conversation and records an event;
    reply never mentions moderation.
    """
    phone = f"+9198{uuid4().int % 100000000:08d}"
    uow = UnitOfWork(session=db)
    t0 = datetime.now(UTC)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        uow.candidates.add(cand)

    replier_mock = MockReplierAgent(name="replier")
    turn_service = TurnService(
        uow=uow,
        session_service=InMemorySessionService(),
        app=create_flow_app(
            custom_policy=PolicyAgent(session_factory=lambda: db),
            custom_replier=replier_mock,
        ),
    )

    # Turn 1: Candidate sends first abusive message
    res1 = await turn_service.run(
        InboundEvent(
            channel=ChannelEnum.simulator,
            phone_number=phone,
            channel_message_id=f"msg-abuse-1-{uuid4()}",
            message="Fuck you idiot",
            timestamp=t0,
        )
    )

    # 1. Calm warning reply emitted
    assert res1.directive == "warn_abuse"
    assert len(res1.reply_text) > 0
    assert is_safe_moderation_reply(res1.reply_text), "Reply must never mention moderation or rules"
    assert "moderation" not in res1.reply_text.lower()
    assert "strike" not in res1.reply_text.lower()
    assert "banned" not in res1.reply_text.lower()

    # 2. Ingress lexicon resolved abuse before calling replier LLM
    assert replier_mock.call_count == 0

    # 3. Moderation event recorded
    with uow:
        events = uow.moderation_events.get_by_candidate(cand.id)
        assert len(events) == 1
        assert events[0].kind == "warn_abuse"
        conv = uow.conversations.get_by_id(res1.conversation_id)
        assert conv.abuse_count == 1
        assert conv.status == ConversationStatusEnum.active

    # Turn 2: Second abuse stops conversation, flags escalated, replies stop
    res2 = await turn_service.run(
        InboundEvent(
            channel=ChannelEnum.simulator,
            phone_number=phone,
            channel_message_id=f"msg-abuse-2-{uuid4()}",
            message="Shut up you bastard",
            timestamp=t0,
        )
    )

    assert res2.directive == "disengage_silent"
    assert res2.reply_text == "", "Second abuse must produce silence"
    assert res2.is_closed is True
    assert res2.outbound_message_id is None

    # Verify conversation status and moderation events in DB
    with uow:
        conv2 = uow.conversations.get_by_id(res1.conversation_id)
        assert conv2.status == ConversationStatusEnum.escalated
        assert conv2.abuse_count == 2
        events = uow.moderation_events.get_by_candidate(cand.id)
        assert len(events) == 2
        kinds = [e.kind for e in events]
        assert "escalated" in kinds

    # Turn 3: Follow-up message while escalated remains silent
    res3 = await turn_service.run(
        InboundEvent(
            channel=ChannelEnum.simulator,
            phone_number=phone,
            channel_message_id=f"msg-abuse-3-{uuid4()}",
            message="Are you still there?",
            timestamp=t0,
        )
    )
    assert res3.reply_text == ""
    assert res3.is_closed is True


@pytest.mark.asyncio
async def test_extractor_abuse_signal_escalates(db):
    """
    Verify that when extractor independently flags abuse_signal (sub-lexical abuse),
    the policy ladder issues warn_abuse on strike 1 and escalates on strike 2.
    """
    phone = f"+9197{uuid4().int % 100000000:08d}"
    uow = UnitOfWork(session=db)
    t0 = datetime.now(UTC)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        uow.candidates.add(cand)

    abuse_extraction = TurnExtraction(
        intent=IntentEnum.abuse,
        facts=[],
        questions=[],
        abuse_signal=True,
    )

    mock_replier = MockReplierAgent(name="replier")
    turn_service = TurnService(
        uow=uow,
        session_service=InMemorySessionService(),
        app=create_flow_app(
            custom_extractor=MockExtractorAgent(extraction=abuse_extraction, name="extractor"),
            custom_policy=PolicyAgent(session_factory=lambda: db, name="policy"),
            custom_replier=mock_replier,
        ),
    )

    # Turn 1: First abuse signal -> warn_abuse
    res1 = await turn_service.run(
        InboundEvent(
            channel=ChannelEnum.simulator,
            phone_number=phone,
            channel_message_id=f"msg-abuse-signal-1-{uuid4()}",
            message="I refuse to speak to a bot who thinks they know anything",
            timestamp=t0,
        )
    )
    assert res1.directive == "warn_abuse"
    assert mock_replier.call_count == 1

    # Turn 2: Second abuse signal -> escalate and silence
    res2 = await turn_service.run(
        InboundEvent(
            channel=ChannelEnum.simulator,
            phone_number=phone,
            channel_message_id=f"msg-abuse-signal-2-{uuid4()}",
            message="You are completely useless and pathetic",
            timestamp=t0,
        )
    )
    assert res2.directive == "disengage_silent"
    assert res2.reply_text == ""
    assert res2.is_closed is True
    # Replier must NOT be called on strike 2
    assert mock_replier.call_count == 1


@pytest.mark.asyncio
async def test_admin_block_shortcircuits_at_first_gate(db):
    """
    FLOW-029 Acceptance test:
    An administrator can set candidates.blocked_at, after which ingress
    short-circuits at the very first check and no message is ever processed again.
    """
    phone = f"+9196{uuid4().int % 100000000:08d}"
    uow = UnitOfWork(session=db)
    t0 = datetime.now(UTC)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        # Admin blocks the candidate
        block_candidate(uow, cand, reason="Repeated abusive harassment across channels")
        uow.commit()

    turn_service = TurnService(
        uow=uow,
        session_service=InMemorySessionService(),
        app=create_flow_app(),
    )

    # Blocked candidate sends message
    res = await turn_service.run(
        InboundEvent(
            channel=ChannelEnum.simulator,
            phone_number=phone,
            channel_message_id=f"msg-blocked-{uuid4()}",
            message="Hello, I want to apply for jobs now",
            timestamp=t0,
        )
    )

    # Rejected at first gate
    assert res.directive == "blocked"
    assert res.reply_text == ""
    assert res.is_closed is True
    assert res.inbound_message_id is None
    assert res.outbound_message_id is None

    # Verify no messages were persisted
    with uow:
        conv = uow.conversations.get_active(cand.id)
        assert conv is None
        events = uow.moderation_events.get_by_candidate(cand.id)
        assert len(events) == 1
        assert events[0].kind == "block"
        assert "harassment" in (events[0].detail or "")
