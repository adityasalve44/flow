"""
app/observability/tracer.py — OpenTelemetry tracer configuration (FLOW-040).

Wires the tracer for turn and agent spans. Guarantees clean correlation with
request_id, candidate_id, and conversation_id.
"""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter

_TRACER_NAME = "flow.turn"
_provider: TracerProvider | None = None


def configure_tracer(exporter: SpanExporter | None = None) -> TracerProvider:
    """Configure the OpenTelemetry TracerProvider and set global tracer."""
    global _provider
    provider = TracerProvider()
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def get_tracer(name: str = _TRACER_NAME) -> trace.Tracer:
    """Return an OpenTelemetry Tracer instance."""
    global _provider
    if _provider is None:
        configure_tracer()
    return trace.get_tracer(name)
