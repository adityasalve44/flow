"""
tests/test_next_question.py — Next-question scoring and policy ordering tests (FLOW-025, §8).

Tests:
1. Dynamic ordering:
   - Asking order differs between two candidates who volunteer different initial facts.
   - A candidate volunteering six facts is never asked about any of them.
2. Hard caps:
   - At most 2 asks per message.
   - Maximum 3 lifetime asks per field on the conversation.
3. Adjacency:
   - Adds a second ask ONLY if it is topically adjacent to the top-scoring ask.
4. Refusal suppression:
   - Declined fields are heavily suppressed in favor of other unasked fields.
5. Recency suppression:
   - Recently asked fields (last 2 turns) are deprioritized to avoid immediate repetition.
"""

from uuid import uuid4

import pytest

from app.domain.merge import Fact
from app.domain.policy import ADJACENCY_MAP, score_missing_fields
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


def test_candidate_volunteering_six_facts_is_never_asked():
    """
    Acceptance test (FLOW-025):
    A candidate volunteering six facts is never asked about any of them.
    score_missing_fields returns empty list.
    """
    facts = [
        make_fact("desired_role", "Frontend Engineer"),
        make_fact("experience_years", 4.0),
        make_fact("skills", ["React", "TypeScript"]),
        make_fact("location_preference", ["Delhi NCR"]),
        make_fact("expected_ctc", 2400000.0),
        make_fact("notice_period", 30),
    ]

    to_ask = score_missing_fields(facts)
    assert to_ask == []


def test_asking_order_differs_based_on_volunteered_facts():
    """
    Acceptance test (FLOW-025):
    Asking order differs between two candidates who volunteer different initial things.
    """
    # Candidate A volunteered desired_role:
    cand_a_facts = [make_fact("desired_role", "Backend Developer")]
    to_ask_a = score_missing_fields(cand_a_facts)

    # Candidate B volunteered location_preference:
    cand_b_facts = [make_fact("location_preference", ["Mumbai"])]
    to_ask_b = score_missing_fields(cand_b_facts)

    assert to_ask_a != to_ask_b
    assert "desired_role" not in to_ask_a
    assert "location_preference" not in to_ask_b


def test_hard_cap_two_asks_per_message():
    """No turn ever solicits more than 2 fields."""
    # Empty profile (all 6 blocking fields missing)
    to_ask = score_missing_fields([])
    assert 1 <= len(to_ask) <= 2


def test_adjacency_map_pairing():
    """
    Acceptance test (FLOW-025, §8):
    A second field is added ONLY if it is topically adjacent to the primary ask.
    """
    # 1. Role is top ask; skills is adjacent -> pairs role + skills
    facts_no_skills_no_role = [
        make_fact("expected_ctc", 3000000.0),
        make_fact("notice_period", 15),
        make_fact("location_preference", ["Remote"]),
        make_fact("experience_years", 5.0),
    ]
    # missing: desired_role and skills
    to_ask = score_missing_fields(facts_no_skills_no_role)
    assert set(to_ask) == {"desired_role", "skills"}

    # 2. Role is missing, but skills is ALREADY known -> only non-adjacent remaining fields
    facts_skills_known = [
        make_fact("skills", ["Python", "FastAPI"]),
        make_fact("expected_ctc", 3000000.0),
        make_fact("notice_period", 15),
        make_fact("location_preference", ["Remote"]),
        # missing: desired_role and experience_years
    ]
    to_ask_non_adjacent = score_missing_fields(facts_skills_known)
    # experience_years is NOT adjacent to desired_role, so only 1 field is asked
    assert to_ask_non_adjacent == ["desired_role"]


def test_refusal_penalty_suppression():
    """
    Acceptance test (FLOW-025):
    Declined fields are heavily suppressed in favor of other unasked fields.
    """
    # Expected CTC is declined
    declined = {"expected_ctc"}

    # Notice period and expected CTC are both missing (both weight 0.9)
    facts = [
        make_fact("desired_role", "DevOps"),
        make_fact("experience_years", 3.0),
        make_fact("skills", ["Terraform"]),
        make_fact("location_preference", ["Bengaluru"]),
    ]

    to_ask = score_missing_fields(facts, declined_keys=declined)

    # Notice period should be preferred over declined expected_ctc
    assert "notice_period" in to_ask
    assert "expected_ctc" not in to_ask


def test_recency_penalty_suppression():
    """Recently asked fields are suppressed to prevent immediate repetition."""
    # desired_role was asked in the last turn
    recently_asked = ["desired_role"]

    facts = [
        make_fact("skills", ["Node.js"]),
        make_fact("expected_ctc", 1800000.0),
    ]

    to_ask = score_missing_fields(facts, recently_asked=recently_asked)

    # Must NOT ask desired_role immediately again when other fields are missing
    assert to_ask[0] != "desired_role"
    assert to_ask[0] in ("experience_years", "location_preference")


def test_lifetime_ask_cap_three():
    """
    Acceptance test (FLOW-025):
    Hard cap of three lifetime asks per field on the conversation.
    Once a field has been asked 3 times, it is never asked again.
    """
    # desired_role has been asked 3 times
    ask_counts = {"desired_role": 3}

    # Only desired_role and notice_period are missing
    facts = [
        make_fact("experience_years", 5.0),
        make_fact("skills", ["C++"]),
        make_fact("location_preference", ["Pune"]),
        make_fact("expected_ctc", 3500000.0),
    ]

    to_ask = score_missing_fields(facts, ask_counts=ask_counts)

    # desired_role is capped at 3; notice_period must be asked instead
    assert "desired_role" not in to_ask
    assert to_ask == ["notice_period"]

    # When all missing fields are capped at 3 asks, returns empty list
    all_capped = {"desired_role": 3, "notice_period": 3}
    to_ask_capped = score_missing_fields(facts, ask_counts=all_capped)
    assert to_ask_capped == []
