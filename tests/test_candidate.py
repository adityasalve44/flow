"""
tests/test_candidate.py — candidate repository tests.

All tests use the `db` fixture which rolls back after every test,
so the development database is never touched.
"""

from app.repositories.candidate import get_candidate_by_phone
from tests.conftest import CandidateFactory


def test_candidate_lookup(db):
    """A created candidate can be retrieved by phone number."""
    CandidateFactory.build(db, phone_number="+919111111111")
    result = get_candidate_by_phone(db, "+919111111111")
    assert result is not None
    assert result.phone_number == "+919111111111"


def test_candidate_lookup_returns_none_for_unknown(db):
    """Looking up an unknown phone number returns None."""
    result = get_candidate_by_phone(db, "+919000000000")
    assert result is None


def test_candidate_lookup_is_isolated(db):
    """Candidates from one test do not bleed into another fixture call."""
    result = get_candidate_by_phone(db, "+919111111111")
    assert result is None  # not the one from test_candidate_lookup
