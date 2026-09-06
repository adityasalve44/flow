"""
app/database.py — SQLAlchemy engine and session factory.

The engine is constructed lazily via get_engine() so that importing this
module has no side effects and does not require DATABASE_URL to be set.
Tests can swap the URL by pointing settings.test_database_url at flow_test.
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def get_engine():
    """Return a synchronous SQLAlchemy engine built from settings."""
    settings = get_settings()
    return create_engine(
        settings.effective_database_url,
        pool_pre_ping=True,
    )


def get_session_factory():
    """Return a sessionmaker bound to the current engine."""
    return sessionmaker(
        bind=get_engine(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yields a database session per request."""
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()