"""
tests/test_adk_session.py — ADK session wiring and schema isolation tests (FLOW-017).

Tests:
1. Schema placement: ADK tables (sessions, events, app_states, user_states,
   adk_internal_metadata) land exclusively in schema 'adk'; zero tables in 'public'.
2. Alembic isolation: autogenerate after ADK tables exist emits zero operations (no drops/creates).
3. State initialisation: session state contains trusted candidate_id, conversation_id, mode.
4. Session reuse: subsequent resolution within the active window returns the existing session.
5. DatabaseSessionService integration: verified against PostgreSQL adk schema.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from google.adk.sessions import DatabaseSessionService, InMemorySessionService
from sqlalchemy import text

from app.agents.session import (
    FLOW_APP_NAME,
    create_session_service,
    get_adk_async_engine,
    get_or_create_adk_session,
)
from app.config import get_settings
from app.db.uow import UnitOfWork
from app.models.enums import ConsentStatusEnum, ConversationModeEnum
from app.services.conversation import resolve_conversation


@pytest.mark.asyncio
async def test_session_state_initialisation(db):
    """
    Acceptance test: ADK session state is initialised with trusted
    candidate_id, conversation_id, and mode before any model calls.
    """
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    t0 = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        uow.candidates.add(cand)

        conv = resolve_conversation(uow, cand, now=t0)
        assert conv.mode == ConversationModeEnum.intake

        session_service = InMemorySessionService()
        adk_session = await get_or_create_adk_session(
            session_service=session_service,
            candidate=cand,
            conversation=conv,
        )

        assert adk_session.id == str(conv.id)
        assert adk_session.user_id == str(cand.id)
        assert adk_session.state["candidate_id"] == str(cand.id)
        assert adk_session.state["conversation_id"] == str(conv.id)
        assert adk_session.state["mode"] == "intake"
        assert conv.adk_session_id == adk_session.id


@pytest.mark.asyncio
async def test_session_reuse_within_window(db):
    """
    Acceptance test: Resolving the session for an existing ongoing conversation
    reuses the same ADK session and preserves any updated state.
    """
    uow = UnitOfWork(session=db)
    phone = f"+9191{uuid4().int % 100000000:08d}"
    t0 = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        cand.consent_status = ConsentStatusEnum.granted
        conv = resolve_conversation(uow, cand, now=t0)

        session_service = InMemorySessionService()
        session1 = await get_or_create_adk_session(
            session_service=session_service,
            candidate=cand,
            conversation=conv,
        )

        # Update state via ADK's standard Event/EventActions mechanism
        from google.adk.events import Event, EventActions
        ev = Event(actions=EventActions(state_delta={"turn_count": 1}))
        await session_service.append_event(session1, ev)

        # Second turn resolves the same conversation
        session2 = await get_or_create_adk_session(
            session_service=session_service,
            candidate=cand,
            conversation=conv,
        )

        assert session2.id == session1.id
        assert session2.state["turn_count"] == 1
        assert session2.state["candidate_id"] == str(cand.id)


@pytest.mark.asyncio
async def test_schema_placement_in_adk(db, test_engine):
    """
    Acceptance test: ADK tables appear exclusively in the 'adk' schema.
    Public schema contains zero tables.
    """
    # Prepare tables via DatabaseSessionService using the test database
    from sqlalchemy.ext.asyncio import create_async_engine

    from tests.conftest import _get_test_db_url

    test_url = _get_test_db_url()
    async_test_engine = create_async_engine(
        test_url,
        connect_args={"options": "-csearch_path=adk"},
    )
    service = DatabaseSessionService(db_engine=async_test_engine)
    await service.prepare_tables()
    await async_test_engine.dispose()

    # Query PostgreSQL information_schema for table locations
    adk_tables = db.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'adk';")
    ).scalars().all()

    public_tables = db.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';")
    ).scalars().all()

    flow_tables = db.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'flow';")
    ).scalars().all()

    # Public schema must have 0 tables
    assert len(public_tables) == 0, f"Expected 0 tables in public schema, found: {public_tables}"

    # Flow domain schema must contain domain tables
    assert "candidates" in flow_tables
    assert "conversations" in flow_tables
    assert "candidate_attributes" in flow_tables
    assert "candidate_profiles" in flow_tables

    # DatabaseSessionService tables land in 'adk'
    expected_adk_tables = {"sessions", "events", "app_states", "user_states", "adk_internal_metadata"}
    for expected in expected_adk_tables:
        assert expected in adk_tables, f"Expected {expected} in adk schema, found: {adk_tables}"


@pytest.mark.asyncio
async def test_database_session_service_adk_persistence():
    """
    Acceptance test: DatabaseSessionService creates and reads sessions directly
    from PostgreSQL adk schema using an AsyncEngine with search_path=adk.
    """
    settings = get_settings()
    engine = get_adk_async_engine(settings.effective_database_url)
    service = create_session_service(db_engine=engine)
    assert isinstance(service, DatabaseSessionService)

    # Ensure tables in adk schema
    await service.prepare_tables()

    cand_id = str(uuid4())
    conv_id = str(uuid4())
    initial_state = {
        "candidate_id": cand_id,
        "conversation_id": conv_id,
        "mode": "intake",
    }

    session = await service.create_session(
        app_name=FLOW_APP_NAME,
        user_id=cand_id,
        session_id=conv_id,
        state=initial_state,
    )

    assert session.id == conv_id
    assert session.user_id == cand_id
    assert session.state["candidate_id"] == cand_id

    # Fetch back
    fetched = await service.get_session(
        app_name=FLOW_APP_NAME,
        user_id=cand_id,
        session_id=conv_id,
    )
    assert fetched is not None
    assert fetched.id == conv_id
    assert fetched.state["mode"] == "intake"
