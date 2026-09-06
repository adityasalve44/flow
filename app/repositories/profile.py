"""
app/repositories/profile.py — candidate profile projection data access.

IMPORTANT: No commit() calls here. The service layer (UnitOfWork) owns
all transactions. These functions only read and stage (flush) — they
never commit.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CandidateProfile


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

    def add(self, profile: CandidateProfile) -> CandidateProfile:
        """Stage a profile for insertion."""
        self.session.add(profile)
        self.session.flush()
        return profile

    save = add
