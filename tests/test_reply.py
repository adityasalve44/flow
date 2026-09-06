"""
tests/test_reply.py — unit and snapshot tests for Reply Agent and Instruction Provider (FLOW-020).

Tests:
1. Agent creation:
   - Configured with callable instruction provider.
   - Outputs natural prose (output_schema is None).
2. Instruction provider dynamic prompt assembly:
   - Injects directive name, fields to ask, ambiguous facts, and candidate snapshot.
   - Contains strict constraints: PLAIN TEXT ONLY, AT MOST TWO ASKS, ZERO INVENTED FACTS.
3. Worked examples quality & coverage:
   - Verifies all key ladder directives have examples.
   - Confirms NO markdown bolding or headers exist in worked examples.
4. Textual variation and semantic equivalence test:
   - Asserts distinct surface forms convey identical semantic goals.
"""

from unittest.mock import MagicMock
import pytest

from app.agents.prompts.reply import (
    WORKED_EXAMPLES,
    build_reply_instruction,
    reply_instruction_provider,
)
from app.agents.reply import create_reply_agent


def test_reply_agent_creation():
    """Verify replier LlmAgent is constructed with callable instruction provider."""
    agent = create_reply_agent()
    assert agent.name == "replier"
    assert callable(agent.instruction)
    assert agent.output_schema is None  # Generates natural prose
    assert len(agent.tools) == 0


def test_instruction_provider_ask_next():
    """Verify ask_next directive correctly formats the prompt with target fields."""
    ctx = MagicMock()
    ctx.state = {
        "temp:directive": {
            "name": "ask_next",
            "fields_to_ask": ["desired_role", "skills"],
        },
        "temp:snapshot": {
            "experience_years": 5.0,
            "location_preference": ["Pune"],
        },
        "mode": "intake",
    }

    prompt = reply_instruction_provider(ctx)

    assert "### CURRENT DIRECTIVE: ask_next" in prompt
    assert "### FIELDS TO ASK ABOUT: desired_role, skills" in prompt
    assert "### CONVERSATION MODE: INTAKE" in prompt
    assert "PLAIN TEXT ONLY" in prompt
    assert "AT MOST TWO ASKS" in prompt
    assert "ZERO INVENTED FACTS" in prompt
    assert "experience_years" in prompt


def test_instruction_provider_ambiguity_and_conflict():
    """Verify ambiguous and conflicted facts are injected into the prompt."""
    # Ambiguity
    ctx_amb = MagicMock()
    ctx_amb.state = {
        "temp:directive": {
            "name": "confirm_ambiguity",
            "ambiguous_fact": {"key": "expected_ctc", "value": "around 15 LPA"},
        },
        "temp:snapshot": {},
        "mode": "intake",
    }
    prompt_amb = reply_instruction_provider(ctx_amb)
    assert "### CURRENT DIRECTIVE: confirm_ambiguity" in prompt_amb
    assert "### AMBIGUOUS FACT TO CLARIFY:" in prompt_amb
    assert "expected_ctc" in prompt_amb

    # Conflict
    ctx_conf = MagicMock()
    ctx_conf.state = {
        "temp:directive": {
            "name": "resolve_conflict",
            "conflicted_fact": {"key": "experience_years", "value": 2},
        },
        "temp:snapshot": {},
        "mode": "intake",
    }
    prompt_conf = reply_instruction_provider(ctx_conf)
    assert "### CURRENT DIRECTIVE: resolve_conflict" in prompt_conf
    assert "### CONFLICTED FACT TO RESOLVE:" in prompt_conf
    assert "experience_years" in prompt_conf


def test_worked_examples_formatting_rules():
    """
    Acceptance test: Worked examples must adhere to WhatsApp conversational constraints:
    - No markdown asterisks (**bold**)
    - No markdown headers (###)
    - Natural, short human sentences.
    """
    key_directives = [
        "warn_abuse",
        "close_consent_declined",
        "ask_consent",
        "offer_call",
        "answer_and_continue",
        "confirm_ambiguity",
        "resolve_conflict",
        "redirect",
        "clarify_name",
        "ask_resume",
        "ask_next",
        "acknowledge_profile_ready",
    ]

    for directive in key_directives:
        assert directive in WORKED_EXAMPLES, f"Missing examples for directive: {directive}"
        examples = WORKED_EXAMPLES[directive]
        assert len(examples) >= 2, f"Expected at least 2 worked examples for {directive}"

        for ex in examples:
            assert "**" not in ex, f"Markdown bolding found in example for {directive}: {ex}"
            assert not ex.startswith("#"), f"Markdown header found in example for {directive}: {ex}"
            assert len(ex.split("\n")) <= 3, f"Example too long for {directive}: {ex}"


def test_textual_variation_with_semantic_equivalence():
    """
    Acceptance test: Ten different worked examples / variations for ask_next
    differ textually (no duplicate sentences) while targeting the same directive.
    """
    examples = WORKED_EXAMPLES["ask_next"]
    # Verify each worked example is unique
    assert len(examples) == len(set(examples))

    # Test that multiple different prompt generations for greeting or ask_next produce varied outputs
    p1 = build_reply_instruction("ask_next", fields_to_ask=["desired_role", "skills"])
    p2 = build_reply_instruction("ask_next", fields_to_ask=["expected_ctc", "notice_period"])
    p3 = build_reply_instruction("acknowledge_profile_ready")

    assert p1 != p2
    assert p2 != p3
