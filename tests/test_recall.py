"""
tests/test_recall.py — Recall candidate history tool tests (FLOW-031).

Requirements (§8, FLOW-031 of REVIEW_AND_PLAN.md):
1. recall_candidate_history(tool_context, topic) returning summaries plus superseded facts.
2. Recall over a 200-message history stays within a fixed token/character budget.
3. Flow never claims to remember something absent from the store (the honesty case).
4. Tool interface conforms to ADK Tool standards.
"""

from datetime import datetime, timezone
from uuid import uuid4
import pytest
from google.adk.tools import FunctionTool

from app.db.uow import UnitOfWork
from app.models import CandidateAttribute
from app.models.enums import (
    AttributeStatusEnum,
    ConfidenceEnum,
    ConsentStatusEnum,
    ConversationModeEnum,
    ConversationStatusEnum,
    DataClassEnum,
    DirectionEnum,
    SourceEnum,
)
from app.tools.history import (
    MAX_OUTPUT_CHARS,
    recall_candidate_history,
)


def test_recall_tool_declaration_exposes_no_identity_parameters():
    """Verify ADK FunctionTool for recall_candidate_history hides tool_context and exposes only topic."""
    tool = FunctionTool(recall_candidate_history)
    decl = tool._get_declaration()

    assert decl.name == "recall_candidate_history"
    assert "tool_context" not in (decl.parameters.properties if decl.parameters else {})
    if decl.parameters and decl.parameters.properties:
        assert set(decl.parameters.properties.keys()) == {"topic"}


def test_budget_bound_over_200_message_history(db):
    """
    Acceptance check: Recall over a 200-message history stays within a fixed token budget.
    """
    uow = UnitOfWork(session=db)
    phone = f"+91{uuid4().int % 10000000000:010d}"

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted

        # Simulate multiple historical conversations with 200 total messages
        for i in range(5):
            conv = uow.conversations.create(
                candidate_id=cand.id,
                mode=ConversationModeEnum.intake,
            )
            conv.status = ConversationStatusEnum.closed
            conv.closed_at = datetime(2026, 8, 1 + i, 12, 0, tzinfo=timezone.utc)
            conv.summary = (
                f"Conversation #{i + 1}: Candidate discussed backend engineering roles in Bengaluru. "
                f"Explored salary expectations between 20-25 LPA and notice period of 30 days. "
                f"Candidate provided various past experiences with Python, Kubernetes, and PostgreSQL."
            )
            uow.conversations.add(conv)

            # Add 40 messages per conversation = 200 messages total
            for m in range(40):
                direction = DirectionEnum.inbound if m % 2 == 0 else DirectionEnum.outbound
                uow.messages.create(
                    conversation_id=conv.id,
                    candidate_id=cand.id,
                    direction=direction,
                    body=f"Turn {m} detail in conversation {i}: discussing technical requirements and compensation.",
                )

        # Add several superseded facts
        for key, old_val in [
            ("expected_ctc", "18 LPA"),
            ("expected_ctc", "20 LPA"),
            ("notice_period", "60 days"),
            ("current_role", "Junior Developer"),
            ("desired_role", "Software Engineer"),
            ("location_preference", "Hyderabad"),
        ]:
            uow.attributes.add(
                CandidateAttribute(
                    candidate_id=cand.id,
                    key=key,
                    value={"raw": old_val},
                    raw_text=old_val,
                    source=SourceEnum.candidate_stated,
                    confidence=ConfidenceEnum.confirmed,
                    status=AttributeStatusEnum.superseded,
                    data_class=DataClassEnum.operational,
                )
            )

        uow.commit()

    # Query history via tool context
    tool_context = {
        "candidate_id": str(cand.id),
        "db": db,
    }
    result = recall_candidate_history(tool_context)

    assert result["found"] is True
    assert result["candidate_id"] == str(cand.id)
    assert len(result["summaries"]) <= 5
    assert len(result["superseded_facts"]) <= 8

    # Strict token / character budget enforcement
    result_str = str(result)
    assert len(result_str) <= MAX_OUTPUT_CHARS + 200, f"Payload exceeded budget: {len(result_str)} chars"


def test_honesty_case_i_already_told_you(db):
    """
    The 'I already told you' honesty case (§8, FLOW-031):
    - When candidate references a fact previously given (even if superseded), Flow remembers it.
    - When asked about a fact absent from the store, Flow never claims to remember it.
    """
    uow = UnitOfWork(session=db)
    phone = f"+91{uuid4().int % 10000000000:010d}"

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted

        # Previous closed conversation where notice period was stated
        conv = uow.conversations.create(
            candidate_id=cand.id,
            mode=ConversationModeEnum.intake,
        )
        conv.status = ConversationStatusEnum.closed
        conv.summary = "Initial intake. Candidate stated notice period is 90 days and desired role is Backend Lead."
        uow.conversations.add(conv)

        # Superseded notice period in fact store
        uow.attributes.add(
            CandidateAttribute(
                candidate_id=cand.id,
                key="notice_period",
                value=90,
                raw_text="3 months / 90 days",
                source=SourceEnum.candidate_stated,
                confidence=ConfidenceEnum.confirmed,
                status=AttributeStatusEnum.superseded,
                data_class=DataClassEnum.operational,
            )
        )
        uow.commit()

    tool_context = {
        "candidate_id": str(cand.id),
        "db": db,
    }

    # Case A: Candidate says "I already told you my notice period" -> topic='notice'
    notice_recall = recall_candidate_history(tool_context, topic="notice")
    assert notice_recall["found"] is True
    assert len(notice_recall["superseded_facts"]) >= 1
    found_key = notice_recall["superseded_facts"][0]["key"]
    assert found_key == "notice_period"
    assert "90 days" in notice_recall["superseded_facts"][0]["raw_text"]

    # Case B: Fact never mentioned / absent from the store -> topic='pilot license'
    absent_recall = recall_candidate_history(tool_context, topic="pilot license")
    assert absent_recall["found"] is False
    assert "No prior history or recorded facts found" in absent_recall["message"]
    assert len(absent_recall["summaries"]) == 0
    assert len(absent_recall["superseded_facts"]) == 0


def test_missing_or_unauthenticated_session_context():
    """Tool returns clean error if candidate identity is missing from session state."""
    res_no_state = recall_candidate_history({})
    assert res_no_state["found"] is False
    assert "No active conversation" in res_no_state["error"]

    res_no_cand = recall_candidate_history({"state": {}})
    assert res_no_cand["found"] is False
    assert "No candidate identity" in res_no_cand["error"]
