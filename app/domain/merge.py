"""
app/domain/merge.py — the merge engine (the heart of the system).

Deterministic resolution of provenance, supersession, and conflict.
Pure functions over dataclasses — no ORM in the signatures.

Precedence order (§7 of REVIEW_AND_PLAN.md):
  6. recruiter_verified  — only another recruiter can supersede it
  5. candidate_confirmed — explicitly confirmed in conversation
  4. candidate_stated    — volunteered clearly and unambiguously
  3. resume              — parsed document; goes stale
  2. system_calculated   — Flow's deterministic code
  1. llm_inferred        — read between the lines, never projected
  0. channel_metadata    — WhatsApp profile name, icebreaker only
"""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.models.enums import (
    AttributeStatusEnum,
    ConfidenceEnum,
    DataClassEnum,
    SourceEnum,
)

SOURCE_RANKS: dict[str, int] = {
    SourceEnum.recruiter_verified.value: 6,
    SourceEnum.candidate_confirmed.value: 5,
    SourceEnum.candidate_stated.value: 4,
    SourceEnum.resume.value: 3,
    SourceEnum.system_calculated.value: 2,
    SourceEnum.llm_inferred.value: 1,
    SourceEnum.channel_metadata.value: 0,
}


@dataclass(frozen=True)
class Fact:
    """Plain-data representation of a candidate fact (attribute)."""

    key: str
    value: Any
    source: str
    confidence: str
    data_class: str
    raw_text: str | None = None
    conversation_id: str | UUID | None = None
    message_id: str | None = None
    id: str | UUID | None = None
    status: str = AttributeStatusEnum.current.value

    def get_source_rank(self) -> int:
        return SOURCE_RANKS.get(self.source, 0)


@dataclass
class MergeContext:
    """Context for the current turn/merge operation."""

    conversation_id: str | UUID | None = None


@dataclass
class MergeResult:
    """Output of merge_facts."""

    accepted: list[Fact] = field(default_factory=list)
    superseded: list[Fact] = field(default_factory=list)
    conflicted: list[Fact] = field(default_factory=list)
    ambiguous: list[Fact] = field(default_factory=list)


def _values_equal(v1: Any, v2: Any) -> bool:
    """Compare two fact values for equality."""
    return v1 == v2


def merge_facts(
    existing: list[Fact],
    incoming: list[Fact],
    context: MergeContext | None = None,
) -> MergeResult:
    """
    Deterministically merge incoming facts against existing facts.

    Implements the §7 precedence table and four conflict cases:
    1. Correction in the same conversation -> accepted, old superseded.
    2. Contradiction from previous conversation -> conflicted (unless higher authority).
    3. Lower-authority source contradicts higher -> conflicted.
    4. Ambiguous value -> marked ambiguous, never promoted.
    """
    ctx = context or MergeContext()
    result = MergeResult()

    # Index current existing facts by key
    current_existing: dict[str, Fact] = {
        f.key: f for f in existing if f.status == AttributeStatusEnum.current.value
    }

    for inc in incoming:
        # Case 4: Ambiguous value ("about 80k a month")
        if inc.confidence == ConfidenceEnum.ambiguous.value:
            result.ambiguous.append(inc)
            result.accepted.append(inc)
            continue

        ext = current_existing.get(inc.key)

        # No existing fact for this key: accept directly
        if ext is None:
            result.accepted.append(inc)
            current_existing[inc.key] = inc
            continue

        # Existing fact found: check if values are identical (idempotency)
        if _values_equal(inc.value, ext.value):
            inc_rank = inc.get_source_rank()
            ext_rank = ext.get_source_rank()
            if inc_rank > ext_rank:
                # Upgrade source authority for identical value
                result.accepted.append(inc)
                result.superseded.append(ext)
                current_existing[inc.key] = inc
            else:
                result.accepted.append(ext)
            continue

        # Values contradict!
        inc_rank = inc.get_source_rank()
        ext_rank = ext.get_source_rank()

        # Rule 1: Recruiter-verified data is NEVER silently replaced by non-recruiter
        if (
            ext.source == SourceEnum.recruiter_verified.value
            and inc.source != SourceEnum.recruiter_verified.value
        ):
            result.conflicted.append(inc)
            continue

        # Case 3: Lower authority contradicts higher authority
        if inc_rank < ext_rank:
            result.conflicted.append(inc)
            continue

        # Check conversation context
        is_same_conversation = (
            ctx.conversation_id is not None
            and ext.conversation_id is not None
            and str(ctx.conversation_id) == str(ext.conversation_id)
        )

        if is_same_conversation:
            # Case 1: Correction in the same conversation ("actually, 5 years")
            # Self-correction applies directly: new becomes current, old superseded.
            result.accepted.append(inc)
            result.superseded.append(ext)
            current_existing[inc.key] = inc
        else:
            # Case 2: Contradiction across different conversations
            if inc_rank > ext_rank or inc.source == SourceEnum.candidate_confirmed.value:
                # Strictly higher authority or candidate explicitly confirmed
                result.accepted.append(inc)
                result.superseded.append(ext)
                current_existing[inc.key] = inc
            else:
                # Same authority from different conversation: mark conflicted
                result.conflicted.append(inc)

    return result
