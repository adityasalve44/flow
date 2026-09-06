"""
tests/test_consent.py — unit and integration tests for the consent gate (Q4, FLOW-045).

Tests:
1. classify_consent:
   - grant phrases ("yes", "proceed", "agree", "sounds good")
   - refuse phrases ("no", "decline", "not interested")
   - withdraw phrases ("withdraw consent", "delete my data", "revoke consent")
   - neutral/other phrases ("I am a Python dev in Berlin", "hello")
2. §3 table 5 acceptance flows:
   - Row 1: First inbound with pending consent -> ask_consent directive, zero attributes stored.
   - Row 2: Candidate replies "Yes" -> consent_status=granted, consent_at set, mode=intake.
   - Row 3: Candidate replies "No" -> consent_status=declined, conversation closed, sticky decline.
   - Row 4: Candidate ignores consent and sends role/city details -> re-ask consent, extractor NOT invoked, zero attributes persisted.
   - Row 5: Candidate withdraws consent mid-conversation -> status=withdrawn, conversation closed.
3. Database invariants:
   - Zero attributes persisted in flow.candidate_attributes while consent is pending or declined.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.agents.prompts.consent import (
    CONSENT_DECLINED_REPLY,
    CONSENT_REASK_NOTICE,
    CONSENT_REQUEST_NOTICE,
    CONSENT_WITHDRAWN_REPLY,
)
from app.db.uow import UnitOfWork
from app.domain.consent import (
    ConsentDecision,
    ConsentIntent,
    classify_consent,
    evaluate_consent_turn,
)
from app.models.enums import (
    ConsentStatusEnum,
    ConversationModeEnum,
    ConversationStatusEnum,
)
from app.services.conversation import resolve_conversation


# ---------------------------------------------------------------------------
# 1. Unit Tests: classify_consent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "yes",
        "Yes",
        "YES!",
        "y",
        "sure",
        "ok",
        "okay",
        "agree",
        "i agree",
        "proceed",
        "go ahead",
        "sounds good",
        "i consent",
    ],
)
def test_classify_consent_grant(text: str):
    assert classify_consent(text) == ConsentIntent.GRANT


@pytest.mark.parametrize(
    "text",
    [
        "no",
        "No",
        "NO.",
        "nope",
        "decline",
        "refuse",
        "not interested",
        "dont agree",
        "don't agree",
        "disagree",
        "stop",
    ],
)
def test_classify_consent_refuse(text: str):
    assert classify_consent(text) == ConsentIntent.REFUSE


@pytest.mark.parametrize(
    "text",
    [
        "withdraw consent",
        "please withdraw consent",
        "revoke consent",
        "delete my data",
        "delete my profile",
        "stop using my data",
        "remove my info",
    ],
)
def test_classify_consent_withdraw(text: str):
    assert classify_consent(text) == ConsentIntent.WITHDRAW


@pytest.mark.parametrize(
    "text",
    [
        "I am a Python developer in Berlin",
        "Looking for remote senior roles",
        "hello",
        "what jobs do you have?",
        "120k",
        "",
        "   ",
    ],
)
def test_classify_consent_neither(text: str):
    assert classify_consent(text) == ConsentIntent.NEITHER


# ---------------------------------------------------------------------------
# 2. §3 Table Acceptance Flows
# ---------------------------------------------------------------------------

def test_row1_first_inbound_pending_consent(db):
    """
    Row 1: Candidate sends first inbound message.
    consent_status=pending -> mode=consent, directive=ask_consent,
    extractor NOT invoked, zero candidate_attributes stored in DB.
    """
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    t0 = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        assert cand.consent_status == ConsentStatusEnum.pending

        conv = resolve_conversation(uow, cand, now=t0)
        assert conv.mode == ConversationModeEnum.consent

        decision = evaluate_consent_turn(
            candidate=cand,
            conversation=conv,
            message_text="Hi there",
            channel_message_id="msg-001",
            now=t0,
        )

        assert decision.intent == ConsentIntent.NEITHER
        assert decision.directive == "ask_consent"
        assert decision.should_invoke_extractor is False
        assert decision.reply_text == CONSENT_REASK_NOTICE

        # DB assertion: candidate attributes must be strictly 0
        attrs = uow.attributes.get_all_for_candidate(cand.id)
        assert len(attrs) == 0


def test_row2_candidate_replies_yes(db):
    """
    Row 2: Candidate replies "Yes" -> consent_at set, mode moves to intake,
    directive=consent_granted.
    """
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    t0 = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        conv = resolve_conversation(uow, cand, now=t0)

        decision = evaluate_consent_turn(
            candidate=cand,
            conversation=conv,
            message_text="Yes, I agree",
            channel_message_id="msg-002",
            now=t0,
        )

        assert decision.intent == ConsentIntent.GRANT
        assert decision.directive == "consent_granted"
        assert decision.should_invoke_extractor is False

        assert cand.consent_status == ConsentStatusEnum.granted
        assert cand.consent_at == t0
        assert cand.consent_message_id == "msg-002"
        assert conv.mode == ConversationModeEnum.intake


def test_row3_candidate_replies_no_sticky_decline(db):
    """
    Row 3: Candidate replies "No" -> consent_status=declined,
    conversation closed politely, decline is sticky on subsequent messages.
    """
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    t0 = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        conv = resolve_conversation(uow, cand, now=t0)

        # Candidate declines
        decision = evaluate_consent_turn(
            candidate=cand,
            conversation=conv,
            message_text="No thanks",
            channel_message_id="msg-003",
            now=t0,
        )

        assert decision.intent == ConsentIntent.REFUSE
        assert decision.directive == "consent_declined"
        assert decision.reply_text == CONSENT_DECLINED_REPLY
        assert decision.should_invoke_extractor is False

        assert cand.consent_status == ConsentStatusEnum.declined
        assert conv.status == ConversationStatusEnum.closed
        assert conv.closed_at == t0

        # Subsequent message from declined candidate is sticky: never re-asked
        t1 = datetime(2026, 9, 6, 12, 5, 0, tzinfo=timezone.utc)
        subsequent_decision = evaluate_consent_turn(
            candidate=cand,
            conversation=conv,
            message_text="Are there any jobs?",
            channel_message_id="msg-004",
            now=t1,
        )
        assert subsequent_decision.intent == ConsentIntent.REFUSE
        assert subsequent_decision.directive == "already_declined"
        assert subsequent_decision.reply_text == CONSENT_DECLINED_REPLY
        assert subsequent_decision.should_invoke_extractor is False

        # Zero attributes stored
        attrs = uow.attributes.get_all_for_candidate(cand.id)
        assert len(attrs) == 0


def test_row4_candidate_ignores_consent_zero_persistence(db):
    """
    Row 4: Candidate ignores consent, sends "I am a Python dev in Berlin" ->
    warm acknowledgment re-ask consent, facts in that message must NOT be persisted (Q4),
    extractor is NOT invoked, candidate_attributes count is strictly 0.
    """
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    t0 = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        conv = resolve_conversation(uow, cand, now=t0)

        # Candidate sends details without granting consent
        decision = evaluate_consent_turn(
            candidate=cand,
            conversation=conv,
            message_text="I am a Python dev in Berlin looking for 100k",
            channel_message_id="msg-005",
            now=t0,
        )

        assert decision.intent == ConsentIntent.NEITHER
        assert decision.directive == "ask_consent"
        assert decision.reply_text == CONSENT_REASK_NOTICE
        # Critical Q4 invariant: Extractor MUST NOT be invoked!
        assert decision.should_invoke_extractor is False
        assert cand.consent_status == ConsentStatusEnum.pending

        # Verify candidate_attributes table is strictly empty
        attrs = uow.attributes.get_all_for_candidate(cand.id)
        assert len(attrs) == 0


def test_row5_withdrawal_mid_conversation(db):
    """
    Row 5: Candidate previously granted consent, but replies "withdraw consent"
    mid-conversation -> status=withdrawn, conversation closed, extractor NOT invoked.
    """
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    t0 = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        cand.consent_at = t0
        uow.candidates.add(cand)

        conv = resolve_conversation(uow, cand, now=t0)
        assert conv.status == ConversationStatusEnum.active

        t1 = datetime(2026, 9, 6, 12, 10, 0, tzinfo=timezone.utc)
        decision = evaluate_consent_turn(
            candidate=cand,
            conversation=conv,
            message_text="Please delete my data and withdraw consent",
            channel_message_id="msg-006",
            now=t1,
        )

        assert decision.intent == ConsentIntent.WITHDRAW
        assert decision.directive == "consent_withdrawn"
        assert decision.reply_text == CONSENT_WITHDRAWN_REPLY
        assert decision.should_invoke_extractor is False

        assert cand.consent_status == ConsentStatusEnum.withdrawn
        assert conv.status == ConversationStatusEnum.closed
        assert conv.closed_at == t1


def test_granted_candidate_passes_through(db):
    """
    A candidate with granted consent returns should_invoke_extractor=True,
    allowing the normal turn pipeline to proceed.
    """
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    t0 = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        cand.consent_at = t0
        uow.candidates.add(cand)

        conv = resolve_conversation(uow, cand, now=t0)

        decision = evaluate_consent_turn(
            candidate=cand,
            conversation=conv,
            message_text="I am looking for a Senior DevOps role",
            channel_message_id="msg-007",
            now=t0,
        )

        assert decision.intent == ConsentIntent.GRANT
        assert decision.directive == "continue"
        assert decision.should_invoke_extractor is True
        assert decision.reply_text == ""
