"""
app/services/conversation.py — conversation lifecycle resolution service.

Determines whether to continue an active conversation, close it and open
a new intake conversation, or open in refresh mode.
"""

from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.db.uow import UnitOfWork
from app.models import Candidate, Conversation
from app.models.enums import (
    ChannelEnum,
    ConsentStatusEnum,
    ConversationModeEnum,
    ConversationStatusEnum,
)


def resolve_conversation(
    uow: UnitOfWork,
    candidate: Candidate,
    now: datetime | None = None,
    channel: ChannelEnum = ChannelEnum.simulator,
) -> Conversation:
    """
    Resolve the active conversation for a candidate.

    Invariants (§8 of REVIEW_AND_PLAN.md & FLOW-016):
    - Within active_window_hours (e.g. 24h) -> continue the existing conversation.
    - Beyond active_window_hours -> close the previous conversation and open a new one.
    - If profile is older than stale_profile_days (e.g. 365 days) -> open in mode=refresh
      and mark current attributes as stale (never delete anything).
    - If consent is not yet granted -> open in mode=consent.
    """
    settings = get_settings()
    current_time = now or datetime.now(timezone.utc)
    active_window = timedelta(hours=settings.active_window_hours)
    stale_window = timedelta(days=settings.stale_profile_days)

    active_conv = uow.conversations.get_active(candidate.id)

    if active_conv is not None:
        last_activity = active_conv.last_inbound_at or active_conv.started_at
        elapsed = current_time - last_activity

        if elapsed <= active_window:
            # Within active window: continue the existing conversation
            active_conv.last_inbound_at = current_time
            uow.conversations.add(active_conv)
            return active_conv

        # Beyond active window: close the old conversation
        active_conv.status = ConversationStatusEnum.closed
        active_conv.closed_at = current_time
        uow.conversations.add(active_conv)

    # Determine mode for the new conversation
    profile = candidate.profile
    is_stale_profile = False

    if profile is not None and profile.last_refreshed_at is not None:
        profile_age = current_time - profile.last_refreshed_at
        if profile_age > stale_window:
            is_stale_profile = True

    if is_stale_profile:
        # Profile is older than stale_profile_days: open in refresh mode
        mode = ConversationModeEnum.refresh
        # Mark all current facts as stale, preserving history
        uow.attributes.mark_all_current_as_stale(candidate.id)
    elif candidate.consent_status == ConsentStatusEnum.granted:
        mode = ConversationModeEnum.intake
    else:
        # Consent not yet granted
        mode = ConversationModeEnum.consent

    # Create new conversation
    new_conv = uow.conversations.create(
        candidate_id=candidate.id,
        channel=channel,
        mode=mode,
    )
    new_conv.started_at = current_time
    new_conv.last_inbound_at = current_time
    uow.conversations.add(new_conv)
    return new_conv
