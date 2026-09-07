"""
app/agents/reply.py — Reply LlmAgent construction and instruction wiring (FLOW-020).

Core requirements (§8, FLOW-020 of REVIEW_AND_PLAN.md):
- LlmAgent with dynamic instruction provider injecting the chosen directive.
- Generates natural, human-styled WhatsApp prose without markdown.
- At most 2 asks, zero invented facts, never confirms a job exists.
"""

from collections.abc import Callable
from typing import Any

from google.adk.agents import LlmAgent

from app.agents.callbacks import (
    after_tool_callback,
    before_model_callback,
    before_tool_callback,
    on_model_error_callback,
)
from app.agents.models import resolve_model
from app.agents.prompts.reply import reply_instruction_provider


def create_reply_agent(
    model: object | None = None,
    name: str = "replier",
    tools: list[Any] | None = None,
    instruction_provider: Callable[..., Any] | None = None,
) -> LlmAgent:
    """
    Build the Reply LlmAgent.

    Invariants (§8, FLOW-020, FLOW-024):
    - Uses replier_model from settings by default.
    - Instruction is dynamically produced by reply_instruction_provider.
    - Generates one natural WhatsApp message fulfilling the directive.
    - Guardrails:
        * before_model_callback (size cap, injection neutralisation, token count)
        * before_tool_callback / after_tool_callback (structured tool execution audit logging)
        * on_model_error_callback (safe degradation without crashing)
    """
    selected_model = resolve_model("replier", override=model)
    provider = instruction_provider or reply_instruction_provider

    return LlmAgent(
        name=name,
        model=selected_model,
        instruction=provider,
        tools=tools or [],
        before_model_callback=before_model_callback,
        before_tool_callback=before_tool_callback,
        after_tool_callback=after_tool_callback,
        on_model_error_callback=on_model_error_callback,
    )
