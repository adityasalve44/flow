"""
app/agents/callbacks.py — Guardrail callbacks defending model boundary (FLOW-024).

Core requirements (§8, FLOW-024 of REVIEW_AND_PLAN.md):
- before_model_callback:
    * Cap input size (e.g. 50KB input truncated to safe limit).
    * Strip or neutralise instruction-injection markers.
    * Count estimated tokens.
- after_tool_callback:
    * Structured audit log of tool name, duration (ms), and result shape — NEVER the payload.
- on_model_error_callback:
    * Structured logging of model errors and trigger safe fallback path.
"""

import math
import re
import time
from typing import Any

from google.adk.agents.context import Context
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.genai import types

from app.logging import get_logger

logger = get_logger(__name__)

SAFE_FALLBACK_REPLY = (
    "Thanks for your message! We're experiencing a brief technical delay on our end. "
    "Please send your message again in a moment."
)

# Maximum allowed text length for any individual input part (4KB)
MAX_INPUT_TEXT_LENGTH = 4096

# Patterns indicating prompt injection or privilege escalation attempts
INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"system\s*prompt\s*:", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+in\s+developer\s+mode", re.IGNORECASE),
    re.compile(r"<\|im_start\|>", re.IGNORECASE),
    re.compile(r"<\|im_end\|>", re.IGNORECASE),
    re.compile(r"\[INST\]", re.IGNORECASE),
    re.compile(r"\[/INST\]", re.IGNORECASE),
    re.compile(r"###\s*(system|instruction):", re.IGNORECASE),
    re.compile(r"(^|\n)system\s*:", re.IGNORECASE),
    re.compile(r"admin\s+override\s*:", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(prior|previous)\s+rules", re.IGNORECASE),
]


def neutralise_injection_markers(text: str) -> str:
    """Neutralise known instruction-injection markers."""
    cleaned = text
    for pattern in INJECTION_PATTERNS:
        cleaned = pattern.sub("[FILTERED_INJECTION_ATTEMPT]", cleaned)
    return cleaned


def estimate_tokens(text: str) -> int:
    """Fast conservative heuristic for token counting (~4 characters per token)."""
    if not text:
        return 0
    return math.ceil(len(text) / 4.0)


def before_model_callback(
    callback_context: Context,
    llm_request: LlmRequest,
) -> LlmResponse | None:
    """Guardrail callback executed before sending request to the LLM.

    Guarantees:
    1. Truncates input text exceeding MAX_INPUT_TEXT_LENGTH (e.g. 50KB messages).
    2. Neutralises instruction-injection markers.
    3. Computes estimated token count and records in session state / telemetry.
    """
    # Bypass model call completely on disengage_silent directive
    state = getattr(callback_context, "state", None)
    if state is not None:
        directive = state.get("temp:directive")
        if isinstance(directive, dict) and directive.get("name") == "disengage_silent":
            return LlmResponse(content=types.Content(role="model", parts=[]))

    total_tokens = 0

    if llm_request.contents:
        for content in llm_request.contents:
            if not getattr(content, "parts", None):
                continue
            for part in content.parts:
                text = getattr(part, "text", None)
                if not text:
                    continue

                # 1. Cap input size (e.g. 50KB truncated)
                if len(text) > MAX_INPUT_TEXT_LENGTH:
                    text = text[:MAX_INPUT_TEXT_LENGTH] + "\n[TRUNCATED: Input exceeded maximum allowed size]"

                # 2. Strip / neutralise instruction injection markers
                text = neutralise_injection_markers(text)

                part.text = text
                total_tokens += estimate_tokens(text)

    # Store token count for accounting / audit
    state = getattr(callback_context, "state", None)
    if state is not None:
        state["last_turn_estimated_tokens"] = total_tokens
        state["total_estimated_tokens"] = state.get("total_estimated_tokens", 0) + total_tokens

    return None


def before_tool_callback(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: Context,
) -> dict[str, Any] | None:
    """Record tool invocation start timestamp for audit latency tracking."""
    tool_name = getattr(tool, "name", str(tool))
    state = getattr(tool_context, "state", None)
    if state is not None:
        state[f"_tool_start_{tool_name}"] = time.perf_counter()
    return None


def after_tool_callback(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: Context,
    response: dict[str, Any],
) -> dict[str, Any] | None:
    """Audit tool execution: log tool name, duration, and result shape — NEVER the payload.

    Prevents sensitive candidate data or raw PII from appearing in tool logs.
    """
    tool_name = getattr(tool, "name", str(tool))
    start_time = None
    state = getattr(tool_context, "state", None)
    if state is not None:
        start_time = state.pop(f"_tool_start_{tool_name}", None)

    duration_ms = (time.perf_counter() - start_time) * 1000.0 if start_time else 0.0

    # Derive shape descriptor (keys, item count, status) without logging any payload values
    if isinstance(response, dict):
        result_shape = {
            "type": "dict",
            "keys": sorted(list(response.keys())),
            "found": response.get("found"),
        }
    elif isinstance(response, (list, set, tuple)):
        result_shape = {
            "type": type(response).__name__,
            "count": len(response),
        }
    else:
        result_shape = {
            "type": type(response).__name__,
        }

    logger.info(
        "tool_audit",
        extra={
            "audit": True,
            "tool_name": tool_name,
            "duration_ms": round(duration_ms, 2),
            "result_shape": result_shape,
        },
    )
    return None


def on_model_error_callback(
    callback_context: Context,
    llm_request: LlmRequest,
    error: Exception,
) -> LlmResponse | None:
    """Structured error handling when model fails; triggers safe fallback response."""
    model_name = getattr(llm_request, "model", "unknown")
    logger.error(
        f"Model execution error on model {model_name}: {error}",
        extra={
            "audit": True,
            "model": model_name,
            "error_type": type(error).__name__,
        },
        exc_info=True,
    )

    # Return safe degradation response
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=SAFE_FALLBACK_REPLY)],
        )
    )
