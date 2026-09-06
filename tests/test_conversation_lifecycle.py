"""
tests/test_conversation_lifecycle.py — tests for conversation lifecycle boundaries.

Tests:
1. 23h elapsed -> same conversation continues (active_window_hours = 24).
2. 25h elapsed -> old conversation closed, new conversation opened, profile intact.
3. 400 days elapsed -> new conversation opened in mode=refresh, old facts marked stale not deleted.
4. Candidate with pending consent opens in mode=consent.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.uow import UnitOfWork
from app.models import CandidateAttribute, CandidateProfile
from app.models.enums import (
    AttributeStatusEnum,
    ConfidenceEnum,
    ConsentStatusEnum,
    ConversationModeEnum,
    ConversationStatusEnum,
    DataClassEnum,
    SourceEnum,
)
from app.services.conversation import resolve_conversation


def test_23h_boundary_same_conversation(db):
    """
    Acceptance test: 23 hours later within active_window_hours (24h)
    resolves to the same ongoing conversation.
    """
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    t0 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        uow.candidates.add(cand)

        # Initial conversation started at t0
        conv1 = resolve_conversation(uow, cand, now=t0)
        assert conv1.status == ConversationStatusEnum.active
        conv1_id = conv1.id

        # Candidate replies 23 hours later
        t_23h = t0 + timedelta(hours=23)
        conv2 = resolve_conversation(uow, cand, now=t_23h)

        # Must be the exact same conversation
        assert conv2.id == conv1_id
        assert conv2.status == ConversationStatusEnum.active
        assert conv2.last_inbound_at == t_23h


def test_25h_boundary_new_conversation_profile_intact(db):
    """
    Acceptance test: 25 hours later exceeds active_window_hours (24h).
    Old conversation is closed; a new intake conversation opens; profile is intact.
    """
    uow = UnitOfWork(session=db)
    phone = f"+9192{uuid4().int % 100000000:08d}"
    t0 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        uow.candidates.add(cand)

        # Set up an initial profile
        profile = CandidateProfile(
            candidate_id=cand.id,
            current_role="Staff Engineer",
            experience_years=8.0,
            last_refreshed_at=t0,
        )
        db.add(profile)
        db.flush()

        # Initial conversation started at t0
        conv1 = resolve_conversation(uow, cand, now=t0)
        conv1_id = conv1.id

        # Inbound message 25 hours later
        t_25h = t0 + timedelta(hours=25)
        conv2 = resolve_conversation(uow, cand, now=t_25h)

        # Must be a new conversation
        assert conv2.id != conv1_id
        assert conv2.status == ConversationStatusEnum.active
        assert conv2.mode == ConversationModeEnum.intake

        # Old conversation must be closed
        old_conv = uow.conversations.get_by_id(conv1_id)
        assert old_conv.status == ConversationStatusEnum.closed
        assert old_conv.closed_at == t_25h

        # Profile remains intact
        refreshed_profile = db.get(CandidateProfile, cand.id)
        assert refreshed_profile.current_role == "Staff Engineer"
        assert refreshed_profile.experience_years == 8.0


def test_400_days_boundary_refresh_mode_facts_stale_not_deleted(db):
    """
    Acceptance test: 400 days later exceeds stale_profile_days (365 days).
    Opens in mode=refresh; previous attributes become status=stale; never deleted.
    """
    uow = UnitOfWork(session=db)
    phone = f"+9193{uuid4().int % 100000000:08d}"
    t0 = datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        uow.candidates.add(cand)

        # Initial profile with last_refreshed_at = t0
        profile = CandidateProfile(
            candidate_id=cand.id,
            current_role="Junior Developer",
            experience_years=1.0,
            last_refreshed_at=t0,
        )
        db.add(profile)
        db.flush()

        # Attribute facts recorded at t0
        attr1 = CandidateAttribute(
            candidate_id=cand.id,
            key="current_role",
            value="Junior Developer",
            source=SourceEnum.candidate_stated.value,
            confidence=ConfidenceEnum.confirmed.value,
            data_class=DataClassEnum.operational.value,
            status=AttributeStatusEnum.current.value,
        )
        attr2 = CandidateAttribute(
            candidate_id=cand.id,
            key="experience_years",
            value={"amount": 1.0},
            source=SourceEnum.candidate_stated.value,
            confidence=ConfidenceEnum.confirmed.value,
            data_class=DataClassEnum.operational.value,
            status=AttributeStatusEnum.current.value,
        )
        uow.attributes.add(attr1)
        uow.attributes.add(attr2)

        # Candidate returns 400 days later
        t_400d = t0 + timedelta(days=400)
        conv = resolve_conversation(uow, cand, now=t_400d)

        # Must open in refresh mode
        assert conv.mode == ConversationModeEnum.refresh
        assert conv.status == ConversationStatusEnum.active

        # Check facts: must NOT be deleted, but status changed to stale
        all_facts = uow.attributes.get_all_for_candidate(cand.id)
        assert len(all_facts) == 2

        # None should have status=current
        current_facts = uow.attributes.get_current_for_candidate(cand.id)
        assert len(current_facts) == 0

        # Both facts are marked stale
        for f in all_facts:
            assert f.status == AttributeStatusEnum.stale


def test_pending_consent_opens_in_consent_mode(db):
    """
    New candidate with consent_status=pending opens in mode=consent (Q4).
    """
    uow = UnitOfWork(session=db)
    phone = f"+9194{uuid4().int % 100000000:08d}"

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        assert cand.consent_status == ConsentStatusEnum.pending

        conv = resolve_conversation(uow, cand)
        assert conv.mode == ConversationModeEnum.consent
        assert conv.status == ConversationStatusEnum.active
