"""
app/repositories/attribute.py — candidate attribute (fact store) data access.

IMPORTANT: No commit() calls here. The service layer (UnitOfWork) owns
all transactions. These functions only read and stage (flush) — they
never commit.
"""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import CandidateAttribute
from app.models.enums import AttributeStatusEnum


class AttributeRepository:
    """Repository for CandidateAttribute entity (the fact store)."""

    def __init__(self, session: Session):
        self.session = session

    def get_current_for_candidate(
        self, candidate_id: UUID | str
    ) -> list[CandidateAttribute]:
        """Fetch all currently valid facts for a candidate."""
        statement = (
            select(CandidateAttribute)
            .where(
                CandidateAttribute.candidate_id == candidate_id,
                CandidateAttribute.status == AttributeStatusEnum.current,
            )
            .order_by(CandidateAttribute.created_at.desc())
        )
        return list(self.session.scalars(statement).all())

    # Alias for ergonomics
    get_current_by_candidate = get_current_for_candidate

    def get_current_by_key(
        self, candidate_id: UUID | str, key: str
    ) -> CandidateAttribute | None:
        """Fetch the current fact for a specific attribute key."""
        statement = select(CandidateAttribute).where(
            CandidateAttribute.candidate_id == candidate_id,
            CandidateAttribute.key == key,
            CandidateAttribute.status == AttributeStatusEnum.current,
        )
        return self.session.scalar(statement)

    def get_all_for_candidate(
        self, candidate_id: UUID | str
    ) -> list[CandidateAttribute]:
        """Fetch complete attribute history for a candidate (including superseded)."""
        statement = (
            select(CandidateAttribute)
            .where(CandidateAttribute.candidate_id == candidate_id)
            .order_by(CandidateAttribute.created_at.desc())
        )
        return list(self.session.scalars(statement).all())

    def add(self, attribute: CandidateAttribute) -> CandidateAttribute:
        """Stage a new attribute entry."""
        self.session.add(attribute)
        self.session.flush()
        return attribute

    def supersede(
        self, old_attribute_id: UUID | str, new_attribute: CandidateAttribute
    ) -> CandidateAttribute:
        """
        Safely supersede an existing attribute with a new one:
        1. Mark the old attribute as superseded (releasing the partial unique index).
        2. Insert the new attribute with status=current.
        3. Link the old attribute's superseded_by_id to the new attribute.
        """
        # Step 1: Release status='current' constraint
        self.session.execute(
            update(CandidateAttribute)
            .where(CandidateAttribute.id == old_attribute_id)
            .values(status=AttributeStatusEnum.superseded)
        )
        self.session.flush()

        # Step 2: Add new current attribute
        self.session.add(new_attribute)
        self.session.flush()

        # Step 3: Link audit chain
        self.session.execute(
            update(CandidateAttribute)
            .where(CandidateAttribute.id == old_attribute_id)
            .values(superseded_by_id=new_attribute.id)
        )
        self.session.flush()
        return new_attribute

    def mark_all_current_as_stale(self, candidate_id: UUID | str) -> int:
        """
        Mark all currently active facts as stale (e.g. when opening in refresh mode).
        Never deletes facts, preserving complete history.
        """
        statement = (
            update(CandidateAttribute)
            .where(
                CandidateAttribute.candidate_id == candidate_id,
                CandidateAttribute.status == AttributeStatusEnum.current,
            )
            .values(status=AttributeStatusEnum.stale)
        )
        result = self.session.execute(statement)
        self.session.flush()
        return result.rowcount or 0
