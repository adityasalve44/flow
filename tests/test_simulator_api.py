"""
tests/test_simulator_api.py — Simulator inspection and frontend static asset tests.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.recruiter import get_uow
from app.database import get_db
from app.db.uow import UnitOfWork
from app.main import app
from app.models.attribute import CandidateAttribute
from app.models.candidate import CandidateProfile
from app.models.enums import (
    AttributeStatusEnum,
    ConfidenceEnum,
    ConsentStatusEnum,
    DataClassEnum,
    LifecycleStatusEnum,
    SourceEnum,
)


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_uow] = lambda: UnitOfWork(session=db)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_frontend_routes_served(client):
    """Root and simulator routes serve the interactive frontend HTML."""
    res_root = client.get("/")
    assert res_root.status_code == 200
    assert "text/html" in res_root.headers["content-type"]
    assert "Channel Simulator" in res_root.text
    assert "Agent Brain & Data Inspector" in res_root.text

    res_sim = client.get("/simulator")
    assert res_sim.status_code == 200
    assert "text/html" in res_sim.headers["content-type"]


def test_static_assets_served(client):
    """CSS and JS static assets are served cleanly."""
    res_css = client.get("/static/styles.css")
    assert res_css.status_code == 200
    assert "text/css" in res_css.headers["content-type"]

    res_js = client.get("/static/app.js")
    assert res_js.status_code == 200


def test_simulator_reset_and_inspect(client, db):
    """Simulator reset endpoint initializes a candidate, and inspect returns all layers."""
    phone = f"+9198{uuid4().int % 100000000:08d}"

    # 1. Reset / Create candidate
    res_reset = client.post(
        "/api/simulator/reset",
        json={"phone_number": phone, "display_name": "Test Tester"},
    )
    assert res_reset.status_code == 200
    data_reset = res_reset.json()
    cand_id = data_reset["candidate_id"]
    assert data_reset["phone_number"] == phone
    assert data_reset["lifecycle_status"] == LifecycleStatusEnum.new.value
    assert data_reset["consent_status"] == ConsentStatusEnum.pending.value

    # 2. Add some attributes and profile facts to the candidate
    uow = UnitOfWork(session=db)
    with uow:
        prof = CandidateProfile(
            candidate_id=cand_id,
            full_name="Test Tester",
            current_role="Staff Engineer",
            experience_years=8.0,
            completeness=0.5,
        )
        uow.profiles.add(prof)

        attr = CandidateAttribute(
            candidate_id=cand_id,
            key="skills",
            value=["python", "fastapi"],
            raw_text="I know python and fastapi",
            source=SourceEnum.candidate_stated,
            confidence=ConfidenceEnum.confirmed,
            status=AttributeStatusEnum.current,
            data_class=DataClassEnum.operational,
        )
        uow.attributes.add(attr)
        uow.commit()

    # 3. Inspect candidate
    res_insp = client.get(f"/api/simulator/inspect/{cand_id}")
    assert res_insp.status_code == 200
    insp = res_insp.json()

    assert insp["candidate"]["id"] == cand_id
    assert insp["profile"]["full_name"] == "Test Tester"
    assert insp["profile"]["current_role"] == "Staff Engineer"
    assert insp["profile"]["experience_years"] == 8.0
    assert len(insp["attributes"]) >= 1
    assert insp["attributes"][0]["key"] == "skills"


def test_simulator_candidates_and_backlog(client, db):
    """Candidates list and simulator backlog endpoints return valid collections."""
    res_list = client.get("/api/simulator/candidates")
    assert res_list.status_code == 200
    assert isinstance(res_list.json(), list)

    res_backlog = client.get("/api/simulator/backlog")
    assert res_backlog.status_code == 200
    assert "items" in res_backlog.json()
    assert "total" in res_backlog.json()
