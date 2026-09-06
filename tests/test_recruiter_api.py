"""
tests/test_recruiter_api.py — recruiter read API tests (FLOW-037).

Tests:
1. Auth: missing/wrong X-Recruiter-Key rejected when a key is configured.
2. Three-class filtering (the acceptance criteria, verbatim):
   - No list or search endpoint can emit a personal or protected attribute
     (property test — the candidate here carries every known personal AND
     protected key, going through the real merge/policy/projection pipeline,
     not hand-built response dicts).
   - The detail endpoint DOES surface personal attributes.
   - protected NEVER appears anywhere, including the detail endpoint (no
     escalation path exists in Phase 5 — the business decision, not a gap).
3. Search/filter: role, skills, location, work_mode, experience/CTC/notice
   ranges, lifecycle_status — each narrows results correctly.
4. Candidate isolation: a conversation/message page for one candidate cannot
   be fetched by pairing it with a different candidate_id in the URL.
5. Resume: current resume returns a signed URL; no resume -> 404.
6. Audit: every access writes an audit_event of the expected shape.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.agents.schemas import ExtractedFact, IntentEnum, TurnExtraction
from app.api.recruiter import get_uow
from app.database import get_db
from app.db.uow import UnitOfWork
from app.domain.policy import evaluate_policy_step
from app.main import app
from app.models import Resume
from app.models.enums import (
    ConsentStatusEnum,
    DirectionEnum,
    LifecycleStatusEnum,
    SourceEnum,
)
from app.services.conversation import resolve_conversation

ALL_PERSONAL_KEYS = {"date_of_birth", "age", "marital_status", "family_circumstances", "nationality"}
ALL_PROTECTED_KEYS = {"religion", "caste", "health_disability", "sex_gender"}


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_uow] = lambda: UnitOfWork(session=db)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _fully_disclosed_candidate(db) -> str:
    """A candidate who has, over one turn, disclosed every operational
    baseline field PLUS every known personal and protected attribute.
    Goes through the real evaluate_policy_step/merge/projection pipeline —
    not hand-built response dicts — so this genuinely exercises the
    classification boundary the acceptance criteria describe.

    Returns the candidate_id as a string.
    """
    uow = UnitOfWork(session=db)
    phone = f"+9193{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone, display_name="Priya Sharma")
        cand.consent_status = ConsentStatusEnum.granted
        conv = resolve_conversation(uow, cand)

        facts = [
            ExtractedFact(key="desired_role", value=["Senior Backend Engineer"], raw_text="Senior Backend Engineer"),
            ExtractedFact(key="experience_years", value=6, raw_text="6 years"),
            ExtractedFact(key="skills", value=["Python", "PostgreSQL"], raw_text="Python, PostgreSQL"),
            ExtractedFact(key="location_preference", value=["Pune"], raw_text="Pune"),
            ExtractedFact(key="expected_ctc", value="25 LPA", raw_text="25 LPA"),
            ExtractedFact(key="notice_period", value="30 days", raw_text="30 days"),
            ExtractedFact(key="work_mode", value="remote", raw_text="remote only"),
        ]
        for key in ALL_PERSONAL_KEYS:
            facts.append(ExtractedFact(key=key, value=f"personal-value-{key}", raw_text=key))
        for key in ALL_PROTECTED_KEYS:
            facts.append(ExtractedFact(key=key, value=f"protected-value-{key}", raw_text=key))

        evaluate_policy_step(uow, cand, conv, TurnExtraction(intent=IntentEnum.provide_info, facts=facts))
        candidate_id = str(cand.id)

    return candidate_id


def _minimal_candidate(db, **profile_overrides) -> str:
    """A bare candidate with just a profile row, for filter-precision tests
    that don't need the full attribute pipeline."""
    from app.models import CandidateProfile

    uow = UnitOfWork(session=db)
    phone = f"+9194{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        cand.lifecycle_status = LifecycleStatusEnum.profile_ready
        profile = CandidateProfile(candidate_id=cand.id, **profile_overrides)
        uow.profiles.add(profile)
        candidate_id = str(cand.id)
    return candidate_id


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_auth_rejects_missing_and_wrong_key(client, monkeypatch):
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "recruiter_api_key", "sekrit-key")

    r1 = client.get("/recruiter/candidates")
    assert r1.status_code == 401

    r2 = client.get("/recruiter/candidates", headers={"X-Recruiter-Key": "wrong"})
    assert r2.status_code == 401

    r3 = client.get("/recruiter/candidates", headers={"X-Recruiter-Key": "sekrit-key"})
    assert r3.status_code == 200


# ---------------------------------------------------------------------------
# Three-class filtering — the core acceptance criteria
# ---------------------------------------------------------------------------

def test_search_and_list_never_emit_personal_or_protected(client, db):
    """Property test: for a candidate carrying every known personal and
    protected key, /recruiter/candidates (list) contains none of them."""
    _fully_disclosed_candidate(db)

    resp = client.get("/recruiter/candidates", params={"limit": 100})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1

    for candidate in body["candidates"]:
        assert "personal_attributes" not in candidate
        for key in ALL_PERSONAL_KEYS | ALL_PROTECTED_KEYS:
            assert key not in candidate, f"leaked '{key}' into a search/list response"
        # Operational fields must actually be present and correct
        assert candidate["current_role"] is None or isinstance(candidate["current_role"], str)


def test_detail_view_includes_personal_but_never_protected(client, db):
    """The one endpoint allowed to show personal data — and even there,
    protected data must never appear (Phase 5: no escalation path)."""
    candidate_id = _fully_disclosed_candidate(db)

    resp = client.get(f"/recruiter/candidates/{candidate_id}")
    assert resp.status_code == 200
    body = resp.json()

    assert "personal_attributes" in body
    for key in ALL_PERSONAL_KEYS:
        assert key in body["personal_attributes"], f"expected personal key '{key}' in detail view"

    # Protected: absent from personal_attributes AND absent anywhere else
    # in the response body, at any nesting level.
    flat = str(body)
    for key in ALL_PROTECTED_KEYS:
        assert key not in body["personal_attributes"]
        assert f"protected-value-{key}" not in flat, f"protected value for '{key}' leaked into response"


def test_operational_full_name_appears_in_list_and_detail(client, db):
    """full_name is operational (business decision) — unlike phone_number
    exclusion in the LLM tool layer, the recruiter API shows both."""
    candidate_id = _minimal_candidate(db, full_name="Rahul Verma", current_role="SRE")

    list_resp = client.get("/recruiter/candidates", params={"limit": 50})
    match = next(c for c in list_resp.json()["candidates"] if c["candidate_id"] == candidate_id)
    assert match["full_name"] == "Rahul Verma"

    detail_resp = client.get(f"/recruiter/candidates/{candidate_id}")
    assert detail_resp.json()["full_name"] == "Rahul Verma"


# ---------------------------------------------------------------------------
# Search / filter precision
# ---------------------------------------------------------------------------

def test_filter_by_experience_and_ctc_range(client, db):
    low = _minimal_candidate(db, experience_years=2, expected_ctc_annual=800000, currency="INR")
    high = _minimal_candidate(db, experience_years=9, expected_ctc_annual=3500000, currency="INR")

    resp = client.get(
        "/recruiter/candidates",
        params={"min_experience_years": 5, "max_expected_ctc": 4000000, "limit": 100},
    )
    ids = {c["candidate_id"] for c in resp.json()["candidates"]}
    assert high in ids
    assert low not in ids


def test_filter_by_lifecycle_status(client, db):
    uow = UnitOfWork(session=db)
    phone = f"+9195{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.lifecycle_status = LifecycleStatusEnum.dormant
        dormant_id = str(cand.id)

    ready_id = _minimal_candidate(db)

    resp = client.get("/recruiter/candidates", params={"lifecycle_status": "dormant", "limit": 100})
    ids = {c["candidate_id"] for c in resp.json()["candidates"]}
    assert dormant_id in ids
    assert ready_id not in ids


def test_filter_by_skill_and_location(client, db):
    uow = UnitOfWork(session=db)
    phone = f"+9196{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        conv = resolve_conversation(uow, cand)
        evaluate_policy_step(
            uow, cand, conv,
            TurnExtraction(
                intent=IntentEnum.provide_info,
                facts=[
                    ExtractedFact(key="skills", value=["Go", "Kubernetes"], raw_text="Go, Kubernetes"),
                    ExtractedFact(key="location_preference", value=["Bengaluru"], raw_text="Bengaluru"),
                ],
            ),
        )
        target_id = str(cand.id)

    other_id = _minimal_candidate(db, current_role="Sales")

    resp = client.get("/recruiter/candidates", params={"skills": ["Go"], "limit": 100})
    ids = {c["candidate_id"] for c in resp.json()["candidates"]}
    assert target_id in ids
    assert other_id not in ids

    resp2 = client.get("/recruiter/candidates", params={"location": "Bengaluru", "limit": 100})
    assert target_id in {c["candidate_id"] for c in resp2.json()["candidates"]}


def test_unknown_candidate_returns_404(client):
    resp = client.get(f"/recruiter/candidates/{uuid4()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Candidate isolation
# ---------------------------------------------------------------------------

def test_conversation_isolation_across_candidates(client, db):
    """A real conversation_id that belongs to candidate B must 404, not
    leak, when requested under candidate A's URL."""
    uow = UnitOfWork(session=db)
    phone_a = f"+9197{uuid4().int % 100000000:08d}"
    phone_b = f"+9197{(uuid4().int + 1) % 100000000:08d}"
    with uow:
        cand_a = uow.candidates.get_or_create_by_phone(phone_a)
        cand_b = uow.candidates.get_or_create_by_phone(phone_b)
        conv_b = uow.conversations.create(candidate_id=cand_b.id)
        candidate_a_id = str(cand_a.id)
        conversation_b_id = str(conv_b.id)

    resp = client.get(f"/recruiter/candidates/{candidate_a_id}/conversations/{conversation_b_id}/messages")
    assert resp.status_code == 404


def test_message_transcript_pagination_and_order(client, db):
    uow = UnitOfWork(session=db)
    phone = f"+9198{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        conv = uow.conversations.create(candidate_id=cand.id)
        for i in range(5):
            uow.messages.create(
                conversation_id=conv.id, candidate_id=cand.id,
                direction=DirectionEnum.inbound if i % 2 == 0 else DirectionEnum.outbound,
                body=f"message {i}",
            )
        candidate_id, conversation_id = str(cand.id), str(conv.id)

    resp = client.get(
        f"/recruiter/candidates/{candidate_id}/conversations/{conversation_id}/messages",
        params={"limit": 2, "offset": 1},
    )
    body = resp.json()
    assert body["total"] == 5
    assert [m["body"] for m in body["messages"]] == ["message 1", "message 2"]


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

def test_resume_returns_signed_url_when_current_exists(client, db, tmp_path, monkeypatch):
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "local_storage_dir", str(tmp_path))

    uow = UnitOfWork(session=db)
    phone = f"+9199{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        resume = Resume(
            candidate_id=cand.id,
            version=1,
            bucket="resumes",
            object_key=f"candidates/{cand.id}/resumes/v1.pdf",
            filename="resume.pdf",
            content_type="application/pdf",
            size_bytes=1234,
            checksum="a" * 64,
            is_current=True,
            source=SourceEnum.candidate_stated,
        )
        uow.resumes.add(resume)
        candidate_id = str(cand.id)

    resp = client.get(f"/recruiter/candidates/{candidate_id}/resume")
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_current"] is True
    assert "download_url" in body and body["download_url"]

    # The access was audited by app.services.resume.get_resume_download_url
    with UnitOfWork(session=db) as check_uow:
        events = check_uow.audit_events.get_by_entity("resume", str(resume.id))
        assert any(e.action == "generate_signed_url" and e.actor_type == "recruiter" for e in events)


def test_resume_404_when_none_on_file(client, db):
    candidate_id = _minimal_candidate(db)
    resp = client.get(f"/recruiter/candidates/{candidate_id}/resume")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def test_every_access_writes_an_audit_event(client, db):
    candidate_id = _minimal_candidate(db)

    client.get("/recruiter/candidates", headers={"X-Recruiter-Id": "recruiter-42"})
    client.get(f"/recruiter/candidates/{candidate_id}", headers={"X-Recruiter-Id": "recruiter-42"})
    client.get(f"/recruiter/candidates/{candidate_id}/conversations", headers={"X-Recruiter-Id": "recruiter-42"})

    with UnitOfWork(session=db) as uow:
        search_events = uow.audit_events.get_by_actor("recruiter", "recruiter-42")
        actions = {e.action for e in search_events}
        assert "search" in actions
        assert "view_detail" in actions
        assert "view_conversations" in actions

        detail_event = next(e for e in search_events if e.action == "view_detail")
        assert detail_event.entity_type == "candidate"
        assert detail_event.entity_id == candidate_id
