"""
tests/test_backlog.py — Incomplete-candidate backlog view tests (FLOW-039).

Acceptance criteria:
- Surface pending work without building a queue (Q2 reactive architecture).
- Inclusion rules: completeness < threshold, inactive beyond window, granted consent.
- Exclusion rules: profile_ready, active (< 3 days), blocked, disengaged (3+ deflections).
- Sensibly ordered by value score.
- Runs on indexed columns.
- Three-class data protection: zero personal or protected data in backlog payloads.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.auth import generate_api_key
from app.api.recruiter import get_uow
from app.database import get_db
from app.db.uow import UnitOfWork
from app.main import app
from app.models.candidate import Candidate, CandidateProfile, Conversation, Message
from app.models.enums import (
    ChannelEnum,
    ConsentStatusEnum,
    ConversationModeEnum,
    ConversationStatusEnum,
    DirectionEnum,
    LifecycleStatusEnum,
    RecruiterRoleEnum,
)
from app.models.recruiter import Recruiter


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_uow] = lambda: UnitOfWork(session=db)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _make_recruiter(
    db, role: RecruiterRoleEnum = RecruiterRoleEnum.recruiter
) -> tuple[Recruiter, dict]:
    plaintext, key_hash = generate_api_key()
    uow = UnitOfWork(session=db)
    recruiter = Recruiter(
        email=f"recruiter_{uuid4().hex[:8]}@example.com",
        display_name="Test Recruiter",
        role=role,
        api_key_hash=key_hash,
        is_active=True,
    )
    with uow:
        uow.recruiters.add(recruiter)
        uow.commit()
    headers = {"Authorization": f"Bearer {plaintext}"}
    return recruiter, headers


def _create_candidate_with_activity(
    db,
    phone: str,
    completeness: float | None = 0.5,
    lifecycle_status: LifecycleStatusEnum = LifecycleStatusEnum.intake,
    consent_status: ConsentStatusEnum = ConsentStatusEnum.granted,
    days_ago: float = 4.0,
    conv_status: ConversationStatusEnum = ConversationStatusEnum.active,
    deflection_count: int = 0,
    blocked_at: datetime | None = None,
    assigned_recruiter_id=None,
) -> Candidate:
    """Helper to populate test candidates with specific profile and conversation history."""
    now = datetime.now(UTC)
    activity_time = now - timedelta(days=days_ago)

    uow = UnitOfWork(session=db)
    with uow:
        cand = Candidate(
            phone_number=phone,
            display_name=f"Candidate {phone[-4:]}",
            lifecycle_status=lifecycle_status,
            consent_status=consent_status,
            blocked_at=blocked_at,
            assigned_recruiter_id=assigned_recruiter_id,
            created_at=activity_time - timedelta(hours=2),
        )
        uow.candidates.add(cand)
        uow.session.flush()

        if completeness is not None:
            profile = CandidateProfile(
                candidate_id=cand.id,
                full_name=f"User {phone[-4:]}",
                current_role="Software Engineer",
                completeness=completeness,
            )
            uow.profiles.add(profile)

        conv = Conversation(
            candidate_id=cand.id,
            channel=ChannelEnum.simulator,
            status=conv_status,
            mode=ConversationModeEnum.intake,
            started_at=activity_time - timedelta(hours=1),
            last_inbound_at=activity_time,
            deflection_count=deflection_count,
        )
        uow.conversations.add(conv)
        uow.session.flush()

        msg = Message(
            conversation_id=conv.id,
            candidate_id=cand.id,
            direction=DirectionEnum.inbound,
            body="Hello, I am looking for a job",
            created_at=activity_time,
        )
        uow.messages.add(msg)
        uow.commit()

    return cand


def test_backlog_inclusion_rules(client, db):
    """Incomplete candidates silent > 3 days are included."""
    _, headers = _make_recruiter(db)
    phone1 = f"+9198{uuid4().int % 100000000:08d}"
    phone2 = f"+9198{uuid4().int % 100000000:08d}"

    # Candidate 1: Incomplete (0.6), inactive 4 days ago -> INCLUDED
    c1 = _create_candidate_with_activity(db, phone1, completeness=0.6, days_ago=4.0)

    # Candidate 2: Incomplete with no profile (None completeness), inactive 5 days -> INCLUDED
    c2 = _create_candidate_with_activity(db, phone2, completeness=None, days_ago=5.0)

    res = client.get("/recruiter/backlog", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["threshold"] == 1.0
    assert data["inactivity_days"] == 3

    item_ids = {item["candidate_id"] for item in data["items"]}
    assert str(c1.id) in item_ids
    assert str(c2.id) in item_ids


def test_backlog_exclusion_profile_ready(client, db):
    """Profile-ready candidates (completeness 1.0 or status profile_ready) are excluded."""
    _, headers = _make_recruiter(db)
    phone_ready = f"+9198{uuid4().int % 100000000:08d}"
    phone_full = f"+9198{uuid4().int % 100000000:08d}"

    # Candidate with lifecycle status profile_ready
    c_ready = _create_candidate_with_activity(
        db,
        phone_ready,
        completeness=0.8,
        lifecycle_status=LifecycleStatusEnum.profile_ready,
        days_ago=4.0,
    )
    # Candidate with completeness 1.0
    c_full = _create_candidate_with_activity(
        db, phone_full, completeness=1.0, lifecycle_status=LifecycleStatusEnum.intake, days_ago=4.0
    )

    res = client.get("/recruiter/backlog", headers=headers)
    assert res.status_code == 200
    data = res.json()
    item_ids = {item["candidate_id"] for item in data["items"]}
    assert str(c_ready.id) not in item_ids
    assert str(c_full.id) not in item_ids


def test_backlog_exclusion_active_candidates(client, db):
    """Candidates with recent inbound activity (< 3 days ago) are excluded."""
    _, headers = _make_recruiter(db)
    phone_active = f"+9198{uuid4().int % 100000000:08d}"

    # Incomplete (0.4), but last active 1 day ago -> ACTIVE, NOT BACKLOG
    c_active = _create_candidate_with_activity(db, phone_active, completeness=0.4, days_ago=1.0)

    res = client.get("/recruiter/backlog", headers=headers)
    assert res.status_code == 200
    data = res.json()
    item_ids = {item["candidate_id"] for item in data["items"]}
    assert str(c_active.id) not in item_ids


def test_backlog_exclusion_blocked_candidates(client, db):
    """Blocked candidates (lifecycle_status=blocked or blocked_at set) are excluded."""
    _, headers = _make_recruiter(db)
    phone_blocked1 = f"+9198{uuid4().int % 100000000:08d}"
    phone_blocked2 = f"+9198{uuid4().int % 100000000:08d}"

    c_b1 = _create_candidate_with_activity(
        db,
        phone_blocked1,
        completeness=0.3,
        lifecycle_status=LifecycleStatusEnum.blocked,
        days_ago=4.0,
    )
    c_b2 = _create_candidate_with_activity(
        db, phone_blocked2, completeness=0.3, blocked_at=datetime.now(UTC), days_ago=4.0
    )

    res = client.get("/recruiter/backlog", headers=headers)
    assert res.status_code == 200
    data = res.json()
    item_ids = {item["candidate_id"] for item in data["items"]}
    assert str(c_b1.id) not in item_ids
    assert str(c_b2.id) not in item_ids


def test_backlog_exclusion_disengaged_candidates(client, db):
    """Disengaged candidates (status=disengaged or 3+ deflections) are excluded."""
    _, headers = _make_recruiter(db)
    phone_dis = f"+9198{uuid4().int % 100000000:08d}"
    phone_defl = f"+9198{uuid4().int % 100000000:08d}"

    c_dis = _create_candidate_with_activity(
        db, phone_dis, completeness=0.5, conv_status=ConversationStatusEnum.disengaged, days_ago=4.0
    )
    c_defl = _create_candidate_with_activity(
        db, phone_defl, completeness=0.5, deflection_count=3, days_ago=4.0
    )

    res = client.get("/recruiter/backlog", headers=headers)
    assert res.status_code == 200
    data = res.json()
    item_ids = {item["candidate_id"] for item in data["items"]}
    assert str(c_dis.id) not in item_ids
    assert str(c_defl.id) not in item_ids


def test_backlog_exclusion_consent_not_granted(client, db):
    """Candidates without granted consent (declined, withdrawn, pending) are excluded."""
    _, headers = _make_recruiter(db)
    phone_dec = f"+9198{uuid4().int % 100000000:08d}"
    phone_with = f"+9198{uuid4().int % 100000000:08d}"
    phone_pen = f"+9198{uuid4().int % 100000000:08d}"

    c_dec = _create_candidate_with_activity(
        db, phone_dec, consent_status=ConsentStatusEnum.declined, days_ago=4.0
    )
    c_with = _create_candidate_with_activity(
        db, phone_with, consent_status=ConsentStatusEnum.withdrawn, days_ago=4.0
    )
    c_pen = _create_candidate_with_activity(
        db, phone_pen, consent_status=ConsentStatusEnum.pending, days_ago=4.0
    )

    res = client.get("/recruiter/backlog", headers=headers)
    assert res.status_code == 200
    data = res.json()
    item_ids = {item["candidate_id"] for item in data["items"]}
    assert str(c_dec.id) not in item_ids
    assert str(c_with.id) not in item_ids
    assert str(c_pen.id) not in item_ids


def test_backlog_ordering_by_value_score(client, db):
    """Backlog is sensibly ordered by value score (high completeness + recent drop-off first)."""
    _, headers = _make_recruiter(db)
    p_high = f"+9198{uuid4().int % 100000000:08d}"
    p_low = f"+9198{uuid4().int % 100000000:08d}"

    # High completeness (0.83), inactive 3.5 days -> High value score
    c_high = _create_candidate_with_activity(db, p_high, completeness=0.83, days_ago=3.5)
    # Low completeness (0.17), inactive 20 days -> Low value score
    c_low = _create_candidate_with_activity(db, p_low, completeness=0.17, days_ago=20.0)

    res = client.get("/recruiter/backlog?order_by=value_score", headers=headers)
    assert res.status_code == 200
    data = res.json()
    items = data["items"]

    idx_high = next(i for i, item in enumerate(items) if item["candidate_id"] == str(c_high.id))
    idx_low = next(i for i, item in enumerate(items) if item["candidate_id"] == str(c_low.id))
    assert idx_high < idx_low
    assert items[idx_high]["value_score"] > items[idx_low]["value_score"]


def test_backlog_filtering_and_assignment(client, db):
    """Backlog respects threshold, min_completeness, inactivity_days, and assignment filters."""
    recruiter, headers = _make_recruiter(db)
    p1 = f"+9198{uuid4().int % 100000000:08d}"
    p2 = f"+9198{uuid4().int % 100000000:08d}"

    # c1: completeness 0.4, assigned to recruiter, inactive 4 days
    c1 = _create_candidate_with_activity(
        db, p1, completeness=0.4, days_ago=4.0, assigned_recruiter_id=recruiter.id
    )
    # c2: completeness 0.8, unassigned, inactive 10 days
    c2 = _create_candidate_with_activity(db, p2, completeness=0.8, days_ago=10.0)

    # 1. Test threshold filter (completeness < 0.5)
    res_thresh = client.get("/recruiter/backlog?threshold=0.5", headers=headers)
    assert res_thresh.status_code == 200
    thresh_ids = {i["candidate_id"] for i in res_thresh.json()["items"]}
    assert str(c1.id) in thresh_ids
    assert str(c2.id) not in thresh_ids

    # 2. Test min_completeness floor (completeness >= 0.5)
    res_floor = client.get("/recruiter/backlog?min_completeness=0.5", headers=headers)
    assert res_floor.status_code == 200
    floor_ids = {i["candidate_id"] for i in res_floor.json()["items"]}
    assert str(c1.id) not in floor_ids
    assert str(c2.id) in floor_ids

    # 3. Test assigned_to_me
    res_assigned = client.get("/recruiter/backlog?assigned_to_me=true", headers=headers)
    assert res_assigned.status_code == 200
    assigned_ids = {i["candidate_id"] for i in res_assigned.json()["items"]}
    assert str(c1.id) in assigned_ids
    assert str(c2.id) not in assigned_ids


def test_backlog_auth_and_audit(client, db):
    """Backlog requires authentication and logs an audit event."""
    # 1. Unauthenticated request rejected
    res_unauth = client.get("/recruiter/backlog")
    assert res_unauth.status_code == 401

    # 2. Authenticated request logs audit event
    recruiter, headers = _make_recruiter(db)
    res_auth = client.get("/recruiter/backlog", headers=headers)
    assert res_auth.status_code == 200

    uow = UnitOfWork(session=db)
    with uow:
        events = uow.audit_events.get_by_actor(actor_type="recruiter", actor_id=str(recruiter.id))
        audit = next((e for e in events if e.action == "list_backlog"), None)
        assert audit is not None
        assert audit.entity_type == "candidate_backlog"


def test_backlog_three_class_data_protection(client, db):
    """Backlog items must never contain personal or protected attributes."""
    _, headers = _make_recruiter(db)
    phone = f"+9198{uuid4().int % 100000000:08d}"
    c = _create_candidate_with_activity(db, phone, completeness=0.5, days_ago=4.0)

    res = client.get("/recruiter/backlog", headers=headers)
    assert res.status_code == 200
    item = next(i for i in res.json()["items"] if i["candidate_id"] == str(c.id))

    # Forbidden personal / protected keys
    forbidden_keys = {
        "date_of_birth",
        "age",
        "marital_status",
        "family_circumstances",
        "nationality",
        "religion",
        "caste",
        "health_disability",
        "sex_gender",
    }
    for k in forbidden_keys:
        assert k not in item
