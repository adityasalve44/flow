"""
tests/test_privacy.py — Tests for Candidate erasure, consent withdrawal, and retention sweeps (FLOW-043).

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

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api.schemas import InboundEvent
from app.db.uow import UnitOfWork
from app.models import (
    Candidate,
    CandidateAttribute,
    CandidateLocationPref,
    CandidateProfile,
    CandidateRolePref,
    CandidateSkill,
    Conversation,
    Message,
    Resume,
)
from app.models.enums import (
    ChannelEnum,
    ConfidenceEnum,
    ConsentStatusEnum,
    ConversationStatusEnum,
    DataClassEnum,
    DirectionEnum,
    LifecycleStatusEnum,
    SourceEnum,
)
from app.services.privacy import (
    RetentionConfig,
    erase_candidate_data,
    run_retention_sweep,
)
from app.services.turn import TurnService
from app.storage.local import LocalStorageAdapter


@pytest.fixture
def temp_storage(tmp_path):
    return LocalStorageAdapter(base_dir=tmp_path / "storage", bucket="resumes")


def test_erase_candidate_data_full(db, temp_storage):
    """Test that erase_candidate_data comprehensively purges PII, files, and tags."""
    uow = UnitOfWork(session=db)
    phone = f"+9198{uuid4().int % 100000000:08d}"
    now = datetime.now(UTC)

    with uow:
        # 1. Setup candidate & profile
        cand = uow.candidates.get_or_create_by_phone(phone, display_name="Test Candidate")
        cand.consent_status = ConsentStatusEnum.granted
        cand.lifecycle_status = LifecycleStatusEnum.intake
        uow.session.flush()

        profile = CandidateProfile(
            candidate_id=cand.id,
            full_name="Alice Smith",
            current_role="Software Engineer",
            current_company="Acme Corp",
        )
        uow.session.add(profile)

        # 2. Setup attributes (personal, protected, operational)
        attr_personal = CandidateAttribute(
            candidate_id=cand.id,
            key="email",
            value={"email": "alice@example.com"},
            source=SourceEnum.candidate_stated,
            confidence=ConfidenceEnum.confirmed,
            data_class=DataClassEnum.personal,
        )
        attr_protected = CandidateAttribute(
            candidate_id=cand.id,
            key="current_salary",
            value={"salary": 120000},
            source=SourceEnum.candidate_stated,
            confidence=ConfidenceEnum.confirmed,
            data_class=DataClassEnum.protected,
        )
        attr_operational = CandidateAttribute(
            candidate_id=cand.id,
            key="years_experience",
            value={"years": 5},
            source=SourceEnum.candidate_stated,
            confidence=ConfidenceEnum.confirmed,
            data_class=DataClassEnum.operational,
        )
        uow.session.add_all([attr_personal, attr_protected, attr_operational])

        # 3. Setup preference tags
        skill = CandidateSkill(candidate_id=cand.id, skill_raw="Python", skill_norm="python")
        role = CandidateRolePref(
            candidate_id=cand.id,
            role_raw="Backend Dev",
            role_norm="backend developer",
            kind="desired",
        )
        loc = CandidateLocationPref(
            candidate_id=cand.id, location_raw="Bangalore", location_norm="bangalore"
        )
        uow.session.add_all([skill, role, loc])

        # 4. Setup resume in object storage & DB
        resume_key = f"{cand.id}/v1/resume.pdf"
        temp_storage.put(resume_key, b"dummy resume content", content_type="application/pdf")
        assert temp_storage.exists(resume_key)

        resume = Resume(
            candidate_id=cand.id,
            version=1,
            bucket=temp_storage.bucket,
            object_key=resume_key,
            filename="resume.pdf",
            content_type="application/pdf",
            size_bytes=20,
            checksum="dummychecksum",
            is_current=True,
        )
        uow.session.add(resume)

        # 5. Setup conversation & messages
        conv = uow.conversations.create(cand.id)
        msg1 = Message(
            conversation_id=conv.id,
            candidate_id=cand.id,
            direction=DirectionEnum.inbound,
            body="Hello, I want to apply",
            created_at=now - timedelta(minutes=5),
        )
        msg2 = Message(
            conversation_id=conv.id,
            candidate_id=cand.id,
            direction=DirectionEnum.outbound,
            body="Welcome! Please share details",
            created_at=now - timedelta(minutes=4),
        )
        uow.session.add_all([msg1, msg2])
        uow.commit()

        # Execute candidate erasure
        cand_id = cand.id
        summary = erase_candidate_data(
            uow=uow,
            candidate_id=cand_id,
            storage=temp_storage,
            actor_type="recruiter",
            actor_id="recruiter_123",
        )
        assert summary["status"] == "erased"
        assert summary["resumes_deleted"] == 1
        assert summary["messages_redacted"] == 2
        uow.commit()

    # Verify results in fresh transaction
    with uow:
        updated_cand = uow.session.get(Candidate, cand_id)
        assert updated_cand is not None
        assert updated_cand.phone_number.startswith("+deleted_")
        assert updated_cand.display_name is None
        assert updated_cand.consent_status == ConsentStatusEnum.withdrawn
        assert updated_cand.lifecycle_status == LifecycleStatusEnum.dormant
        assert updated_cand.blocked_at is not None

        # Verify attributes: personal and protected wiped
        remaining_attrs = uow.session.scalars(
            select(CandidateAttribute).where(CandidateAttribute.candidate_id == cand_id)
        ).all()
        for a in remaining_attrs:
            assert a.data_class not in (DataClassEnum.personal, DataClassEnum.protected)

        # Verify preference tags deleted
        assert (
            len(
                uow.session.scalars(
                    select(CandidateSkill).where(CandidateSkill.candidate_id == cand_id)
                ).all()
            )
            == 0
        )
        assert (
            len(
                uow.session.scalars(
                    select(CandidateRolePref).where(CandidateRolePref.candidate_id == cand_id)
                ).all()
            )
            == 0
        )
        assert (
            len(
                uow.session.scalars(
                    select(CandidateLocationPref).where(
                        CandidateLocationPref.candidate_id == cand_id
                    )
                ).all()
            )
            == 0
        )

        # Verify profile projection deleted
        assert uow.session.get(CandidateProfile, cand_id) is None

        # Verify resumes deleted from DB and storage
        assert (
            len(uow.session.scalars(select(Resume).where(Resume.candidate_id == cand_id)).all())
            == 0
        )
        assert not temp_storage.exists(resume_key)

        # Verify messages redacted
        messages = uow.session.scalars(select(Message).where(Message.candidate_id == cand_id)).all()
        for m in messages:
            assert m.body == "[deleted]"
            assert m.media_ref is None

        # Verify conversation closed
        convs = uow.session.scalars(
            select(Conversation).where(Conversation.candidate_id == cand_id)
        ).all()
        for c in convs:
            assert c.status == ConversationStatusEnum.closed

        # Verify AuditEvent
        events = uow.audit_events.get_by_entity("candidate", cand_id)
        assert len(events) >= 1
        assert events[0].action == "candidate_erasure"
        assert events[0].actor_type == "recruiter"
        assert events[0].actor_id == "recruiter_123"


@pytest.mark.asyncio
async def test_consent_withdrawal_turn_integration(db):
    """Test that stating 'stop contacting me and delete everything' triggers candidate erasure."""
    uow = UnitOfWork(session=db)
    phone = f"+9199{uuid4().int % 100000000:08d}"
    now = datetime.now(UTC)

    # 1. Create candidate with granted consent and existing attributes
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone, display_name="John Doe")
        cand.consent_status = ConsentStatusEnum.granted
        cand.lifecycle_status = LifecycleStatusEnum.intake
        uow.session.flush()

        attr = CandidateAttribute(
            candidate_id=cand.id,
            key="location",
            value={"city": "Mumbai"},
            source=SourceEnum.candidate_stated,
            confidence=ConfidenceEnum.confirmed,
            data_class=DataClassEnum.personal,
        )
        uow.session.add(attr)
        uow.commit()
        cand_id = cand.id

    # 2. Candidate sends withdrawal statement
    service = TurnService(uow=uow)
    event = InboundEvent(
        channel=ChannelEnum.simulator,
        phone_number=phone,
        channel_message_id=f"withdraw_{uuid4().hex}",
        message="stop contacting me and delete everything",
        received_at=now,
    )
    result = await service.run(event, now=now)

    # 3. Verify turn result
    assert result.directive == "consent_withdrawn"
    assert "withdrawn" in result.reply_text.lower()
    assert result.is_closed is True

    # 4. Verify candidate in DB is erased
    with uow:
        cand = uow.session.get(Candidate, cand_id)
        assert cand.phone_number.startswith("+deleted_")
        assert cand.consent_status == ConsentStatusEnum.withdrawn
        assert cand.lifecycle_status == LifecycleStatusEnum.dormant
        assert cand.blocked_at is not None

        # Personal attribute wiped
        attrs = uow.session.scalars(
            select(CandidateAttribute).where(CandidateAttribute.candidate_id == cand_id)
        ).all()
        assert len(attrs) == 0

        # Inbound message redacted to [deleted]
        inbound_msg = uow.session.get(Message, result.inbound_message_id)
        assert inbound_msg.body == "[deleted]"

        # AuditEvent logged
        audit_events = uow.audit_events.get_by_entity("candidate", cand_id)
        assert any(e.action == "candidate_erasure" for e in audit_events)


def test_retention_sweep_dry_run_vs_execution(db):
    """Test that retention sweep obeys dry_run flag and correctly enforces data class windows."""
    uow = UnitOfWork(session=db)
    phone = f"+9196{uuid4().int % 100000000:08d}"
    now = datetime.now(UTC)

    config = RetentionConfig(
        protected_days=30,
        personal_days=90,
        operational_days=365,
        closed_conversation_days=180,
    )

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        uow.session.flush()

        # 1. Protected attributes: 1 expired (40 days), 1 active (10 days)
        attr_prot_expired = CandidateAttribute(
            candidate_id=cand.id,
            key="prot_exp",
            value={"v": 1},
            source=SourceEnum.candidate_stated,
            confidence=ConfidenceEnum.confirmed,
            data_class=DataClassEnum.protected,
            created_at=now - timedelta(days=40),
        )
        attr_prot_active = CandidateAttribute(
            candidate_id=cand.id,
            key="prot_act",
            value={"v": 2},
            source=SourceEnum.candidate_stated,
            confidence=ConfidenceEnum.confirmed,
            data_class=DataClassEnum.protected,
            created_at=now - timedelta(days=10),
        )

        # 2. Personal attributes: 1 expired (100 days), 1 active (20 days)
        attr_pers_expired = CandidateAttribute(
            candidate_id=cand.id,
            key="pers_exp",
            value={"v": 3},
            source=SourceEnum.candidate_stated,
            confidence=ConfidenceEnum.confirmed,
            data_class=DataClassEnum.personal,
            created_at=now - timedelta(days=100),
        )
        attr_pers_active = CandidateAttribute(
            candidate_id=cand.id,
            key="pers_act",
            value={"v": 4},
            source=SourceEnum.candidate_stated,
            confidence=ConfidenceEnum.confirmed,
            data_class=DataClassEnum.personal,
            created_at=now - timedelta(days=20),
        )

        # 3. Operational attributes: 1 expired (400 days), 1 active (50 days)
        attr_oper_expired = CandidateAttribute(
            candidate_id=cand.id,
            key="oper_exp",
            value={"v": 5},
            source=SourceEnum.candidate_stated,
            confidence=ConfidenceEnum.confirmed,
            data_class=DataClassEnum.operational,
            created_at=now - timedelta(days=400),
        )
        attr_oper_active = CandidateAttribute(
            candidate_id=cand.id,
            key="oper_act",
            value={"v": 6},
            source=SourceEnum.candidate_stated,
            confidence=ConfidenceEnum.confirmed,
            data_class=DataClassEnum.operational,
            created_at=now - timedelta(days=50),
        )

        # 4. Closed conversations: 1 expired (200 days), 1 active (50 days)
        conv_expired = Conversation(
            candidate_id=cand.id,
            status=ConversationStatusEnum.closed,
            started_at=now - timedelta(days=201),
            closed_at=now - timedelta(days=200),
        )
        conv_active = Conversation(
            candidate_id=cand.id,
            status=ConversationStatusEnum.closed,
            started_at=now - timedelta(days=51),
            closed_at=now - timedelta(days=50),
        )

        uow.session.add_all(
            [
                attr_prot_expired,
                attr_prot_active,
                attr_pers_expired,
                attr_pers_active,
                attr_oper_expired,
                attr_oper_active,
                conv_expired,
                conv_active,
            ]
        )
        uow.commit()

        cand_id = cand.id
        conv_exp_id = conv_expired.id
        conv_act_id = conv_active.id

    # 1. Run dry-run
    with uow:
        dry_res = run_retention_sweep(uow, retention_config=config, dry_run=True, now=now)
        assert dry_res.dry_run is True
        assert dry_res.protected_attributes_pruned >= 1
        assert dry_res.personal_attributes_pruned >= 1
        assert dry_res.operational_attributes_pruned >= 1
        assert dry_res.conversations_pruned >= 1

        # Check that dry_run DID NOT delete anything
        all_attrs = uow.session.scalars(
            select(CandidateAttribute).where(CandidateAttribute.candidate_id == cand_id)
        ).all()
        assert len(all_attrs) == 6
        assert uow.session.get(Conversation, conv_exp_id) is not None
        assert uow.session.get(Conversation, conv_act_id) is not None

    # 2. Run actual sweep
    with uow:
        exec_res = run_retention_sweep(uow, retention_config=config, dry_run=False, now=now)
        assert exec_res.dry_run is False
        assert exec_res.protected_attributes_pruned >= 1
        assert exec_res.personal_attributes_pruned >= 1
        assert exec_res.operational_attributes_pruned >= 1
        assert exec_res.conversations_pruned >= 1
        uow.commit()

    # 3. Verify expired items deleted, active items remain
    with uow:
        remaining_attrs = uow.session.scalars(
            select(CandidateAttribute).where(CandidateAttribute.candidate_id == cand_id)
        ).all()
        keys = {a.key for a in remaining_attrs}
        assert "prot_act" in keys
        assert "pers_act" in keys
        assert "oper_act" in keys
        assert "prot_exp" not in keys
        assert "pers_exp" not in keys
        assert "oper_exp" not in keys

        assert uow.session.get(Conversation, conv_exp_id) is None
        assert uow.session.get(Conversation, conv_act_id) is not None

        # Verify audit event logged
        events = uow.audit_events.get_by_entity("system", "retention_sweep")
        assert len(events) >= 1
        assert events[0].action == "retention_sweep"
