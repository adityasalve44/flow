"""
tests/test_identity_policy.py — Greeting and name policy tests (FLOW-026, §3).

Tests:
1. Five greeting branches (§3 greeting table):
   - First message with facts -> ack_and_continue_intake (starts intake, no name ask)
   - Profile name conflicts with contact name -> clarify_name
   - Profile name known & compatible -> greet_known
   - No profile name, contact name present -> greet_with_contact_name
   - No profile name, no contact name -> greet_and_ask_name
2. Fuzzy name comparison:
   - 'Rahul K' and 'Rahul' are compatible (no needless clarification).
   - 'Rahul Kumar' and 'Rahul' are compatible.
   - 'Rahul' and 'Rohit' conflict.
3. Confirmation writes vs non-confirmation:
   - Channel contact_name updates candidate.display_name, never profile.full_name.
   - profile.full_name is written ONLY upon explicit self-identification or confirmation.
4. Policy integration:
   - Contact 'Rahul' against profile 'Rohit' triggers clarify_name rung and preserves profile.
   - A first message full of facts begins intake without asking the name.
"""

from uuid import uuid4

from app.agents.schemas import ExtractedFact, ExtractionConfidenceEnum, IntentEnum, TurnExtraction
from app.db.uow import UnitOfWork
from app.domain.identity import (
    evaluate_greeting_directive,
    names_are_compatible,
    update_candidate_identity,
)
from app.domain.policy import evaluate_policy_step
from app.models import Candidate, CandidateProfile, Conversation
from app.models.enums import ChannelEnum, ConsentStatusEnum, ConversationModeEnum, ConversationStatusEnum


def test_greeting_branch_1_facts_in_first_message():
    """
    Branch 1 (§3):
    First message already carries recruitment facts -> ack_and_continue_intake.
    Intake starts immediately; name is collected later, opportunistically.
    """
    directive, ctx = evaluate_greeting_directive(
        contact_name="Aditya",
        profile_name=None,
        has_extracted_facts=True,
    )
    assert directive == "ack_and_continue_intake"
    assert ctx["skip_name_ask"] is True


def test_greeting_branch_2_conflicting_names():
    """
    Branch 2 (§3):
    Profile name conflicts with contact name -> clarify_name.
    """
    directive, ctx = evaluate_greeting_directive(
        contact_name="Rahul",
        profile_name="Rohit",
        has_extracted_facts=False,
    )
    assert directive == "clarify_name"
    assert ctx["contact_name"] == "Rahul"
    assert ctx["profile_name"] == "Rohit"


def test_greeting_branch_3_known_profile_name():
    """
    Branch 3 (§3):
    Profile name known and matches/compatible -> greet_known.
    """
    directive, ctx = evaluate_greeting_directive(
        contact_name="Rahul K",
        profile_name="Rahul",
        has_extracted_facts=False,
    )
    assert directive == "greet_known"
    assert ctx["name"] == "Rahul"


def test_greeting_branch_4_contact_name_only():
    """
    Branch 4 (§3):
    No profile name, but contact name present -> greet_with_contact_name.
    """
    directive, ctx = evaluate_greeting_directive(
        contact_name="Deepa Sharma",
        profile_name=None,
        has_extracted_facts=False,
    )
    assert directive == "greet_with_contact_name"
    assert ctx["name"] == "Deepa Sharma"


def test_greeting_branch_5_no_name_available():
    """
    Branch 5 (§3):
    No profile name and no contact name -> greet_and_ask_name.
    """
    directive, ctx = evaluate_greeting_directive(
        contact_name=None,
        profile_name=None,
        has_extracted_facts=False,
    )
    assert directive == "greet_and_ask_name"


def test_fuzzy_name_compatibility():
    """Fuzzy comparison avoids needless clarifications for minor formatting variations."""
    # Compatible
    assert names_are_compatible("Rahul", "Rahul") is True
    assert names_are_compatible("Rahul K", "Rahul") is True
    assert names_are_compatible("Rahul Kumar", "Rahul") is True
    assert names_are_compatible("Priya Sharma", "Priya S.") is True
    assert names_are_compatible("Alice", None) is True

    # Conflicting
    assert names_are_compatible("Rahul", "Rohit") is False
    assert names_are_compatible("Priya Sharma", "Ananya Singh") is False
    assert names_are_compatible("Alice", "Bob") is False


def test_identity_update_trust_boundaries():
    """
    Acceptance test (FLOW-026, §3):
    - Channel contact_name writes to display_name, NEVER profile.full_name.
    - Explicit confirmation or self-identification writes profile.full_name.
    """
    cand = Candidate(phone_number="+919876543210")
    profile = CandidateProfile(candidate_id=cand.id)

    # 1. WhatsApp channel metadata arrives
    updated = update_candidate_identity(
        candidate=cand,
        profile=profile,
        contact_name="Rahul WhatsApp",
    )
    assert updated is False
    assert cand.display_name == "Rahul WhatsApp"
    assert profile.full_name is None, "profile.full_name must NOT be written by channel metadata"

    # 2. Candidate self-identifies in message text: "I'm Rahul Sharma"
    updated_self = update_candidate_identity(
        candidate=cand,
        profile=profile,
        self_identified_name="Rahul Sharma",
    )
    assert updated_self is True
    assert profile.full_name == "Rahul Sharma"
    assert cand.display_name == "Rahul Sharma"


def test_clarify_name_policy_integration(db):
    """
    Acceptance test (FLOW-026):
    Contact 'Rahul' against profile 'Rohit' produces clarify_name and changes nothing until confirmed.
    """
    uow = UnitOfWork(session=db)
    cand_phone = f"+9198{uuid4().int % 100000000:08d}"

    with uow:
        cand = uow.candidates.get_or_create_by_phone(cand_phone)
        cand.consent_status = ConsentStatusEnum.granted
        cand.display_name = "Rahul"  # WhatsApp contact name
        uow.candidates.add(cand)

        profile = CandidateProfile(
            candidate_id=cand.id,
            full_name="Rohit",  # Known profile name
        )
        uow.profiles.add(profile)

        conv = uow.conversations.create(candidate_id=cand.id, channel=ChannelEnum.simulator)
        conv.mode = ConversationModeEnum.intake
        uow.commit()

        # Turn with no facts (e.g. casual greeting "Hi there")
        extraction = TurnExtraction(intent=IntentEnum.greet, facts=[], questions=[])
        directive, snapshot = evaluate_policy_step(
            uow=uow,
            candidate=cand,
            conversation=conv,
            extraction=extraction,
        )

        assert directive.name == "clarify_name"

        # Assert profile.full_name was NOT overwritten
        refreshed_profile = uow.profiles.get_by_candidate_id(cand.id)
        assert refreshed_profile.full_name == "Rohit"


def test_first_message_full_of_facts_starts_intake_without_asking_name(db):
    """
    Acceptance test (FLOW-026):
    A first message full of facts starts intake without asking the name.
    """
    uow = UnitOfWork(session=db)
    cand_phone = f"+9198{uuid4().int % 100000000:08d}"

    with uow:
        cand = uow.candidates.get_or_create_by_phone(cand_phone)
        cand.consent_status = ConsentStatusEnum.granted
        cand.display_name = "Anonymous"
        uow.candidates.add(cand)

        conv = uow.conversations.create(candidate_id=cand.id, channel=ChannelEnum.simulator)
        conv.mode = ConversationModeEnum.intake
        uow.commit()

        # Candidate provides role and experience immediately
        extraction = TurnExtraction(
            intent=IntentEnum.provide_info,
            facts=[
                ExtractedFact(
                    key="desired_role",
                    value="Python Developer",
                    raw_text="I am a Python developer",
                    confidence=ExtractionConfidenceEnum.stated,
                ),
                ExtractedFact(
                    key="experience_years",
                    value=4.0,
                    raw_text="4 years experience",
                    confidence=ExtractionConfidenceEnum.stated,
                ),
            ],
            questions=[],
        )

        directive, snapshot = evaluate_policy_step(
            uow=uow,
            candidate=cand,
            conversation=conv,
            extraction=extraction,
        )

        # Must NOT trigger clarify_name or ask_name; continues intake immediately!
        assert directive.name == "ask_next"
        assert "full_name" not in directive.fields_to_ask
        assert any(f in ("skills", "location_preference", "expected_ctc", "notice_period") for f in directive.fields_to_ask)
