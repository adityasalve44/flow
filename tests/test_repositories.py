"""
tests/test_repositories.py — tests for repositories and UnitOfWork.

Tests:
1. UnitOfWork commits on clean exit.
2. UnitOfWork rolls back on exception — a failed turn writes nothing.
3. CandidateRepository.get_or_create_by_phone idempotency & atomicity.
4. Concurrency test: simultaneous get_or_create_by_phone from multiple threads yields exactly 1 candidate.
5. Conversation & Message repositories operations.
6. AttributeRepository fact store, provenance and supersession.
7. Architecture test: no commit() or rollback() calls anywhere inside app/repositories.
"""

import inspect
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.repositories
from app.db.uow import UnitOfWork
from app.models import Candidate, CandidateAttribute
from app.models.enums import (
    AttributeStatusEnum,
    ChannelEnum,
    ConfidenceEnum,
    ConversationModeEnum,
    ConversationStatusEnum,
    DataClassEnum,
    DirectionEnum,
    SourceEnum,
)
from tests.conftest import _get_test_db_url


def test_no_commits_in_repositories():
    """Architectural invariant: repositories must never call commit() or rollback()."""
    for name in dir(app.repositories):
        obj = getattr(app.repositories, name)
        if inspect.isclass(obj) and obj.__module__.startswith("app.repositories"):
            source = inspect.getsource(obj)
            assert "session.commit()" not in source, f"{name} contains session.commit()"
            assert "session.rollback()" not in source, f"{name} contains session.rollback()"


def test_uow_commits_on_clean_exit(db):
    """When UnitOfWork exits cleanly, staged records are committed."""
    phone = f"+9199999{uuid4().hex[:6]}"

    uow = UnitOfWork(session=db)
    with uow:
        uow.candidates.get_or_create_by_phone(phone_number=phone, display_name="Committed User")

    # Verify candidate exists in db session
    candidate = db.scalar(select(Candidate).where(Candidate.phone_number == phone))
    assert candidate is not None
    assert candidate.display_name == "Committed User"


def test_uow_rolls_back_on_exception(db):
    """When an exception is raised inside UnitOfWork, changes are rolled back."""
    phone = f"+9188888{uuid4().hex[:6]}"

    class SimulatedTurnFailure(Exception):
        pass

    uow = UnitOfWork(session=db)
    with pytest.raises(SimulatedTurnFailure), uow:
        uow.candidates.get_or_create_by_phone(phone_number=phone, display_name="Failed User")
        raise SimulatedTurnFailure("Something went wrong during turn processing")

    # In the database session, candidate must NOT exist
    candidate = db.scalar(select(Candidate).where(Candidate.phone_number == phone))
    assert candidate is None


def test_candidate_get_or_create_by_phone(db):
    """get_or_create_by_phone creates on first call and retrieves on second."""
    phone = f"+9177777{uuid4().hex[:6]}"
    uow = UnitOfWork(session=db)

    with uow:
        c1 = uow.candidates.get_or_create_by_phone(phone, display_name="First Call")
        assert c1.id is not None
        first_id = c1.id

        # Second call with the same phone returns the same candidate
        c2 = uow.candidates.get_or_create_by_phone(phone, display_name="Second Call")
        assert c2.id == first_id

    # Verify only one row exists
    count = len(db.scalars(select(Candidate).where(Candidate.phone_number == phone)).all())
    assert count == 1


def test_concurrent_candidate_get_or_create():
    """
    Two or more concurrent first-messages from the same phone number
    must yield exactly one candidate row without race condition exceptions.
    """
    db_url = _get_test_db_url()
    engine = create_engine(db_url)
    phone = f"+9166666{uuid4().hex[:6]}"

    def create_candidate(thread_idx: int):
        # Each thread gets its own independent Session and transaction
        with Session(engine) as session:
            uow = UnitOfWork(session=session)
            with uow:
                cand = uow.candidates.get_or_create_by_phone(
                    phone_number=phone,
                    display_name=f"Thread-{thread_idx}",
                )
                return cand.id

    # Run 6 threads trying to insert/resolve the same candidate simultaneously
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(create_candidate, i) for i in range(6)]
        results = [f.result() for f in futures]

    # All threads must receive the same candidate ID
    first_id = results[0]
    assert all(cid == first_id for cid in results), f"Got different IDs: {results}"

    # Verify exactly one row in the database
    with Session(engine) as session:
        matching = session.scalars(select(Candidate).where(Candidate.phone_number == phone)).all()
        assert len(matching) == 1
        # Cleanup
        session.delete(matching[0])
        session.commit()

    engine.dispose()


def test_conversation_and_message_repositories(db):
    """ConversationRepository and MessageRepository creation and queries."""
    uow = UnitOfWork(session=db)
    phone = f"+9155555{uuid4().hex[:6]}"

    with uow:
        candidate = uow.candidates.get_or_create_by_phone(phone)
        conv = uow.conversations.create(
            candidate_id=candidate.id,
            channel=ChannelEnum.whatsapp,
            mode=ConversationModeEnum.intake,
        )
        assert conv.id is not None
        assert conv.status == ConversationStatusEnum.active

        # Check get_active
        active_conv = uow.conversations.get_active(candidate.id)
        assert active_conv is not None
        assert active_conv.id == conv.id

        # Create messages
        m1 = uow.messages.create(
            conversation_id=conv.id,
            candidate_id=candidate.id,
            direction=DirectionEnum.inbound,
            body="Hello, I want to apply",
            channel_message_id="msg_ext_001",
        )
        m2 = uow.messages.create(
            conversation_id=conv.id,
            candidate_id=candidate.id,
            direction=DirectionEnum.outbound,
            body="Hi! What is your current role?",
            channel_message_id="msg_ext_002",
        )
        assert m1.id is not None
        assert m2.id is not None

        # Check idempotency lookup
        found = uow.messages.get_by_channel_message_id("msg_ext_001")
        assert found is not None
        assert found.id == m1.id

        # Check recent messages
        recent = uow.messages.get_recent(conv.id, limit=10)
        assert len(recent) == 2
        assert {m.id for m in recent} == {m1.id, m2.id}


def test_attribute_repository_provenance_and_supersession(db):
    """AttributeRepository stores facts, tracks provenance, and supersedes."""
    uow = UnitOfWork(session=db)
    phone = f"+9144444{uuid4().hex[:6]}"

    with uow:
        cand = uow.candidates.get_or_create_by_phone(phone)
        conv = uow.conversations.create(candidate_id=cand.id)

        # Fact 1: candidate states 3 years experience
        attr1 = CandidateAttribute(
            candidate_id=cand.id,
            key="experience_years",
            value={"amount": 3.0},
            raw_text="3 yrs",
            source=SourceEnum.candidate_stated,
            confidence=ConfidenceEnum.confirmed,
            status=AttributeStatusEnum.current,
            data_class=DataClassEnum.operational,
            conversation_id=conv.id,
        )
        uow.attributes.add(attr1)

        # Retrieve current fact
        current_fact = uow.attributes.get_current_by_key(cand.id, "experience_years")
        assert current_fact is not None
        assert current_fact.value == {"amount": 3.0}

        # Fact 2: later corrects to 4.5 years (supersedes attr1)
        attr2 = CandidateAttribute(
            candidate_id=cand.id,
            key="experience_years",
            value={"amount": 4.5},
            raw_text="actually 4.5 years",
            source=SourceEnum.candidate_stated,
            confidence=ConfidenceEnum.confirmed,
            status=AttributeStatusEnum.current,
            data_class=DataClassEnum.operational,
            conversation_id=conv.id,
        )
        uow.attributes.supersede(attr1.id, attr2)

        # Verify attr1 is superseded and attr2 is current
        all_facts = uow.attributes.get_all_for_candidate(cand.id)
        assert len(all_facts) == 2

        current_facts = uow.attributes.get_current_for_candidate(cand.id)
        assert len(current_facts) == 1
        assert current_facts[0].id == attr2.id
        assert current_facts[0].value == {"amount": 4.5}
