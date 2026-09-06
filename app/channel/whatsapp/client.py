"""
app/channel/whatsapp/client.py — Async Meta WhatsApp Cloud API outbound client (FLOW-041).

Features:
1. Sends outbound WhatsApp text replies via Graph API.
2. 24-hour customer service window awareness (free-form messaging limit).
3. Resilient delivery with exponential backoff retries on transient errors.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import get_settings
from app.logging import get_logger

logger = get_logger(__name__)

# WhatsApp customer service window duration
CUSTOMER_SERVICE_WINDOW_HOURS = 24


class WhatsAppClientError(Exception):
    """Base error for WhatsApp outbound operations."""


class WhatsAppWindowExpiredError(WhatsAppClientError):
    """Raised when attempting to send free-form message outside 24h window."""


class WhatsAppTransientError(WhatsAppClientError):
    """Raised on 5xx or connection errors eligible for retry."""


def _is_within_24h_window(last_inbound_at: datetime | None, now: datetime | None = None) -> bool:
    """Check whether candidate's last inbound message is within 24h customer care window."""
    if last_inbound_at is None:
        return True  # If unknown, assume permissible

    current_time = now or datetime.now(UTC)
    if last_inbound_at.tzinfo is None:
        last_inbound_at = last_inbound_at.replace(tzinfo=UTC)

    diff = current_time - last_inbound_at
    return diff <= timedelta(hours=CUSTOMER_SERVICE_WINDOW_HOURS)


class WhatsAppClient:
    """Outbound client for Meta WhatsApp Cloud API."""

    def __init__(
        self,
        phone_number_id: str | None = None,
        access_token: str | None = None,
        api_version: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self.phone_number_id = phone_number_id or settings.whatsapp_phone_number_id
        self.access_token = access_token or settings.whatsapp_access_token
        self.api_version = api_version or settings.whatsapp_api_version
        self._http_client = http_client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is not None:
            return self._http_client
        return httpx.AsyncClient(timeout=15.0)

    @retry(
        retry=retry_if_exception_type(WhatsAppTransientError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5.0),
        reraise=True,
    )
    async def _post_message_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute HTTP POST with retries on transient server errors."""
        try:
            resp = await client.post(url, headers=headers, json=payload)
        except (httpx.ConnectError, httpx.TimeoutException) as err:
            logger.warning("WhatsApp API network error: %s (retrying)", err)
            raise WhatsAppTransientError(f"Network error: {err}") from err

        if resp.status_code >= 500:
            logger.warning(
                "WhatsApp API transient server error (status=%d): %s (retrying)",
                resp.status_code,
                resp.text,
            )
            raise WhatsAppTransientError(f"Server error: {resp.status_code}")

        if resp.status_code not in (200, 201):
            logger.error("WhatsApp API 4xx error (status=%d): %s", resp.status_code, resp.text)
            raise WhatsAppClientError(f"WhatsApp API error {resp.status_code}: {resp.text}")

        return resp.json()

    async def send_text_message(
        self,
        to_phone: str,
        text: str,
        reply_to_message_id: str | None = None,
        last_inbound_at: datetime | None = None,
    ) -> str:
        """
        Send a text message to a WhatsApp user.

        Checks 24-hour window awareness.
        Returns the channel message ID (e.g. 'wamid.HBg...').
        """
        if not self.phone_number_id or not self.access_token:
            logger.warning("WhatsApp credentials missing; skipping real outbound HTTP delivery")
            return f"wamid.mock_{datetime.now(UTC).timestamp()}"

        # 24-hour customer service window check
        if not _is_within_24h_window(last_inbound_at):
            logger.warning(
                "Cannot send free-form reply to %s: 24h customer service window expired.",
                to_phone,
            )
            raise WhatsAppWindowExpiredError(
                f"24h customer service window expired for recipient {to_phone}"
            )

        # Meta API expects digits without '+'
        recipient = to_phone.lstrip("+").strip()

        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": text,
            },
        }

        if reply_to_message_id:
            payload["context"] = {"message_id": reply_to_message_id}

        client = await self._get_client()
        should_close = self._http_client is None

        try:
            data = await self._post_message_with_retry(client, url, headers, payload)
            messages = data.get("messages", [])
            if messages and isinstance(messages, list):
                return str(messages[0].get("id", ""))
            return ""
        finally:
            if should_close:
                await client.aclose()
