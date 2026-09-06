"""
app/observability/plugin.py — ADK Observability Plugin (FLOW-040).

Hooks into ADK execution to produce:
1. One trace per turn with three child spans (extractor, policy, replier) and tool spans.
2. Latency, token counts, and estimated cost per turn and per agent.
3. Strict PII-absence guarantee: zero message bodies, prompts, responses, or phone numbers.
"""

import time
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from opentelemetry import trace
from opentelemetry.trace import Span, StatusCode

from app.logging import get_candidate_id, get_conversation_id, get_request_id
from app.observability.cost import estimate_cost
from app.observability.tracer import get_tracer


class FlowObservabilityPlugin(BasePlugin):
    """Custom ADK plugin wiring OpenTelemetry tracing, latency, and cost tracking."""

    def __init__(self, tracer: trace.Tracer | None = None) -> None:
        super().__init__(name="flow_observability")
        self.tracer = tracer or get_tracer()
        self._turn_span: Span | None = None
        self._agent_spans: dict[str, Span] = {}
        self._agent_start_times: dict[str, float] = {}
        self._tool_spans: dict[str, Span] = {}
        self._tool_start_times: dict[str, float] = {}

        # Turn metrics accumulators
        self._turn_start_time: float = 0.0
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._total_cost_usd: float = 0.0
        self._tool_calls_count: int = 0
        self._last_model_id: str | None = None

    def _get_correlation_ids(self) -> dict[str, str]:
        """Extract request correlation IDs without any PII."""
        ids: dict[str, str] = {}
        req_id = get_request_id()
        if req_id:
            ids["request_id"] = str(req_id)
        cand_id = get_candidate_id()
        if cand_id:
            ids["candidate_id"] = str(cand_id)
        conv_id = get_conversation_id()
        if conv_id:
            ids["conversation_id"] = str(conv_id)
        return ids

    # -----------------------------------------------------------------------
    # Run lifecycle (Turn root span)
    # -----------------------------------------------------------------------

    async def before_run_callback(self, *, invocation_context: InvocationContext) -> Any:
        self._turn_start_time = time.time()
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cost_usd = 0.0
        self._tool_calls_count = 0
        self._agent_spans.clear()
        self._agent_start_times.clear()

        # Start root turn span
        self._turn_span = self.tracer.start_span("turn")
        correlation = self._get_correlation_ids()
        for k, v in correlation.items():
            self._turn_span.set_attribute(k, v)
        self._turn_span.set_attribute("turn.status", "running")
        return None

    async def after_run_callback(self, *, invocation_context: InvocationContext) -> None:
        if self._turn_span is not None:
            latency_ms = round((time.time() - self._turn_start_time) * 1000.0, 2)
            self._turn_span.set_attribute("turn.latency_ms", latency_ms)
            self._turn_span.set_attribute("turn.tokens.input", self._total_input_tokens)
            self._turn_span.set_attribute("turn.tokens.output", self._total_output_tokens)
            self._turn_span.set_attribute(
                "turn.tokens.total", self._total_input_tokens + self._total_output_tokens
            )
            self._turn_span.set_attribute("turn.cost_usd", round(self._total_cost_usd, 6))
            self._turn_span.set_attribute("turn.tool_calls_count", self._tool_calls_count)
            self._turn_span.set_attribute("turn.status", "ok")
            self._turn_span.set_status(StatusCode.OK)
            self._turn_span.end()
            self._turn_span = None

    async def on_run_error_callback(
        self, *, invocation_context: InvocationContext, error: Exception
    ) -> None:
        if self._turn_span is not None:
            self._turn_span.set_attribute("turn.status", "error")
            self._turn_span.set_attribute("error.type", error.__class__.__name__)
            self._turn_span.set_status(StatusCode.ERROR, str(error.__class__.__name__))
            self._turn_span.end()
            self._turn_span = None

    # -----------------------------------------------------------------------
    # Agent lifecycle (Sub-spans: extractor, policy, replier)
    # -----------------------------------------------------------------------

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Any:
        agent_name = getattr(agent, "name", "agent")
        # Do not create duplicate nested span for the outer pipeline container itself
        if agent_name in ("flow_pipeline", "flow"):
            return None

        # Parent context from turn span
        parent_ctx = None
        if self._turn_span is not None:
            parent_ctx = trace.set_span_in_context(self._turn_span)

        span = self.tracer.start_span(agent_name, context=parent_ctx)
        correlation = self._get_correlation_ids()
        for k, v in correlation.items():
            span.set_attribute(k, v)
        span.set_attribute("agent.name", agent_name)

        self._agent_spans[agent_name] = span
        self._agent_start_times[agent_name] = time.time()
        return None

    async def after_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Any:
        agent_name = getattr(agent, "name", "agent")
        if agent_name in ("flow_pipeline", "flow"):
            return None

        span = self._agent_spans.pop(agent_name, None)
        start_time = self._agent_start_times.pop(agent_name, None)

        if span is not None:
            if start_time:
                latency_ms = round((time.time() - start_time) * 1000.0, 2)
                span.set_attribute("agent.latency_ms", latency_ms)

            # Record operational policy decisions on policy span without PII
            if agent_name == "policy" and hasattr(callback_context, "state"):
                directive = callback_context.state.get("temp:directive")
                if directive and isinstance(directive, dict):
                    span.set_attribute("policy.directive", str(directive.get("name", "")))
                elif directive:
                    span.set_attribute("policy.directive", str(directive))

            span.set_status(StatusCode.OK)
            span.end()
        return None

    async def on_agent_error_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext, error: Exception
    ) -> None:
        agent_name = getattr(agent, "name", "agent")
        span = self._agent_spans.pop(agent_name, None)
        if span is not None:
            span.set_attribute("error.type", error.__class__.__name__)
            span.set_status(StatusCode.ERROR, str(error.__class__.__name__))
            span.end()

    # -----------------------------------------------------------------------
    # Model callbacks (Token usage & cost estimation)
    # -----------------------------------------------------------------------

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> Any:
        if llm_response is None:
            return None

        usage = getattr(llm_response, "usage_metadata", None)
        model_version = getattr(llm_response, "model_version", None) or "gemini-2.5-flash"
        self._last_model_id = model_version

        input_tokens = 0
        output_tokens = 0
        if usage:
            input_tokens = getattr(usage, "prompt_token_count", 0) or 0
            output_tokens = getattr(usage, "candidates_token_count", 0) or 0

        self._total_input_tokens += input_tokens
        self._total_output_tokens += output_tokens

        cost = estimate_cost(model_version, input_tokens, output_tokens)
        self._total_cost_usd += cost

        # Attribute model metrics to the currently executing sub-agent span
        agent_name = getattr(callback_context, "agent_name", None)
        if agent_name and agent_name in self._agent_spans:
            span = self._agent_spans[agent_name]
            span.set_attribute("model.id", model_version)
            span.set_attribute("model.tokens.input", input_tokens)
            span.set_attribute("model.tokens.output", output_tokens)
            span.set_attribute("model.cost_usd", cost)

        return None

    # -----------------------------------------------------------------------
    # Tool callbacks (Tool spans & counts — strictly no payload PII)
    # -----------------------------------------------------------------------

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        self._tool_calls_count += 1
        tool_name = getattr(tool, "name", "tool")

        # Parent to currently running agent span or turn span
        parent_span = self._turn_span
        agent_name = getattr(tool_context, "agent_name", None)
        if agent_name and agent_name in self._agent_spans:
            parent_span = self._agent_spans[agent_name]

        parent_ctx = None
        if parent_span is not None:
            parent_ctx = trace.set_span_in_context(parent_span)

        span = self.tracer.start_span(f"tool:{tool_name}", context=parent_ctx)
        span.set_attribute("tool.name", tool_name)
        correlation = self._get_correlation_ids()
        for k, v in correlation.items():
            span.set_attribute(k, v)

        # Invariant: tool_args is NEVER logged or saved as attribute (PII defense)
        self._tool_spans[tool_name] = span
        self._tool_start_times[tool_name] = time.time()
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> Any:
        tool_name = getattr(tool, "name", "tool")
        span = self._tool_spans.pop(tool_name, None)
        start_time = self._tool_start_times.pop(tool_name, None)

        if span is not None:
            if start_time:
                duration_ms = round((time.time() - start_time) * 1000.0, 2)
                span.set_attribute("tool.duration_ms", duration_ms)
            span.set_attribute("tool.success", True)
            # Invariant: result payload is NEVER recorded as attribute (PII defense)
            span.set_status(StatusCode.OK)
            span.end()

        return None
