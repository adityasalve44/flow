from app.database import SessionLocal
from app.models import Candidate
from app.repositories.candidate import get_candidate_by_phone


def test_candidate_lookup():

    phone = "+919999999999"

    with SessionLocal() as db:

        candidate = Candidate(
            phone_number=phone,
            name="Test Candidate",
        )

        db.add(candidate)
        db.commit()

        result = get_candidate_by_phone(
            db,
            phone,
        )

        assert result is not None
        assert result.phone_number == phone
        assert result.name == "Test Candidate"

        db.delete(result)
        db.commit()
