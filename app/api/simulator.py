"""
app/api/simulator.py — Simulator inspection and test-assist endpoints.

Provides full transparency into Flow's agent decisions and storage layers
for the interactive WhatsApp simulator & live data inspector cockpit.
"""

from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.db.uow import UnitOfWork
from app.models.attribute import CandidateAttribute
from app.models.candidate import Candidate, CandidateProfile, Conversation, Message
from app.models.enums import (
    ConsentStatusEnum,
    LifecycleStatusEnum,
)
from app.repositories.backlog import BacklogParams
from app.serializers.recruiter import serialize_backlog_item

router = APIRouter(prefix="/api/simulator", tags=["simulator"])


def get_uow(db: Session = Depends(get_db)) -> UnitOfWork:
    return UnitOfWork(session=db)


class CandidateListItem(BaseModel):
    id: str
    phone_number: str
    display_name: str | None
    lifecycle_status: str
    consent_status: str
    completeness: float
    created_at: str | None


class ResetRequest(BaseModel):
    phone_number: str | None = None
    display_name: str | None = None


@router.get("/candidates", response_model=list[CandidateListItem])
def list_simulator_candidates(
    limit: int = Query(20, ge=1, le=50),
    uow: UnitOfWork = Depends(get_uow),
) -> list[CandidateListItem]:
    """List recent candidates for quick selection in the simulator."""
    with uow:
        stmt = (
            select(Candidate, CandidateProfile)
            .outerjoin(CandidateProfile, CandidateProfile.candidate_id == Candidate.id)
            .order_by(Candidate.created_at.desc())
            .limit(limit)
        )
        rows = uow.session.execute(stmt).all()
        return [
            CandidateListItem(
                id=str(c.id),
                phone_number=c.phone_number,
                display_name=c.display_name,
                lifecycle_status=c.lifecycle_status.value,
                consent_status=c.consent_status.value,
                completeness=p.completeness if (p and p.completeness is not None) else 0.0,
                created_at=c.created_at.isoformat() if c.created_at else None,
            )
            for c, p in rows
        ]


@router.get("/inspect/{candidate_id}")
def inspect_candidate(
    candidate_id: UUID,
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    """Retrieve full real-time state across all layers for the data inspector cockpit."""
    with uow:
        cand = uow.candidates.get_by_id(candidate_id)
        if cand is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")

        prof = uow.profiles.get_by_candidate_id(candidate_id)

        # Candidate attributes with provenance
        attr_rows = (
            uow.session.execute(
                select(CandidateAttribute)
                .where(CandidateAttribute.candidate_id == candidate_id)
                .order_by(CandidateAttribute.key.asc(), CandidateAttribute.created_at.desc())
            )
            .scalars()
            .all()
        )

        # Multi-value preferences
        skill_rows = uow.skills.get_by_candidate(candidate_id)
        role_rows = uow.role_prefs.get_by_candidate(candidate_id)
        loc_rows = uow.location_prefs.get_by_candidate(candidate_id)

        # Conversations
        conv_rows = (
            uow.session.execute(
                select(Conversation)
                .where(Conversation.candidate_id == candidate_id)
                .order_by(Conversation.started_at.desc())
            )
            .scalars()
            .all()
        )

        # Recent messages
        msg_rows = (
            uow.session.execute(
                select(Message)
                .where(Message.candidate_id == candidate_id)
                .order_by(Message.created_at.asc())
            )
            .scalars()
            .all()
        )

        return {
            "candidate": {
                "id": str(cand.id),
                "phone_number": cand.phone_number,
                "display_name": cand.display_name,
                "lifecycle_status": cand.lifecycle_status.value,
                "consent_status": cand.consent_status.value,
                "consent_at": cand.consent_at.isoformat() if cand.consent_at else None,
                "blocked_at": cand.blocked_at.isoformat() if cand.blocked_at else None,
                "created_at": cand.created_at.isoformat() if cand.created_at else None,
            },
            "profile": {
                "full_name": prof.full_name if prof else None,
                "current_role": prof.current_role if prof else None,
                "current_company": prof.current_company if prof else None,
                "experience_years": prof.experience_years if prof else None,
                "current_ctc_annual": float(prof.current_ctc_annual)
                if (prof and prof.current_ctc_annual)
                else None,
                "expected_ctc_annual": float(prof.expected_ctc_annual)
                if (prof and prof.expected_ctc_annual)
                else None,
                "currency": prof.currency if prof else None,
                "notice_period_days": prof.notice_period_days if prof else None,
                "work_mode": prof.work_mode if prof else None,
                "education_level": prof.education_level if prof else None,
                "completeness": prof.completeness
                if (prof and prof.completeness is not None)
                else 0.0,
                "last_refreshed_at": prof.last_refreshed_at.isoformat()
                if (prof and prof.last_refreshed_at)
                else None,
            },
            "attributes": [
                {
                    "id": str(a.id),
                    "key": a.key,
                    "value": a.value,
                    "raw_text": a.raw_text,
                    "source": a.source.value,
                    "confidence": a.confidence.value,
                    "status": a.status.value,
                    "data_class": a.data_class.value,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in attr_rows
            ],
            "skills": [
                {
                    "skill_raw": s.skill_raw,
                    "skill_norm": s.skill_norm,
                    "years": s.years,
                    "confidence": s.confidence,
                    "status": s.status,
                }
                for s in skill_rows
            ],
            "role_prefs": [
                {
                    "role_raw": r.role_raw,
                    "role_norm": r.role_norm,
                    "kind": r.kind,
                    "strength": r.strength,
                }
                for r in role_rows
            ],
            "location_prefs": [
                {
                    "location_raw": loc.location_raw,
                    "location_norm": loc.location_norm,
                    "kind": loc.kind,
                }
                for loc in loc_rows
            ],
            "conversations": [
                {
                    "id": str(c.id),
                    "status": c.status.value,
                    "mode": c.mode.value,
                    "started_at": c.started_at.isoformat() if c.started_at else None,
                    "last_inbound_at": c.last_inbound_at.isoformat() if c.last_inbound_at else None,
                    "deflection_count": c.deflection_count,
                    "abuse_count": c.abuse_count,
                    "summary": c.summary,
                }
                for c in conv_rows
            ],
            "messages": [
                {
                    "id": str(m.id),
                    "conversation_id": str(m.conversation_id),
                    "direction": m.direction.value,
                    "body": m.body,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in msg_rows
            ],
        }


@router.post("/reset")
def reset_or_create_candidate(
    req: ResetRequest,
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    """Generate a new test candidate or clear a candidate for testing."""
    phone = req.phone_number or f"+9198{uuid4().int % 100000000:08d}"
    name = req.display_name or f"Simulated Candidate {phone[-4:]}"

    with uow:
        existing = uow.candidates.get_by_phone(phone)
        if existing:
            # Re-initialize to clean state
            existing.lifecycle_status = LifecycleStatusEnum.new
            existing.consent_status = ConsentStatusEnum.pending
            existing.consent_at = None
            existing.blocked_at = None
            cand = existing
        else:
            cand = uow.candidates.get_or_create_by_phone(phone)
            cand.display_name = name
            uow.candidates.add(cand)

        uow.commit()

        return {
            "candidate_id": str(cand.id),
            "phone_number": cand.phone_number,
            "display_name": cand.display_name,
            "lifecycle_status": cand.lifecycle_status.value,
            "consent_status": cand.consent_status.value,
        }


@router.get("/backlog")
def get_simulator_backlog(
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    """Retrieve backlog view for the simulator inspector cockpit."""
    params = BacklogParams(
        completeness_threshold=1.0,
        inactivity_days=3,
        limit=limit,
        offset=offset,
    )
    with uow:
        rows, total = uow.backlog.get_backlog(params)
        items = []
        for row in rows:
            skill_rows = uow.skills.get_by_candidate(row.candidate.id)
            role_rows = uow.role_prefs.get_by_candidate(row.candidate.id)
            loc_rows = uow.location_prefs.get_by_candidate(row.candidate.id)
            items.append(serialize_backlog_item(row, skill_rows, role_rows, loc_rows))
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": items,
        }
