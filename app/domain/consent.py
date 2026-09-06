"""
app/domain/consent.py — consent gate evaluation and intent classification (Q4).

Core requirements (§3, §8, §19 of REVIEW_AND_PLAN.md):
- Explicit consent is required BEFORE any persistent candidate-data storage.
- Before consent, candidate_attributes is empty for that candidate.
- While consent_status=pending, the turn runs a narrow consent-detection path only:
    GRANT -> consent_at set, mode moves to intake.
    REFUSE -> consent_status=declined, conversation closed politely, sticky decline.
    WITHDRAW -> recognized at any point, conversation closed.
    NEITHER -> warm acknowledgement, ask/reask consent, 0 attributes written.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import re

from app.agents.prompts.consent import (
    CONSENT_DECLINED_REPLY,
    CONSENT_REASK_NOTICE,
    CONSENT_REQUEST_NOTICE,
    CONSENT_WITHDRAWN_REPLY,
)
from app.models import Candidate, Conversation
from app.models.enums import (
    ConsentStatusEnum,
    ConversationModeEnum,
    ConversationStatusEnum,
)

GRANT_PHRASES: frozenset[str] = frozenset({
    "yes",
    "y",
    "sure",
    "ok",
    "okay",
    "agree",
    "i agree",
    "proceed",
    "go ahead",
    "fine",
    "yep",
    "yeah",
    "i consent",
    "sounds good",
    "continue",
    "accept",
    "yes i agree",
    "yes please",
    "allowed",
    "i accept",
    "start",
})

REFUSE_PHRASES: frozenset[str] = frozenset({
    "no",
    "n",
    "nope",
    "decline",
    "refuse",
    "not interested",
    "dont agree",
    "don't agree",
    "disagree",
    "stop",
    "cancel",
    "dont store",
    "don't store",
    "no thanks",
    "no thank you",
    "never mind",
    "nevermind",
    "i decline",
})

WITHDRAW_PHRASES: frozenset[str] = frozenset({
    "withdraw",
    "withdraw consent",
    "revoke consent",
    "delete my data",
    "remove my data",
    "delete my profile",
    "stop using my data",
    "remove my info",
    "delete my details",
    "withdraw my application",
})


class ConsentIntent(str, Enum):
    GRANT = "grant"
    REFUSE = "refuse"
    WITHDRAW = "withdraw"
    NEITHER = "neither"


@dataclass(frozen=True)
class ConsentDecision:
    """The outcome of evaluating a turn through the consent gate."""

    intent: ConsentIntent
    directive: str
    reply_text: str
    should_invoke_extractor: bool


def classify_consent(text: str) -> ConsentIntent:
    """
    Classify a message into GRANT, REFUSE, WITHDRAW, or NEITHER.

    Uses deterministic phrase matching against standardized vocabularies.
    """
    if not text or not text.strip():
        return ConsentIntent.NEITHER

    cleaned = text.strip().lower()

    # 1. Check withdrawal first (can happen at any point)
    for phrase in WITHDRAW_PHRASES:
        pattern = r"\b" + re.escape(phrase) + r"\b"
        if re.search(pattern, cleaned):
            return ConsentIntent.WITHDRAW

    # 2. Check exact / isolated word or short phrase match for refuse
    for phrase in REFUSE_PHRASES:
        pattern = r"^" + re.escape(phrase) + r"[\.\!\?]?$"
        if re.match(pattern, cleaned) or re.search(r"\b" + re.escape(phrase) + r"\b", cleaned):
            return ConsentIntent.REFUSE

    # 3. Check exact / isolated word or short phrase match for grant
    for phrase in GRANT_PHRASES:
        pattern = r"^" + re.escape(phrase) + r"[\.\!\?]?$"
        if re.match(pattern, cleaned) or re.search(r"\b" + re.escape(phrase) + r"\b", cleaned):
            return ConsentIntent.GRANT

    return ConsentIntent.NEITHER


def evaluate_consent_turn(
    candidate: Candidate,
    conversation: Conversation,
    message_text: str,
    channel_message_id: str | None = None,
    now: datetime | None = None,
) -> ConsentDecision:
    """
    Evaluate the inbound turn through the consent gate.

    Invariants:
    - If consent is pending, extractor must NEVER run, and 0 attributes are persisted.
    - If declined, sticky decline is honored; never re-asked.
    - If withdrawn, immediately transitions to withdrawn and closes conversation.
    """
    current_time = now or datetime.now(timezone.utc)

    # 1. Sticky declined candidate: never re-ask, politely close
    if candidate.consent_status == ConsentStatusEnum.declined:
        return ConsentDecision(
            intent=ConsentIntent.REFUSE,
            directive="already_declined",
            reply_text=CONSENT_DECLINED_REPLY,
            should_invoke_extractor=False,
        )

    # 2. Check for explicit withdrawal across any lifecycle stage
    intent = classify_consent(message_text)
    if intent == ConsentIntent.WITHDRAW:
        candidate.consent_status = ConsentStatusEnum.withdrawn
        conversation.status = ConversationStatusEnum.closed
        conversation.closed_at = current_time
        return ConsentDecision(
            intent=ConsentIntent.WITHDRAW,
            directive="consent_withdrawn",
            reply_text=CONSENT_WITHDRAWN_REPLY,
            should_invoke_extractor=False,
        )

    # 3. If candidate already has granted consent: pass through to normal pipeline
    if candidate.consent_status == ConsentStatusEnum.granted:
        return ConsentDecision(
            intent=ConsentIntent.GRANT,
            directive="continue",
            reply_text="",
            should_invoke_extractor=True,
        )

    # 4. Candidate is PENDING consent: evaluate response
    if intent == ConsentIntent.GRANT:
        # Candidate opted in!
        candidate.consent_status = ConsentStatusEnum.granted
        candidate.consent_at = current_time
        candidate.consent_message_id = channel_message_id
        conversation.mode = ConversationModeEnum.intake
        return ConsentDecision(
            intent=ConsentIntent.GRANT,
            directive="consent_granted",
            reply_text="Thank you! To help find the right opportunities, what role or position are you looking for?",
            should_invoke_extractor=False,
        )

    if intent == ConsentIntent.REFUSE:
        # Candidate declined
        candidate.consent_status = ConsentStatusEnum.declined
        conversation.status = ConversationStatusEnum.closed
        conversation.closed_at = current_time
        return ConsentDecision(
            intent=ConsentIntent.REFUSE,
            directive="consent_declined",
            reply_text=CONSENT_DECLINED_REPLY,
            should_invoke_extractor=False,
        )

    # Intent is NEITHER: Candidate ignored consent or sent other information
    # Facts in that message must NOT be persisted (Q4). Re-ask consent.
    return ConsentDecision(
        intent=ConsentIntent.NEITHER,
        directive="ask_consent",
        reply_text=CONSENT_REASK_NOTICE,
        should_invoke_extractor=False,
    )
