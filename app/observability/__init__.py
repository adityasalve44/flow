"""
app/observability — OpenTelemetry tracing, latency tracking, and token cost accounting (FLOW-040).
"""

from app.observability.cost import estimate_cost
from app.observability.plugin import FlowObservabilityPlugin
from app.observability.tracer import configure_tracer, get_tracer

__all__ = [
    "FlowObservabilityPlugin",
    "configure_tracer",
    "estimate_cost",
    "get_tracer",
]
