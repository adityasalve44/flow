"""
app/repositories/recruiter.py — Recruiter and RecruiterNote data access (FLOW-038).

IMPORTANT: No commit() calls here. The service layer (UnitOfWork) owns
all transactions. These functions only read and stage (flush) — they
never commit.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.recruiter import Recruiter, RecruiterNote


class RecruiterRepository:
    """Repository for Recruiter entity."""

    def __init__(self, session: Session):
        self.session = session

    def get_by_id(self, recruiter_id: UUID | str) -> Recruiter | None:
        return self.session.scalar(select(Recruiter).where(Recruiter.id == recruiter_id))

    def get_by_email(self, email: str) -> Recruiter | None:
        return self.session.scalar(select(Recruiter).where(Recruiter.email == email))

    def get_by_api_key_hash(self, api_key_hash: str) -> Recruiter | None:
        """The auth lookup path — see app/api/auth.py:get_current_recruiter."""
        return self.session.scalar(
            select(Recruiter).where(Recruiter.api_key_hash == api_key_hash)
        )

    def list_all(self, active_only: bool = False) -> list[Recruiter]:
        statement = select(Recruiter).order_by(Recruiter.created_at.desc())
        if active_only:
            statement = statement.where(Recruiter.is_active.is_(True))
        return list(self.session.scalars(statement).all())

    def add(self, recruiter: Recruiter) -> Recruiter:
        self.session.add(recruiter)
        self.session.flush()
        return recruiter


class RecruiterNoteRepository:
    """Repository for RecruiterNote entity — never read by any
    candidate-facing code path (see app/models/recruiter.py)."""

    def __init__(self, session: Session):
        self.session = session

    def get_by_candidate(self, candidate_id: UUID | str) -> list[RecruiterNote]:
        statement = (
            select(RecruiterNote)
            .where(RecruiterNote.candidate_id == candidate_id)
            .order_by(RecruiterNote.created_at.desc())
        )
        return list(self.session.scalars(statement).all())

    def add(self, note: RecruiterNote) -> RecruiterNote:
        self.session.add(note)
        self.session.flush()
        return note
