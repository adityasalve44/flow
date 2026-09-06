"""
app/channel/whatsapp/parser.py — Meta WhatsApp Cloud API payload parser (FLOW-041).

Extracts and transforms the Cloud API webhook envelope into normalized InboundEvent DTOs.
Gracefully skips non-message events (e.g. delivery receipts, status callbacks).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.api.schemas import InboundEvent


@dataclass(frozen=True)
class WhatsAppMediaInfo:
    """Extracted metadata for an incoming WhatsApp media attachment."""

    media_id: str
    mime_type: str | None = None
    filename: str | None = None
    caption: str | None = None


@dataclass(frozen=True)
class ParsedWhatsAppMessage:
    """Result of parsing a single WhatsApp message from the webhook payload."""

    event: InboundEvent
    phone_number_id: str | None = None
    media_info: WhatsAppMediaInfo | None = None


def _format_e164(raw_phone: str) -> str:
    """Format raw WhatsApp phone string (which lacks '+') to E.164."""
    cleaned = raw_phone.strip()
    if not cleaned.startswith("+"):
        return f"+{cleaned}"
    return cleaned


def parse_whatsapp_payload(payload: dict[str, Any]) -> list[ParsedWhatsAppMessage]:
    """
    Parse a Meta WhatsApp Cloud API webhook JSON payload.

    Structure:
    entry[] -> changes[] -> value -> messages[]
    Contacts are matched via contacts[].profile.name.

    Returns a list of ParsedWhatsAppMessage instances.
    Ignores status updates, delivery receipts, or unhandled message types safely.
    """
    results: list[ParsedWhatsAppMessage] = []

    if not isinstance(payload, dict):
        return results

    entries = payload.get("entry", [])
    if not isinstance(entries, list):
        return results

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes", [])
        if not isinstance(changes, list):
            continue

        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value", {})
            if not isinstance(value, dict):
                continue

            # Check if this is a message event rather than a status callback
            messages = value.get("messages", [])
            if not messages or not isinstance(messages, list):
                continue

            # Extract phone_number_id from metadata
            metadata = value.get("metadata", {})
            phone_number_id = str(metadata.get("phone_number_id", "")) if metadata else None

            # Map contact profile names by wa_id
            contact_map: dict[str, str] = {}
            contacts = value.get("contacts", [])
            if isinstance(contacts, list):
                for c in contacts:
                    if isinstance(c, dict):
                        wa_id = str(c.get("wa_id", ""))
                        profile = c.get("profile", {})
                        if isinstance(profile, dict) and "name" in profile:
                            contact_map[wa_id] = str(profile["name"])

            for msg in messages:
                if not isinstance(msg, dict):
                    continue

                raw_sender = msg.get("from")
                msg_id = msg.get("id")
                if not raw_sender or not msg_id:
                    continue

                phone_number = _format_e164(str(raw_sender))
                contact_name = contact_map.get(str(raw_sender))

                # Parse timestamp (epoch seconds string or int)
                raw_ts = msg.get("timestamp")
                ts = datetime.now(UTC)
                if raw_ts:
                    try:
                        ts = datetime.fromtimestamp(float(raw_ts), tz=UTC)
                    except ValueError, TypeError, OverflowError:
                        ts = datetime.now(UTC)

                msg_type = msg.get("type", "text")
                text_content: str | None = None
                media_info: WhatsAppMediaInfo | None = None

                if msg_type == "text":
                    text_obj = msg.get("text", {})
                    if isinstance(text_obj, dict):
                        text_content = text_obj.get("body")

                elif msg_type == "document":
                    doc_obj = msg.get("document", {})
                    if isinstance(doc_obj, dict) and "id" in doc_obj:
                        media_info = WhatsAppMediaInfo(
                            media_id=str(doc_obj["id"]),
                            mime_type=doc_obj.get("mime_type"),
                            filename=doc_obj.get("filename"),
                            caption=doc_obj.get("caption"),
                        )
                        text_content = doc_obj.get("caption") or ""

                elif msg_type == "image":
                    img_obj = msg.get("image", {})
                    if isinstance(img_obj, dict) and "id" in img_obj:
                        media_info = WhatsAppMediaInfo(
                            media_id=str(img_obj["id"]),
                            mime_type=img_obj.get("mime_type"),
                            caption=img_obj.get("caption"),
                        )
                        text_content = img_obj.get("caption") or ""

                elif msg_type in ("audio", "voice"):
                    audio_obj = msg.get(msg_type, {})
                    if isinstance(audio_obj, dict) and "id" in audio_obj:
                        media_info = WhatsAppMediaInfo(
                            media_id=str(audio_obj["id"]),
                            mime_type=audio_obj.get("mime_type"),
                        )

                else:
                    # Other message types (reaction, location, sticker, etc.)
                    # Provide an informative fallback text representation
                    text_content = f"[{msg_type} message]"

                # If message is empty and no media, set empty string
                if text_content is None and not media_info:
                    text_content = ""

                try:
                    event = InboundEvent(
                        phone_number=phone_number,
                        contact_name=contact_name,
                        message=text_content or "",
                        media=media_info.media_id if media_info else None,
                        channel_message_id=str(msg_id),
                        timestamp=ts,
                    )
                    results.append(
                        ParsedWhatsAppMessage(
                            event=event,
                            phone_number_id=phone_number_id,
                            media_info=media_info,
                        )
                    )
                except Exception:
                    # Skip invalid phone or malformed event gracefully
                    continue

    return results
