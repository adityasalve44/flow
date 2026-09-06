"""
tests/test_registry.py — key registry unit tests.

Tests that:
- Every key in the registry has an explicit data_class
- Unknown keys default to 'personal' (never 'operational')
- All six blocking keys are registered
- Protected and personal keys are not operational
"""

from __future__ import annotations

import pytest

from app.domain.registry import (
    REGISTRY,
    all_blocking_keys,
    get_data_class,
    is_blocking,
    lookup_key,
)
from app.models.enums import DataClassEnum


EXPECTED_BLOCKING_KEYS = {
    "desired_role",
    "experience_years",
    "skills",
    "location_preference",
    "expected_ctc",
    "notice_period",
}

KNOWN_PROTECTED_KEYS = {
    "religion", "caste", "health_disability", "sex_gender",
}

KNOWN_PERSONAL_KEYS = {
    "full_name", "date_of_birth", "age", "marital_status",
    "family_circumstances", "nationality",
}


def test_every_registry_key_has_explicit_data_class():
    """Every key must have an explicit data_class — no missing classifications."""
    for key, spec in REGISTRY.items():
        assert spec.data_class in DataClassEnum, (
            f"Key '{key}' has invalid data_class: {spec.data_class}"
        )


def test_unknown_key_defaults_to_personal():
    """An unknown key must default to personal, never operational."""
    spec = lookup_key("completely_unknown_key_xyz")
    assert spec.data_class == DataClassEnum.personal, (
        f"Unknown key defaulted to {spec.data_class}, expected personal"
    )


def test_unknown_key_never_defaults_to_operational():
    """Explicitly verify the fail-closed invariant."""
    for suffix in ["new_field", "extra.something", "recruiter.note", "xyz_abc"]:
        spec = lookup_key(suffix)
        assert spec.data_class != DataClassEnum.operational, (
            f"Unknown key '{suffix}' was classified as operational — fail-open!"
        )


def test_all_six_blocking_keys_are_registered():
    """All six profile-readiness baseline keys must be in the registry."""
    registered_blocking = set(all_blocking_keys())
    missing = EXPECTED_BLOCKING_KEYS - registered_blocking
    assert not missing, f"Blocking keys missing from registry: {missing}"


def test_blocking_keys_are_all_operational():
    """Profile-readiness keys must be operational (they feed the projection)."""
    for key in EXPECTED_BLOCKING_KEYS:
        spec = lookup_key(key)
        assert spec.data_class == DataClassEnum.operational, (
            f"Blocking key '{key}' is not operational: {spec.data_class}"
        )
        assert spec.blocking is True


@pytest.mark.parametrize("key", sorted(KNOWN_PROTECTED_KEYS))
def test_protected_keys_classified_correctly(key: str):
    assert get_data_class(key) == DataClassEnum.protected


@pytest.mark.parametrize("key", sorted(KNOWN_PERSONAL_KEYS))
def test_personal_keys_classified_correctly(key: str):
    assert get_data_class(key) == DataClassEnum.personal


def test_current_ctc_is_operational():
    """current_ctc is operational for the India-focused MVP (Q5 note)."""
    assert get_data_class("current_ctc") == DataClassEnum.operational


def test_is_blocking_returns_correct_values():
    for key in EXPECTED_BLOCKING_KEYS:
        assert is_blocking(key) is True
    assert is_blocking("current_ctc") is False
    assert is_blocking("nonexistent_key") is False
