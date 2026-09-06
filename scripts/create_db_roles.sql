-- scripts/create_db_roles.sql — Least-privilege PostgreSQL role setup (FLOW-044).
--
-- Run this as a database superuser ONCE against the production database.
-- The 'flow' schema must already exist (run migrations first).
--
-- Usage:
--   psql -U postgres -d flow -f scripts/create_db_roles.sql
--
-- Roles created:
--   flow_app      — used by the FastAPI application server (DML only, no DDL)
--   flow_migrate  — used by Alembic migration runs only (DDL + DML)
--   flow_readonly — used by analytics / business intelligence queries (SELECT only)
--   flow_retention — used by the retention sweep cron job (DELETE + SELECT on specific tables)

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. flow_app — application server role
-- ─────────────────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'flow_app') THEN
        CREATE ROLE flow_app LOGIN PASSWORD 'REPLACE_WITH_STRONG_PASSWORD';
    END IF;
END$$;

-- Grant connection to the database
GRANT CONNECT ON DATABASE flow TO flow_app;

-- Grant schema usage
GRANT USAGE ON SCHEMA flow TO flow_app;

-- DML on all current tables
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA flow TO flow_app;

-- DML on future tables (for migrations)
ALTER DEFAULT PRIVILEGES IN SCHEMA flow
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO flow_app;

-- Allow use of sequences (needed for UUID generation via gen_random_uuid and any sequences)
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA flow TO flow_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA flow
    GRANT USAGE, SELECT ON SEQUENCES TO flow_app;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. flow_migrate — Alembic DDL role (only used during migration runs)
-- ─────────────────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'flow_migrate') THEN
        CREATE ROLE flow_migrate LOGIN PASSWORD 'REPLACE_WITH_STRONG_PASSWORD';
    END IF;
END$$;

GRANT CONNECT ON DATABASE flow TO flow_migrate;

-- Full DDL and DML on the flow schema
GRANT CREATE, USAGE ON SCHEMA flow TO flow_migrate;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA flow TO flow_migrate;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA flow TO flow_migrate;

-- Needed to create enums (CREATE TYPE)
GRANT flow_migrate TO pgsql;  -- adjust to your superuser if different

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. flow_readonly — BI / analytics read-only role
-- ─────────────────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'flow_readonly') THEN
        CREATE ROLE flow_readonly LOGIN PASSWORD 'REPLACE_WITH_STRONG_PASSWORD';
    END IF;
END$$;

GRANT CONNECT ON DATABASE flow TO flow_readonly;
GRANT USAGE ON SCHEMA flow TO flow_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA flow TO flow_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA flow GRANT SELECT ON TABLES TO flow_readonly;

-- IMPORTANT: flow_readonly must NEVER have access to these sensitive columns.
-- Once column-level privileges are mature, revoke the following explicitly:
--   REVOKE SELECT (phone_number) ON flow.candidates FROM flow_readonly;
--   REVOKE SELECT (body) ON flow.messages FROM flow_readonly;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. flow_retention — Retention sweep cron job role
-- ─────────────────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'flow_retention') THEN
        CREATE ROLE flow_retention LOGIN PASSWORD 'REPLACE_WITH_STRONG_PASSWORD';
    END IF;
END$$;

GRANT CONNECT ON DATABASE flow TO flow_retention;
GRANT USAGE ON SCHEMA flow TO flow_retention;

-- Needs SELECT to identify expired rows, DELETE to prune them, INSERT for audit events
GRANT SELECT, DELETE ON flow.candidate_attributes TO flow_retention;
GRANT SELECT, DELETE ON flow.conversations TO flow_retention;
GRANT SELECT, DELETE ON flow.messages TO flow_retention;
GRANT SELECT, DELETE ON flow.resumes TO flow_retention;
GRANT SELECT, INSERT ON flow.audit_events TO flow_retention;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA flow TO flow_retention;
