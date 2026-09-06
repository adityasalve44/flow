"""
app/channel/whatsapp/router.py — FastAPI webhook endpoints for WhatsApp Cloud API (FLOW-041).

Endpoints:
- GET /webhook/whatsapp (and /whatsapp/webhook): Meta webhook subscription handshake.
- POST /webhook/whatsapp (and /whatsapp/webhook): Message notifications with X-Hub-Signature-256.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.api.limits import IPRateLimiter
from app.channel.inbound import IngressDecision, process_ingress
from app.channel.whatsapp.client import WhatsAppClient
from app.channel.whatsapp.parser import parse_whatsapp_payload
from app.channel.whatsapp.security import verify_hub_signature, verify_webhook_handshake
from app.config import get_settings
from app.database import get_db
from app.db.uow import UnitOfWork
from app.logging import get_logger
from app.services.recovery import has_admin_intervened
from app.services.turn import TurnService

logger = get_logger(__name__)

router = APIRouter(tags=["whatsapp"])


def get_whatsapp_client() -> WhatsAppClient:
    """Dependency providing a configured WhatsAppClient instance."""
    return WhatsAppClient()


def get_turn_service(db: Session = Depends(get_db)) -> TurnService:
    """Dependency providing a TurnService instance."""
    uow = UnitOfWork(session=db)
    return TurnService(uow=uow)


@router.get("/webhook/whatsapp", response_class=PlainTextResponse)
@router.get("/whatsapp/webhook", response_class=PlainTextResponse)
def whatsapp_webhook_handshake(
    mode: str | None = Query(None, alias="hub.mode"),
    token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge"),
) -> Response:
    """
    Handle Meta WhatsApp Cloud API subscription verification handshake.

    Returns the challenge string with HTTP 200 on matching verify_token.
    Returns HTTP 403 Forbidden on mismatch.
    """
    settings = get_settings()
    expected_token = settings.whatsapp_verify_token

    try:
        challenge_resp = verify_webhook_handshake(
            mode=mode,
            token=token,
            challenge=challenge,
            expected_token=expected_token,
        )
        return PlainTextResponse(content=challenge_resp, status_code=status.HTTP_200_OK)
    except ValueError as err:
        logger.warning("WhatsApp webhook handshake failed: %s", err)
        return Response(content="Forbidden", status_code=status.HTTP_403_FORBIDDEN)


@router.post("/webhook/whatsapp", status_code=status.HTTP_200_OK, dependencies=[Depends(IPRateLimiter())])
@router.post("/whatsapp/webhook", status_code=status.HTTP_200_OK, dependencies=[Depends(IPRateLimiter())])
async def handle_whatsapp_webhook(
    request: Request,
    signature: str | None = Header(None, alias="X-Hub-Signature-256"),
    turn_service: TurnService = Depends(get_turn_service),
    client: WhatsAppClient = Depends(get_whatsapp_client),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """
    Process inbound WhatsApp Cloud API message events.

    1. Verify X-Hub-Signature-256 HMAC-SHA256 signature.
    2. Parse webhook envelope into InboundEvent DTOs.
    3. Run boundary defenses (idempotency, block, rate limit).
    4. Run TurnService for valid messages.
    5. Dispatch outbound replies via WhatsAppClient.
    6. Return 200 OK immediately.
    """
    settings = get_settings()
    raw_body = await request.body()

    # Step 1: Verify HMAC signature if app_secret is configured
    if settings.whatsapp_app_secret:
        is_valid = verify_hub_signature(
            raw_body=raw_body,
            signature_header=signature,
            app_secret=settings.whatsapp_app_secret,
        )
        if not is_valid:
            logger.warning("Rejected WhatsApp webhook notification: invalid X-Hub-Signature-256")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid signature header",
            )

    # Step 2: Parse payload
    try:
        payload = await request.json()
    except Exception:
        return {"status": "ok"}

    parsed_messages = parse_whatsapp_payload(payload)
    if not parsed_messages:
        # Non-message event (e.g. delivery receipt, status callback)
        return {"status": "ok"}

    uow = UnitOfWork(session=db)

    # Step 3: Process each parsed message
    for parsed in parsed_messages:
        event = parsed.event

        # Ingress boundary checks
        ingress = process_ingress(uow=uow, event=event)
        if ingress.decision != IngressDecision.PROCEED:
            logger.info(
                "WhatsApp ingress boundary rejected message %s with decision: %s",
                event.channel_message_id,
                ingress.decision.value,
            )
            continue

        # Turn execution
        try:
            result = await turn_service.run(event)

            # Check if admin has intervened while system was processing or off
            if result.conversation_id and has_admin_intervened(uow, result.conversation_id):
                logger.info(
                    "Admin has intervened on conversation %s; suppressing automated bot reply",
                    result.conversation_id,
                )
                continue

            if result.reply_text:
                # Retrieve last_inbound_at for 24h window check
                last_inbound = None
                if result.conversation_id:
                    conv = uow.conversations.get_by_id(result.conversation_id)
                    if conv:
                        last_inbound = conv.last_inbound_at

                await client.send_text_message(
                    to_phone=event.phone_number,
                    text=result.reply_text,
                    reply_to_message_id=event.channel_message_id,
                    last_inbound_at=last_inbound,
                )
        except Exception as err:
            logger.error(
                "Error processing WhatsApp turn for message %s: %s",
                event.channel_message_id,
                err,
            )

    return {"status": "ok"}
