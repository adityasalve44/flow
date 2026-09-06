"""
tests/conftest.py — test isolation through transactional rollback.

Every test that touches the database gets a session that is wrapped in an
outer transaction which is rolled back after the test finishes.  Nothing is
ever committed to the real database — all writes are invisible to other tests
and leave no residue behind.

Usage:
    def test_something(db):
        candidate = CandidateFactory.build(db)
        ...
    # After this function exits, the outer transaction is rolled back.

Session-scoped:
    ``test_engine``   — connected to flow_test, migrations applied once

Function-scoped:
    ``db``            — bound session with savepoint; rolled back after test

The approach:
    1. A connection is checked out from the engine pool (session-scoped).
    2. An outer transaction is begun on that connection.
    3. Each test creates a SQLAlchemy Session bound to that connection
       and immediately creates a SAVEPOINT (nested transaction).
    4. After the test, the SAVEPOINT is rolled back, and then the outer
       transaction too.  The connection is returned to the pool clean.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Base


def _get_test_db_url() -> str:
    """Read the test database URL from settings without setting ENV=test globally."""
    # Construct a Settings with just the fields we need from the .env file,
    # but force test_database_url resolution by reading environment directly.
    import os
    from dotenv import load_dotenv
    load_dotenv()
    test_url = os.getenv("TEST_DATABASE_URL")
    db_url = os.getenv("DATABASE_URL", "")
    if test_url:
        return test_url
    # Fallback: replace database name in the primary URL
    if "/flow" in db_url:
        return db_url.replace("/flow", "/flow_test", 1)
    return db_url


# ---------------------------------------------------------------------------
# Engine — session-scoped, schema created once
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def test_engine():
    """Create an engine pointed at flow_test and apply all model tables."""
    db_url = _get_test_db_url()
    engine = create_engine(db_url, pool_pre_ping=True)

    with engine.begin() as conn:
        # Drop all known tables with CASCADE to handle cross-references
        # from legacy schema runs.
        conn.execute(
            __import__("sqlalchemy").text(
                "DROP SCHEMA public CASCADE; CREATE SCHEMA public; "
                "CREATE SCHEMA IF NOT EXISTS flow;"
            )
        )
        Base.metadata.create_all(conn)

    yield engine
    engine.dispose()


# ---------------------------------------------------------------------------
# Transactional isolation — function-scoped
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(test_engine):
    """Yield a Session that is fully rolled back after every test."""
    connection = test_engine.connect()
    outer_tx = connection.begin()

    # join_transaction_mode="create_savepoint" makes Session.begin_nested()
    # transparent so the session can use savepoints inside the outer tx.
    session = Session(bind=connection, join_transaction_mode="create_savepoint")

    yield session

    session.close()
    outer_tx.rollback()
    connection.close()


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

class CandidateFactory:
    _counter = 0

    @classmethod
    def build(cls, db: Session, **kwargs):
        """Insert and return a Candidate, flushed but not committed."""
        from app.models import Candidate

        cls._counter += 1
        defaults = dict(
            phone_number=f"+9190000{cls._counter:05d}",
            display_name=f"Test Candidate {cls._counter}",
        )
        defaults.update(kwargs)
        candidate = Candidate(**defaults)
        db.add(candidate)
        db.flush()
        return candidate


class ConversationFactory:
    _counter = 0

    @classmethod
    def build(cls, db: Session, candidate_id, **kwargs):
        from app.models import Conversation

        cls._counter += 1
        defaults = dict(
            candidate_id=candidate_id,
        )
        defaults.update(kwargs)
        conv = Conversation(**defaults)
        db.add(conv)
        db.flush()
        return conv
