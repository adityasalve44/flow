"""
app/repositories package — clean data-access layer.

Repositories only read and stage (flush); transactions are owned
by the UnitOfWork context manager in app/db/uow.py.
"""

from app.repositories.attribute import AttributeRepository
from app.repositories.audit import AuditRepository
from app.repositories.candidate import (
    CandidateRepository,
    get_candidate_by_id,
    get_candidate_by_phone,
)
from app.repositories.conversation import (
    ConversationRepository,
    MessageRepository,
    get_active_conversation,
    get_recent_messages,
)
from app.repositories.profile import ProfileRepository
from app.repositories.resume import ResumeRepository

__all__ = [
    "AttributeRepository",
    "AuditRepository",
    "CandidateRepository",
    "ConversationRepository",
    "MessageRepository",
    "ProfileRepository",
    "ResumeRepository",
    "get_candidate_by_id",
    "get_candidate_by_phone",
    "get_active_conversation",
    "get_recent_messages",
]
