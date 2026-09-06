.PHONY: up down migrate revision test lint run

PYTHON := .venv/bin/python
PIP    := .venv/bin/pip
PYTEST := .venv/bin/pytest
RUFF   := .venv/bin/ruff
ALEMBIC := .venv/bin/alembic
UVICORN := .venv/bin/uvicorn

# ── Infrastructure ─────────────────────────────────────────────────────────

up:
	docker compose up -d
	@echo "Waiting for Postgres to be ready..."
	@until docker compose exec postgres pg_isready -U pgsql -d flow >/dev/null 2>&1; do sleep 1; done
	@echo "Postgres is ready."

down:
	docker compose down

# ── Database ───────────────────────────────────────────────────────────────

migrate:
	$(ALEMBIC) upgrade head

revision:
	@if [ -z "$(msg)" ]; then echo "Usage: make revision msg='your message'"; exit 1; fi
	$(ALEMBIC) revision --autogenerate -m "$(msg)"

# ── Development server ─────────────────────────────────────────────────────

run:
	$(UVICORN) app.main:app --reload --host 0.0.0.0 --port 8000

# ── Quality ────────────────────────────────────────────────────────────────

lint:
	$(RUFF) check .
	$(RUFF) format --check .

test:
	$(PYTEST) -v

test-fast:
	$(PYTEST) -v -m "not live_model and not integration"

# ── Setup ──────────────────────────────────────────────────────────────────

install:
	$(PIP) install -r requirements.txt -r requirements-dev.txt

venv:
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(MAKE) install
