"""
app/repositories/candidate.py — read-only candidate data access.

IMPORTANT: No commit() calls here. The service layer (UnitOfWork) owns
all transactions.  These functions only read and stage (flush) — they
never commit.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Candidate


def get_candidate_by_phone(
    db: Session,
    phone_number: str,
) -> Candidate | None:
    statement = select(Candidate).where(Candidate.phone_number == phone_number)
    return db.scalar(statement)


def get_candidate_by_id(
    db: Session,
    candidate_id,
) -> Candidate | None:
    statement = select(Candidate).where(Candidate.id == candidate_id)
    return db.scalar(statement)
