"""
app/repositories/resume.py — ResumeRepository: database operations for candidate resumes.

Key invariants:
- Repositories only read/stage changes against the Session; they never commit.
- Transaction boundaries belong strictly to the UnitOfWork.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.resume import Resume


class ResumeRepository:
    """Repository for Resume entity (FLOW-035)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, resume: Resume) -> Resume:
        """Stage a new Resume record."""
        self.session.add(resume)
        return resume

    def get_by_id(self, resume_id: UUID) -> Resume | None:
        """Fetch a resume by its primary key."""
        return self.session.execute(
            select(Resume).where(Resume.id == resume_id)
        ).scalar_one_or_none()

    def get_current(self, candidate_id: UUID) -> Resume | None:
        """Fetch the currently active resume for a candidate, if one exists."""
        return self.session.execute(
            select(Resume).where(
                Resume.candidate_id == candidate_id,
                Resume.is_current.is_(True),
            )
        ).scalar_one_or_none()

    def get_by_candidate(self, candidate_id: UUID) -> list[Resume]:
        """Fetch all resumes for a candidate, newest version first."""
        return list(
            self.session.execute(
                select(Resume)
                .where(Resume.candidate_id == candidate_id)
                .order_by(Resume.version.desc())
            ).scalars().all()
        )

    def get_by_checksum(self, candidate_id: UUID, checksum: str) -> Resume | None:
        """Find an existing resume with the identical checksum for this candidate."""
        return self.session.execute(
            select(Resume).where(
                Resume.candidate_id == candidate_id,
                Resume.checksum == checksum,
            )
        ).scalar_one_or_none()

    def get_max_version(self, candidate_id: UUID) -> int:
        """Return the highest version number for this candidate, or 0 if none exist."""
        result = self.session.execute(
            select(func.coalesce(func.max(Resume.version), 0)).where(
                Resume.candidate_id == candidate_id
            )
        ).scalar_one()
        return int(result)

    def demote_current(self, candidate_id: UUID) -> int:
        """
        Mark all current resumes for a candidate as not current.
        Returns the number of rows updated.
        """
        result = self.session.execute(
            update(Resume)
            .where(
                Resume.candidate_id == candidate_id,
                Resume.is_current.is_(True),
            )
            .values(is_current=False)
        )
        return result.rowcount

    def confirm_resume(
        self,
        resume_id: UUID,
        confirmed_at: datetime | None = None,
    ) -> Resume | None:
        """Mark a resume as confirmed by the candidate at the given timestamp."""
        resume = self.get_by_id(resume_id)
        if resume is not None:
            resume.confirmed_at = confirmed_at or datetime.now(UTC)
        return resume
