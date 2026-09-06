from app.database import SessionLocal
from app.repositories.candidate import (
    get_candidate_by_phone,
    get_or_create_candidate,
)


def lookup_candidate(phone_number: str) -> dict:
    """
    Look up a candidate using their phone number.

    The phone number comes from Flow's trusted application context.
    """

    with SessionLocal() as db:

        candidate = get_candidate_by_phone(
            db,
            phone_number,
        )

        if candidate is None:
            return {
                "exists": False,
                "phone_number": phone_number,
            }

        return {
            "exists": True,
            "candidate_id": candidate.id,
            "phone_number": candidate.phone_number,
            "name": candidate.name,
        }


def ensure_candidate(phone_number: str) -> dict:
    """
    Create a candidate if they do not already exist.
    """

    with SessionLocal() as db:

        candidate, created = get_or_create_candidate(
            db,
            phone_number,
        )

        return {
            "candidate_id": candidate.id,
            "phone_number": candidate.phone_number,
            "created": created,
        }
