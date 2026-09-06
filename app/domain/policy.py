"""
app/domain/policy.py — deterministic policy engine and next-question scoring (FLOW-019).

Core responsibilities (§8, FLOW-019 of REVIEW_AND_PLAN.md):
1. Validate & normalise each extracted fact (money, notice, experience, location).
2. Merge into attribute store using MergeEngine with 7x7 precedence & provenance.
3. Persist mutations within UnitOfWork.
4. Rebuild projection snapshot (ProfileSnapshot) and update candidate profile.
5. Update deflection and abuse counters.
6. Score missing fields: importance x missingness x recency_penalty x refusal_penalty.
7. Evaluate the 13-rung priority ladder to select exactly ONE directive.
8. Zero model calls — 100% deterministic, testable, and reproducible.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.agents.schemas import ExtractionConfidenceEnum, TurnExtraction
from app.db.uow import UnitOfWork
from app.domain.completeness import is_profile_ready
from app.domain.merge import Fact, MergeContext, merge_facts
from app.domain.normalize import (
    normalize_experience,
    normalize_location,
    normalize_money,
    normalize_notice_period,
)
from app.domain.projection import ProfileSnapshot, rebuild_projection
from app.domain.registry import BLOCKING_KEYS, REGISTRY
from app.models import Candidate, CandidateAttribute, CandidateProfile, Conversation
from app.models.enums import (
    AttributeStatusEnum,
    ConfidenceEnum,
    ConsentStatusEnum,
    ConversationModeEnum,
    ConversationStatusEnum,
    DataClassEnum,
    LifecycleStatusEnum,
    SourceEnum,
)

# Adjacency map for asking up to 2 related fields in one turn (§8)
ADJACENCY_MAP: dict[str, set[str]] = {
    "desired_role": {"skills", "current_role"},
    "skills": {"desired_role"},
    "expected_ctc": {"current_ctc"},
    "current_ctc": {"expected_ctc"},
    "location_preference": {"work_mode"},
    "work_mode": {"location_preference"},
    "notice_period": {"expected_ctc"},
}


@dataclass
class PolicyDirective:
    """The chosen directive output by the policy ladder."""

    name: str
    fields_to_ask: list[str] = field(default_factory=list)
    ambiguous_fact: dict[str, Any] | None = None
    conflicted_fact: dict[str, Any] | None = None
    question_topic: str | None = None
    reason: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_fact_value(key: str, raw_value: Any) -> tuple[Any, str | None]:
    """
    Normalise raw extracted fact values. Returns (normalized_value, ambiguity_reason).
    """
    text_val = str(raw_value) if raw_value is not None else ""
    if key in ("expected_ctc", "current_ctc"):
        norm_money = normalize_money(text_val)
        if norm_money:
            amb = "hedged amount" if norm_money.confidence == "ambiguous" else None
            return {
                "amount": float(norm_money.amount),
                "currency": norm_money.currency,
                "period": norm_money.period,
            }, amb
        return raw_value, None

    if key == "notice_period":
        norm_notice = normalize_notice_period(text_val)
        if norm_notice:
            amb = "hedged notice period" if norm_notice.confidence == "ambiguous" else None
            return {"days": norm_notice.days}, amb
        return raw_value, None

    if key == "experience_years":
        norm_exp = normalize_experience(text_val)
        if norm_exp:
            amb = "hedged experience" if norm_exp.confidence == "ambiguous" else None
            return {"amount": norm_exp.years}, amb
        return raw_value, None

    if key == "location_preference":
        items = raw_value if isinstance(raw_value, list) else [str(raw_value)]
        canonical_locs: list[str] = []
        amb = None
        for item in items:
            norm_loc = normalize_location(str(item))
            if norm_loc:
                canonical_locs.append(norm_loc.canonical)
                if norm_loc.confidence == "ambiguous":
                    amb = "hedged location"
            else:
                canonical_locs.append(str(item))
        return canonical_locs, amb

    return raw_value, None


def score_missing_fields(
    current_facts: list[Fact],
    recently_asked: list[str] | None = None,
    declined_keys: set[str] | None = None,
    ask_counts: dict[str, int] | None = None,
) -> list[str]:
    """
    Compute next-question scores across all registry keys:
      score(field) = importance * missingness * recency_penalty * refusal_penalty

    Invariants (§8, FLOW-025):
    - Hard cap of 2 asks per message.
    - Hard cap of 3 lifetime asks per field on the conversation.
    - Only unconfirmed blocking fields are solicited via ask_next.
    - A second field is only added if topically adjacent to the top-scoring ask.
    """
    recent = set(recently_asked or [])
    declined = set(declined_keys or [])
    counts = ask_counts or {}

    # Determine status of each key
    confirmed_keys = {
        f.key
        for f in current_facts
        if f.status == AttributeStatusEnum.current.value
        and f.confidence == ConfidenceEnum.confirmed.value
    }
    ambiguous_or_stale_keys = {
        f.key
        for f in current_facts
        if f.status == AttributeStatusEnum.stale.value
        or f.confidence == ConfidenceEnum.ambiguous.value
    }

    scores: list[tuple[str, float]] = []

    for key, spec in REGISTRY.items():
        # Hard lifetime cap: maximum 3 asks per field per conversation
        if counts.get(key, 0) >= 3:
            continue

        importance = spec.importance

        # Missingness
        if key in confirmed_keys:
            missingness = 0.0
        elif key in ambiguous_or_stale_keys:
            missingness = 0.5
        else:
            missingness = 1.0

        if missingness <= 0.0:
            continue

        # Recency penalty: suppresses fields asked in the last two turns
        recency_penalty = 0.05 if key in recent else 1.0

        # Refusal penalty: heavily suppresses fields the candidate declined
        refusal_penalty = 0.01 if key in declined else 1.0

        final_score = importance * missingness * recency_penalty * refusal_penalty
        scores.append((key, final_score))

    # Primary ask must be a missing blocking field (solicitation rule)
    blocking_candidates = [
        (k, s) for k, s in scores if k in BLOCKING_KEYS and s > 0.0
    ]
    blocking_candidates.sort(key=lambda item: item[1], reverse=True)

    if not blocking_candidates:
        return []

    top_key = blocking_candidates[0][0]
    result = [top_key]

    # Check for adjacent second key (hard cap of 2 asks per message)
    # Only blocking fields can be solicited; never solicit declined or recent fields
    adjacent_allowed = ADJACENCY_MAP.get(top_key, set())
    remaining_scores = sorted(
        [
            item
            for item in scores
            if item[0] != top_key
            and item[0] in BLOCKING_KEYS
            and item[0] not in declined
            and item[0] not in recent
            and item[1] > 0.1
        ],
        key=lambda item: item[1],
        reverse=True,
    )
    for candidate_key, score in remaining_scores:
        if candidate_key in adjacent_allowed:
            result.append(candidate_key)
            break

    return result


def evaluate_policy_step(
    uow: UnitOfWork,
    candidate: Candidate,
    conversation: Conversation,
    extraction: TurnExtraction,
    recently_asked: list[str] | None = None,
    declined_keys: set[str] | None = None,
    channel_message_id: str | None = None,
    now: datetime | None = None,
) -> tuple[PolicyDirective, ProfileSnapshot]:
    """
    Execute the full deterministic policy evaluation and ladder.

    Steps:
    1. Validate and normalise extracted facts.
    2. Merge into candidate_attributes via MergeEngine.
    3. Update candidate projection (candidate_profiles).
    4. Update conversation counters (deflections, abuse).
    5. Evaluate ladder to choose ONE directive.
    6. Commit changes.
    """
    current_time = now or datetime.now(timezone.utc)

    # Fetch all existing facts for candidate
    db_attrs = uow.attributes.get_all_for_candidate(candidate.id)
    current_facts = [
        Fact(
            id=a.id,
            candidate_id=a.candidate_id,
            key=a.key,
            value=a.value,
            raw_text=a.raw_text or "",
            source=a.source.value if hasattr(a.source, "value") else str(a.source),
            confidence=a.confidence.value if hasattr(a.confidence, "value") else str(a.confidence),
            status=a.status.value if hasattr(a.status, "value") else str(a.status),
            data_class=a.data_class.value if hasattr(a.data_class, "value") else str(a.data_class),
            conversation_id=a.conversation_id,
            created_at=a.created_at,
        )
        for a in db_attrs
    ]

    ambiguous_facts: list[dict[str, Any]] = []
    conflicted_facts: list[dict[str, Any]] = []

    # 1 & 2. Process each extracted fact
    for ext_fact in extraction.facts:
        key = ext_fact.key
        norm_val, amb_reason = normalize_fact_value(key, ext_fact.value)
        ambiguity_reason = ext_fact.ambiguity_reason or amb_reason

        # Register lookup for data class
        spec = REGISTRY.get(key)
        data_class = spec.data_class.value if spec else DataClassEnum.personal.value

        conf_enum = ext_fact.confidence.to_confidence_enum()
        source_val = SourceEnum.candidate_stated.value
        if ext_fact.confidence == ExtractionConfidenceEnum.inferred:
            source_val = SourceEnum.llm_inferred.value

        new_fact = Fact(
            id=uuid4(),
            candidate_id=candidate.id,
            key=key,
            value=norm_val,
            raw_text=ext_fact.raw_text,
            source=source_val,
            confidence=conf_enum.value,
            status=AttributeStatusEnum.current.value,
            data_class=data_class,
            conversation_id=conversation.id,
            created_at=current_time,
        )

        merge_result = merge_facts(
            existing=current_facts,
            incoming=[new_fact],
            context=MergeContext(conversation_id=conversation.id),
        )

        if new_fact in merge_result.conflicted:
            db_attr = CandidateAttribute(
                id=new_fact.id,
                candidate_id=candidate.id,
                key=new_fact.key,
                value=new_fact.value,
                raw_text=new_fact.raw_text,
                source=SourceEnum.candidate_stated,
                confidence=ConfidenceEnum.unknown,
                status=AttributeStatusEnum.conflicted,
                data_class=DataClassEnum(new_fact.data_class),
                conversation_id=conversation.id,
                message_id=None,
                created_at=current_time,
            )
            uow.attributes.add(db_attr)
            conflicted_facts.append({"key": key, "value": norm_val, "reason": "Conflict with authoritative record"})

        elif new_fact in merge_result.ambiguous:
            db_attr = CandidateAttribute(
                id=new_fact.id,
                candidate_id=candidate.id,
                key=new_fact.key,
                value=new_fact.value,
                raw_text=new_fact.raw_text,
                source=SourceEnum.candidate_stated,
                confidence=ConfidenceEnum.ambiguous,
                status=AttributeStatusEnum.current,
                data_class=DataClassEnum(new_fact.data_class),
                conversation_id=conversation.id,
                message_id=None,
                created_at=current_time,
            )
            uow.attributes.add(db_attr)
            ambiguous_facts.append({"key": key, "value": norm_val, "reason": ambiguity_reason or "Ambiguous"})

        elif new_fact in merge_result.accepted:
            db_attr = CandidateAttribute(
                id=new_fact.id,
                candidate_id=candidate.id,
                key=new_fact.key,
                value=new_fact.value,
                raw_text=new_fact.raw_text,
                source=SourceEnum.candidate_stated,
                confidence=ConfidenceEnum.confirmed,
                status=AttributeStatusEnum.current,
                data_class=DataClassEnum(new_fact.data_class),
                conversation_id=conversation.id,
                message_id=None,
                created_at=current_time,
            )
            if merge_result.superseded:
                for sup in merge_result.superseded:
                    uow.attributes.supersede(
                        old_attribute_id=sup.id,
                        new_attribute=db_attr,
                    )
                    for f in current_facts:
                        if f.id == sup.id:
                            object.__setattr__(f, "status", AttributeStatusEnum.superseded.value)
            else:
                uow.attributes.add(db_attr)

            current_facts.append(new_fact)

    # 3. Rebuild projection
    snapshot = rebuild_projection(current_facts)

    # Update candidate_profiles in DB
    profile = uow.profiles.get_by_candidate_id(candidate.id)
    if profile is None:
        profile = CandidateProfile(candidate_id=candidate.id)
        uow.profiles.add(profile)

    profile.current_role = snapshot.current_role
    profile.current_company = snapshot.current_company
    profile.experience_years = snapshot.experience_years
    profile.current_ctc_annual = snapshot.current_ctc_annual
    profile.expected_ctc_annual = snapshot.expected_ctc_annual
    profile.currency = snapshot.currency
    profile.notice_period_days = snapshot.notice_period_days
    profile.work_mode = snapshot.work_mode
    profile.education_level = snapshot.education_level
    profile.completeness = snapshot.completeness
    profile.last_refreshed_at = current_time

    # Lifecycle transition to profile_ready if all 6 blocking fields confirmed
    ready = is_profile_ready(current_facts)
    if ready and candidate.lifecycle_status in (LifecycleStatusEnum.new, LifecycleStatusEnum.intake):
        candidate.lifecycle_status = LifecycleStatusEnum.profile_ready

    # 4. Update counters
    if extraction.refusal_signal:
        conversation.deflection_count += 1
    if extraction.abuse_signal:
        conversation.abuse_count += 1

    # 5. Evaluate the 13-Rung Ladder (§8)
    chosen_directive: PolicyDirective

    # Conversation ask counters
    ask_counts = getattr(conversation, "ask_counts", None)
    if ask_counts is None:
        ask_counts = {}
        conversation.ask_counts = ask_counts

    # Rung 1: disengage_silent (deflection_count >= 3)
    if conversation.deflection_count >= 3:
        conversation.status = ConversationStatusEnum.closed
        conversation.closed_at = current_time
        chosen_directive = PolicyDirective(
            name="disengage_silent",
            reason="Candidate disengaged after 3 deflections",
        )

    # Rung 2: warn_abuse (Abuse signal)
    elif extraction.abuse_signal or conversation.abuse_count > 0:
        chosen_directive = PolicyDirective(
            name="warn_abuse",
            reason="Abuse detected in turn",
        )

    # Rung 3: close_consent_declined
    elif candidate.consent_status in (ConsentStatusEnum.declined, ConsentStatusEnum.withdrawn):
        chosen_directive = PolicyDirective(
            name="close_consent_declined",
            reason="Consent declined or withdrawn",
        )

    # Rung 4: ask_consent (pending consent)
    elif candidate.consent_status == ConsentStatusEnum.pending:
        chosen_directive = PolicyDirective(
            name="ask_consent",
            reason="Consent pending",
        )

    # Rung 5: offer_call (deflection_count == 2)
    elif conversation.deflection_count == 2:
        chosen_directive = PolicyDirective(
            name="offer_call",
            reason="Two deflections reached; offering human recruiter call",
        )

    # Rung 6: answer_and_continue (candidate asked question related to pending ask)
    elif any(q.is_related_to_pending for q in extraction.questions):
        rel_q = next(q for q in extraction.questions if q.is_related_to_pending)
        missing = score_missing_fields(
            current_facts, recently_asked, declined_keys, ask_counts=ask_counts
        )
        chosen_directive = PolicyDirective(
            name="answer_and_continue",
            question_topic=rel_q.topic,
            fields_to_ask=missing[:1],
            reason="Question relates to pending topic",
        )

    # Rung 7: confirm_ambiguity (ambiguous fact this turn)
    elif ambiguous_facts:
        chosen_directive = PolicyDirective(
            name="confirm_ambiguity",
            ambiguous_fact=ambiguous_facts[0],
            reason="Fact landed ambiguous",
        )

    # Rung 8: resolve_conflict (conflicted fact this turn)
    elif conflicted_facts:
        chosen_directive = PolicyDirective(
            name="resolve_conflict",
            conflicted_fact=conflicted_facts[0],
            reason="Fact contradicted existing authoritative record",
        )

    # Rung 9: redirect (off-topic question, no facts supplied)
    elif extraction.questions and not extraction.facts:
        chosen_directive = PolicyDirective(
            name="redirect",
            question_topic=extraction.questions[0].topic,
            fields_to_ask=score_missing_fields(
                current_facts, recently_asked, declined_keys, ask_counts=ask_counts
            )[:1],
            reason="Off-topic question without answers",
        )

    # Rung 10: clarify_name (contact name conflicts with profile name)
    elif (
        candidate.display_name
        and profile.full_name
        and candidate.display_name.strip().lower() != profile.full_name.strip().lower()
    ):
        chosen_directive = PolicyDirective(
            name="clarify_name",
            reason="Candidate WhatsApp contact name conflicts with full_name",
        )

    # Check missing fields for rungs 11, 12, 13
    else:
        missing_fields = score_missing_fields(
            current_facts, recently_asked, declined_keys, ask_counts=ask_counts
        )

        # Rung 11: ask_resume (profile ready or substantially complete, no current resume)
        if ready and not any(f.key == "resume" for f in current_facts):
            chosen_directive = PolicyDirective(
                name="ask_resume",
                reason="Profile substantially complete; request resume",
            )
        # Rung 13: acknowledge_profile_ready (all 6 baseline fields confirmed)
        elif ready:
            chosen_directive = PolicyDirective(
                name="acknowledge_profile_ready",
                reason="All six baseline fields confirmed; profile ready",
            )
        # Rung 12: ask_next (default top 1-2 scored fields)
        else:
            chosen_directive = PolicyDirective(
                name="ask_next",
                fields_to_ask=missing_fields,
                reason="Top scored missing fields",
            )

    # Record ask counts on the conversation
    if chosen_directive.fields_to_ask:
        for f in chosen_directive.fields_to_ask:
            conversation.ask_counts[f] = conversation.ask_counts.get(f, 0) + 1

    # Commit the transaction
    uow.commit()

    return chosen_directive, snapshot
