"""
tests/test_migrations.py — Alembic migration tests.

Tests that:
- `alembic upgrade head` from empty builds the full schema.
- `alembic downgrade base` cleanly removes tables and enums.
- Migration round trip (upgrade -> downgrade -> upgrade) completes with no errors.
- `alembic revision --autogenerate` produces no operations when DB is up-to-date.
- ADK tables in the `adk` schema are completely ignored by autogenerate.
"""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from app.models import Base
from tests.conftest import _get_test_db_url


@pytest.fixture
def alembic_config():
    """Build an Alembic Config pointed at the test database."""
    db_url = _get_test_db_url()
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_migration_round_trip(alembic_config):
    """Upgrade from empty, downgrade to base, and upgrade again."""
    db_url = _get_test_db_url()
    engine = create_engine(db_url)

    # Empty the test database completely
    with engine.begin() as conn:
        conn.execute(
            text(
                "DROP SCHEMA IF EXISTS flow CASCADE; "
                "DROP SCHEMA IF EXISTS adk CASCADE; "
                "DROP SCHEMA public CASCADE; "
                "CREATE SCHEMA public;"
            )
        )

    # 1. Upgrade from empty to head
    command.upgrade(alembic_config, "head")

    # Verify tables exist in flow schema
    with engine.connect() as conn:
        tables = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'flow' ORDER BY table_name;"
            )
        ).fetchall()
        table_names = {t[0] for t in tables}
        expected_tables = {
            "alembic_version",
            "candidates",
            "candidate_profiles",
            "candidate_attributes",
            "candidate_skills",
            "candidate_role_prefs",
            "candidate_location_prefs",
            "conversations",
            "messages",
            "moderation_events",
            "resumes",
            "audit_events",
        }
        assert expected_tables.issubset(table_names), f"Missing tables: {expected_tables - table_names}"

    # 2. Downgrade to base
    command.downgrade(alembic_config, "base")

    with engine.connect() as conn:
        domain_tables = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'flow' AND table_name != 'alembic_version';"
            )
        ).fetchall()
        assert len(domain_tables) == 0, f"Expected 0 domain tables after downgrade, got: {domain_tables}"

    # 3. Upgrade back to head
    command.upgrade(alembic_config, "head")

    with engine.connect() as conn:
        tables_recreated = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'flow' ORDER BY table_name;"
            )
        ).fetchall()
        table_names_recreated = {t[0] for t in tables_recreated}
        assert expected_tables.issubset(table_names_recreated)


def test_autogenerate_produces_no_diff(alembic_config):
    """Autogenerate against an upgraded database must emit no diff."""
    from migrations.env import include_object

    db_url = _get_test_db_url()
    engine = create_engine(db_url)

    with engine.connect() as conn:
        mc = MigrationContext.configure(
            connection=conn,
            opts={
                "target_metadata": Base.metadata,
                "compare_type": True,
                "compare_server_default": True,
                "version_table_schema": "flow",
                "include_schemas": True,
                "include_object": include_object,
            },
        )
        diff = compare_metadata(mc, Base.metadata)
        assert diff == [], f"Autogenerate emitted unexpected diff: {diff}"


def test_adk_tables_are_ignored_by_autogenerate(alembic_config):
    """ADK tables in the adk schema must be completely ignored by autogenerate."""
    from migrations.env import include_object

    db_url = _get_test_db_url()
    engine = create_engine(db_url)

    adk_tables = ["sessions", "events", "app_states", "user_states", "adk_internal_metadata"]

    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS adk;"))
        for t in adk_tables:
            conn.execute(text(f"CREATE TABLE IF NOT EXISTS adk.{t} (id text primary key);"))

    try:
        with engine.connect() as conn:
            mc = MigrationContext.configure(
                connection=conn,
                opts={
                    "target_metadata": Base.metadata,
                    "compare_type": True,
                    "compare_server_default": True,
                    "version_table_schema": "flow",
                    "include_schemas": True,
                    "include_object": include_object,
                },
            )
            diff = compare_metadata(mc, Base.metadata)
            assert diff == [], f"Autogenerate detected ADK tables or emitted diff: {diff}"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS adk CASCADE; CREATE SCHEMA adk;"))
