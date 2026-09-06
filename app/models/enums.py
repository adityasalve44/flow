"""
app/models/enums.py — all PostgreSQL enum types for the Flow schema.

Every enum is defined once here so SQLAlchemy, Alembic and application code
share a single source of truth.  Values match §6 of the architecture review.

Important: ``LifecycleStatus`` contains ONLY the five candidate-lifecycle
states.  Application-pipeline states (shortlisted, submitted, interview,
selected, joined, rejected) are NEVER on the candidate record — they belong
to the future matching system (Q8, decided 2026-09-06).
"""

import enum


class SourceEnum(str, enum.Enum):
    """Where a candidate attribute value came from."""
    candidate_stated = "candidate_stated"
    candidate_confirmed = "candidate_confirmed"
    resume = "resume"
    llm_inferred = "llm_inferred"
    system_calculated = "system_calculated"
    recruiter_verified = "recruiter_verified"
    channel_metadata = "channel_metadata"


class ConfidenceEnum(str, enum.Enum):
    """How sure we are about a fact."""
    confirmed = "confirmed"
    ambiguous = "ambiguous"
    inferred = "inferred"
    unknown = "unknown"


class AttributeStatusEnum(str, enum.Enum):
    """Lifecycle of a single attribute value."""
    current = "current"
    superseded = "superseded"
    stale = "stale"
    conflicted = "conflicted"
    rejected = "rejected"


class DataClassEnum(str, enum.Enum):
    """Three-class data model for sensitivity (Q5, decided 2026-09-06).

    operational — safe for projection, indexing, matching
    personal    — stored, restricted; never in search results or list payloads
    protected   — stored with strong access control; never in any automated output
    """
    operational = "operational"
    personal = "personal"
    protected = "protected"


class LifecycleStatusEnum(str, enum.Enum):
    """Five-state candidate lifecycle (Q8, decided 2026-09-06).

    The matching system owns per-application states such as shortlisted,
    submitted, interview, selected, rejected and joined.  Those states
    NEVER appear on the candidate record.
    """
    new = "new"
    intake = "intake"
    profile_ready = "profile_ready"
    dormant = "dormant"
    blocked = "blocked"


class ConsentStatusEnum(str, enum.Enum):
    """Consent lifecycle for persistent candidate data storage (Q4)."""
    pending = "pending"
    granted = "granted"
    declined = "declined"
    withdrawn = "withdrawn"


class ConversationStatusEnum(str, enum.Enum):
    """Status of a single conversation."""
    active = "active"
    awaiting_reply = "awaiting_reply"
    closed = "closed"
    disengaged = "disengaged"
    escalated = "escalated"
    blocked = "blocked"


class ConversationModeEnum(str, enum.Enum):
    """Conversation mode (Q4 adds consent mode)."""
    consent = "consent"
    intake = "intake"
    refresh = "refresh"


class DirectionEnum(str, enum.Enum):
    """Message direction."""
    inbound = "inbound"
    outbound = "outbound"


class ChannelEnum(str, enum.Enum):
    """Communication channel."""
    whatsapp = "whatsapp"
    simulator = "simulator"


class RecruiterRoleEnum(str, enum.Enum):
    """Recruiter account role (FLOW-038).

    Two roles only, matching what Phase 5 actually needs: any active
    recruiter can search, view, correct data and assign candidates; only
    admin can create or deactivate recruiter accounts. Finer-grained
    permissions are not justified until a real organisation with more than
    a handful of recruiters exists to need them.
    """
    recruiter = "recruiter"
    admin = "admin"
