from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_number: Mapped[str] = mapped_column(
        String(30),
        unique=True,
        index=True,
        nullable=False,
    )
    name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    profile: Mapped["CandidateProfile | None"] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
        uselist=False,
    )

    skills: Mapped[list["CandidateSkill"]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
    )

    roles: Mapped[list["CandidateRole"]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
    )

    locations: Mapped[list["CandidateLocation"]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
    )

    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="candidate",
        cascade="all, delete-orphan",
    )


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"),
        primary_key=True,
    )

    current_ctc: Mapped[float | None] = mapped_column(Float)
    expected_ctc: Mapped[float | None] = mapped_column(Float)
    experience_years: Mapped[float | None] = mapped_column(Float)

    candidate: Mapped["Candidate"] = relationship(
        back_populates="profile"
    )


class CandidateSkill(Base):
    __tablename__ = "candidate_skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"),
        index=True,
    )
    skill: Mapped[str] = mapped_column(String(255), index=True)

    candidate: Mapped["Candidate"] = relationship(
        back_populates="skills"
    )


class CandidateRole(Base):
    __tablename__ = "candidate_roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"),
        index=True,
    )
    role: Mapped[str] = mapped_column(String(255), index=True)

    candidate: Mapped["Candidate"] = relationship(
        back_populates="roles"
    )


class CandidateLocation(Base):
    __tablename__ = "candidate_locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"),
        index=True,
    )
    location: Mapped[str] = mapped_column(String(255), index=True)

    candidate: Mapped["Candidate"] = relationship(
        back_populates="locations"
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"),
        index=True,
    )
    direction: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    candidate: Mapped["Candidate"] = relationship(
        back_populates="conversations"
    )