# Flow Rebuild — Findings and Architectural Decisions

*Last updated: 2026-09-07 · Covers Phases 0 through 4 (FLOW-001 to FLOW-036), the pre-Phase-5 review pass, and FLOW-037/FLOW-038*

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
- **Nuance, corrected:** `phone_number` is excluded from the **LLM tool layer** (`app/tools/snapshot.py`) — that exclusion is about preventing identity spoofing through the model, and is unaffected by this change. It is **not** excluded from the recruiter API (FLOW-037): a recruiter's job requires actually contacting the candidate, and `phone_number` isn't a `data_class`-governed attribute at all — it's trusted identity on the `candidates` row, a different category entirely. The two layers have different reasons to treat it differently; this is a decision, not an inconsistency.
- **Related, separately fixed:** `update_candidate_identity()` (FLOW-026) is fully implemented and unit-tested, but is never called from the live turn pipeline (`app/agents/policy.py` / `app/services/turn.py`) — only from its own tests. `profile.full_name` is therefore always `None` in production today, regardless of this reclassification. Not fixed as part of this finding: wiring it in touches the name/greeting directive ladder, which has its own passing test suite, and deserves a dedicated pass rather than a fix folded into unrelated work. `app/domain/policy.py`'s profile sync is guarded (`if snapshot.full_name is not None`) so it can never clobber whatever that future fix sets.

### Finding: `candidate_skills` / `candidate_role_prefs` / `candidate_location_prefs` were never populated
- **Problem:** These tables have existed since FLOW-010 with the correct indexes and unique constraints, but nothing ever wrote to them — `rebuild_projection()` held skills/desired_roles/locations only as transient `ProfileSnapshot` fields, never persisted. Found while building the recruiter search API (FLOW-037), which needs exactly this data to filter by skill and location.
- **Decision:** Added `sync_preference_tables()` to `app/domain/policy.py`, called from `evaluate_policy_step` right after the `candidate_profiles` scalar sync. Same design as the profile projection: delete-and-reinsert on every turn from the full current attribute set, not diffed, since these are derived/rebuildable, not history. Location `strength` defaults to `"preferred"` — the extractor does not yet distinguish preferred/acceptable/hard-requirement, a real, separate gap in the extraction schema, not invented here.

### Finding: `Message.created_at` used a transaction-scoped default, breaking transcript ordering
- **Problem:** Found while testing the recruiter transcript endpoint. `Message.created_at` used `server_default=func.now()`. PostgreSQL's `now()` is stable for the whole transaction, not per-statement — so several messages written in one commit (routine: a turn writes inbound and outbound together) can share an identical timestamp. With a random UUID primary key as the only tiebreaker, chronological order was not actually recoverable, and a five-message test batch came back in a scrambled order (`message 4, message 0, ...`).
- **Decision:** Added a Python-side `default=lambda: datetime.now(UTC)` on `Message.created_at`, evaluated once per row at insert time. `server_default` stays as a fallback for any insert that bypasses the ORM. No migration required — a Python-level `default` is invisible to the schema Alembic compares against, confirmed by `test_migrations.py` (autogenerate still emits an empty diff).

### Finding: protected-class data in the recruiter API — no escalation path in Phase 5 (business decision)
- **Decision:** Protected attributes (religion, caste, health/disability, sex/gender) are stored with full provenance but **no Phase 5 endpoint returns them under any condition** — no flag, no justification parameter, no role check. Simplest to build, structurally impossible to leak, and avoids designing a governance workflow (who may escalate, under what justification, with what record) before there is an organisation to run it. That governance question is deliberately deferred to Phase 6, alongside moving `protected` attributes into the separate `flow_sensitive` schema with its own Postgres role and column grants (§7 of `REVIEW_AND_PLAN.md`).

## 9. FLOW-038 — Recruiter Identity, Roles and Verification

### Design
- `Recruiter` (email, display_name, role: recruiter|admin, api_key_hash, is_active) and `RecruiterNote` (candidate_id, recruiter_id, note — never read by any candidate-facing code path; there is no candidate-facing API in Flow at all, and no `app/tools/*.py` queries this table, so the isolation is structural, not conventional).
- `candidates.assigned_recruiter_id` — a nullable FK, current-state only (not a history table). "Assign candidates" per the requirements is a simple current-owner operation; every change is still recorded in `audit_events`, so history is available without a dedicated assignment table.
- Auth: a per-recruiter API key, SHA-256 hashed at rest (`app/api/auth.py`), replacing FLOW-037's shared-secret placeholder exactly where that placeholder said it would be replaced. `hash_api_key`/`generate_api_key` follow the same `hashlib.sha256(...).hexdigest()` idiom already used for resume checksums (`app/channel/media.py`) rather than introducing a new dependency (bcrypt/argon2/passlib) for a credential that is a random 32-byte token, not a user-chosen password needing slow-hash brute-force resistance.
- Bootstrap: there is no self-service "create the first admin" HTTP endpoint. `POST /recruiter/admin/recruiters` requires an existing admin, which is exactly the chicken-and-egg problem an unauthenticated bootstrap endpoint would need to solve — and an unauthenticated account-creation endpoint is a standing attack surface for as long as it exists. `scripts/create_recruiter.py` creates the first account directly against the database, as an operator action.
- Corrections write at `source=recruiter_verified` (rank 6, the ceiling of `app.domain.merge.SOURCE_RANKS`) through the same `merge_facts` engine every other fact goes through, then immediately rebuild the projection and preference tables (reusing `rebuild_projection` / `sync_preference_tables`) so the correction is visible via the recruiter API right away, not only after the candidate's next WhatsApp turn.

### Finding: two recruiter corrections to the same key were incorrectly marked `conflicted`
- **Problem:** Found while testing "a recruiter fixing their own earlier correction." `merge_facts`'s same-rank/different-conversation rule (`tests/test_merge.py::test_7x7_precedence_matrix`) deliberately marks two equal-rank facts as conflicted unless the incoming source is `candidate_confirmed` — correct and already tested for candidate-side facts, where "different conversation" is a meaningful signal about whether a correction is the same episode or an old, separate contradiction. It is not a meaningful signal for this endpoint: a correction made through `POST /candidates/{id}/corrections` has no real conversation at all, so two recruiter corrections to the same key always looked like a "different conversation, same rank" case, which the existing (correct, tested) rule marks conflicted.
- **Decision:** Handled at the endpoint, not by changing `merge_facts`. When the existing current fact for a key is *also* `source=recruiter_verified`, the correction endpoint accepts the new value directly rather than routing it through `merge_facts` — recruiter_verified is already the top of the precedence table, so there is no higher authority whose confirmation an update could need, and a recruiter calling this endpoint a second time is unambiguously saying "update it again." This leaves `merge_facts`'s tested behaviour for every other caller (the live turn pipeline) completely unchanged.

### Finding: `Fact.source`/`.confidence`/`.data_class` are plain strings — assigning them directly to a `CandidateAttribute` ORM column skips Enum coercion
- **Problem:** `app.domain.merge.Fact` is a pure dataclass (`source: str`, no ORM in its signature, by design). Constructing `CandidateAttribute(source=accepted.source, ...)` with that raw string compiles and inserts fine, but the in-memory Python object then holds a plain `str`, not a `SourceEnum` member — and a later read of that same object from the session's identity map (e.g. immediately rebuilding the projection in the same request) hits `AttributeError: 'str' object has no attribute 'value'` on `.source.value`. `app/domain/policy.py`'s existing turn-pipeline code already avoided this by always constructing with real Enum members (`SourceEnum.candidate_stated`, `DataClassEnum(new_fact.data_class)`, etc.) — this was purely a bug in the new correction endpoint, not a pre-existing issue.
- **Decision:** The correction endpoint now explicitly coerces (`SourceEnum(accepted.source)`, `ConfidenceEnum(accepted.confidence)`, `DataClassEnum(accepted.data_class)`) when constructing the ORM row, matching the established pattern.

### Finding: the migration's downgrade left an orphaned Postgres enum type
- **Problem:** `op.drop_table('recruiters', ...)` does not drop the `recruiter_role_enum` TYPE it depends on. `tests/test_migrations.py::test_migration_round_trip` (upgrade → downgrade → upgrade) caught this: the second upgrade failed with `type "recruiter_role_enum" already exists`, since the type survived the downgrade.
- **Decision:** Added `op.execute("DROP TYPE IF EXISTS flow.recruiter_role_enum CASCADE")` to the migration's `downgrade()`, matching the convention already established in the baseline migration (`042e6ac5de67`) for every other enum type. Also gave the new `candidates.assigned_recruiter_id` foreign key an explicit constraint name (`fk_candidates_assigned_recruiter_id_recruiters`) — autogenerate left it unnamed (`None`) in the downgrade's `drop_constraint` call, which is a distinct failure mode from the enum issue and would have broken the same round-trip test.

### CorrectionRequest value-shape contract
- **Decision:** A correction's `value` must already be in the same canonical shape `app.domain.policy.normalize_fact_value()` produces for candidate-stated text (`{"amount": X}` for `experience_years`, `{"amount": X, "currency": ..., "period": ...}` for CTC fields, `{"days": X}` for `notice_period`), documented explicitly on `CorrectionRequest` in `app/api/recruiter.py`. Deliberately **not** routed through `normalize_fact_value()` itself: that function parses loose candidate phrasing and exists specifically to catch hedge words in free text — a recruiter correction is by definition already an authoritative, resolved value (`confidence=confirmed`), not text to be reinterpreted, and running it through a text parser risks silently mangling a well-formed structured value rather than visibly failing on a malformed one.

### Status
- Phases 0–4 complete (FLOW-001 through FLOW-036, plus FLOW-045 and FLOW-047 added by the Q4/Q8 decisions). FLOW-037, FLOW-038, and FLOW-039 complete. Phase 5 continues with FLOW-040 (observability).

---

## 10. Phase 5 Decisions — FLOW-039 & Simulator Frontend

### FLOW-039 — Incomplete-Candidate Backlog View
- **Indexed columns:** Added database migration (`42c70b2325f7_add_backlog_indexes.py`) indexing `candidate_profiles.completeness`, `conversations.last_inbound_at`, and `candidates.lifecycle_status`. Verified autogenerate produces zero diff against models.
- **Inclusion & Exclusion Rules:**
  - Candidates with `completeness < threshold` (default 1.0) inactive beyond `inactivity_days` (default 3) are surfaced.
  - Candidates who are `profile_ready`, `blocked`, `disengaged` (3+ deflections), or who have not granted consent (`pending`, `declined`, `withdrawn`) are strictly excluded.
- **Value Score:** Balanced metric combining profile readiness progress and recency:
  $0.70 \times \text{completeness} + 0.30 \times \max(0, 1.0 - (\text{days\_inactive} - 3)/30)$.
- **Three-Class Compliance:** Backlog response strictly conforms to Q5; zero personal or protected attributes are emitted.

- **Interactive WhatsApp Simulator & Agent Intelligence Cockpit:**
  - Served directly at `/` and `/simulator` via FastAPI static mount with vanilla HTML5, custom CSS design system (dark glassmorphism, responsive split layout), and vanilla JS.
  - **Phone Simulator:** Live turn exchange with persona presets, thinking indicators, delivery status receipts, and quick reply chips.
  - **Data Inspector Cockpit:** Real-time visibility into the directive ladder, conversation mode, deflection counter, operational profile projection, attribute provenance store (with source, confidence, status, data_class), preferences tag clouds, and live FLOW-039 backlog view.
  - **Housekeeping:** Removed dead `recruiter_api_key` setting from `app/config.py` and `.env.example`.

### FLOW-040 — Observability, Tracing, Cost Accounting & PII Absence
- **One Trace Per Turn:** Root span named `turn` wraps the entire lifecycle of an inbound message from ingress to reply emission, reporting `turn.latency_ms`, `turn.cost_usd`, `turn.tokens.input`, `turn.tokens.output`, `turn.tokens.total`, and `turn.tool_calls_count`.
- **Three Child Spans:** Child spans for each ADK pipeline agent (`extractor`, `policy`, `replier`) share the root turn's `trace_id` and point to the root turn as `parent_id`.
- **Gemini Token Cost Modeling:** Implemented rate-card estimation in `app/observability/cost.py` for Gemini 2.5 Flash ($0.075 / 1M prompt tokens, $0.30 / 1M candidate tokens) and Gemini 2.5 Pro.
- **Tool Spans:** Tool invocations create child spans recording `tool.name`, `tool.duration_ms`, and `tool.success`, while strictly suppressing all parameter arguments and result payloads to prevent identity or profile leakage.
- **Strict PII Absence (§17):** Verified by automated test suites. Under no circumstances are phone numbers, candidate message bodies, prompt contents, or model completions recorded in OpenTelemetry span attributes or events. Only correlation IDs (`request_id`, `candidate_id`, `conversation_id`), latencies, counts, and cost metrics are emitted.

### FLOW-041 — WhatsApp Channel Adapter (Meta WhatsApp Cloud API)
- **Meta WhatsApp Cloud API Exclusivity (Q7):** Built exclusively against Graph API v21.0. No secondary provider or multi-provider abstraction layer.
- **Strict Layering Boundary:** `InboundEvent` is the immutable architectural boundary. Absolutely no file under `app/domain/`, `app/services/`, or `app/agents/` imports from `app/channel/whatsapp/` (enforced via AST inspection test in `tests/test_layering.py`).
- **Security & Integrity:** Verified `X-Hub-Signature-256` HMAC-SHA256 signature against raw payload bytes before parsing. Handshake endpoint handles GET `hub.mode`/`hub.verify_token`/`hub.challenge`.
- **Payload Parsing:** Normalizes Cloud API webhook envelopes (`entry[].changes[].value.messages[]`) with E.164 phone formatting and profile contact names. Non-message events (e.g. delivery receipts) are ignored safely.
- **Two-Step Media Download:** Media ID queries Graph API metadata for download URL, followed by authenticated binary stream fetch piped into `validate_media` (FLOW-034).
- **Outbound Client:** Features exponential backoff retries on transient 5xx errors and strictly checks 24-hour customer service window expiry.

### Status
- Phases 0–5 complete (FLOW-001 through FLOW-040, plus FLOW-045 and FLOW-047).
- Phase 6: FLOW-041, FLOW-042, FLOW-043 complete.
- Next up: FLOW-044 (Production hardening).

---

## 2026-09-07 · FLOW-042: Rate limiting, replay protection & daily candidate spend control

### Architectural Decisions
1. **5-Minute Timestamp Replay Window (`±300s`)**:
   - Webhook ingress evaluates the webhook payload timestamp. Any inbound event drifting by more than 300 seconds from server clock is rejected immediately at ingress with HTTP 400 (`timestamp_out_of_bounds`).
2. **Postgres-Backed Sliding Window Rate Limiting (`flow.rate_limit_hits`)**:
   - `RateLimitHit` table stores `(id, key, hit_at)` with compound B-tree index `(key, hit_at)`.
   - Sliding window count executed via `SELECT COUNT(*) WHERE key = :key AND hit_at >= :window_start`.
   - `IPRateLimiter` FastAPI dependency enforces 60 requests/min per IP on webhook endpoints, returning HTTP 429 (`Too many requests from this IP address`).
   - Per-phone rate limit enforces 20 turns/min per phone number.
3. **Candidate Daily Spend Protection**:
   - Candidate turn counting queries inbound turns in the past 24 hours against the configured `daily_model_call_budget` (default: 30 turns/day).
   - When budget is exhausted, turn execution halts before any LLM extractor or replier call.
   - The candidate receives a graceful polite hold message: `"Thank you for sharing all these details today! Our team will review your profile and get back to you shortly."`

---

## 2026-09-07 · FLOW-043: Retention, deletion and consent integration

### Architectural Decisions
1. **Candidate PII Erasure Path (`erase_candidate_data`)**:
   - Anonymizes phone number to unique identifier `+deleted_<uuid>` (maintaining database unique index constraints).
   - Clears `display_name`, sets `consent_status=withdrawn`, `lifecycle_status=dormant`, and sets `blocked_at`.
   - Deletes all personal and protected attributes from `flow.candidate_attributes` (breaking self-referential foreign keys cleanly before deletion).
   - Removes all preference tags (`CandidateSkill`, `CandidateRolePref`, `CandidateLocationPref`) and profile projection (`CandidateProfile`).
   - Removes resume records from DB and physically invokes `storage.delete(object_key)` on the configured storage adapter (`LocalStorageAdapter` and `SupabaseStorageAdapter`).
   - Overwrites historical message bodies with `[deleted]` and clears `media_ref`.
   - Emits immutable `AuditEvent` (`action="candidate_erasure"`).
2. **Consent Withdrawal Integration**:
   - Expanded vocabulary in `app.domain.consent` (`classify_consent`) detects expressions of withdrawal like "delete my data", "stop contacting me and delete everything", and "erase my data".
   - In `TurnService.run()`, encountering `directive == "consent_withdrawn"` triggers immediate execution of `erase_candidate_data` before returning `CONSENT_WITHDRAWN_REPLY` and closing the conversation.
3. **Data Classification Retention Sweep (`run_retention_sweep`)**:
   - Enforces Q5 retention periods:
     * `protected`: 30 days
     * `personal`: 90 days
     * `operational`: 365 days
     * closed conversations: 180 days (with cascaded message pruning)
   - Supports `--dry-run` flag via Python CLI (`python -m app.services.privacy --dry-run`) and programmatic calls, computing exact prune counts without mutating DB or storage.
   - Non-dry run prunes expired rows and emits an immutable `AuditEvent` (`action="retention_sweep"`).


