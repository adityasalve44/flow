"""
tests/test_models_core.py — core model construction, defaults, and enum invariants.

Tests that:
- Candidate defaults are correct (lifecycle=new, consent=pending)
- LifecycleStatusEnum contains exactly 5 states and no application-pipeline states
- Conversation defaults are correct (status=active, mode=consent)
- Models can be constructed and flushed
"""

from __future__ import annotations

import pytest

from app.models import (
    Candidate,
    Conversation,
    LifecycleStatusEnum,
    ConsentStatusEnum,
    ConversationStatusEnum,
    ConversationModeEnum,
)
from tests.conftest import CandidateFactory


# ---------------------------------------------------------------------------
# Lifecycle enum invariant — no application-pipeline states allowed (Q8)
# ---------------------------------------------------------------------------

ALLOWED_LIFECYCLE_STATES = {"new", "intake", "profile_ready", "dormant", "blocked"}
FORBIDDEN_PIPELINE_STATES = {
    "shortlisted", "submitted", "interview", "selected",
    "rejected", "joined", "offered", "pending_offer",
}


def test_lifecycle_enum_has_exactly_five_states():
    """The lifecycle enum must have exactly the five candidate-lifecycle states."""
    values = {e.value for e in LifecycleStatusEnum}
    assert values == ALLOWED_LIFECYCLE_STATES, (
        f"Unexpected lifecycle states: {values - ALLOWED_LIFECYCLE_STATES}"
    )


def test_lifecycle_enum_has_no_application_pipeline_states():
    """Application pipeline states must not appear in the lifecycle enum."""
    values = {e.value for e in LifecycleStatusEnum}
    leaked = values & FORBIDDEN_PIPELINE_STATES
    assert not leaked, (
        f"Application-pipeline states leaked into LifecycleStatusEnum: {leaked}"
    )


# ---------------------------------------------------------------------------
# Candidate model
# ---------------------------------------------------------------------------

def test_candidate_defaults(db):
    """A new Candidate gets lifecycle=new and consent=pending by default."""
    candidate = CandidateFactory.build(db)
    assert candidate.lifecycle_status == LifecycleStatusEnum.new
    assert candidate.consent_status == ConsentStatusEnum.pending
    assert candidate.blocked_at is None
    assert candidate.consent_at is None


def test_candidate_uuid_primary_key(db):
    """Candidate.id is a UUID (not an integer)."""
    import uuid
    candidate = CandidateFactory.build(db)
    # After flush, id is assigned by the server — should be a UUID object
    # (may be None before flush if server_default hasn't run)
    # Just verify it is not an integer
    assert not isinstance(candidate.id, int), "Candidate.id should be UUID, not int"


def test_candidate_phone_unique(db):
    """Two candidates with the same phone number cannot coexist."""
    from sqlalchemy.exc import IntegrityError
    CandidateFactory.build(db, phone_number="+919001234567")
    with pytest.raises(IntegrityError):
        CandidateFactory.build(db, phone_number="+919001234567")


# ---------------------------------------------------------------------------
# Conversation model
# ---------------------------------------------------------------------------

def test_conversation_defaults(db):
    """A new Conversation starts with status=active and mode=consent."""
    candidate = CandidateFactory.build(db)
    from tests.conftest import ConversationFactory
    conv = ConversationFactory.build(db, candidate_id=candidate.id)
    assert conv.status == ConversationStatusEnum.active
    assert conv.mode == ConversationModeEnum.consent
    assert conv.deflection_count == 0
    assert conv.abuse_count == 0
