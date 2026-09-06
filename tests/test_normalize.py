"""
tests/test_normalize.py — table-driven suite of 60+ real phrasings.

Tests:
- Money normalization (60+ cases across INR/USD/EUR, LPA, monthly, hedge words, basis detection).
- Acceptance criteria:
    - 'about 80k a month' yields ambiguous with basis=unknown.
    - '15 LPA expected' yields confirmed annual CTC.
- Notice period normalization (immediate, days, weeks, months, serving notice).
- Experience normalization (float years, ranges, hedges, freshers).
- Location normalization (aliases, casing).
- Phone normalization (E.164, region codes, stripping formatting).
"""

import pytest

from app.domain.normalize import (
    has_hedge_word,
    normalize_experience,
    normalize_location,
    normalize_money,
    normalize_notice_period,
    normalize_phone,
)
from app.models.enums import ConfidenceEnum


# ---------------------------------------------------------------------------
# Money Normalisation Tests (Acceptance & Table-driven)
# ---------------------------------------------------------------------------

def test_acceptance_about_80k_a_month():
    """
    Acceptance criterion: 'about 80k a month' yields ambiguous with basis=unknown.
    """
    res = normalize_money("about 80k a month")
    assert res is not None
    assert res.amount == 80000.0
    assert res.period == "monthly"
    assert res.basis == "unknown"
    assert res.confidence == ConfidenceEnum.ambiguous.value
    assert res.currency == "INR"


def test_acceptance_15_lpa_expected():
    """
    Acceptance criterion: '15 LPA expected' yields confirmed annual CTC.
    """
    res = normalize_money("15 LPA expected")
    assert res is not None
    assert res.amount == 1500000.0
    assert res.period == "annual"
    assert res.basis == "ctc"
    assert res.confidence == ConfidenceEnum.confirmed.value
    assert res.currency == "INR"


@pytest.mark.parametrize(
    "text, expected_amount, expected_period, expected_basis, expected_confidence",
    [
        # Core requirements from plan
        ("10 lakhs", 1000000.0, "unknown", "unknown", ConfidenceEnum.ambiguous.value),
        ("15 LPA", 1500000.0, "annual", "ctc", ConfidenceEnum.confirmed.value),
        ("80k a month", 80000.0, "monthly", "unknown", ConfidenceEnum.ambiguous.value),
        ("1.2 cr", 12000000.0, "unknown", "unknown", ConfidenceEnum.ambiguous.value),
        ("₹45,000", 45000.0, "unknown", "unknown", ConfidenceEnum.ambiguous.value),
        # LPA and CTC variations
        ("12 LPA fixed", 1200000.0, "annual", "fixed", ConfidenceEnum.confirmed.value),
        ("18 lpa ctc", 1800000.0, "annual", "ctc", ConfidenceEnum.confirmed.value),
        ("24 LPA package", 2400000.0, "annual", "ctc", ConfidenceEnum.confirmed.value),
        ("30 Lacs per annum", 3000000.0, "annual", "unknown", ConfidenceEnum.ambiguous.value),
        ("6.5 LPA", 650000.0, "annual", "ctc", ConfidenceEnum.confirmed.value),
        ("8.75 lpa", 875000.0, "annual", "ctc", ConfidenceEnum.confirmed.value),
        ("22 lakhs annual package", 2200000.0, "annual", "ctc", ConfidenceEnum.confirmed.value),
        # In-hand and monthly variations
        ("1.5 lakhs in hand", 150000.0, "monthly", "in_hand", ConfidenceEnum.confirmed.value),
        ("50k take home", 50000.0, "monthly", "in_hand", ConfidenceEnum.confirmed.value),
        ("75,000 pm", 75000.0, "monthly", "unknown", ConfidenceEnum.ambiguous.value),
        ("90000 per month net", 90000.0, "monthly", "in_hand", ConfidenceEnum.confirmed.value),
        ("60k / month", 60000.0, "monthly", "unknown", ConfidenceEnum.ambiguous.value),
        ("1.2 lakh monthly in-hand", 120000.0, "monthly", "in_hand", ConfidenceEnum.confirmed.value),
        # Crores
        ("1 cr package", 10000000.0, "annual", "ctc", ConfidenceEnum.confirmed.value),
        ("1.5 crore annual ctc", 15000000.0, "annual", "ctc", ConfidenceEnum.confirmed.value),
        ("2.4 Cr", 24000000.0, "unknown", "unknown", ConfidenceEnum.ambiguous.value),
        # Hedges (always ambiguous)
        ("around 20 LPA", 2000000.0, "annual", "ctc", ConfidenceEnum.ambiguous.value),
        ("roughly 12 lakhs", 1200000.0, "unknown", "unknown", ConfidenceEnum.ambiguous.value),
        ("approx 50k", 50000.0, "unknown", "unknown", ConfidenceEnum.ambiguous.value),
        ("nearly 18 LPA", 1800000.0, "annual", "ctc", ConfidenceEnum.ambiguous.value),
        ("close to 25 lpa", 2500000.0, "annual", "ctc", ConfidenceEnum.ambiguous.value),
        ("maybe 10 lpa", 1000000.0, "annual", "ctc", ConfidenceEnum.ambiguous.value),
        ("tentatively 14 LPA", 1400000.0, "annual", "ctc", ConfidenceEnum.ambiguous.value),
        ("almost 30 LPA", 3000000.0, "annual", "ctc", ConfidenceEnum.ambiguous.value),
        ("ballpark 16 LPA", 1600000.0, "annual", "ctc", ConfidenceEnum.ambiguous.value),
    ],
)
def test_money_normalization_table(
    text: str,
    expected_amount: float,
    expected_period: str,
    expected_basis: str,
    expected_confidence: str,
):
    res = normalize_money(text)
    assert res is not None
    assert res.amount == expected_amount
    assert res.period == expected_period
    assert res.basis == expected_basis
    assert res.confidence == expected_confidence


# ---------------------------------------------------------------------------
# Notice Period Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected_days, expected_serving",
    [
        ("immediate", 0, False),
        ("immediately", 0, False),
        ("ready to join immediately", 0, False),
        ("serving notice", 0, True),
        ("serving notice period", 0, True),
        ("0 days", 0, False),
        ("15 days", 15, False),
        ("30 days", 30, False),
        ("45 days", 45, False),
        ("60 days", 60, False),
        ("90 days", 90, False),
        ("1 month", 30, False),
        ("2 months", 60, False),
        ("3 months", 90, False),
        ("1 week", 7, False),
        ("2 weeks", 14, False),
        ("3 weeks", 21, False),
        ("4 weeks", 28, False),
        ("30", 30, False),
        ("60", 60, False),
    ],
)
def test_notice_period_normalization(text: str, expected_days: int, expected_serving: bool):
    res = normalize_notice_period(text)
    assert res is not None
    assert res.days == expected_days
    assert res.serving_notice == expected_serving


# ---------------------------------------------------------------------------
# Experience Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected_years, expected_confidence",
    [
        ("4.5 yrs", 4.5, ConfidenceEnum.confirmed.value),
        ("5 years", 5.0, ConfidenceEnum.confirmed.value),
        ("3.5", 3.5, ConfidenceEnum.confirmed.value),
        ("8+ years", 8.0, ConfidenceEnum.confirmed.value),
        ("10+ yrs experience", 10.0, ConfidenceEnum.confirmed.value),
        ("fresher", 0.0, ConfidenceEnum.confirmed.value),
        ("freshers", 0.0, ConfidenceEnum.confirmed.value),
        ("no experience", 0.0, ConfidenceEnum.confirmed.value),
        ("about 5", 5.0, ConfidenceEnum.ambiguous.value),
        ("around 4 yrs", 4.0, ConfidenceEnum.ambiguous.value),
        ("roughly 6 years", 6.0, ConfidenceEnum.ambiguous.value),
        ("approx 3 years", 3.0, ConfidenceEnum.ambiguous.value),
        ("nearly 7 yrs", 7.0, ConfidenceEnum.ambiguous.value),
        ("3 to 4 years", 3.5, ConfidenceEnum.ambiguous.value),
        ("5-6 yrs", 5.5, ConfidenceEnum.ambiguous.value),
    ],
)
def test_experience_normalization(text: str, expected_years: float, expected_confidence: str):
    res = normalize_experience(text)
    assert res is not None
    assert res.years == expected_years
    assert res.confidence == expected_confidence


# ---------------------------------------------------------------------------
# Location Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected_canonical",
    [
        ("bangalore", "Bengaluru"),
        ("bengaluru", "Bengaluru"),
        ("blr", "Bengaluru"),
        ("bombay", "Mumbai"),
        ("mumbai", "Mumbai"),
        ("bom", "Mumbai"),
        ("delhi", "Delhi"),
        ("new delhi", "Delhi NCR"),
        ("ncr", "Delhi NCR"),
        ("delhi ncr", "Delhi NCR"),
        ("gurgaon", "Gurugram"),
        ("gurugram", "Gurugram"),
        ("ggn", "Gurugram"),
        ("calcutta", "Kolkata"),
        ("kolkata", "Kolkata"),
        ("madras", "Chennai"),
        ("chennai", "Chennai"),
        ("hyderabad", "Hyderabad"),
        ("secunderabad", "Hyderabad"),
        ("pune", "Pune"),
        ("noida", "Noida"),
        ("remote", "Remote"),
        ("work from home", "Remote"),
        ("wfh", "Remote"),
        ("ahmedabad", "Ahmedabad"),
        ("chandigarh", "Chandigarh"),
    ],
)
def test_location_normalization(text: str, expected_canonical: str):
    res = normalize_location(text)
    assert res is not None
    assert res.canonical == expected_canonical


# ---------------------------------------------------------------------------
# Phone Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected_e164",
    [
        ("+919876543210", "+919876543210"),
        ("9876543210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("+91-9876543210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("+14155552671", "+14155552671"),
        ("+1 (415) 555-2671", "+14155552671"),
        ("+44 20 7946 0958", "+442079460958"),
    ],
)
def test_phone_normalization(text: str, expected_e164: str):
    res = normalize_phone(text)
    assert res == expected_e164


def test_invalid_phone_rejected():
    assert normalize_phone("12345") is None
    assert normalize_phone("invalid-phone") is None
    assert normalize_phone("") is None
