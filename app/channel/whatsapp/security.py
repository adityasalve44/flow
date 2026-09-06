"""
app/channel/whatsapp/security.py — HMAC signature verification & webhook handshake (FLOW-041).

Implements:
1. X-Hub-Signature-256 HMAC-SHA256 payload verification against Meta app secret.
2. Subscription handshake (GET hub.mode, hub.verify_token, hub.challenge).
"""

import hashlib
import hmac


def verify_hub_signature(
    raw_body: bytes,
    signature_header: str | None,
    app_secret: str,
) -> bool:
    """
    Verify Meta WhatsApp Cloud API webhook signature.

    The header format is: sha256=<hex_encoded_hmac>
    Uses hmac.compare_digest to prevent timing attacks.
    """
    if not app_secret:
        # If no secret configured (local testing / dry run), reject or pass depending on strictness
        return False

    if not signature_header:
        return False

    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False

    received_hash = signature_header[len(prefix) :].strip()
    expected_hash = hmac.new(
        key=app_secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(received_hash, expected_hash)


def verify_webhook_handshake(
    mode: str | None,
    token: str | None,
    challenge: str | None,
    expected_token: str,
) -> str:
    """
    Validate the GET subscription handshake from Meta.

    Meta sends:
      hub.mode = "subscribe"
      hub.verify_token = <configured verify token>
      hub.challenge = <integer or string challenge>

    Returns the challenge string on success, or raises ValueError on mismatch.
    """
    if not expected_token:
        raise ValueError("Webhook verify token is not configured on server")

    if mode != "subscribe":
        raise ValueError(f"Invalid hub.mode: '{mode}'. Expected 'subscribe'")

    if not token or not hmac.compare_digest(token, expected_token):
        raise ValueError("Invalid hub.verify_token")

    if not challenge:
        raise ValueError("Missing hub.challenge")

    return challenge
