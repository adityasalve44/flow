"""
app/domain/staleness.py — Profile staleness and refresh mode domain engine (FLOW-030).

Core requirements (§3, §8, FLOW-030 of REVIEW_AND_PLAN.md):
- A year later, ask what is true now.
- In mode=refresh, mark current attributes stale (never delete anything).
- Stale facts remain fully queryable and are restored to current on reconfirmation
  rather than rewritten or duplicated.
- A 400-day-old candidate is greeted as returning, not re-onboarded from zero.
- No old preference is asserted as current; acknowledge_profile_ready is suppressed.
- Opens by asking what they are doing and looking for now.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.db.uow import UnitOfWork
from app.domain.projection import rebuild_projection
from app.models import Candidate, CandidateAttribute, CandidateProfile
from app.models.enums import (
    AttributeStatusEnum,
    ConfidenceEnum,
    LifecycleStatusEnum,
    SourceEnum,
)


def is_candidate_profile_stale(
    profile: CandidateProfile | None,
    now: datetime | None = None,
    stale_days: int | None = None,
) -> bool:
    """Check if candidate profile has exceeded the staleness threshold (e.g. 365 days)."""
    if profile is None or profile.last_refreshed_at is None:
        return False
    settings = get_settings()
    days = stale_days if stale_days is not None else settings.stale_profile_days
    current_time = now or datetime.now(UTC)
    return (current_time - profile.last_refreshed_at) > timedelta(days=days)


def mark_candidate_profile_stale(
    uow: UnitOfWork,
    candidate: Candidate,
    now: datetime | None = None,
) -> int:
    """Mark all current attributes as stale, preserving history and audit trails.

    Invariants (§8, FLOW-030):
    - Never deletes anything.
    - Status transitions from current -> stale.
    - Lifecycle status moves from profile_ready -> intake (FLOW-047).
    - Clears current projection assertions so old preferences are not asserted as current.
    """
    current_time = now or datetime.now(UTC)
    current_attrs = uow.attributes.get_current_for_candidate(candidate.id)

    stale_count = 0
    for attr in current_attrs:
        attr.status = AttributeStatusEnum.stale
        attr.updated_at = current_time
        uow.attributes.add(attr)
        stale_count += 1

    # Demote lifecycle status to intake since stale profile lacks verified current readiness
    if candidate.lifecycle_status == LifecycleStatusEnum.profile_ready:
        from app.domain.lifecycle import transition_candidate_lifecycle
        transition_candidate_lifecycle(
            candidate=candidate,
            target_status=LifecycleStatusEnum.intake,
            reason="refresh_mode_stale_facts",
            uow=uow,
            now=current_time,
        )
        uow.candidates.add(candidate)

    if candidate.profile is not None:
        candidate.profile.last_refreshed_at = current_time
        candidate.profile.completeness = 0.0
        # Clear projected attributes
        snapshot = rebuild_projection([])
        candidate.profile.experience_years = snapshot.experience_years
        candidate.profile.current_ctc_annual = snapshot.current_ctc_annual
        candidate.profile.expected_ctc_annual = snapshot.expected_ctc_annual
        candidate.profile.currency = snapshot.currency
        candidate.profile.notice_period_days = snapshot.notice_period_days
        uow.profiles.add(candidate.profile)

    uow.commit()

    return stale_count


def get_stale_attributes(
    uow: UnitOfWork,
    candidate_id: UUID | str,
) -> list[CandidateAttribute]:
    """Return all attributes currently in stale status for a candidate."""
    from sqlalchemy import select
    stmt = (
        select(CandidateAttribute)
        .where(
            CandidateAttribute.candidate_id == candidate_id,
            CandidateAttribute.status == AttributeStatusEnum.stale,
        )
        .order_by(CandidateAttribute.created_at.asc())
    )
    return list(uow.session.scalars(stmt).all())


def reconfirm_stale_attribute(
    uow: UnitOfWork,
    candidate_id: UUID | str,
    key: str,
    confirmed_value: Any = None,
    now: datetime | None = None,
) -> CandidateAttribute | None:
    """Restore a stale attribute to current upon candidate reconfirmation.

    Invariants (§8, FLOW-030):
    - Restores existing row to status=current rather than writing a duplicate.
    - Updates confirmed_at and updated_at timestamps.
    - If a new/updated value is supplied, updates the value while retaining record continuity.
    - Triggers projection rebuild.
    """
    current_time = now or datetime.now(UTC)
    stale_attrs = get_stale_attributes(uow, candidate_id)
    matching = next((a for a in stale_attrs if a.key == key), None)

    if matching is None:
        return None

    matching.status = AttributeStatusEnum.current
    matching.confidence = ConfidenceEnum.confirmed
    matching.source = SourceEnum.candidate_stated
    matching.confirmed_at = current_time
    matching.updated_at = current_time

    if confirmed_value is not None:
        matching.value = confirmed_value

    uow.attributes.add(matching)
    uow.session.flush()

    current_attrs = uow.attributes.get_current_for_candidate(candidate_id)
    snapshot = rebuild_projection(current_attrs)
    profile = uow.profiles.get_by_candidate_id(candidate_id)
    if profile is not None:
        profile.experience_years = snapshot.experience_years
        profile.current_ctc_annual = snapshot.current_ctc_annual
        profile.expected_ctc_annual = snapshot.expected_ctc_annual
        profile.currency = snapshot.currency
        profile.notice_period_days = snapshot.notice_period_days
        uow.profiles.add(profile)

    uow.commit()
    return matching
