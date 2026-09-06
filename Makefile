.PHONY: up down migrate revision test lint run retention-dry retention-sweep erase-candidate create-db-roles

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
	@until docker compose exec postgres pg_isready -U pgsql -d flow > /dev/null 2>&1; do sleep 1; done
	@echo "Postgres is ready."

down:
	docker compose down

# ── Database ───────────────────────────────────────────────────────────────

migrate:
	$(ALEMBIC) upgrade head

revision:
	@if [ -z "$(msg)" ]; then echo "Usage: make revision msg='your message'"; exit 1; fi
	$(ALEMBIC) revision --autogenerate -m "$(msg)"

create-db-roles:
	@echo "Creating least-privilege DB roles (requires superuser)..."
	psql -U postgres -d flow -f scripts/create_db_roles.sql

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

# ── Production Operations (FLOW-044) ──────────────────────────────────────

# Preview what the retention sweep would delete (no changes)
retention-dry:
	$(PYTHON) scripts/retention_sweep.py --dry-run

# Execute the retention sweep (deletes expired rows)
retention-sweep:
	$(PYTHON) scripts/retention_sweep.py

# Erase a specific candidate by phone (for GDPR requests)
# Usage: make erase-candidate PHONE="+91XXXXXXXXXX" ACTOR="legal_team" REASON="GDPR REF-123"
erase-candidate:
	@if [ -z "$(PHONE)" ]; then echo "Usage: make erase-candidate PHONE='+91...' ACTOR='...' REASON='...'"; exit 1; fi
	$(PYTHON) scripts/erase_candidate.py \
		--phone "$(PHONE)" \
		--actor "$(ACTOR)" \
		--reason "$(REASON)"

# Backup the production database
# Usage: make backup BACKUP_DIR=/backups
backup:
	@if [ -z "$(BACKUP_DIR)" ]; then echo "Usage: make backup BACKUP_DIR=/backups"; exit 1; fi
	pg_dump -U flow_migrate -d flow -F c -Z 9 \
		-f "$(BACKUP_DIR)/flow_$(shell date +%Y%m%d_%H%M%S).dump"
	@echo "Backup written to $(BACKUP_DIR)"

