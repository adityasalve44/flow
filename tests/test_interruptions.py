"""
tests/test_interruptions.py — Interruptions and redirection policy tests (FLOW-027, §3, §8).

Tests:
1. Related question:
   - Question related to pending ask -> answer_and_continue directive.
   - Re-asks pending field in the same message.
   - Authoritative glossary provides 4-5 lines of explanation.
2. Unrelated job opening question:
   - Question about openings at specific companies -> redirect directive.
   - Strict rule: Never confirms a job exists; never names clients.
3. Out of scope entirely:
   - Off-topic questions (e.g. weather, homework) -> redirect directive.
4. Deflection counting rule (§3):
   - Asking a question without refusal does NOT increment deflection_count.
   - Question accompanied by explicit refusal increments deflection_count.
"""

from uuid import uuid4

import pytest

from app.agents.prompts.reply import build_reply_instruction
from app.agents.schemas import (
    ExtractedQuestion,
    IntentEnum,
    TurnExtraction,
)
from app.db.uow import UnitOfWork
from app.domain.policy import evaluate_policy_step
from app.models.enums import ChannelEnum, ConsentStatusEnum, ConversationModeEnum
from app.tools.glossary import explain_recruitment_term


def test_related_question_triggers_answer_and_continue(db):
    """
    Acceptance test (FLOW-027):
    'What is CTC?' is answered in 4-5 lines and the pending question is re-asked in the same message.
    """
    uow = UnitOfWork(session=db)
    cand_phone = f"+9198{uuid4().int % 100000000:08d}"

    with uow:
        cand = uow.candidates.get_or_create_by_phone(cand_phone)
        cand.consent_status = ConsentStatusEnum.granted
        uow.candidates.add(cand)

        conv = uow.conversations.create(candidate_id=cand.id, channel=ChannelEnum.simulator)
        conv.mode = ConversationModeEnum.intake
        uow.commit()

        # Candidate asks "What is CTC?" while expected_ctc was pending
        extraction = TurnExtraction(
            intent=IntentEnum.ask_question,
            facts=[],
            questions=[
                ExtractedQuestion(
                    text="What is CTC?",
                    topic="ctc",
                    is_related_to_pending=True,
                )
            ],
            refusal_signal=False,
        )

        directive, snapshot = evaluate_policy_step(
            uow=uow,
            candidate=cand,
            conversation=conv,
            extraction=extraction,
        )

        assert directive.name == "answer_and_continue"
        assert directive.question_topic == "ctc"
        assert len(directive.fields_to_ask) == 1

        # Glossary verification: 4-5 line authoritative text
        glossary_res = explain_recruitment_term("CTC")
        assert glossary_res["found"] is True
        explanation_lines = glossary_res["explanation"].strip().split("\n")
        assert 3 <= len(explanation_lines) <= 5

        # Instruction prompt verification: tells model to answer and continue asking
        instruction = build_reply_instruction(
            directive_name=directive.name,
            fields_to_ask=directive.fields_to_ask,
            question_topic=directive.question_topic,
        )
        assert "answer the candidate's question" in instruction.lower()
        assert "continue the conversation" in instruction.lower()


def test_unrelated_job_question_triggers_redirect_with_non_commitment(db):
    """
    Acceptance test (FLOW-027, §3):
    'Do you have an opening at Google?' triggers redirect and never confirms an opening exists.
    """
    uow = UnitOfWork(session=db)
    cand_phone = f"+9198{uuid4().int % 100000000:08d}"

    with uow:
        cand = uow.candidates.get_or_create_by_phone(cand_phone)
        cand.consent_status = ConsentStatusEnum.granted
        uow.candidates.add(cand)

        conv = uow.conversations.create(candidate_id=cand.id, channel=ChannelEnum.simulator)
        conv.mode = ConversationModeEnum.intake
        uow.commit()

        extraction = TurnExtraction(
            intent=IntentEnum.ask_question,
            facts=[],
            questions=[
                ExtractedQuestion(
                    text="Do you have an opening at Google for me?",
                    topic="job_openings",
                    is_related_to_pending=False,
                )
            ],
            refusal_signal=False,
        )

        directive, snapshot = evaluate_policy_step(
            uow=uow,
            candidate=cand,
            conversation=conv,
            extraction=extraction,
        )

        assert directive.name == "redirect"
        assert directive.question_topic == "job_openings"
        assert len(directive.fields_to_ask) == 1

        # Instruction prompt guarantees non-commitment
        instruction = build_reply_instruction(
            directive_name=directive.name,
            fields_to_ask=directive.fields_to_ask,
            question_topic=directive.question_topic,
        )
        assert "never confirm a specific job opening exists" in instruction.lower()
        assert "redirect" in instruction.lower()


def test_out_of_scope_interruption_triggers_redirect(db):
    """Off-topic questions without recruitment facts trigger polite redirect."""
    uow = UnitOfWork(session=db)
    cand_phone = f"+9198{uuid4().int % 100000000:08d}"

    with uow:
        cand = uow.candidates.get_or_create_by_phone(cand_phone)
        cand.consent_status = ConsentStatusEnum.granted
        uow.candidates.add(cand)

        conv = uow.conversations.create(candidate_id=cand.id, channel=ChannelEnum.simulator)
        conv.mode = ConversationModeEnum.intake
        uow.commit()

        extraction = TurnExtraction(
            intent=IntentEnum.ask_question,
            facts=[],
            questions=[
                ExtractedQuestion(
                    text="Can you write a poem about the monsoon?",
                    topic="creative_writing",
                    is_related_to_pending=False,
                )
            ],
            refusal_signal=False,
        )

        directive, snapshot = evaluate_policy_step(
            uow=uow,
            candidate=cand,
            conversation=conv,
            extraction=extraction,
        )

        assert directive.name == "redirect"
        assert len(directive.fields_to_ask) == 1


def test_deflection_counter_not_incremented_on_benign_questions(db):
    """
    Acceptance test (§3, FLOW-027):
    A benign question does NOT increment deflection_count unless the candidate also refused to answer.
    """
    uow = UnitOfWork(session=db)
    cand_phone = f"+9198{uuid4().int % 100000000:08d}"

    with uow:
        cand = uow.candidates.get_or_create_by_phone(cand_phone)
        cand.consent_status = ConsentStatusEnum.granted
        uow.candidates.add(cand)

        conv = uow.conversations.create(candidate_id=cand.id, channel=ChannelEnum.simulator)
        conv.mode = ConversationModeEnum.intake
        assert conv.deflection_count == 0
        uow.commit()

        # 1. Candidate asks benign question (refusal_signal=False)
        extraction1 = TurnExtraction(
            intent=IntentEnum.ask_question,
            facts=[],
            questions=[
                ExtractedQuestion(
                    text="How long does this intake take?",
                    topic="intake_process",
                    is_related_to_pending=False,
                )
            ],
            refusal_signal=False,
        )

        evaluate_policy_step(
            uow=uow,
            candidate=cand,
            conversation=conv,
            extraction=extraction1,
        )

        assert conv.deflection_count == 0, "Benign question must NOT increment deflection_count"

        # 2. Candidate asks question AND refuses to answer pending field (refusal_signal=True)
        extraction2 = TurnExtraction(
            intent=IntentEnum.refuse,
            facts=[],
            questions=[
                ExtractedQuestion(
                    text="Why do you need my current salary? I won't tell you.",
                    topic="salary_privacy",
                    is_related_to_pending=False,
                )
            ],
            refusal_signal=True,
        )

        evaluate_policy_step(
            uow=uow,
            candidate=cand,
            conversation=conv,
            extraction=extraction2,
        )

        assert conv.deflection_count == 1, "Question with refusal MUST increment deflection_count"
