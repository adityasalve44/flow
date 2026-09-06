# Flow

AI-powered WhatsApp recruitment intake assistant built on Google ADK + Gemini.

Flow receives inbound WhatsApp messages via the Meta Cloud API, extracts candidate information using a multi-agent ADK pipeline (extractor → policy → replier), and surfaces structured profiles to recruiters via a REST API.

## Quick start

```bash
# 1. Copy and fill in secrets
cp .env.example .env

# 2. Start Postgres
make up

# 3. Apply migrations
make migrate

# 4. Run tests
make test

# 5. Start the dev server
make run
```

Then open **http://localhost:8000/simulator** to interact with the agent.

## Requirements

- Python 3.14+
- PostgreSQL 16+
- Google API key (Gemini)

## Architecture

See [docs/REVIEW_AND_PLAN.md](docs/REVIEW_AND_PLAN.md) for the full architecture review and build plan.

Key invariants:
- Every inbound turn is one atomic database transaction.
- No candidate PII ever appears in logs (enforced by structured log redaction).
- Three-class data model (Q5): `operational` · `personal` · `protected` — each with its own retention window and access level.
- Explicit consent required before any persistent candidate data storage (Q4).

## Development commands

| Command | Description |
|---|---|
| `make up` | Start Postgres via Docker Compose |
| `make down` | Stop Postgres |
| `make migrate` | Apply all Alembic migrations |
| `make revision msg="..."` | Generate a new migration |
| `make test` | Run the full test suite |
| `make test-fast` | Run tests excluding live-model and integration tests |
| `make lint` | Run ruff (check + format) |
| `make run` | Start the FastAPI dev server |

## Production operations commands

| Command | Description |
|---|---|
| `make retention-dry` | Preview what the retention sweep would prune (no changes) |
| `make retention-sweep` | Execute the data retention sweep |
| `make erase-candidate PHONE="+91..." ACTOR="..." REASON="..."` | Process a GDPR erasure request |
| `make backup BACKUP_DIR=/backups` | Dump the production database |
| `make create-db-roles` | Create least-privilege PostgreSQL roles (requires superuser) |

See [docs/runbook.md](docs/runbook.md) for the full operations runbook, alert definitions, and the backup/restore drill procedure.

## Project layout

```
flow/
├── app/
│   ├── config.py           Settings (pydantic-settings)
│   ├── api/                FastAPI routers, schemas, limits
│   ├── agents/             ADK agent pipeline (extractor → policy → replier)
│   ├── channel/            Ingress normalisation + WhatsApp adapter
│   ├── db/                 Unit-of-work
│   ├── domain/             Pure business logic (merge, policy, normalise, consent)
│   ├── models/             SQLAlchemy ORM (candidates, attributes, resumes, audit...)
│   ├── observability/      OpenTelemetry tracing + cost tracking
│   ├── repositories/       Data access (never commits)
│   ├── services/           Orchestration (TurnService, privacy erasure, resume)
│   ├── storage/            Object storage adapters (local + Supabase)
│   └── tools/              ADK FunctionTools
├── docs/
│   ├── REVIEW_AND_PLAN.md  Architecture review and build plan
│   ├── FINDINGS_AND_DECISIONS.md  Architectural decisions log
│   ├── runbook.md          Operations runbook (alerts, failures, drill)
│   └── tasks.md            Build task tracker
├── migrations/             Alembic
├── scripts/
│   ├── init_db.sql         Docker database initialisation
│   ├── create_db_roles.sql Least-privilege PostgreSQL roles
│   ├── retention_sweep.py  Cron-ready data retention sweep runner
│   └── erase_candidate.py  GDPR/legal erasure request tool
├── tests/                  514 tests, 0 failures
└── compose.yaml
```
