"""
tests/test_completeness.py — Profile readiness and completeness tests (FLOW-025, Q6).

Tests:
1. Readiness with all six baseline fields present -> True.
2. Readiness despite every non-blocking field being absent (no name, no current CTC, no education) -> True.
3. Readiness with each blocking field individually absent -> False.
4. Readiness with a blocking field ambiguous or stale -> False.
5. Works uniformly across dict snapshots, ProfileSnapshot instances, and Fact lists.
"""

from uuid import uuid4

import pytest

from app.domain.completeness import (
    calculate_completeness,
    get_missing_blocking_fields,
    is_profile_ready,
)
from app.domain.merge import Fact
from app.domain.projection import ProfileSnapshot
from app.domain.registry import BLOCKING_KEYS


def make_fact(key: str, value: any, status: str = "current", confidence: str = "confirmed") -> Fact:
    return Fact(
        id=uuid4(),
        candidate_id=uuid4(),
        key=key,
        value=value,
        raw_text=str(value),
        source="candidate_stated",
        confidence=confidence,
        status=status,
        data_class="operational",
        conversation_id=uuid4(),
    )


def test_profile_ready_with_all_six_blocking_fields():
    """All six blocking fields confirmed and current makes candidate profile ready."""
    facts = [
        make_fact("desired_role", "Backend Engineer"),
        make_fact("experience_years", 5.0),
        make_fact("skills", ["Python", "FastAPI"]),
        make_fact("location_preference", ["Bengaluru"]),
        make_fact("expected_ctc", 3000000.0),
        make_fact("notice_period", 30),
    ]
    assert is_profile_ready(facts) is True
    assert len(get_missing_blocking_fields(facts)) == 0


def test_profile_ready_despite_every_non_blocking_field_absent():
    """
    Acceptance test (FLOW-025, Q6):
    A candidate with the six baseline fields and no name, current CTC or education is profile_ready.
    Flow stops interrogating and says so.
    """
    baseline_snapshot = {
        "desired_role": "Data Scientist",
        "experience_years": 3.5,
        "skills": ["Python", "PyTorch"],
        "location_preference": ["Remote"],
        "expected_ctc": 2800000.0,
        "notice_period": 15,
        # Explicitly absent non-blocking fields:
        "full_name": None,
        "current_ctc": None,
        "current_company": None,
        "current_role": None,
        "education": None,
        "work_mode": None,
    }
    assert is_profile_ready(baseline_snapshot) is True


@pytest.mark.parametrize("missing_field", sorted(BLOCKING_KEYS))
def test_readiness_with_each_blocking_field_individually_absent(missing_field):
    """
    Acceptance test (FLOW-025):
    If any single blocking field is absent, is_profile_ready is False.
    """
    all_six = {
        "desired_role": "Backend Engineer",
        "experience_years": 4.0,
        "skills": ["Java", "Spring"],
        "location_preference": ["Pune"],
        "expected_ctc": 2000000.0,
        "notice_period": 60,
    }
    # Remove one field
    del all_six[missing_field]

    assert is_profile_ready(all_six) is False

    missing = get_missing_blocking_fields([
        make_fact(k, v) for k, v in all_six.items()
    ])
    assert missing == [missing_field]


def test_readiness_fails_if_blocking_field_is_ambiguous():
    """A blocking field that is ambiguous is NOT confirmed, so profile is not ready."""
    facts = [
        make_fact("desired_role", "Backend Engineer"),
        make_fact("experience_years", 5.0),
        make_fact("skills", ["Python"]),
        make_fact("location_preference", ["Bengaluru"]),
        make_fact("expected_ctc", 3000000.0),
        # Notice period is ambiguous (e.g. 'maybe 1 or 2 months')
        make_fact("notice_period", 30, confidence="ambiguous"),
    ]
    assert is_profile_ready(facts) is False


def test_readiness_fails_if_blocking_field_is_stale():
    """A blocking field that is stale is not current, so profile is not ready."""
    facts = [
        make_fact("desired_role", "Backend Engineer"),
        # Experience is stale
        make_fact("experience_years", 3.0, status="stale"),
        make_fact("skills", ["Python"]),
        make_fact("location_preference", ["Bengaluru"]),
        make_fact("expected_ctc", 3000000.0),
        make_fact("notice_period", 30),
    ]
    assert is_profile_ready(facts) is False


def test_profile_snapshot_object_readiness():
    """ProfileSnapshot object readiness evaluation."""
    snapshot = ProfileSnapshot(
        desired_roles=["SRE"],
        experience_years=6.0,
        skills=["Kubernetes", "Go"],
        locations=["Hyderabad"],
        expected_ctc_annual=4000000.0,
        notice_period_days=30,
    )
    assert is_profile_ready(snapshot) is True


def test_calculate_completeness_weighted_scoring():
    """Completeness score reflects weighted proportion of confirmed attributes."""
    # Empty facts
    assert calculate_completeness([]) == 0.0

    # 3 blocking fields confirmed (3.0 weight)
    facts = [
        make_fact("desired_role", "Engineer"),
        make_fact("experience_years", 2.0),
        make_fact("skills", ["React"]),
    ]
    score = calculate_completeness(facts)
    assert 0.3 < score < 0.6
