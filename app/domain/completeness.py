"""
app/domain/completeness.py — profile readiness and completeness evaluation (Q6, FLOW-019, FLOW-025).

Core requirements (§6, §8 of REVIEW_AND_PLAN.md):
- Six blocking baseline fields determine profile readiness:
    1. desired_role
    2. experience_years
    3. skills
    4. location_preference
    5. expected_ctc
    6. notice_period
- When all 6 blocking fields hold confirmed, current values, is_profile_ready returns True.
- Non-blocking fields (name, current_ctc, current_company, work_mode, education)
  do NOT gate profile readiness.
"""

from typing import Any

from app.domain.merge import Fact
from app.domain.registry import BLOCKING_KEYS, REGISTRY
from app.models import CandidateAttribute
from app.models.enums import AttributeStatusEnum, ConfidenceEnum


def is_profile_ready(data: Any) -> bool:
    """
    Determine if the candidate has all 6 baseline blocking fields confirmed and current.

    Accepts:
    - dict: projection dictionary or attribute map.
    - ProfileSnapshot / CandidateProfile: projection object with attribute fields.
    - list[Fact] / list[CandidateAttribute]: raw facts/attributes with status and confidence.

    A candidate who gives role, experience, skills, location, expected CTC and notice period
    is profile-ready even with no name, no current CTC and no education (§8).
    """
    if isinstance(data, dict):
        for key in BLOCKING_KEYS:
            val = data.get(key)
            if val is None or val == "" or val == []:
                # Check potential schema aliases
                if key == "notice_period" and data.get("notice_period_days") is not None:
                    continue
                if key == "expected_ctc" and data.get("expected_ctc_annual") is not None:
                    continue
                return False
        return True

    if hasattr(data, "desired_roles") or hasattr(data, "desired_role") or hasattr(data, "current_role"):
        # ProfileSnapshot or CandidateProfile
        has_role = bool(
            getattr(data, "desired_roles", None)
            or getattr(data, "desired_role", None)
            or getattr(data, "current_role", None)
        )
        has_exp = getattr(data, "experience_years", None) is not None
        has_skills = bool(getattr(data, "skills", None))
        has_loc = bool(
            getattr(data, "locations", None)
            or getattr(data, "location_preference", None)
            or getattr(data, "location_prefs", None)
        )
        has_ctc = bool(
            getattr(data, "expected_ctc", None)
            or getattr(data, "expected_ctc_annual", None)
        )
        has_notice = (
            getattr(data, "notice_period", None) is not None
            or getattr(data, "notice_period_days", None) is not None
        )
        return bool(has_role and has_exp and has_skills and has_loc and has_ctc and has_notice)

    # Assume iterable of Fact or CandidateAttribute
    confirmed_current_keys: set[str] = set()
    for attr in data:
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

    if "current_role" in confirmed_current_keys:
        confirmed_current_keys.add("desired_role")

    return [k for k in sorted(BLOCKING_KEYS) if k not in confirmed_current_keys]


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
