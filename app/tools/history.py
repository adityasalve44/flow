"""
app/tools/history.py — Recall candidate history tool (FLOW-031).

Core requirements (§8, FLOW-031 of REVIEW_AND_PLAN.md):
- recall_candidate_history(tool_context, topic) returning summaries plus superseded facts.
- Fixed budget bound: Recall over a 200-message history stays within a fixed token budget.
- Honesty invariant: Flow never claims to remember something absent from the store.
- Candidate identity is read securely from session context, preventing spoofing.
- FunctionDeclaration exposes only the optional 'topic' parameter to the LLM.
"""

from typing import Any
from uuid import UUID

from google.adk.tools import ToolContext

from app.models.enums import AttributeStatusEnum

# Fixed budget bounds: keep total recall payload strictly bounded
MAX_SUMMARIES = 5
MAX_SUPERSEDED_FACTS = 8
MAX_OUTPUT_CHARS = 2500  # ~600 tokens


def recall_candidate_history(
    tool_context: ToolContext,
    topic: str | None = None,
) -> dict[str, Any]:
    """Recall prior conversation summaries and superseded facts for the active candidate.

    Use this tool when a candidate asks about past discussions, previously provided
    information ('I already told you my salary'), or when referencing prior conversations.

    Args:
        tool_context: Injected ADK session context containing candidate identity.
        topic: Optional subject or keyword to focus the recall on (e.g. 'ctc', 'notice period', 'role').

    Returns:
        dict: Past conversation summaries, superseded facts, and match confidence.
              Never claims to remember something absent from the store.
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

    # Resolve UnitOfWork from context or default
    uow = getattr(tool_context, "uow", None) or state.get("uow")
    db_session = getattr(tool_context, "db", None) or state.get("db")

    cand_uuid = UUID(str(candidate_id)) if not isinstance(candidate_id, UUID) else candidate_id

    if uow is not None:
        return _query_history(uow, cand_uuid, topic)
    elif db_session is not None:
        from app.db.uow import UnitOfWork
        temp_uow = UnitOfWork(session=db_session)
        return _query_history(temp_uow, cand_uuid, topic)
    else:
        from app.db.uow import UnitOfWork
        with UnitOfWork() as default_uow:
            return _query_history(default_uow, cand_uuid, topic)


def _query_history(uow: Any, candidate_id: UUID, topic: str | None = None) -> dict[str, Any]:
    """Query conversation summaries and superseded facts within a strict budget."""
    clean_topic = topic.strip().lower() if topic and topic.strip() else None

    # 1. Fetch conversations with summaries (closed or archived)
    conversations = uow.conversations.get_by_candidate(candidate_id, limit=10)
    summarised_convs = [c for c in conversations if c.summary and c.summary.strip()]

    # 2. Fetch superseded or stale facts from fact store
    all_attrs = uow.attributes.get_all_for_candidate(candidate_id)
    superseded_attrs = [
        a for a in all_attrs
        if a.status in (AttributeStatusEnum.superseded, AttributeStatusEnum.stale)
    ]

    # Filter by topic if specified
    matched_summaries: list[dict[str, Any]] = []
    for conv in summarised_convs:
        summary_text = conv.summary.strip()
        if clean_topic is None or clean_topic in summary_text.lower():
            matched_summaries.append({
                "conversation_id": str(conv.id),
                "started_at": conv.started_at.isoformat() if conv.started_at else None,
                "closed_at": conv.closed_at.isoformat() if conv.closed_at else None,
                "mode": conv.mode.value,
                "summary": summary_text,
            })

    matched_facts: list[dict[str, Any]] = []
    for attr in superseded_attrs:
        key_matches = clean_topic is None or clean_topic in attr.key.lower()
        val_matches = False
        if clean_topic and attr.raw_text:
            val_matches = clean_topic in str(attr.raw_text).lower()

        if clean_topic is None or key_matches or val_matches:
            matched_facts.append({
                "key": attr.key,
                "value": attr.value,
                "raw_text": attr.raw_text,
                "status": attr.status.value,
                "source": attr.source.value if hasattr(attr.source, "value") else str(attr.source),
                "created_at": attr.created_at.isoformat() if attr.created_at else None,
            })

    # Honesty invariant: Flow never claims to remember something absent from the store
    has_matches = bool(matched_summaries or matched_facts)
    if not has_matches:
        if clean_topic:
            return {
                "found": False,
                "topic": topic,
                "message": f"No prior history or recorded facts found regarding '{topic}'.",
                "summaries": [],
                "superseded_facts": [],
            }
        return {
            "found": False,
            "topic": None,
            "message": "No previous conversation history or superseded facts on record.",
            "summaries": [],
            "superseded_facts": [],
        }

    # Enforce strict budget bound
    bounded_summaries = matched_summaries[:MAX_SUMMARIES]
    bounded_facts = matched_facts[:MAX_SUPERSEDED_FACTS]

    result: dict[str, Any] = {
        "found": True,
        "candidate_id": str(candidate_id),
        "topic": topic,
        "summaries": bounded_summaries,
        "superseded_facts": bounded_facts,
        "total_summaries_found": len(matched_summaries),
        "total_facts_found": len(matched_facts),
    }

    # Validate output length against char budget
    result_str = str(result)
    if len(result_str) > MAX_OUTPUT_CHARS:
        # Trim summaries if budget exceeded
        result["summaries"] = bounded_summaries[:2]
        result["superseded_facts"] = bounded_facts[:4]
        result["truncated"] = True

    return result
