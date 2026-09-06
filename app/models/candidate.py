"""
app/models/candidate.py — Candidate, Conversation, Message models.

Key design decisions (from §6 of REVIEW_AND_PLAN.md):
- UUID primary keys with gen_random_uuid() server-default.
- candidates.display_name is channel metadata (the WhatsApp contact name).
  It is NEVER used as the profile name — see candidate_profiles.full_name.
- candidates.lifecycle_status is the FIVE-STATE enum only.
  Application-pipeline states belong to the future matching system (Q8).
- candidates.consent_status / consent_at / consent_message_id implement
  the consent gate (Q4).
- conversations has real state: status, mode, counters, timestamps.
- messages.channel_message_id is unique for idempotency.
- No org_id anywhere — single-tenant (Q1).
"""

from datetime import datetime
from decimal import Decimal
import uuid

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import (
    ChannelEnum,
    ConsentStatusEnum,
    ConversationModeEnum,
    ConversationStatusEnum,
    DirectionEnum,
    LifecycleStatusEnum,
)


class Candidate(Base, TimestampMixin):
    __tablename__ = "candidates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    phone_number: Mapped[str] = mapped_column(
        String(30),
        unique=True,
        index=True,
        nullable=False,
    )
    # WhatsApp contact name — channel metadata only, never overwritten by profile
    display_name: Mapped[str | None] = mapped_column(String(255))

    # Five-state lifecycle — no application states allowed here (Q8)
    lifecycle_status: Mapped[LifecycleStatusEnum] = mapped_column(
        Enum(LifecycleStatusEnum, name="lifecycle_status_enum", schema="flow"),
        nullable=False,
        default=LifecycleStatusEnum.new,
        server_default=LifecycleStatusEnum.new.value,
    )

    # Consent gate (Q4)
    consent_status: Mapped[ConsentStatusEnum] = mapped_column(
        Enum(ConsentStatusEnum, name="consent_status_enum", schema="flow"),
        nullable=False,
        default=ConsentStatusEnum.pending,
        server_default=ConsentStatusEnum.pending.value,
    )
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ID of the message that granted consent (string to avoid circular FK)
    consent_message_id: Mapped[str | None] = mapped_column(String(255))

    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    profile: Mapped["CandidateProfile | None"] = relationship(
        "CandidateProfile",
        back_populates="candidate",
        cascade="all, delete-orphan",
        uselist=False,
    )
    conversations: Mapped[list["Conversation"]] = relationship(
        "Conversation",
        back_populates="candidate",
        cascade="all, delete-orphan",
    )
    messages: Mapped[list["Message"]] = relationship(
        "Message",
        back_populates="candidate",
        cascade="all, delete-orphan",
        foreign_keys="[Message.candidate_id]",
    )


class CandidateProfile(Base, TimestampMixin):
    """Derived projection — written only by the merge engine, never by hand.

    Contains only operational facts that are confirmed and current.
    Money is NUMERIC(12,2) with an explicit currency column, never Float.
    """
    __tablename__ = "candidate_profiles"

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow.candidates.id", ondelete="CASCADE"),
        primary_key=True,
    )
    full_name: Mapped[str | None] = mapped_column(String(255))
    current_role: Mapped[str | None] = mapped_column(String(255))
    current_company: Mapped[str | None] = mapped_column(String(255))
    experience_years: Mapped[float | None] = mapped_column()
    # Money — NUMERIC via Python Decimal; currency is always explicit
    current_ctc_annual: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    expected_ctc_annual: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(10))  # e.g. "INR"
    notice_period_days: Mapped[int | None] = mapped_column(Integer)
    work_mode: Mapped[str | None] = mapped_column(String(50))
    education_level: Mapped[str | None] = mapped_column(String(255))
    completeness: Mapped[float | None] = mapped_column()
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    candidate: Mapped["Candidate"] = relationship(
        "Candidate",
        back_populates="profile",
    )


class Conversation(Base, TimestampMixin):
    """A bounded exchange — has real state, counters and lifecycle."""
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow.candidates.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    channel: Mapped[ChannelEnum] = mapped_column(
        Enum(ChannelEnum, name="channel_enum", schema="flow"),
        nullable=False,
        default=ChannelEnum.simulator,
        server_default=ChannelEnum.simulator.value,
    )
    # ADK session ID — maps conversation ↔ ADK session
    adk_session_id: Mapped[str | None] = mapped_column(String(255))

    status: Mapped[ConversationStatusEnum] = mapped_column(
        Enum(ConversationStatusEnum, name="conversation_status_enum", schema="flow"),
        nullable=False,
        default=ConversationStatusEnum.active,
        server_default=ConversationStatusEnum.active.value,
    )
    mode: Mapped[ConversationModeEnum] = mapped_column(
        Enum(ConversationModeEnum, name="conversation_mode_enum", schema="flow"),
        nullable=False,
        default=ConversationModeEnum.consent,
        server_default=ConversationModeEnum.consent.value,
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    deflection_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    abuse_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    candidate: Mapped["Candidate"] = relationship(
        "Candidate",
        back_populates="conversations",
    )
    messages: Mapped[list["Message"]] = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )


class Message(Base):
    """Every message, both directions. Unique on channel_message_id for idempotency."""
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow.conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow.candidates.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    direction: Mapped[DirectionEnum] = mapped_column(
        Enum(DirectionEnum, name="direction_enum", schema="flow"),
        nullable=False,
    )
    # channel_message_id is the external message ID from the BSP — unique for idempotency
    channel_message_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    body: Mapped[str | None] = mapped_column(Text)
    media_ref: Mapped[str | None] = mapped_column(String(1024))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    conversation: Mapped["Conversation"] = relationship(
        "Conversation",
        back_populates="messages",
    )
    candidate: Mapped["Candidate"] = relationship(
        "Candidate",
        back_populates="messages",
        foreign_keys=[candidate_id],
    )
