"""
tests/test_logging_redaction.py — PII redaction tests.

Tests the JsonFormatter and _redact_phone directly for unit-testable
behaviour.  We avoid trying to capture Python logging output through
stdout since pytest intercepts it — instead we test the formatter's
format() output directly.
"""

from __future__ import annotations

import json
import logging
import io

import pytest

from app.logging import JsonFormatter, LogContext, _redact_phone


def _format_record(msg: str, level=logging.INFO, extra: dict | None = None) -> dict:
    """Helper: format a log record using JsonFormatter and return the parsed JSON."""
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=level,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    formatted = formatter.format(record)
    return json.loads(formatted)


# ---------------------------------------------------------------------------
# Unit tests for _redact_phone
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected_tail", [
    ("+919999999999", "9999"),
    ("+14155552671", "2671"),
    ("919999999999", "9999"),
    ("91 99999 99999", "9999"),
])
def test_phone_redaction_masks_all_but_last_four(raw: str, expected_tail: str):
    result = _redact_phone(raw)
    # The raw digits should not appear verbatim after redaction
    assert raw not in result
    # The last four digits should still be present for disambiguation
    assert expected_tail in result


def test_full_e164_never_appears_in_formatter_output():
    """A full E.164 phone number must be masked in any string field."""
    record = _format_record("event", extra={"phone": "+919999999999"})
    assert "+919999999999" not in json.dumps(record)
    assert "9999" in json.dumps(record)


def test_body_field_redacted_at_info():
    """The ``body`` extra field must be redacted at INFO level."""
    record = _format_record("inbound", extra={"body": "I make 10 lakhs per year"})
    assert "I make 10 lakhs" not in json.dumps(record)
    assert "<redacted>" in json.dumps(record)


def test_candidate_message_key_also_redacted():
    """The ``candidate_message`` key is also subject to body redaction."""
    record = _format_record("inbound", extra={"candidate_message": "secret text"})
    assert "secret text" not in json.dumps(record)
    assert "<redacted>" in json.dumps(record)


def test_correlation_ids_in_output():
    """LogContext injects all four correlation IDs into formatted records."""
    formatter = JsonFormatter()
    with LogContext(
        request_id="req-1",
        candidate_id="cand-1",
        conversation_id="conv-1",
        turn_id="turn-1",
    ):
        log_record = logging.LogRecord(
            name="test.correlation",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="turn_event",
            args=(),
            exc_info=None,
        )
        output = formatter.format(log_record)

    record = json.loads(output)
    assert record.get("request_id") == "req-1"
    assert record.get("candidate_id") == "cand-1"
    assert record.get("conversation_id") == "conv-1"
    assert record.get("turn_id") == "turn-1"


def test_output_is_valid_json():
    """Every formatted record must be valid JSON."""
    record = _format_record("hello", extra={"x": 1})
    assert isinstance(record, dict)
    assert record["message"] == "hello"


def test_no_correlation_outside_context():
    """Outside a LogContext, correlation IDs must be absent."""
    record = _format_record("event")
    assert "request_id" not in record
    assert "candidate_id" not in record
