"""
app/models/__init__.py — re-exports all models and the shared Base.

Import from here to get everything:
    from app.models import Base, Candidate, Conversation, Message
"""

from app.models.base import Base, TimestampMixin
from app.models.enums import (
    AttributeStatusEnum,
    ChannelEnum,
    ConfidenceEnum,
    ConsentStatusEnum,
    ConversationModeEnum,
    ConversationStatusEnum,
    DataClassEnum,
    DirectionEnum,
    LifecycleStatusEnum,
    SourceEnum,
)
from app.models.candidate import (
    Candidate,
    CandidateProfile,
    Conversation,
    Message,
)
from app.models.attribute import CandidateAttribute
from app.models.profile import (
    CandidateSkill,
    CandidateRolePref,
    CandidateLocationPref,
)
from app.models.moderation import ModerationEvent
from app.models.resume import Resume
from app.models.audit import AuditEvent

__all__ = [
    "Base",
    "TimestampMixin",
    # Enums
    "AttributeStatusEnum",
    "ChannelEnum",
    "ConfidenceEnum",
    "ConsentStatusEnum",
    "ConversationModeEnum",
    "ConversationStatusEnum",
    "DataClassEnum",
    "DirectionEnum",
    "LifecycleStatusEnum",
    "SourceEnum",
    # Models
    "AuditEvent",
    "Candidate",
    "CandidateAttribute",
    "CandidateLocationPref",
    "CandidateProfile",
    "CandidateRolePref",
    "CandidateSkill",
    "Conversation",
    "Message",
    "ModerationEvent",
    "Resume",
]
