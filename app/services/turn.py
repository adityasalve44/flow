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

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.genai import types

from app.agents.callbacks import SAFE_FALLBACK_REPLY, _get_fallback_reply
from app.agents.root import create_flow_app
from app.agents.session import get_or_create_adk_session, get_session_service
from app.channel.inbound import InboundEvent
from app.db.uow import UnitOfWork
from app.domain.consent import evaluate_consent_turn
from app.domain.moderation import (
    CALM_ABUSE_WARNING,
    check_abuse_lexicon,
    record_moderation_event,
    reopen_conversation,
    should_reopen_conversation,
)
from app.logging import LogContext
from app.models import Message
from app.models.enums import (
    ChannelEnum,
    ConversationStatusEnum,
    DirectionEnum,
)
from app.services.conversation import resolve_conversation

logger = logging.getLogger(__name__)

__all__ = ["SAFE_FALLBACK_REPLY", "TurnResult", "TurnService"]


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
        blackout_sentiment: str | None = None,
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
            or datetime.now(UTC)
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

            # Invariant: Blocked candidate rejected at the very first gate
            if candidate.blocked_at is not None:
                return TurnResult(
                    candidate_id=candidate.id,
                    conversation_id=None,
                    reply_text="",
                    directive="blocked",
                    mode="blocked",
                    is_closed=True,
                    inbound_message_id=None,
                    outbound_message_id=None,
                )

            # Check if candidate has a recent conversation that was closed, escalated, or disengaged
            latest_conv = uow.conversations.get_latest(candidate.id)

            # If previous conversation was escalated, replies stop
            if latest_conv and latest_conv.status in (
                ConversationStatusEnum.escalated,
                ConversationStatusEnum.blocked,
            ):
                inbound_msg = Message(
                    conversation_id=latest_conv.id,
                    candidate_id=candidate.id,
                    direction=DirectionEnum.inbound,
                    channel_message_id=inbound.channel_message_id,
                    body=message_body,
                    media_ref=media_ref,
                    created_at=current_time,
                )
                uow.messages.add(inbound_msg)
                latest_conv.last_inbound_at = current_time
                uow.commit()
                return TurnResult(
                    candidate_id=candidate.id,
                    conversation_id=latest_conv.id,
                    reply_text="",
                    directive="disengage_silent",
                    mode=latest_conv.mode.value
                    if hasattr(latest_conv.mode, "value")
                    else str(latest_conv.mode),
                    is_closed=True,
                    inbound_message_id=inbound_msg.id,
                    outbound_message_id=None,
                )

            # Check if candidate has a recent conversation closed due to disengagement
            if (
                latest_conv
                and latest_conv.status
                in (ConversationStatusEnum.closed, ConversationStatusEnum.disengaged)
                and latest_conv.deflection_count >= 3
            ):
                if not should_reopen_conversation(message_body):
                    inbound_msg = Message(
                        conversation_id=latest_conv.id,
                        candidate_id=candidate.id,
                        direction=DirectionEnum.inbound,
                        channel_message_id=inbound.channel_message_id,
                        body=message_body,
                        media_ref=media_ref,
                        created_at=current_time,
                    )
                    uow.messages.add(inbound_msg)
                    latest_conv.last_inbound_at = current_time
                    uow.commit()
                    return TurnResult(
                        candidate_id=candidate.id,
                        conversation_id=latest_conv.id,
                        reply_text="",
                        directive="disengage_silent",
                        mode=latest_conv.mode.value
                        if hasattr(latest_conv.mode, "value")
                        else str(latest_conv.mode),
                        is_closed=True,
                        inbound_message_id=inbound_msg.id,
                        outbound_message_id=None,
                    )
                else:
                    reopen_conversation(latest_conv)
                    uow.conversations.add(latest_conv)
                    conversation = latest_conv
            else:
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

            # Deterministic ingress abuse check before any model call (§3, FLOW-029)
            if check_abuse_lexicon(message_body):
                conversation.abuse_count += 1
                if conversation.abuse_count == 1:
                    # Strike 1: One calm warning, record event, zero model calls
                    record_moderation_event(
                        uow=uow,
                        candidate_id=cand_id,
                        conversation_id=conv_id,
                        message_id=inbound_msg_id,
                        kind="warn_abuse",
                        detail="Abusive language detected by ingress lexicon",
                    )
                    outbound_msg = Message(
                        conversation_id=conv_id,
                        candidate_id=cand_id,
                        direction=DirectionEnum.outbound,
                        channel_message_id=f"out-{uuid4()}",
                        body=CALM_ABUSE_WARNING,
                        created_at=current_time,
                    )
                    uow.messages.add(outbound_msg)
                    conversation.last_outbound_at = current_time
                    uow.commit()
                    return TurnResult(
                        candidate_id=cand_id,
                        conversation_id=conv_id,
                        reply_text=CALM_ABUSE_WARNING,
                        directive="warn_abuse",
                        mode=conversation.mode.value
                        if hasattr(conversation.mode, "value")
                        else str(conversation.mode),
                        is_closed=False,
                        inbound_message_id=inbound_msg_id,
                        outbound_message_id=outbound_msg.id,
                    )
                else:
                    # Strike 2: Escalate, record event, silence (replies stop), zero model calls
                    conversation.status = ConversationStatusEnum.escalated
                    conversation.closed_at = current_time
                    record_moderation_event(
                        uow=uow,
                        candidate_id=cand_id,
                        conversation_id=conv_id,
                        message_id=inbound_msg_id,
                        kind="escalated",
                        detail="Repeated abuse detected by ingress lexicon; conversation escalated",
                    )
                    uow.commit()
                    return TurnResult(
                        candidate_id=cand_id,
                        conversation_id=conv_id,
                        reply_text="",
                        directive="disengage_silent",
                        mode=conversation.mode.value
                        if hasattr(conversation.mode, "value")
                        else str(conversation.mode),
                        is_closed=True,
                        inbound_message_id=inbound_msg_id,
                        outbound_message_id=None,
                    )

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
                if consent_decision.directive == "consent_withdrawn":
                    from app.services.privacy import erase_candidate_data

                    erase_candidate_data(
                        uow=uow,
                        candidate_id=cand_id,
                        actor_type="candidate",
                        actor_id=str(cand_id),
                        now=current_time,
                    )

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
                    mode=conversation.mode.value
                    if hasattr(conversation.mode, "value")
                    else str(conversation.mode),
                    is_closed=conversation.status == ConversationStatusEnum.closed,
                    inbound_message_id=inbound_msg_id,
                    outbound_message_id=outbound_msg.id,
                )

            # Step 2.5: Check disengagement / silence guard
            if (
                conversation.status == ConversationStatusEnum.closed
                or conversation.deflection_count >= 3
            ):
                if should_reopen_conversation(message_body):
                    reopen_conversation(conversation)
                else:
                    # Suppress model call entirely; return silence
                    uow.commit()
                    return TurnResult(
                        candidate_id=cand_id,
                        conversation_id=conv_id,
                        reply_text="",
                        directive="disengage_silent",
                        mode=conversation.mode.value
                        if hasattr(conversation.mode, "value")
                        else str(conversation.mode),
                        is_closed=True,
                        inbound_message_id=inbound_msg_id,
                        outbound_message_id=None,
                    )

            # Step 2.6: Check daily candidate model budget (FLOW-042)
            from app.api.limits import POLITE_HOLD_MESSAGE, check_daily_model_budget
            from app.config import get_settings

            settings = get_settings()
            if not check_daily_model_budget(
                uow, cand_id, daily_limit=settings.daily_model_call_budget, now=current_time
            ):
                outbound_msg = Message(
                    conversation_id=conv_id,
                    candidate_id=cand_id,
                    direction=DirectionEnum.outbound,
                    channel_message_id=f"out-{uuid4()}",
                    body=POLITE_HOLD_MESSAGE,
                    created_at=current_time,
                )
                uow.messages.add(outbound_msg)
                conversation.last_outbound_at = current_time
                uow.commit()

                return TurnResult(
                    candidate_id=cand_id,
                    conversation_id=conv_id,
                    reply_text=POLITE_HOLD_MESSAGE,
                    directive="daily_budget_exhausted",
                    mode=conversation.mode.value
                    if hasattr(conversation.mode, "value")
                    else str(conversation.mode),
                    is_closed=False,
                    inbound_message_id=inbound_msg_id,
                    outbound_message_id=outbound_msg.id,
                )

            uow.commit()

        # Step 3: Consent granted -> run ADK agent pipeline.
        # Called for its side effect: ensures the ADK session exists and carries
        # the trusted candidate_id in state before the first model call.
        with LogContext(candidate_id=str(cand_id), conversation_id=str(conv_id)):
            adk_session = await get_or_create_adk_session(
                session_service=self.session_service,
                candidate=candidate,
                conversation=conversation,
            )
            if blackout_sentiment:
                adk_session.state["temp:blackout_sentiment"] = blackout_sentiment

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
                    # Extract directive if emitted by policy agent
                    if event.actions and event.actions.state_delta:
                        dir_dict = event.actions.state_delta.get(
                            "directive"
                        ) or event.actions.state_delta.get("temp:directive")
                        if dir_dict and isinstance(dir_dict, dict):
                            directive_name = dir_dict.get("name", directive_name)

                    if event.content and event.content.parts:
                        text_parts = [p.text for p in event.content.parts if p.text]
                        if text_parts:
                            reply_text = "".join(text_parts)

            except Exception as err:
                logger.error(
                    "Model or pipeline error during turn for candidate %s: %s",
                    cand_id,
                    err,
                    exc_info=True,
                )
                reply_text = _get_fallback_reply()
                directive_name = "error_fallback"

        # Handle disengage_silent: suppress outbound reply and model call
        if directive_name == "disengage_silent":
            with uow:
                conv_record = uow.conversations.get_by_id(conv_id)
                if conv_record:
                    conv_record.status = ConversationStatusEnum.closed
                uow.commit()

            return TurnResult(
                candidate_id=cand_id,
                conversation_id=conv_id,
                reply_text="",
                directive="disengage_silent",
                mode="intake",
                is_closed=True,
                inbound_message_id=inbound_msg_id,
                outbound_message_id=None,
            )

        if not reply_text:
            reply_text = (
                "Hey! Priya here from Flow. Great to connect! To help find the right opportunities, "
                "could you share a bit about yourself? Your current role, experience, core skills, "
                "location preference, expected compensation, and notice period. Feel free to take your time!"
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
                created_at=datetime.now(UTC),
            )
            uow.messages.add(outbound_msg)
            if conv_record:
                conv_record.last_outbound_at = datetime.now(UTC)
            uow.commit()

            mode_val = (
                conv_record.mode.value
                if conv_record and hasattr(conv_record.mode, "value")
                else "intake"
            )
            is_closed = (
                conv_record.status == ConversationStatusEnum.closed if conv_record else False
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
