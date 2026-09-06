# Flow

AI-powered WhatsApp recruitment intake assistant.

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

## Requirements

- Python 3.14+
- PostgreSQL 16+
- Google API key (Gemini)

## Architecture

See [docs/REVIEW_AND_PLAN.md](docs/REVIEW_AND_PLAN.md) for the full architecture review and build plan.

## Development commands

| Command | Description |
|---|---|
| `make up` | Start Postgres via Docker Compose |
| `make down` | Stop Postgres |
| `make migrate` | Apply all Alembic migrations |
| `make revision msg="..."` | Generate a new migration |
| `make test` | Run the test suite |
| `make lint` | Run ruff |
| `make run` | Start the FastAPI dev server |

## Project layout

```
flow/
├── app/
│   ├── config.py         Settings (pydantic-settings)
│   ├── api/              FastAPI routers and schemas
│   ├── agents/           ADK agent pipeline
│   ├── channel/          Ingress normalisation
│   ├── db/               Unit-of-work
│   ├── domain/           Pure business logic (merge, policy, normalise)
│   ├── models/           SQLAlchemy ORM
│   ├── repositories/     Data access (never commits)
│   ├── services/         Orchestration, owns the transaction
│   └── tools/            ADK FunctionTools
├── migrations/           Alembic
├── tests/
├── docs/
└── compose.yaml
```
# flow
