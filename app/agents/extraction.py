"""
app/agents/extraction.py — Extractor LlmAgent construction and degradation guardrails (FLOW-018).

Core requirements (§8, FLOW-018 of REVIEW_AND_PLAN.md):
- Pydantic TurnExtraction structured output.
- LlmAgent with output_schema=TurnExtraction, output_key="temp:extraction", tools=[].
- Prompt carries the fact-key registry and hard rules.
- Validation failure degrades to an empty extraction rather than raising.
"""


from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.genai.types import Content

from app.agents.callbacks import before_model_callback, on_model_error_callback
from app.agents.prompts.extraction import EXTRACTOR_SYSTEM_INSTRUCTION
from app.agents.schemas import TurnExtraction
from app.config import get_settings

EXTRACTION_OUTPUT_KEY = "temp:extraction"


def ensure_valid_extraction_callback(callback_context: CallbackContext) -> Content | None:
    """
    After-agent callback to guarantee that state["temp:extraction"] is a valid,
    sanitized extraction object (or safe fallback empty extraction).

    If validation fails, gracefully degrades to empty extraction.
    """
    raw = callback_context.state.get(EXTRACTION_OUTPUT_KEY)
    parsed = TurnExtraction.safe_parse(raw)
    # Store normalized dict in state so downstream agents / serializers can read it safely
    callback_context.state[EXTRACTION_OUTPUT_KEY] = parsed.model_dump()
    return None


def create_extractor_agent(
    model: str | None = None,
    name: str = "extractor",
) -> LlmAgent:
    """
    Build the Extractor LlmAgent.

    Invariants (§8, FLOW-018, FLOW-024):
    - output_schema = TurnExtraction
    - output_key = "temp:extraction"
    - tools = [] (no tools allowed — prevents untrusted input from invoking functions)
    - Instruction carries registry + hard extraction rules.
    - after_agent_callback ensures degradation to empty extraction if parsing fails.
    - Guardrails: before_model_callback (caps size, neutralises injections), on_model_error_callback.
    """
    settings = get_settings()
    selected_model = model or settings.extractor_model

    return LlmAgent(
        name=name,
        model=selected_model,
        instruction=EXTRACTOR_SYSTEM_INSTRUCTION,
        output_schema=TurnExtraction,
        output_key=EXTRACTION_OUTPUT_KEY,
        tools=[],
        before_model_callback=before_model_callback,
        after_agent_callback=ensure_valid_extraction_callback,
        on_model_error_callback=on_model_error_callback,
    )
