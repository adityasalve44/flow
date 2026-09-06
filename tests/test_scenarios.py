"""
tests/test_scenarios.py — End-to-end multi-turn conversation scenario test suite (FLOW-032).

Encodes all 23 standard scenarios from §15 of REVIEW_AND_PLAN.md.
Validates:
1. Each scenario runs end-to-end within an isolated transactional database session.
2. Structured assertions on directives, states, deflections, abuse, staleness, and attributes.
3. ADK tool trajectory validation for Flow tools (snapshot, glossary, history).
"""

import pytest

from evals.runner import load_scenarios, run_scenario

ALL_SCENARIOS = load_scenarios()


@pytest.mark.parametrize(
    "scenario",
    ALL_SCENARIOS,
    ids=[f"scenario_{s['id']:02d}_{s['name']}" for s in ALL_SCENARIOS],
)
@pytest.mark.asyncio
async def test_scenario_scripted_conversation(db, scenario):
    """
    Execute a scripted multi-turn conversation scenario from §15.
    Asserts database state and directive sequence, never generated text.
    """
    result = await run_scenario(scenario, db)
    assert result.passed, f"Scenario #{result.scenario_id} '{result.name}' failed: {result.error}"


def test_scenario_suite_completeness():
    """Verify that all 23 scenarios from §15 are present and unique."""
    assert len(ALL_SCENARIOS) == 23
    scenario_ids = [s["id"] for s in ALL_SCENARIOS]
    assert scenario_ids == list(range(1, 24))
    assert len(set(s["name"] for s in ALL_SCENARIOS)) == 23


def test_tool_trajectory_declarations():
    """
    Tool trajectory validation: Verify all V1 tools conform to ADK specifications.
    Asserts no identity parameters are exposed to LLMs.
    """
    from google.adk.tools import FunctionTool
    from app.tools import FLOW_V1_TOOLS

    assert len(FLOW_V1_TOOLS) == 4
    tool_names = {t.__name__ for t in FLOW_V1_TOOLS}
    assert tool_names == {
        "get_candidate_snapshot",
        "explain_recruitment_term",
        "recall_candidate_history",
        "record_resume_confirmation",
    }

    for tool_callable in FLOW_V1_TOOLS:
        adk_tool = FunctionTool(tool_callable)
        decl = adk_tool._get_declaration()

        # Invariant: Never accept phone_number, candidate_id, or org_id from model
        exposed_props = set((decl.parameters.properties if decl.parameters else {}).keys())
        forbidden_props = {"phone_number", "candidate_id", "org_id", "tool_context"}
        assert not (exposed_props & forbidden_props), f"Tool {decl.name} exposed sensitive param: {exposed_props}"
