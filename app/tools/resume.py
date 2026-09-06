"""
app/tools/resume.py — Safe resume confirmation tool (FLOW-036).

Core requirements (§8, §11, FLOW-036 of REVIEW_AND_PLAN.md):
- record_resume_confirmation(tool_context: ToolContext) -> dict[str, Any]
- Reads candidate_id securely from the session context state — NEVER from model-supplied arguments.
- Updates resumes.confirmed_at = now() for the current resume without requesting a file upload.
- Generates an AuditEvent recording the confirmation.
- FunctionDeclaration exposes zero arguments to the LLM (identity spoofing prevention).
"""

from typing import Any
from uuid import UUID

from google.adk.tools import ToolContext

from app.services.resume import confirm_current_resume


def record_resume_confirmation(tool_context: ToolContext) -> dict[str, Any]:
    """Record confirmation that the candidate's existing resume on file is current.

    Candidate identity is read strictly from session state, preventing identity spoofing.
    Sets confirmed_at to current timestamp and writes an audit event.

    Args:
        tool_context: Injected ADK session context containing candidate identity and state.

    Returns:
        dict: Confirmation outcome and status.
    """
    state = getattr(tool_context, "state", None)
    if state is None and isinstance(tool_context, dict):
        state = tool_context

    if not state:
        return {
            "success": False,
            "error": "No active conversation session context found.",
        }

    candidate_id_raw = state.get("candidate_id")
    if not candidate_id_raw:
        return {
            "success": False,
            "error": "No candidate identity found in active session context.",
        }

    try:
        candidate_id = UUID(str(candidate_id_raw))
    except (ValueError, TypeError):
        return {
            "success": False,
            "error": f"Invalid candidate_id format: {candidate_id_raw}",
        }

    # Resolve UnitOfWork from context or default
    uow = getattr(tool_context, "uow", None) or state.get("uow")
    if uow is not None:
        confirmed = confirm_current_resume(uow, candidate_id, actor_type="candidate")
        if confirmed:
            return {
                "success": True,
                "confirmed": True,
                "resume_id": str(confirmed.id),
                "version": confirmed.version,
                "message": "Resume confirmed as latest.",
            }
        return {
            "success": False,
            "confirmed": False,
            "message": "No active resume found on file to confirm.",
        }

    from app.db.uow import UnitOfWork

    with UnitOfWork() as session_uow:
        confirmed = confirm_current_resume(session_uow, candidate_id, actor_type="candidate")
        session_uow.commit()
        if confirmed:
            return {
                "success": True,
                "confirmed": True,
                "resume_id": str(confirmed.id),
                "version": confirmed.version,
                "message": "Resume confirmed as latest.",
            }
        return {
            "success": False,
            "confirmed": False,
            "message": "No active resume found on file to confirm.",
        }
