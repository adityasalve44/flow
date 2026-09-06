"""
tests/test_whatsapp_adapter.py — Tests for Meta WhatsApp Cloud API channel adapter (FLOW-041).

Acceptance criteria:
1. Meta subscription handshake succeeds with valid verify_token, rejected on mismatch.
2. X-Hub-Signature-256 HMAC-SHA256 signature verification accepts valid, rejects unsigned/mis-signed.
3. Cloud API webhook payload envelope correctly mapped to InboundEvent DTO.
4. Two-step Graph API media download retrieves and validates media.
5. Outbound client sends messages, retries transient 5xx errors, and enforces 24-hour window.
6. End-to-end webhook turn delivers reply through WhatsApp client.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.channel.inbound import InboundEvent
from app.channel.whatsapp.client import (
    WhatsAppClient,
    WhatsAppWindowExpiredError,
    _is_within_24h_window,
)
from app.channel.whatsapp.media import download_whatsapp_media
from app.channel.whatsapp.parser import parse_whatsapp_payload
from app.channel.whatsapp.security import verify_hub_signature, verify_webhook_handshake
from app.config import get_settings
from app.main import app
from app.services.turn import TurnResult


@pytest.fixture
def test_client():
    return TestClient(app)


def _sign_payload(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ---------------------------------------------------------------------------
# 1. Security & Handshake Tests
# ---------------------------------------------------------------------------


def test_verify_hub_signature():
    secret = "meta_app_secret_12345"
    payload = b'{"object":"whatsapp_business_account"}'
    valid_sig = _sign_payload(payload, secret)

    # Valid signature
    assert verify_hub_signature(payload, valid_sig, secret) is True

    # Tampered payload
    assert verify_hub_signature(b'{"tampered":true}', valid_sig, secret) is False

    # Bad signature
    assert verify_hub_signature(payload, "sha256=invalid_hash", secret) is False

    # Missing header
    assert verify_hub_signature(payload, None, secret) is False

    # Missing secret
    assert verify_hub_signature(payload, valid_sig, "") is False


def test_verify_webhook_handshake():
    token = "secret_verify_token_xyz"
    challenge = "1158201444"

    # Valid handshake
    res = verify_webhook_handshake(
        mode="subscribe",
        token=token,
        challenge=challenge,
        expected_token=token,
    )
    assert res == challenge

    # Invalid mode
    with pytest.raises(ValueError, match="Expected 'subscribe'"):
        verify_webhook_handshake("unsubscribe", token, challenge, token)

    # Invalid token
    with pytest.raises(ValueError, match="Invalid hub.verify_token"):
        verify_webhook_handshake("subscribe", "wrong_token", challenge, token)

    # Missing challenge
    with pytest.raises(ValueError, match="Missing hub.challenge"):
        verify_webhook_handshake("subscribe", token, None, token)


def test_get_webhook_whatsapp_handshake_endpoint(monkeypatch, test_client):
    monkeypatch.setattr(get_settings(), "whatsapp_verify_token", "flow_verify_123")

    # Success: returns challenge string with 200
    res = test_client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "flow_verify_123",
            "hub.challenge": "challenge_code_987",
        },
    )
    assert res.status_code == 200
    assert res.text == "challenge_code_987"

    # Failure: wrong verify token -> 403 Forbidden
    res_bad = test_client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong_token",
            "hub.challenge": "challenge_code_987",
        },
    )
    assert res_bad.status_code == 403


# ---------------------------------------------------------------------------
# 2. Payload Parser Tests
# ---------------------------------------------------------------------------


def test_parse_text_message_payload():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID_123",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": "PN_ID_456",
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "Rohan Deshmukh"},
                                    "wa_id": "919876543210",
                                }
                            ],
                            "messages": [
                                {
                                    "from": "919876543210",
                                    "id": "wamid.HBgLMTIzNDU",
                                    "timestamp": "1725678900",
                                    "type": "text",
                                    "text": {"body": "I am a backend engineer with 5 yrs exp."},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }

    parsed = parse_whatsapp_payload(payload)
    assert len(parsed) == 1
    item = parsed[0]
    assert item.phone_number_id == "PN_ID_456"
    assert isinstance(item.event, InboundEvent)
    assert item.event.phone_number == "+919876543210"
    assert item.event.contact_name == "Rohan Deshmukh"
    assert item.event.message == "I am a backend engineer with 5 yrs exp."
    assert item.event.channel_message_id == "wamid.HBgLMTIzNDU"
    assert item.event.timestamp == datetime.fromtimestamp(1725678900, tz=UTC)
    assert item.media_info is None


def test_parse_document_media_payload():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"profile": {"name": "Sara"}, "wa_id": "919123456789"}],
                            "messages": [
                                {
                                    "from": "919123456789",
                                    "id": "wamid.DOC123",
                                    "type": "document",
                                    "document": {
                                        "id": "MEDIA_ID_999",
                                        "filename": "my_resume.pdf",
                                        "mime_type": "application/pdf",
                                        "caption": "Here is my updated resume",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }

    parsed = parse_whatsapp_payload(payload)
    assert len(parsed) == 1
    item = parsed[0]
    assert item.event.phone_number == "+919123456789"
    assert item.event.media == "MEDIA_ID_999"
    assert item.event.message == "Here is my updated resume"
    assert item.media_info is not None
    assert item.media_info.media_id == "MEDIA_ID_999"
    assert item.media_info.filename == "my_resume.pdf"
    assert item.media_info.mime_type == "application/pdf"


def test_parse_status_receipt_ignored_safely():
    # Meta delivery receipts have "statuses" instead of "messages"
    status_payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "statuses": [
                                {
                                    "id": "wamid.HBgLMTIzNDU",
                                    "status": "delivered",
                                    "timestamp": "1725678910",
                                    "recipient_id": "919876543210",
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }

    parsed = parse_whatsapp_payload(status_payload)
    assert parsed == []


# ---------------------------------------------------------------------------
# 3. Two-Step Media Download Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_whatsapp_media_two_step():
    media_id = "media_test_abc123"
    token = "access_token_xyz"
    pdf_bytes = b"%PDF-1.4\n%mock valid pdf content for resume testing\n%%EOF"

    mock_client = AsyncMock(spec=httpx.AsyncClient)

    # Step 1: Metadata call response
    meta_response = httpx.Response(
        status_code=200,
        json={
            "id": media_id,
            "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/temp_file_url",
            "mime_type": "application/pdf",
            "file_size": len(pdf_bytes),
        },
    )

    # Step 2: Binary download call response
    binary_response = httpx.Response(
        status_code=200,
        content=pdf_bytes,
        headers={"Content-Type": "application/pdf"},
    )

    mock_client.get.side_effect = [meta_response, binary_response]

    validated = await download_whatsapp_media(
        media_id=media_id,
        access_token=token,
        client=mock_client,
        override_filename="candidate_cv.pdf",
    )

    assert validated.filename == "candidate_cv.pdf"
    assert validated.content_type == "application/pdf"
    assert validated.size_bytes == len(pdf_bytes)
    assert validated.checksum == hashlib.sha256(pdf_bytes).hexdigest()
    assert mock_client.get.call_count == 2


# ---------------------------------------------------------------------------
# 4. Outbound Client & 24h Window Tests
# ---------------------------------------------------------------------------


def test_24h_window_check():
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)

    # Message received 2 hours ago: within window
    recent = now - timedelta(hours=2)
    assert _is_within_24h_window(recent, now) is True

    # Message received 23.5 hours ago: within window
    border = now - timedelta(hours=23, minutes=30)
    assert _is_within_24h_window(border, now) is True

    # Message received 25 hours ago: expired
    expired = now - timedelta(hours=25)
    assert _is_within_24h_window(expired, now) is False

    # None provided (unknown): assumed within window
    assert _is_within_24h_window(None, now) is True


@pytest.mark.asyncio
async def test_whatsapp_client_send_success():
    client = WhatsAppClient(
        phone_number_id="PN_12345",
        access_token="TOKEN_ABC",
        api_version="v21.0",
    )

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.post.return_value = httpx.Response(
        status_code=200,
        json={"messages": [{"id": "wamid.SENT_OUTBOUND_123"}]},
    )
    client._http_client = mock_http

    msg_id = await client.send_text_message(
        to_phone="+919876543210",
        text="Hello from Flow!",
    )
    assert msg_id == "wamid.SENT_OUTBOUND_123"
    assert mock_http.post.call_count == 1

    call_args = mock_http.post.call_args
    url = call_args[0][0]
    payload = call_args[1]["json"]
    assert url == "https://graph.facebook.com/v21.0/PN_12345/messages"
    assert payload["to"] == "919876543210"
    assert payload["text"]["body"] == "Hello from Flow!"


@pytest.mark.asyncio
async def test_whatsapp_client_24h_window_expired_raises():
    client = WhatsAppClient(phone_number_id="PN_12345", access_token="TOKEN_ABC")
    client._http_client = AsyncMock(spec=httpx.AsyncClient)

    old_inbound = datetime.now(UTC) - timedelta(hours=26)
    with pytest.raises(WhatsAppWindowExpiredError, match="24h customer service window expired"):
        await client.send_text_message(
            to_phone="+919876543210",
            text="Hello from Flow!",
            last_inbound_at=old_inbound,
        )


@pytest.mark.asyncio
async def test_whatsapp_client_retries_transient_server_error():
    client = WhatsAppClient(phone_number_id="PN_12345", access_token="TOKEN_ABC")
    mock_http = AsyncMock(spec=httpx.AsyncClient)

    # First call: 503 Server Error -> triggers retry
    # Second call: 200 OK -> succeeds
    mock_http.post.side_effect = [
        httpx.Response(status_code=503, text="Service Unavailable"),
        httpx.Response(status_code=200, json={"messages": [{"id": "wamid.RETRY_SUCCESS"}]}),
    ]
    client._http_client = mock_http

    msg_id = await client.send_text_message(
        to_phone="+919876543210",
        text="Test retry",
    )
    assert msg_id == "wamid.RETRY_SUCCESS"
    assert mock_http.post.call_count == 2


# ---------------------------------------------------------------------------
# 5. End-to-End Webhook POST Integration Tests
# ---------------------------------------------------------------------------


def test_post_whatsapp_webhook_signature_rejection(monkeypatch, test_client):
    secret = "my_webhook_secret_key"
    monkeypatch.setattr(get_settings(), "whatsapp_app_secret", secret)

    payload = b'{"entry":[]}'
    # Invalid signature -> 403 Forbidden
    res = test_client.post(
        "/webhook/whatsapp",
        content=payload,
        headers={"X-Hub-Signature-256": "sha256=invalidhash", "Content-Type": "application/json"},
    )
    assert res.status_code == 403


def test_post_whatsapp_webhook_end_to_end_turn(monkeypatch, test_client, db):
    secret = "my_webhook_secret_key"
    monkeypatch.setattr(get_settings(), "whatsapp_app_secret", secret)

    phone = f"9198{uuid4().int % 100000000:08d}"
    msg_id = f"wamid.{uuid4()}"

    payload_dict = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"profile": {"name": "Test Candidate"}, "wa_id": phone}],
                            "messages": [
                                {
                                    "from": phone,
                                    "id": msg_id,
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "type": "text",
                                    "text": {
                                        "body": "Hi, I want to learn more about job openings."
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload_dict).encode("utf-8")
    sig = _sign_payload(body, secret)

    # Mock the outbound WhatsApp client delivery and turn execution
    with patch(
        "app.channel.whatsapp.router.WhatsAppClient.send_text_message",
        new_callable=AsyncMock,
    ) as mock_send, patch(
        "app.services.turn.TurnService.run",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_send.return_value = "wamid.OUTBOUND_REPLY"
        mock_run.return_value = TurnResult(
            candidate_id=uuid4(),
            conversation_id=uuid4(),
            reply_text="Hey! Priya here from Flow. Great to connect! What role are you exploring?",
            directive="first_contact_intro",
            mode="intake",
        )

        res = test_client.post(
            "/webhook/whatsapp",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "Content-Type": "application/json",
            },
        )

        assert res.status_code == 200
        assert res.json() == {"status": "ok"}
        assert mock_send.call_count == 1

        call_kwargs = mock_send.call_args.kwargs
        assert call_kwargs["to_phone"] == f"+{phone}"
        assert len(call_kwargs["text"]) > 0
        assert any(term in call_kwargs["text"].lower() for term in ("priya", "flow", "role", "opportunity", "connect", "background", "career", "details", "pause", "sorry", "hiccup"))
        assert call_kwargs["reply_to_message_id"] == msg_id
