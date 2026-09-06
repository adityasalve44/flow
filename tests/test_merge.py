"""
tests/test_merge.py — exhaustive tests for the merge engine.

Tests:
1. 7x7 precedence matrix (every source pair tested).
2. Four conflict cases from §7 of REVIEW_AND_PLAN.md:
   - Case 1: Same-conversation correction.
   - Case 2: Cross-conversation contradiction.
   - Case 3: Lower-authority source contradicts higher-authority.
   - Case 4: Ambiguous value.
3. Invariant: recruiter_verified data is NEVER superseded by candidate statements.
4. Idempotency of repeated identical facts.
"""

import itertools
import pytest

from app.domain.merge import (
    Fact,
    MergeContext,
    SOURCE_RANKS,
    merge_facts,
)
from app.models.enums import (
    AttributeStatusEnum,
    ConfidenceEnum,
    DataClassEnum,
    SourceEnum,
)

ALL_SOURCES = [
    SourceEnum.channel_metadata.value,
    SourceEnum.llm_inferred.value,
    SourceEnum.system_calculated.value,
    SourceEnum.resume.value,
    SourceEnum.candidate_stated.value,
    SourceEnum.candidate_confirmed.value,
    SourceEnum.recruiter_verified.value,
]


@pytest.mark.parametrize("existing_source, incoming_source", list(itertools.product(ALL_SOURCES, ALL_SOURCES)))
def test_7x7_precedence_matrix(existing_source: str, incoming_source: str):
    """
    Test all 49 source pairs in the 7x7 precedence matrix for conflicting values.
    Verifies that authority rank ordering is strictly and deterministically respected.
    """
    ext_rank = SOURCE_RANKS[existing_source]
    inc_rank = SOURCE_RANKS[incoming_source]

    existing_fact = Fact(
        key="experience_years",
        value=3.0,
        source=existing_source,
        confidence=ConfidenceEnum.confirmed.value,
        data_class=DataClassEnum.operational.value,
        conversation_id="conv_1",
    )

    incoming_fact = Fact(
        key="experience_years",
        value=5.0,  # Contradicts existing 3.0
        source=incoming_source,
        confidence=ConfidenceEnum.confirmed.value,
        data_class=DataClassEnum.operational.value,
        conversation_id="conv_2",  # Different conversation
    )

    result = merge_facts([existing_fact], [incoming_fact], context=MergeContext(conversation_id="conv_2"))

    # Rule: recruiter_verified can only be superseded by another recruiter_verified
    if existing_source == SourceEnum.recruiter_verified.value and incoming_source != SourceEnum.recruiter_verified.value:
        assert incoming_fact in result.conflicted, f"{incoming_source} should not supersede recruiter_verified"
        assert existing_fact not in result.superseded
        return

    if inc_rank > ext_rank:
        # Strictly higher authority wins across conversations
        assert incoming_fact in result.accepted, f"{incoming_source} (rank {inc_rank}) should supersede {existing_source} (rank {ext_rank})"
        assert existing_fact in result.superseded
    elif inc_rank < ext_rank:
        # Lower authority loses
        assert incoming_fact in result.conflicted, f"{incoming_source} (rank {inc_rank}) should conflict against {existing_source} (rank {ext_rank})"
        assert existing_fact not in result.superseded
    else:
        # Same rank across different conversations: candidate_confirmed can supersede, otherwise conflicted
        if incoming_source == SourceEnum.candidate_confirmed.value:
            assert incoming_fact in result.accepted
            assert existing_fact in result.superseded
        else:
            assert incoming_fact in result.conflicted


def test_conflict_case_1_same_conversation_correction():
    """
    Case 1: Correction in the SAME conversation ("actually, 5 years").
    New fact becomes current, old becomes superseded without confirmation.
    """
    conv_id = "conv_current"
    f1 = Fact(
        key="experience_years",
        value=3.0,
        source=SourceEnum.candidate_stated.value,
        confidence=ConfidenceEnum.confirmed.value,
        data_class=DataClassEnum.operational.value,
        conversation_id=conv_id,
    )
    # Correction within the same conversation
    f2 = Fact(
        key="experience_years",
        value=5.0,
        source=SourceEnum.candidate_stated.value,
        confidence=ConfidenceEnum.confirmed.value,
        data_class=DataClassEnum.operational.value,
        conversation_id=conv_id,
    )

    result = merge_facts([f1], [f2], context=MergeContext(conversation_id=conv_id))
    assert f2 in result.accepted
    assert f1 in result.superseded
    assert len(result.conflicted) == 0


def test_conflict_case_2_cross_conversation_contradiction():
    """
    Case 2: Contradiction with a fact from a PREVIOUS conversation.
    New fact stored as conflicted until explicit confirmation.
    """
    f_past = Fact(
        key="experience_years",
        value=4.0,
        source=SourceEnum.candidate_stated.value,
        confidence=ConfidenceEnum.confirmed.value,
        data_class=DataClassEnum.operational.value,
        conversation_id="conv_old",
    )
    f_new = Fact(
        key="experience_years",
        value=5.0,
        source=SourceEnum.candidate_stated.value,
        confidence=ConfidenceEnum.confirmed.value,
        data_class=DataClassEnum.operational.value,
        conversation_id="conv_new",
    )

    result = merge_facts([f_past], [f_new], context=MergeContext(conversation_id="conv_new"))
    assert f_new in result.conflicted
    assert f_past not in result.superseded


def test_conflict_case_3_lower_authority_contradicts_higher():
    """
    Case 3: Lower-authority source contradicts a higher one.
    Stored as conflicted, never promoted.
    """
    f_high = Fact(
        key="current_company",
        value="Google",
        source=SourceEnum.recruiter_verified.value,
        confidence=ConfidenceEnum.confirmed.value,
        data_class=DataClassEnum.operational.value,
    )
    f_low = Fact(
        key="current_company",
        value="Startup Inc",
        source=SourceEnum.candidate_stated.value,
        confidence=ConfidenceEnum.confirmed.value,
        data_class=DataClassEnum.operational.value,
    )

    result = merge_facts([f_high], [f_low])
    assert f_low in result.conflicted
    assert f_high not in result.superseded


def test_conflict_case_4_ambiguous_value():
    """
    Case 4: Ambiguous value ("about 80k a month").
    Stored with confidence=ambiguous, raw_text preserved, placed in ambiguous list.
    """
    ambig_fact = Fact(
        key="current_ctc",
        value={"amount": 80000, "period": "monthly", "basis": "unknown"},
        source=SourceEnum.candidate_stated.value,
        confidence=ConfidenceEnum.ambiguous.value,
        data_class=DataClassEnum.operational.value,
        raw_text="I make about 80k a month",
    )

    result = merge_facts([], [ambig_fact])
    assert ambig_fact in result.ambiguous
    assert ambig_fact in result.accepted


def test_recruiter_verified_immune_to_candidate_statements():
    """
    Acceptance criteria: A candidate statement NEVER supersedes recruiter-verified data.
    """
    rv_fact = Fact(
        key="expected_ctc",
        value={"amount": 2500000, "currency": "INR"},
        source=SourceEnum.recruiter_verified.value,
        confidence=ConfidenceEnum.confirmed.value,
        data_class=DataClassEnum.operational.value,
        conversation_id="conv_1",
    )
    cand_fact = Fact(
        key="expected_ctc",
        value={"amount": 3500000, "currency": "INR"},
        source=SourceEnum.candidate_stated.value,
        confidence=ConfidenceEnum.confirmed.value,
        data_class=DataClassEnum.operational.value,
        conversation_id="conv_1",  # Even in the same conversation!
    )

    result = merge_facts([rv_fact], [cand_fact], context=MergeContext(conversation_id="conv_1"))
    assert cand_fact in result.conflicted
    assert rv_fact not in result.superseded


def test_idempotency_of_repeated_identical_facts():
    """
    Repeated identical facts must not trigger conflict or supersession.
    """
    fact = Fact(
        key="skills",
        value=["python", "fastapi"],
        source=SourceEnum.candidate_stated.value,
        confidence=ConfidenceEnum.confirmed.value,
        data_class=DataClassEnum.operational.value,
    )
    same_fact = Fact(
        key="skills",
        value=["python", "fastapi"],
        source=SourceEnum.candidate_stated.value,
        confidence=ConfidenceEnum.confirmed.value,
        data_class=DataClassEnum.operational.value,
    )

    result = merge_facts([fact], [same_fact])
    assert len(result.conflicted) == 0
    assert len(result.superseded) == 0
    assert fact in result.accepted or same_fact in result.accepted
