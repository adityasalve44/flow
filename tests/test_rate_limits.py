"""
tests/test_rate_limits.py — Tests for rate limiting, replay protection, and daily budgets (FLOW-042).

Acceptance criteria (§8, FLOW-042 of REVIEW_AND_PLAN.md):
1. Webhook timestamps outside 5-minute window (±300s) are rejected.
2. Per-phone and per-IP limits with Postgres-backed counter (flow.rate_limit_hits).
3. A per-candidate daily model-call budget that degrades to a polite hold rather than unbounded spend.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.limits import (
    POLITE_HOLD_MESSAGE,
    check_daily_model_budget,
    is_timestamp_valid,
    record_and_check_rate_limit,
)
from app.api.schemas import InboundEvent
from app.channel.inbound import IngressDecision, process_ingress
from app.db.uow import UnitOfWork
from app.main import app
from app.models import Message
from app.models.enums import ChannelEnum, ConsentStatusEnum, DirectionEnum
from app.services.turn import TurnService


@pytest.fixture
def test_client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. 5-Minute Timestamp Window (Replay Protection)
# ---------------------------------------------------------------------------


def test_timestamp_window_validation():
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)

    # Valid: exactly now
    assert is_timestamp_valid(now, max_drift_seconds=300, now=now) is True

    # Valid: 2 minutes ago
    assert is_timestamp_valid(now - timedelta(minutes=2), max_drift_seconds=300, now=now) is True

    # Valid: 4.9 minutes ago
    assert (
        is_timestamp_valid(now - timedelta(minutes=4, seconds=50), max_drift_seconds=300, now=now)
        is True
    )

    # Valid: 2 minutes in the future (acceptable small clock drift)
    assert is_timestamp_valid(now + timedelta(minutes=2), max_drift_seconds=300, now=now) is True

    # Invalid: 5.5 minutes ago (stale replay)
    assert (
        is_timestamp_valid(now - timedelta(minutes=5, seconds=30), max_drift_seconds=300, now=now)
        is False
    )

    # Invalid: 10 minutes ago
    assert is_timestamp_valid(now - timedelta(minutes=10), max_drift_seconds=300, now=now) is False

    # Invalid: 6 minutes in the future (clock desync / fabricated)
    assert is_timestamp_valid(now + timedelta(minutes=6), max_drift_seconds=300, now=now) is False


def test_ingress_rejects_stale_timestamp(db):
    """process_ingress returns REPLAY on timestamps older than 5 minutes."""
    uow = UnitOfWork(session=db)
    phone = f"+9198{uuid4().int % 100000000:08d}"
    stale_time = datetime.now(UTC) - timedelta(minutes=6)

    event = InboundEvent(
        phone_number=phone,
        message="Hi, I am replaying an old signed message",
        channel_message_id=f"stale_{uuid4().hex}",
        timestamp=stale_time,
    )

    with uow:
        result = process_ingress(uow, event)
        assert result.decision == IngressDecision.REPLAY
        assert "5-minute replay window" in (result.detail or "")


# ---------------------------------------------------------------------------
# 2. Postgres-Backed Sliding Window Rate Limiter
# ---------------------------------------------------------------------------


def test_postgres_sliding_window_counter(db):
    key = f"phone:+9198{uuid4().int % 100000000:08d}"
    limit = 3
    window = 60
    now = datetime.now(UTC)

    # 1st request -> allowed
    assert (
        record_and_check_rate_limit(db, key=key, max_requests=limit, window_seconds=window, now=now)
        is True
    )
    # 2nd request -> allowed
    assert (
        record_and_check_rate_limit(db, key=key, max_requests=limit, window_seconds=window, now=now)
        is True
    )
    # 3rd request -> allowed (reached limit)
    assert (
        record_and_check_rate_limit(db, key=key, max_requests=limit, window_seconds=window, now=now)
        is True
    )
    # 4th request -> throttled!
    assert (
        record_and_check_rate_limit(db, key=key, max_requests=limit, window_seconds=window, now=now)
        is False
    )

    # After window passes (61 seconds later), requests are allowed again
    future = now + timedelta(seconds=61)
    assert (
        record_and_check_rate_limit(
            db, key=key, max_requests=limit, window_seconds=window, now=future
        )
        is True
    )


def test_ip_rate_limiting_endpoint(test_client, monkeypatch):
    """Rapid requests from same IP trigger HTTP 429 Too Many Requests."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "rate_limit_per_ip_per_minute", 5)

    ip_header = {"X-Forwarded-For": f"198.51.100.{uuid4().int % 250}"}

    # First 5 requests succeed (status 200)
    for _ in range(5):
        res = test_client.post("/webhook/whatsapp", json={}, headers=ip_header)
        assert res.status_code == 200

    # 6th request should be rejected with 429
    res = test_client.post("/webhook/whatsapp", json={}, headers=ip_header)
    assert res.status_code == 429
    assert "Rate limit exceeded" in res.json()["detail"]


# ---------------------------------------------------------------------------
# 3. Daily Candidate Model Budget & Polite Hold
# ---------------------------------------------------------------------------


def test_daily_candidate_model_budget_check(db):
    uow = UnitOfWork(session=db)
    phone = f"+9197{uuid4().int % 100000000:08d}"
    now = datetime.now(UTC)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        conv = uow.conversations.create(cand.id)

        # Pre-seed 3 inbound turns in the last 24h
        for i in range(3):
            msg = Message(
                conversation_id=conv.id,
                candidate_id=cand.id,
                direction=DirectionEnum.inbound,
                body=f"Turn message {i}",
                channel_message_id=f"turn_{i}_{uuid4().hex}",
                created_at=now - timedelta(hours=i),
            )
            uow.messages.add(msg)
        uow.commit()

    with uow:
        # Budget = 3 -> budget exhausted (not under budget)
        assert check_daily_model_budget(uow, cand.id, daily_limit=3, now=now) is False

        # Budget = 5 -> under budget
        assert check_daily_model_budget(uow, cand.id, daily_limit=5, now=now) is True


@pytest.mark.asyncio
async def test_turn_service_daily_budget_exhaustion_polite_hold(db, monkeypatch):
    """When daily model budget is exhausted, TurnService returns polite hold with 0 model calls."""
    phone = f"+9196{uuid4().int % 100000000:08d}"
    uow = UnitOfWork(session=db)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        uow.candidates.add(cand)
        conv = uow.conversations.create(cand.id)

        # Seed 2 inbound messages
        for i in range(2):
            uow.messages.create(
                conversation_id=conv.id,
                candidate_id=cand.id,
                direction=DirectionEnum.inbound,
                body=f"Turn {i}",
                channel_message_id=f"msg_{i}_{uuid4().hex}",
            )
        uow.commit()

    # Set daily budget to 2 (already met)
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "daily_model_call_budget", 2)

    turn_service = TurnService(uow=uow)

    # Next message should trigger polite hold
    result = await turn_service.run(
        InboundEvent(
            channel=ChannelEnum.simulator,
            phone_number=phone,
            channel_message_id=f"msg_over_budget_{uuid4().hex}",
            message="Can you tell me about the job roles?",
            timestamp=datetime.now(UTC),
        )
    )

    assert result.reply_text == POLITE_HOLD_MESSAGE
    assert result.directive == "daily_budget_exhausted"
    assert result.outbound_message_id is not None
