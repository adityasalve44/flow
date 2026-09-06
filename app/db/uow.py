"""
app/db/uow.py — Unit of Work transaction manager.

The UnitOfWork context manager owns transaction boundaries across the system:
- One request / turn = one transaction.
- Repositories only read and stage (flush) data; they never commit.
- If an exception is raised inside the block, rollback() is executed.
- If the block exits cleanly, commit() is executed.
- Provided as a FastAPI dependency via get_uow().
"""

from collections.abc import Generator
from types import TracebackType

from sqlalchemy.orm import Session, sessionmaker

from app.database import get_session_factory
from app.repositories.attribute import AttributeRepository
from app.repositories.candidate import CandidateRepository
from app.repositories.conversation import (
    ConversationRepository,
    MessageRepository,
)
from app.repositories.profile import ProfileRepository
from app.repositories.resume import ResumeRepository


class UnitOfWork:
    """Coordinates transactions and data-access repositories."""

    def __init__(
        self,
        session: Session | None = None,
        session_factory: sessionmaker[Session] | None = None,
    ):
        self._session_factory = session_factory or get_session_factory()
        self._external_session = session is not None
        self.session: Session | None = session
        if session is not None:
            self._init_repositories(session)

    def _init_repositories(self, session: Session) -> None:
        self.candidates = CandidateRepository(session)
        self.conversations = ConversationRepository(session)
        self.messages = MessageRepository(session)
        self.attributes = AttributeRepository(session)
        self.profiles = ProfileRepository(session)
        self.resumes = ResumeRepository(session)

    def __enter__(self) -> "UnitOfWork":
        if self.session is None:
            self.session = self._session_factory()
            self._init_repositories(self.session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None:
                self.rollback()
            else:
                self.commit()
        finally:
            if not self._external_session and self.session is not None:
                self.session.close()

    def commit(self) -> None:
        """Commit the current transaction."""
        if self.session is not None:
            self.session.commit()

    def rollback(self) -> None:
        """Rollback the current transaction."""
        if self.session is not None:
            self.session.rollback()


def get_uow() -> Generator[UnitOfWork, None, None]:
    """FastAPI dependency yielding a UnitOfWork instance per request."""
    with UnitOfWork() as uow:
        yield uow
