# Flow Build Tasks

## Phase 0 · Foundation

- `[x]` **FLOW-001** — git init, .gitignore, .env.example, README.md
- `[x]` **FLOW-002** — pyproject.toml, requirements.txt, requirements-dev.txt, missing __init__ files
- `[x]` **FLOW-003** — Settings module (app/config.py), refactor app/database.py
- `[x]` **FLOW-004** — Test isolation: conftest.py, factories, fix failing tests
- `[x]` **FLOW-005** — Docker compose + Makefile
- `[x]` **FLOW-006** — GitHub Actions CI

## Phase 1 · Schema and domain

- `[x]` **FLOW-007** — Structured logging and request correlation
- `[x]` **FLOW-008** — Core models: candidates, conversations, messages (UUID PKs, lifecycle enums)
- `[x]` **FLOW-009** — Attribute store with provenance + key registry
- `[x]` **FLOW-010** — Projection and preference tables
- `[x]` **FLOW-011** — Baseline migration + Alembic hardening (ADK schema isolation)
- `[x]` **FLOW-012** — Unit-of-work repositories (no commits in repos)
- `[x]` **FLOW-013** — Merge engine (provenance, supersession, conflict resolution)
- `[x]` **FLOW-014** — Normalisers (money, notice period, experience, locations)

## Phase 2 · Turn engine

- `[x]` **FLOW-015** — Channel ingress (InboundEvent DTO, E.164, idempotency, rate limit)
- `[x]` **FLOW-016** — Conversation lifecycle service
- `[x]` **FLOW-045** — Consent gate
- `[x]` **FLOW-017** — ADK session wiring (adk schema)
- `[x]` **FLOW-018** — Extraction schema + extractor agent
- `[x]` **FLOW-019** — Policy agent
- `[x]` **FLOW-020** — Reply agent + instruction provider
- `[x]` **FLOW-021** — Root agent + App + Runner + TurnService
- `[x]` **FLOW-022** — V1 tools (snapshot, glossary; delete unsafe tools)
- `[x]` **FLOW-023** — Webhook rewrite
- `[x]` **FLOW-024** — Guardrail callbacks

## Phase 3 · Conversation quality

- `[x]` **FLOW-025** — Next-question scoring
- `[x]` **FLOW-026** — Greeting and name policy
- `[x]` **FLOW-027** — Interruptions and redirection
- `[x]` **FLOW-028** — Deflection counting and disengagement
- `[x]` **FLOW-029** — Abuse handling
- `[x]` **FLOW-030** — Staleness and refresh conversation
- `[x]` **FLOW-047** — Candidate lifecycle state machine
- `[x]` **FLOW-031** — Conversation summaries and history recall
- `[x]` **FLOW-032** — Evaluation harness

## Phase 4 · Resumes

- `[x]` **FLOW-033** — Storage adapter
- `[x]` **FLOW-034** — Media validation
- `[x]` **FLOW-035** — Resume model and versioning
- `[x]` **FLOW-036** — Resume conversation policy

## Phase 5 · Recruiter surface

- `[x]` **FLOW-037** — Recruiter read API with three-class filtering
- `[x]` **FLOW-038** — Recruiter identity, roles and verification
- `[ ]` **FLOW-039** — Incomplete-candidate backlog view
- `[ ]` **FLOW-040** — Observability

## Phase 6 · Production

- `[ ]` **FLOW-041** — WhatsApp channel adapter
- `[ ]` **FLOW-042** — Rate limiting and replay protection
- `[ ]` **FLOW-043** — Retention, deletion and consent
- `[ ]` **FLOW-044** — Production hardening
