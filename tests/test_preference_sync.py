"""
tests/test_preference_sync.py — sync_preference_tables tests (FLOW-037).

The candidate_skills / candidate_role_prefs / candidate_location_prefs tables
existed since FLOW-010 but were never populated — rebuild_projection() held
skills/desired_roles/locations only as transient ProfileSnapshot fields. This
tests the fix: evaluate_policy_step() now reconciles those tables on every
turn so the recruiter search API (FLOW-037) has something to filter on.

Tests:
- Skills, desired roles and locations extracted in a turn land in their
  respective tables, deduped and normalised.
- A later turn that drops a previously-mentioned skill removes it (these are
  derived, rebuildable projections — not append-only history).
- Duplicate/differently-cased mentions of the same skill dedupe to one row.
- Location strength defaults to "preferred" (documented extractor gap).
"""

from uuid import uuid4

from app.agents.schemas import ExtractedFact, IntentEnum, TurnExtraction
from app.db.uow import UnitOfWork
from app.domain.policy import evaluate_policy_step
from app.models.enums import ConsentStatusEnum
from app.services.conversation import resolve_conversation


def _granted_candidate(uow, db):
    phone = f"+9192{uuid4().int % 100000000:08d}"
    cand = uow.candidates.get_or_create_by_phone(phone)
    cand.consent_status = ConsentStatusEnum.granted
    conv = resolve_conversation(uow, cand)
    return cand, conv


def test_skills_roles_locations_populate_on_first_turn(db):
    """Extracted skills/desired_roles/location_preference land in their tables."""
    uow = UnitOfWork(session=db)
    with uow:
        cand, conv = _granted_candidate(uow, db)

        extraction = TurnExtraction(
            intent=IntentEnum.provide_info,
            facts=[
                ExtractedFact(
                    key="skills", value=["Python", "PostgreSQL"], raw_text="Python, PostgreSQL"
                ),
                ExtractedFact(
                    key="desired_role", value=["Backend Developer"], raw_text="Backend Developer"
                ),
                ExtractedFact(
                    key="location_preference", value=["Pune"], raw_text="Pune"
                ),
            ],
        )
        evaluate_policy_step(uow, cand, conv, extraction)

        skills = {s.skill_norm: s.skill_raw for s in uow.skills.get_by_candidate(cand.id)}
        roles = {r.role_norm: r.role_raw for r in uow.role_prefs.get_by_candidate(cand.id)}
        locs = uow.location_prefs.get_by_candidate(cand.id)

        assert skills == {"python": "Python", "postgresql": "PostgreSQL"}
        assert roles == {"backend_developer": "Backend Developer"}
        assert len(locs) == 1
        assert locs[0].location_norm == "pune"
        assert locs[0].location_raw == "Pune"
        assert locs[0].strength == "preferred"


def test_role_pref_kind_is_desired(db):
    """Synced role preferences are always kind='desired' — sync_preference_tables
    never touches 'current' rows, matching the RolePrefRepository contract."""
    uow = UnitOfWork(session=db)
    with uow:
        cand, conv = _granted_candidate(uow, db)
        extraction = TurnExtraction(
            intent=IntentEnum.provide_info,
            facts=[ExtractedFact(key="desired_role", value=["SRE"], raw_text="SRE")],
        )
        evaluate_policy_step(uow, cand, conv, extraction)

        roles = uow.role_prefs.get_by_candidate(cand.id)
        assert len(roles) == 1
        assert roles[0].kind == "desired"


def test_dropping_a_skill_in_a_later_turn_removes_it(db):
    """These are derived projections, not history: a fact that is no longer
    current (superseded) must disappear from candidate_skills, not just
    accumulate forever."""
    uow = UnitOfWork(session=db)
    with uow:
        cand, conv = _granted_candidate(uow, db)

        evaluate_policy_step(
            uow,
            cand,
            conv,
            TurnExtraction(
                intent=IntentEnum.provide_info,
                facts=[ExtractedFact(key="skills", value=["Java"], raw_text="Java")],
            ),
        )
        assert {s.skill_norm for s in uow.skills.get_by_candidate(cand.id)} == {"java"}

        # Candidate corrects: "Actually it's Python, not Java" -> merge engine
        # supersedes the same-conversation fact; snapshot.skills should now
        # reflect only Python.
        evaluate_policy_step(
            uow,
            cand,
            conv,
            TurnExtraction(
                intent=IntentEnum.correct,
                facts=[ExtractedFact(key="skills", value=["Python"], raw_text="Python")],
            ),
        )

    with uow:
        norms = {s.skill_norm for s in uow.skills.get_by_candidate(cand.id)}
        assert norms == {"python"}, f"expected only 'python' after correction, got {norms}"


def test_differently_cased_duplicate_mentions_dedupe_to_one_row(db):
    """'Python' and 'python' in the same current attribute set collapse to
    one row via the skill_norm dedup key, matching the unique constraint."""
    uow = UnitOfWork(session=db)
    with uow:
        cand, conv = _granted_candidate(uow, db)
        evaluate_policy_step(
            uow,
            cand,
            conv,
            TurnExtraction(
                intent=IntentEnum.provide_info,
                facts=[
                    ExtractedFact(key="skills", value=["Python", "python", "PYTHON "], raw_text="Python")
                ],
            ),
        )
        skills = uow.skills.get_by_candidate(cand.id)
        assert len(skills) == 1
        assert skills[0].skill_norm == "python"


def test_no_preference_facts_leaves_tables_empty(db):
    """A candidate who has never mentioned a skill/role/location has empty
    (not error-raising) preference tables."""
    uow = UnitOfWork(session=db)
    with uow:
        cand, conv = _granted_candidate(uow, db)
        evaluate_policy_step(
            uow,
            cand,
            conv,
            TurnExtraction(
                intent=IntentEnum.provide_info,
                facts=[ExtractedFact(key="experience_years", value=3, raw_text="3 years")],
            ),
        )
        assert uow.skills.get_by_candidate(cand.id) == []
        assert uow.role_prefs.get_by_candidate(cand.id) == []
        assert uow.location_prefs.get_by_candidate(cand.id) == []
