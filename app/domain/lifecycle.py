"""
app/domain/lifecycle.py — Candidate lifecycle state machine (FLOW-047, Q8).

The candidate lifecycle has exactly FIVE explicit states (§6, §8, Q8):
    new → intake → profile_ready → dormant → blocked

Strict separation of concerns (Q8):
- The candidate lifecycle tracks recruitment contact & readiness in Flow.
- Application-pipeline states (shortlisted, submitted, interview, selected,
  joined, rejected) belong to the future matching system and MUST NEVER appear
  on the candidate record.
- Every transition is audited with a reason.
- Illegal transitions raise InvalidLifecycleTransitionError rather than silently no-op.
- Reactive only (Q2): No transition ever enqueues or emits an outbound message.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.models.candidate import Candidate
from app.models.enums import LifecycleStatusEnum

# Application pipeline states owned by the future matching system (Q8).
# These must NEVER appear on the candidate record.
FORBIDDEN_APPLICATION_STATES: frozenset[str] = frozenset({
    "shortlisted",
    "submitted",
    "interview",
    "interviewing",
    "selected",
    "joined",
    "rejected",
    "applied",
    "screening",
    "screened",
    "hired",
    "offered",
    "offer_extended",
    "offer_accepted",
})

# Complete transition matrix for valid state machine paths
ALLOWED_TRANSITIONS: dict[LifecycleStatusEnum, frozenset[LifecycleStatusEnum]] = {
    LifecycleStatusEnum.new: frozenset({
        LifecycleStatusEnum.intake,       # on consent granted
        LifecycleStatusEnum.blocked,      # on admin block
    }),
    LifecycleStatusEnum.intake: frozenset({
        LifecycleStatusEnum.profile_ready,  # when is_profile_ready returns true
        LifecycleStatusEnum.dormant,        # on inactivity beyond window
        LifecycleStatusEnum.blocked,        # on admin block
    }),
    LifecycleStatusEnum.profile_ready: frozenset({
        LifecycleStatusEnum.intake,       # on mode=refresh or fact invalidation
        LifecycleStatusEnum.dormant,      # on inactivity beyond window
        LifecycleStatusEnum.blocked,      # on admin block
    }),
    LifecycleStatusEnum.dormant: frozenset({
        LifecycleStatusEnum.intake,       # candidate returns / activity resumes
        LifecycleStatusEnum.profile_ready,# candidate returns with ready profile
        LifecycleStatusEnum.blocked,      # on admin block
    }),
    LifecycleStatusEnum.blocked: frozenset({
        LifecycleStatusEnum.new,          # admin unblock (if was pre-consent)
        LifecycleStatusEnum.intake,       # admin unblock (standard)
        LifecycleStatusEnum.profile_ready,# admin unblock (if profile was ready)
        LifecycleStatusEnum.dormant,      # admin unblock to dormant
    }),
}


class InvalidLifecycleTransitionError(ValueError):
    """Raised when an illegal candidate lifecycle transition is attempted."""

    def __init__(
        self,
        from_status: Any,
        to_status: Any,
        reason: str | None = None,
    ):
        msg = f"Invalid candidate lifecycle transition from '{from_status}' to '{to_status}'"
        if reason:
            msg += f" (reason: {reason})"
        super().__init__(msg)
        self.from_status = from_status
        self.to_status = to_status
        self.reason = reason


@dataclass(frozen=True)
class LifecycleTransitionEvent:
    """Audit record emitted upon every valid lifecycle transition."""

    candidate_id: UUID | str | None
    from_status: LifecycleStatusEnum
    to_status: LifecycleStatusEnum
    reason: str
    timestamp: datetime


def transition_candidate_lifecycle(
    candidate: Candidate,
    target_status: LifecycleStatusEnum | str,
    reason: str,
    uow: Any | None = None,
    now: datetime | None = None,
) -> LifecycleTransitionEvent:
    """
    Execute a pure candidate lifecycle state transition.

    Rules & Invariants:
    1. Only the five explicit candidate lifecycle states are allowed.
    2. Application pipeline states raise InvalidLifecycleTransitionError immediately.
    3. Self-transitions or transitions outside ALLOWED_TRANSITIONS raise InvalidLifecycleTransitionError.
    4. Transition writes an audit record in ModerationEvent if UoW is provided.
    5. Zero side-effects: no outbound messages are ever generated, queued, or dispatched.
    """
    if not reason or not reason.strip():
        raise ValueError("Lifecycle transition requires a non-empty reason for audit trail.")

    # Guard against application pipeline state contamination
    target_str = target_status.value if isinstance(target_status, LifecycleStatusEnum) else str(target_status)
    if target_str.lower() in FORBIDDEN_APPLICATION_STATES:
        raise InvalidLifecycleTransitionError(
            from_status=candidate.lifecycle_status,
            to_status=target_str,
            reason=f"Forbidden application pipeline state '{target_str}'. Pipeline states belong to matching system (Q8).",
        )

    # Validate target status enum
    try:
        target_enum = LifecycleStatusEnum(target_status)
    except ValueError as exc:
        raise InvalidLifecycleTransitionError(
            from_status=candidate.lifecycle_status,
            to_status=target_status,
            reason=f"Unknown lifecycle status '{target_status}'",
        ) from exc

    current_status = candidate.lifecycle_status
    if not isinstance(current_status, LifecycleStatusEnum):
        current_status = LifecycleStatusEnum(current_status)

    # Reject self-transitions (illegal transition, no silent no-ops)
    if current_status == target_enum:
        raise InvalidLifecycleTransitionError(
            from_status=current_status,
            to_status=target_enum,
            reason=f"Candidate is already in state '{target_enum.value}'",
        )

    # Validate transition against matrix
    allowed_targets = ALLOWED_TRANSITIONS.get(current_status, frozenset())
    if target_enum not in allowed_targets:
        raise InvalidLifecycleTransitionError(
            from_status=current_status,
            to_status=target_enum,
            reason=reason,
        )

    transition_time = now or datetime.now(UTC)

    # Apply state mutation
    candidate.lifecycle_status = target_enum

    # Maintain blocked_at timestamp
    if target_enum == LifecycleStatusEnum.blocked:
        if candidate.blocked_at is None:
            candidate.blocked_at = transition_time
    elif current_status == LifecycleStatusEnum.blocked:
        # Unblocked
        candidate.blocked_at = None

    # Construct audit event
    event = LifecycleTransitionEvent(
        candidate_id=getattr(candidate, "id", None),
        from_status=current_status,
        to_status=target_enum,
        reason=reason.strip(),
        timestamp=transition_time,
    )

    # Persist audit record if UoW with moderation_events is supplied
    if uow is not None and getattr(uow, "moderation_events", None) is not None:
        cand_id = getattr(candidate, "id", None)
        if cand_id:
            uow.moderation_events.create(
                candidate_id=cand_id,
                kind="lifecycle_transition",
                detail=f"{current_status.value} -> {target_enum.value}: {reason.strip()}",
            )

    return event


def check_and_mark_dormant(
    candidate: Candidate,
    last_activity_at: datetime | None,
    inactivity_window_days: int = 365,
    uow: Any | None = None,
    now: datetime | None = None,
) -> bool:
    """
    Reactive-only check: determine whether an inactive candidate should move to dormant.

    §8, Q2: Dormancy is a state observed by a query. It NEVER triggers an outbound
    message, reminder, or notification.
    """
    if candidate.lifecycle_status not in (
        LifecycleStatusEnum.intake,
        LifecycleStatusEnum.profile_ready,
    ):
        return False

    if last_activity_at is None:
        return False

    current_time = now or datetime.now(UTC)
    # Ensure timezone awareness
    if last_activity_at.tzinfo is None:
        last_activity_at = last_activity_at.replace(tzinfo=UTC)

    delta = current_time - last_activity_at
    if delta.days >= inactivity_window_days:
        transition_candidate_lifecycle(
            candidate=candidate,
            target_status=LifecycleStatusEnum.dormant,
            reason=f"Inactivity window of {inactivity_window_days} days exceeded ({delta.days} days since last activity)",
            uow=uow,
            now=current_time,
        )
        return True

    return False
