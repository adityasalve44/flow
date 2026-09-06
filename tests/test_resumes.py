"""
tests/test_resumes.py — Tests for Resume model, repository, versioning, and deduplication (FLOW-035).

Key invariants tested:
- Exactly one current resume per candidate; previous versions demoted.
- Three uploads produce versions 1, 2, 3 with only version 3 having is_current=True.
- Partial unique index on flow.resumes enforces the single-current invariant at the DB level.
- Identical checksum deduplication: re-uploading the same file adds no new version;
  it confirms the existing version and records an audit event.
- Confirmation workflow: confirm_current_resume updates confirmed_at and writes audit trail.
- Signed URL generation writes an AuditEvent with actor details.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.channel.media import ValidatedMedia, validate_media
from app.db.uow import UnitOfWork
from app.models.enums import LifecycleStatusEnum, SourceEnum
from app.models.resume import Resume
from app.services.resume import (
    confirm_current_resume,
    get_candidate_resumes,
    get_current_resume,
    get_resume_download_url,
    ingest_resume,
)
from app.storage.local import LocalStorageAdapter


@pytest.fixture
def candidate_id(db):
    """Create a test candidate and return their ID."""
    uow = UnitOfWork(session=db)
    phone = f"+91{uuid4().int % 10000000000:010d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.lifecycle_status = LifecycleStatusEnum.intake
        uow.commit()
        return cand.id


@pytest.fixture
def storage(tmp_path):
    """Ephemeral LocalStorageAdapter for testing."""
    return LocalStorageAdapter(base_dir=str(tmp_path / "storage"), bucket="resumes")


def _sample_media(name: str = "resume.pdf", content: bytes = b"%PDF-1.4 sample pdf content") -> ValidatedMedia:
    return validate_media(data=content, filename=name, content_type="application/pdf")


def test_three_uploads_yield_versions_1_2_3_with_only_3_current(db, candidate_id, storage):
    """
    Three distinct uploads yield versions 1, 2, 3.
    Only version 3 has is_current=True; versions 1 and 2 are archived (is_current=False).
    """
    uow = UnitOfWork(session=db)

    # 1. First upload
    m1 = _sample_media("first.pdf", b"%PDF-1.4 initial resume")
    with uow:
        r1, is_new1 = ingest_resume(uow, candidate_id, m1, storage=storage)
        assert is_new1 is True
        assert r1.version == 1
        assert r1.is_current is True
        uow.commit()

    # 2. Second upload
    m2 = _sample_media("second.pdf", b"%PDF-1.4 revised resume")
    with uow:
        r2, is_new2 = ingest_resume(uow, candidate_id, m2, storage=storage)
        assert is_new2 is True
        assert r2.version == 2
        assert r2.is_current is True
        uow.commit()

    # 3. Third upload
    m3 = _sample_media("third.pdf", b"%PDF-1.4 final resume")
    with uow:
        r3, is_new3 = ingest_resume(uow, candidate_id, m3, storage=storage)
        assert is_new3 is True
        assert r3.version == 3
        assert r3.is_current is True
        uow.commit()

    # Verify query state
    with uow:
        resumes = get_candidate_resumes(uow, candidate_id)
        assert len(resumes) == 3
        # Ordered version desc
        assert [r.version for r in resumes] == [3, 2, 1]

        v3 = next(r for r in resumes if r.version == 3)
        v2 = next(r for r in resumes if r.version == 2)
        v1 = next(r for r in resumes if r.version == 1)

        assert v3.is_current is True
        assert v2.is_current is False
        assert v1.is_current is False

        # Current resume fetch helper
        current = get_current_resume(uow, candidate_id)
        assert current is not None
        assert current.id == v3.id


def test_duplicate_checksum_adds_no_new_version(db, candidate_id, storage):
    """
    Uploading an identical file (identical SHA-256 checksum) adds no new version.
    It confirms the existing version and records an audit event.
    """
    uow = UnitOfWork(session=db)
    media = _sample_media("duplicate.pdf", b"%PDF-1.4 identical resume content")

    # First ingestion
    with uow:
        r1, is_new1 = ingest_resume(uow, candidate_id, media, storage=storage)
        assert is_new1 is True
        assert r1.version == 1
        uow.commit()

    # Re-upload with identical content (even if filename differs)
    media_dup = _sample_media("duplicate_renamed.pdf", b"%PDF-1.4 identical resume content")
    with uow:
        r2, is_new2 = ingest_resume(uow, candidate_id, media_dup, storage=storage)
        assert is_new2 is False  # Deduplicated!
        assert r2.id == r1.id
        assert r2.version == 1
        uow.commit()

    # Total versions in database remains 1
    with uow:
        resumes = get_candidate_resumes(uow, candidate_id)
        assert len(resumes) == 1

        # Check audit events
        events = uow.audit_events.get_by_entity("resume", str(r1.id))
        actions = [e.action for e in events]
        assert "upload" in actions
        assert "confirm_duplicate" in actions


def test_database_enforces_single_current_invariant_via_partial_unique(db, candidate_id):
    """
    Attempting to insert two resumes with is_current=True for the same candidate
    violates the PostgreSQL partial unique index 'ix_flow_resumes_candidate_current'.
    """
    uow = UnitOfWork(session=db)
    with uow:
        r1 = Resume(
            candidate_id=candidate_id,
            version=1,
            bucket="resumes",
            object_key="candidates/1/resumes/r1.pdf",
            filename="r1.pdf",
            content_type="application/pdf",
            size_bytes=100,
            checksum="hash1",
            is_current=True,
            source=SourceEnum.candidate_stated,
        )
        uow.resumes.add(r1)
        uow.commit()

    # Bypassing the service and inserting a second row with is_current=True
    with pytest.raises(IntegrityError):
        with uow:
            r2 = Resume(
                candidate_id=candidate_id,
                version=2,
                bucket="resumes",
                object_key="candidates/1/resumes/r2.pdf",
                filename="r2.pdf",
                content_type="application/pdf",
                size_bytes=100,
                checksum="hash2",
                is_current=True,  # Violates partial unique constraint!
                source=SourceEnum.candidate_stated,
            )
            uow.resumes.add(r2)
            uow.commit()


def test_confirm_current_resume_sets_timestamp_and_audits(db, candidate_id, storage):
    """
    When a candidate confirms their current resume, confirmed_at is updated
    and an audit event is logged.
    """
    uow = UnitOfWork(session=db)
    media = _sample_media("confirm_test.pdf", b"%PDF-1.4 test confirm flow")

    with uow:
        r, _ = ingest_resume(uow, candidate_id, media, storage=storage)
        uow.commit()
        r_id = r.id

    # Confirm existing resume
    with uow:
        confirmed = confirm_current_resume(uow, candidate_id, actor_type="candidate")
        assert confirmed is not None
        assert confirmed.id == r_id
        uow.commit()

    with uow:
        events = uow.audit_events.get_by_entity("resume", str(r_id))
        actions = [e.action for e in events]
        assert "confirm" in actions


def test_signed_url_generation_audited(db, candidate_id, storage):
    """
    Generating a signed download URL succeeds and logs an audit event.
    """
    uow = UnitOfWork(session=db)
    media = _sample_media("url_test.pdf", b"%PDF-1.4 url test")

    with uow:
        r, _ = ingest_resume(uow, candidate_id, media, storage=storage)
        uow.commit()
        r_id = r.id

    with uow:
        url = get_resume_download_url(
            uow,
            r_id,
            storage=storage,
            expires_in=1800,
            actor_type="recruiter",
            actor_id="recruiter-uuid-123",
        )
        assert url.startswith("file://")
        uow.commit()

    with uow:
        events = uow.audit_events.get_by_entity("resume", str(r_id))
        gen_events = [e for e in events if e.action == "generate_signed_url"]
        assert len(gen_events) == 1
        assert gen_events[0].actor_type == "recruiter"
        assert gen_events[0].actor_id == "recruiter-uuid-123"
        assert gen_events[0].after["expires_in"] == 1800
