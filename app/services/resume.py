"""
app/services/resume.py — Resume versioning and storage service (FLOW-035).

Key invariants (§6, §8, §11, FLOW-035 of REVIEW_AND_PLAN.md):
- Exactly one current resume; every previous one preserved.
- Three uploads yield versions 1, 2, 3 with only version 3 current.
- Re-uploading an identical file (matching checksum) adds no version; it confirms the existing version.
- Binary data goes to object storage via StorageAdapter; only metadata in PostgreSQL.
- Every state change and URL generation writes an AuditEvent.
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.channel.media import ValidatedMedia
from app.config import get_settings
from app.db.uow import UnitOfWork
from app.models.audit import AuditEvent
from app.models.enums import SourceEnum
from app.models.resume import Resume
from app.storage.base import StorageAdapter, get_storage_adapter


def ingest_resume(
    uow: UnitOfWork,
    candidate_id: UUID,
    validated_media: ValidatedMedia,
    storage: StorageAdapter | None = None,
) -> tuple[Resume, bool]:
    """
    Ingest a validated resume media object for a candidate.

    Returns:
        (resume, is_new_version)
        - is_new_version is True if a new version was uploaded and stored.
        - is_new_version is False if identical checksum was found and deduplicated.
    """
    adapter = storage or get_storage_adapter()
    now = datetime.now(timezone.utc)

    # 1. Deduplication check: Has this candidate uploaded this exact file before?
    existing = uow.resumes.get_by_checksum(candidate_id, validated_media.checksum)
    if existing is not None:
        # Confirm existing version instead of inserting a duplicate
        existing.confirmed_at = now
        if not existing.is_current:
            uow.resumes.demote_current(candidate_id)
            existing.is_current = True

        uow.audit_events.add(
            AuditEvent(
                actor_type="candidate",
                actor_id=str(candidate_id),
                entity_type="resume",
                entity_id=str(existing.id),
                action="confirm_duplicate",
                before=None,
                after={
                    "version": existing.version,
                    "checksum": existing.checksum,
                    "confirmed_at": now.isoformat(),
                },
            )
        )
        return existing, False

    # 2. New versioning transaction
    resume_id = uuid4()
    object_key = f"candidates/{candidate_id}/resumes/{resume_id}{validated_media.extension}"
    bucket = get_settings().supabase_resume_bucket

    # Demote previous current resume
    uow.resumes.demote_current(candidate_id)

    # Max version + 1
    next_version = uow.resumes.get_max_version(candidate_id) + 1

    # Upload file data to storage
    adapter.put(
        object_key=object_key,
        data=validated_media.data,
        content_type=validated_media.content_type,
    )

    # Create Resume entity
    resume = Resume(
        id=resume_id,
        candidate_id=candidate_id,
        version=next_version,
        bucket=bucket,
        object_key=object_key,
        filename=validated_media.filename,
        content_type=validated_media.content_type,
        size_bytes=validated_media.size_bytes,
        checksum=validated_media.checksum,
        is_current=True,
        source=SourceEnum.candidate_stated,
        uploaded_at=now,
        confirmed_at=now,
        parse_status="pending",
    )
    uow.resumes.add(resume)

    # Record audit event
    uow.audit_events.add(
        AuditEvent(
            actor_type="candidate",
            actor_id=str(candidate_id),
            entity_type="resume",
            entity_id=str(resume.id),
            action="upload",
            before=None,
            after={
                "version": next_version,
                "filename": validated_media.filename,
                "checksum": validated_media.checksum,
                "size_bytes": validated_media.size_bytes,
                "object_key": object_key,
            },
        )
    )

    return resume, True


def confirm_current_resume(
    uow: UnitOfWork,
    candidate_id: UUID,
    actor_type: str = "candidate",
    actor_id: str | None = None,
) -> Resume | None:
    """
    Candidate confirms that their existing resume on file is still latest.
    Sets confirmed_at to now and records audit event.
    """
    resume = uow.resumes.get_current(candidate_id)
    if resume is None:
        return None

    prev_confirmed = resume.confirmed_at.isoformat() if resume.confirmed_at else None
    now = datetime.now(timezone.utc)
    resume.confirmed_at = now

    uow.audit_events.add(
        AuditEvent(
            actor_type=actor_type,
            actor_id=actor_id or str(candidate_id),
            entity_type="resume",
            entity_id=str(resume.id),
            action="confirm",
            before={"confirmed_at": prev_confirmed},
            after={"confirmed_at": now.isoformat()},
        )
    )
    return resume


def get_resume_download_url(
    uow: UnitOfWork,
    resume_id: UUID,
    storage: StorageAdapter | None = None,
    expires_in: int = 3600,
    actor_type: str = "system",
    actor_id: str | None = None,
) -> str:
    """
    Generate short-TTL signed URL for resume access and write an audit event.
    """
    resume = uow.resumes.get_by_id(resume_id)
    if resume is None:
        raise ValueError(f"Resume {resume_id} not found")

    adapter = storage or get_storage_adapter()
    signed_url = adapter.signed_url(resume.object_key, expires_in=expires_in)

    uow.audit_events.add(
        AuditEvent(
            actor_type=actor_type,
            actor_id=actor_id,
            entity_type="resume",
            entity_id=str(resume.id),
            action="generate_signed_url",
            before=None,
            after={"expires_in": expires_in, "object_key": resume.object_key},
        )
    )
    return signed_url


def get_current_resume(uow: UnitOfWork, candidate_id: UUID) -> Resume | None:
    """Retrieve the current resume for candidate, if any."""
    return uow.resumes.get_current(candidate_id)


def get_candidate_resumes(uow: UnitOfWork, candidate_id: UUID) -> list[Resume]:
    """Retrieve all resume versions for candidate ordered by version desc."""
    return uow.resumes.get_by_candidate(candidate_id)
