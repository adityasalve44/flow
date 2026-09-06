"""
app/models/rate_limit.py — RateLimitHit model for Postgres-backed sliding-window rate limiting (FLOW-042).

Enables distributed, persistent per-phone and per-IP rate limiting without Redis dependencies.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RateLimitHit(Base):
    """Immutable record of an inbound request for rate limit accounting."""

    __tablename__ = "rate_limit_hits"
    __table_args__ = (
        Index("ix_flow_rate_limit_key_created_at", "key", "created_at"),
        {"schema": "flow"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<RateLimitHit key={self.key} created_at={self.created_at}>"
