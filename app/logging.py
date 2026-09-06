"""
app/logging.py — structured JSON logging with PII redaction.

All log output is newline-delimited JSON.  A single contextvars-based
LogContext carries per-request correlation IDs; every record produced during
a request automatically includes those IDs.

PII rules:
  - Phone numbers (E.164 format or similar) are masked to their last 4 digits.
  - ``message`` field (candidate text) is never logged at INFO or below.
  - Any field whose key is ``body``, ``text``, or ``message`` is redacted
    at INFO and below unless the log level is DEBUG and the env is development.

Usage:
    from app.logging import get_logger, LogContext

    logger = get_logger(__name__)

    with LogContext(request_id="abc", candidate_id="uuid"):
        logger.info("turn_started", extra={"turn": 1})
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Context variables
# ---------------------------------------------------------------------------

_ctx_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_ctx_candidate_id: ContextVar[str | None] = ContextVar("candidate_id", default=None)
_ctx_conversation_id: ContextVar[str | None] = ContextVar("conversation_id", default=None)
_ctx_turn_id: ContextVar[str | None] = ContextVar("turn_id", default=None)


class LogContext:
    """Context manager that sets per-request correlation IDs in contextvars."""

    def __init__(
        self,
        *,
        request_id: str | None = None,
        candidate_id: str | None = None,
        conversation_id: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        self._tokens: list = []
        self._values = {
            _ctx_request_id: request_id,
            _ctx_candidate_id: candidate_id,
            _ctx_conversation_id: conversation_id,
            _ctx_turn_id: turn_id,
        }

    def __enter__(self) -> LogContext:
        for var, value in self._values.items():
            if value is not None:
                self._tokens.append(var.set(value))
        return self

    def __exit__(self, *_) -> None:
        for token in reversed(self._tokens):
            token.var.reset(token)


def get_request_id() -> str | None:
    return _ctx_request_id.get()


def get_candidate_id() -> str | None:
    return _ctx_candidate_id.get()


def get_conversation_id() -> str | None:
    return _ctx_conversation_id.get()


def get_turn_id() -> str | None:
    return _ctx_turn_id.get()


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------

# Matches E.164 phone numbers and common loose formats (+91XXXXXXXXXX, etc.)
_PHONE_RE = re.compile(r"(\+?[0-9]{1,3}[-.\s]?)?(\(?[0-9]{1,4}\)?[-.\s]?){2,}[0-9]{4,}")


def _redact_phone(value: str) -> str:
    """Mask all but the last 4 digits of every phone-number-like sequence."""

    def _mask(m: re.Match) -> str:
        full = m.group(0)
        digits_only = re.sub(r"\D", "", full)
        if len(digits_only) >= 7:  # long enough to be a phone number
            return "****" + digits_only[-4:]
        return full

    return _PHONE_RE.sub(_mask, value)


# Keys whose values are always redacted (candidate free text).
# Note: "message" is reserved by logging.LogRecord — use "candidate_message"
# or "body" when logging candidate input.
_BODY_KEYS = frozenset({"body", "text", "candidate_message", "content", "reply"})


def _redact_record(record: dict, level: int) -> dict:
    """Return a shallow copy with PII fields scrubbed."""
    out = {}
    for k, v in record.items():
        if isinstance(v, str):
            # Always mask phone numbers in string values
            v = _redact_phone(v)
            # Redact message bodies unless DEBUG
            if k in _BODY_KEYS and level >= logging.INFO:
                v = "<redacted>"
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON object on stdout."""

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Inject correlation IDs from context
        for key, var in [
            ("request_id", _ctx_request_id),
            ("candidate_id", _ctx_candidate_id),
            ("conversation_id", _ctx_conversation_id),
            ("turn_id", _ctx_turn_id),
        ]:
            val = var.get()
            if val is not None:
                obj[key] = val

        # Merge any ``extra`` fields, then redact
        extra = {
            k: v
            for k, v in record.__dict__.items()
            if k not in logging.LogRecord("", 0, "", 0, "", (), None).__dict__
            and not k.startswith("_")
        }
        obj.update(extra)
        obj = _redact_record(obj, record.levelno)

        if record.exc_info:
            obj["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(obj, default=str)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger.

    Call once at application startup (e.g. from main.py lifespan).
    Subsequent calls are idempotent per level.
    """
    root = logging.getLogger()
    # Check whether our formatter is already installed (not just any handler)
    already_installed = any(
        isinstance(h, logging.StreamHandler) and isinstance(h.formatter, JsonFormatter)
        for h in root.handlers
    )
    if already_installed:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_logger(name: str) -> logging.Logger:
    """Return a module logger.  configure_logging() must be called first."""
    return logging.getLogger(name)
