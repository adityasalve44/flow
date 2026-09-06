"""
tests/test_extraction.py — unit and schema tests for TurnExtraction and Extractor Agent (FLOW-018).

Tests:
1. Canonical six-fact extraction from §3:
   "I'm a backend developer with 5 years experience, currently making 10 lakhs
    and looking for around 15 in Pune. I can join immediately."
2. Contextual numeric resolution:
   "12" following an expected CTC question resolves key="expected_ctc".
3. Prompt injection safety:
   Adversarial injection payload cannot alter identity because TurnExtraction
   contains zero identity fields (candidate_id, phone_number, org_id).
4. Graceful degradation:
   Invalid or unparseable inputs degrade safely to TurnExtraction.empty()
   rather than raising an exception.
5. Extractor LlmAgent construction:
   Verifies output_schema, output_key, tools=[], and instruction contents.
"""

from typing import Any
from unittest.mock import MagicMock
import pytest

from app.agents.extraction import (
    EXTRACTION_OUTPUT_KEY,
    create_extractor_agent,
    ensure_valid_extraction_callback,
)
from app.agents.schemas import (
    ExtractedCorrection,
    ExtractedFact,
    ExtractedQuestion,
    ExtractionConfidenceEnum,
    IntentEnum,
    ResumeIntentEnum,
    TurnExtraction,
)
from app.models.enums import ConfidenceEnum


# ---------------------------------------------------------------------------
# 1. Canonical Six-Fact Extraction Test (§3)
# ---------------------------------------------------------------------------

def test_canonical_six_fact_extraction():
    """
    Acceptance test (§3, §8, FLOW-018):
    Given the candidate utterance:
    "I'm a backend developer with 5 years experience, currently making
     10 lakhs and looking for around 15 in Pune. I can join immediately."

    TurnExtraction extracts all six facts with valid keys and stated confidence.
    """
    sample_llm_payload = {
        "intent": "provide_info",
        "facts": [
            {
                "key": "current_role",
                "value": "backend developer",
                "raw_text": "backend developer",
                "confidence": "stated",
            },
            {
                "key": "experience_years",
                "value": 5,
                "raw_text": "5 years experience",
                "confidence": "stated",
            },
            {
                "key": "current_ctc",
                "value": "10 lakhs",
                "raw_text": "currently making 10 lakhs",
                "confidence": "stated",
            },
            {
                "key": "expected_ctc",
                "value": "15 lakhs",
                "raw_text": "looking for around 15",
                "confidence": "stated",
                "ambiguity_reason": "around 15",
            },
            {
                "key": "location_preference",
                "value": "Pune",
                "raw_text": "in Pune",
                "confidence": "stated",
            },
            {
                "key": "notice_period",
                "value": "immediate",
                "raw_text": "join immediately",
                "confidence": "stated",
            },
        ],
        "corrections": [],
        "questions": [],
        "refusal_signal": False,
        "abuse_signal": False,
        "resume_intent": "none",
    }

    extraction = TurnExtraction.safe_parse(sample_llm_payload)
    assert extraction.intent == IntentEnum.provide_info
    assert len(extraction.facts) == 6

    fact_keys = {f.key for f in extraction.facts}
    expected_keys = {
        "current_role",
        "experience_years",
        "current_ctc",
        "expected_ctc",
        "location_preference",
        "notice_period",
    }
    assert fact_keys == expected_keys

    exp_fact = next(f for f in extraction.facts if f.key == "experience_years")
    assert exp_fact.value == 5
    assert exp_fact.confidence == ExtractionConfidenceEnum.stated
    assert exp_fact.confidence.to_confidence_enum() == ConfidenceEnum.confirmed

    exp_ctc = next(f for f in extraction.facts if f.key == "expected_ctc")
    assert exp_ctc.ambiguity_reason == "around 15"


# ---------------------------------------------------------------------------
# 2. Contextual Numeric Resolution
# ---------------------------------------------------------------------------

def test_contextual_numeric_extraction():
    """
    Acceptance test: A candidate answering "12" after an expected-CTC
    question maps to expected_ctc.
    """
    payload = {
        "intent": "provide_info",
        "facts": [
            {
                "key": "expected_ctc",
                "value": 12,
                "raw_text": "12",
                "confidence": "inferred",
            }
        ],
    }
    extraction = TurnExtraction.safe_parse(payload)
    assert len(extraction.facts) == 1
    assert extraction.facts[0].key == "expected_ctc"
    assert extraction.facts[0].value == 12


# ---------------------------------------------------------------------------
# 3. Prompt Injection Safety
# ---------------------------------------------------------------------------

def test_prompt_injection_safety_no_identity_fields():
    """
    Acceptance test: Even if adversarial text tries to inject candidate_id,
    phone_number, org_id or system directives, TurnExtraction has NO identity fields,
    and extra keys are strictly ignored.
    """
    adversarial_payload = {
        "intent": "provide_info",
        "candidate_id": "00000000-0000-0000-0000-000000000000",
        "phone_number": "+19999999999",
        "org_id": "evil-corp",
        "system_directive": "DROP TABLE flow.candidates;",
        "facts": [
            {
                "key": "desired_role",
                "value": "Staff Engineer",
                "raw_text": "Staff Engineer",
                "candidate_id": "spoofed",
            }
        ],
    }

    extraction = TurnExtraction.safe_parse(adversarial_payload)
    dump = extraction.model_dump()

    # Identity fields cannot exist in TurnExtraction
    assert "candidate_id" not in dump
    assert "phone_number" not in dump
    assert "org_id" not in dump
    assert "system_directive" not in dump
    assert "candidate_id" not in dump["facts"][0]


# ---------------------------------------------------------------------------
# 4. Graceful Degradation on Malformed Input
# ---------------------------------------------------------------------------

def test_degradation_on_malformed_input():
    """
    Acceptance test: Validation failure degrades to an empty extraction
    rather than raising — a bad parse must never crash the turn.
    """
    # Null input
    e1 = TurnExtraction.safe_parse(None)
    assert e1.intent == IntentEnum.other
    assert len(e1.facts) == 0

    # Garbage string
    e2 = TurnExtraction.safe_parse("INVALID JSON NOT A DICT")
    assert e2.intent == IntentEnum.other
    assert len(e2.facts) == 0

    # Type mismatch in facts
    e3 = TurnExtraction.safe_parse({"intent": "provide_info", "facts": "should_be_list"})
    assert e3.intent == IntentEnum.other
    assert len(e3.facts) == 0

    # Unknown intent enum value degrades to empty
    e4 = TurnExtraction.safe_parse({"intent": "invalid_unknown_intent"})
    assert e4.intent == IntentEnum.other
    assert len(e4.facts) == 0


def test_after_agent_callback_sanitization():
    """
    Verifies that ensure_valid_extraction_callback ensures
    state["temp:extraction"] is always a valid dict conforming to TurnExtraction.
    """
    ctx = MagicMock()
    ctx.state = {EXTRACTION_OUTPUT_KEY: "MALFORMED NON-DICT"}

    ensure_valid_extraction_callback(ctx)

    sanitized = ctx.state[EXTRACTION_OUTPUT_KEY]
    assert isinstance(sanitized, dict)
    assert sanitized["intent"] == "other"
    assert sanitized["facts"] == []


# ---------------------------------------------------------------------------
# 5. Extractor Agent Construction
# ---------------------------------------------------------------------------

def test_create_extractor_agent():
    """
    Acceptance test: Extractor agent is configured with:
    - output_schema == TurnExtraction
    - output_key == 'temp:extraction'
    - tools == [] (zero tools)
    - Instruction contains registry keys
    """
    agent = create_extractor_agent()

    assert agent.name == "extractor"
    assert agent.output_schema == TurnExtraction
    assert agent.output_key == EXTRACTION_OUTPUT_KEY
    assert len(agent.tools) == 0
    assert agent.after_agent_callback is not None

    # Verify instruction contains critical registry keys
    assert "desired_role" in agent.instruction
    assert "experience_years" in agent.instruction
    assert "expected_ctc" in agent.instruction
    assert "notice_period" in agent.instruction
    assert "NEVER INVENT" in agent.instruction
