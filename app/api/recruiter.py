"""
app/api/recruiter.py — recruiter-facing read API (FLOW-037).

Search and filter candidates over the operational projection; view a single
candidate's detail (operational + personal, never protected); view
conversation history and message transcripts; obtain a signed resume link.
Every access writes exactly one audit_event.

Auth today is a placeholder shared-secret header (X-Recruiter-Key),
mirroring the webhook's X-Webhook-Secret — there are no recruiter accounts
or roles yet. FLOW-038 replaces this with real recruiter identity and
authorisation; every dependency and audit call here is written so that
swap only touches get_recruiter_actor(), not the route bodies.

Candidate isolation: every nested resource (a conversation, a message page,
a resume) is looked up scoped to the candidate_id in the URL, and a mismatch
(e.g. a real conversation_id belonging to a DIFFERENT candidate) returns 404,
never someone else's data.
"""

import hmac
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.db.uow import UnitOfWork
from app.logging import get_logger
from app.models import AuditEvent
from app.models.enums import LifecycleStatusEnum
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


# ---------------------------------------------------------------------------
# Auth (placeholder — see module docstring)
# ---------------------------------------------------------------------------

def verify_recruiter_key(
    x_recruiter_key: str | None = Header(None, alias="X-Recruiter-Key"),
    authorization: str | None = Header(None),
) -> None:
    """Validate shared-secret recruiter API credentials against configuration.

    Mirrors app.api.webhook.verify_webhook_secret exactly. In local/test
    environments where no key is configured, requests are allowed — this
    must never be true in production (see docs/REVIEW_AND_PLAN.md §16).
    """
    settings = get_settings()
    expected = settings.recruiter_api_key.strip()
    if not expected:
        return

    provided = x_recruiter_key
    if not provided and authorization:
        provided = authorization[7:].strip() if authorization.lower().startswith("bearer ") else authorization.strip()

    if not provided or not hmac.compare_digest(provided, expected):
        logger.warning("Rejected recruiter API request: invalid or missing credentials")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing recruiter authentication credentials",
        )


def get_recruiter_actor(
    x_recruiter_id: str | None = Header(None, alias="X-Recruiter-Id"),
) -> str:
    """Free-text actor id for the audit trail — NOT verified identity.

    Until FLOW-038 adds real recruiter accounts, this is whatever the caller
    claims to be. It is good enough to make audit rows readable during
    development; it must never be treated as an authorization decision.
    """
    return x_recruiter_id or "unknown"


def get_uow(db: Session = Depends(get_db)) -> UnitOfWork:
    return UnitOfWork(session=db)


def _audit(uow: UnitOfWork, actor_id: str, entity_type: str, entity_id: str, action: str, after: dict) -> None:
    uow.audit_events.add(
        AuditEvent(
            actor_type="recruiter",
            actor_id=actor_id,
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

@router.get(
    "/candidates",
    response_model=SearchResponse,
    dependencies=[Depends(verify_recruiter_key)],
)
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
    actor_id: str = Depends(get_recruiter_actor),
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
            uow,
            actor_id,
            entity_type="candidate_search",
            entity_id="search",
            action="search",
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

@router.get(
    "/candidates/{candidate_id}",
    dependencies=[Depends(verify_recruiter_key)],
)
def get_candidate_detail(
    candidate_id: UUID,
    actor_id: str = Depends(get_recruiter_actor),
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

        resume = uow.resumes.get_current(candidate_id)
        result["current_resume"] = serialize_resume_metadata(resume) if resume else None

        _audit(
            uow, actor_id,
            entity_type="candidate", entity_id=str(candidate_id),
            action="view_detail", after={"fields_returned": list(result.keys())},
        )

    return result


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------

@router.get(
    "/candidates/{candidate_id}/conversations",
    dependencies=[Depends(verify_recruiter_key)],
)
def list_candidate_conversations(
    candidate_id: UUID,
    limit: int = Query(20, ge=1, le=100),
    actor_id: str = Depends(get_recruiter_actor),
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
            uow, actor_id,
            entity_type="candidate", entity_id=str(candidate_id),
            action="view_conversations", after={"count": len(conversations)},
        )

    return result


@router.get(
    "/candidates/{candidate_id}/conversations/{conversation_id}/messages",
    dependencies=[Depends(verify_recruiter_key)],
)
def get_conversation_messages(
    candidate_id: UUID,
    conversation_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    actor_id: str = Depends(get_recruiter_actor),
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
            uow, actor_id,
            entity_type="conversation", entity_id=str(conversation_id),
            action="view_messages", after={"returned": len(messages), "total": total},
        )

    return result


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

@router.get(
    "/candidates/{candidate_id}/resume",
    dependencies=[Depends(verify_recruiter_key)],
)
def get_candidate_resume(
    candidate_id: UUID,
    actor_id: str = Depends(get_recruiter_actor),
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
            actor_id=actor_id,
        )

        result = serialize_resume_metadata(resume)
        result["download_url"] = signed_url
        result["expires_in_seconds"] = settings.resume_signed_url_ttl_seconds

    return result
