"""
app/domain/moderation.py — Deflection counting, disengagement, and abuse moderation (FLOW-028, FLOW-029).

Core requirements (§3, §8, FLOW-028, FLOW-029 of REVIEW_AND_PLAN.md):
- Deflections:
  * Count only genuine deflections: candidate declined and/or deflected questions.
  * Second deflection triggers offer_call (human recruiter handoff offer).
  * Third deflection sets status=closed (disengaged) and suppresses model calls (silence).
  * Three consecutive refusals produce exactly two replies and then silence, with no third model call.
  * Reopen if a later message supplies information or changes tone.
- Abuse:
  * Cheap deterministic lexicon check runs at ingress, before any model call.
  * Plus the extractor independently emits abuse_signal for cases the lexicon misses.
  * First occurrence: a single calm request to keep it respectful (warn_abuse).
  * Second occurrence: moderation_event written, conversation flagged escalated, replies stop.
  * An administrator can set candidates.blocked_at, after which ingress short-circuits at the first check.
  * Invariant: Flow never explains the moderation mechanism, rules, or strikes to the candidate.
"""

from datetime import datetime, timezone
import re
from typing import Any
from uuid import UUID

from app.db.uow import UnitOfWork
from app.models import Candidate, Conversation
from app.models.enums import ConversationStatusEnum
from app.models.moderation import ModerationEvent

# Heuristics for detecting change of tone or willingness to engage
REOPEN_CUES: list[re.Pattern] = [
    re.compile(r"\b(sorry|apolog|start over|restart|continue|ready now|let'?s (start|continue))\b", re.IGNORECASE),
    re.compile(r"\b(my (role|experience|notice|ctc|salary|resume|cv|skills)|i am a|i work as)\b", re.IGNORECASE),
    re.compile(r"\b(i want to (apply|share|proceed)|hire me|looking for (a )?job)\b", re.IGNORECASE),
    re.compile(r"\b(\d+\s*(years?|yrs?|lpa|lakhs?|k|months?|days?))\b", re.IGNORECASE),
]

# Deterministic abuse lexicon for cheap ingress boundary check
ABUSE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(fuck(\s*(off|you|u|ing))?)\b", re.IGNORECASE),
    re.compile(r"\b(bastard|bitch|asshole|idiot|cunt|motherfucker|whore|slut)\b", re.IGNORECASE),
    re.compile(r"\b(shut\s*up|piss\s*off|go\s*to\s*hell|screw\s*you)\b", re.IGNORECASE),
    re.compile(r"\b(kill\s*yourself|die\s*(in\s*a\s*fire|slowly)?)\b", re.IGNORECASE),
    re.compile(r"\b(moron|retard|scumbag|piece\s*of\s*shit)\b", re.IGNORECASE),
    re.compile(r"\b(f[*u]ck|b[*i]tch|a[*s]shole|sh[*i]t)\b", re.IGNORECASE),
]

CALM_ABUSE_WARNING = (
    "Please keep our conversation respectful. I am here to assist with your career opportunities."
)

# Words that must NEVER be leaked in replies to candidates
MODERATION_LEAK_WORDS: list[str] = [
    "moderation",
    "moderator",
    "banned",
    "blacklist",
    "flagged",
    "strike",
    "violation",
    "penalized",
    "disciplinary",
    "warn_abuse",
    "escalated",
]


def check_abuse_lexicon(text: str | None) -> bool:
    """Evaluate if message text contains abusive, profane, or hostile language.

    Runs deterministically at ingress before any LLM invocation.
    """
    if not text:
        return False
    clean = text.strip()
    return any(p.search(clean) for p in ABUSE_PATTERNS)


def is_safe_moderation_reply(text: str | None) -> bool:
    """Verify that a reply never mentions internal moderation rules or mechanisms."""
    if not text:
        return True
    lower = text.lower()
    return not any(w in lower for w in MODERATION_LEAK_WORDS)


def is_genuine_deflection(
    refusal_signal: bool,
    demanded_other: bool = False,
    is_off_topic: bool = False,
) -> bool:
    """Evaluate if a turn constitutes a genuine deflection (§3).

    A genuine deflection occurs when the candidate explicitly declines an ask,
    or deflects while demanding something unrelated.
    """
    if refusal_signal:
        return True
    if demanded_other and is_off_topic:
        return True
    return False


def record_deflection(
    conversation: Conversation,
    is_deflection: bool,
) -> int:
    """Increment and return the conversation deflection counter if deflection occurred."""
    if is_deflection:
        conversation.deflection_count += 1
    return conversation.deflection_count


def should_reopen_conversation(
    message_text: str | None,
    has_facts: bool = False,
) -> bool:
    """Determine whether a disengaged conversation should be reopened.

    A conversation reopens if the candidate:
    1. Supplies recruitment facts (e.g. role, experience, salary, notice).
    2. Uses conciliatory language or explicitly requests to continue/restart.
    """
    if has_facts:
        return True

    if not message_text:
        return False

    text = message_text.strip()
    return any(pattern.search(text) for pattern in REOPEN_CUES)


def reopen_conversation(conversation: Conversation) -> None:
    """Reopen a previously disengaged or closed conversation."""
    conversation.status = ConversationStatusEnum.active
    conversation.closed_at = None
    conversation.deflection_count = 0


def record_moderation_event(
    uow: UnitOfWork,
    candidate_id: UUID | str,
    kind: str,
    detail: str | None = None,
    conversation_id: UUID | str | None = None,
    message_id: UUID | str | None = None,
) -> ModerationEvent:
    """Record an audit trail entry in flow.moderation_events."""
    return uow.moderation_events.create(
        candidate_id=candidate_id,
        conversation_id=conversation_id,
        message_id=message_id,
        kind=kind,
        detail=detail,
    )


def block_candidate(
    uow: UnitOfWork,
    candidate: Candidate,
    reason: str = "Administrative block",
) -> None:
    """Admin path: block a candidate permanently.

    Transitions candidate lifecycle to blocked and sets candidate.blocked_at.
    Ingress will reject and short-circuit immediately.
    """
    now = datetime.now(timezone.utc)
    from app.domain.lifecycle import transition_candidate_lifecycle
    from app.models.enums import LifecycleStatusEnum

    transition_candidate_lifecycle(
        candidate=candidate,
        target_status=LifecycleStatusEnum.blocked,
        reason=reason,
        uow=None,
        now=now,
    )
    uow.candidates.add(candidate)

    # If there is an active conversation, escalate and close it
    active_conv = uow.conversations.get_active(candidate.id)
    if active_conv:
        active_conv.status = ConversationStatusEnum.blocked
        active_conv.closed_at = now
        uow.conversations.add(active_conv)

    record_moderation_event(
        uow=uow,
        candidate_id=candidate.id,
        kind="block",
        detail=reason,
        conversation_id=active_conv.id if active_conv else None,
    )


def unblock_candidate(
    uow: UnitOfWork,
    candidate: Candidate,
    target_status: "LifecycleStatusEnum | str" = "intake",
    reason: str = "Administrative unblock",
) -> None:
    """Admin path: unblock a candidate and restore to a valid active/dormant status."""
    now = datetime.now(timezone.utc)
    from app.domain.lifecycle import transition_candidate_lifecycle

    transition_candidate_lifecycle(
        candidate=candidate,
        target_status=target_status,
        reason=reason,
        uow=None,
        now=now,
    )
    uow.candidates.add(candidate)

    record_moderation_event(
        uow=uow,
        candidate_id=candidate.id,
        kind="unblock",
        detail=f"Restored to {target_status}: {reason}",
    )
