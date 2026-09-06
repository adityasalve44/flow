"""
app/services/turn.py — single-turn orchestration engine (FLOW-021).

Orchestrates the entire turn lifecycle:
1. Inbound event resolution (candidate & conversation).
2. Consent gate check (Q4: zero persistence if pending/declined).
3. Message logging (inbound & outbound).
4. ADK session retrieval & SequentialAgent execution (extractor -> policy -> replier).
5. Safe degradation on model/network error (never crash with a 500).
6. Outbound message persistence & timestamp updates.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any
from uuid import UUID, uuid4

from google.adk.apps import App
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.genai import types

from app.agents.root import create_flow_app
from app.agents.session import get_or_create_adk_session, get_session_service
from app.channel.inbound import InboundEvent
from app.db.uow import UnitOfWork
from app.domain.consent import evaluate_consent_turn
from app.models import Message
from app.models.enums import (
    ChannelEnum,
    ConversationModeEnum,
    ConversationStatusEnum,
    DirectionEnum,
)
from app.services.conversation import resolve_conversation

logger = logging.getLogger(__name__)

SAFE_FALLBACK_REPLY = (
    "Thanks for your message! We're experiencing a brief technical delay on our end. "
    "Please send your message again in a moment."
)


@dataclass(frozen=True)
class TurnResult:
    """Structured result of executing a conversation turn."""

    candidate_id: UUID
    conversation_id: UUID
    reply_text: str
    directive: str
    mode: str
    is_closed: bool = False
    inbound_message_id: UUID | None = None
    outbound_message_id: UUID | None = None


class TurnService:
    """Service that executes one complete conversational turn."""

    def __init__(
        self,
        uow: UnitOfWork | None = None,
        session_service: BaseSessionService | None = None,
        app: App | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.uow = uow
        self.session_service = session_service or get_session_service()
        self.app = app or create_flow_app()
        self.runner = runner or Runner(app=self.app, session_service=self.session_service)

    async def run(
        self,
        inbound: InboundEvent,
        now: datetime | None = None,
    ) -> TurnResult:
        """
        Execute one inbound turn.

        Guarantees:
        - Never throws unhandled model errors; degrades to SAFE_FALLBACK_REPLY.
        - Persists inbound and outbound messages in flow.messages.
        - Respects Q4 consent gate before invoking extractor.
        """
        current_time = (
            now
            or getattr(inbound, "received_at", None)
            or getattr(inbound, "timestamp", None)
            or datetime.now(timezone.utc)
        )
        phone = getattr(inbound, "phone_number", None) or getattr(inbound, "sender_phone", "")
        message_body = getattr(inbound, "message", None) or getattr(inbound, "body", "") or ""
        media_ref = getattr(inbound, "media", None) or getattr(inbound, "media_url", None)
        channel = getattr(inbound, "channel", ChannelEnum.simulator)
        uow = self.uow or UnitOfWork()

        # Step 1: Resolve candidate & conversation
        with uow:
            candidate = uow.candidates.get_or_create_by_phone(
                phone_number=phone,
                display_name=inbound.contact_name,
            )
            conversation = resolve_conversation(
                uow=uow,
                candidate=candidate,
                channel=channel,
                now=current_time,
            )

            # Persist inbound message
            inbound_msg = Message(
                conversation_id=conversation.id,
                candidate_id=candidate.id,
                direction=DirectionEnum.inbound,
                channel_message_id=inbound.channel_message_id,
                body=message_body,
                media_ref=media_ref,
                created_at=current_time,
            )
            uow.messages.add(inbound_msg)
            conversation.last_inbound_at = current_time

            cand_id = candidate.id
            conv_id = conversation.id
            inbound_msg_id = inbound_msg.id

            # Step 2: Evaluate consent gate
            consent_decision = evaluate_consent_turn(
                candidate=candidate,
                conversation=conversation,
                message_text=message_body,
                channel_message_id=inbound.channel_message_id,
                now=current_time,
            )

            # If consent gate handled the turn (pending, declined, withdrawn):
            if not consent_decision.should_invoke_extractor:
                outbound_msg = Message(
                    conversation_id=conv_id,
                    candidate_id=cand_id,
                    direction=DirectionEnum.outbound,
                    channel_message_id=f"out-{uuid4()}",
                    body=consent_decision.reply_text,
                    created_at=current_time,
                )
                uow.messages.add(outbound_msg)
                conversation.last_outbound_at = current_time
                uow.commit()

                return TurnResult(
                    candidate_id=cand_id,
                    conversation_id=conv_id,
                    reply_text=consent_decision.reply_text,
                    directive=consent_decision.directive,
                    mode=conversation.mode.value if hasattr(conversation.mode, "value") else str(conversation.mode),
                    is_closed=conversation.status == ConversationStatusEnum.closed,
                    inbound_message_id=inbound_msg_id,
                    outbound_message_id=outbound_msg.id,
                )

            uow.commit()

        # Step 3: Consent granted -> run ADK agent pipeline
        adk_session = await get_or_create_adk_session(
            session_service=self.session_service,
            candidate=candidate,
            conversation=conversation,
        )

        user_content = types.Content(
            role="user",
            parts=[types.Part.from_text(text=message_body)],
        )

        reply_text = ""
        directive_name = "ask_next"

        try:
            async for event in self.runner.run_async(
                user_id=str(cand_id),
                session_id=str(conv_id),
                new_message=user_content,
            ):
                # Extract text
                if event.content and event.content.parts:
                    text_parts = [p.text for p in event.content.parts if p.text]
                    if text_parts:
                        reply_text = "".join(text_parts)

                # Extract directive if emitted by policy agent
                if event.actions and event.actions.state_delta:
                    dir_dict = event.actions.state_delta.get("temp:directive")
                    if dir_dict and isinstance(dir_dict, dict):
                        directive_name = dir_dict.get("name", directive_name)

        except Exception as err:
            logger.error(
                "Model or pipeline error during turn for candidate %s: %s",
                cand_id,
                err,
                exc_info=True,
            )
            reply_text = SAFE_FALLBACK_REPLY
            directive_name = "error_fallback"

        if not reply_text:
            reply_text = (
                "Thanks for sharing! What role and work location are you targeting next?"
            )

        # Step 4: Persist outbound message
        with uow:
            # Re-fetch conversation to ensure fresh session attachment
            conv_record = uow.conversations.get_by_id(conv_id)
            outbound_msg = Message(
                conversation_id=conv_id,
                candidate_id=cand_id,
                direction=DirectionEnum.outbound,
                channel_message_id=f"out-{uuid4()}",
                body=reply_text,
                created_at=datetime.now(timezone.utc),
            )
            uow.messages.add(outbound_msg)
            if conv_record:
                conv_record.last_outbound_at = datetime.now(timezone.utc)
            uow.commit()

            mode_val = (
                conv_record.mode.value
                if conv_record and hasattr(conv_record.mode, "value")
                else "intake"
            )
            is_closed = (
                conv_record.status == ConversationStatusEnum.closed
                if conv_record
                else False
            )

            return TurnResult(
                candidate_id=cand_id,
                conversation_id=conv_id,
                reply_text=reply_text,
                directive=directive_name,
                mode=mode_val,
                is_closed=is_closed,
                inbound_message_id=inbound_msg_id,
                outbound_message_id=outbound_msg.id,
            )
