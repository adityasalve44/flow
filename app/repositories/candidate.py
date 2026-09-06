"""
app/repositories/candidate.py — candidate data access.

IMPORTANT: No commit() calls here. The service layer (UnitOfWork) owns
all transactions. These functions only read and stage (flush) — they
never commit.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Candidate


class CandidateRepository:
    """Repository for Candidate entity."""

    def __init__(self, session: Session):
        self.session = session

    def get_by_id(self, candidate_id: UUID | str) -> Candidate | None:
        """Fetch a candidate by UUID primary key."""
        statement = select(Candidate).where(Candidate.id == candidate_id)
        return self.session.scalar(statement)

    def get_by_phone(self, phone_number: str) -> Candidate | None:
        """Fetch a candidate by E.164 phone number."""
        statement = select(Candidate).where(Candidate.phone_number == phone_number)
        return self.session.scalar(statement)

    def get_or_create_by_phone(
        self,
        phone_number: str,
        display_name: str | None = None,
    ) -> Candidate:
        """
        Atomically get or create a candidate by phone number.

        Uses INSERT ... ON CONFLICT (phone_number) DO NOTHING followed by a re-select,
        closing the concurrent-insert race condition (Q1, FLOW-012).
        """
        insert_stmt = (
            insert(Candidate)
            .values(phone_number=phone_number, display_name=display_name)
            .on_conflict_do_nothing(index_elements=["phone_number"])
        )
        self.session.execute(insert_stmt)
        self.session.flush()

        # Re-select the candidate (will find existing or newly inserted row)
        candidate = self.session.scalars(
            select(Candidate).where(Candidate.phone_number == phone_number)
        ).one()
        return candidate

    def add(self, candidate: Candidate) -> Candidate:
        """Stage a candidate for insertion."""
        self.session.add(candidate)
        self.session.flush()
        return candidate


# Backward-compatible functional interface
def get_candidate_by_phone(db: Session, phone_number: str) -> Candidate | None:
    return CandidateRepository(db).get_by_phone(phone_number)


def get_candidate_by_id(db: Session, candidate_id: UUID | str) -> Candidate | None:
    return CandidateRepository(db).get_by_id(candidate_id)
