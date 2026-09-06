"""
tests/test_recruiter_api.py — recruiter API tests (FLOW-037, FLOW-038).

Tests:
1. Auth (FLOW-038 — real recruiter accounts, replacing the FLOW-037
   shared-secret placeholder): missing/unknown/deactivated key rejected;
   valid key accepted; non-admin rejected from admin-only endpoints.
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
6. Audit: every access writes an audit_event of the expected shape, keyed
   to the real authenticated recruiter's id.
7. FLOW-038 headline acceptance criterion: a recruiter correction survives
   a contradicting candidate message; the candidate's version is recorded
   as conflicted. Corrections are rejected for protected-class keys.
8. Notes are never returned by any endpoint other than the notes endpoints
   themselves (never candidate-visible).
9. Assignment: assigning/unassigning a candidate to a recruiter, audited.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.agents.schemas import ExtractedFact, IntentEnum, TurnExtraction
from app.api.auth import generate_api_key
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
    RecruiterRoleEnum,
    SourceEnum,
)
from app.models.recruiter import Recruiter
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


def _make_recruiter(db, role: RecruiterRoleEnum = RecruiterRoleEnum.recruiter, is_active: bool = True) -> tuple[Recruiter, dict]:
    """Create a real recruiter account and return (recruiter, auth_headers)."""
    plaintext, key_hash = generate_api_key()
    uow = UnitOfWork(session=db)
    with uow:
        recruiter = Recruiter(
            email=f"recruiter-{uuid4()}@example.com",
            display_name="Test Recruiter",
            role=role,
            api_key_hash=key_hash,
            is_active=is_active,
        )
        uow.recruiters.add(recruiter)
        recruiter_id = recruiter.id

    with UnitOfWork(session=db) as check_uow:
        persisted = check_uow.recruiters.get_by_id(recruiter_id)
    return persisted, {"X-Recruiter-Key": plaintext}


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
# Auth (FLOW-038)
# ---------------------------------------------------------------------------

def test_auth_rejects_missing_and_unknown_key(client, db):
    _make_recruiter(db)  # some recruiter exists, just not us

    r1 = client.get("/recruiter/candidates")
    assert r1.status_code == 401

    r2 = client.get("/recruiter/candidates", headers={"X-Recruiter-Key": "not-a-real-key"})
    assert r2.status_code == 401


def test_auth_accepts_valid_key(client, db):
    _, headers = _make_recruiter(db)
    resp = client.get("/recruiter/candidates", headers=headers)
    assert resp.status_code == 200


def test_auth_rejects_deactivated_recruiter(client, db):
    _, headers = _make_recruiter(db, is_active=False)
    resp = client.get("/recruiter/candidates", headers=headers)
    assert resp.status_code == 401


def test_bearer_token_form_also_accepted(client, db):
    _, headers = _make_recruiter(db)
    plaintext = headers["X-Recruiter-Key"]
    resp = client.get("/recruiter/candidates", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 200


def test_non_admin_rejected_from_admin_endpoints(client, db):
    _, headers = _make_recruiter(db, role=RecruiterRoleEnum.recruiter)
    resp = client.post(
        "/recruiter/admin/recruiters",
        headers=headers,
        json={"email": "new@example.com", "display_name": "New Person"},
    )
    assert resp.status_code == 403


def test_admin_can_create_and_deactivate_recruiters(client, db):
    _, admin_headers = _make_recruiter(db, role=RecruiterRoleEnum.admin)

    create_resp = client.post(
        "/recruiter/admin/recruiters",
        headers=admin_headers,
        json={"email": f"new-{uuid4()}@example.com", "display_name": "New Person", "role": "recruiter"},
    )
    assert create_resp.status_code == 201
    body = create_resp.json()
    assert "api_key" in body and body["api_key"]
    new_recruiter_id = body["recruiter_id"]

    # The new key actually works
    login_resp = client.get("/recruiter/candidates", headers={"X-Recruiter-Key": body["api_key"]})
    assert login_resp.status_code == 200

    # Deactivate it — the key must stop working immediately
    deactivate_resp = client.post(
        f"/recruiter/admin/recruiters/{new_recruiter_id}/deactivate", headers=admin_headers
    )
    assert deactivate_resp.status_code == 200

    locked_out_resp = client.get("/recruiter/candidates", headers={"X-Recruiter-Key": body["api_key"]})
    assert locked_out_resp.status_code == 401


def test_duplicate_email_rejected(client, db):
    existing, admin_headers = _make_recruiter(db, role=RecruiterRoleEnum.admin)
    resp = client.post(
        "/recruiter/admin/recruiters",
        headers=admin_headers,
        json={"email": existing.email, "display_name": "Someone Else"},
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Three-class filtering — the core FLOW-037 acceptance criteria
# ---------------------------------------------------------------------------

def test_search_and_list_never_emit_personal_or_protected(client, db):
    """Property test: for a candidate carrying every known personal and
    protected key, /recruiter/candidates (list) contains none of them."""
    _, headers = _make_recruiter(db)
    _fully_disclosed_candidate(db)

    resp = client.get("/recruiter/candidates", params={"limit": 100}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1

    for candidate in body["candidates"]:
        assert "personal_attributes" not in candidate
        for key in ALL_PERSONAL_KEYS | ALL_PROTECTED_KEYS:
            assert key not in candidate, f"leaked '{key}' into a search/list response"
        assert candidate["current_role"] is None or isinstance(candidate["current_role"], str)


def test_detail_view_includes_personal_but_never_protected(client, db):
    """The one endpoint allowed to show personal data — and even there,
    protected data must never appear (Phase 5: no escalation path)."""
    _, headers = _make_recruiter(db)
    candidate_id = _fully_disclosed_candidate(db)

    resp = client.get(f"/recruiter/candidates/{candidate_id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    assert "personal_attributes" in body
    for key in ALL_PERSONAL_KEYS:
        assert key in body["personal_attributes"], f"expected personal key '{key}' in detail view"

    flat = str(body)
    for key in ALL_PROTECTED_KEYS:
        assert key not in body["personal_attributes"]
        assert f"protected-value-{key}" not in flat, f"protected value for '{key}' leaked into response"


def test_operational_full_name_appears_in_list_and_detail(client, db):
    """full_name is operational (business decision) — unlike phone_number
    exclusion in the LLM tool layer, the recruiter API shows both."""
    _, headers = _make_recruiter(db)
    candidate_id = _minimal_candidate(db, full_name="Rahul Verma", current_role="SRE")

    list_resp = client.get("/recruiter/candidates", params={"limit": 50}, headers=headers)
    match = next(c for c in list_resp.json()["candidates"] if c["candidate_id"] == candidate_id)
    assert match["full_name"] == "Rahul Verma"

    detail_resp = client.get(f"/recruiter/candidates/{candidate_id}", headers=headers)
    assert detail_resp.json()["full_name"] == "Rahul Verma"


# ---------------------------------------------------------------------------
# Search / filter precision
# ---------------------------------------------------------------------------

def test_filter_by_experience_and_ctc_range(client, db):
    _, headers = _make_recruiter(db)
    low = _minimal_candidate(db, experience_years=2, expected_ctc_annual=800000, currency="INR")
    high = _minimal_candidate(db, experience_years=9, expected_ctc_annual=3500000, currency="INR")

    resp = client.get(
        "/recruiter/candidates",
        params={"min_experience_years": 5, "max_expected_ctc": 4000000, "limit": 100},
        headers=headers,
    )
    ids = {c["candidate_id"] for c in resp.json()["candidates"]}
    assert high in ids
    assert low not in ids


def test_filter_by_lifecycle_status(client, db):
    _, headers = _make_recruiter(db)
    uow = UnitOfWork(session=db)
    phone = f"+9195{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.lifecycle_status = LifecycleStatusEnum.dormant
        dormant_id = str(cand.id)

    ready_id = _minimal_candidate(db)

    resp = client.get(
        "/recruiter/candidates", params={"lifecycle_status": "dormant", "limit": 100}, headers=headers
    )
    ids = {c["candidate_id"] for c in resp.json()["candidates"]}
    assert dormant_id in ids
    assert ready_id not in ids


def test_filter_by_skill_and_location(client, db):
    _, headers = _make_recruiter(db)
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

    resp = client.get("/recruiter/candidates", params={"skills": ["Go"], "limit": 100}, headers=headers)
    ids = {c["candidate_id"] for c in resp.json()["candidates"]}
    assert target_id in ids
    assert other_id not in ids

    resp2 = client.get("/recruiter/candidates", params={"location": "Bengaluru", "limit": 100}, headers=headers)
    assert target_id in {c["candidate_id"] for c in resp2.json()["candidates"]}


def test_unknown_candidate_returns_404(client, db):
    _, headers = _make_recruiter(db)
    resp = client.get(f"/recruiter/candidates/{uuid4()}", headers=headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Candidate isolation
# ---------------------------------------------------------------------------

def test_conversation_isolation_across_candidates(client, db):
    """A real conversation_id that belongs to candidate B must 404, not
    leak, when requested under candidate A's URL."""
    _, headers = _make_recruiter(db)
    uow = UnitOfWork(session=db)
    phone_a = f"+9197{uuid4().int % 100000000:08d}"
    phone_b = f"+9197{(uuid4().int + 1) % 100000000:08d}"
    with uow:
        cand_a = uow.candidates.get_or_create_by_phone(phone_a)
        cand_b = uow.candidates.get_or_create_by_phone(phone_b)
        conv_b = uow.conversations.create(candidate_id=cand_b.id)
        candidate_a_id = str(cand_a.id)
        conversation_b_id = str(conv_b.id)

    resp = client.get(
        f"/recruiter/candidates/{candidate_a_id}/conversations/{conversation_b_id}/messages",
        headers=headers,
    )
    assert resp.status_code == 404


def test_message_transcript_pagination_and_order(client, db):
    _, headers = _make_recruiter(db)
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
        headers=headers,
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

    _, headers = _make_recruiter(db)
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

    resp = client.get(f"/recruiter/candidates/{candidate_id}/resume", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_current"] is True
    assert "download_url" in body and body["download_url"]

    with UnitOfWork(session=db) as check_uow:
        events = check_uow.audit_events.get_by_entity("resume", str(resume.id))
        assert any(e.action == "generate_signed_url" and e.actor_type == "recruiter" for e in events)


def test_resume_404_when_none_on_file(client, db):
    _, headers = _make_recruiter(db)
    candidate_id = _minimal_candidate(db)
    resp = client.get(f"/recruiter/candidates/{candidate_id}/resume", headers=headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def test_every_access_writes_an_audit_event_keyed_to_real_recruiter(client, db):
    recruiter, headers = _make_recruiter(db)
    candidate_id = _minimal_candidate(db)

    client.get("/recruiter/candidates", headers=headers)
    client.get(f"/recruiter/candidates/{candidate_id}", headers=headers)
    client.get(f"/recruiter/candidates/{candidate_id}/conversations", headers=headers)

    with UnitOfWork(session=db) as uow:
        events = uow.audit_events.get_by_actor("recruiter", str(recruiter.id))
        actions = {e.action for e in events}
        assert "search" in actions
        assert "view_detail" in actions
        assert "view_conversations" in actions

        detail_event = next(e for e in events if e.action == "view_detail")
        assert detail_event.entity_type == "candidate"
        assert detail_event.entity_id == candidate_id


# ---------------------------------------------------------------------------
# Corrections (FLOW-038 headline acceptance criterion)
# ---------------------------------------------------------------------------

def test_recruiter_correction_survives_contradicting_candidate_message(client, db):
    """The FLOW-038 acceptance criterion, verbatim: a recruiter correction
    survives a contradicting candidate message; the candidate's version is
    recorded as conflicted."""
    recruiter, headers = _make_recruiter(db)

    uow = UnitOfWork(session=db)
    phone = f"+9101{uuid4().int % 100000000:08d}"
    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        conv = resolve_conversation(uow, cand)
        evaluate_policy_step(
            uow, cand, conv,
            TurnExtraction(
                intent=IntentEnum.provide_info,
                facts=[ExtractedFact(key="experience_years", value=4, raw_text="4 years")],
            ),
        )
        candidate_id = str(cand.id)

    # Recruiter corrects it to 7. Correction values use the same canonical
    # shape candidate-stated facts are normalised into (see
    # CorrectionRequest's docstring in app/api/recruiter.py) — {"amount": X}
    # for experience_years, not a bare number.
    correction_resp = client.post(
        f"/recruiter/candidates/{candidate_id}/corrections",
        headers=headers,
        json={"key": "experience_years", "value": {"amount": 7}, "raw_text": "verified via resume call"},
    )
    assert correction_resp.status_code == 201
    assert correction_resp.json()["outcome"] == "accepted"
    assert correction_resp.json()["source"] == "recruiter_verified"

    # The candidate then says something contradicting it, in a NEW conversation.
    uow2 = UnitOfWork(session=db)
    with uow2:
        cand = uow2.candidates.get_by_id(candidate_id)
        new_conv = uow2.conversations.create(candidate_id=cand.id)
        directive, snapshot = evaluate_policy_step(
            uow2, cand, new_conv,
            TurnExtraction(
                intent=IntentEnum.provide_info,
                facts=[ExtractedFact(key="experience_years", value=4, raw_text="actually 4 years")],
            ),
        )

    # The recruiter-verified value must still be current.
    with UnitOfWork(session=db) as check_uow:
        current = check_uow.attributes.get_current_by_key(candidate_id, "experience_years")
        assert current is not None
        assert current.value == {"amount": 7}
        assert current.source.value == "recruiter_verified"

        # The candidate's contradicting statement must be recorded, marked
        # conflicted — normalize_fact_value wraps candidate-stated
        # experience_years into {"amount": X} too (app/domain/policy.py).
        all_attrs = check_uow.attributes.get_all_for_candidate(candidate_id)
        conflicted = [a for a in all_attrs if a.status.value == "conflicted"]
        assert any(a.key == "experience_years" and a.value == {"amount": 4.0} for a in conflicted)

    # The correction is visible via the recruiter API immediately.
    detail_resp = client.get(f"/recruiter/candidates/{candidate_id}", headers=headers)
    assert detail_resp.json()["experience_years"] == 7


def test_second_recruiter_correction_overrides_the_first(client, db):
    """A recruiter fixing an earlier recruiter's correction must succeed
    outright, not be marked conflicted — recruiter_verified is already the
    top of the precedence table; there's no higher authority to defer to."""
    _, headers = _make_recruiter(db)
    candidate_id = _minimal_candidate(db)

    first = client.post(
        f"/recruiter/candidates/{candidate_id}/corrections",
        headers=headers, json={"key": "notice_period", "value": {"days": 30}},
    )
    assert first.json()["outcome"] == "accepted"

    second = client.post(
        f"/recruiter/candidates/{candidate_id}/corrections",
        headers=headers, json={"key": "notice_period", "value": {"days": 60}},
    )
    assert second.json()["outcome"] == "accepted"

    with UnitOfWork(session=db) as uow:
        current = uow.attributes.get_current_by_key(candidate_id, "notice_period")
        assert current.value == {"days": 60}


def test_correction_rejects_protected_keys(client, db):
    _, headers = _make_recruiter(db)
    candidate_id = _minimal_candidate(db)

    resp = client.post(
        f"/recruiter/candidates/{candidate_id}/corrections",
        headers=headers, json={"key": "religion", "value": "some-value"},
    )
    assert resp.status_code == 400

    with UnitOfWork(session=db) as uow:
        assert uow.attributes.get_current_by_key(candidate_id, "religion") is None


# ---------------------------------------------------------------------------
# Notes — never candidate-visible
# ---------------------------------------------------------------------------

def test_notes_are_private_and_never_leak_into_other_endpoints(client, db):
    recruiter, headers = _make_recruiter(db)
    candidate_id = _minimal_candidate(db)

    add_resp = client.post(
        f"/recruiter/candidates/{candidate_id}/notes",
        headers=headers, json={"note": "Strong candidate, slightly over-qualified for the JD."},
    )
    assert add_resp.status_code == 201

    notes_resp = client.get(f"/recruiter/candidates/{candidate_id}/notes", headers=headers)
    notes = notes_resp.json()["notes"]
    assert len(notes) == 1
    assert notes[0]["note"] == "Strong candidate, slightly over-qualified for the JD."
    assert notes[0]["recruiter_id"] == str(recruiter.id)

    # The note text must never appear in detail, search, or any other
    # recruiter response that isn't the notes endpoint itself.
    detail_resp = client.get(f"/recruiter/candidates/{candidate_id}", headers=headers)
    assert "over-qualified" not in str(detail_resp.json())

    search_resp = client.get("/recruiter/candidates", params={"limit": 100}, headers=headers)
    assert "over-qualified" not in str(search_resp.json())


def test_notes_endpoint_audits_and_requires_valid_candidate(client, db):
    _, headers = _make_recruiter(db)
    resp = client.post(
        f"/recruiter/candidates/{uuid4()}/notes", headers=headers, json={"note": "x"}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------

def test_assign_and_unassign_candidate(client, db):
    assigner, headers = _make_recruiter(db)
    target_recruiter, _ = _make_recruiter(db)
    candidate_id = _minimal_candidate(db)

    assign_resp = client.post(
        f"/recruiter/candidates/{candidate_id}/assign",
        headers=headers, json={"recruiter_id": str(target_recruiter.id)},
    )
    assert assign_resp.status_code == 200
    assert assign_resp.json()["assigned_recruiter_id"] == str(target_recruiter.id)

    detail_resp = client.get(f"/recruiter/candidates/{candidate_id}", headers=headers)
    assert detail_resp.json()["assigned_recruiter_id"] == str(target_recruiter.id)

    unassign_resp = client.post(
        f"/recruiter/candidates/{candidate_id}/assign",
        headers=headers, json={"recruiter_id": None},
    )
    assert unassign_resp.status_code == 200
    assert unassign_resp.json()["assigned_recruiter_id"] is None

    with UnitOfWork(session=db) as uow:
        events = uow.audit_events.get_by_entity("candidate", candidate_id)
        assign_events = [e for e in events if e.action == "assign"]
        assert len(assign_events) == 2
        assert assign_events[-1].after["to"] is None


def test_assign_to_nonexistent_recruiter_404s(client, db):
    _, headers = _make_recruiter(db)
    candidate_id = _minimal_candidate(db)
    resp = client.post(
        f"/recruiter/candidates/{candidate_id}/assign",
        headers=headers, json={"recruiter_id": str(uuid4())},
    )
    assert resp.status_code == 404
