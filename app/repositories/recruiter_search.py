"""
app/repositories/recruiter_search.py — candidate search/filter for the recruiter API (FLOW-037).

A dedicated repository rather than a method on CandidateRepository: search
spans candidates, candidate_profiles, and (via EXISTS) candidate_skills /
candidate_location_prefs, which is a cross-cutting read no single entity
repository owns.

Filters are conjunctive (AND) — a filter narrows the result set, it never
widens it. Skill/location filters match if ANY of the candidate's rows
matches ANY of the requested values (a candidate with skills [Python, Go]
matches skills=["Python"]).

No filter here ever reads a personal- or protected-class attribute:
everything queried is either a candidates/candidate_profiles column or one
of the derived preference tables, which by construction only ever hold
operational data (skills/roles/locations are operational keys in the
registry — see app/domain/registry.py).
"""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.models.candidate import Candidate, CandidateProfile
from app.models.enums import LifecycleStatusEnum
from app.models.profile import CandidateLocationPref, CandidateRolePref, CandidateSkill


@dataclass
class CandidateSearchFilters:
    """All fields optional; an unset field does not narrow the result set."""

    role: str | None = None  # substring match against current_role OR desired role
    skills: list[str] = field(default_factory=list)  # any-of, matched on skill_norm
    location: str | None = None  # matched on location_norm
    work_mode: str | None = None
    lifecycle_status: LifecycleStatusEnum | None = None
    min_experience_years: float | None = None
    max_experience_years: float | None = None
    min_expected_ctc: float | None = None
    max_expected_ctc: float | None = None
    max_notice_period_days: int | None = None
    min_completeness: float | None = None


@dataclass
class CandidateSearchRow:
    """One row of a search/list result — candidate + its projection, joined."""

    candidate: Candidate
    profile: CandidateProfile | None


class CandidateSearchRepository:
    """Read-only search over candidates + their operational projection."""

    def __init__(self, session: Session):
        self.session = session

    def search(
        self,
        filters: CandidateSearchFilters,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[CandidateSearchRow], int]:
        """Return (rows, total_count) for the given filters, newest candidate first."""
        base = (
            select(Candidate, CandidateProfile)
            .outerjoin(CandidateProfile, CandidateProfile.candidate_id == Candidate.id)
        )
        base = self._apply_filters(base, filters)

        count_query = select(func.count()).select_from(base.with_only_columns(Candidate.id).subquery())
        total = self.session.scalar(count_query) or 0

        stmt = (
            base.order_by(Candidate.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = self.session.execute(stmt).all()
        return (
            [CandidateSearchRow(candidate=c, profile=p) for c, p in rows],
            total,
        )

    def _apply_filters(self, stmt, filters: CandidateSearchFilters):
        if filters.lifecycle_status is not None:
            stmt = stmt.where(Candidate.lifecycle_status == filters.lifecycle_status)

        if filters.role:
            like = f"%{filters.role.lower()}%"
            desired_role_exists = exists(
                select(CandidateRolePref.id).where(
                    CandidateRolePref.candidate_id == Candidate.id,
                    CandidateRolePref.role_norm.ilike(like.replace(" ", "_")),
                )
            )
            stmt = stmt.where(
                CandidateProfile.current_role.ilike(like) | desired_role_exists
            )

        if filters.skills:
            norms = [self._norm(s) for s in filters.skills]
            stmt = stmt.where(
                exists(
                    select(CandidateSkill.id).where(
                        CandidateSkill.candidate_id == Candidate.id,
                        CandidateSkill.skill_norm.in_(norms),
                    )
                )
            )

        if filters.location:
            norm = self._norm(filters.location)
            stmt = stmt.where(
                exists(
                    select(CandidateLocationPref.id).where(
                        CandidateLocationPref.candidate_id == Candidate.id,
                        CandidateLocationPref.location_norm == norm,
                    )
                )
            )

        if filters.work_mode:
            stmt = stmt.where(CandidateProfile.work_mode == filters.work_mode)

        if filters.min_experience_years is not None:
            stmt = stmt.where(CandidateProfile.experience_years >= filters.min_experience_years)
        if filters.max_experience_years is not None:
            stmt = stmt.where(CandidateProfile.experience_years <= filters.max_experience_years)

        if filters.min_expected_ctc is not None:
            stmt = stmt.where(CandidateProfile.expected_ctc_annual >= filters.min_expected_ctc)
        if filters.max_expected_ctc is not None:
            stmt = stmt.where(CandidateProfile.expected_ctc_annual <= filters.max_expected_ctc)

        if filters.max_notice_period_days is not None:
            stmt = stmt.where(
                CandidateProfile.notice_period_days <= filters.max_notice_period_days
            )

        if filters.min_completeness is not None:
            stmt = stmt.where(CandidateProfile.completeness >= filters.min_completeness)

        return stmt

    @staticmethod
    def _norm(text: str) -> str:
        """Match app.domain.policy._preference_dedup_key exactly, so filter
        values normalise the same way the stored rows do."""
        return "_".join(text.strip().lower().split())

    def get_by_id_with_profile(
        self, candidate_id: UUID | str
    ) -> CandidateSearchRow | None:
        stmt = (
            select(Candidate, CandidateProfile)
            .outerjoin(CandidateProfile, CandidateProfile.candidate_id == Candidate.id)
            .where(Candidate.id == candidate_id)
        )
        row = self.session.execute(stmt).first()
        if row is None:
            return None
        c, p = row
        return CandidateSearchRow(candidate=c, profile=p)
