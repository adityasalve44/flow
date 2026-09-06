"""
app/domain/normalize.py — normalisers and ambiguity rules.

Deterministic, pure functions turning human phrasing into structured data.
English-only (Q3). Hedge words, units, aliases kept in module-level data tables.

Key normalization domains:
1. Money (Indian English numbering: lakh, crore, LPA, monthly, hedge detection).
2. Notice period (immediate, days, weeks, months, serving notice).
3. Experience (years as float, hedge detection).
4. Locations (aliases: Bangalore->Bengaluru, Bombay->Mumbai, Gurgaon->Gurugram).
5. Phone (E.164 with default region).
"""

import re
from dataclasses import dataclass

from app.models.enums import ConfidenceEnum

# ---------------------------------------------------------------------------
# Module-level data tables (no inline regex magic, extensible)
# ---------------------------------------------------------------------------

HEDGE_WORDS: frozenset[str] = frozenset({
    "about",
    "around",
    "roughly",
    "approx",
    "approximately",
    "nearly",
    "close to",
    "almost",
    "tentative",
    "tentatively",
    "maybe",
    "somewhere around",
    "ballpark",
    "more or less",
})

CURRENCY_SYMBOLS: dict[str, str] = {
    "₹": "INR",
    "rs": "INR",
    "rs.": "INR",
    "inr": "INR",
    "rupee": "INR",
    "rupees": "INR",
    "$": "USD",
    "usd": "USD",
    "dollar": "USD",
    "dollars": "USD",
    "€": "EUR",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "pound": "GBP",
    "pounds": "GBP",
}

MULTIPLIERS: dict[str, float] = {
    "lakh": 100_000.0,
    "lakhs": 100_000.0,
    "lac": 100_000.0,
    "lacs": 100_000.0,
    "lpa": 100_000.0,
    "l": 100_000.0,
    "crore": 10_000_000.0,
    "crores": 10_000_000.0,
    "cr": 10_000_000.0,
    "k": 1_000.0,
    "thousand": 1_000.0,
    "thousands": 1_000.0,
    "million": 1_000_000.0,
    "m": 1_000_000.0,
}

PERIOD_INDICATORS: dict[str, str] = {
    "month": "monthly",
    "monthly": "monthly",
    "per month": "monthly",
    "pm": "monthly",
    "p.m.": "monthly",
    "/m": "monthly",
    "/month": "monthly",
    "a month": "monthly",
    "each month": "monthly",
    "in hand": "monthly",
    "in-hand": "monthly",
    "take home": "monthly",
    "take-home": "monthly",
    "year": "annual",
    "yearly": "annual",
    "annual": "annual",
    "annually": "annual",
    "per annum": "annual",
    "pa": "annual",
    "p.a.": "annual",
    "/yr": "annual",
    "/year": "annual",
    "a year": "annual",
    "lpa": "annual",
}

BASIS_INDICATORS: dict[str, str] = {
    "ctc": "ctc",
    "fixed": "fixed",
    "package": "ctc",
    "total": "ctc",
    "lpa": "ctc",
    "in hand": "in_hand",
    "in-hand": "in_hand",
    "take home": "in_hand",
    "take-home": "in_hand",
    "net": "in_hand",
    "gross": "gross",
}

LOCATION_ALIASES: dict[str, str] = {
    "bangalore": "Bengaluru",
    "bengaluru": "Bengaluru",
    "blr": "Bengaluru",
    "bombay": "Mumbai",
    "mumbai": "Mumbai",
    "bom": "Mumbai",
    "calcutta": "Kolkata",
    "kolkata": "Kolkata",
    "ccu": "Kolkata",
    "madras": "Chennai",
    "chennai": "Chennai",
    "maa": "Chennai",
    "gurgaon": "Gurugram",
    "gurugram": "Gurugram",
    "ggn": "Gurugram",
    "delhi": "Delhi",
    "new delhi": "Delhi NCR",
    "ncr": "Delhi NCR",
    "delhi ncr": "Delhi NCR",
    "delhi-ncr": "Delhi NCR",
    "hyderabad": "Hyderabad",
    "hyd": "Hyderabad",
    "secunderabad": "Hyderabad",
    "pune": "Pune",
    "pnq": "Pune",
    "noida": "Noida",
    "greater noida": "Noida",
    "remote": "Remote",
    "work from home": "Remote",
    "wfh": "Remote",
    "anywhere": "Remote",
    "ahmedabad": "Ahmedabad",
    "chandigarh": "Chandigarh",
    "jaipur": "Jaipur",
    "kochi": "Kochi",
    "cochin": "Kochi",
    "trivandrum": "Thiruvananthapuram",
    "thiruvananthapuram": "Thiruvananthapuram",
}


# ---------------------------------------------------------------------------
# Output DTOs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MoneyNormalized:
    amount: float
    currency: str
    period: str  # annual | monthly | unknown
    basis: str  # ctc | in_hand | fixed | gross | unknown
    confidence: str  # confirmed | ambiguous
    raw_text: str


@dataclass(frozen=True)
class NoticePeriodNormalized:
    days: int
    confidence: str
    serving_notice: bool
    raw_text: str


@dataclass(frozen=True)
class ExperienceNormalized:
    years: float
    confidence: str
    raw_text: str


@dataclass(frozen=True)
class LocationNormalized:
    canonical: str
    confidence: str
    raw_text: str


# ---------------------------------------------------------------------------
# Normalization Functions
# ---------------------------------------------------------------------------

def has_hedge_word(text: str) -> bool:
    """Return True if any known hedge word appears in the phrasing."""
    lower = text.lower()
    for hedge in HEDGE_WORDS:
        # Match as whole word or phrase
        pattern = r"\b" + re.escape(hedge) + r"\b"
        if re.search(pattern, lower):
            return True
    return False


def normalize_money(text: str) -> MoneyNormalized | None:
    """
    Parse natural language compensation into structured MoneyNormalized.

    Rules:
    - 10 lakhs -> 1,000,000 annual INR (if basis unstated, basis=unknown, confidence=ambiguous).
    - 15 LPA -> 1,500,000 annual CTC INR (LPA implies annual CTC).
    - 80k a month -> 80,000 monthly INR.
    - 1.2 cr -> 12,000,000 INR.
    - ₹45,000 -> 45,000 INR.
    - If a hedge word ("about", "around", "roughly") is present, confidence=ambiguous.
    - If basis is undetermined (gross vs net vs CTC), basis=unknown and confidence=ambiguous.
    """
    if not text or not text.strip():
        return None

    raw = text.strip()
    lower = raw.lower()

    is_hedged = has_hedge_word(lower)

    # 1. Detect currency
    currency = "INR"  # Default for Flow
    for sym, curr in CURRENCY_SYMBOLS.items():
        if re.search(r"\b" + re.escape(sym) + r"\b", lower) or sym in lower:
            currency = curr
            break

    # 2. Detect basis
    basis = "unknown"
    for ind, bas in BASIS_INDICATORS.items():
        if re.search(r"\b" + re.escape(ind) + r"\b", lower):
            basis = bas
            break
    if basis == "unknown" and "lpa" in lower:
        basis = "ctc"

    # 3. Detect period
    period = "unknown"
    if "lpa" in lower or "per annum" in lower or "p.a." in lower or "annual" in lower or "package" in lower:
        period = "annual"
    else:
        for ind, per in PERIOD_INDICATORS.items():
            if re.search(r"\b" + re.escape(ind) + r"\b", lower):
                period = per
                break

    # 4. Extract numeric value and multiplier
    # Clean text to isolate number and multiplier
    # Handle patterns: "1.2 cr", "10 lakhs", "80k", "₹45,000", "15 LPA", "25,00,000"
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*([a-zA-Z]+)?", lower.replace(",", ""))
    if not match:
        return None

    number_str = match.group(1)
    unit_str = (match.group(2) or "").strip()

    try:
        base_number = float(number_str)
    except ValueError:
        return None

    multiplier = 1.0
    # Check unit attached to number or elsewhere in text
    if unit_str in MULTIPLIERS:
        multiplier = MULTIPLIERS[unit_str]
    else:
        for unit_key, mult in MULTIPLIERS.items():
            if re.search(r"\b" + re.escape(unit_key) + r"\b", lower):
                multiplier = mult
                break

    amount = base_number * multiplier

    # If amount is very small and no multiplier given (e.g. "15"), check if LPA is implied
    if amount < 100 and "lpa" in lower:
        amount = amount * 100_000.0

    # If unit was LPA, period is annual and basis is ctc unless explicit fixed/in-hand stated
    if ("lpa" in lower or unit_str == "lpa") and basis == "unknown":
        period = "annual"
        basis = "ctc"

    # Determine confidence:
    # If hedged -> ambiguous
    # If basis is unknown -> ambiguous (Q5 & §7: basis unknown is ambiguous)
    # If period is unknown -> ambiguous
    if is_hedged or basis == "unknown" or period == "unknown":
        confidence = ConfidenceEnum.ambiguous.value
    else:
        confidence = ConfidenceEnum.confirmed.value

    return MoneyNormalized(
        amount=amount,
        currency=currency,
        period=period,
        basis=basis,
        confidence=confidence,
        raw_text=raw,
    )


def normalize_notice_period(text: str) -> NoticePeriodNormalized | None:
    """
    Parse notice period into integer days.

    Examples:
    - 'immediate', 'immediately', '0 days' -> 0 days
    - 'serving notice' -> 0 days (or remaining days if specified), serving_notice=True
    - '15 days' -> 15 days
    - '1 month' -> 30 days
    - '2 months' -> 60 days
    - '3 months' -> 90 days
    - '2 weeks' -> 14 days
    """
    if not text or not text.strip():
        return None

    raw = text.strip()
    lower = raw.lower()

    is_hedged = has_hedge_word(lower)
    confidence = (
        ConfidenceEnum.ambiguous.value if is_hedged else ConfidenceEnum.confirmed.value
    )

    serving_notice = "serving" in lower

    # Immediate / zero notice
    if any(w in lower for w in ["immediate", "immediately", "ready to join", "join immediately"]):
        return NoticePeriodNormalized(
            days=0,
            confidence=confidence,
            serving_notice=serving_notice,
            raw_text=raw,
        )

    # Days
    days_match = re.search(r"(\d+)\s*(?:day|days)\b", lower)
    if days_match:
        return NoticePeriodNormalized(
            days=int(days_match.group(1)),
            confidence=confidence,
            serving_notice=serving_notice,
            raw_text=raw,
        )

    # Weeks
    weeks_match = re.search(r"(\d+)\s*(?:week|weeks)\b", lower)
    if weeks_match:
        return NoticePeriodNormalized(
            days=int(weeks_match.group(1)) * 7,
            confidence=confidence,
            serving_notice=serving_notice,
            raw_text=raw,
        )

    # Months
    months_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:month|months)\b", lower)
    if months_match:
        return NoticePeriodNormalized(
            days=int(round(float(months_match.group(1)) * 30)),
            confidence=confidence,
            serving_notice=serving_notice,
            raw_text=raw,
        )

    # Just a number: e.g. "30" or "60" -> default to days
    num_match = re.search(r"^\s*(\d+)\s*$", lower)
    if num_match:
        return NoticePeriodNormalized(
            days=int(num_match.group(1)),
            confidence=confidence,
            serving_notice=serving_notice,
            raw_text=raw,
        )

    # Serving notice with no duration specified
    if serving_notice:
        return NoticePeriodNormalized(
            days=0,
            confidence=ConfidenceEnum.ambiguous.value,
            serving_notice=True,
            raw_text=raw,
        )

    return None


def normalize_experience(text: str) -> ExperienceNormalized | None:
    """
    Parse experience into float years.

    Examples:
    - '4.5 yrs' -> 4.5
    - 'about 5' -> 5.0 (ambiguous)
    - 'freshers' / 'fresher' -> 0.0
    - '3 to 4 years' -> 3.5 (ambiguous)
    - '8+ years' -> 8.0
    """
    if not text or not text.strip():
        return None

    raw = text.strip()
    lower = raw.lower()

    is_hedged = has_hedge_word(lower)
    confidence = (
        ConfidenceEnum.ambiguous.value if is_hedged else ConfidenceEnum.confirmed.value
    )

    if any(f in lower for f in ["fresher", "freshers", "no experience", "zero"]):
        return ExperienceNormalized(
            years=0.0,
            confidence=confidence,
            raw_text=raw,
        )

    # Range: e.g. "3 to 4 years" or "3-4 years"
    range_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:to|-)\s*(\d+(?:\.\d+)?)\s*(?:yr|yrs|year|years)?", lower)
    if range_match:
        low = float(range_match.group(1))
        high = float(range_match.group(2))
        avg = (low + high) / 2.0
        return ExperienceNormalized(
            years=avg,
            confidence=ConfidenceEnum.ambiguous.value,
            raw_text=raw,
        )

    # Single number: "4.5 yrs", "5 years", "4.5", "5+"
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:\+)?\s*(?:yr|yrs|year|years)?", lower)
    if match:
        years = float(match.group(1))
        return ExperienceNormalized(
            years=years,
            confidence=confidence,
            raw_text=raw,
        )

    return None


def normalize_location(text: str) -> LocationNormalized | None:
    """
    Normalize location phrasing against common Indian and global tech hubs.

    Examples:
    - 'bangalore', 'bengaluru', 'blr' -> 'Bengaluru'
    - 'bombay', 'mumbai' -> 'Mumbai'
    - 'gurgaon', 'gurugram' -> 'Gurugram'
    - 'remote', 'wfh' -> 'Remote'
    """
    if not text or not text.strip():
        return None

    raw = text.strip()
    lower = raw.lower()

    if lower in LOCATION_ALIASES:
        return LocationNormalized(
            canonical=LOCATION_ALIASES[lower],
            confidence=ConfidenceEnum.confirmed.value,
            raw_text=raw,
        )

    # Title case fallback for unknown cities (e.g. "Mysore" -> "Mysore")
    return LocationNormalized(
        canonical=raw.title(),
        confidence=ConfidenceEnum.confirmed.value,
        raw_text=raw,
    )


def normalize_phone(text: str, default_region: str = "+91") -> str | None:
    """
    Normalize phone number to E.164 standard.

    Examples:
    - '+919876543210' -> '+919876543210'
    - '9876543210' -> '+919876543210'
    - '09876543210' -> '+919876543210'
    - '+1 (415) 555-2671' -> '+14155552671'
    """
    if not text or not text.strip():
        return None

    # Strip spaces, dashes, brackets
    cleaned = re.sub(r"[\s\-\(\)\.]", "", text.strip())

    if cleaned.startswith("+"):
        # Already has international prefix
        digits = re.sub(r"[^\d]", "", cleaned[1:])
        if len(digits) >= 10:
            return f"+{digits}"
        return None

    # Starts with 00 (international format)
    if cleaned.startswith("00"):
        digits = cleaned[2:]
        if len(digits) >= 10:
            return f"+{digits}"
        return None

    # Starts with single leading 0 (trunk prefix in India, UK, etc.)
    if cleaned.startswith("0") and len(cleaned) == 11:
        cleaned = cleaned[1:]

    # 10 digit number: prepend default region
    if len(cleaned) == 10 and cleaned.isdigit():
        return f"{default_region}{cleaned}"

    # Number already has country code without +
    if len(cleaned) == 12 and cleaned.startswith("91"):
        return f"+{cleaned}"

    if len(cleaned) >= 10 and cleaned.isdigit():
        return f"+{cleaned}"

    return None
