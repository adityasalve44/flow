"""
app/agents/schemas.py — structured extraction and turn payload schemas (FLOW-018).

Core requirements (§8, FLOW-018 of REVIEW_AND_PLAN.md):
- Structured output schema for TurnExtraction.
- Contains only allowed fact keys and attributes.
- Has NO identity fields (phone_number, candidate_id, org_id) — prevents prompt injection
  from ever altering candidate identity.
- Validation errors degrade gracefully to an empty extraction.
"""

import logging
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.models.enums import ConfidenceEnum

logger = logging.getLogger(__name__)


class IntentEnum(str, Enum):
    provide_info = "provide_info"
    ask_question = "ask_question"
    greet = "greet"
    refuse = "refuse"
    correct = "correct"
    confirm = "confirm"
    resume_offer = "resume_offer"
    abuse = "abuse"
    other = "other"


class ResumeIntentEnum(str, Enum):
    none = "none"
    offering = "offering"
    confirming_existing = "confirming_existing"
    declining = "declining"


class ExtractionConfidenceEnum(str, Enum):
    confirmed = "confirmed"
    stated = "stated"
    ambiguous = "ambiguous"
    inferred = "inferred"
    unknown = "unknown"

    def to_confidence_enum(self) -> ConfidenceEnum:
        if self in (ExtractionConfidenceEnum.confirmed, ExtractionConfidenceEnum.stated):
            return ConfidenceEnum.confirmed
        if self == ExtractionConfidenceEnum.ambiguous:
            return ConfidenceEnum.ambiguous
        if self == ExtractionConfidenceEnum.inferred:
            return ConfidenceEnum.inferred
        return ConfidenceEnum.unknown


class ExtractedFact(BaseModel):
    """A single factual claim extracted from candidate utterance."""

    model_config = ConfigDict(extra="ignore")

    key: str
    value: Any
    raw_text: str
    confidence: ExtractionConfidenceEnum = ExtractionConfidenceEnum.confirmed
    ambiguity_reason: str | None = None


class ExtractedCorrection(BaseModel):
    """An explicit correction to a previously stated fact."""

    model_config = ConfigDict(extra="ignore")

    key: str
    old_hint: str | None = None
    new_value: Any


class ExtractedQuestion(BaseModel):
    """A candidate question or inquiry."""

    model_config = ConfigDict(extra="ignore")

    topic: str
    is_related_to_pending: bool = False


class TurnExtraction(BaseModel):
    """The structured representation of one inbound candidate turn."""

    model_config = ConfigDict(extra="ignore")

    intent: IntentEnum = IntentEnum.provide_info
    facts: list[ExtractedFact] = Field(default_factory=list)
    corrections: list[ExtractedCorrection] = Field(default_factory=list)
    questions: list[ExtractedQuestion] = Field(default_factory=list)
    refusal_signal: bool = False
    abuse_signal: bool = False
    name_claim: str | None = None
    resume_intent: ResumeIntentEnum = ResumeIntentEnum.none
    raw_message_text: str | None = None

    @classmethod
    def empty(cls) -> TurnExtraction:
        """Return a safe empty extraction."""
        return cls(intent=IntentEnum.other)

    @classmethod
    def safe_parse(cls, data: Any) -> TurnExtraction:
        """
        Safely parse raw dictionary or model output.
        Degrades gracefully to empty extraction on validation failure.
        """
        if data is None:
            return cls.empty()
        if isinstance(data, cls):
            return data
        try:
            if isinstance(data, dict):
                return cls.model_validate(data)
            if isinstance(data, str):
                return cls.model_validate_json(data)
            return cls.empty()
        except ValidationError as err:
            logger.warning("TurnExtraction validation failed; degrading to empty extraction: %s", err)
            return cls.empty()
        except Exception as ex:
            logger.warning("Unexpected error parsing TurnExtraction: %s", ex)
            return cls.empty()
