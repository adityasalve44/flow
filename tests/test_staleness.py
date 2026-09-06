"""
tests/test_staleness.py — Profile staleness and refresh mode conversation tests (FLOW-030).

Tests:
1. mark_candidate_profile_stale preserves history (status=stale, never deleted).
2. 400-day-old candidate opens in mode=refresh and is greeted as returning.
3. No old preference is asserted as current; acknowledge_profile_ready is suppressed.
4. Reconfirmation restores stale attribute to current rather than rewriting/duplicating.
5. Updating a stale fact with a new value maintains clean record continuity.
6. Stale facts remain fully queryable.
"""

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from google.adk.agents import BaseAgent
from google.adk.events import Event, EventActions
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field
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
from app.domain.staleness import (
    get_stale_attributes,
    is_candidate_profile_stale,
    mark_candidate_profile_stale,
    reconfirm_stale_attribute,
)
from app.models import CandidateAttribute, CandidateProfile
from app.models.enums import (
    AttributeStatusEnum,
    ChannelEnum,
    ConfidenceEnum,
    ConsentStatusEnum,
    ConversationModeEnum,
    DataClassEnum,
    LifecycleStatusEnum,
    SourceEnum,
)
from app.services.conversation import resolve_conversation
from app.services.turn import TurnService


class MockExtractorAgent(BaseAgent):
    """Mock extractor returning a predetermined extraction."""
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
    """Mock replier that records calls and formats text."""
    call_count: int = Field(default=0)

    async def _run_async_impl(self, ctx) -> AsyncGenerator[Event, None]:
        self.call_count += 1
        directive = ctx.session.state.get("temp:directive", {})
        dir_name = directive.get("name", "ask_next")
        text = f"Reply for {dir_name}"
        content = types.Content(role="model", parts=[types.Part.from_text(text=text)])
        yield Event(author=self.name, content=content)


def test_is_candidate_profile_stale():
    """Verify staleness detection against configured threshold."""
    now = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
    profile_fresh = CandidateProfile(
        candidate_id=uuid4(),
        last_refreshed_at=now - timedelta(days=30),
    )
    assert is_candidate_profile_stale(profile_fresh, now=now) is False

    profile_stale = CandidateProfile(
        candidate_id=uuid4(),
        last_refreshed_at=now - timedelta(days=400),
    )
    assert is_candidate_profile_stale(profile_stale, now=now) is True
    assert is_candidate_profile_stale(None, now=now) is False


def test_mark_candidate_profile_stale_never_deletes_anything(db):
    """
    Acceptance test (FLOW-030):
    Every current fact is marked stale — not deleted — and must be reconfirmed.
    """
    phone = f"+9198{uuid4().int % 100000000:08d}"
    uow = UnitOfWork(session=db)
    t0 = datetime.now(timezone.utc)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        cand.lifecycle_status = LifecycleStatusEnum.profile_ready

        # Add 3 current attributes
        attr1 = CandidateAttribute(
            candidate_id=cand.id,
            key="desired_role",
            value="Backend Engineer",
            status=AttributeStatusEnum.current,
            confidence=ConfidenceEnum.confirmed,
            source=SourceEnum.candidate_stated,
            data_class=DataClassEnum.operational,
        )
        attr2 = CandidateAttribute(
            candidate_id=cand.id,
            key="experience_years",
            value=4,
            status=AttributeStatusEnum.current,
            confidence=ConfidenceEnum.confirmed,
            source=SourceEnum.candidate_stated,
            data_class=DataClassEnum.operational,
        )
        uow.attributes.add(attr1)
        uow.attributes.add(attr2)
        uow.commit()

        # Mark profile stale
        stale_count = mark_candidate_profile_stale(uow, cand, now=t0)
        assert stale_count == 2

    with uow:
        # 1. Zero current attributes remain
        current_attrs = uow.attributes.get_current_by_candidate(cand.id)
        assert len(current_attrs) == 0

        # 2. Stale facts remain fully queryable (never deleted!)
        stale_attrs = get_stale_attributes(uow, cand.id)
        assert len(stale_attrs) == 2
        keys = {a.key for a in stale_attrs}
        assert keys == {"desired_role", "experience_years"}

        # 3. Lifecycle status demoted to intake
        refreshed_cand = uow.candidates.get_by_id(cand.id)
        assert refreshed_cand.lifecycle_status == LifecycleStatusEnum.intake


@pytest.mark.asyncio
async def test_400_day_old_candidate_returns_in_refresh_mode(db):
    """
    Acceptance test (FLOW-030):
    A 400-day-old candidate is greeted as returning, not re-onboarded from zero,
    and no old preference is asserted as current.
    """
    phone = f"+9195{uuid4().int % 100000000:08d}"
    uow = UnitOfWork(session=db)
    t_past = datetime(2025, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    t_now = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Pre-seed a candidate onboarded 400+ days ago
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        cand.lifecycle_status = LifecycleStatusEnum.profile_ready

        profile = CandidateProfile(
            candidate_id=cand.id,
            full_name="Priya Sharma",
            last_refreshed_at=t_past,
        )
        uow.profiles.add(profile)

        # Pre-seed old attributes
        attr = CandidateAttribute(
            candidate_id=cand.id,
            key="desired_role",
            value="Data Scientist",
            status=AttributeStatusEnum.current,
            confidence=ConfidenceEnum.confirmed,
            source=SourceEnum.candidate_stated,
            data_class=DataClassEnum.operational,
            created_at=t_past,
        )
        uow.attributes.add(attr)
        uow.commit()

    # 2. Candidate contacts Flow after 400 days
    turn_service = TurnService(
        uow=uow,
        session_service=InMemorySessionService(),
        app=create_flow_app(
            custom_extractor=MockExtractorAgent(extraction=TurnExtraction(intent=IntentEnum.greet)),
            custom_policy=PolicyAgent(session_factory=lambda: db),
            custom_replier=MockReplierAgent(name="replier"),
        ),
    )

    res = await turn_service.run(
        InboundEvent(
            channel=ChannelEnum.simulator,
            phone_number=phone,
            channel_message_id=f"msg-refresh-1-{uuid4()}",
            message="Hi Flow, I am looking for new opportunities!",
            timestamp=t_now,
        ),
        now=t_now,
    )

    # 3. Assertions:
    # Mode is refresh
    assert res.mode == "refresh"
    # Directive is greet_returning (not acknowledge_profile_ready or generic greeting)
    assert res.directive == "greet_returning"
    assert res.is_closed is False

    # Stale facts preserved in DB
    with uow:
        stale = get_stale_attributes(uow, cand.id)
        assert len(stale) == 1
        assert stale[0].value == "Data Scientist"
        # Current attributes is empty until reconfirmed
        current = uow.attributes.get_current_by_candidate(cand.id)
        assert len(current) == 0


@pytest.mark.asyncio
async def test_reconfirmation_restores_stale_fact_without_duplicate(db):
    """
    Acceptance test (FLOW-030):
    Stale facts remain fully queryable and are restored to current on reconfirmation
    rather than rewritten or duplicated.
    """
    phone = f"+9194{uuid4().int % 100000000:08d}"
    uow = UnitOfWork(session=db)
    t_past = datetime(2025, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    t_now = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)

    # Pre-seed candidate with 1 stale attribute
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted

        old_attr = CandidateAttribute(
            candidate_id=cand.id,
            key="desired_role",
            value="Python Developer",
            status=AttributeStatusEnum.stale,
            confidence=ConfidenceEnum.confirmed,
            source=SourceEnum.candidate_stated,
            data_class=DataClassEnum.operational,
            created_at=t_past,
        )
        uow.attributes.add(old_attr)
        uow.commit()
        original_attr_id = old_attr.id

    # Extraction providing the same desired_role
    extraction = TurnExtraction(
        intent=IntentEnum.provide_info,
        facts=[
            ExtractedFact(
                key="desired_role",
                value="Python Developer",
                raw_text="I am still a python developer",
                confidence=ExtractionConfidenceEnum.confirmed,
            ),
        ],
    )

    turn_service = TurnService(
        uow=uow,
        session_service=InMemorySessionService(),
        app=create_flow_app(
            custom_extractor=MockExtractorAgent(extraction=extraction),
            custom_policy=PolicyAgent(session_factory=lambda: db),
            custom_replier=MockReplierAgent(name="replier"),
        ),
    )

    res = await turn_service.run(
        InboundEvent(
            channel=ChannelEnum.simulator,
            phone_number=phone,
            channel_message_id=f"msg-reconfirm-{uuid4()}",
            message="I am still looking for Python Developer roles",
            timestamp=t_now,
        ),
        now=t_now,
    )

    # Verify: The SAME attribute row was restored to current, not duplicated!
    with uow:
        attrs = uow.attributes.get_current_by_candidate(cand.id)
        assert len(attrs) == 1
        restored = attrs[0]
        assert restored.id == original_attr_id, "Must restore existing row, not insert a duplicate"
        assert restored.status == AttributeStatusEnum.current
        assert restored.value == "Python Developer"
        assert restored.confirmed_at > t_past


def test_reconfirm_stale_attribute_direct(db):
    """Verify programmatic reconfirmation helper restores stale attribute."""
    phone = f"+9193{uuid4().int % 100000000:08d}"
    uow = UnitOfWork(session=db)
    t0 = datetime.now(timezone.utc)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        attr = CandidateAttribute(
            candidate_id=cand.id,
            key="location_preference",
            value="Bengaluru",
            status=AttributeStatusEnum.stale,
            confidence=ConfidenceEnum.confirmed,
            source=SourceEnum.candidate_stated,
            data_class=DataClassEnum.operational,
        )
        uow.attributes.add(attr)
        uow.commit()

        # Reconfirm with updated value
        restored = reconfirm_stale_attribute(
            uow=uow,
            candidate_id=cand.id,
            key="location_preference",
            confirmed_value="Pune",
            now=t0,
        )
        assert restored is not None
        assert restored.status == AttributeStatusEnum.current
        assert restored.value == "Pune"
        assert restored.confirmed_at == t0
