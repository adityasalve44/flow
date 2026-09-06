"""
app/api/limits.py — Rate limiting, replay protection, and daily budgets (FLOW-042).

Core requirements (§8, FLOW-042 of REVIEW_AND_PLAN.md):
1. Per-phone and per-IP limits with a Postgres-backed counter (flow.rate_limit_hits).
2. Reject webhook timestamps outside a five-minute window (±300s).
3. A per-candidate daily model-call budget that degrades to a polite hold rather than unbounded spend.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.db.uow import UnitOfWork
from app.models.candidate import Message
from app.models.enums import DirectionEnum
from app.models.rate_limit import RateLimitHit

DEFAULT_REPLAY_WINDOW_SECONDS = 300  # 5 minutes
DEFAULT_DAILY_MODEL_BUDGET = 30
POLITE_HOLD_MESSAGE = (
    "You have reached the daily message limit with Flow. "
    "Our recruitment team will review your profile, or you can message us again tomorrow!"
)


def is_timestamp_valid(
    timestamp: datetime,
    max_drift_seconds: int = DEFAULT_REPLAY_WINDOW_SECONDS,
    now: datetime | None = None,
) -> bool:
    """
    Validate that an incoming event timestamp is within the acceptable window (FLOW-042).

    Protects against replay attacks and clock desynchronization.
    Rejects timestamps older than max_drift_seconds or in the future by more than max_drift_seconds.
    """
    current_time = now or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)

    diff = abs((current_time - timestamp).total_seconds())
    return diff <= max_drift_seconds


def record_and_check_rate_limit(
    session: Session,
    key: str,
    max_requests: int,
    window_seconds: int = 60,
    now: datetime | None = None,
) -> bool:
    """
    Postgres-backed sliding-window rate limit checker.

    Returns True if the request is ALLOWED.
    Returns False if the rate limit has been EXCEEDED.
    """
    current_time = now or datetime.now(UTC)
    window_start = current_time - timedelta(seconds=window_seconds)

    # Count recent hits within the sliding window
    count_stmt = select(func.count(RateLimitHit.id)).where(
        RateLimitHit.key == key,
        RateLimitHit.created_at >= window_start,
    )
    hit_count = session.scalar(count_stmt) or 0

    if hit_count >= max_requests:
        return False

    # Record new hit
    hit = RateLimitHit(key=key, created_at=current_time)
    session.add(hit)
    session.flush()
    return True


def check_daily_model_budget(
    uow: UnitOfWork,
    candidate_id: UUID,
    daily_limit: int = DEFAULT_DAILY_MODEL_BUDGET,
    now: datetime | None = None,
) -> bool:
    """
    Check if a candidate has exceeded their daily model-call turn budget (FLOW-042).

    Returns True if under budget (proceed with model pipeline).
    Returns False if budget exhausted (degrade to polite hold).
    """
    current_time = now or datetime.now(UTC)
    window_start = current_time - timedelta(hours=24)

    # Count inbound candidate messages processed in the last 24 hours
    count_stmt = select(func.count(Message.id)).where(
        Message.candidate_id == candidate_id,
        Message.direction == DirectionEnum.inbound,
        Message.created_at >= window_start,
    )
    inbound_count = uow.session.scalar(count_stmt) or 0

    return inbound_count < daily_limit


class IPRateLimiter:
    """FastAPI dependency for per-IP sliding window rate limiting."""

    def __init__(self, max_requests_per_minute: int | None = None):
        self.max_requests = max_requests_per_minute

    async def __call__(
        self,
        request: Request,
        db: Session = Depends(get_db),
    ) -> None:
        settings = get_settings()
        limit = self.max_requests or settings.rate_limit_per_ip_per_minute

        # Extract client IP
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
        else:
            client_ip = request.client.host if request.client else "unknown"

        rate_limit_key = f"ip:{client_ip}"
        allowed = record_and_check_rate_limit(
            session=db,
            key=rate_limit_key,
            max_requests=limit,
            window_seconds=60,
        )
        db.commit()
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Please try again later.",
            )
