"""
app/domain/identity.py — Greeting and name policy engine (FLOW-026, §3).

Core principles (§3 of REVIEW_AND_PLAN.md):
- phone_number: Trusted primary external identity (immutable).
- contact_name: Channel metadata icebreaker only. Written to candidate.display_name,
                never directly to profile.full_name.
- message: Untrusted natural language.
- profile.full_name: Written ONLY on explicit confirmation or self-identification.
- Fuzzy comparison: "Rahul K" and "Rahul" do not trigger a needless clarification.
- Greeting table (5 deterministic branches):
    1. First message carries facts -> ack_and_continue_intake (never gates intake)
    2. Profile name conflicts with contact name -> clarify_name
    3. Profile name known -> greet_known
    4. No profile name, contact name present -> greet_with_contact_name
    5. No profile name, no contact name -> greet_and_ask_name
"""

import re
from typing import Any

from app.models import Candidate, CandidateProfile


def normalize_name(name: str | None) -> str:
    """Strip, lowercase, and remove extraneous punctuation from a name."""
    if not name:
        return ""
    cleaned = re.sub(r"[^\w\s]", " ", name)
    return " ".join(cleaned.lower().split())


def names_are_compatible(name_a: str | None, name_b: str | None) -> bool:
    """Fuzzy comparison to check if two names are compatible without needless clarification.

    Returns True if:
    - Either name is empty or None.
    - Names match exactly (case-insensitive).
    - One is an abbreviated version, prefix, or initial of the other (e.g. "Rahul" vs "Rahul K",
      "Rahul" vs "Rahul Kumar", "P. Sharma" vs "Priya Sharma").

    Returns False if:
    - Names clearly refer to different people (e.g. "Rahul" vs "Rohit", "Alice" vs "Bob").
    """
    norm_a = normalize_name(name_a)
    norm_b = normalize_name(name_b)

    if not norm_a or not norm_b:
        return True

    if norm_a == norm_b:
        return True

    tokens_a = norm_a.split()
    tokens_b = norm_b.split()

    # If first names differ completely, they conflict
    first_a = tokens_a[0]
    first_b = tokens_b[0]

    # Check if one is a 1-letter initial of the other (e.g. 'r' vs 'rahul')
    if len(first_a) == 1 or len(first_b) == 1:
        if first_a[0] == first_b[0]:
            return True

    if first_a != first_b:
        return False

    # First names match! Now check surnames / initials if both provide more than 1 token
    if len(tokens_a) > 1 and len(tokens_b) > 1:
        last_a = tokens_a[-1]
        last_b = tokens_b[-1]
        if last_a == last_b:
            return True
        if len(last_a) == 1 and last_b.startswith(last_a):
            return True
        if len(last_b) == 1 and last_a.startswith(last_b):
            return True
        # Different non-initial surnames (e.g. "Rahul Verma" vs "Rahul Sharma")
        return False

    # One is single name "Rahul", other is "Rahul Kumar" or "Rahul K" -> compatible
    return True


def evaluate_greeting_directive(
    contact_name: str | None,
    profile_name: str | None,
    has_extracted_facts: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Determine the greeting directive according to §3 greeting table.

    Returns (directive_name, context_dict).
    """
    # Branch 1: First message already carries recruitment facts -> start intake immediately
    if has_extracted_facts:
        return "ack_and_continue_intake", {
            "name": profile_name or contact_name,
            "skip_name_ask": True,
        }

    # Branch 2: Profile name conflicts with contact name -> clarify_name
    if profile_name and contact_name and not names_are_compatible(profile_name, contact_name):
        return "clarify_name", {
            "contact_name": contact_name,
            "profile_name": profile_name,
        }

    # Branch 3: Profile name is known and matches/compatible
    if profile_name:
        return "greet_known", {
            "name": profile_name,
        }

    # Branch 4: No profile name, but contact name is present
    if contact_name:
        return "greet_with_contact_name", {
            "name": contact_name,
        }

    # Branch 5: No profile name and no contact name
    return "greet_and_ask_name", {}


def update_candidate_identity(
    candidate: Candidate,
    profile: CandidateProfile | None,
    contact_name: str | None = None,
    self_identified_name: str | None = None,
    confirmed_name: str | None = None,
) -> bool:
    """Update identity fields according to strict trust hierarchy.

    Rules:
    - candidate.display_name is updated freely from channel contact_name.
    - profile.full_name is written ONLY when candidate self-identifies or confirms.
    - Returns True if profile.full_name was updated, False otherwise.
    """
    # 1. Update display_name from channel metadata (icebreaker only)
    if contact_name and not candidate.display_name:
        candidate.display_name = contact_name.strip()

    # 2. Only write profile.full_name on explicit self-identification or confirmation
    explicit_name = self_identified_name or confirmed_name
    if explicit_name and profile is not None:
        clean = explicit_name.strip()
        if clean:
            profile.full_name = clean
            candidate.display_name = clean
            return True

    return False
