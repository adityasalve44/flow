"""
app/agents/reply.py — Reply LlmAgent construction and instruction wiring (FLOW-020).

Core requirements (§8, FLOW-020 of REVIEW_AND_PLAN.md):
- LlmAgent with dynamic instruction provider injecting the chosen directive.
- Generates natural, human-styled WhatsApp prose without markdown.
- At most 2 asks, zero invented facts, never confirms a job exists.
"""

from typing import Any, Callable

from google.adk.agents import LlmAgent

from app.agents.prompts.reply import reply_instruction_provider
from app.config import get_settings


def create_reply_agent(
    model: str | None = None,
    name: str = "replier",
    tools: list[Any] | None = None,
    instruction_provider: Callable[..., Any] | None = None,
) -> LlmAgent:
    """
    Build the Reply LlmAgent.

    Invariants (§8, FLOW-020):
    - Uses replier_model from settings by default.
    - Instruction is dynamically produced by reply_instruction_provider.
    - Generates one natural WhatsApp message fulfilling the directive.
    """
    settings = get_settings()
    selected_model = model or settings.replier_model
    provider = instruction_provider or reply_instruction_provider

    return LlmAgent(
        name=name,
        model=selected_model,
        instruction=provider,
        tools=tools or [],
    )
