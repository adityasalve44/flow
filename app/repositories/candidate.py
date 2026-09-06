from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Candidate,
    CandidateProfile,
    CandidateSkill,
    CandidateRole,
    CandidateLocation,
)


def get_candidate_by_phone(
    db: Session,
    phone_number: str,
) -> Candidate | None:
    statement = (
        select(Candidate)
        .where(Candidate.phone_number == phone_number)
    )

    return db.scalar(statement)


def create_candidate(
    db: Session,
    phone_number: str,
    name: str | None = None,
) -> Candidate:
    candidate = Candidate(
        phone_number=phone_number,
        name=name,
    )

    db.add(candidate)
    db.flush()

    return candidate


def get_or_create_candidate(
    db: Session,
    phone_number: str,
) -> tuple[Candidate, bool]:

    candidate = get_candidate_by_phone(
        db,
        phone_number,
    )

    if candidate:
        return candidate, False

    candidate = create_candidate(
        db,
        phone_number,
    )

    db.commit()

    return candidate, True
