"""
app/repositories/profile.py — candidate profile projection data access.

IMPORTANT: No commit() calls here. The service layer (UnitOfWork) owns
all transactions. These functions only read and stage (flush) — they
never commit.

SkillRepository, RolePrefRepository and LocationPrefRepository (FLOW-037)
back the candidate_skills / candidate_role_prefs / candidate_location_prefs
tables. These are derived projections exactly like CandidateProfile — never
written by hand, only reconciled from ProfileSnapshot by
app.domain.policy.sync_preference_tables() on every turn. replace_all()
deletes and re-inserts rather than diffing in place: the source of truth is
always the current attribute set, so there is nothing worth preserving in
an old row once it no longer matches the snapshot.
"""

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import CandidateProfile
from app.models.profile import CandidateLocationPref, CandidateRolePref, CandidateSkill


class ProfileRepository:
    """Repository for CandidateProfile derived projection."""

    def __init__(self, session: Session):
        self.session = session

    def get_by_candidate_id(self, candidate_id: UUID | str) -> CandidateProfile | None:
        """Fetch candidate profile projection by candidate ID."""
        statement = select(CandidateProfile).where(
            CandidateProfile.candidate_id == candidate_id
        )
        return self.session.scalar(statement)

    def get_or_create(self, candidate_id: UUID | str) -> CandidateProfile:
        """Fetch or initialize a candidate profile projection."""
        profile = self.get_by_candidate_id(candidate_id)
        if profile is None:
            profile = CandidateProfile(candidate_id=candidate_id)
            self.session.add(profile)
            self.session.flush()
        return profile

    def add(self, profile: CandidateProfile) -> CandidateProfile:
        """Stage a profile for insertion."""
        self.session.add(profile)
        self.session.flush()
        return profile

    save = add


class SkillRepository:
    """Repository for CandidateSkill (derived projection, FLOW-037)."""

    def __init__(self, session: Session):
        self.session = session

    def get_by_candidate(self, candidate_id: UUID | str) -> list[CandidateSkill]:
        statement = select(CandidateSkill).where(
            CandidateSkill.candidate_id == candidate_id
        ).order_by(CandidateSkill.skill_norm)
        return list(self.session.scalars(statement).all())

    def replace_all(
        self, candidate_id: UUID | str, skills: list[dict]
    ) -> list[CandidateSkill]:
        """Replace every skill row for a candidate with the given set.

        Each dict: {"skill_raw": str, "skill_norm": str}.
        """
        self.session.execute(
            delete(CandidateSkill).where(CandidateSkill.candidate_id == candidate_id)
        )
        rows = [
            CandidateSkill(
                candidate_id=candidate_id,
                skill_raw=s["skill_raw"],
                skill_norm=s["skill_norm"],
            )
            for s in skills
        ]
        self.session.add_all(rows)
        self.session.flush()
        return rows


class RolePrefRepository:
    """Repository for CandidateRolePref (derived projection, FLOW-037)."""

    def __init__(self, session: Session):
        self.session = session

    def get_by_candidate(self, candidate_id: UUID | str) -> list[CandidateRolePref]:
        statement = select(CandidateRolePref).where(
            CandidateRolePref.candidate_id == candidate_id
        ).order_by(CandidateRolePref.role_norm)
        return list(self.session.scalars(statement).all())

    def replace_all(
        self, candidate_id: UUID | str, roles: list[dict], kind: str = "desired"
    ) -> list[CandidateRolePref]:
        """Replace every role-preference row of the given ``kind`` for a candidate.

        Each dict: {"role_raw": str, "role_norm": str}. Only rows matching
        ``kind`` are replaced, so "current" and "desired" rows never collide.
        """
        self.session.execute(
            delete(CandidateRolePref).where(
                CandidateRolePref.candidate_id == candidate_id,
                CandidateRolePref.kind == kind,
            )
        )
        rows = [
            CandidateRolePref(
                candidate_id=candidate_id,
                role_raw=r["role_raw"],
                role_norm=r["role_norm"],
                kind=kind,
            )
            for r in roles
        ]
        self.session.add_all(rows)
        self.session.flush()
        return rows


class LocationPrefRepository:
    """Repository for CandidateLocationPref (derived projection, FLOW-037)."""

    def __init__(self, session: Session):
        self.session = session

    def get_by_candidate(self, candidate_id: UUID | str) -> list[CandidateLocationPref]:
        statement = select(CandidateLocationPref).where(
            CandidateLocationPref.candidate_id == candidate_id
        ).order_by(CandidateLocationPref.location_norm)
        return list(self.session.scalars(statement).all())

    def replace_all(
        self, candidate_id: UUID | str, locations: list[dict]
    ) -> list[CandidateLocationPref]:
        """Replace every location-preference row for a candidate.

        Each dict: {"location_raw": str, "location_norm": str,
        "strength": str}. Strength defaults to "preferred" everywhere it is
        set by the caller — the extraction layer does not yet distinguish
        preferred / acceptable / hard-requirement (a known gap, tracked
        against the extractor, not this repository).
        """
        self.session.execute(
            delete(CandidateLocationPref).where(
                CandidateLocationPref.candidate_id == candidate_id
            )
        )
        rows = [
            CandidateLocationPref(
                candidate_id=candidate_id,
                location_raw=loc["location_raw"],
                location_norm=loc["location_norm"],
                strength=loc.get("strength", "preferred"),
            )
            for loc in locations
        ]
        self.session.add_all(rows)
        self.session.flush()
        return rows
