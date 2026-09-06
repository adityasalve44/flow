"""
tests/test_observability.py — Observability, OpenTelemetry tracing, and cost tests (FLOW-040).

Acceptance criteria:
1. A turn produces one trace with three spans (extractor, policy, replier) and a cost figure.
2. Traces carry request_id, candidate_id, conversation_id and never message content.
3. Strict PII-absence assertion over all span attributes and events.
4. Tool call counts and tool spans recorded without logging parameter payloads.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from google.adk.agents import BaseAgent
from google.adk.events import Event, EventActions
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.agents.policy import PolicyAgent
from app.agents.root import create_flow_app
from app.agents.schemas import IntentEnum, TurnExtraction
from app.channel.inbound import InboundEvent
from app.db.uow import UnitOfWork
from app.logging import LogContext
from app.models.enums import ChannelEnum, ConsentStatusEnum
from app.observability.cost import estimate_cost
from app.observability.plugin import FlowObservabilityPlugin
from app.services.turn import TurnService


@pytest.fixture
def memory_tracer():
    """Configure an isolated TracerProvider with InMemorySpanExporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.flow.turn")
    return tracer, exporter


class MockExtractor(BaseAgent):
    """Mock extractor simulating an LLM agent with usage metadata."""

    _plugin: FlowObservabilityPlugin | None = None

    def __init__(self, name: str = "extractor", plugin: FlowObservabilityPlugin | None = None):
        super().__init__(name=name)
        object.__setattr__(self, "_plugin", plugin)

    async def _run_async_impl(self, ctx):
        if self._plugin:
            mock_response = LlmResponse(
                model_version="gemini-2.5-flash",
                usage_metadata=types.GenerateContentResponseUsageMetadata(
                    prompt_token_count=1500,
                    candidates_token_count=200,
                    total_token_count=1700,
                ),
            )
            await self._plugin.after_model_callback(
                callback_context=type("Ctx", (), {"agent_name": "extractor"})(),
                llm_response=mock_response,
            )

        extraction = TurnExtraction(
            intent=IntentEnum.provide_info,
            facts=[],
            questions=[],
        )
        ctx.session.state["temp:extraction"] = extraction.model_dump()
        yield Event(
            author=self.name,
            actions=EventActions(state_delta={"temp:extraction": extraction.model_dump()}),
        )


class MockReplier(BaseAgent):
    """Mock replier simulating an LLM agent replying to candidate."""

    def __init__(self, name: str = "replier"):
        super().__init__(name=name)

    async def _run_async_impl(self, ctx):
        reply = "What is your target work location?"
        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part.from_text(text=reply)]),
        )


def test_cost_estimation():
    """Verify Gemini Flash token pricing calculation."""
    # gemini-2.5-flash: $0.075 / 1M input, $0.30 / 1M output
    cost = estimate_cost("gemini-2.5-flash", input_tokens=10_000, output_tokens=1_000)
    # (10,000 * 0.075 / 1M) + (1,000 * 0.30 / 1M) = 0.00075 + 0.00030 = 0.00105
    assert cost == 0.00105

    # Zero tokens -> 0.0
    assert estimate_cost("gemini-2.5-flash", 0, 0) == 0.0


@pytest.mark.asyncio
async def test_turn_produces_one_trace_with_three_spans_and_cost(db, memory_tracer):
    """Headline acceptance criterion: One turn produces one trace with 3 sub-spans and cost figure."""
    tracer, exporter = memory_tracer
    obs_plugin = FlowObservabilityPlugin(tracer=tracer)

    phone = f"+9198{uuid4().int % 100000000:08d}"
    uow = UnitOfWork(session=db)
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        uow.candidates.add(cand)
        uow.commit()

    session_service = InMemorySessionService()
    app = create_flow_app(
        custom_extractor=MockExtractor(plugin=obs_plugin),
        custom_policy=PolicyAgent(session_factory=lambda: db),
        custom_replier=MockReplier(),
        plugins=[obs_plugin],
    )

    turn_service = TurnService(
        uow=uow,
        session_service=session_service,
        app=app,
    )

    req_id = f"req-{uuid4()}"
    with LogContext(request_id=req_id):
        res = await turn_service.run(
            InboundEvent(
                channel=ChannelEnum.simulator,
                phone_number=phone,
                channel_message_id=f"msg-{uuid4()}",
                message="I am looking for backend engineering roles in Bengaluru",
                timestamp=datetime.now(UTC),
            )
        )
        assert res.reply_text != ""

    spans = exporter.get_finished_spans()
    assert len(spans) >= 4  # 1 root turn span + 3 sub-agent spans (extractor, policy, replier)

    # 1. Root turn span
    turn_span = next(s for s in spans if s.name == "turn")
    assert turn_span.attributes.get("request_id") == req_id
    assert turn_span.attributes.get("candidate_id") == str(cand.id)
    assert turn_span.attributes.get("turn.status") == "ok"
    assert "turn.latency_ms" in turn_span.attributes
    assert turn_span.attributes.get("turn.cost_usd") > 0.0

    # 2. Three sub-agent spans share the SAME trace_id as turn_span
    agent_span_names = {"extractor", "policy", "replier"}
    agent_spans = [s for s in spans if s.name in agent_span_names]
    found_agent_names = {s.name for s in agent_spans}
    assert found_agent_names == agent_span_names

    for a_span in agent_spans:
        assert a_span.context.trace_id == turn_span.context.trace_id
        assert a_span.parent.span_id == turn_span.context.span_id
        assert "agent.latency_ms" in a_span.attributes
        assert a_span.attributes.get("request_id") == req_id
        assert a_span.attributes.get("candidate_id") == str(cand.id)


@pytest.mark.asyncio
async def test_pii_absence_in_all_span_attributes(db, memory_tracer):
    """Strict PII-absence assertion: prompt, completion, candidate phone, and message body NEVER leak to spans."""
    tracer, exporter = memory_tracer
    obs_plugin = FlowObservabilityPlugin(tracer=tracer)

    secret_phone = "+919876543210"
    secret_candidate_text = "My secret message body that must never appear in telemetry"

    uow = UnitOfWork(session=db)
    with uow:
        cand = uow.candidates.get_or_create_by_phone(secret_phone)
        cand.consent_status = ConsentStatusEnum.granted
        uow.candidates.add(cand)
        uow.commit()

    session_service = InMemorySessionService()
    app = create_flow_app(
        custom_extractor=MockExtractor(),
        custom_policy=PolicyAgent(session_factory=lambda: db),
        custom_replier=MockReplier(),
        plugins=[obs_plugin],
    )

    turn_service = TurnService(uow=uow, session_service=session_service, app=app)

    with LogContext(request_id="req-pii-test"):
        await turn_service.run(
            InboundEvent(
                channel=ChannelEnum.simulator,
                phone_number=secret_phone,
                channel_message_id=f"msg-{uuid4()}",
                message=secret_candidate_text,
                timestamp=datetime.now(UTC),
            )
        )

    spans = exporter.get_finished_spans()
    assert len(spans) >= 4

    # Comprehensive PII-absence inspection across all spans and attributes
    for span in spans:
        for attr_key, attr_val in span.attributes.items():
            val_str = str(attr_val)
            assert secret_phone not in val_str, (
                f"Phone number leaked in span attribute {attr_key}: {val_str}"
            )
            assert secret_candidate_text not in val_str, (
                f"Message content leaked in span attribute {attr_key}: {val_str}"
            )
            # Ensure prompt / completion text keys are not present
            assert attr_key not in (
                "prompt",
                "completion",
                "message_body",
                "raw_text",
                "user_content",
            )

        for event in span.events:
            for _ev_key, ev_val in event.attributes.items():
                ev_str = str(ev_val)
                assert secret_phone not in ev_str
                assert secret_candidate_text not in ev_str


@pytest.mark.asyncio
async def test_tool_call_spans_without_payloads(memory_tracer):
    """Tool calls generate spans with duration and name, never raw parameter payloads."""
    tracer, exporter = memory_tracer
    obs_plugin = FlowObservabilityPlugin(tracer=tracer)

    # Start a mock turn span
    invocation_ctx = type("InvocCtx", (), {})()
    await obs_plugin.before_run_callback(invocation_context=invocation_ctx)

    mock_tool = type("MockTool", (), {"name": "snapshot"})()
    tool_context = type("ToolContext", (), {"agent_name": "replier"})()

    secret_payload = {"phone_number": "+919876543210", "candidate_id": "secret-123"}
    await obs_plugin.before_tool_callback(
        tool=mock_tool,
        tool_args=secret_payload,
        tool_context=tool_context,
    )

    await obs_plugin.after_tool_callback(
        tool=mock_tool,
        tool_args=secret_payload,
        tool_context=tool_context,
        result={"status": "success", "profile": {"role": "Engineer"}},
    )

    await obs_plugin.after_run_callback(invocation_context=invocation_ctx)

    spans = exporter.get_finished_spans()
    tool_span = next(s for s in spans if s.name == "tool:snapshot")
    assert tool_span is not None
    assert tool_span.attributes.get("tool.name") == "snapshot"
    assert tool_span.attributes.get("tool.success") is True
    assert "tool.duration_ms" in tool_span.attributes

    # Verify tool_args or result payload are absent from span attributes
    for k, v in tool_span.attributes.items():
        assert "+919876543210" not in str(v)
        assert "secret_payload" not in str(k)
        assert "result" not in str(k)
