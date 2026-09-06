"""
app/api/schemas.py — Pydantic DTOs for ingress, webhooks, and API payloads.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.normalize import normalize_phone

MAX_MESSAGE_LENGTH = 4096


class InboundEvent(BaseModel):
    """Normalized channel inbound event DTO.

    Represents a message received from WhatsApp BSP or test simulator.
    Validates E.164 phone formatting and enforces length limits.
    """

    model_config = ConfigDict(extra="ignore")

    phone_number: str = Field(
        ...,
        description="Sender phone number, normalized to E.164 (+91...)",
    )
    contact_name: str | None = Field(
        None,
        description="Sender profile display name from WhatsApp channel metadata",
    )
    message: str | None = Field(
        None,
        description="Text content of the message",
    )
    media: str | None = Field(
        None,
        description="URL or media storage reference if an attachment is present",
    )
    channel_message_id: str = Field(
        ...,
        min_length=1,
        description="Unique message ID assigned by the channel (for idempotency)",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC timestamp when message was received",
    )

    @field_validator("phone_number")
    @classmethod
    def validate_and_normalize_phone(cls, v: str) -> str:
        norm = normalize_phone(v)
        if not norm:
            raise ValueError(
                f"Invalid phone number: '{v}'. Must be a valid phone number resolvable to E.164."
            )
        return norm

    @field_validator("message")
    @classmethod
    def validate_message_length(cls, v: str | None) -> str | None:
        if v and len(v) > MAX_MESSAGE_LENGTH:
            raise ValueError(
                f"Message exceeds maximum allowed length of {MAX_MESSAGE_LENGTH} characters."
            )
        return v


class WebhookResponse(BaseModel):
    """Standard response for inbound webhook processing."""

    status: str
    decision: str
    channel_message_id: str
    detail: str | None = None
