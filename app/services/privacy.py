"""
app/services/privacy.py — Candidate erasure and data retention sweep service (FLOW-043).

Acceptance criteria (§8, FLOW-043 of REVIEW_AND_PLAN.md):
1. A candidate data-erasure path that anonymizes the phone, wipes personal and protected
   attributes, deletes resumes from object storage, clears candidate preference tags,
   redacts messages to [deleted], and leaves an auditable AuditEvent row.
2. Integration with consent withdrawal: candidate stating 'delete my data' or
   'stop contacting me and delete everything' triggers the erasure path.
3. Retention sweep job (runnable via CLI/cron, dry-run supported) that enforces the
   Q5 data classification rules:
   - protected: shortest retention window (default 30 days)
   - personal: 90 days
   - operational: 365 days
   - closed conversations: 180 days
"""

import argparse
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update

from app.config import get_settings
from app.db.uow import UnitOfWork
from app.models.attribute import CandidateAttribute
from app.models.audit import AuditEvent
from app.models.candidate import Candidate, CandidateProfile, Conversation, Message
from app.models.enums import (
    ConsentStatusEnum,
    ConversationStatusEnum,
    DataClassEnum,
    LifecycleStatusEnum,
)
from app.models.profile import (
    CandidateLocationPref,
    CandidateRolePref,
    CandidateSkill,
)
from app.models.resume import Resume
from app.storage.base import StorageAdapter, get_storage_adapter

logger = logging.getLogger(__name__)


def erase_candidate_data(
    uow: UnitOfWork,
    candidate_id: UUID | str,
    storage: StorageAdapter | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
    anonymize_phone: bool = True,
    now: datetime | None = None,
) -> dict[str, int | str]:
    """
    Execute full PII erasure for a candidate.

    Guarantees:
    - Anonymizes phone number to '+deleted_<hex>' (ensures unique index invariant).
    - Clears display_name.
    - Sets consent_status to 'withdrawn', lifecycle_status to 'dormant', and blocked_at.
    - Deletes all personal and protected attributes.
    - Clears candidate profile and preference tags (skills, role prefs, location prefs).
    - Deletes resumes from object storage and removes resume records.
    - Redacts all messages to '[deleted]' and clears media_ref.
    - Closes any active conversations.
    - Emits an auditable AuditEvent row before returning.
    """
    cand_uuid = UUID(str(candidate_id)) if not isinstance(candidate_id, UUID) else candidate_id
    current_time = now or datetime.now(UTC)

    candidate = uow.session.get(Candidate, cand_uuid)
    if candidate is None:
        return {"candidate_id": str(cand_uuid), "status": "not_found"}

    old_phone = candidate.phone_number
    old_display_name = candidate.display_name

    # 1. Resumes: delete from object storage and database
    storage_adapter = storage or get_storage_adapter()
    resumes = list(
        uow.session.scalars(select(Resume).where(Resume.candidate_id == cand_uuid)).all()
    )
    resumes_deleted = 0
    for r in resumes:
        try:
            storage_adapter.delete(r.object_key)
        except Exception as err:
            logger.warning("Failed to delete storage object %s: %s", r.object_key, err)
        uow.session.delete(r)
        resumes_deleted += 1

    # 2. Attributes: wipe personal and protected attributes
    attr_stmt = select(CandidateAttribute.id).where(
        CandidateAttribute.candidate_id == cand_uuid,
        CandidateAttribute.data_class.in_([DataClassEnum.personal, DataClassEnum.protected]),
    )
    attr_ids = list(uow.session.scalars(attr_stmt).all())
    attributes_deleted = 0
    if attr_ids:
        # Break self-referential foreign keys
        uow.session.execute(
            update(CandidateAttribute)
            .where(CandidateAttribute.id.in_(attr_ids))
            .values(superseded_by_id=None)
        )
        del_attr_res = uow.session.execute(
            delete(CandidateAttribute).where(CandidateAttribute.id.in_(attr_ids))
        )
        attributes_deleted = del_attr_res.rowcount or len(attr_ids)

    # 3. Preference tags: skills, role prefs, location prefs
    skills_deleted = (
        uow.session.execute(
            delete(CandidateSkill).where(CandidateSkill.candidate_id == cand_uuid)
        ).rowcount
        or 0
    )
    role_prefs_deleted = (
        uow.session.execute(
            delete(CandidateRolePref).where(CandidateRolePref.candidate_id == cand_uuid)
        ).rowcount
        or 0
    )
    location_prefs_deleted = (
        uow.session.execute(
            delete(CandidateLocationPref).where(CandidateLocationPref.candidate_id == cand_uuid)
        ).rowcount
        or 0
    )

    # 4. Profile projection: delete or clear
    profiles_deleted = (
        uow.session.execute(
            delete(CandidateProfile).where(CandidateProfile.candidate_id == cand_uuid)
        ).rowcount
        or 0
    )

    # 5. Redact messages: overwrite body with '[deleted]' and clear media_ref
    messages_redacted = (
        uow.session.execute(
            update(Message)
            .where(Message.candidate_id == cand_uuid)
            .values(body="[deleted]", media_ref=None)
        ).rowcount
        or 0
    )

    # 6. Close any open conversations
    conversations_closed = (
        uow.session.execute(
            update(Conversation)
            .where(
                Conversation.candidate_id == cand_uuid,
                Conversation.status != ConversationStatusEnum.closed,
            )
            .values(status=ConversationStatusEnum.closed, closed_at=current_time)
        ).rowcount
        or 0
    )

    # 7. Anonymize candidate record
    if anonymize_phone:
        candidate.phone_number = f"+deleted_{uuid4().hex[:16]}"
    candidate.display_name = None
    candidate.consent_status = ConsentStatusEnum.withdrawn
    candidate.lifecycle_status = LifecycleStatusEnum.dormant
    candidate.blocked_at = current_time
    candidate.assigned_recruiter_id = None

    # 8. Record audit event
    audit = AuditEvent(
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="candidate",
        entity_id=str(cand_uuid),
        action="candidate_erasure",
        before={"phone_number": old_phone, "display_name": old_display_name},
        after={
            "consent_status": ConsentStatusEnum.withdrawn.value,
            "lifecycle_status": LifecycleStatusEnum.dormant.value,
            "attributes_deleted": attributes_deleted,
            "resumes_deleted": resumes_deleted,
            "messages_redacted": messages_redacted,
            "skills_deleted": skills_deleted,
            "role_prefs_deleted": role_prefs_deleted,
            "location_prefs_deleted": location_prefs_deleted,
            "profiles_deleted": profiles_deleted,
            "conversations_closed": conversations_closed,
        },
    )
    uow.audit_events.add(audit)
    uow.session.flush()

    return {
        "candidate_id": str(cand_uuid),
        "status": "erased",
        "attributes_deleted": attributes_deleted,
        "resumes_deleted": resumes_deleted,
        "messages_redacted": messages_redacted,
        "skills_deleted": skills_deleted,
        "role_prefs_deleted": role_prefs_deleted,
        "location_prefs_deleted": location_prefs_deleted,
        "profiles_deleted": profiles_deleted,
        "conversations_closed": conversations_closed,
    }


@dataclass
class RetentionConfig:
    protected_days: int = 30
    personal_days: int = 90
    operational_days: int = 365
    closed_conversation_days: int = 180


def get_default_retention_config() -> RetentionConfig:
    """Load retention periods from configuration settings."""
    settings = get_settings()
    return RetentionConfig(
        protected_days=settings.retention_protected_days,
        personal_days=settings.retention_personal_days,
        operational_days=settings.retention_operational_days,
        closed_conversation_days=settings.retention_closed_conversation_days,
    )


@dataclass
class RetentionSweepResult:
    protected_attributes_pruned: int = 0
    personal_attributes_pruned: int = 0
    operational_attributes_pruned: int = 0
    conversations_pruned: int = 0
    messages_pruned: int = 0
    dry_run: bool = False


def run_retention_sweep(
    uow: UnitOfWork,
    storage: StorageAdapter | None = None,
    retention_config: RetentionConfig | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> RetentionSweepResult:
    """
    Run the data retention sweep job enforcing Q5 data classification rules.

    Cutoffs:
    - protected: 30 days
    - personal: 90 days
    - operational: 365 days
    - closed conversations: 180 days

    If dry_run is True, returns counts of expired rows without modifying database or storage.
    If dry_run is False, deletes expired rows and emits an AuditEvent.
    """
    config = retention_config or get_default_retention_config()
    current_time = now or datetime.now(UTC)

    cutoff_protected = current_time - timedelta(days=config.protected_days)
    cutoff_personal = current_time - timedelta(days=config.personal_days)
    cutoff_operational = current_time - timedelta(days=config.operational_days)
    cutoff_conv = current_time - timedelta(days=config.closed_conversation_days)

    # 1. Identify expired protected attributes
    protected_ids = list(
        uow.session.scalars(
            select(CandidateAttribute.id).where(
                CandidateAttribute.data_class == DataClassEnum.protected,
                CandidateAttribute.created_at < cutoff_protected,
            )
        ).all()
    )

    # 2. Identify expired personal attributes
    personal_ids = list(
        uow.session.scalars(
            select(CandidateAttribute.id).where(
                CandidateAttribute.data_class == DataClassEnum.personal,
                CandidateAttribute.created_at < cutoff_personal,
            )
        ).all()
    )

    # 3. Identify expired operational attributes
    operational_ids = list(
        uow.session.scalars(
            select(CandidateAttribute.id).where(
                CandidateAttribute.data_class == DataClassEnum.operational,
                CandidateAttribute.created_at < cutoff_operational,
            )
        ).all()
    )

    # 4. Identify expired closed conversations
    conv_stmt = select(Conversation.id).where(
        Conversation.status.in_([ConversationStatusEnum.closed, ConversationStatusEnum.disengaged]),
        func.coalesce(Conversation.closed_at, Conversation.started_at) < cutoff_conv,
    )
    expired_conv_ids = list(uow.session.scalars(conv_stmt).all())

    # Count messages linked to those conversations
    messages_count = 0
    if expired_conv_ids:
        msg_stmt = select(func.count(Message.id)).where(
            Message.conversation_id.in_(expired_conv_ids)
        )
        messages_count = uow.session.scalar(msg_stmt) or 0

    result = RetentionSweepResult(
        protected_attributes_pruned=len(protected_ids),
        personal_attributes_pruned=len(personal_ids),
        operational_attributes_pruned=len(operational_ids),
        conversations_pruned=len(expired_conv_ids),
        messages_pruned=messages_count,
        dry_run=dry_run,
    )

    if dry_run:
        return result

    # Execute pruning
    all_attr_ids = protected_ids + personal_ids + operational_ids
    if all_attr_ids:
        # Break self-referential foreign keys before deleting
        uow.session.execute(
            update(CandidateAttribute)
            .where(CandidateAttribute.id.in_(all_attr_ids))
            .values(superseded_by_id=None)
        )
        uow.session.execute(
            delete(CandidateAttribute).where(CandidateAttribute.id.in_(all_attr_ids))
        )

    if expired_conv_ids:
        # Cascade will delete messages
        uow.session.execute(delete(Conversation).where(Conversation.id.in_(expired_conv_ids)))

    # Record retention sweep audit event
    audit = AuditEvent(
        actor_type="system",
        actor_id="retention_sweep_job",
        entity_type="system",
        entity_id="retention_sweep",
        action="retention_sweep",
        before=None,
        after=asdict(result),
    )
    uow.audit_events.add(audit)
    uow.session.flush()

    return result


def main() -> None:
    """CLI runner for retention sweep."""
    parser = argparse.ArgumentParser(description="Run data retention sweep job.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the retention sweep without deleting any records.",
    )
    args = parser.parse_args()

    uow = UnitOfWork()
    with uow:
        res = run_retention_sweep(uow, dry_run=args.dry_run)
        uow.commit()

    print(f"Retention sweep finished (dry_run={args.dry_run}): {asdict(res)}")


if __name__ == "__main__":
    main()
