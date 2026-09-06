"""
app/channel/inbound.py — channel ingress validation and boundary defense.

Step 0 of the turn engine:
1. Normalise phone to E.164 (handled via InboundEvent validator).
2. Check idempotency via unique channel_message_id -> REPLAY (no-op).
3. Resolve candidate via atomic upsert (ON CONFLICT DO NOTHING + re-select).
4. Check candidates.blocked_at -> BLOCKED (store/drop, no reply, exit).
5. Check per-phone rate limit counted in Postgres -> RATE_LIMITED (no reply, exit).
6. Success -> PROCEED to conversation lifecycle and turn engine.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import UUID

from sqlalchemy import func, select

from app.api.schemas import InboundEvent
from app.db.uow import UnitOfWork
from app.models import Candidate, Message
from app.models.enums import DirectionEnum

DEFAULT_RATE_LIMIT_PER_MINUTE = 20


class IngressDecision(str, Enum):
    PROCEED = "proceed"
    REPLAY = "replay"
    BLOCKED = "blocked"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class IngressResult:
    """Outcome of the ingress evaluation."""

    decision: IngressDecision
    candidate: Candidate | None = None
    existing_message: Message | None = None
    detail: str | None = None


def check_rate_limit(
    uow: UnitOfWork,
    candidate_id: UUID,
    max_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
    now: datetime | None = None,
) -> bool:
    """
    Check if the candidate has exceeded the per-phone rate limit in Postgres.
    Returns True if rate limit is exceeded, False otherwise.
    """
    current_time = now or datetime.now(timezone.utc)
    window_start = current_time - timedelta(seconds=60)

    statement = select(func.count(Message.id)).where(
        Message.candidate_id == candidate_id,
        Message.direction == DirectionEnum.inbound,
        Message.created_at >= window_start,
    )
    count = uow.session.scalar(statement) or 0
    return count >= max_per_minute


def process_ingress(
    uow: UnitOfWork,
    event: InboundEvent,
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
    now: datetime | None = None,
) -> IngressResult:
    """
    Evaluate boundary defenses for an inbound event before processing the turn.

    Invariants (§8 of REVIEW_AND_PLAN.md):
    - Replay of an existing channel_message_id is an idempotent no-op.
    - Block check on candidates.blocked_at is evaluated as the very first gate.
    - Rate limit is strictly per-phone, counted directly in Postgres.
    """
    # 1. Idempotency gate: check if channel_message_id has already been processed
    existing_msg = uow.messages.get_by_channel_message_id(event.channel_message_id)
    if existing_msg is not None:
        candidate = uow.candidates.get_by_id(existing_msg.candidate_id)
        return IngressResult(
            decision=IngressDecision.REPLAY,
            candidate=candidate,
            existing_message=existing_msg,
            detail="Message already processed (idempotent replay)",
        )

    # 2. Resolve candidate via atomic upsert
    candidate = uow.candidates.get_or_create_by_phone(
        phone_number=event.phone_number,
        display_name=event.contact_name,
    )

    # 3. Blocked gate: candidates.blocked_at set?
    if candidate.blocked_at is not None:
        return IngressResult(
            decision=IngressDecision.BLOCKED,
            candidate=candidate,
            detail="Candidate is blocked",
        )

    # 4. Rate limit gate: per-phone rate limit in Postgres
    if check_rate_limit(
        uow,
        candidate_id=candidate.id,
        max_per_minute=rate_limit_per_minute,
        now=now,
    ):
        return IngressResult(
            decision=IngressDecision.RATE_LIMITED,
            candidate=candidate,
            detail=f"Rate limit exceeded (>{rate_limit_per_minute} msgs/min)",
        )

    # All boundary checks passed
    return IngressResult(
        decision=IngressDecision.PROCEED,
        candidate=candidate,
        detail="Boundary checks passed",
    )
