"""
app/tools/snapshot.py — Safe candidate profile snapshot tool (FLOW-022).

Core requirements (§8, FLOW-022 of REVIEW_AND_PLAN.md):
- get_candidate_snapshot(tool_context: ToolContext) -> dict[str, Any]
- Reads candidate_id from session context state — NEVER from model-supplied arguments.
- Excludes sensitive attributes (protected, personal, or unknown fail-closed data classes).
- Only returns operational attributes (desired_role, experience_years, skills, etc.).
- Generated FunctionDeclaration has zero parameters exposed to the LLM.
"""

from typing import Any

from google.adk.tools import ToolContext

from app.domain.registry import get_data_class
from app.models.enums import DataClassEnum


def get_candidate_snapshot(tool_context: ToolContext) -> dict[str, Any]:
    """Retrieve the operational profile snapshot for the active candidate in this conversation.

    Sensitive personal and protected attributes are strictly excluded from the output.
    Candidate identity is read securely from the session context, preventing identity spoofing.

    Args:
        tool_context: Injected ADK session context containing candidate identity and state.

    Returns:
        dict: Operational candidate snapshot excluding personal/protected information.
    """
    state = getattr(tool_context, "state", None)
    if state is None and isinstance(tool_context, dict):
        state = tool_context

    if not state:
        return {
            "found": False,
            "error": "No active conversation session context found.",
        }

    candidate_id = state.get("candidate_id")
    if not candidate_id:
        return {
            "found": False,
            "error": "No candidate identity found in active session context.",
        }

    # Retrieve raw snapshot data from state or database
    raw_data: dict[str, Any] = {}
    if "candidate_snapshot" in state and isinstance(state["candidate_snapshot"], dict):
        raw_data.update(state["candidate_snapshot"])
    elif "profile" in state and isinstance(state["profile"], dict):
        raw_data.update(state["profile"])
    else:
        # DB lookup if candidate_id is present
        profile = None
        uow = getattr(tool_context, "uow", None) or state.get("uow")
        db_session = getattr(tool_context, "db", None) or state.get("db")

        if uow is not None:
            profile = uow.profiles.get_by_candidate_id(candidate_id)
        elif db_session is not None:
            from app.repositories.profile import ProfileRepository
            profile = ProfileRepository(db_session).get_by_candidate_id(candidate_id)
        else:
            try:
                from app.db.uow import UnitOfWork
                with UnitOfWork() as default_uow:
                    profile = default_uow.profiles.get_by_candidate_id(candidate_id)
            except Exception:
                pass

        if profile:
            raw_data = {
                "current_role": profile.current_role,
                "current_company": profile.current_company,
                "experience_years": profile.experience_years,
                "current_ctc": float(profile.current_ctc_annual) if profile.current_ctc_annual is not None else None,
                "expected_ctc": float(profile.expected_ctc_annual) if profile.expected_ctc_annual is not None else None,
                "notice_period": profile.notice_period_days,
                "work_mode": profile.work_mode,
                "education": profile.education_level,
                "completeness_score": profile.completeness,
            }

    # Strictly filter out sensitive data:
    # 1. Reject keys explicitly designated personal or protected by the key registry.
    # 2. Reject unknown keys (fail-closed, as unknown keys default to personal).
    # 3. Only keep keys that are operational, plus harmless metadata like completeness_score.
    safe_snapshot: dict[str, Any] = {}
    for key, val in raw_data.items():
        if key in ("completeness_score", "is_ready"):
            safe_snapshot[key] = val
            continue

        # Check data classification against authoritative registry
        data_class = get_data_class(key)
        if data_class == DataClassEnum.operational:
            safe_snapshot[key] = val

    return {
        "found": True,
        "candidate_id": str(candidate_id),
        "snapshot": safe_snapshot,
    }
