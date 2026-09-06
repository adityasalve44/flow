"""
app/domain/projection.py — derived projection builder.

rebuild_projection(attributes) -> ProfileSnapshot

Key invariants (from §7 of REVIEW_AND_PLAN.md & Q5):
- Projects ONLY facts where:
    status == "current"
    AND confidence == "confirmed"
    AND data_class == "operational"
- Non-operational facts (personal, protected) NEVER enter ProfileSnapshot.
- Unconfirmed facts (ambiguous, inferred, unknown) NEVER enter ProfileSnapshot.
- Superseded, conflicted, or rejected facts NEVER enter ProfileSnapshot.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.domain.merge import Fact
from app.models.enums import AttributeStatusEnum, ConfidenceEnum, DataClassEnum

BLOCKING_KEYS: frozenset[str] = frozenset({
    "desired_role",
    "experience_years",
    "skills",
    "location_preference",
    "expected_ctc",
    "notice_period",
})


@dataclass
class ProfileSnapshot:
    """The derived projection consumed by matching and recruiter search views."""

    full_name: str | None = None
    current_role: str | None = None
    current_company: str | None = None
    experience_years: float | None = None
    current_ctc_annual: Decimal | float | None = None
    expected_ctc_annual: Decimal | float | None = None
    currency: str | None = None
    notice_period_days: int | None = None
    work_mode: str | None = None
    education_level: str | None = None
    skills: list[str] = field(default_factory=list)
    desired_roles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    completeness: float = 0.0
    operational_attributes: dict[str, Any] = field(default_factory=dict)


def rebuild_projection(attributes: list[Fact]) -> ProfileSnapshot:
    """
    Construct a ProfileSnapshot strictly from current, confirmed, operational facts.

    Any attribute with data_class != 'operational' (personal or protected)
    is structurally excluded from the snapshot (Q5).
    """
    # Strict filter: only current, confirmed, operational facts
    valid_facts = [
        f
        for f in attributes
        if f.status == AttributeStatusEnum.current.value
        and f.confidence == ConfidenceEnum.confirmed.value
        and f.data_class == DataClassEnum.operational.value
    ]

    snapshot = ProfileSnapshot()
    present_keys: set[str] = set()

    for fact in valid_facts:
        key = fact.key
        val = fact.value
        present_keys.add(key)
        snapshot.operational_attributes[key] = val

        if key == "experience_years":
            if isinstance(val, dict):
                snapshot.experience_years = float(val.get("amount", 0.0))
            elif isinstance(val, (int, float)):
                snapshot.experience_years = float(val)

        elif key == "expected_ctc":
            if isinstance(val, dict):
                amount = val.get("amount")
                snapshot.expected_ctc_annual = (
                    Decimal(str(amount)) if amount is not None else None
                )
                snapshot.currency = val.get("currency", "INR")
            elif isinstance(val, (int, float)):
                snapshot.expected_ctc_annual = Decimal(str(val))
                snapshot.currency = "INR"

        elif key == "current_ctc":
            if isinstance(val, dict):
                amount = val.get("amount")
                snapshot.current_ctc_annual = (
                    Decimal(str(amount)) if amount is not None else None
                )
                if not snapshot.currency:
                    snapshot.currency = val.get("currency", "INR")
            elif isinstance(val, (int, float)):
                snapshot.current_ctc_annual = Decimal(str(val))

        elif key == "notice_period":
            if isinstance(val, dict):
                snapshot.notice_period_days = int(val.get("days", 0))
            elif isinstance(val, int):
                snapshot.notice_period_days = val

        elif key == "full_name":
            snapshot.full_name = str(val) if val is not None else None

        elif key == "current_role":
            snapshot.current_role = str(val) if val is not None else None

        elif key == "current_company":
            snapshot.current_company = str(val) if val is not None else None

        elif key == "work_mode":
            snapshot.work_mode = str(val) if val is not None else None

        elif key == "education":
            snapshot.education_level = str(val) if val is not None else None

        elif key == "skills":
            if isinstance(val, list):
                snapshot.skills = [str(s) for s in val]
            elif isinstance(val, str):
                snapshot.skills = [val]

        elif key == "desired_role":
            if isinstance(val, list):
                snapshot.desired_roles = [str(r) for r in val]
            elif isinstance(val, str):
                snapshot.desired_roles = [val]

        elif key == "location_preference":
            if isinstance(val, list):
                snapshot.locations = [str(loc) for loc in val]
            elif isinstance(val, str):
                snapshot.locations = [val]

    # Calculate completeness score based on blocking keys
    present_blocking = present_keys.intersection(BLOCKING_KEYS)
    snapshot.completeness = round(len(present_blocking) / len(BLOCKING_KEYS), 2)

    return snapshot
