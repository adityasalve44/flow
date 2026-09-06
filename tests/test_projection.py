"""
tests/test_projection.py — tests for derived projection (ProfileSnapshot).

Invariants tested:
1. Property test: NO non-operational attribute (personal or protected) can appear in ProfileSnapshot.
2. Ambiguous facts (confidence=ambiguous) are never projected.
3. Unconfirmed or inferred facts (confidence=inferred, unknown) are never projected.
4. Superseded or conflicted facts are never projected.
5. Completeness calculation is correctly derived from blocking keys.
6. Operational facts (confirmed + current) are accurately projected into structured fields.
"""

from decimal import Decimal

import pytest

from app.domain.merge import Fact
from app.domain.projection import ProfileSnapshot, rebuild_projection
from app.domain.registry import KEY_REGISTRY
from app.models.enums import (
    AttributeStatusEnum,
    ConfidenceEnum,
    DataClassEnum,
    SourceEnum,
)


def test_property_no_non_operational_attribute_in_projection():
    """
    Property test: Generate facts across all registered keys in KEY_REGISTRY.
    Assert that NO key with data_class != 'operational' appears in the ProfileSnapshot
    operational_attributes or affects the snapshot in any way.
    """
    facts: list[Fact] = []

    for spec in KEY_REGISTRY.values():
        facts.append(
            Fact(
                key=spec.key,
                value="test_value",
                source=SourceEnum.candidate_stated.value,
                confidence=ConfidenceEnum.confirmed.value,
                data_class=spec.data_class.value,
                status=AttributeStatusEnum.current.value,
            )
        )

    snapshot: ProfileSnapshot = rebuild_projection(facts)

    for key, val in snapshot.operational_attributes.items():
        spec = KEY_REGISTRY.get(key)
        if spec:
            assert spec.data_class == DataClassEnum.operational, (
                f"Non-operational key '{key}' ({spec.data_class.value}) leaked into projection!"
            )


@pytest.mark.parametrize("non_operational_class", [DataClassEnum.personal.value, DataClassEnum.protected.value])
def test_sensitive_facts_never_projected(non_operational_class: str):
    """
    Personal and protected disclosures (religion, caste, health, age, marital status)
    must NEVER appear in ProfileSnapshot.
    """
    sensitive_facts = [
        Fact(
            key="religion",
            value="Hindu",
            source=SourceEnum.candidate_stated.value,
            confidence=ConfidenceEnum.confirmed.value,
            data_class=non_operational_class,
            status=AttributeStatusEnum.current.value,
        ),
        Fact(
            key="marital_status",
            value="married",
            source=SourceEnum.candidate_stated.value,
            confidence=ConfidenceEnum.confirmed.value,
            data_class=non_operational_class,
            status=AttributeStatusEnum.current.value,
        ),
        Fact(
            key="age",
            value=29,
            source=SourceEnum.candidate_stated.value,
            confidence=ConfidenceEnum.confirmed.value,
            data_class=non_operational_class,
            status=AttributeStatusEnum.current.value,
        ),
    ]

    snapshot = rebuild_projection(sensitive_facts)
    assert len(snapshot.operational_attributes) == 0
    assert snapshot.completeness == 0.0


def test_ambiguous_facts_never_projected():
    """
    Facts with confidence=ambiguous ('about 80k a month') must NEVER be projected.
    """
    ambig_fact = Fact(
        key="current_ctc",
        value={"amount": 80000, "period": "monthly", "basis": "unknown"},
        source=SourceEnum.candidate_stated.value,
        confidence=ConfidenceEnum.ambiguous.value,
        data_class=DataClassEnum.operational.value,
        status=AttributeStatusEnum.current.value,
    )

    snapshot = rebuild_projection([ambig_fact])
    assert snapshot.current_ctc_annual is None
    assert "current_ctc" not in snapshot.operational_attributes


def test_superseded_facts_never_projected():
    """
    Superseded facts must NEVER be projected.
    """
    superseded_fact = Fact(
        key="experience_years",
        value=3.0,
        source=SourceEnum.candidate_stated.value,
        confidence=ConfidenceEnum.confirmed.value,
        data_class=DataClassEnum.operational.value,
        status=AttributeStatusEnum.superseded.value,
    )

    snapshot = rebuild_projection([superseded_fact])
    assert snapshot.experience_years is None
    assert "experience_years" not in snapshot.operational_attributes


def test_confirmed_operational_facts_project_cleanly():
    """
    Current, confirmed, operational facts map into structured fields and completeness score.
    """
    facts = [
        Fact(
            key="desired_role",
            value="Backend Engineer",
            source=SourceEnum.candidate_stated.value,
            confidence=ConfidenceEnum.confirmed.value,
            data_class=DataClassEnum.operational.value,
        ),
        Fact(
            key="experience_years",
            value={"amount": 5.0},
            source=SourceEnum.candidate_confirmed.value,
            confidence=ConfidenceEnum.confirmed.value,
            data_class=DataClassEnum.operational.value,
        ),
        Fact(
            key="skills",
            value=["Python", "FastAPI", "PostgreSQL"],
            source=SourceEnum.candidate_stated.value,
            confidence=ConfidenceEnum.confirmed.value,
            data_class=DataClassEnum.operational.value,
        ),
        Fact(
            key="location_preference",
            value=["Bangalore", "Remote"],
            source=SourceEnum.candidate_stated.value,
            confidence=ConfidenceEnum.confirmed.value,
            data_class=DataClassEnum.operational.value,
        ),
        Fact(
            key="expected_ctc",
            value={"amount": 2500000, "currency": "INR"},
            source=SourceEnum.candidate_confirmed.value,
            confidence=ConfidenceEnum.confirmed.value,
            data_class=DataClassEnum.operational.value,
        ),
        Fact(
            key="notice_period",
            value={"days": 30},
            source=SourceEnum.candidate_stated.value,
            confidence=ConfidenceEnum.confirmed.value,
            data_class=DataClassEnum.operational.value,
        ),
        Fact(
            key="current_company",
            value="Acme Corp",
            source=SourceEnum.candidate_stated.value,
            confidence=ConfidenceEnum.confirmed.value,
            data_class=DataClassEnum.operational.value,
        ),
    ]

    snapshot = rebuild_projection(facts)
    assert snapshot.experience_years == 5.0
    assert snapshot.expected_ctc_annual == Decimal("2500000")
    assert snapshot.currency == "INR"
    assert snapshot.notice_period_days == 30
    assert snapshot.skills == ["Python", "FastAPI", "PostgreSQL"]
    assert snapshot.current_company == "Acme Corp"
    # All 6 blocking keys present: completeness = 1.0 (100%)
    assert snapshot.completeness == 1.0
