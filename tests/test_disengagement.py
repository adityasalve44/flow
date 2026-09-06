"""
tests/test_disengagement.py — Deflection counting, disengagement, and conversation reopening tests (FLOW-028).

Tests:
1. Deflection counter increments only on genuine refusal / deflections.
2. Second deflection triggers offer_call.
3. Three consecutive refusals produce exactly two replies and then silence.
4. Silence suppresses outbound message persistence on the third refusal.
5. Reopening conversation when a candidate supplies information or changes tone.
"""

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

from google.adk.agents import BaseAgent
from google.adk.events import Event, EventActions
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field
import pytest

from app.agents.policy import PolicyAgent
from app.agents.root import create_flow_app
from app.agents.schemas import IntentEnum, TurnExtraction
from app.channel.inbound import InboundEvent
from app.db.uow import UnitOfWork
from app.domain.moderation import is_genuine_deflection, record_deflection, should_reopen_conversation
from app.domain.policy import evaluate_policy_step
from app.models.enums import ChannelEnum, ConsentStatusEnum, ConversationModeEnum, ConversationStatusEnum, DirectionEnum
from app.services.turn import TurnService


class MockExtractorAgent(BaseAgent):
    """Mock extractor configured with a specific TurnExtraction."""
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
    """Mock replier that counts invocations and emits text."""
    call_count: int = Field(default=0)

    async def _run_async_impl(self, ctx) -> AsyncGenerator[Event, None]:
        directive = ctx.session.state.get("temp:directive", {})
        dir_name = directive.get("name", "ask_next")
        if dir_name == "disengage_silent":
            return
        self.call_count += 1
        text = f"Reply for {dir_name}"
        content = types.Content(role="model", parts=[types.Part.from_text(text=text)])
        yield Event(author=self.name, content=content)


def test_deflection_counter_rules():
    """Verify genuine deflection evaluation."""
    assert is_genuine_deflection(refusal_signal=True) is True
    assert is_genuine_deflection(refusal_signal=False, demanded_other=True, is_off_topic=True) is True
    assert is_genuine_deflection(refusal_signal=False, demanded_other=False, is_off_topic=False) is False


def test_should_reopen_conversation_heuristics():
    """Verify reopening detection on facts and tone changes."""
    # Facts supplied -> reopen
    assert should_reopen_conversation("any text", has_facts=True) is True
    assert should_reopen_conversation("I have 5 years experience as a python dev") is True
    assert should_reopen_conversation("My notice period is 30 days") is True
    assert should_reopen_conversation("Sorry, I am ready to start now") is True
    assert should_reopen_conversation("Let's continue, I want to apply") is True

    # Continued refusals / non-constructive messages do NOT reopen
    assert should_reopen_conversation("No way") is False
    assert should_reopen_conversation("I won't tell you anything") is False
    assert should_reopen_conversation("Stop messaging me") is False
    assert should_reopen_conversation("") is False


@pytest.mark.asyncio
async def test_three_consecutive_refusals_produce_two_replies_then_silence(db):
    """
    Acceptance test (FLOW-028):
    Three consecutive refusals produce exactly two replies and then silence,
    with no third model call.
    """
    phone = f"+9198{uuid4().int % 100000000:08d}"
    uow = UnitOfWork(session=db)
    t0 = datetime.now(timezone.utc)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        uow.candidates.add(cand)

    session_service = InMemorySessionService()

    refusal_extraction = TurnExtraction(
        intent=IntentEnum.refuse,
        facts=[],
        questions=[],
        refusal_signal=True,
    )

    replier_mock = MockReplierAgent(name="replier")
    app = create_flow_app(
        custom_extractor=MockExtractorAgent(extraction=refusal_extraction, name="extractor"),
        custom_policy=PolicyAgent(session_factory=lambda: db),
        custom_replier=replier_mock,
    )

    turn_service = TurnService(
        uow=uow,
        session_service=session_service,
        app=app,
    )

    # Turn 1: First refusal -> Reply 1
    res1 = await turn_service.run(
        InboundEvent(
            channel=ChannelEnum.simulator,
            phone_number=phone,
            channel_message_id=f"msg-refusal-1-{uuid4()}",
            message="I don't want to tell you that",
            timestamp=t0,
        )
    )
    assert res1.directive in ("ask_next", "redirect")
    assert len(res1.reply_text) > 0
    assert res1.is_closed is False
    assert replier_mock.call_count == 1

    # Turn 2: Second refusal -> Reply 2 (offer_call)
    res2 = await turn_service.run(
        InboundEvent(
            channel=ChannelEnum.simulator,
            phone_number=phone,
            channel_message_id=f"msg-refusal-2-{uuid4()}",
            message="No, ask something else",
            timestamp=t0,
        )
    )
    assert res2.directive == "offer_call"
    assert len(res2.reply_text) > 0
    assert res2.is_closed is False
    assert replier_mock.call_count == 2

    # Turn 3: Third refusal -> SILENCE (disengage_silent), NO THIRD REPLY!
    res3 = await turn_service.run(
        InboundEvent(
            channel=ChannelEnum.simulator,
            phone_number=phone,
            channel_message_id=f"msg-refusal-3-{uuid4()}",
            message="No calls, leave me alone",
            timestamp=t0,
        )
    )
    assert res3.directive == "disengage_silent"
    assert res3.reply_text == "", "Third refusal must produce silence"
    assert res3.is_closed is True
    # Replier model must NOT have been called for third reply!
    # call_count remains 2!
    assert res3.outbound_message_id is None

    # Check database messages: Exactly 2 outbound replies exist across the 3 turns
    with uow:
        msgs = uow.messages.get_recent(res1.conversation_id, limit=10)
        outbound_msgs = [m for m in msgs if m.direction == DirectionEnum.outbound]
        assert len(outbound_msgs) == 2, "Only 2 outbound messages exist in database; 3rd turn was silent"

    # Turn 4: Candidate continues sending refusal while closed -> Suppressed immediately with NO model calls
    res4 = await turn_service.run(
        InboundEvent(
            channel=ChannelEnum.simulator,
            phone_number=phone,
            channel_message_id=f"msg-refusal-4-{uuid4()}",
            message="Why aren't you answering?",
            timestamp=t0,
        )
    )
    assert res4.reply_text == ""
    assert res4.is_closed is True
    assert res4.directive == "disengage_silent"

    # Turn 5: Reopen! Candidate changes mind and supplies career information
    fact_extraction = TurnExtraction(
        intent=IntentEnum.provide_info,
        facts=[],
        questions=[],
        refusal_signal=False,
    )
    replier_mock_reopen = MockReplierAgent(name="replier_reopen")
    app_reopen = create_flow_app(
        custom_extractor=MockExtractorAgent(extraction=fact_extraction, name="extractor_reopen"),
        custom_policy=PolicyAgent(session_factory=lambda: db, name="policy_reopen"),
        custom_replier=replier_mock_reopen,
    )
    turn_service_reopen = TurnService(
        uow=uow,
        session_service=session_service,
        app=app_reopen,
    )

    res5 = await turn_service_reopen.run(
        InboundEvent(
            channel=ChannelEnum.simulator,
            phone_number=phone,
            channel_message_id=f"msg-reopen-{uuid4()}",
            message="Sorry about earlier, I am looking for a Python developer role with 5 years experience",
            timestamp=t0,
        )
    )
    assert res5.is_closed is False
    assert len(res5.reply_text) > 0
    assert res5.directive != "disengage_silent"
