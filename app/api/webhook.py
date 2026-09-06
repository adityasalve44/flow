"""
app/api/webhook.py — Inbound channel webhook endpoint (FLOW-023).

Core requirements (§8, FLOW-023 of REVIEW_AND_PLAN.md):
- Accept full InboundEvent including contact_name, phone_number, message, channel_message_id.
- Shared-secret header check (X-Webhook-Secret or Bearer token).
- Boundary defense via process_ingress:
    * Idempotency on channel_message_id -> REPLAY (returns 200 quickly and idempotently)
    * Blocked candidate gate -> BLOCKED (no turn execution)
    * Per-phone rate limit in Postgres -> RATE_LIMITED (HTTP 429)
- On PROCEED: invokes TurnService.run(inbound), returning reply text so the simulator can display it.
- Structured error handling — zero unhandled exceptions or stack traces leaked to callers.
"""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.schemas import InboundEvent
from app.channel.inbound import IngressDecision, process_ingress
from app.config import get_settings
from app.database import get_db
from app.db.uow import UnitOfWork
from app.logging import get_logger
from app.models.enums import DirectionEnum
from app.services.turn import TurnService

logger = get_logger(__name__)

router = APIRouter(tags=["webhook"])


class WebhookResponse(BaseModel):
    """Structured response for channel webhook requests."""

    status: str = Field(..., description="'ok' or 'error'")
    decision: str = Field(..., description="ingress/processing decision: proceed, replay, blocked, rate_limited")
    channel_message_id: str = Field(..., description="Unique message ID from the channel")
    reply_text: str | None = Field(None, description="Outbound reply text for the message")
    candidate_id: str | None = Field(None, description="ID of the candidate")
    conversation_id: str | None = Field(None, description="ID of the conversation")
    directive: str | None = Field(None, description="Directive executed by policy agent")
    mode: str | None = Field(None, description="Conversation mode")
    detail: str | None = Field(None, description="Optional diagnostic or error message")


def verify_webhook_secret(
    x_webhook_secret: str | None = Header(None, alias="X-Webhook-Secret"),
    x_flow_secret: str | None = Header(None, alias="X-Flow-Secret"),
    authorization: str | None = Header(None),
) -> None:
    """Validate shared-secret webhook credentials against configuration."""
    settings = get_settings()
    expected_secret = settings.webhook_secret.strip()

    if not expected_secret:
        # In local/test environments where no secret is configured, allow requests
        return

    provided_secret = x_webhook_secret or x_flow_secret
    if not provided_secret and authorization:
        if authorization.lower().startswith("bearer "):
            provided_secret = authorization[7:].strip()
        else:
            provided_secret = authorization.strip()

    if not provided_secret or not hmac.compare_digest(provided_secret, expected_secret):
        logger.warning("Rejected webhook request due to invalid or missing secret")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook authentication credentials",
        )


def get_turn_service(db: Session = Depends(get_db)) -> TurnService:
    """Dependency providing a configured TurnService instance."""
    uow = UnitOfWork(session=db)
    return TurnService(uow=uow)


@router.post(
    "/webhook",
    response_model=WebhookResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_webhook_secret)],
)
async def handle_inbound_webhook(
    event: InboundEvent,
    response: Response,
    db: Session = Depends(get_db),
    turn_service: TurnService = Depends(get_turn_service),
) -> WebhookResponse:
    """Process an inbound WhatsApp/simulator message turn.

    Guarantees:
    - Replay of an existing channel_message_id is idempotent (200 OK, no duplicate processing).
    - Blocked numbers are dropped immediately without reply.
    - Rate-limited numbers are rejected with 429 Too Many Requests.
    - On proceed, executes single-turn pipeline and returns the outbound reply.
    - Structured error handling: catches errors gracefully without leaking stack traces.
    """
    uow = UnitOfWork(session=db)

    # 1. Evaluate ingress boundary defenses
    try:
        with uow:
            ingress = process_ingress(uow=uow, event=event)
    except Exception as err:
        logger.error(
            f"Database error during ingress evaluation for message {event.channel_message_id}: {err}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error processing ingress boundary checks",
        ) from err

    # Handle Replay (Idempotent return)
    if ingress.decision == IngressDecision.REPLAY:
        reply_body = None
        # Retrieve the corresponding outbound reply if available
        if ingress.existing_message:
            with uow:
                recent_msgs = uow.messages.get_recent(ingress.existing_message.conversation_id, limit=5)
                for m in recent_msgs:
                    if m.direction == DirectionEnum.outbound and m.created_at >= ingress.existing_message.created_at:
                        reply_body = m.body
                        break

        return WebhookResponse(
            status="ok",
            decision="replay",
            channel_message_id=event.channel_message_id,
            reply_text=reply_body,
            candidate_id=str(ingress.candidate.id) if ingress.candidate else None,
            detail=ingress.detail or "Message already processed (idempotent replay)",
        )

    # Handle Blocked candidate
    if ingress.decision == IngressDecision.BLOCKED:
        return WebhookResponse(
            status="ok",
            decision="blocked",
            channel_message_id=event.channel_message_id,
            candidate_id=str(ingress.candidate.id) if ingress.candidate else None,
            detail=ingress.detail or "Candidate is blocked",
        )

    # Handle Rate Limited
    if ingress.decision == IngressDecision.RATE_LIMITED:
        response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
        return WebhookResponse(
            status="error",
            decision="rate_limited",
            channel_message_id=event.channel_message_id,
            candidate_id=str(ingress.candidate.id) if ingress.candidate else None,
            detail=ingress.detail or "Rate limit exceeded",
        )

    # 2. Ingress passed: execute single-turn processing pipeline
    try:
        turn_result = await turn_service.run(event)
    except Exception as err:
        logger.error(
            f"Unhandled error during turn execution for message {event.channel_message_id}: {err}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while processing the conversation turn",
        ) from err

    return WebhookResponse(
        status="ok",
        decision="proceed",
        channel_message_id=event.channel_message_id,
        reply_text=turn_result.reply_text,
        candidate_id=str(turn_result.candidate_id),
        conversation_id=str(turn_result.conversation_id),
        directive=turn_result.directive,
        mode=turn_result.mode,
        detail="Turn processed successfully",
    )
