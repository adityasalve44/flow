"""
app/serializers/recruiter.py — recruiter-facing response shaping (FLOW-037).

The single place that decides what a recruiter is allowed to see. Every
recruiter API response is built here, never assembled ad hoc in a route
handler — that is what makes "no endpoint can emit a personal/protected
attribute" a property of one module instead of a rule every handler has to
remember.

Q5 three-class rules, enforced here:
- operational: always included (candidates/candidate_profiles columns, plus
  skills/desired_roles/locations from the derived preference tables).
- personal: included ONLY in serialize_candidate_detail(), read live from
  candidate_attributes so a serializer bug in the projection layer can't
  leak it into search/list — this function re-checks data_class itself
  rather than trusting the caller.
- protected: NEVER included by any function in this module. Per the
  business decision recorded for Phase 5, there is no escalation path yet —
  that governance question (who may see it, under what justification) is
  deliberately deferred to Phase 6 alongside the flow_sensitive schema.

phone_number is not part of the three-class system at all — it is trusted
identity, not a candidate_attribute — and recruiters legitimately need it to
do their job (they have to actually contact the candidate). It is therefore
included everywhere a candidate is serialized, unlike the LLM tool layer
(app/tools/snapshot.py), which excludes it for a different reason: there,
excluding it prevents identity spoofing through the model. That reasoning
does not apply to an authenticated, human-operated recruiter API.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from app.domain.registry import get_data_class
from app.models.candidate import CandidateProfile
from app.models.enums import DataClassEnum
from app.models.profile import CandidateLocationPref, CandidateRolePref, CandidateSkill
from app.models.resume import Resume
from app.repositories.recruiter_search import CandidateSearchRow


def _money(value: Decimal | float | None) -> float | None:
    return float(value) if value is not None else None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _profile_fields(profile: CandidateProfile | None) -> dict[str, Any]:
    """Operational scalar fields from candidate_profiles — every key here is
    a plain column, so there is nothing to classify: candidate_profiles is
    the operational-only projection by construction (§7, FLOW-013)."""
    if profile is None:
        return {
            "full_name": None,
            "current_role": None,
            "current_company": None,
            "experience_years": None,
            "current_ctc_annual": None,
            "expected_ctc_annual": None,
            "currency": None,
            "notice_period_days": None,
            "work_mode": None,
            "education_level": None,
            "completeness": 0.0,
        }
    return {
        "full_name": profile.full_name,
        "current_role": profile.current_role,
        "current_company": profile.current_company,
        "experience_years": profile.experience_years,
        "current_ctc_annual": _money(profile.current_ctc_annual),
        "expected_ctc_annual": _money(profile.expected_ctc_annual),
        "currency": profile.currency,
        "notice_period_days": profile.notice_period_days,
        "work_mode": profile.work_mode,
        "education_level": profile.education_level,
        "completeness": profile.completeness or 0.0,
    }


def serialize_candidate_summary(
    row: CandidateSearchRow,
    skills: list[CandidateSkill] | None = None,
    desired_roles: list[CandidateRolePref] | None = None,
    locations: list[CandidateLocationPref] | None = None,
) -> dict[str, Any]:
    """One row of a search/list response. Operational fields only."""
    c = row.candidate
    out: dict[str, Any] = {
        "candidate_id": str(c.id),
        "phone_number": c.phone_number,
        "lifecycle_status": c.lifecycle_status.value,
        "consent_status": c.consent_status.value,
        "created_at": _iso(c.created_at),
        **_profile_fields(row.profile),
        "skills": sorted({s.skill_raw for s in (skills or [])}),
        "desired_roles": sorted({r.role_raw for r in (desired_roles or [])}),
        "locations": [
            {"location": loc.location_raw, "strength": loc.strength}
            for loc in (locations or [])
        ],
    }
    return out


def serialize_candidate_detail(
    row: CandidateSearchRow,
    skills: list[CandidateSkill],
    desired_roles: list[CandidateRolePref],
    locations: list[CandidateLocationPref],
    current_attributes: list[Any],
) -> dict[str, Any]:
    """Full single-candidate view: operational fields plus personal
    attributes (never protected). This is the ONLY function in this module
    that may emit personal-class data, and only via this explicit call —
    never from serialize_candidate_summary.
    """
    summary = serialize_candidate_summary(row, skills, desired_roles, locations)

    personal: dict[str, Any] = {}
    for attr in current_attributes:
        # Re-derive data_class from the registry rather than trusting the
        # stored attr.data_class alone — belt and suspenders against a bad
        # write anywhere upstream ever reaching this response.
        data_class = get_data_class(attr.key)
        if data_class == DataClassEnum.personal:
            personal[attr.key] = attr.value
        # protected is deliberately never added to any dict here.

    summary["personal_attributes"] = personal
    return summary


def serialize_resume_metadata(resume: Resume) -> dict[str, Any]:
    """Resume metadata only — never a download URL. Signed URLs are minted
    per-request by the dedicated resume endpoint, which audits the access;
    embedding one here would mean an unaudited link handed out on every
    candidate-detail view."""
    return {
        "resume_id": str(resume.id),
        "version": resume.version,
        "filename": resume.filename,
        "content_type": resume.content_type,
        "size_bytes": resume.size_bytes,
        "is_current": resume.is_current,
        "uploaded_at": _iso(resume.uploaded_at),
        "confirmed_at": _iso(resume.confirmed_at),
        "parse_status": resume.parse_status,
    }


def serialize_conversation_summary(conversation: Any) -> dict[str, Any]:
    return {
        "conversation_id": str(conversation.id),
        "channel": conversation.channel.value,
        "status": conversation.status.value,
        "mode": conversation.mode.value,
        "started_at": _iso(conversation.started_at),
        "last_inbound_at": _iso(conversation.last_inbound_at),
        "last_outbound_at": _iso(conversation.last_outbound_at),
        "closed_at": _iso(conversation.closed_at),
        "summary": conversation.summary,
    }


def serialize_message(message: Any) -> dict[str, Any]:
    return {
        "message_id": str(message.id),
        "direction": message.direction.value,
        "body": message.body,
        "has_media": message.media_ref is not None,
        "created_at": _iso(message.created_at),
    }


def serialize_search_response(
    rows_with_prefs: list[tuple[CandidateSearchRow, list, list, list]],
    total: int,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "candidates": [
            serialize_candidate_summary(row, skills, roles, locs)
            for row, skills, roles, locs in rows_with_prefs
        ],
    }
