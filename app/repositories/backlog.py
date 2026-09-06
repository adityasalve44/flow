"""
app/repositories/backlog.py — Incomplete-candidate backlog view (FLOW-039).

Surfaces pending candidate work without building a queue (Q2 reactive architecture).
A pure query — completeness below threshold, inactive beyond an inactivity window,
not disengaged or blocked — ordered by a simple value score.

Runs on indexed columns (candidates.lifecycle_status, candidate_profiles.completeness,
conversations.last_inbound_at).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.models.candidate import Candidate, CandidateProfile, Conversation
from app.models.enums import (
    ConsentStatusEnum,
    ConversationStatusEnum,
    LifecycleStatusEnum,
)


@dataclass
class BacklogParams:
    """Parameters controlling the backlog view."""

    completeness_threshold: float = 1.0
    inactivity_days: int = 3
    min_completeness: float | None = None
    assigned_recruiter_id: UUID | None = None
    order_by: str = "value_score"  # "value_score", "completeness", "inactive_days"
    limit: int = 25
    offset: int = 0
    now: datetime | None = None


@dataclass
class BacklogRow:
    """A single row from the backlog query."""

    candidate: Candidate
    profile: CandidateProfile | None
    last_inbound_at: datetime | None
    days_inactive: float
    value_score: float


class BacklogRepository:
    """Read-only backlog query over incomplete candidates."""

    def __init__(self, session: Session):
        self.session = session

    def get_backlog(
        self,
        params: BacklogParams,
    ) -> tuple[list[BacklogRow], int]:
        """Return (rows, total_count) for incomplete candidates matching backlog criteria."""
        now = params.now or datetime.now(UTC)
        cutoff = now - timedelta(days=params.inactivity_days)

        # Correlated subquery for candidate's latest inbound or activity timestamp
        latest_conv_activity = (
            select(func.max(func.coalesce(Conversation.last_inbound_at, Conversation.started_at)))
            .where(Conversation.candidate_id == Candidate.id)
            .scalar_subquery()
        )
        latest_activity = func.coalesce(latest_conv_activity, Candidate.created_at)

        # Base statement joining candidate and operational profile
        base = select(
            Candidate,
            CandidateProfile,
            latest_activity.label("last_activity_time"),
        ).outerjoin(CandidateProfile, CandidateProfile.candidate_id == Candidate.id)

        # 1. Inclusion: Lifecycle status must NOT be profile_ready (candidate is incomplete)
        base = base.where(Candidate.lifecycle_status != LifecycleStatusEnum.profile_ready)

        # 2. Exclusions: Blocked candidates are completely excluded
        base = base.where(
            and_(
                Candidate.lifecycle_status != LifecycleStatusEnum.blocked,
                Candidate.blocked_at.is_(None),
            )
        )

        # 3. Exclusions: Unconsented, declined, or withdrawn candidates are excluded
        base = base.where(Candidate.consent_status == ConsentStatusEnum.granted)

        # 4. Exclusions: Disengaged or blocked conversations
        # If candidate has an active disengaged conversation or 3+ deflections, exclude
        disengaged_subquery = exists(
            select(Conversation.id).where(
                Conversation.candidate_id == Candidate.id,
                or_(
                    Conversation.status == ConversationStatusEnum.disengaged,
                    Conversation.status == ConversationStatusEnum.blocked,
                    Conversation.deflection_count >= 3,
                ),
            )
        )
        base = base.where(~disengaged_subquery)

        # 5. Inclusion: Inactivity beyond the configured window
        base = base.where(latest_activity < cutoff)

        # 6. Inclusion: Completeness below threshold
        completeness_col = func.coalesce(CandidateProfile.completeness, 0.0)
        base = base.where(completeness_col < params.completeness_threshold)

        # Optional floor filter
        if params.min_completeness is not None:
            base = base.where(completeness_col >= params.min_completeness)

        # Optional assigned recruiter filter
        if params.assigned_recruiter_id is not None:
            base = base.where(Candidate.assigned_recruiter_id == params.assigned_recruiter_id)

        # Total count query
        count_query = select(func.count()).select_from(
            base.with_only_columns(Candidate.id).subquery()
        )
        total = self.session.scalar(count_query) or 0

        # Ordering runs on indexed columns
        if params.order_by == "completeness":
            stmt = base.order_by(
                CandidateProfile.completeness.desc().nulls_last(),
                latest_activity.desc(),
                Candidate.created_at.desc(),
            )
        elif params.order_by == "inactive_days":
            stmt = base.order_by(
                latest_activity.asc(),
                CandidateProfile.completeness.desc().nulls_last(),
            )
        else:
            # Default "value_score" ordering:
            # Higher completeness first, then more recently active first
            stmt = base.order_by(
                CandidateProfile.completeness.desc().nulls_last(),
                latest_activity.desc(),
                Candidate.created_at.desc(),
            )

        stmt = stmt.limit(params.limit).offset(params.offset)
        raw_rows = self.session.execute(stmt).all()

        results: list[BacklogRow] = []
        for cand, prof, act_time in raw_rows:
            # Calculate days inactive
            if act_time:
                # Ensure tz-aware
                if act_time.tzinfo is None:
                    act_time = act_time.replace(tzinfo=UTC)
                delta_sec = max(0.0, (now - act_time).total_seconds())
                days_inactive = round(delta_sec / 86400.0, 1)
            else:
                days_inactive = float(params.inactivity_days)

            # Value score calculation:
            # 70% completeness + 30% recency factor
            comp = prof.completeness if (prof and prof.completeness is not None) else 0.0
            recency_gap = max(0.0, days_inactive - params.inactivity_days)
            recency_factor = max(0.0, 1.0 - (recency_gap / 30.0))
            score = round(0.70 * comp + 0.30 * recency_factor, 2)

            results.append(
                BacklogRow(
                    candidate=cand,
                    profile=prof,
                    last_inbound_at=act_time,
                    days_inactive=days_inactive,
                    value_score=score,
                )
            )

        # If ordering by value_score, sort the loaded page by the computed score
        if params.order_by == "value_score":
            results.sort(key=lambda r: (r.value_score, r.days_inactive * -1), reverse=True)

        return results, total
