"""
app/agents/summariser.py — Conversation summarisation on conversation close (FLOW-031).

Core requirements (§8, FLOW-031 of REVIEW_AND_PLAN.md):
- Summarise on conversation close, store on the conversation row (conversations.summary).
- Keep old context available without paying for it every turn.
- Summaries capture key facts discussed, objections/deflections, and final status.
- Hybrid: Uses Gemini LLM when available; robust deterministic fallback otherwise.
- Invariant: Never claims to remember something absent from the store.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.db.uow import UnitOfWork
from app.models.candidate import Conversation, Message
from app.models.enums import ConversationStatusEnum, DirectionEnum

logger = logging.getLogger(__name__)

SUMMARISER_SYSTEM_PROMPT = """You are a recruitment conversation summariser for Flow.
Summarise the following WhatsApp conversation between Flow and a candidate.

Requirements:
1. Maximum 150 words.
2. Focus on facts established or discussed: roles, experience, skills, locations, CTC/salary, notice period.
3. Note any objections, deflections, or reason for closure.
4. Note the final outcome and readiness state.
5. Invariant: Only state facts present in the transcript. Do NOT hallucinate or extrapolate.
"""


def format_transcript(messages: list[Message]) -> str:
    """Format a list of Message models into a chronological dialogue transcript."""
    lines: list[str] = []
    for msg in messages:
        sender = "Candidate" if msg.direction == DirectionEnum.inbound else "Flow"
        body = (msg.body or "").strip()
        lines.append(f"{sender}: {body}")
    return "\n".join(lines)


def build_deterministic_summary(conversation: Conversation, messages: list[Message]) -> str:
    """
    Generate a structured, compact deterministic summary without external LLM calls.
    Used for unit tests, offline execution, or LLM fallback.
    """
    inbound_count = sum(1 for m in messages if m.direction == DirectionEnum.inbound)
    outbound_count = sum(1 for m in messages if m.direction == DirectionEnum.outbound)
    start_date = (
        conversation.started_at.strftime("%Y-%m-%d") if conversation.started_at else "Unknown"
    )

    summary_parts = [
        f"Conversation started {start_date} in mode '{conversation.mode.value}'.",
        f"Total turns: {inbound_count} inbound, {outbound_count} outbound messages.",
    ]

    if (conversation.deflection_count or 0) > 0:
        summary_parts.append(f"Recorded {conversation.deflection_count} deflections/refusals.")
    if (conversation.abuse_count or 0) > 0:
        summary_parts.append(f"Recorded {conversation.abuse_count} moderation/abuse warnings.")

    # Extract key candidate statements
    candidate_statements = [
        (m.body or "").strip()
        for m in messages
        if m.direction == DirectionEnum.inbound and m.body and len(m.body.strip()) > 3
    ]
    if candidate_statements:
        sample = "; ".join(candidate_statements[:4])
        if len(candidate_statements) > 4:
            sample += f"; ... (+{len(candidate_statements) - 4} more)"
        summary_parts.append(f"Candidate inputs: {sample}")

    summary_parts.append(f"Final status: {conversation.status.value}.")
    return " ".join(summary_parts)


def summarise_messages(
    messages: list[Message],
    conversation: Conversation,
    client: Any | None = None,
    model: str | None = None,
) -> str:
    """
    Produce a concise conversation summary.
    Attempts LLM summarisation via Gemini API if client/key is available;
    falls back cleanly to deterministic structured summarisation.

    Summarisation always runs on Gemini directly (not the agent pipeline's
    provider) — it is a cheap one-shot call with a robust deterministic
    fallback, so it does not go through app.agents.models.
    """
    if not messages:
        return f"Empty conversation in mode '{conversation.mode.value}' with 0 messages."

    settings = get_settings()
    model = model or settings.summariser_model

    # Try LLM summarisation
    llm_client = client
    if llm_client is None:
        try:
            if settings.google_api_key:
                from google import genai

                llm_client = genai.Client(api_key=settings.google_api_key)
        except Exception:
            llm_client = None

    if llm_client is not None:
        try:
            transcript = format_transcript(messages)
            prompt = f"{SUMMARISER_SYSTEM_PROMPT}\n\nTranscript:\n{transcript}"
            response = llm_client.models.generate_content(
                model=model,
                contents=prompt,
            )
            if response and getattr(response, "text", None):
                return response.text.strip()
        except Exception as exc:
            logger.warning("LLM summarisation failed, falling back to deterministic: %s", exc)

    return build_deterministic_summary(conversation, messages)


def summarise_conversation(
    uow: UnitOfWork,
    conversation_id: UUID | str,
    client: Any | None = None,
) -> str:
    """Summarise a conversation by ID and store on the conversation row."""
    conv = uow.conversations.get_by_id(conversation_id)
    if conv is None:
        raise ValueError(f"Conversation {conversation_id} not found.")

    messages = uow.messages.get_all_by_conversation(conv.id)
    summary = summarise_messages(messages, conv, client=client)
    conv.summary = summary
    uow.conversations.add(conv)
    return summary


def close_and_summarise_conversation(
    uow: UnitOfWork,
    conversation: Conversation,
    client: Any | None = None,
    reason: str = "closed",
) -> str:
    """
    Mark conversation as closed, generate summary, and persist to conversations.summary.
    """
    now = datetime.now(UTC)
    conversation.status = ConversationStatusEnum.closed
    conversation.closed_at = now

    messages = uow.messages.get_all_by_conversation(conversation.id)
    summary = summarise_messages(messages, conversation, client=client)
    conversation.summary = summary
    uow.conversations.add(conversation)
    return summary
