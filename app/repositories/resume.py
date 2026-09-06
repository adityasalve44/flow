"""
app/repositories/resume.py — placeholder for resume repository.

The Resume model and table are defined in Phase 4 (FLOW-035).
This repository will be fully implemented when the Resume model exists.
"""

from sqlalchemy.orm import Session


class ResumeRepository:
    """Repository for Resume entity (Phase 4)."""

    def __init__(self, session: Session):
        self.session = session
