# Flow Rebuild — Findings and Architectural Decisions

*Last updated: 2026-09-06 · Covers Phases 0 through 4 (FLOW-001 to FLOW-036), plus the pre-Phase-5 review pass*

---

## 1. Core Architecture & Schema Separation

### Finding: ADK and Domain State Collision
- **Problem:** When Google ADK (Agent Development Kit) writes session states, runner events, and execution metadata directly into the application schema, Alembic migrations attempt to autogenerate migration diffs for ADK internal tables, creating schema corruption and test instability.
- **Decision:** Strict PostgreSQL schema isolation.
  - `flow` schema: Owns all domain models (`candidates`, `candidate_attributes`, `candidate_profiles`, `conversations`, `messages`, `resumes`, `audit_events`, `moderation_events`).
  - `adk` schema: Dedicated sandbox for ADK session tables (`sessions`, `events`, `app_states`, `user_states`, `adk_internal_metadata`).
  - Alembic `migrations/env.py` uses `include_object` to filter out everything outside `flow`. Test suite (`tests/test_migrations.py`) asserts autogenerate emits an empty diff against active DB metadata.

### Finding: Multi-Tenant Complexity vs. Single-Tenant Reality
- **Problem:** Prematurely adding `org_id` across all tables adds cognitive overhead, unnecessary foreign keys, index bloat, and query complexity for a single-tenant deployment.
- **Decision (Q1):** Flow is strictly single-tenant in V1. No `org_id` on any domain entity or repository.

---

## 2. Privacy, Consent, and Data Classification

### Finding: The Sensitive Data Dilemma
- **Problem:** Recruitment conversations frequently uncover sensitive facts (e.g. marital status, health accommodations, reasons for relocation) that recruiters must not see in candidate search results, and which automated matching engines must never process.
- **Decision (Q5 — Three-Class Model):**
  1. `operational`: Safe for projection (`candidate_profiles`), search, filtering, and automated matching (e.g. `desired_role`, `experience_years`, `skills`, `expected_ctc`, `notice_period`, `work_mode`).
  2. `personal`: Stored with provenance in `candidate_attributes`, but filtered out from `candidate_profiles`. Only accessible via audited detail endpoints (e.g. `full_name`, `date_of_birth`, `marital_status`).
  3. `protected`: Stored under strict access control; never emitted to candidate-facing or recruiter search responses without explicit escalation (e.g. `religion`, `caste`, `health_disability`, `sex_gender`).
  - Unknown keys fail closed and default to `personal`.

### Finding: Pre-Consent Data Persistence Leakage
- **Problem:** If candidate messages containing personal data or career facts are parsed and persisted to `candidate_attributes` before the candidate agrees to the data privacy terms, it violates GDPR/DPDP regulations.
- **Decision (Q4 — Consent Gate):**
  - First-time candidates start in `conversation_mode=consent`.
  - Zero rows are written to `candidate_attributes` before `consent_status=granted`.
  - If a candidate declines or withdraws consent, Flow gracefully closes with a single polite farewell, stores no facts, and sets `consent_status=declined` / `withdrawn`.

---

## 3. Concurrency & Transaction Management

### Finding: Concurrent Ingress Candidate Creation Race
- **Problem:** Two near-simultaneous WhatsApp webhooks from the same phone number can cause concurrent `SELECT` queries to both find no candidate, followed by concurrent `INSERT` statements, leading to unique constraint violations or duplicate sessions.
- **Decision (Q1, FLOW-012):**
  - Candidate acquisition uses atomic upsert semantics: `INSERT INTO flow.candidates (phone_number, ...) ON CONFLICT (phone_number) DO NOTHING` followed by an immediate `SELECT`.
- **Unit of Work Pattern:**
  - Repositories (`CandidateRepository`, `AttributeRepository`, `ResumeRepository`, etc.) are strictly prohibited from calling `session.commit()`, `session.rollback()`, or `session.refresh()`.
  - All transactional boundaries are managed via `UnitOfWork` context managers (`app/db/uow.py`).

---

## 4. Normalisation & Fact Precedence

### Finding: The Precedence Ladder & Conflict Resolution
- **Problem:** Candidates often provide contradictory statements, or LLMs make faulty inferences from ambiguous text.
- **Decision (FLOW-013, FLOW-014):**
  - 7x7 precedence matrix (rank matches `app/domain/merge.py`'s `_PRECEDENCE` table exactly — higher number wins):
    1. `recruiter_verified` (rank 6 — highest authority; no model output ever overwrites it)
    2. `candidate_confirmed` (rank 5)
    3. `candidate_stated` (rank 4)
    4. `resume` (rank 3 — an opaque parsed document; sits below what the candidate says today, because resumes go stale)
    5. `system_calculated` (rank 2)
    6. `llm_inferred` (rank 1)
    7. `channel_metadata` (rank 0 — lowest authority; the WhatsApp contact name, icebreaker only)
  - Higher rank strictly supersedes lower rank.
  - Same rank with different values: candidate-stated facts in the same conversation supersede silently; cross-conversation contradictions mark the old fact `conflicted` and trigger a `resolve_conflict` directive.
  - Money normaliser handles Indian denominations (`12 LPA` -> `1,200,000`, `1.5 Cr` -> `15,000,000`, `80k/month` -> `960,000`). Ambiguous statements (e.g. `about 80k`) store `status=ambiguous` and never project.

---

## 5. Resume Architecture & Invariants

### Finding: Opaque Blob vs. Premature OCR/Parsing
- **Problem:** Attempting full text parsing or LLM resume extraction within the conversational turn engine introduces high latency, cost, and hallucinated fact corruption.
- **Decision (§11, FLOW-033 to FLOW-036):**
  - Flow treats resumes as opaque binary files stored in private object storage (Supabase or LocalStorage adapter).
  - PostgreSQL `flow.resumes` table stores metadata only (`id`, `candidate_id`, `version`, `bucket`, `object_key`, `filename`, `content_type`, `size_bytes`, `checksum`, `is_current`, `parse_status`, `confirmed_at`).
  - **Single Current Invariant:** Database-enforced partial unique index `ix_flow_resumes_candidate_current` on `(candidate_id) WHERE is_current = true`.
  - **Deduplication:** When an identical SHA-256 checksum is uploaded, Flow confirms the existing version rather than uploading a duplicate file or incrementing the version.
  - **Policy ("Ask once, confirm gracefully, never nag"):**
    - If no resume exists on file: Flow asks for a resume once upon reaching profile readiness (`ask_resume`).
    - If a resume already exists: Flow asks whether the resume on file is still latest (`confirm_resume`).
    - On candidate confirmation ("yes"): Flow marks `confirmed_at = now()`, stores `source=candidate_confirmed`, does not request a file, and never asks again in that conversation.
    - If candidate uploads a new file: version *n+1* is stored and set current; earlier versions are archived (`is_current = False`). Intake does not restart.

---

## 6. Safety & LLM Tool Security

### Finding: Identity Spoofing in Agent Tool Calls
- **Problem:** If LLM tools accept parameters like `candidate_id` or `phone_number`, prompt injection could cause the model to query or modify data belonging to other candidates.
- **Decision (FLOW-022, FLOW-036):**
  - All agent-callable tools (`get_candidate_snapshot`, `explain_recruitment_term`, `recall_candidate_history`, `record_resume_confirmation`) accept `ToolContext` injected by the runner.
  - The generated FunctionDeclarations exposed to the LLM have **zero parameters** related to candidate identity. Candidate identity is read strictly from verified session context state.

---

## 7. Migration & Autogenerate Discoveries

### Finding: Server Defaults on Partial Indexes and Boolean Columns
- **Problem:** When Alembic autogenerates comparisons against PostgreSQL, columns with `default=True` or `default=SourceEnum.candidate_stated` in SQLAlchemy without matching `server_default` clauses emit spurious `modify_default` diffs during `test_autogenerate_produces_no_diff`.
- **Decision:** Every column created with a server default in Alembic migrations must declare matching `server_default=text("true")` and `server_default=SourceEnum.candidate_stated.value` in the model definition.

---

## 8. Pre-Phase-5 Review Findings

*Reviewed on 2026-09-06 before starting Phase 5. Full review in `docs/REVIEW_AND_PLAN.md`.*

### Finding: `full_name` reclassified from `personal` to `operational`
- **Problem:** The registry originally classified `full_name` as `personal`, on the reasoning that a name is PII. But `app/domain/identity.py` writes `profile.full_name` directly onto the `candidate_profiles` projection row — bypassing `rebuild_projection`'s operational-only filter entirely, since name confirmation doesn't go through the attribute/merge pipeline the way other facts do. That meant a `personal`-classified field was already sitting, unfiltered, on the one table the recruiter API and future matching system are meant to read as "safe."
- **Decision:** A name is required to submit a candidate to a client, so it does not meet Q5's own definition of personal data ("not required for recruitment"). `full_name` is now `operational` — visible in recruiter search and list views like any other operational field, and `ProfileSnapshot` in `app/domain/projection.py` carries it explicitly so it flows through the same projection path as every other operational fact rather than around it.
- **Still true:** `phone_number` remains excluded from every tool and API response regardless of `data_class` — that exclusion is about identity, not sensitivity, and is unaffected by this change.

### Finding: `docs/tasks.md` FLOW-037 onward not yet checked off
- **Status at review time:** Phases 0–4 complete (FLOW-001 through FLOW-036, plus FLOW-045 and FLOW-047 added by the Q4/Q8 decisions). Phase 5 (FLOW-037 to FLOW-040) begins next.
