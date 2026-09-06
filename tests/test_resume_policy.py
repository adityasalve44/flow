"""
tests/test_resume_policy.py — Tests for resume conversation policy (FLOW-036).

Validates (§8, §11, FLOW-036 of REVIEW_AND_PLAN.md):
- First ask: When candidate is profile-ready and has no resume, Flow asks once (`ask_resume`).
- Confirmation: When candidate has a resume on file, Flow asks to confirm (`confirm_resume`).
- Affirmative confirmation: Candidate replies "yes" -> confirmed_at set, no file requested, directive moves on.
- Replacement: Ingesting a new file increments version without restarting intake.
- No repeat ask: A confirmed or asked resume is NEVER requested again in that conversation.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.agents.schemas import IntentEnum, TurnExtraction
from app.channel.media import validate_media
from app.db.uow import UnitOfWork
from app.domain.policy import evaluate_policy_step
from app.models import CandidateAttribute
from app.models.enums import (
    AttributeStatusEnum,
    ConfidenceEnum,
    ConsentStatusEnum,
    DataClassEnum,
    LifecycleStatusEnum,
    SourceEnum,
)
from app.models.resume import Resume
from app.services.resume import ingest_resume
from app.storage.local import LocalStorageAdapter


def _populate_baseline_profile(uow: UnitOfWork, cand_id) -> None:
    """Populate 6 baseline operational fields to make candidate profile ready."""
    facts = [
        ("desired_role", "Backend Engineer"),
        ("experience_years", 5.0),
        ("skills", ["Python", "FastAPI"]),
        ("location_preference", ["Bengaluru"]),
        ("expected_ctc", 2500000.0),
        ("notice_period", 30),
    ]
    for key, val in facts:
        uow.attributes.add(
            CandidateAttribute(
                candidate_id=cand_id,
                key=key,
                value=val if not isinstance(val, (int, float, str, list)) else (val if isinstance(val, list) else str(val)),
                raw_text=str(val),
                source=SourceEnum.candidate_stated,
                confidence=ConfidenceEnum.confirmed,
                status=AttributeStatusEnum.current,
                data_class=DataClassEnum.operational,
            )
        )
    prof = uow.profiles.get_or_create(cand_id)
    prof.current_role = "Backend Engineer"
    prof.experience_years = 5.0
    prof.notice_period_days = 30
    prof.expected_ctc_annual = 2500000.0
    uow.profiles.add(prof)


@pytest.fixture
def candidate_and_conversation(db):
    """Setup candidate and active conversation with consent granted."""
    uow = UnitOfWork(session=db)
    phone = f"+91{uuid4().int % 10000000000:010d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone, display_name="Priya")
        cand.consent_status = ConsentStatusEnum.granted
        cand.lifecycle_status = LifecycleStatusEnum.intake
        uow.candidates.add(cand)

        conv = uow.conversations.create(candidate_id=cand.id)
        conv.ask_counts = {}
        uow.conversations.add(conv)
        uow.commit()
        return cand.id, conv.id


def test_resume_policy_first_ask(db, candidate_and_conversation):
    """
    First ask: When profile is ready and no resume exists on file,
    Flow asks for resume once.
    """
    cand_id, conv_id = candidate_and_conversation
    uow = UnitOfWork(session=db)

    with uow:
        _populate_baseline_profile(uow, cand_id)
        uow.commit()

    with uow:
        cand = uow.candidates.get_by_id(cand_id)
        conv = uow.conversations.get_by_id(conv_id)
        extraction = TurnExtraction(
            intent=IntentEnum.provide_info,
            facts=[],
            raw_message_text="I have 5 years of experience in Python and notice period is 30 days.",
        )

        directive, _ = evaluate_policy_step(uow, cand, conv, extraction)
        assert directive.name == "ask_resume"
        assert conv.ask_counts.get("resume") == 1


def test_resume_policy_confirmation_flow(db, candidate_and_conversation):
    """
    Confirmation flow (§11):
    1. Resume exists on file from earlier -> directive is confirm_resume.
    2. Candidate replies "yes" -> confirmed_at updated, directive moves on without requesting file.
    """
    cand_id, conv_id = candidate_and_conversation
    uow = UnitOfWork(session=db)

    # Populate baseline facts and a pre-existing unconfirmed/stale resume
    with uow:
        _populate_baseline_profile(uow, cand_id)
        stale_date = datetime.now(UTC) - timedelta(days=400)
        resume = Resume(
            candidate_id=cand_id,
            version=1,
            bucket="resumes",
            object_key=f"candidates/{cand_id}/resumes/prev.pdf",
            filename="my_old_cv.pdf",
            content_type="application/pdf",
            size_bytes=2048,
            checksum="hash-v1",
            is_current=True,
            source=SourceEnum.candidate_stated,
            uploaded_at=stale_date,
            confirmed_at=None,
            parse_status="pending",
        )
        uow.resumes.add(resume)
        uow.commit()

    # Turn 1: Flow asks to confirm existing resume
    with uow:
        cand = uow.candidates.get_by_id(cand_id)
        conv = uow.conversations.get_by_id(conv_id)
        extraction = TurnExtraction(
            intent=IntentEnum.provide_info,
            facts=[],
            raw_message_text="My details are all updated.",
        )
        directive, _ = evaluate_policy_step(uow, cand, conv, extraction)
        assert directive.name == "confirm_resume"
        assert conv.ask_counts.get("resume") == 1
        uow.commit()

    # Turn 2: Candidate replies "Yes, it is the latest one"
    with uow:
        cand = uow.candidates.get_by_id(cand_id)
        conv = uow.conversations.get_by_id(conv_id)
        conv.ask_counts = {"resume": 1}
        extraction_turn2 = TurnExtraction(
            intent=IntentEnum.provide_info,
            facts=[],
            raw_message_text="Yes, that's still the latest one.",
        )
        directive2, _ = evaluate_policy_step(uow, cand, conv, extraction_turn2)
        assert directive2.name == "acknowledge_resume_confirmed"

        # Verify confirmed_at was set
        current_res = uow.resumes.get_current(cand_id)
        assert current_res is not None
        assert current_res.confirmed_at is not None

        # Verify audit trail
        audits = uow.audit_events.get_by_entity("resume", str(current_res.id))
        assert any(a.action == "confirm" for a in audits)


def test_resume_never_requested_again_in_same_conversation(db, candidate_and_conversation):
    """
    No repeat ask: Once resume has been asked or confirmed in a conversation,
    Flow NEVER nags or asks again.
    """
    cand_id, conv_id = candidate_and_conversation
    uow = UnitOfWork(session=db)

    with uow:
        _populate_baseline_profile(uow, cand_id)

    with uow:
        cand = uow.candidates.get_by_id(cand_id)
        conv = uow.conversations.get_by_id(conv_id)
        conv.ask_counts = {"resume": 1}
        extraction = TurnExtraction(
            intent=IntentEnum.provide_info,
            facts=[],
            raw_message_text="I'm also open to hybrid roles in Pune.",
        )

        directive, _ = evaluate_policy_step(uow, cand, conv, extraction)
        # Must NOT be ask_resume or confirm_resume!
        assert directive.name not in ("ask_resume", "confirm_resume")
        assert directive.name == "acknowledge_profile_ready"


def test_resume_replacement_preserves_intake(tmp_path, db, candidate_and_conversation):
    """
    Replacement: Candidate uploads a new resume file.
    Version increments to 2, version 1 is archived, intake is preserved.
    """
    cand_id, conv_id = candidate_and_conversation
    storage = LocalStorageAdapter(base_dir=str(tmp_path / "storage"), bucket="resumes")
    uow = UnitOfWork(session=db)

    # 1. Existing version 1
    media1 = validate_media(data=b"%PDF-1.4 initial version", filename="v1.pdf", content_type="application/pdf")
    with uow:
        r1, is_new1 = ingest_resume(uow, cand_id, media1, storage=storage)
        assert is_new1 is True
        assert r1.version == 1
        assert r1.is_current is True
        uow.commit()

    # 2. Candidate uploads version 2
    media2 = validate_media(data=b"%PDF-1.4 newer version updated", filename="v2.pdf", content_type="application/pdf")
    with uow:
        r2, is_new2 = ingest_resume(uow, cand_id, media2, storage=storage)
        assert is_new2 is True
        assert r2.version == 2
        assert r2.is_current is True
        uow.commit()

    # Invariant checks
    with uow:
        resumes = uow.resumes.get_by_candidate(cand_id)
        assert len(resumes) == 2
        current = uow.resumes.get_current(cand_id)
        assert current.id == r2.id
        assert current.version == 2

        old = next(r for r in resumes if r.version == 1)
        assert old.is_current is False
