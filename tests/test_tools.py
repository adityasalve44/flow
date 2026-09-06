"""
tests/test_tools.py — V1 safe tools tests (FLOW-022).

Tests:
1. Declaration inspection:
   - Generated FunctionDeclaration shows no candidate_id, no phone_number, no tool_context.
2. Candidate snapshot tool:
   - Scoped strictly to state candidate_id.
   - Strictly excludes sensitive attributes (personal, protected, unknown fail-closed).
   - Only operational fields are exposed.
   - DB fallback loads operational profile for candidate_id in state.
3. Recruitment glossary tool:
   - Explains known terms (CTC, notice period, buyout, ESOP, etc.) with 3-5 line authoritative text.
   - Handles case and aliases gracefully.
   - Unknown terms return found=False rather than improvising.
4. Unsafe tool deletion:
   - Verifies app/tools/candidate.py and app/tools/conversation.py do not exist.
"""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from google.adk.tools.function_tool import FunctionTool
import pytest

from app.db.uow import UnitOfWork
from app.models import CandidateProfile
from app.models.enums import ConsentStatusEnum
from app.tools import FLOW_V1_TOOLS
from app.tools.glossary import explain_recruitment_term
from app.tools.snapshot import get_candidate_snapshot


def test_unsafe_legacy_tools_deleted():
    """Verify that model-callable write tools and phone_number-accepting tools are deleted."""
    tools_dir = Path(__file__).resolve().parent.parent / "app" / "tools"
    assert not (tools_dir / "candidate.py").exists(), "app/tools/candidate.py must not exist"
    assert not (tools_dir / "conversation.py").exists(), "app/tools/conversation.py must not exist"


def test_function_declarations_expose_no_identity_parameters():
    """
    Acceptance test (FLOW-022):
    Inspecting generated FunctionDeclarations shows NO candidate_id,
    NO phone_number, and NO tool_context parameter exposed to the LLM.
    """
    # 1. Snapshot tool
    snapshot_tool = FunctionTool(func=get_candidate_snapshot)
    snapshot_decl = snapshot_tool._get_declaration()

    assert snapshot_decl.name == "get_candidate_snapshot"
    schema = snapshot_decl.parameters_json_schema or {}
    props = schema.get("properties", {})

    assert "candidate_id" not in props
    assert "phone_number" not in props
    assert "tool_context" not in props
    # Zero arguments exposed to model for snapshot
    assert len(props) == 0

    # 2. Glossary tool
    glossary_tool = FunctionTool(func=explain_recruitment_term)
    glossary_decl = glossary_tool._get_declaration()

    assert glossary_decl.name == "explain_recruitment_term"
    g_schema = glossary_decl.parameters_json_schema or {}
    g_props = g_schema.get("properties", {})

    assert "candidate_id" not in g_props
    assert "phone_number" not in g_props
    assert "tool_context" not in g_props
    assert "term" in g_props

    # 3. Resume confirmation tool
    from app.tools.resume import record_resume_confirmation
    resume_tool = FunctionTool(func=record_resume_confirmation)
    resume_decl = resume_tool._get_declaration()

    assert resume_decl.name == "record_resume_confirmation"
    r_schema = resume_decl.parameters_json_schema or {}
    r_props = r_schema.get("properties", {})
    assert "candidate_id" not in r_props
    assert "phone_number" not in r_props
    assert "tool_context" not in r_props
    assert len(r_props) == 0


def test_candidate_snapshot_excludes_sensitive_attributes():
    """
    Acceptance test (Q5, FLOW-022):
    Snapshot excludes personal and protected attributes, as well as unknown fail-closed keys.
    Only operational fields are returned to the caller.
    """
    cand_id = str(uuid4())

    mock_state = {
        "candidate_id": cand_id,
        "candidate_snapshot": {
            # Operational (should be preserved)
            "desired_role": "Senior Backend Engineer",
            "experience_years": 6.5,
            "skills": ["Python", "PostgreSQL", "FastAPI"],
            "location_preference": ["Bengaluru", "Remote"],
            "expected_ctc": 3500000.0,
            "notice_period": 30,
            "work_mode": "hybrid",
            "completeness_score": 0.85,
            # Personal (must be EXCLUDED)
            "full_name": "Deepa Sharma",
            "phone_number": "+919876543210",
            "date_of_birth": "1994-08-15",
            "age": 30,
            "marital_status": "Married",
            "family_circumstances": "Relocating with spouse",
            "nationality": "Indian",
            # Protected (must be EXCLUDED)
            "religion": "Hindu",
            "caste": "General",
            "health_disability": "None",
            "sex_gender": "Female",
            # Unknown key (must fail-closed and be EXCLUDED)
            "internal_score_secret": 99,
        },
    }

    mock_context = SimpleNamespace(state=mock_state)
    result = get_candidate_snapshot(mock_context)

    assert result["found"] is True
    assert result["candidate_id"] == cand_id

    snapshot = result["snapshot"]

    # Operational fields must be present
    assert snapshot["desired_role"] == "Senior Backend Engineer"
    assert snapshot["experience_years"] == 6.5
    assert snapshot["skills"] == ["Python", "PostgreSQL", "FastAPI"]
    assert snapshot["location_preference"] == ["Bengaluru", "Remote"]
    assert snapshot["expected_ctc"] == 3500000.0
    assert snapshot["notice_period"] == 30
    assert snapshot["work_mode"] == "hybrid"
    assert snapshot["completeness_score"] == 0.85

    # Personal attributes must be absent
    assert "full_name" not in snapshot
    assert "phone_number" not in snapshot
    assert "date_of_birth" not in snapshot
    assert "age" not in snapshot
    assert "marital_status" not in snapshot
    assert "family_circumstances" not in snapshot
    assert "nationality" not in snapshot

    # Protected attributes must be absent
    assert "religion" not in snapshot
    assert "caste" not in snapshot
    assert "health_disability" not in snapshot
    assert "sex_gender" not in snapshot

    # Unknown fail-closed keys must be absent
    assert "internal_score_secret" not in snapshot


def test_candidate_snapshot_missing_session_context():
    """Returns found=False when no candidate is in session state."""
    mock_context = SimpleNamespace(state={})
    result = get_candidate_snapshot(mock_context)
    assert result["found"] is False
    assert "error" in result


def test_candidate_snapshot_db_fallback(db):
    """Snapshot tool can read operational projection from DB using candidate_id in state."""
    uow = UnitOfWork(session=db)
    cand_phone = f"+9198{uuid4().int % 100000000:08d}"

    with uow:
        cand = uow.candidates.get_or_create_by_phone(cand_phone)
        cand.consent_status = ConsentStatusEnum.granted
        uow.candidates.add(cand)

        profile = CandidateProfile(
            candidate_id=cand.id,
            current_role="DevOps Engineer",
            experience_years=4.0,
            notice_period_days=15,
            completeness=0.75,
        )
        uow.profiles.save(profile)
        uow.commit()

        cand_id_str = str(cand.id)

    # State has candidate_id and uow
    mock_context = SimpleNamespace(state={"candidate_id": cand_id_str}, uow=uow)
    result = get_candidate_snapshot(mock_context)

    assert result["found"] is True
    assert result["candidate_id"] == cand_id_str
    assert result["snapshot"]["current_role"] == "DevOps Engineer"
    assert result["snapshot"]["experience_years"] == 4.0
    assert result["snapshot"]["notice_period"] == 15


def test_explain_recruitment_term_known_terms():
    """Authoritative recruitment terms are correctly retrieved."""
    # CTC
    r1 = explain_recruitment_term("CTC")
    assert r1["found"] is True
    assert "Cost to Company" in r1["explanation"]
    assert len(r1["explanation"].strip().split("\n")) >= 3

    # Notice period alias
    r2 = explain_recruitment_term("notice period")
    assert r2["found"] is True
    assert "resignation" in r2["explanation"].lower()

    # Buyout
    r3 = explain_recruitment_term("buyout")
    assert r3["found"] is True
    assert "compensates" in r3["explanation"].lower()

    # ESOP
    r4 = explain_recruitment_term("ESOP")
    assert r4["found"] is True
    assert "shares" in r4["explanation"].lower()

    # Garden leave
    r5 = explain_recruitment_term("garden leave")
    assert r5["found"] is True
    assert "workplace" in r5["explanation"].lower()


def test_explain_recruitment_term_unknown_term():
    """
    Acceptance test:
    Unknown terms return found=False rather than hallucinating or improvising.
    """
    unknown_terms = [
        "quantum entanglement",
        "supercalifragilistic",
        "crypto tokenomics",
        "mars terraforming",
    ]
    for term in unknown_terms:
        result = explain_recruitment_term(term)
        assert result["found"] is False
        assert "not found" in result["message"].lower()


def test_flow_v1_tools_list():
    """Verify FLOW_V1_TOOLS contains the expected safe tool callables."""
    assert len(FLOW_V1_TOOLS) == 4
    tool_names = {t.__name__ for t in FLOW_V1_TOOLS}
    assert tool_names == {
        "get_candidate_snapshot",
        "explain_recruitment_term",
        "recall_candidate_history",
        "record_resume_confirmation",
    }
