"""
app/agents/session.py — ADK session wiring and PostgreSQL schema isolation (FLOW-017).

Core requirements (§4, §5, §6, §8, §19 of REVIEW_AND_PLAN.md):
- Persistent ADK sessions must not collide with Flow's domain schema.
- Built over postgresql+psycopg using an AsyncEngine with connect_args:
    options="-csearch_path=adk"
- This isolates the 5 ADK tables (sessions, events, app_states, user_states,
  adk_internal_metadata) entirely within the 'adk' schema.
- Maps user_id -> str(candidate.id), session_id -> str(conversation.id).
- Stores trusted state at creation (candidate_id, conversation_id, mode).
- Factory supports InMemorySessionService for fast, isolated tests.
"""

from typing import Any

from google.adk.sessions import (
    BaseSessionService,
    DatabaseSessionService,
    InMemorySessionService,
    Session,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import get_settings
from app.models import Candidate, Conversation

FLOW_APP_NAME = "flow"

_adk_engine: AsyncEngine | None = None
_session_service: BaseSessionService | None = None


def get_adk_async_engine(db_url: str | None = None) -> AsyncEngine:
    """Construct or return the AsyncEngine pinned to search_path=adk."""
    global _adk_engine
    if _adk_engine is None or db_url is not None:
        url = db_url or get_settings().effective_database_url
        engine = create_async_engine(
            url,
            connect_args={"options": "-csearch_path=adk"},
            pool_pre_ping=True,
        )
        if db_url is None:
            _adk_engine = engine
        return engine
    return _adk_engine


async def dispose_adk_async_engine() -> None:
    """Dispose the global ADK async engine."""
    global _adk_engine
    if _adk_engine is not None:
        await _adk_engine.dispose()
        _adk_engine = None


def create_session_service(
    db_engine: AsyncEngine | None = None,
    db_url: str | None = None,
    in_memory: bool = False,
) -> BaseSessionService:
    """
    Create a new ADK session service.

    If in_memory is True, returns InMemorySessionService.
    Otherwise returns DatabaseSessionService bound to an AsyncEngine targeting schema 'adk'.
    """
    if in_memory:
        return InMemorySessionService()

    engine = db_engine or get_adk_async_engine(db_url)
    return DatabaseSessionService(db_engine=engine)


def get_session_service(force_in_memory: bool = False) -> BaseSessionService:
    """Get or initialize the singleton session service."""
    global _session_service
    if _session_service is None:
        in_mem = force_in_memory or get_settings().is_testing
        _session_service = create_session_service(in_memory=in_mem)
    return _session_service


def set_session_service(service: BaseSessionService | None) -> None:
    """Override or reset the singleton session service (e.g. for testing)."""
    global _session_service
    _session_service = service


async def get_or_create_adk_session(
    session_service: BaseSessionService,
    candidate: Candidate,
    conversation: Conversation,
    app_name: str = FLOW_APP_NAME,
    extra_state: dict[str, Any] | None = None,
) -> Session:
    """
    Retrieve an existing ADK session or create a new one with trusted state.

    Invariants (§8, FLOW-017):
    - user_id = str(candidate.id)
    - session_id = str(conversation.id)
    - Initial state contains trusted identity:
        'candidate_id': str(candidate.id)
        'conversation_id': str(conversation.id)
        'mode': conversation.mode.value
    - conversation.adk_session_id is synchronized with session.id.
    """
    user_id = str(candidate.id)
    session_id = str(conversation.id)

    # 1. Try fetching existing session
    session = await session_service.get_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
    )

    # 2. If not found, create new session with trusted state
    if session is None:
        mode_val = (
            conversation.mode.value
            if hasattr(conversation.mode, "value")
            else str(conversation.mode)
        )
        trusted_state: dict[str, Any] = {
            "candidate_id": str(candidate.id),
            "conversation_id": str(conversation.id),
            "mode": mode_val,
        }
        if extra_state:
            trusted_state.update(extra_state)

        session = await session_service.create_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            state=trusted_state,
        )

    # 3. Synchronize conversation.adk_session_id
    if conversation.adk_session_id != session.id:
        conversation.adk_session_id = session.id

    return session
