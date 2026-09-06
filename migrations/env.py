import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# Make the Flow project root importable.
sys.path.append(str(Path(__file__).resolve().parents[1]))

# Load environment variables from .env
load_dotenv()

from app.models import Base

# Alembic Config object
try:
    config = context.config
except AttributeError:
    config = None

if config is not None:
    url_from_config = config.get_main_option("sqlalchemy.url")
    if url_from_config and not url_from_config.startswith("driver://"):
        database_url = url_from_config
    else:
        database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")
    config.set_main_option("sqlalchemy.url", database_url)

    # Configure logging
    if config.config_file_name is not None:
        fileConfig(config.config_file_name)

target_metadata = Base.metadata

ADK_TABLES = {"sessions", "events", "app_states", "user_states", "adk_internal_metadata"}


def include_object(object, name, type_, reflected, compare_to):
    """Exclude ADK tables and schema from Alembic migrations."""
    if type_ == "table":
        # Ignore any table in the adk schema
        if getattr(object, "schema", None) == "adk":
            return False
        # Ignore the five ADK tables by name regardless of schema
        if name in ADK_TABLES:
            return False
        # Only manage tables in the flow schema
        if getattr(object, "schema", None) not in ("flow", None):
            return False
    elif type_ == "schema":
        if name in ("adk", "public"):
            return False
    return True


def run_migrations_offline() -> None:
    """Run migrations in offline mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        version_table_schema="flow",
        include_schemas=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in online mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        from sqlalchemy import text

        connection.execute(
            text("CREATE SCHEMA IF NOT EXISTS flow; CREATE SCHEMA IF NOT EXISTS adk;")
        )
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            version_table_schema="flow",
            include_schemas=True,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if config is not None:
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()