# Flow Operations Runbook

> **Scope:** V1 production deployment. Every section names the on-call owner, the immediate action, and the escalation path.

---

## Table of Contents

1. [Service Architecture](#1-service-architecture)
2. [Alert Definitions](#2-alert-definitions)
3. [Failure 1 — API Error Rate Spike](#3-failure-1--api-error-rate-spike)
4. [Failure 2 — Model Spend Spike / Budget Exhaustion](#4-failure-2--model-spend-spike--budget-exhaustion)
5. [Failure 3 — Database Connectivity Loss](#5-failure-3--database-connectivity-loss)
6. [Failure 4 — Moderation Escalation Surge](#6-failure-4--moderation-escalation-surge)
7. [Failure 5 — Candidate Data Erasure Request (GDPR / Legal)](#7-failure-5--candidate-data-erasure-request-gdpr--legal)
8. [Backup and Restore Drill](#8-backup-and-restore-drill)
9. [Secrets Rotation](#9-secrets-rotation)
10. [Retention Sweep Operations](#10-retention-sweep-operations)
11. [Deployment Checklist](#11-deployment-checklist)

---

## 1. Service Architecture

```
                     Meta WhatsApp Cloud API
                             │  POST /webhook
                             ▼
                     ┌──────────────┐
                     │  FastAPI App  │  (Uvicorn + Gunicorn)
                     │  :8000        │
                     └──────┬───────┘
              ┌─────────────┼──────────────┐
              ▼             ▼              ▼
        PostgreSQL 16   Google Gemini   Supabase Storage
        (flow schema)   (Gemini Flash)  (resumes bucket)
```

**Key invariants:**
- Every inbound turn is one atomic database transaction (`UnitOfWork`).
- No candidate PII ever appears in logs (enforced by `app/logging.py` redaction).
- Every database write carries a `request_id` correlation header for tracing.

**Database schemas:**
- `flow` — all application tables (candidates, conversations, messages, attributes, resumes, audit_events, rate_limit_hits)
- `adk` — Google ADK session state (managed by ADK library, never touched by migrations)

---

## 2. Alert Definitions

Configure these in your monitoring platform (Prometheus + Alertmanager, Datadog, GCP Monitoring, etc.).

### 2.1 Error Rate Alert

```yaml
# Alert fires when HTTP 5xx error rate exceeds 1% over 5 minutes
alert: FlowApiErrorRateHigh
expr: rate(http_requests_total{status=~"5.."}[5m]) / rate(http_requests_total[5m]) > 0.01
for: 3m
severity: warning
annotations:
  summary: "Flow API 5xx error rate above 1%"
  runbook: "docs/runbook.md#failure-1--api-error-rate-spike"
```

### 2.2 Model Spend Alert

```yaml
# Alert fires when estimated model cost exceeds $5 in a 1-hour window
alert: FlowModelSpendHigh
expr: sum(rate(flow_model_cost_usd_total[1h])) * 3600 > 5.0
for: 5m
severity: warning
annotations:
  summary: "Flow model spend exceeding $5/hour"
  runbook: "docs/runbook.md#failure-2--model-spend-spike--budget-exhaustion"
```

### 2.3 Turn Latency Alert

```yaml
# Alert fires when P99 turn latency exceeds 10 seconds
alert: FlowTurnLatencyHigh
expr: histogram_quantile(0.99, rate(flow_turn_duration_seconds_bucket[5m])) > 10
for: 5m
severity: warning
annotations:
  summary: "Flow turn P99 latency above 10s"
  runbook: "docs/runbook.md#failure-3--database-connectivity-loss"
```

### 2.4 Moderation Escalation Alert

```yaml
# Alert fires when abuse escalations exceed 5 in a 10-minute window
alert: FlowModerationEscalationSpike
expr: increase(flow_moderation_events_total{kind="escalation"}[10m]) > 5
for: 0m
severity: critical
annotations:
  summary: "Flow moderation escalations spiking"
  runbook: "docs/runbook.md#failure-4--moderation-escalation-surge"
```

### 2.5 Database Connection Alert

```yaml
# Alert fires when database connection pool is exhausted
alert: FlowDatabasePoolExhausted
expr: flow_db_pool_connections_idle < 2
for: 2m
severity: critical
annotations:
  summary: "Flow database connection pool nearly exhausted"
  runbook: "docs/runbook.md#failure-3--database-connectivity-loss"
```

### 2.6 Daily Budget Exhaustion Rate Alert

```yaml
# Alert fires when > 5% of candidates hit the daily model budget cap
alert: FlowDailyBudgetExhaustionHigh
expr: rate(flow_directive_total{directive="daily_budget_exhausted"}[1h]) / rate(flow_turns_total[1h]) > 0.05
for: 10m
severity: warning
annotations:
  summary: "Many candidates hitting daily model budget cap"
  runbook: "docs/runbook.md#failure-2--model-spend-spike--budget-exhaustion"
```

---

## 3. Failure 1 — API Error Rate Spike

**Symptoms:** HTTP 5xx rate > 1% over 5 minutes, latency spikes, Sentry errors.

**Triage steps:**

1. **Check recent logs** for structured error payloads:
   ```bash
   # GCP / CloudWatch / any structured log query:
   # level=ERROR, last 15m
   ```

2. **Check for model failures** (`SAFE_FALLBACK_REPLY` in replies signals model error):
   ```sql
   SELECT COUNT(*), MAX(created_at)
   FROM flow.messages
   WHERE body LIKE '%We encountered a small hiccup%'
     AND created_at > NOW() - INTERVAL '15 minutes';
   ```

3. **Check for migration mismatch** (schema out of date):
   ```bash
   .venv/bin/alembic current
   .venv/bin/alembic heads
   ```

4. **Check rate_limit_hits table** for a thundering herd:
   ```sql
   SELECT key, COUNT(*) as hits
   FROM flow.rate_limit_hits
   WHERE hit_at > NOW() - INTERVAL '5 minutes'
   GROUP BY key ORDER BY hits DESC LIMIT 20;
   ```

**Remediation:**
- Model errors: check Google API status page; the service degrades to `SAFE_FALLBACK_REPLY` automatically — no action needed for transient outages.
- Migration mismatch: run `make migrate` (use `flow_migrate` role).
- Thundering herd: temporary IP block via WAF or load balancer rule.

**Escalation:** If unresolved in 30 minutes, page the on-call engineer.

---

## 4. Failure 2 — Model Spend Spike / Budget Exhaustion

**Symptoms:** Model cost alert firing, candidates receiving `POLITE_HOLD_MESSAGE` unexpectedly.

**Triage steps:**

1. **Check which candidates are hitting daily budget**:
   ```sql
   SELECT candidate_id, COUNT(*) as turns_today
   FROM flow.messages
   WHERE direction = 'inbound'
     AND created_at > NOW() - INTERVAL '24 hours'
   GROUP BY candidate_id
   HAVING COUNT(*) >= 25
   ORDER BY turns_today DESC LIMIT 20;
   ```

2. **Check for unusual conversation activity** (possible spam / bot):
   ```sql
   SELECT c.phone_number, COUNT(m.id) as msg_count
   FROM flow.candidates c
   JOIN flow.messages m ON m.candidate_id = c.id
   WHERE m.created_at > NOW() - INTERVAL '1 hour'
   GROUP BY c.phone_number
   HAVING COUNT(m.id) > 30
   ORDER BY msg_count DESC;
   ```

3. **Temporarily lower the daily budget** (config change, no code deploy):
   ```bash
   # Set DAILY_MODEL_CALL_BUDGET=10 in your secrets manager and restart the app
   ```

**Remediation:**
- Spam/bot: block the phone number immediately:
  ```sql
  UPDATE flow.candidates
  SET blocked_at = NOW()
  WHERE phone_number = '+91XXXXXXXXXX';
  ```
- Legitimate high-volume day: temporarily increase `DAILY_MODEL_CALL_BUDGET`.

---

## 5. Failure 3 — Database Connectivity Loss

**Symptoms:** HTTP 503 errors, `OperationalError` in logs, all turns failing.

**Triage steps:**

1. **Verify database reachability**:
   ```bash
   psql -U flow_app -d flow -c "SELECT 1;"
   ```

2. **Check connection pool usage**:
   ```sql
   SELECT state, COUNT(*) FROM pg_stat_activity WHERE datname = 'flow' GROUP BY state;
   ```

3. **Check for long-running queries blocking connections**:
   ```sql
   SELECT pid, now() - pg_stat_activity.query_start AS duration, query, state
   FROM pg_stat_activity
   WHERE datname = 'flow' AND state != 'idle'
     AND (now() - pg_stat_activity.query_start) > INTERVAL '30 seconds'
   ORDER BY duration DESC;
   ```
   To terminate a blocking query: `SELECT pg_terminate_backend(<pid>);`

4. **Check disk space on the database host**:
   ```bash
   df -h /var/lib/postgresql
   ```

**Remediation:**
- Full disk: archive old WAL files; rotate logs; clear temp files.
- Connection exhaustion: reduce pool size via `DATABASE_URL` `pool_size` param; kill idle connections.
- Network partition: fail over to replica (see section 8).

---

## 6. Failure 4 — Moderation Escalation Surge

**Symptoms:** Moderation alert firing, unusual abuse_count values in conversations.

**Triage steps:**

1. **Review recent escalations**:
   ```sql
   SELECT me.kind, c.phone_number, me.detail, me.created_at
   FROM flow.moderation_events me
   JOIN flow.candidates c ON c.id = me.candidate_id
   WHERE me.created_at > NOW() - INTERVAL '30 minutes'
   ORDER BY me.created_at DESC
   LIMIT 20;
   ```

2. **Check if a single source is responsible** (coordinated attack):
   ```sql
   SELECT c.phone_number, COUNT(me.id) as events
   FROM flow.moderation_events me
   JOIN flow.candidates c ON c.id = me.candidate_id
   WHERE me.created_at > NOW() - INTERVAL '1 hour'
   GROUP BY c.phone_number
   ORDER BY events DESC;
   ```

**Remediation:**
- Coordinated attack: block all offending numbers; optionally raise the `RATE_LIMIT_PER_IP_PER_MINUTE` threshold temporarily.
- Organic surge (sensitive topic went viral): review abuse lexicon for false-positive phrases; adjust `app/domain/abuse.py`.
- **Never expose moderation event details in external reports** — these may contain message body fragments.

---

## 7. Failure 5 — Candidate Data Erasure Request (GDPR / Legal)

**Triggered by:** Legal or compliance team, regulatory request, or candidate "delete my data" message not caught by the automatic flow.

**SLA: Process within 72 hours of verified request.**

**Steps:**

1. **Verify the request is legitimate** (proof of identity from legal team).

2. **Find the candidate by phone number** (normalise to E.164 first):
   ```sql
   SELECT id, phone_number, lifecycle_status, consent_status
   FROM flow.candidates
   WHERE phone_number = '+91XXXXXXXXXX';
   ```

3. **Run the erasure script** (uses `flow_app` credentials):
   ```bash
   python scripts/erase_candidate.py --phone "+91XXXXXXXXXX" --actor "legal_team" --reason "GDPR request REF-12345"
   ```

4. **Verify the audit event was recorded**:
   ```sql
   SELECT id, actor_type, actor_id, action, after, created_at
   FROM flow.audit_events
   WHERE entity_type = 'candidate'
     AND action = 'candidate_erasure'
   ORDER BY created_at DESC LIMIT 5;
   ```

5. **Confirm storage deletion** — check that resume objects are gone:
   ```sql
   SELECT COUNT(*) FROM flow.resumes WHERE candidate_id = '<UUID>';
   -- Must return 0
   ```

6. **Report back to legal team** with the `audit_events.id` as the erasure receipt.

> **Note:** The `erase_candidate_data` function in `app/services/privacy.py` is the single code path for erasure. Never delete rows manually — always go through this function to ensure the audit trail is intact.

---

## 8. Backup and Restore Drill

> **Schedule:** Run this drill quarterly. Last successful drill date: _________________

### 8.1 Backup

**Automated backup (should be running nightly via cron):**
```bash
# Full logical dump (recommended for small-medium databases)
pg_dump -U flow_migrate -d flow -F c -Z 9 \
    -f /backups/flow_$(date +%Y%m%d_%H%M%S).dump

# Verify the backup is readable
pg_restore --list /backups/flow_<timestamp>.dump | head -20
```

**For production (WAL archiving / continuous backup):**
Configure `archive_mode = on` and `archive_command` in `postgresql.conf` for point-in-time recovery (PITR). Use `pgBackRest` or Barman.

### 8.2 Restore Drill (run in a separate environment)

```bash
# 1. Create a restore target database
createdb -U postgres flow_restore

# 2. Restore from backup
pg_restore -U postgres -d flow_restore /backups/flow_<timestamp>.dump

# 3. Verify row counts in key tables
psql -U postgres -d flow_restore -c "
SELECT
  (SELECT COUNT(*) FROM flow.candidates) AS candidates,
  (SELECT COUNT(*) FROM flow.conversations) AS conversations,
  (SELECT COUNT(*) FROM flow.messages) AS messages,
  (SELECT COUNT(*) FROM flow.candidate_attributes) AS attributes,
  (SELECT COUNT(*) FROM flow.audit_events) AS audit_events;
"

# 4. Verify migrations are consistent
DATABASE_URL=postgresql+psycopg://postgres:@localhost/flow_restore \
    .venv/bin/alembic current

# 5. Run the test suite against the restored DB
TEST_DATABASE_URL=postgresql+psycopg://postgres:@localhost/flow_restore \
    .venv/bin/pytest tests/test_models_core.py tests/test_repositories.py -v

# 6. Record result
echo "Restore drill complete: $(date)" >> docs/restore_drill_log.txt

# 7. Drop the restore target
dropdb -U postgres flow_restore
```

**Sign-off:** The drill passes when steps 3–5 all complete without errors.

---

## 9. Secrets Rotation

**Frequency:** Rotate all secrets every 90 days, or immediately on suspected compromise.

| Secret | Environment Variable | Action on Rotation |
|--------|---------------------|-------------------|
| Google API Key | `GOOGLE_API_KEY` | Rotate in Google Cloud Console → update secrets manager → rolling restart |
| Database password | `DATABASE_URL` | Change in Postgres → update secrets manager → rolling restart |
| Webhook HMAC secret | `WEBHOOK_SECRET` | Update Meta app settings → update secrets manager → rolling restart |
| WhatsApp Access Token | `WHATSAPP_ACCESS_TOKEN` | Rotate in Meta Business Manager → update secrets manager → rolling restart |
| Supabase Service Role Key | `SUPABASE_SERVICE_ROLE_KEY` | Rotate in Supabase dashboard → update secrets manager → rolling restart |

**Rotation process:**
1. Generate new secret.
2. Update in secrets manager (AWS Secrets Manager / Google Secret Manager / Vault).
3. Update in Meta/Google/Supabase if applicable.
4. Deploy new app version that reads the new secret.
5. Verify health check passes.
6. Revoke old secret.

---

## 10. Retention Sweep Operations

The retention sweep runs nightly via cron and prunes data that has exceeded its class-specific retention window.

### 10.1 Manual Dry Run

```bash
# See what WOULD be deleted without touching the database
python scripts/retention_sweep.py --dry-run
```

### 10.2 Execute Sweep Manually

```bash
# Run the sweep immediately (e.g. after changing retention windows)
python scripts/retention_sweep.py
```

### 10.3 Scheduled Cron

Add to `/etc/cron.d/flow-retention`:
```
# Flow data retention sweep — daily at 02:00 UTC
0 2 * * * flow-app-user cd /app && python scripts/retention_sweep.py >> /var/log/flow/retention.log 2>&1
```

### 10.4 Verify Sweep Audit Trail

```sql
SELECT
    id,
    actor_id,
    action,
    after,
    created_at
FROM flow.audit_events
WHERE action = 'retention_sweep'
ORDER BY created_at DESC
LIMIT 5;
```

### 10.5 Override Retention Windows

```bash
# Temporarily use shorter windows (e.g. for urgent compliance)
RETENTION_PROTECTED_DAYS=15 python scripts/retention_sweep.py --dry-run
```

> **Warning:** Never reduce the `protected` window below 7 days without legal approval.

---

## 11. Deployment Checklist

Use before every production deployment.

### Pre-deployment

- [ ] All tests pass locally: `make test`
- [ ] Linting clean: `make lint`
- [ ] No unapplied migrations: `alembic current == alembic heads`
- [ ] `.env.example` updated if new settings were added
- [ ] `docs/tasks.md` task marked `[x]`
- [ ] `docs/FINDINGS_AND_DECISIONS.md` updated with any architectural decisions

### Deployment

- [ ] Backup taken before migration: `pg_dump -U flow_migrate -d flow -F c -f backup_pre_deploy.dump`
- [ ] Migration applied with `flow_migrate` role: `make migrate`
- [ ] App server restarted with new code
- [ ] Health check passing: `curl http://localhost:8000/health`
- [ ] Smoke test: Send a test message via the simulator at `http://localhost:8000/simulator`

### Post-deployment (watch for 15 minutes)

- [ ] Error rate stable (< 0.5%)
- [ ] Latency stable (P99 < 5s)
- [ ] No unexpected moderation escalations
- [ ] Model cost per turn within expected range

### Rollback

If any post-deployment check fails:
```bash
# 1. Revert the app to the previous deployment
# 2. If migration was applied, rollback:
alembic downgrade -1
# 3. Restore from pre-deploy backup if data was corrupted
```
