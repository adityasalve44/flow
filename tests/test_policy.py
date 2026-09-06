"""
tests/test_policy.py — comprehensive tests for deterministic policy agent and ladder (FLOW-019).

Tests:
1. Completeness & readiness:
   - Profile-ready when all 6 baseline blocking fields are confirmed.
   - NOT ready if any single blocking field is absent.
   - Ready even if non-blocking fields (education, current_ctc, name, company) are absent.
2. Next-question scoring:
   - Never re-asks a just-answered field.
   - Applies recency penalty to recently asked fields.
   - Applies refusal penalty to declined fields.
   - Two fields chosen only when adjacent in ADJACENCY_MAP.
3. 13-Rung Directive Ladder (acceptance cases):
   - Rung 1: disengage_silent (deflection_count >= 3)
   - Rung 2: warn_abuse (abuse_signal = True)
   - Rung 3: close_consent_declined (declined/withdrawn)
   - Rung 4: ask_consent (pending consent)
   - Rung 5: offer_call (deflection_count == 2)
   - Rung 6: answer_and_continue (is_related_to_pending = True)
   - Rung 7: confirm_ambiguity (fact lands ambiguous)
   - Rung 8: resolve_conflict (fact contradicts existing)
   - Rung 9: redirect (question asked with no facts supplied)
   - Rung 10: clarify_name (display_name != profile.full_name)
   - Rung 11: ask_resume (profile ready, no resume)
   - Rung 12: ask_next (default top-scored missing fields)
   - Rung 13: acknowledge_profile_ready (all baseline fields confirmed)
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.agents.schemas import (
    ExtractedFact,
    ExtractedQuestion,
    ExtractionConfidenceEnum,
    IntentEnum,
    TurnExtraction,
)
from app.db.uow import UnitOfWork
from app.domain.completeness import (
    calculate_completeness,
    get_missing_blocking_fields,
    is_profile_ready,
)
from app.domain.merge import Fact
from app.domain.policy import (
    ADJACENCY_MAP,
    evaluate_policy_step,
    score_missing_fields,
)
from app.domain.registry import BLOCKING_KEYS
from app.models.enums import (
    AttributeStatusEnum,
    ConfidenceEnum,
    ConsentStatusEnum,
    ConversationModeEnum,
    DataClassEnum,
    LifecycleStatusEnum,
)
from app.services.conversation import resolve_conversation


# ---------------------------------------------------------------------------
# 1. Completeness and Readiness Tests
# ---------------------------------------------------------------------------

def test_readiness_all_blocking_fields_present():
    """All 6 blocking fields present and confirmed -> profile ready."""
    facts = [
        Fact(
            id=uuid4(),
            candidate_id=uuid4(),
            key=key,
            value="sample",
            raw_text="sample",
            source="candidate_stated",
            confidence=ConfidenceEnum.confirmed.value,
            status=AttributeStatusEnum.current.value,
            data_class=DataClassEnum.operational.value,
            created_at=datetime.now(timezone.utc),
        )
        for key in BLOCKING_KEYS
    ]
    assert is_profile_ready(facts) is True
    assert get_missing_blocking_fields(facts) == []


def test_readiness_individually_missing_blocking_field():
    """Missing any single blocking field means NOT profile ready."""
    for missing_key in BLOCKING_KEYS:
        remaining_keys = BLOCKING_KEYS - {missing_key}
        facts = [
            Fact(
                id=uuid4(),
                candidate_id=uuid4(),
                key=key,
                value="sample",
                raw_text="sample",
                source="candidate_stated",
                confidence=ConfidenceEnum.confirmed.value,
                status=AttributeStatusEnum.current.value,
                data_class=DataClassEnum.operational.value,
                created_at=datetime.now(timezone.utc),
            )
            for key in remaining_keys
        ]
        assert is_profile_ready(facts) is False, f"Should not be ready when missing {missing_key}"
        assert missing_key in get_missing_blocking_fields(facts)


def test_readiness_ready_without_non_blocking_fields():
    """Profile is ready even if name, current_ctc, education, company are completely absent."""
    facts = [
        Fact(
            id=uuid4(),
            candidate_id=uuid4(),
            key=key,
            value="sample",
            raw_text="sample",
            source="candidate_stated",
            confidence=ConfidenceEnum.confirmed.value,
            status=AttributeStatusEnum.current.value,
            data_class=DataClassEnum.operational.value,
            created_at=datetime.now(timezone.utc),
        )
        for key in BLOCKING_KEYS
    ]
    # No non-blocking fields added
    assert is_profile_ready(facts) is True
    comp = calculate_completeness(facts)
    assert 0.6 < comp < 1.0  # Earned ~5.8 out of ~8.0 total weight


# ---------------------------------------------------------------------------
# 2. Next-Question Scoring Tests
# ---------------------------------------------------------------------------

def test_scoring_never_reasks_just_answered_field():
    """A field present in current_facts with confirmed status has missingness=0 and is never asked."""
    facts = [
        Fact(
            id=uuid4(),
            candidate_id=uuid4(),
            key="desired_role",
            value="Backend Engineer",
            raw_text="Backend Engineer",
            source="candidate_stated",
            confidence=ConfidenceEnum.confirmed.value,
            status=AttributeStatusEnum.current.value,
            data_class=DataClassEnum.operational.value,
            created_at=datetime.now(timezone.utc),
        )
    ]
    fields_to_ask = score_missing_fields(facts)
    assert "desired_role" not in fields_to_ask


def test_scoring_suppresses_recently_asked_and_declined():
    """Recency penalty and refusal penalty suppress fields."""
    empty_facts: list[Fact] = []

    # Without penalties, desired_role is top
    top_default = score_missing_fields(empty_facts)[0]

    # If desired_role was just asked, next top field is chosen
    top_with_recency = score_missing_fields(empty_facts, recently_asked=[top_default])[0]
    assert top_with_recency != top_default

    # If candidate declined top_default, it is heavily suppressed
    top_with_refusal = score_missing_fields(empty_facts, declined_keys={top_default})[0]
    assert top_with_refusal != top_default


def test_scoring_two_fields_chosen_only_when_adjacent():
    """A second field is included ONLY if it is in ADJACENCY_MAP for the top field."""
    empty_facts: list[Fact] = []
    fields = score_missing_fields(empty_facts)
    if len(fields) == 2:
        top, second = fields
        assert second in ADJACENCY_MAP.get(top, set()), f"{second} not adjacent to {top}"


# ---------------------------------------------------------------------------
# 3. 13-Rung Directive Ladder Acceptance Tests
# ---------------------------------------------------------------------------

def test_rung1_disengage_silent(db):
    """Rung 1: deflection_count >= 3 -> disengage_silent, conversation closed."""
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        conv = resolve_conversation(uow, cand)
        conv.deflection_count = 3

        extraction = TurnExtraction.empty()
        directive, _ = evaluate_policy_step(uow, cand, conv, extraction)

        assert directive.name == "disengage_silent"
        assert conv.status.value == "closed"


def test_rung2_warn_abuse(db):
    """Rung 2: abuse signal -> warn_abuse."""
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        conv = resolve_conversation(uow, cand)

        extraction = TurnExtraction(intent=IntentEnum.abuse, abuse_signal=True)
        directive, _ = evaluate_policy_step(uow, cand, conv, extraction)

        assert directive.name == "warn_abuse"
        assert conv.abuse_count == 1


def test_rung3_close_consent_declined(db):
    """Rung 3: consent declined -> close_consent_declined."""
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.declined
        conv = resolve_conversation(uow, cand)

        extraction = TurnExtraction.empty()
        directive, _ = evaluate_policy_step(uow, cand, conv, extraction)

        assert directive.name == "close_consent_declined"


def test_rung4_ask_consent(db):
    """Rung 4: consent pending -> ask_consent."""
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        assert cand.consent_status == ConsentStatusEnum.pending
        conv = resolve_conversation(uow, cand)

        extraction = TurnExtraction.empty()
        directive, _ = evaluate_policy_step(uow, cand, conv, extraction)

        assert directive.name == "ask_consent"


def test_rung5_offer_call(db):
    """Rung 5: deflection_count == 2 -> offer_call."""
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        conv = resolve_conversation(uow, cand)
        conv.deflection_count = 2

        extraction = TurnExtraction.empty()
        directive, _ = evaluate_policy_step(uow, cand, conv, extraction)

        assert directive.name == "offer_call"


def test_rung6_answer_and_continue(db):
    """Rung 6: Question relates to pending ask -> answer_and_continue."""
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        conv = resolve_conversation(uow, cand)

        extraction = TurnExtraction(
            intent=IntentEnum.ask_question,
            questions=[ExtractedQuestion(topic="CTC meaning", is_related_to_pending=True)],
        )
        directive, _ = evaluate_policy_step(uow, cand, conv, extraction)

        assert directive.name == "answer_and_continue"
        assert directive.question_topic == "CTC meaning"


def test_rung7_confirm_ambiguity(db):
    """Rung 7: Fact landed ambiguous -> confirm_ambiguity."""
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        conv = resolve_conversation(uow, cand)

        extraction = TurnExtraction(
            intent=IntentEnum.provide_info,
            facts=[
                ExtractedFact(
                    key="expected_ctc",
                    value="around 15 lakhs",
                    raw_text="around 15 lakhs",
                    confidence=ExtractionConfidenceEnum.ambiguous,
                    ambiguity_reason="hedged amount",
                )
            ],
        )
        directive, _ = evaluate_policy_step(uow, cand, conv, extraction)

        assert directive.name == "confirm_ambiguity"
        assert directive.ambiguous_fact is not None
        assert directive.ambiguous_fact["key"] == "expected_ctc"


def test_rung8_resolve_conflict(db):
    """Rung 8: Fact contradicts an existing authoritative fact -> resolve_conflict."""
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        conv = resolve_conversation(uow, cand)

        # Pre-seed candidate with authoritative 10 years experience
        t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
        initial_fact = TurnExtraction(
            intent=IntentEnum.provide_info,
            facts=[
                ExtractedFact(
                    key="experience_years",
                    value=10,
                    raw_text="10 years",
                    confidence=ExtractionConfidenceEnum.confirmed,
                )
            ],
        )
        evaluate_policy_step(uow, cand, conv, initial_fact, now=t0)

        # Inbound says 2 years with low confidence / inferred
        t1 = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
        conflicting_fact = TurnExtraction(
            intent=IntentEnum.provide_info,
            facts=[
                ExtractedFact(
                    key="experience_years",
                    value=2,
                    raw_text="2 years",
                    confidence=ExtractionConfidenceEnum.inferred,
                )
            ],
        )
        directive, _ = evaluate_policy_step(uow, cand, conv, conflicting_fact, now=t1)

        assert directive.name == "resolve_conflict"
        assert directive.conflicted_fact is not None
        assert directive.conflicted_fact["key"] == "experience_years"


def test_rung9_redirect(db):
    """Rung 9: Off-topic question, no facts supplied -> redirect."""
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        conv = resolve_conversation(uow, cand)

        extraction = TurnExtraction(
            intent=IntentEnum.ask_question,
            questions=[ExtractedQuestion(topic="Do you have jobs at Google?", is_related_to_pending=False)],
            facts=[],
        )
        directive, _ = evaluate_policy_step(uow, cand, conv, extraction)

        assert directive.name == "redirect"
        assert directive.question_topic == "Do you have jobs at Google?"


def test_rung10_clarify_name(db):
    """Rung 10: WhatsApp contact name conflicts with profile.full_name -> clarify_name."""
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        cand.display_name = "Rahul Sharma"
        conv = resolve_conversation(uow, cand)

        # Profile already has full_name = Rohit Verma
        profile = uow.profiles.get_by_candidate_id(cand.id)
        if not profile:
            from app.models import CandidateProfile
            profile = CandidateProfile(candidate_id=cand.id, full_name="Rohit Verma")
            uow.profiles.add(profile)
        else:
            profile.full_name = "Rohit Verma"
        uow.commit()

        extraction = TurnExtraction.empty()
        directive, _ = evaluate_policy_step(uow, cand, conv, extraction)

        assert directive.name == "clarify_name"


def test_rung11_ask_resume(db):
    """Rung 11: Profile ready, no resume -> ask_resume."""
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        conv = resolve_conversation(uow, cand)

        # Supply all 6 blocking fields
        extraction = TurnExtraction(
            intent=IntentEnum.provide_info,
            facts=[
                ExtractedFact(key="desired_role", value="SRE", raw_text="SRE"),
                ExtractedFact(key="experience_years", value=6, raw_text="6 years"),
                ExtractedFact(key="skills", value=["Linux", "K8s"], raw_text="Linux, K8s"),
                ExtractedFact(key="location_preference", value=["Bengaluru"], raw_text="Bengaluru"),
                ExtractedFact(key="expected_ctc", value="25 LPA", raw_text="25 LPA"),
                ExtractedFact(key="notice_period", value="15 days", raw_text="15 days"),
            ],
        )
        directive, snapshot = evaluate_policy_step(uow, cand, conv, extraction)

        # Profile is ready; next logical step is ask_resume
        assert directive.name == "ask_resume"
        assert cand.lifecycle_status == LifecycleStatusEnum.profile_ready


def test_rung12_ask_next_default(db):
    """Rung 12: Default ladder rung — asks top 1-2 missing fields."""
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        conv = resolve_conversation(uow, cand)

        extraction = TurnExtraction.empty()
        directive, _ = evaluate_policy_step(uow, cand, conv, extraction)

        assert directive.name == "ask_next"
        assert len(directive.fields_to_ask) in (1, 2)
        assert directive.fields_to_ask[0] in BLOCKING_KEYS


def test_rung13_acknowledge_profile_ready(db):
    """Rung 13: All six baseline fields confirmed and resume already known -> acknowledge_profile_ready."""
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        conv = resolve_conversation(uow, cand)

        extraction = TurnExtraction(
            intent=IntentEnum.provide_info,
            facts=[
                ExtractedFact(key="desired_role", value="Backend Dev", raw_text="Backend Dev"),
                ExtractedFact(key="experience_years", value=5, raw_text="5 years"),
                ExtractedFact(key="skills", value=["Python"], raw_text="Python"),
                ExtractedFact(key="location_preference", value=["Pune"], raw_text="Pune"),
                ExtractedFact(key="expected_ctc", value="15 LPA", raw_text="15 LPA"),
                ExtractedFact(key="notice_period", value="0 days", raw_text="immediate"),
                ExtractedFact(key="resume", value="confirmed", raw_text="resume on file"),
            ],
        )
        directive, snapshot = evaluate_policy_step(uow, cand, conv, extraction)

        assert directive.name == "acknowledge_profile_ready"
        assert cand.lifecycle_status == LifecycleStatusEnum.profile_ready


@pytest.mark.asyncio
async def test_policy_agent_async_invocation(db):
    """Test PolicyAgent BaseAgent execution via _run_async_impl."""
    from unittest.mock import MagicMock
    from app.agents.policy import PolicyAgent

    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        conv = resolve_conversation(uow, cand)

    agent = PolicyAgent(session_factory=lambda: db)

    # Mock InvocationContext
    ctx = MagicMock()
    ctx.session.state = {
        "candidate_id": str(cand.id),
        "conversation_id": str(conv.id),
        "temp:extraction": TurnExtraction.empty().model_dump(),
        "recently_asked": [],
        "declined_keys": [],
    }

    events = []
    async for event in agent._run_async_impl(ctx):
        events.append(event)

    assert len(events) == 1
    ev = events[0]
    state_delta = ev.actions.state_delta
    assert "temp:directive" in state_delta
    assert state_delta["temp:directive"]["name"] == "ask_next"
    assert "temp:snapshot" in state_delta
    assert len(state_delta["recently_asked"]) > 0

