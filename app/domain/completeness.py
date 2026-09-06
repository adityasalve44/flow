"""
app/domain/completeness.py — profile readiness and completeness evaluation (Q6, FLOW-019).

Core requirements (§6, §8 of REVIEW_AND_PLAN.md):
- Six blocking baseline fields determine profile readiness:
    1. desired_role
    2. experience_years
    3. skills
    4. location_preference
    5. expected_ctc
    6. notice_period
- When all 6 blocking fields are confirmed and current, the candidate is profile_ready.
- Non-blocking fields (education, current_ctc, current_company, work_mode, name)
  do NOT gate profile readiness.
"""

from typing import Any

from app.domain.merge import Fact
from app.domain.registry import BLOCKING_KEYS, REGISTRY
from app.models import CandidateAttribute
from app.models.enums import AttributeStatusEnum, ConfidenceEnum


def is_profile_ready(attributes: list[Fact] | list[CandidateAttribute]) -> bool:
    """
    Determine if the candidate has all 6 baseline blocking fields confirmed and current.

    A candidate who gives role, experience, skills, location, expected CTC and notice period
    is profile-ready even with no name, no current CTC and no education (§8).
    """
    confirmed_current_keys: set[str] = set()

    for attr in attributes:
        status = attr.status.value if hasattr(attr.status, "value") else str(attr.status)
        conf = attr.confidence.value if hasattr(attr.confidence, "value") else str(attr.confidence)
        if status == AttributeStatusEnum.current.value and conf == ConfidenceEnum.confirmed.value:
            confirmed_current_keys.add(attr.key)

    return BLOCKING_KEYS.issubset(confirmed_current_keys)


def get_missing_blocking_fields(
    attributes: list[Fact] | list[CandidateAttribute],
) -> list[str]:
    """Return the list of blocking fields not yet confirmed and current."""
    confirmed_current_keys: set[str] = set()

    for attr in attributes:
        status = attr.status.value if hasattr(attr.status, "value") else str(attr.status)
        conf = attr.confidence.value if hasattr(attr.confidence, "value") else str(attr.confidence)
        if status == AttributeStatusEnum.current.value and conf == ConfidenceEnum.confirmed.value:
            confirmed_current_keys.add(attr.key)

    return [k for k in BLOCKING_KEYS if k not in confirmed_current_keys]


def calculate_completeness(
    attributes: list[Fact] | list[CandidateAttribute],
) -> float:
    """
    Compute the weighted completeness score across known attribute keys (0.0 to 1.0).
    """
    confirmed_current_keys: set[str] = set()
    for attr in attributes:
        status = attr.status.value if hasattr(attr.status, "value") else str(attr.status)
        conf = attr.confidence.value if hasattr(attr.confidence, "value") else str(attr.confidence)
        if status == AttributeStatusEnum.current.value and conf == ConfidenceEnum.confirmed.value:
            confirmed_current_keys.add(attr.key)

    total_weight = sum(spec.importance for spec in REGISTRY.values())
    earned_weight = sum(
        spec.importance for key, spec in REGISTRY.items() if key in confirmed_current_keys
    )

    if total_weight <= 0.0:
        return 0.0

    return round(min(1.0, earned_weight / total_weight), 2)
