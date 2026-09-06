"""
tests/test_lifecycle.py — Candidate lifecycle state machine tests (FLOW-047, Q8).

Validates:
1. LifecycleStatusEnum contains exactly the 5 states (Q8).
2. Application-pipeline states never leak onto the candidate record.
3. Full transition matrix: all legal transitions succeed.
4. All illegal transitions raise InvalidLifecycleTransitionError (subclass of ValueError).
5. Audit event emission for all valid transitions.
6. Zero outbound side effects: transitions never enqueue or emit messages.
7. Reactive dormancy observation (Q2).
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest

from app.db.uow import UnitOfWork
from app.domain.lifecycle import (
    ALLOWED_TRANSITIONS,
    FORBIDDEN_APPLICATION_STATES,
    InvalidLifecycleTransitionError,
    LifecycleTransitionEvent,
    check_and_mark_dormant,
    transition_candidate_lifecycle,
)
from app.models.candidate import Candidate
from app.models.enums import LifecycleStatusEnum


def test_lifecycle_enum_contains_exactly_five_states():
    """Q8: Candidate lifecycle contains only the five core Flow states."""
    expected_states = {"new", "intake", "profile_ready", "dormant", "blocked"}
    actual_states = {s.value for s in LifecycleStatusEnum}
    assert actual_states == expected_states
    assert len(LifecycleStatusEnum) == 5


def test_application_states_strictly_forbidden():
    """Q8: Application-pipeline states belong to future matching, never Flow candidate."""
    cand = Candidate(phone_number="+919876543210", lifecycle_status=LifecycleStatusEnum.intake)

    for app_state in FORBIDDEN_APPLICATION_STATES:
        with pytest.raises(InvalidLifecycleTransitionError) as exc_info:
            transition_candidate_lifecycle(cand, app_state, reason="Pipeline advancement")
        assert "Forbidden application pipeline state" in str(exc_info.value)
        assert issubclass(InvalidLifecycleTransitionError, ValueError)


def test_transition_matrix_legal_transitions():
    """All permitted paths through the candidate lifecycle execute cleanly."""
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    for source_state, target_set in ALLOWED_TRANSITIONS.items():
        for target_state in target_set:
            cand = Candidate(
                phone_number=f"+91{uuid4().int % 10000000000:010d}",
                lifecycle_status=source_state,
            )
            event = transition_candidate_lifecycle(
                candidate=cand,
                target_status=target_state,
                reason=f"Transition from {source_state.value} to {target_state.value}",
                now=now,
            )

            assert cand.lifecycle_status == target_state
            assert isinstance(event, LifecycleTransitionEvent)
            assert event.from_status == source_state
            assert event.to_status == target_state
            assert event.timestamp == now


def test_illegal_transitions_raise():
    """Illegal transitions must raise InvalidLifecycleTransitionError rather than silently no-op."""
    illegal_pairs = [
        (LifecycleStatusEnum.new, LifecycleStatusEnum.profile_ready),  # skips intake
        (LifecycleStatusEnum.new, LifecycleStatusEnum.dormant),        # cannot be dormant without intake
        (LifecycleStatusEnum.intake, LifecycleStatusEnum.new),         # cannot revert to new
        (LifecycleStatusEnum.profile_ready, LifecycleStatusEnum.new),  # cannot revert to new
        (LifecycleStatusEnum.dormant, LifecycleStatusEnum.new),        # cannot revert to new
        # Self transitions (silent no-ops forbidden)
        (LifecycleStatusEnum.new, LifecycleStatusEnum.new),
        (LifecycleStatusEnum.intake, LifecycleStatusEnum.intake),
        (LifecycleStatusEnum.profile_ready, LifecycleStatusEnum.profile_ready),
        (LifecycleStatusEnum.dormant, LifecycleStatusEnum.dormant),
        (LifecycleStatusEnum.blocked, LifecycleStatusEnum.blocked),
    ]

    for source_state, target_state in illegal_pairs:
        cand = Candidate(
            phone_number="+919999999999",
            lifecycle_status=source_state,
        )
        with pytest.raises(InvalidLifecycleTransitionError):
            transition_candidate_lifecycle(
                candidate=cand,
                target_status=target_state,
                reason="Illegal attempt",
            )


def test_unknown_status_and_empty_reason_raise():
    """Invalid statuses and empty reasons must raise ValueError."""
    cand = Candidate(phone_number="+919999999999", lifecycle_status=LifecycleStatusEnum.intake)

    # Empty reason
    with pytest.raises(ValueError, match="Lifecycle transition requires a non-empty reason"):
        transition_candidate_lifecycle(cand, LifecycleStatusEnum.profile_ready, reason="")

    # Unknown status
    with pytest.raises(InvalidLifecycleTransitionError):
        transition_candidate_lifecycle(cand, "completely_unknown_state", reason="Testing unknown")


def test_blocked_at_timestamp_maintenance():
    """Blocking sets blocked_at; unblocking clears blocked_at."""
    now = datetime(2026, 9, 6, 15, 30, tzinfo=timezone.utc)
    cand = Candidate(
        phone_number="+919876543210",
        lifecycle_status=LifecycleStatusEnum.intake,
        blocked_at=None,
    )

    # 1. Block
    transition_candidate_lifecycle(cand, LifecycleStatusEnum.blocked, reason="Spam", now=now)
    assert cand.lifecycle_status == LifecycleStatusEnum.blocked
    assert cand.blocked_at == now

    # 2. Unblock to intake
    transition_candidate_lifecycle(cand, LifecycleStatusEnum.intake, reason="Admin unblock", now=now)
    assert cand.lifecycle_status == LifecycleStatusEnum.intake
    assert cand.blocked_at is None


def test_zero_side_effects_invariant():
    """No lifecycle transition ever generates, enqueues, or dispatches a message."""
    cand = Candidate(
        phone_number="+919876543210",
        lifecycle_status=LifecycleStatusEnum.new,
    )

    # Transition through full lifecycle
    states_sequence = [
        (LifecycleStatusEnum.intake, "consent_granted"),
        (LifecycleStatusEnum.profile_ready, "profile_ready"),
        (LifecycleStatusEnum.dormant, "inactivity"),
        (LifecycleStatusEnum.blocked, "admin_block"),
        (LifecycleStatusEnum.intake, "admin_unblock"),
    ]

    for target_state, reason in states_sequence:
        event = transition_candidate_lifecycle(cand, target_state, reason=reason)
        # Verify candidate message collections or queues are completely empty
        assert len(cand.messages) == 0
        assert getattr(event, "outbound_message", None) is None


def test_audit_event_persisted_with_uow(db):
    """Lifecycle transition writes ModerationEvent audit trail in database when UoW is present."""
    uow = UnitOfWork(session=db)
    phone = f"+91{uuid4().int % 10000000000:010d}"

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.lifecycle_status = LifecycleStatusEnum.intake
        uow.candidates.add(cand)
        uow.commit()

    with uow:
        cand = uow.candidates.get_by_phone(phone)
        transition_candidate_lifecycle(
            candidate=cand,
            target_status=LifecycleStatusEnum.profile_ready,
            reason="All 6 blocking fields confirmed and current",
            uow=uow,
        )
        uow.commit()

    with uow:
        events = uow.moderation_events.get_by_candidate(cand.id)
        assert len(events) >= 1
        audit = events[0]
        assert audit.kind == "lifecycle_transition"
        assert "intake -> profile_ready" in audit.detail
        assert "All 6 blocking fields confirmed" in audit.detail


def test_check_and_mark_dormant_reactive_only():
    """Inactivity window check transitions candidate to dormant without side effects (Q2)."""
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    cand = Candidate(phone_number="+919876543210", lifecycle_status=LifecycleStatusEnum.intake)

    # 1. Active recently (30 days ago) -> remains intake
    recent_activity = now - timedelta(days=30)
    changed = check_and_mark_dormant(cand, recent_activity, inactivity_window_days=365, now=now)
    assert changed is False
    assert cand.lifecycle_status == LifecycleStatusEnum.intake

    # 2. Inactive beyond window (400 days ago) -> transitions to dormant
    old_activity = now - timedelta(days=400)
    changed = check_and_mark_dormant(cand, old_activity, inactivity_window_days=365, now=now)
    assert changed is True
    assert cand.lifecycle_status == LifecycleStatusEnum.dormant
    assert len(cand.messages) == 0  # Reactive only: zero outbound messages

    # 3. Already dormant -> does not transition again
    changed = check_and_mark_dormant(cand, old_activity, inactivity_window_days=365, now=now)
    assert changed is False
