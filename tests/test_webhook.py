"""
tests/test_webhook.py — Channel webhook endpoint tests (FLOW-023).

Tests:
1. Health check (GET /health) returns 200 OK.
2. Shared-secret authentication:
   - Rejected when secret is configured and header is missing or wrong (401).
   - Accepted with X-Webhook-Secret or Authorization: Bearer.
3. Ingress payload validation:
   - Invalid phone number (E.164) rejected with 422.
   - Missing channel_message_id rejected with 422.
   - Oversized message (> 4096) rejected with 422.
4. Boundary defense & idempotency:
   - Replay with duplicate channel_message_id returns 200 with decision="replay".
   - Zero duplicate turn executions or extra messages created.
   - Blocked candidate returns decision="blocked".
   - Rate limited candidate returns 429 with decision="rate_limited".
5. Full turn integration:
   - Round trip yields reply text and 2 messages in the database.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from app.api.webhook import get_turn_service
from app.config import get_settings
from app.database import get_db
from app.db.uow import UnitOfWork
from app.main import app
from app.models.enums import ChannelEnum, DirectionEnum
from app.services.turn import TurnService


@pytest.fixture
def client(db):
    """Test client with db and session service dependency overrides."""
    uow = UnitOfWork(session=db)
    session_service = InMemorySessionService()
    test_turn_service = TurnService(uow=uow, session_service=session_service)

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_turn_service] = lambda: test_turn_service

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def test_health_check(client):
    """GET /health returns 200 and operational status."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["app"] == "flow"


def test_webhook_auth_enforcement(client, monkeypatch):
    """
    Acceptance test (FLOW-023):
    When webhook_secret is configured, unauthenticated requests are rejected with 401.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "webhook_secret", "secret-token-12345")

    phone = f"+9198{uuid4().int % 100000000:08d}"
    payload = {
        "phone_number": phone,
        "contact_name": "Test User",
        "message": "Hello Flow",
        "channel_message_id": f"msg-{uuid4()}",
    }

    # 1. Missing secret header -> 401
    res_no_auth = client.post("/webhook", json=payload)
    assert res_no_auth.status_code == 401
    assert "authentication" in res_no_auth.json()["detail"].lower()

    # 2. Wrong secret header -> 401
    res_wrong_auth = client.post(
        "/webhook",
        json=payload,
        headers={"X-Webhook-Secret": "invalid-secret"},
    )
    assert res_wrong_auth.status_code == 401

    # 3. Valid X-Webhook-Secret header -> 200
    res_valid_header = client.post(
        "/webhook",
        json=payload,
        headers={"X-Webhook-Secret": "secret-token-12345"},
    )
    assert res_valid_header.status_code == 200
    assert res_valid_header.json()["decision"] == "proceed"

    # 4. Valid Authorization Bearer header -> 200
    payload["channel_message_id"] = f"msg-{uuid4()}"
    res_valid_bearer = client.post(
        "/webhook",
        json=payload,
        headers={"Authorization": "Bearer secret-token-12345"},
    )
    assert res_valid_bearer.status_code == 200
    assert res_valid_bearer.json()["decision"] == "proceed"


def test_webhook_validation_errors(client):
    """Invalid payload formats fail validation before any business logic runs."""
    # Invalid phone number (cannot resolve to E.164)
    res_invalid_phone = client.post(
        "/webhook",
        json={
            "phone_number": "not-a-phone-number",
            "message": "Hello",
            "channel_message_id": f"msg-{uuid4()}",
        },
    )
    assert res_invalid_phone.status_code == 422

    # Missing channel_message_id
    res_missing_msg_id = client.post(
        "/webhook",
        json={
            "phone_number": "+919876543210",
            "message": "Hello",
        },
    )
    assert res_missing_msg_id.status_code == 422

    # Message exceeds length limit
    res_oversized = client.post(
        "/webhook",
        json={
            "phone_number": "+919876543210",
            "message": "a" * 5000,
            "channel_message_id": f"msg-{uuid4()}",
        },
    )
    assert res_oversized.status_code == 422


def test_webhook_idempotency_replay(client, db):
    """
    Acceptance test (FLOW-023, FLOW-015):
    Replaying a message with an identical channel_message_id returns 200 OK
    with decision='replay' and creates zero duplicate rows in messages.
    """
    phone = f"+9198{uuid4().int % 100000000:08d}"
    channel_msg_id = f"msg-idem-{uuid4()}"
    payload = {
        "phone_number": phone,
        "contact_name": "Idempotent Candidate",
        "message": "Hi, I am interested in software jobs",
        "channel_message_id": channel_msg_id,
    }

    # 1. First call: processed
    res1 = client.post("/webhook", json=payload)
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["status"] == "ok"
    assert data1["decision"] == "proceed"
    assert data1["reply_text"] is not None
    conv_id = data1["conversation_id"]

    # In DB: exactly 2 messages (inbound + outbound)
    uow = UnitOfWork(session=db)
    with uow:
        msgs = uow.messages.get_recent(conv_id)
        assert len(msgs) == 2

    # 2. Replay call with exact same channel_message_id: no-op
    res2 = client.post("/webhook", json=payload)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "ok"
    assert data2["decision"] == "replay"
    assert "replay" in data2["detail"].lower()

    # In DB: still exactly 2 messages (no duplicate inserted)
    with uow:
        msgs = uow.messages.get_recent(conv_id)
        assert len(msgs) == 2


def test_webhook_blocked_candidate(client, db):
    """Candidate with blocked_at timestamp is dropped at ingress."""
    phone = f"+9198{uuid4().int % 100000000:08d}"
    uow = UnitOfWork(session=db)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.blocked_at = datetime.now(UTC)
        uow.candidates.add(cand)
        uow.commit()

    payload = {
        "phone_number": phone,
        "message": "Spam message",
        "channel_message_id": f"msg-blocked-{uuid4()}",
    }

    res = client.post("/webhook", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["decision"] == "blocked"
    assert "blocked" in data["detail"].lower()


def test_webhook_rate_limited_candidate(client, db):
    """Candidate exceeding per-minute threshold receives 429 Too Many Requests."""
    phone = f"+9198{uuid4().int % 100000000:08d}"
    uow = UnitOfWork(session=db)
    t0 = datetime.now(UTC)

    # Seed 20 inbound messages in the last 60 seconds
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        conv = uow.conversations.create(candidate_id=cand.id, channel=ChannelEnum.simulator)
        for i in range(20):
            uow.messages.create(
                conversation_id=conv.id,
                candidate_id=cand.id,
                direction=DirectionEnum.inbound,
                channel_message_id=f"seed-msg-{uuid4()}",
                body=f"seed {i}",
            )
        uow.commit()

    payload = {
        "phone_number": phone,
        "message": "Message over rate limit",
        "channel_message_id": f"msg-ratelimit-{uuid4()}",
    }

    res = client.post("/webhook", json=payload)
    assert res.status_code == 429
    data = res.json()
    assert data["decision"] == "rate_limited"
    assert "rate limit" in data["detail"].lower()


def test_webhook_full_turn_round_trip(client, db):
    """
    Acceptance test (FLOW-023):
    A round trip yields a natural reply and two rows in messages.
    """
    phone = f"+9198{uuid4().int % 100000000:08d}"
    channel_msg_id = f"msg-roundtrip-{uuid4()}"
    payload = {
        "phone_number": phone,
        "contact_name": "Aarav Mehta",
        "message": "Hi, I am looking for backend engineering roles",
        "channel_message_id": channel_msg_id,
    }

    response = client.post("/webhook", json=payload)
    assert response.status_code == 200
    body = response.json()

    assert body["status"] == "ok"
    assert body["decision"] == "proceed"
    assert body["reply_text"] is not None
    assert len(body["reply_text"]) > 0
    assert body["candidate_id"] is not None
    assert body["conversation_id"] is not None

    # Assert database state: 2 rows in messages table
    uow = UnitOfWork(session=db)
    with uow:
        messages = uow.messages.get_recent(body["conversation_id"])
        assert len(messages) == 2
        directions = {m.direction.value for m in messages}
        assert directions == {"inbound", "outbound"}

        # Inbound message attributes
        inbound_db = next(m for m in messages if m.direction == DirectionEnum.inbound)
        assert inbound_db.channel_message_id == channel_msg_id
        assert inbound_db.body == "Hi, I am looking for backend engineering roles"

        # Outbound message attributes
        outbound_db = next(m for m in messages if m.direction == DirectionEnum.outbound)
        assert outbound_db.body == body["reply_text"]
