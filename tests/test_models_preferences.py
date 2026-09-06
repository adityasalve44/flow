"""
tests/test_models_preferences.py — preference table tests.

Tests that:
- Candidate location preferences support preferred/acceptable/excluded distinctions
- Skills are deduped per candidate by skill_norm (unique constraint)
- Role preferences support current/desired kinds
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import CandidateLocationPref, CandidateRolePref, CandidateSkill
from tests.conftest import CandidateFactory


def test_location_pref_preferred_acceptable_excluded(db):
    """Preferred, acceptable and excluded locations are all representable."""
    candidate = CandidateFactory.build(db)

    prefs = []
    for strength in ["preferred", "acceptable", "excluded"]:
        pref = CandidateLocationPref(
            candidate_id=candidate.id,
            location_raw=f"Pune ({strength})",
            location_norm="pune",
            strength=strength,
        )
        db.add(pref)
        prefs.append(pref)

    db.flush()
    assert len(prefs) == 3
    strengths = {p.strength for p in prefs}
    assert strengths == {"preferred", "acceptable", "excluded"}


def test_location_pref_hard_requirement(db):
    """is_hard_requirement distinguishes hard no from soft exclusions."""
    candidate = CandidateFactory.build(db)
    pref = CandidateLocationPref(
        candidate_id=candidate.id,
        location_raw="Only remote",
        location_norm="remote",
        strength="preferred",
        is_hard_requirement=True,
    )
    db.add(pref)
    db.flush()
    assert pref.is_hard_requirement is True


def test_skill_deduplication(db):
    """Two skills with the same norm form for the same candidate are rejected."""
    candidate = CandidateFactory.build(db)
    s1 = CandidateSkill(
        candidate_id=candidate.id,
        skill_raw="Python",
        skill_norm="python",
    )
    s2 = CandidateSkill(
        candidate_id=candidate.id,
        skill_raw="Python3",  # different raw, same norm
        skill_norm="python",
    )
    db.add(s1)
    db.flush()
    db.add(s2)
    with pytest.raises(IntegrityError):
        db.flush()


def test_role_pref_current_and_desired(db):
    """Current and desired role preferences are both representable."""
    candidate = CandidateFactory.build(db)
    for kind in ["current", "desired"]:
        pref = CandidateRolePref(
            candidate_id=candidate.id,
            role_raw=f"Backend Developer ({kind})",
            role_norm="backend_developer",
            kind=kind,
        )
        db.add(pref)
    db.flush()
