"""
app/domain/moderation.py — Deflection counting, disengagement, and conversation moderation (FLOW-028).

Core requirements (§3, §8, FLOW-028 of REVIEW_AND_PLAN.md):
- Count only genuine deflections: candidate declined and/or deflected questions.
- Second deflection triggers offer_call (human recruiter handoff offer).
- Third deflection sets status=closed (disengaged) and suppresses model calls (silence).
- Three consecutive refusals produce exactly two replies and then silence, with no third model call.
- Reopen if a later message supplies information or changes tone.
"""

import re
from typing import Any

from app.models import Conversation
from app.models.enums import ConversationStatusEnum

# Heuristics for detecting change of tone or willingness to engage
REOPEN_CUES: list[re.Pattern] = [
    re.compile(r"\b(sorry|apolog|start over|restart|continue|ready now|let'?s (start|continue))\b", re.IGNORECASE),
    re.compile(r"\b(my (role|experience|notice|ctc|salary|resume|cv|skills)|i am a|i work as)\b", re.IGNORECASE),
    re.compile(r"\b(i want to (apply|share|proceed)|hire me|looking for (a )?job)\b", re.IGNORECASE),
    re.compile(r"\b(\d+\s*(years?|yrs?|lpa|lakhs?|k|months?|days?))\b", re.IGNORECASE),
]


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
