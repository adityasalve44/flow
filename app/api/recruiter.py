"""
app/api/recruiter.py — recruiter-facing API (FLOW-037, FLOW-038).

Search and filter candidates over the operational projection; view a single
candidate's detail (operational + personal, never protected); view
conversation history and message transcripts; obtain a signed resume link;
correct candidate data with recruiter authority; leave private notes;
assign candidates; manage recruiter accounts (admin only). Every access
writes exactly one audit_event.

Auth (FLOW-038): a real, per-recruiter API key via get_current_recruiter
(app/api/auth.py), replacing FLOW-037's shared-secret placeholder. Every
route depends on it directly rather than a separate dependencies=[...]
entry, since it both authenticates and supplies the actor's identity for
the audit trail — there is no longer a free-text, unverified actor id.

Candidate isolation: every nested resource (a conversation, a message page,
a resume) is looked up scoped to the candidate_id in the URL, and a mismatch
(e.g. a real conversation_id belonging to a DIFFERENT candidate) returns 404,
never someone else's data.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import generate_api_key, get_current_recruiter, require_admin
from app.config import get_settings
from app.database import get_db
from app.db.uow import UnitOfWork
from app.domain.merge import Fact, MergeContext, merge_facts
from app.domain.policy import sync_preference_tables
from app.domain.projection import rebuild_projection
from app.domain.registry import get_data_class
from app.logging import get_logger
from app.models import AuditEvent, CandidateAttribute
from app.models.enums import (
    AttributeStatusEnum,
    ConfidenceEnum,
    DataClassEnum,
    LifecycleStatusEnum,
    RecruiterRoleEnum,
    SourceEnum,
)
from app.models.recruiter import Recruiter, RecruiterNote
from app.repositories.recruiter_search import CandidateSearchFilters
from app.serializers.recruiter import (
    serialize_candidate_detail,
    serialize_candidate_summary,
    serialize_conversation_summary,
    serialize_message,
    serialize_resume_metadata,
)
from app.services.resume import get_resume_download_url

logger = get_logger(__name__)

router = APIRouter(prefix="/recruiter", tags=["recruiter"])


def get_uow(db: Session = Depends(get_db)) -> UnitOfWork:
    return UnitOfWork(session=db)


def _audit(uow: UnitOfWork, recruiter: Recruiter, entity_type: str, entity_id: str, action: str, after: dict) -> None:
    uow.audit_events.add(
        AuditEvent(
            actor_type="recruiter",
            actor_id=str(recruiter.id),
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            before=None,
            after=after,
        )
    )


def _load_candidate_or_404(uow: UnitOfWork, candidate_id: UUID):
    row = uow.candidate_search.get_by_id_with_profile(candidate_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    return row


class SearchResponse(BaseModel):
    total: int
    limit: int
    offset: int
    candidates: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Search / list
# ---------------------------------------------------------------------------

@router.get("/candidates", response_model=SearchResponse)
def search_candidates(
    role: str | None = Query(None, description="Substring match on current or desired role"),
    skills: list[str] | None = Query(None, description="Any-of match on skill (repeat param for multiple)"),
    location: str | None = Query(None, description="Match on a location preference"),
    work_mode: str | None = Query(None),
    lifecycle_status: LifecycleStatusEnum | None = Query(None),
    min_experience_years: float | None = Query(None, ge=0),
    max_experience_years: float | None = Query(None, ge=0),
    min_expected_ctc: float | None = Query(None, ge=0),
    max_expected_ctc: float | None = Query(None, ge=0),
    max_notice_period_days: int | None = Query(None, ge=0),
    min_completeness: float | None = Query(None, ge=0, le=1),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    recruiter: Recruiter = Depends(get_current_recruiter),
    uow: UnitOfWork = Depends(get_uow),
) -> SearchResponse:
    """Search and filter candidates over the operational projection.

    Every filter is AND-combined. skills/location match against the derived
    preference tables (populated per-turn by
    app.domain.policy.sync_preference_tables). Personal and protected
    attributes never appear in this response — only operational columns and
    the operational preference tables are queried at all.
    """
    filters = CandidateSearchFilters(
        role=role,
        skills=skills or [],
        location=location,
        work_mode=work_mode,
        lifecycle_status=lifecycle_status,
        min_experience_years=min_experience_years,
        max_experience_years=max_experience_years,
        min_expected_ctc=min_expected_ctc,
        max_expected_ctc=max_expected_ctc,
        max_notice_period_days=max_notice_period_days,
        min_completeness=min_completeness,
    )

    with uow:
        rows, total = uow.candidate_search.search(filters, limit=limit, offset=offset)

        candidates = []
        for row in rows:
            skill_rows = uow.skills.get_by_candidate(row.candidate.id)
            role_rows = uow.role_prefs.get_by_candidate(row.candidate.id)
            location_rows = uow.location_prefs.get_by_candidate(row.candidate.id)
            candidates.append(serialize_candidate_summary(row, skill_rows, role_rows, location_rows))

        _audit(
            uow, recruiter,
            entity_type="candidate_search", entity_id="search", action="search",
            after={
                "filters": {k: (v.value if hasattr(v, "value") else v) for k, v in filters.__dict__.items() if v},
                "result_count": len(candidates),
                "total": total,
                "candidate_ids": [c["candidate_id"] for c in candidates],
            },
        )

    return SearchResponse(total=total, limit=limit, offset=offset, candidates=candidates)


# ---------------------------------------------------------------------------
# Candidate detail
# ---------------------------------------------------------------------------

@router.get("/candidates/{candidate_id}")
def get_candidate_detail(
    candidate_id: UUID,
    recruiter: Recruiter = Depends(get_current_recruiter),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    """Full single-candidate view: operational fields plus personal
    attributes. Protected attributes are never returned (Phase 5 decision:
    no escalation path exists yet)."""
    with uow:
        row = _load_candidate_or_404(uow, candidate_id)

        skill_rows = uow.skills.get_by_candidate(candidate_id)
        role_rows = uow.role_prefs.get_by_candidate(candidate_id)
        location_rows = uow.location_prefs.get_by_candidate(candidate_id)
        current_attrs = uow.attributes.get_current_for_candidate(candidate_id)

        result = serialize_candidate_detail(row, skill_rows, role_rows, location_rows, current_attrs)
        result["assigned_recruiter_id"] = (
            str(row.candidate.assigned_recruiter_id) if row.candidate.assigned_recruiter_id else None
        )

        resume = uow.resumes.get_current(candidate_id)
        result["current_resume"] = serialize_resume_metadata(resume) if resume else None

        _audit(
            uow, recruiter,
            entity_type="candidate", entity_id=str(candidate_id),
            action="view_detail", after={"fields_returned": list(result.keys())},
        )

    return result


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------

@router.get("/candidates/{candidate_id}/conversations")
def list_candidate_conversations(
    candidate_id: UUID,
    limit: int = Query(20, ge=1, le=100),
    recruiter: Recruiter = Depends(get_current_recruiter),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    with uow:
        _load_candidate_or_404(uow, candidate_id)

        conversations = uow.conversations.get_by_candidate(candidate_id, limit=limit)
        result = {
            "candidate_id": str(candidate_id),
            "conversations": [serialize_conversation_summary(c) for c in conversations],
        }

        _audit(
            uow, recruiter,
            entity_type="candidate", entity_id=str(candidate_id),
            action="view_conversations", after={"count": len(conversations)},
        )

    return result


@router.get("/candidates/{candidate_id}/conversations/{conversation_id}/messages")
def get_conversation_messages(
    candidate_id: UUID,
    conversation_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    recruiter: Recruiter = Depends(get_current_recruiter),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    """Paginated chronological transcript. 404s (never leaks) if the
    conversation exists but belongs to a different candidate — this is the
    candidate-isolation boundary for this endpoint."""
    with uow:
        _load_candidate_or_404(uow, candidate_id)

        conversation = uow.conversations.get_by_id(conversation_id)
        if conversation is None or conversation.candidate_id != candidate_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

        messages, total = uow.messages.get_page_by_conversation(conversation_id, limit=limit, offset=offset)
        result = {
            "candidate_id": str(candidate_id),
            "conversation_id": str(conversation_id),
            "total": total,
            "limit": limit,
            "offset": offset,
            "messages": [serialize_message(m) for m in messages],
        }

        _audit(
            uow, recruiter,
            entity_type="conversation", entity_id=str(conversation_id),
            action="view_messages", after={"returned": len(messages), "total": total},
        )

    return result


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

@router.get("/candidates/{candidate_id}/resume")
def get_candidate_resume(
    candidate_id: UUID,
    recruiter: Recruiter = Depends(get_current_recruiter),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    """Current resume metadata plus a short-TTL signed download URL.

    The signed URL is minted fresh on every call and the access is audited
    by app.services.resume.get_resume_download_url itself — this endpoint
    does not duplicate that audit write.
    """
    with uow:
        _load_candidate_or_404(uow, candidate_id)

        resume = uow.resumes.get_current(candidate_id)
        if resume is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No resume on file")

        settings = get_settings()
        signed_url = get_resume_download_url(
            uow=uow,
            resume_id=resume.id,
            expires_in=settings.resume_signed_url_ttl_seconds,
            actor_type="recruiter",
            actor_id=str(recruiter.id),
        )

        result = serialize_resume_metadata(resume)
        result["download_url"] = signed_url
        result["expires_in_seconds"] = settings.resume_signed_url_ttl_seconds

    return result


# ---------------------------------------------------------------------------
# Corrections — recruiter-verified data (FLOW-038)
# ---------------------------------------------------------------------------

class CorrectionRequest(BaseModel):
    """value must already be in the SAME canonical shape
    app.domain.policy.normalize_fact_value() produces for candidate-stated
    text, since that is the shape app.domain.projection.rebuild_projection()
    and every other reader in the system expects:

        experience_years            {"amount": <float>}
        expected_ctc / current_ctc  {"amount": <float>, "currency": <str>, "period": <str>}
        notice_period               {"days": <int>}
        location_preference         [<str>, ...]
        everything else             the plain value (str, list[str], etc.)

    Deliberately NOT run through normalize_fact_value(): that function
    parses loose candidate phrasing ("about 80k a month") and exists to
    catch hedge words — a recruiter correction is by definition already an
    authoritative, resolved value (confidence=confirmed, source=
    recruiter_verified), not text to be reinterpreted. Passing a value
    through the text parser here would risk silently mangling a
    well-formed structured value; a wrong shape either fails validation
    downstream or is visibly wrong, which is preferable to a silent
    reinterpretation.
    """
    key: str
    value: Any
    raw_text: str | None = None


@router.post("/candidates/{candidate_id}/corrections", status_code=status.HTTP_201_CREATED)
def correct_candidate_attribute(
    candidate_id: UUID,
    body: CorrectionRequest,
    recruiter: Recruiter = Depends(get_current_recruiter),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    """Write a fact at source=recruiter_verified — rank 6, the ceiling of
    the precedence table (app.domain.merge.SOURCE_RANKS). The merge engine
    then protects it from being silently overwritten by any candidate
    message or model inference: a contradicting lower-authority fact is
    recorded as conflicted, never promoted (app.domain.merge.merge_facts).

    Restricted to operational and personal keys. protected-class keys are
    rejected here too, symmetrically with the read side of this API — Phase
    5 has no governance workflow for protected data in either direction.
    """
    data_class = get_data_class(body.key)
    if data_class == DataClassEnum.protected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{body.key}' is a protected-class key; corrections to protected data are not supported in Phase 5",
        )

    with uow:
        candidate = uow.candidates.get_by_id(candidate_id)
        if candidate is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")

        db_attrs = uow.attributes.get_all_for_candidate(candidate_id)
        existing_current = [
            Fact(
                id=a.id, candidate_id=a.candidate_id, key=a.key, value=a.value,
                raw_text=a.raw_text or "", source=a.source.value, confidence=a.confidence.value,
                status=a.status.value, data_class=a.data_class.value,
                conversation_id=a.conversation_id, created_at=a.created_at,
            )
            for a in db_attrs if a.status == AttributeStatusEnum.current
        ]
        existing_for_key = next((f for f in existing_current if f.key == body.key), None)

        incoming = Fact(
            key=body.key,
            value=body.value,
            raw_text=body.raw_text or f"recruiter correction by {recruiter.email}",
            source=SourceEnum.recruiter_verified.value,
            confidence=ConfidenceEnum.confirmed.value,
            data_class=data_class.value,
            candidate_id=str(candidate_id),
            conversation_id=existing_for_key.conversation_id if existing_for_key else None,
        )

        # A second recruiter_verified correction of the same key is a
        # direct, unconditional update — not run through merge_facts.
        # merge_facts' same-rank/different-conversation rule (see
        # tests/test_merge.py::test_7x7_precedence_matrix) deliberately
        # marks two equal-rank facts as conflicted unless the incoming
        # source is candidate_confirmed — correct for candidate-side facts,
        # where "different conversation" is a meaningful signal about
        # whether this is the same correction episode. It is not a
        # meaningful signal here: a correction made through this endpoint
        # has no real conversation at all, and a recruiter calling this
        # endpoint a second time is unambiguously saying "update it again".
        # recruiter_verified is already the top of the precedence table
        # (rank 6) — there is no higher authority whose confirmation this
        # could need. This does not change merge_facts itself, or its
        # tested behaviour for any other caller.
        if existing_for_key is not None and existing_for_key.source == SourceEnum.recruiter_verified.value:
            accepted_directly = True
            outcome = "accepted"
        else:
            result = merge_facts(
                existing=existing_current,
                incoming=[incoming],
                context=MergeContext(conversation_id=incoming.conversation_id),
            )
            accepted_directly = any(f is incoming for f in result.accepted)
            outcome = (
                "accepted" if accepted_directly
                else "conflicted" if any(f is incoming for f in result.conflicted)
                else "ambiguous"
            )

        if accepted_directly:
            # Fact.source/.confidence/.data_class are plain strings (pure
            # dataclass, no ORM in the merge engine's signatures per
            # app/domain/merge.py) — coerce back to real Enum members here,
            # matching the pattern in app/domain/policy.py. Passing the raw
            # strings through would let SQLAlchemy accept them at
            # assignment time but return them uncoerced from the identity
            # map on a later read within the same session.
            db_attr = CandidateAttribute(
                candidate_id=candidate_id, key=incoming.key, value=incoming.value,
                raw_text=incoming.raw_text, source=SourceEnum(incoming.source),
                confidence=ConfidenceEnum(incoming.confidence),
                data_class=DataClassEnum(incoming.data_class),
                conversation_id=incoming.conversation_id,
            )
            if existing_for_key is not None:
                uow.attributes.supersede(old_attribute_id=existing_for_key.id, new_attribute=db_attr)
            else:
                uow.attributes.add(db_attr)

        # Rebuild the projection immediately so the correction is visible
        # in search/detail right away, not only after the candidate's next
        # WhatsApp turn.
        refreshed_attrs = uow.attributes.get_current_for_candidate(candidate_id)
        refreshed_facts = [
            Fact(
                id=a.id, candidate_id=a.candidate_id, key=a.key, value=a.value,
                raw_text=a.raw_text or "", source=a.source.value, confidence=a.confidence.value,
                status=a.status.value, data_class=a.data_class.value,
                conversation_id=a.conversation_id, created_at=a.created_at,
            )
            for a in refreshed_attrs
        ]
        snapshot = rebuild_projection(refreshed_facts)

        profile = uow.profiles.get_or_create(candidate_id)
        profile.current_role = snapshot.current_role
        profile.current_company = snapshot.current_company
        profile.experience_years = snapshot.experience_years
        profile.current_ctc_annual = snapshot.current_ctc_annual
        profile.expected_ctc_annual = snapshot.expected_ctc_annual
        profile.currency = snapshot.currency
        profile.notice_period_days = snapshot.notice_period_days
        profile.work_mode = snapshot.work_mode
        profile.education_level = snapshot.education_level
        if snapshot.full_name is not None:
            profile.full_name = snapshot.full_name
        profile.completeness = snapshot.completeness
        sync_preference_tables(uow, candidate_id, snapshot)

        _audit(
            uow, recruiter,
            entity_type="candidate_attribute", entity_id=f"{candidate_id}:{body.key}",
            action="correct", after={"key": body.key, "outcome": outcome},
        )

    return {"key": body.key, "outcome": outcome, "source": SourceEnum.recruiter_verified.value}


# ---------------------------------------------------------------------------
# Notes — private, never candidate-visible (FLOW-038)
# ---------------------------------------------------------------------------

class NoteRequest(BaseModel):
    note: str


@router.get("/candidates/{candidate_id}/notes")
def list_candidate_notes(
    candidate_id: UUID,
    recruiter: Recruiter = Depends(get_current_recruiter),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    with uow:
        _load_candidate_or_404(uow, candidate_id)
        notes = uow.recruiter_notes.get_by_candidate(candidate_id)
        result = {
            "candidate_id": str(candidate_id),
            "notes": [
                {
                    "note_id": str(n.id),
                    "recruiter_id": str(n.recruiter_id) if n.recruiter_id else None,
                    "note": n.note,
                    "created_at": n.created_at.isoformat(),
                }
                for n in notes
            ],
        }
        _audit(
            uow, recruiter,
            entity_type="candidate", entity_id=str(candidate_id),
            action="view_notes", after={"count": len(notes)},
        )
    return result


@router.post("/candidates/{candidate_id}/notes", status_code=status.HTTP_201_CREATED)
def add_candidate_note(
    candidate_id: UUID,
    body: NoteRequest,
    recruiter: Recruiter = Depends(get_current_recruiter),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    with uow:
        _load_candidate_or_404(uow, candidate_id)
        note = uow.recruiter_notes.add(
            RecruiterNote(candidate_id=candidate_id, recruiter_id=recruiter.id, note=body.note)
        )
        _audit(
            uow, recruiter,
            entity_type="candidate", entity_id=str(candidate_id),
            action="add_note", after={"note_id": str(note.id)},
        )
        note_id = str(note.id)
    return {"note_id": note_id}


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------

class AssignRequest(BaseModel):
    recruiter_id: UUID | None  # null unassigns


@router.post("/candidates/{candidate_id}/assign")
def assign_candidate(
    candidate_id: UUID,
    body: AssignRequest,
    recruiter: Recruiter = Depends(get_current_recruiter),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    with uow:
        candidate = uow.candidates.get_by_id(candidate_id)
        if candidate is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")

        if body.recruiter_id is not None and uow.recruiters.get_by_id(body.recruiter_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target recruiter not found")

        before = str(candidate.assigned_recruiter_id) if candidate.assigned_recruiter_id else None
        candidate.assigned_recruiter_id = body.recruiter_id

        _audit(
            uow, recruiter,
            entity_type="candidate", entity_id=str(candidate_id),
            action="assign", after={"from": before, "to": str(body.recruiter_id) if body.recruiter_id else None},
        )

    return {
        "candidate_id": str(candidate_id),
        "assigned_recruiter_id": str(body.recruiter_id) if body.recruiter_id else None,
    }


# ---------------------------------------------------------------------------
# Recruiter account management (admin only)
# ---------------------------------------------------------------------------

class CreateRecruiterRequest(BaseModel):
    email: str
    display_name: str
    role: str = "recruiter"


@router.post("/admin/recruiters", status_code=status.HTTP_201_CREATED)
def create_recruiter(
    body: CreateRecruiterRequest,
    admin: Recruiter = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    """Admin-only. Returns the plaintext API key exactly once — it is not
    stored anywhere and cannot be recovered afterwards. See
    scripts/create_recruiter.py for how the first admin account is
    bootstrapped, since this endpoint itself requires an existing admin."""
    with uow:
        if uow.recruiters.get_by_email(body.email) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A recruiter with email '{body.email}' already exists",
            )

        try:
            role = RecruiterRoleEnum(body.role)
        except ValueError as err:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid role '{body.role}'",
            ) from err

        plaintext, key_hash = generate_api_key()
        new_recruiter = uow.recruiters.add(
            Recruiter(email=body.email, display_name=body.display_name, role=role, api_key_hash=key_hash)
        )

        _audit(
            uow, admin,
            entity_type="recruiter", entity_id=str(new_recruiter.id),
            action="create", after={"email": body.email, "role": role.value},
        )
        new_id = str(new_recruiter.id)

    return {"recruiter_id": new_id, "email": body.email, "role": role.value, "api_key": plaintext}


@router.post("/admin/recruiters/{recruiter_id}/deactivate")
def deactivate_recruiter(
    recruiter_id: UUID,
    admin: Recruiter = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    with uow:
        target = uow.recruiters.get_by_id(recruiter_id)
        if target is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recruiter not found")

        target.is_active = False

        _audit(
            uow, admin,
            entity_type="recruiter", entity_id=str(recruiter_id),
            action="deactivate", after={"email": target.email},
        )

    return {"recruiter_id": str(recruiter_id), "is_active": False}
