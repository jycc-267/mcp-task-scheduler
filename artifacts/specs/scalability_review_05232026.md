# Scalability Review: MCP Task Scheduler

**Date:** 05/23/2026
**Reviewed by:** Scalability Reviewer (AI)
**Scope:** Full-stack review including application code and GCP infrastructure (Cloud Run, Cloud SQL, Secret Manager, IAM, Artifact Registry)

---

## Executive Summary

Since the previous review (05/11/2026), the MCP Task Scheduler has undergone a **transformative infrastructure migration** from a local SQLite-only prototype to a production-grade GCP deployment. The three most critical ceilings identified in the prior review have been directly addressed:

1. **SQLite → Cloud SQL (PostgreSQL 15)**: The single-writer bottleneck is eliminated. The database now supports hundreds of concurrent writers via Cloud SQL's managed PostgreSQL engine.
2. **DB session held during LLM call → Split-transaction pattern**: `worker_loop` now uses two short-lived `SessionLocal()` contexts with the LLM call running outside any session — exactly the fix recommended in the prior review.
3. **Single worker thread → Parameterized N-worker fan-out**: `start_scheduler(num_workers)` now spawns N worker threads, with `DEFAULT_NUM_WORKERS = 1` as a safe default.

Additionally, all six "Fix Now" code-level issues from the prior review have been resolved: LLM timeout enforcement via `ThreadPoolExecutor`, watcher exception logging, `task_list` pagination, multi-dependency warning in `nlp_task_create`, and `datetime.utcnow()` replaced with `_utcnow()`.

The **new scalability frontier** is now defined by the GCP infrastructure layer — Cloud Run concurrency limits, Cloud SQL connection pooling against IAM-authenticated connections, the in-memory `queue.Queue` broker (still not crash-persistent), and the `--allow-unauthenticated` security posture. These are the ceilings this review focuses on.

---

## What Works Well

| Decision | Why It's Good |
|---|---|
| **Cloud SQL IAM Authentication** via `google-cloud-sql-connector` ([database.py:17-44](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/database.py#L17-L44)) | No static `DATABASE_URL` secret with embedded password. The connector generates short-lived OAuth2 tokens from the Service Account's identity. Credentials rotate automatically — zero secret rotation burden. |
| **Service Account isolation** (`mcp-scheduler-runner@...`) ([deploy.sh:8](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/deploy.sh#L8)) | Least-privilege: the SA only has `roles/cloudsql.client` and `roles/cloudsql.instanceUser`. No project-wide admin. Cloud Run binds to this SA at deploy time. |
| **`--clear-secrets` on migration job** ([deploy.sh:38](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/deploy.sh#L38)) | The `mcp-db-migrate` Cloud Run Job has no lingering secret bindings. Only environment variables (`SERVICE_ACCOUNT`, `PROJECT_ID`) are passed — which are configuration, not secrets. |
| **Split-transaction worker pattern** ([scheduler.py:229-335](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/scheduler.py#L229-L335)) | Transaction 1 (mark running) → LLM call outside session → Transaction 2 (write result). Pool connections are held for milliseconds, not minutes. Directly resolves Ceiling 3 from the prior review. |
| **LLM timeout enforcement** ([scheduler.py:189-204](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/scheduler.py#L189-L204)) | `ThreadPoolExecutor` with `future.result(timeout=60)` prevents hung Gemini calls from permanently blocking a worker thread. |
| **Parameterized worker count** ([scheduler.py:341-354](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/scheduler.py#L341-L354)) | `start_scheduler(num_workers)` spawns N workers. `queue.Queue.get()` distributes work fairly across all threads. |
| **Multi-stage Docker build** ([Dockerfile:1-33](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/Dockerfile#L1-L33)) | Builder stage with `uv sync --frozen --no-dev` produces a minimal runtime image. `UV_COMPILE_BYTECODE=1` pre-compiles `.pyc` for faster cold starts. |
| **Partial index `idx_pending_scheduler`** ([models.py:35-41](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/models.py#L35-L41)) | Now uses `postgresql_where=text("status = 'pending'")` alongside `sqlite_where` for dialect-agnostic correctness. Jobs exit the index on completion — keeps it compact forever. |
| **`--no-cpu-throttling`** on Cloud Run ([deploy.sh:64](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/deploy.sh#L64)) | Watcher, Reaper, and Worker daemon threads run on background CPU even when no HTTP requests are in flight. Essential for a scheduler that polls on a timer. |
| **`--min-instances 1`** ([deploy.sh:63](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/deploy.sh#L63)) | Eliminates cold-start latency for the first SSE connection. The Watcher thread begins polling immediately on deploy. |
| **Reaper thread** ([scheduler.py:118-154](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/scheduler.py#L118-L154)) | Recovers jobs stuck in `running` for >5 minutes. Atomic `WHERE id IN (...) AND status = 'running'` update prevents race with concurrent workers. |
| **Dialect-agnostic migration** ([migrate.py:28-40](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/migrate.py#L28-L40)) | Column introspection via `sqlalchemy.inspect()` works for both SQLite and PostgreSQL. The `timezone` column migration is safe and idempotent. |
| **`GRANT ALL ... TO public`** after `create_all` ([migrate.py:17-25](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/migrate.py#L17-L25)) | Resolves the PostgreSQL 15+ `42501 permission denied` issue. Other IAM users (developers, monitoring) can query tables without needing table-level `GRANT` from the owner SA. |

---

## GCP Infrastructure Analysis

### ☁️ Cloud Run — Compute Layer

| Property | Current Setting | Assessment |
|---|---|---|
| Transport | SSE (`--transport sse --port 8080`) | Correct. Long-lived HTTP stream for MCP. |
| Min Instances | 1 | ✅ No cold start penalty. |
| CPU Throttling | Disabled (`--no-cpu-throttling`) | ✅ Required for background threads (Watcher, Reaper, Worker). |
| Authentication | `--allow-unauthenticated` | ⚠️ See Ceiling 4 below. |
| Service Account | `mcp-scheduler-runner@...` | ✅ Attached at deploy; used for IAM DB auth and Secret Manager access. |
| Concurrency | Default (80 per instance) | ⚠️ SSE connections are long-lived; 80 may exhaust in-process resources. See Ceiling 2. |
| Max Instances | Default (100) | Fine for current scale. Each instance is independent with its own Watcher/Reaper/Workers. |
| Cloud SQL Instance | `--add-cloudsql-instances` | ✅ Uses the built-in Cloud SQL Auth Proxy sidecar — no manual proxy container needed. |

**Key Risk: Horizontal scaling creates duplicate Watchers.** If Cloud Run autoscales to N instances, there will be N independent Watcher threads all polling the same `jobs` table. The `enqueued_job_ids` set is **per-process**, so different instances will enqueue the same jobs, resulting in **duplicate execution**. The `job.status == "pending"` guard in `worker_loop` Transaction 1 mitigates this (only one worker wins the `status = "running"` update), but it wastes LLM API calls if the race is lost after the LLM call completes.

---

### 🗄️ Cloud SQL — Data Layer

| Property | Current Setting | Assessment |
|---|---|---|
| Instance | `jycchien-mcp-scheduler` | Single instance, `us-central1`. |
| Database | `task-scheduler-db` | ✅ Dedicated database, not `postgres` default. |
| Engine | PostgreSQL 15+ | ✅ MVCC, full concurrent read/write. |
| Auth Mode | IAM Authentication (automatic) | ✅ No password rotation. Token-based. |
| Connection User | `mcp-scheduler-runner@PROJECT_ID.iam` (derived) | ✅ SA email → DB username via `.replace(".gserviceaccount.com", "")`. |
| Pool Size | `pool_size=1, max_overflow=2` | Conservative. See Ceiling 2. |
| Pool Recycle | `1800` seconds (30 min) | ✅ Correct for Cloud SQL Proxy which may drop idle connections. |

**IAM User Naming Convention:** The `db_user` is derived by stripping `.gserviceaccount.com` from the SA email ([database.py:27](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/database.py#L27)). This is the [documented convention](https://cloud.google.com/sql/docs/postgres/iam-authentication) for Cloud SQL IAM auth. The `GRANT` in `migrate.py` uses the `public` role — which is correct because all IAM-authenticated users are members of `public` by default.

---

### 🔐 Secret Manager — Secrets vs. Configuration

| Secret | Used By | Assessment |
|---|---|---|
| `GEMINI_API_KEY` | Cloud Run Service (`--set-secrets=GEMINI_API_KEY=GEMINI_API_KEY:latest`) | ✅ Correct. This is a true secret (API key). |
| `SERVICE_ACCOUNT` | Env var (`--set-env-vars`) | ✅ Correct. This is configuration, not a secret. |
| `PROJECT_ID` | Env var (`--set-env-vars`) | ✅ Correct. Configuration, not a secret. |
| `DATABASE_URL` | **Removed** | ✅ No longer needed. IAM auth replaces static connection strings. |

The previous architecture stored `DATABASE_URL` (containing `mcp-user` password) in Secret Manager. This has been correctly eliminated. Only `GEMINI_API_KEY` remains as a secret, which is the correct minimal surface.

---

### 🛡️ IAM — Identity & Access

| Principal | Role(s) | Scope | Assessment |
|---|---|---|---|
| `mcp-scheduler-runner` SA | `roles/cloudsql.client` | Project | ✅ Required for Cloud SQL Auth Proxy sidecar. |
| `mcp-scheduler-runner` SA | `roles/cloudsql.instanceUser` | Project | ✅ Required for IAM DB login. |
| `mcp-scheduler-runner` SA | `roles/secretmanager.secretAccessor` | `GEMINI_API_KEY` secret | ✅ Should be scoped to the specific secret, not project-wide. |
| `mcp-scheduler-runner` SA | `roles/run.invoker` (implicit) | Cloud Run Service | Default for the service's own SA. |
| Cloud Build SA | `roles/cloudbuild.builds.builder` | Project | Default for `gcloud builds submit`. |

**Least-Privilege Assessment:** The SA has the minimum roles needed. No `roles/editor` or `roles/owner` grants. The `GRANT ALL ... TO public` in `migrate.py` is broad at the PostgreSQL level but scoped to the `task-scheduler-db` database only.

---

### 📦 Artifact Registry — Container Storage

| Property | Current Setting | Assessment |
|---|---|---|
| Repository | `mcp-registry` | ✅ Dedicated repo, not default. |
| Image Path | `us-central1-docker.pkg.dev/${PROJECT_ID}/mcp-registry/mcp-task-scheduler` | ✅ Regional, versioned. |
| Build | `gcloud builds submit` (Cloud Build) | ✅ Server-side build, no local Docker required. |

No scalability concerns. Artifact Registry scales automatically.

---

## Hard Scalability Ceilings

### 🔴 Ceiling 1: `queue.Queue` — No Crash Persistence, No Cross-Instance Coordination

**The Problem:** This ceiling persists unchanged from the prior review. `GLOBAL_JOB_QUEUE` is an in-memory Python `queue.Queue`. Two compounding failure modes now exist in the GCP deployment:

1. **Crash-loss:** If the Cloud Run instance is evicted (OOM, scaling down, new revision deployment), all in-flight job IDs in the queue are lost. The Reaper recovers `running` jobs after 5 minutes, but `pending` jobs that were enqueued but not yet picked up by a worker are silently re-enqueued only after the next Watcher cycle.

2. **Multi-instance duplication:** With `--min-instances 1` and Cloud Run autoscaling, multiple instances each run their own Watcher/Worker/Queue. The same `pending` jobs get enqueued by every instance's Watcher. While the `status = "running"` guard prevents duplicate *completion*, it wastes Gemini API calls (and money) when multiple workers race to process the same job.

**Current Impact:** Immediate. Any Cloud Run instance restart (deploy, scale-in, eviction) loses queued work. Multi-instance autoscaling causes redundant LLM API costs.

**Migration Path:**
- Step 1: Add Cloud Tasks or Pub/Sub as the job broker
- Step 2: Replace `GLOBAL_JOB_QUEUE.put(job.id)` → `cloud_tasks_client.create_task()`
- Step 3: Worker becomes an HTTP handler (`/process-job`) instead of a polling loop
- Step 4: Cloud Tasks handles retry, deduplication, and delivery guarantees natively
- Step 5: Remove `enqueued_job_ids` `ThreadSafeSet` — Cloud Tasks provides visibility timeout
- Code change estimate: **High** (architectural shift from pull-based to push-based)

---

### 🔴 Ceiling 2: Cloud SQL Connection Pool Sizing vs. Cloud Run Concurrency

**The Problem:** The pool is configured with `pool_size=1, max_overflow=2` ([database.py:41-42](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/database.py#L41-L42)), yielding a maximum of **3 concurrent connections per instance**. Meanwhile:

- The Watcher thread needs 1 connection every 10 seconds
- Each Worker thread needs 2 short-lived connections per job (Transaction 1 + Transaction 2)
- The Reaper thread needs 1 connection every 5 minutes
- MCP tool calls (`task_create`, `task_list`, `task_cancel`, `task_status`) each need 1 connection

With `DEFAULT_NUM_WORKERS = 1`, the total concurrent connection demand is low (~2-3). But if `num_workers` is increased to even 3-5 workers, the 3-connection ceiling will be hit immediately, causing `QueuePool` `TimeoutError`s.

Additionally, Cloud SQL's default `max_connections` is ~100 for small instances. With Cloud Run autoscaling, if 10 instances each hold 3 connections, that's 30 connections consumed — still within limits. But with larger pool sizes and more instances, the aggregate connection count can exceed `max_connections`.

**Current Impact:** Safe with 1 worker. Breaks immediately when scaling workers.

**Migration Path:**
- Step 1: Increase `pool_size=5, max_overflow=5` for a comfortable 10-connection ceiling per instance
- Step 2: Set Cloud Run `--max-instances` to bound the aggregate connection count (`instances × pool_size ≤ max_connections`)
- Step 3: For extreme scale, use [Cloud SQL Connection Pooler](https://cloud.google.com/sql/docs/postgres/connect-connectors) or PgBouncer sidecar
- Code change estimate: **Low** (pool config change) to **Medium** (adding PgBouncer)

---

### 🟡 Ceiling 3: Single-Process Scheduler Architecture on Cloud Run

**The Problem:** The current architecture assumes a single long-running process with daemon threads (Watcher, Reaper, Workers). Cloud Run is designed for request-driven autoscaling — it can run multiple concurrent instances, and each instance runs the full scheduler stack.

This creates an **N-Watcher problem**: N instances = N Watchers, all independently querying the database for `pending` jobs. The Watcher has no distributed lock or leader election mechanism.

**Current Impact:** With `--min-instances 1`, autoscaling is rare for SSE workloads (connections are sticky). But any autoscale event or instance restart causes temporary duplication.

**Migration Path:**
- Step 1 (Short-term): Add a distributed lock on the Watcher loop using `SELECT ... FOR UPDATE SKIP LOCKED` on the `jobs` table, so only one Watcher instance claims each batch of jobs
- Step 2 (Long-term): Extract the scheduler into a dedicated Cloud Run Job (cron-triggered) or use Cloud Scheduler + Cloud Tasks to externalize the scheduling loop entirely
- Code change estimate: **Medium** (SQL-level locking) to **High** (architectural extraction)

---

### 🟡 Ceiling 4: `--allow-unauthenticated` Exposes the MCP SSE Endpoint Publicly

**The Problem:** The Cloud Run service accepts unauthenticated HTTP requests ([deploy.sh:65](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/deploy.sh#L65)). This means **anyone with the service URL** can:
- Connect to the SSE stream
- Call `task_create` to schedule arbitrary jobs (which invoke the Gemini API at the project owner's cost)
- Call `task_list` to enumerate all scheduled jobs
- Call `task_cancel` to cancel any job

**Current Impact:** Immediate security and cost risk. An attacker can create thousands of jobs, each triggering a Gemini API call, running up the API bill.

**Migration Path:**
- Step 1: Remove `--allow-unauthenticated` from `deploy.sh`
- Step 2: Use `gcloud run services add-iam-policy-binding` to grant `roles/run.invoker` only to the Claude Desktop user's Google account or a specific SA
- Step 3: Configure the MCP client to attach a bearer token (e.g., `gcloud auth print-identity-token`)
- Step 4: (Alternative) Deploy behind Cloud Endpoints or API Gateway with API key authentication
- Code change estimate: **Low** (deploy flag + IAM binding) to **Medium** (client-side auth header injection)

---

### 🟢 Ceiling 5: `GRANT ALL ... TO public` is Overly Broad

**The Problem:** The migration script grants `ALL PRIVILEGES` on all tables and sequences to the `public` role ([migrate.py:21-22](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/migrate.py#L21-L22)). In PostgreSQL, `public` includes **every** authenticated user. This means any IAM user with `roles/cloudsql.instanceUser` can `INSERT`, `UPDATE`, `DELETE`, and `TRUNCATE` any table — not just `SELECT`.

**Current Impact:** Low, since only `mcp-scheduler-runner` has `cloudsql.instanceUser`. But if you add developer IAM DB users later, they'll have full write access by default.

**Migration Path:**
- Step 1: Replace `GRANT ALL` with `GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO public`
- Step 2: Remove `DELETE` and `TRUNCATE` from the default grant
- Step 3: Use `ALTER DEFAULT PRIVILEGES` to apply grants to future tables automatically
- Code change estimate: **Low**

---

## Code-Level Issues

### Issue 1: `Connector()` is not cleaned up on application shutdown

**File:** [database.py:24](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/database.py#L24)

```python
connector = Connector()
```

The `google-cloud-sql-connector` `Connector` object starts a background thread for token refresh. It's created inside `init_connection_engine()` but never closed. On Cloud Run, this is benign (process lifecycle is the container lifecycle), but it will leak resources in tests or local dev if the engine is recreated.

**Fix:**
```python
import atexit

connector = Connector()
atexit.register(connector.close)
```

---

### Issue 2: `getconn()` closure captures mutable outer scope

**File:** [database.py:29-36](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/database.py#L29-L36)

```python
def getconn():
    return connector.connect(
        instance_connection_name,
        "pg8000",
        user=db_user,
        db=DB_NAME,
        enable_iam_auth=True
    )
```

`connector`, `instance_connection_name`, `db_user`, and `DB_NAME` are captured from the enclosing scope. If any of these module-level variables were reassigned between engine creation and connection use, the closure would silently pick up the new value. This is safe today (module-level variables are assigned once), but is fragile.

**Fix:** Not critical — document the assumption or use default arguments to capture-by-value.

---

### Issue 3: SQLite pool configuration is now unreachable dead code in Cloud Run

**File:** [database.py:59-67](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/database.py#L59-L67)

```python
# 3. Local SQLite fallback (default)
return create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 15},
    poolclass=QueuePool,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
)
```

On Cloud Run, `SERVICE_ACCOUNT` is always set, so the IAM path is always taken. The SQLite path with `pool_size=5, max_overflow=10` is only for local development. This is fine but note the asymmetry: local SQLite allows 15 connections while Cloud SQL only allows 3. If local testing uses more than 1 worker, it will pass locally but fail in production.

**Recommendation:** Align the Cloud SQL pool config with at least `pool_size=5` to match local behavior. Or better, make the pool size configurable via an environment variable.

---

### Issue 4: `TODO: Implement this function` comment is stale

**File:** [scheduler.py:58](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/scheduler.py#L58)

```python
# TODO: Implement this function
```

The function `get_time_bucket` is fully implemented (returns `scheduled_at.strftime("%Y%m%d%H")`). The TODO is misleading.

**Fix:** Remove the stale TODO block (lines 58-66).

---

### Issue 5: Worker mutation pattern — `job.status = "running"` mutates a SQLAlchemy object in-place

**File:** [scheduler.py:243](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/scheduler.py#L243)

```python
job.status = "running"
db.commit()
```

Per the project's coding standards (immutability rule), direct attribute mutation is discouraged. However, SQLAlchemy's ORM is inherently mutation-based — the unit-of-work pattern requires attribute assignment for dirty tracking. This is an **acceptable exception** to the immutability rule, as SQLAlchemy provides no immutable update API. A bulk `.update()` call would avoid loading the object but loses the `job.status != "pending"` guard.

**Verdict:** No change needed. Document as an intentional exception.

---

### Issue 6: `logs` field mutation via string concatenation

**File:** [scheduler.py:281](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/scheduler.py#L281), [scheduler.py:311](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/scheduler.py#L311), [scheduler.py:324](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/scheduler.py#L324)

```python
job.logs = (job.logs or "") + f"\n{llm_log}"
```

String concatenation for log accumulation will create progressively larger `TEXT` fields. At 1000+ retries or recurring job executions with errors, a single `logs` cell could grow to megabytes, slowing down queries that load the full `Job` row.

**Fix:** Either:
1. Use a separate `job_logs` table with a `ForeignKey` to `jobs.id` (normalized, queryable)
2. Cap the `logs` field size with a guard: `job.logs = (job.logs or "")[-4096:] + f"\n{llm_log}"`
- Code change estimate: **Low**

---

## Answers to Developer Questions

### "add partition" ([models.py:18](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/models.py#L18))

**Answer:** Now that you're on Cloud SQL (PostgreSQL), you can implement native range partitioning on `time_bucket`. However, **don't do it yet.** PostgreSQL's partial index `idx_pending_scheduler` already ensures the Watcher only scans pending jobs. Native partitioning adds maintenance complexity (partition creation, `pg_partman` automation). It becomes worthwhile only at **10M+ rows** or when you need partition-level `DROP` for data retention. Until then, the partial index is sufficient.

### "do we still need order_by at DB level? or reaper pattern?" ([scheduler.py:208](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/scheduler.py#L208))

**Answer:** **Both, and you already have both.** The answer from the prior review stands:
- `order_by(scheduled_at.asc())` → **correctness guarantee** (FIFO fairness within a Watcher poll cycle)
- Reaper → **crash-recovery guarantee** (jobs stuck in `running` are rescued after 5 minutes)

The Reaper is now implemented ([scheduler.py:118-154](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/scheduler.py#L118-L154)). `order_by` ensures deterministic processing order when multiple jobs are due in the same poll window.

### "should we persist failed job_id in the queue?" ([scheduler.py:222](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/scheduler.py#L222))

**Answer:** No, and the current design handles this correctly. The `finally` block in `worker_loop` calls `enqueued_job_ids.discard(job_id)`, which allows the Watcher to re-discover the job on its next poll if it was reset to `pending`. The Reaper handles `running` → `pending` recovery for stuck jobs. Persisting failed IDs in the queue would add complexity without benefit — the database is already the source of truth. When you migrate to Cloud Tasks (Ceiling 1), the broker will handle retry semantics natively.

### "將 get_time_bucket 改為「純日期」（%Y%m%d）" (daily vs. hourly bucket granularity) ([scheduler.py:66](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/scheduler.py#L66))

**Answer:** Keep hourly (`%Y%m%d%H`). The prior review's answer remains correct. On Cloud SQL, you also have the option of native `PARTITION BY RANGE (time_bucket)` in the future, where hourly granularity maps cleanly to partition boundaries. Daily buckets would produce partitions that are 24x larger, reducing the benefit of partition pruning.

---

## Scalability Verdict

| Component | Current Tech | Scales To | Ceiling | Migration Effort |
|---|---|---|---|---|
| Compute | Cloud Run (1 min instance, no CPU throttle) | ~80 concurrent SSE connections per instance | Autoscaling creates duplicate Watchers | Medium |
| Database | Cloud SQL PostgreSQL 15 + IAM Auth | 100+ concurrent connections (instance default) | `pool_size=1` limits per-instance usage to 3 | Low (config change) |
| Queue / Broker | `queue.Queue` (in-memory) | 1 process, no crash safety | Lost on instance eviction, no cross-instance coordination | High (Cloud Tasks/Pub/Sub) |
| Workers | N threads (configurable, default=1) | ~12-30 jobs/min per thread (LLM-bound) | Connection pool ceiling at N>3 workers | Low (pool resize) |
| LLM Timeout | 60s via `ThreadPoolExecutor` | ✅ Enforced | External API rate limits | N/A |
| Watcher Query | Partial index + range scan + `.limit(500)` | 1M+ rows | ✅ Scales well | N/A |
| Index Design | `idx_pending_scheduler` (partial) + `idx_bucket_status` | 1M+ rows | ✅ Scales well | N/A |
| NLP Parsing | Gemini structured output + Pydantic | Stateless, horizontally scalable | External API rate limits | N/A |
| Auth (Network) | `--allow-unauthenticated` | ⚠️ Open to internet | Cost/security risk | Low (deploy flag) |
| Auth (DB) | IAM automatic token rotation | ✅ No secret management | None | N/A |
| Secrets | Secret Manager (GEMINI_API_KEY only) | ✅ Correctly scoped | None | N/A |
| Container Build | Cloud Build + Artifact Registry | ✅ Fully managed | None | N/A |
| Migration | `create_all` + column introspection | ✅ Dialect-agnostic | No rollback support (no Alembic) | Medium to add Alembic |

---

## Delta from Prior Review (05/11/2026)

| Prior Ceiling / Issue | Status | How It Was Resolved |
|---|---|---|
| 🔴 Ceiling 1: SQLite single-writer | ✅ **Resolved** | Migrated to Cloud SQL PostgreSQL 15 |
| 🔴 Ceiling 2: `queue.Queue` no crash persistence | ⚠️ **Still Open** | Architecture unchanged; now compounded by Cloud Run multi-instance |
| 🔴 Ceiling 3: DB session held during LLM call | ✅ **Resolved** | Split-transaction pattern in `worker_loop` |
| 🟡 Ceiling 4: Single worker thread | ✅ **Resolved** | `start_scheduler(num_workers)` parameterized |
| Issue 1: `LLM_TIMEOUT_SECONDS` unenforced | ✅ **Fixed** | `ThreadPoolExecutor` + `future.result(timeout=60)` |
| Issue 2: Silent watcher exception swallow | ✅ **Fixed** | `logger.error(..., exc_info=True)` |
| Issue 3: `task_list` no pagination | ✅ **Fixed** | `limit`/`offset` parameters added |
| Issue 4: Multi-dependency silently dropped | ✅ **Fixed** | Warning log added |
| Issue 5: `datetime.utcnow()` deprecated | ✅ **Fixed** | Replaced with `_utcnow()` |

---

## Recommended Action Items

Priority-ordered by risk:

1. **🔴 Fix Now — Remove `--allow-unauthenticated`** ([deploy.sh:65](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/deploy.sh#L65)): The MCP endpoint is publicly accessible. Anyone can create jobs and consume Gemini API quota. Add IAM-based invocation control or an API Gateway with key auth.

2. **🔴 Fix Now — Increase Cloud SQL pool size** ([database.py:41-42](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/database.py#L41-L42)): Change `pool_size=1, max_overflow=2` to at least `pool_size=5, max_overflow=5` to support multi-worker configurations. The current 3-connection ceiling breaks the moment `num_workers > 1`.

3. **🟡 Fix Soon — Narrow `GRANT ALL` to `GRANT SELECT, INSERT, UPDATE`** ([migrate.py:21-22](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/migrate.py#L21-L22)): Remove `DELETE`, `DROP`, and `TRUNCATE` from the default public role grant.

4. **🟡 Fix Soon — Clean up stale TODO comments** ([scheduler.py:58-66](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/scheduler.py#L58-L66)): Remove the `TODO: Implement this function` block — the function is fully implemented.

5. **🟡 Fix Soon — Add `atexit.register(connector.close)`** ([database.py:24](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/database.py#L24)): Prevents resource leak on graceful shutdown and in test environments.

6. **🟡 Fix Soon — Cap `logs` field growth** ([scheduler.py:281](file:///Users/jimmychien/personal_project/build-moat-live-sessions/chatgpt_task/app/scheduler.py#L281)): Unbounded string concatenation in the `logs` column will degrade query performance for frequently-retried or long-running recurring jobs.

7. **🟢 Scale When Needed — Add `SELECT ... FOR UPDATE SKIP LOCKED`** to Watcher query: Prevents duplicate job processing across multiple Cloud Run instances. Required before setting `--max-instances > 1`.

8. **🟢 Scale When Needed — Migrate `queue.Queue` → Cloud Tasks or Pub/Sub**: Essential for crash-recovery guarantees and multi-instance coordination. This is the single largest remaining architectural limitation.

9. **🟢 Scale When Needed — Add Alembic for schema migrations**: `create_all` cannot handle column renames, type changes, or index drops. Alembic provides versioned, reversible migrations critical for production schema evolution.

10. **🟢 Scale When Needed — Extract scheduler to Cloud Run Job + Cloud Scheduler**: Move the Watcher/Reaper from always-on daemon threads to cron-triggered Cloud Run Jobs. Eliminates the N-Watcher problem and reduces idle compute costs.
