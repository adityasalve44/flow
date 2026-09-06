"""
app/domain/registry.py — the attribute key registry.

This is the authoritative table of every known attribute key, its value
shape, its data_class, and its importance weight for next-question scoring.

Rules:
  1. ``data_class`` is ALWAYS read from this registry — never guessed by a model.
  2. Unknown keys default to ``personal`` (fail closed, never ``operational``).
  3. ``importance`` is the Q6 weight table used by the next-question scorer.
  4. ``blocking`` means the field is in the six-field profile-readiness baseline.

The registry is plain data — no database, no model calls, no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.enums import DataClassEnum


@dataclass(frozen=True)
class KeySpec:
    """Specification for a single attribute key."""
    key: str
    data_class: DataClassEnum
    importance: float  # 0.0–1.0 for next-question scoring
    blocking: bool = False  # part of the six-field profile-readiness baseline
    description: str = ""


# ---------------------------------------------------------------------------
# The registry — every known attribute key
# ---------------------------------------------------------------------------

REGISTRY: dict[str, KeySpec] = {k.key: k for k in [
    # ── Blocking fields (profile-readiness baseline, Q6) ───────────────────
    KeySpec(
        key="desired_role",
        data_class=DataClassEnum.operational,
        importance=1.0,
        blocking=True,
        description="What role the candidate is looking for",
    ),
    KeySpec(
        key="experience_years",
        data_class=DataClassEnum.operational,
        importance=1.0,
        blocking=True,
        description="Total years of relevant professional experience",
    ),
    KeySpec(
        key="skills",
        data_class=DataClassEnum.operational,
        importance=1.0,
        blocking=True,
        description="Relevant technical or functional skills",
    ),
    KeySpec(
        key="location_preference",
        data_class=DataClassEnum.operational,
        importance=1.0,
        blocking=True,
        description="Where the candidate is willing to work",
    ),
    KeySpec(
        key="expected_ctc",
        data_class=DataClassEnum.operational,
        importance=0.9,
        blocking=True,
        description="Expected compensation (CTC, annual, INR by default)",
    ),
    KeySpec(
        key="notice_period",
        data_class=DataClassEnum.operational,
        importance=0.9,
        blocking=True,
        description="Notice period in days (0 = immediate joiner)",
    ),

    # ── Non-blocking operational ────────────────────────────────────────────
    KeySpec(
        key="current_ctc",
        data_class=DataClassEnum.operational,
        importance=0.5,
        description="Current compensation (CTC, annual, INR by default)",
    ),
    KeySpec(
        key="current_role",
        data_class=DataClassEnum.operational,
        importance=0.4,
        description="Candidate's current job title",
    ),
    KeySpec(
        key="current_company",
        data_class=DataClassEnum.operational,
        importance=0.4,
        description="Candidate's current employer",
    ),
    KeySpec(
        key="work_mode",
        data_class=DataClassEnum.operational,
        importance=0.3,
        description="Preference: remote / hybrid / on-site",
    ),
    KeySpec(
        key="education",
        data_class=DataClassEnum.operational,
        importance=0.25,
        description="Highest education level or degree",
    ),
    KeySpec(
        key="resume",
        data_class=DataClassEnum.operational,
        importance=0.6,
        description="Resume availability and confirmation status",
    ),

    # ── Personal — stored, restricted, never projected (Q5) ─────────────────
    # Name is personal because it is PII even though it helps the conversation.
    # It is stored on candidates.display_name / candidate_profiles.full_name
    # separately, but as an attribute key it is classified personal.
    KeySpec(
        key="full_name",
        data_class=DataClassEnum.personal,
        importance=0.35,
        description="Candidate's full name (also stored on candidate.display_name)",
    ),
    KeySpec(
        key="date_of_birth",
        data_class=DataClassEnum.personal,
        importance=0.1,
        description="Date of birth — personal, not for matching",
    ),
    KeySpec(
        key="age",
        data_class=DataClassEnum.personal,
        importance=0.1,
        description="Age — personal, not for matching",
    ),
    KeySpec(
        key="marital_status",
        data_class=DataClassEnum.personal,
        importance=0.1,
        description="Marital status — personal, not for matching",
    ),
    KeySpec(
        key="family_circumstances",
        data_class=DataClassEnum.personal,
        importance=0.1,
        description="Family-related context (e.g. spouse's location) — personal",
    ),
    KeySpec(
        key="nationality",
        data_class=DataClassEnum.personal,
        importance=0.1,
        description="Nationality — personal",
    ),

    # ── Protected — legally/ethically sensitive (Q5) ────────────────────────
    KeySpec(
        key="religion",
        data_class=DataClassEnum.protected,
        importance=0.0,
        description="Religious affiliation — protected, stored with strong access control",
    ),
    KeySpec(
        key="caste",
        data_class=DataClassEnum.protected,
        importance=0.0,
        description="Caste — protected",
    ),
    KeySpec(
        key="health_disability",
        data_class=DataClassEnum.protected,
        importance=0.0,
        description="Health status or disability — protected",
    ),
    KeySpec(
        key="sex_gender",
        data_class=DataClassEnum.protected,
        importance=0.0,
        description="Sex or gender identity — protected",
    ),

    # ── Extras — flexible namespace for anything else ────────────────────────
    # extras.* keys are caught by the lookup_key() fallback to personal.
    # Candidates can volunteer any information; it is preserved but not projected.
]}

KEY_REGISTRY = REGISTRY

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Default class for unknown keys — fail closed, never operational
_UNKNOWN_DEFAULT = DataClassEnum.personal


def lookup_key(key: str) -> KeySpec:
    """Return the KeySpec for a known key, or a personal-class default.

    An unknown key NEVER gets data_class=operational.  This is the fail-closed
    behaviour required by Q5: a new fact extracted for an unknown field cannot
    accidentally reach the projection and influence matching.
    """
    if key in REGISTRY:
        return REGISTRY[key]
    # extras.* and anything else default to personal
    return KeySpec(
        key=key,
        data_class=_UNKNOWN_DEFAULT,
        importance=0.1,
        description=f"Unknown key '{key}' — defaulted to personal",
    )


def get_data_class(key: str) -> DataClassEnum:
    """Return only the data_class for a key (most common use case)."""
    return lookup_key(key).data_class


def get_importance(key: str) -> float:
    """Return only the importance weight for a key."""
    return lookup_key(key).importance


def is_blocking(key: str) -> bool:
    """Return whether a key is in the six-field profile-readiness baseline."""
    spec = REGISTRY.get(key)
    return spec.blocking if spec else False


def all_blocking_keys() -> list[str]:
    """Return all keys that are in the profile-readiness baseline."""
    return [spec.key for spec in REGISTRY.values() if spec.blocking]


get_key_spec = lookup_key
get_blocking_keys = all_blocking_keys
