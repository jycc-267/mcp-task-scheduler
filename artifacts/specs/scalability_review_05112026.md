# Scalability Review: MCP Task Scheduler

**Date:** 05/11/2026
**Reviewed by:** Scalability Reviewer (AI)

---

## Executive Summary

The MCP Task Scheduler has made substantial architectural improvements since the prior review. The most critical thread-safety and concurrency regressions have been addressed: SQLite WAL mode is now enforced on every connection via a SQLAlchemy event listener, the Watcher is strictly read-only (eliminating per-poll write-locks), a `ThreadSafeSet` guards the in-memory `enqueued_job_ids` independently of the GIL, and the Worker now owns all DB state mutations exclusively. The "Virtual State" pattern correctly surfaces a `"queued"` status to the MCP UI without an extra DB write.

What **works well**: the partial index + range-scan query design continues to scale to 1M+ rows. The WAL upgrade meaningfully raises the read/write concurrency ceiling within a single process. The Gemini LLM integration in the worker is correctly isolated with a graceful no-op fallback. The `nlp_task_create` async MCP tool correctly delegates to the structured-output `parse_task` async path.

What is **still a hard ceiling**: The three foundational infrastructure ceilings from the prior review remain entirely in place — SQLite as the sole DB, `queue.Queue` as the broker (single-process, no persistence across crashes), and a single worker thread processing one LLM job at a time. A new, unique ceiling has been introduced: **the LLM call inside the worker holds an open SQLAlchemy session for the full duration of the Gemini API round-trip** — potentially 5–60+ seconds — which exhausts the `QueuePool` (size 5) and blocks all other DB operations on the server under any meaningful concurrent load.

---

## What Works Well

| Decision | Why It's Good |
|---|---|
| `PRAGMA journal_mode=WAL` via `@event.listens_for(engine, "connect")` | Correctly enables WAL on every new connection without relying on the caller to remember. Allows simultaneous readers with a single writer — the correct primitive for this architecture. |
| `QueuePool(pool_size=5, max_overflow=10, pool_timeout=30)` | Proper pool management prevents connection handle leaks. The 30s `pool_timeout` raises a clear `TimeoutError` instead of hanging silently. |
| Read-only Watcher | Removing the per-job `db.commit()` from the watcher loop eliminates the most frequent source of write-lock contention. Now only the Worker holds write transactions. |
| `ThreadSafeSet` with `threading.Lock()` | Correct and explicit. Does not rely on CPython's GIL. Safe for Python 3.13 free-threading mode (`-X gil=0`). |
| "Virtual State" pattern (`enqueued_job_ids` → `"queued"` overlay in API) | Delivers UX richness with zero extra DB writes. Architecturally clean. |
| `_execute_with_llm` isolated with graceful fallback | `gemini_client is None` path ensures the worker degrades to a placeholder instead of crashing. |
| `TaskSchema` validated with `model_validate_json` | Prevents malformed LLM responses from propagating as corrupt job data. |
| Partial index `idx_pending_scheduler` on `(time_bucket, scheduled_at) WHERE status = 'pending'` | Jobs exit the index the moment they complete — keeps the index tiny and fast forever. |
| `.limit(500)` on watcher query | Correct memory guard against unbounded query results. |
| `LLM_TIMEOUT_SECONDS = 60` constant defined | Defined correctly — but **not enforced anywhere in the code** (see Code-Level Issues §1). |

---

## Hard Scalability Ceilings

### 🔴 Ceiling 1: SQLite — Still a Single-Writer Database

**The Problem:** SQLite's WAL mode improves concurrent *reads*, but the fundamental constraint is unchanged: **only one writer at a time**. The current writers are: `task_create`, `task_cancel` (MCP thread), and `worker_loop` (Worker thread). Under load, they still contend for the write slot.

**Current Impact:** At ~100 writes/second (realistic at scale with recurring cron jobs spawning child jobs), write-queue latency exceeds the 15s `timeout` and `OperationalError: database is locked` errors resume.

**Migration Path:**
- Step 1: Swap `DATABASE_URL` to `postgresql+psycopg://...` in `app/database.py`
- Step 2: `uv add psycopg[binary]`
- Step 3: Replace `poolclass=QueuePool` explicit config — SQLAlchemy defaults correctly for Postgres
- Step 4: Update partial index syntax in `models.py` from `sqlite_where=` to `postgresql_where=`
- Code change estimate: **Low** (ORM layer is untouched)

---

### 🔴 Ceiling 2: `queue.Queue` — No Crash Persistence, No Multi-Process Scale-Out

**The Problem:** `GLOBAL_JOB_QUEUE` is a Python in-memory queue. Two failure modes:

1. **Crash-loss:** If the process dies after the Watcher pushes `job.id` to the queue but before the Worker completes the job, that job ID is **lost forever**. The DB still shows `status = "pending"`, so the Watcher will rediscover and re-enqueue it on restart — but only after `watcher_loop` runs again (up to `interval=10` seconds). Any job in the `running` state at crash time will **stick in `running` permanently** (Reaper needed).

2. **No horizontal scale-out:** You cannot run two worker processes pointing at the same `GLOBAL_JOB_QUEUE`. A second process would have a second, isolated queue — jobs would only be processed by whichever process the Watcher runs in.

**Current Impact:** Immediately relevant in any production deployment. A single `SIGKILL` or OOM-kill leaves all in-flight jobs orphaned.

**Migration Path:**
- Step 1: Add `redis` or `celery[redis]` dependency
- Step 2: Replace `GLOBAL_JOB_QUEUE.put(job.id)` → `redis_client.rpush("job_queue", job.id)`
- Step 3: Replace `GLOBAL_JOB_QUEUE.get()` → `redis_client.blpop("job_queue")`
- Step 4: The Reaper pattern (already designed in comments) handles jobs stuck in `running`
- Code change estimate: **Medium** (queue abstraction interface changes)

---

### 🔴 Ceiling 3: Single Worker Thread Holding Open DB Session During LLM Call

**The Problem:** This is a **new ceiling introduced with the Gemini LLM integration**. Look at `worker_loop` in `scheduler.py` lines 160–201:

```python
# scheduler.py L160-201
with SessionLocal() as db:               # ← connection checked out from QueuePool
    job = db.query(Job)...               # ← DB read
    job.status = "running"
    db.commit()

    result = _execute_with_llm(gemini_client, job.description)  # ← BLOCKS HERE 5-60+ seconds
    # The SQLAlchemy session (and its QueuePool connection) is HELD OPEN
    # for the ENTIRE duration of the Gemini API call.

    job.result = result
    job.status = "completed"
    db.commit()                          # ← connection finally returned to pool
```

The `SessionLocal()` context is held open across the entire `_execute_with_llm` call. With a `QueuePool(pool_size=5, max_overflow=10)` ceiling of 15 total connections, and each LLM call taking 5–60 seconds, **15 concurrent in-flight jobs will exhaust the pool entirely** — causing `pool_timeout=30` `TimeoutError`s on the MCP UI thread and the Watcher.

**Current Impact:** With only 1 worker thread, this isn't visible today. But it's a ticking bomb — adding even 3–5 worker threads causes immediate pool exhaustion.

**The Fix:** Split the `with SessionLocal()` block into two separate, short-lived transactions:

```python
def worker_loop():
    gemini_client = _get_gemini_client_sync()
    while True:
        job_id = GLOBAL_JOB_QUEUE.get()
        description = None
        cron_expr = None
        parent_job_id = None

        # Transaction 1: Fast read + mark running
        try:
            with SessionLocal() as db:
                job = db.query(Job).filter(Job.id == job_id).first()
                if job is None or job.status == "cancelled":
                    GLOBAL_JOB_QUEUE.task_done()
                    continue
                description = job.description
                cron_expr = job.cron_expr
                parent_job_id = job.parent_job_id
                job.status = "running"
                db.commit()
        except Exception as e:
            logger.error("Failed to start job %d: %s", job_id, e)
            GLOBAL_JOB_QUEUE.task_done()
            continue

        # LLM call runs OUTSIDE any DB session — pool is fully released
        result = f"Executed (no LLM): {description}"
        if gemini_client is not None:
            try:
                result = _execute_with_llm(gemini_client, description)
            except Exception as llm_err:
                result = f"LLM error: {llm_err}"

        # Transaction 2: Fast write of result
        try:
            with SessionLocal() as db:
                job = db.query(Job).filter(Job.id == job_id).first()
                job.result = result
                job.status = "completed"
                if cron_expr:
                    next_time = croniter(cron_expr, job.scheduled_at).get_next(datetime)
                    db.add(Job(description=description, scheduled_at=next_time,
                               time_bucket=get_time_bucket(next_time),
                               cron_expr=cron_expr, parent_job_id=parent_job_id))
                db.commit()
        except Exception as e:
            logger.error("Failed to complete job %d: %s", job_id, e)
        finally:
            enqueued_job_ids.discard(job_id)
            GLOBAL_JOB_QUEUE.task_done()
```

- Code change estimate: **Low**

---

### 🟡 Ceiling 4: Single Worker Thread — Sequential LLM Execution

**The Problem:** `start_scheduler()` spawns exactly **one** `worker_loop` thread. With a Gemini API latency of ~2–5 seconds per call, throughput is capped at ~12–30 jobs/minute.

**Current Impact:** Fine for a personal Claude Desktop integration. Becomes a bottleneck the moment there are more than a handful of scheduled tasks.

**Migration Path:**
- Step 1: Parameterize `start_scheduler(num_workers: int = 1)`
- Step 2: Spin N `worker_loop` threads in a loop
- Step 3: `queue.Queue` is already thread-safe — multiple worker threads can call `.get()` concurrently with no changes
- Prerequisite: Fix Ceiling 3 first (split DB session from LLM call), otherwise adding workers multiplies pool exhaustion
- Code change estimate: **Low**

---

## Code-Level Issues

### Issue 1: `LLM_TIMEOUT_SECONDS` is defined but never enforced

**File:** `app/scheduler.py`, line 20

```python
LLM_TIMEOUT_SECONDS = 60  # defined but never used
```

The `_execute_with_llm` function makes a synchronous `generate_content` call with no timeout argument. If Gemini's API hangs, the worker thread **blocks forever**, holding the DB session open (Ceiling 3) and never calling `task_done()`, causing `GLOBAL_JOB_QUEUE.join()` to hang indefinitely.

**Fix:**
```python
import concurrent.futures

def _execute_with_llm(client: genai.Client, description: str) -> str:
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.models.generate_content,
            model=GEMINI_MODEL,
            contents=f"Execute the following task and provide the result:\n\n{description}",
            config=genai.types.GenerateContentConfig(temperature=0.3),
        )
        try:
            response = future.result(timeout=LLM_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"Gemini API call timed out after {LLM_TIMEOUT_SECONDS}s")
    return response.text
```

---

### Issue 2: Silent swallow of all Watcher exceptions

**File:** `app/scheduler.py`, lines 108–110

```python
except Exception as e:
    # Keep watcher alive if DB encounters a transient error
    pass
```

A bare `pass` in the except block means **all errors are silently discarded** — including logic errors, import failures, and persistent DB corruption. This makes debugging production incidents nearly impossible.

**Fix:**
```python
except Exception as e:
    logger.error("Watcher loop error: %s", e, exc_info=True)
```

---

### Issue 3: `task_list` loads ALL jobs into memory with no pagination

**File:** `app/mcp_server.py`, line 78

```python
jobs = db.query(Job).order_by(Job.scheduled_at.desc()).all()
```

At 10K+ rows, this materializes the entire table into a Python list on every call. The MCP response payload also grows unbounded, potentially exceeding the MCP client's message size limit.

**Fix:** Add a `limit` and optional `offset` parameter:
```python
@mcp.tool()
def task_list(limit: int = 100, offset: int = 0) -> dict:
    """List scheduled tasks with pagination."""
    with SessionLocal() as db:
        jobs = db.query(Job).order_by(Job.scheduled_at.desc()).offset(offset).limit(limit).all()
        ...
```

---

### Issue 4: `nlp_task_create` only uses `dependencies[0]`, silently drops the rest

**File:** `app/mcp_server.py`, line 130

```python
parent_job_id = schema.dependencies[0] if schema.dependencies else None
```

`TaskSchema.dependencies` is a `list[int]`, and the LLM can return multiple dependency IDs. The current code silently drops `dependencies[1:]`. This is a data-loss bug when the user expresses a multi-dependency relationship.

**Fix:** Either enforce single-dependency in the `TaskSchema` field definition, or log a warning when multiple dependencies are found:
```python
if len(schema.dependencies) > 1:
    logger.warning(
        "nlp_task_create: %d dependencies returned, only first will be used: %s",
        len(schema.dependencies), schema.dependencies
    )
parent_job_id = schema.dependencies[0] if schema.dependencies else None
```

---

### Issue 5: `parse_task` uses deprecated `datetime.utcnow()`

**File:** `app/llm_parser.py`, line 98 and 132

```python
current_time = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
```

`datetime.utcnow()` is deprecated in Python 3.12+ and emits a `DeprecationWarning`. The project already defines `_utcnow()` in `app/models.py` for exactly this purpose.

**Fix:**
```python
from app.models import _utcnow
current_time = _utcnow().strftime("%Y-%m-%dT%H:%M:%S")
```

---

## Answers to Developer Questions

### *"scale out with this pattern, does it solve starvation when worker scale out?"*

**Answer:** Yes, for in-process scaling. Spawning N worker threads all calling `GLOBAL_JOB_QUEUE.get()` works correctly — `queue.Queue` is thread-safe and distributes work fairly across all blocked `.get()` callers. The `order_by(scheduled_at.asc())` on the Watcher query means jobs are enqueued in FIFO order, which maps directly to fair execution order. Starvation is prevented.

For cross-process scaling (multiple server instances), this does not work — see Ceiling 2. Each process would have a private queue and only process jobs enqueued by its own Watcher.

### *"do we still need order_by at DB level?"*

**Answer:** Yes. `order_by(scheduled_at.asc())` in `find_due_jobs` ensures the Watcher enqueues jobs in time-ordered sequence. `queue.Queue` is FIFO — it preserves insertion order. Therefore, DB-level ordering flows through to execution order. Removing it would cause jobs within the same polling window to execute in arbitrary (hash-map) order, reintroducing starvation risk for older jobs.

### *"or reaper pattern?"*

**Answer:** Both, for different failure modes:
- `order_by` → **correctness guarantee** (FIFO fairness within a polling cycle)
- Reaper → **crash-recovery guarantee** (jobs stuck in `running` after a process death are rescued)

The Reaper should specifically target: `status == "running" AND updated_at < now - 5min`. Implement it as a third daemon thread in `start_scheduler()`, sleeping every 10 minutes.

### *"將 get_time_bucket 改為「純日期」（%Y%m%d）..."* (daily vs. hourly bucket granularity)

**Answer:** Keep hourly (`%Y%m%d%H`). The comment itself correctly identifies the tradeoff. Daily buckets would cause a single partition to accumulate up to 24x more rows — blowing up the watcher's scan width during the reaper's catch-up phase. Hourly buckets with `time_bucket <= current_bucket` range scans are the correct design. The range-scan approach is strictly better than widening the bucket.

---

## Scalability Verdict

| Component | Current Tech | Scales To | Ceiling | Migration Effort |
|---|---|---|---|---|
| Database | SQLite + WAL | ~500 writes/sec burst | Single-writer lock | Low (swap URL + driver) |
| Queue / Broker | `queue.Queue` | 1 process, no crash safety | Lost on crash, no multi-process | Medium (Redis/SQS) |
| Workers | 1 thread | ~12–30 jobs/min (LLM-bound) | Sequential LLM calls | Low (N-thread fan-out) |
| DB Session Scope | Open during LLM call | 15 concurrent jobs | `QueuePool` exhaustion | Low (split into 2 transactions) |
| LLM Timeout | `LLM_TIMEOUT_SECONDS` (unenforced) | 1 stuck job = 1 stuck thread forever | Hung worker, hung DB session | Low (wrap in `ThreadPoolExecutor`) |
| Watcher Query | Partial index + range scan + limit(500) | 1M+ rows | ✅ Scales well | N/A |
| Index Design | `idx_pending_scheduler` (partial) | 1M+ rows | ✅ Scales well | N/A |
| NLP Parsing | Gemini structured output + Pydantic | Stateless, horizontally scalable | External API rate limits | N/A |

---

## Recommended Action Items

Priority-ordered, fix-now vs. fix-when-scaling:

1. **🔴 Fix Now — Split the DB session from the LLM call in `worker_loop`** (`scheduler.py`): The open-session-during-LLM-call pattern will exhaust `QueuePool` the instant you add a second worker thread. This is a one-time, low-risk refactor.

2. **🔴 Fix Now — Enforce `LLM_TIMEOUT_SECONDS`** (`scheduler.py` `_execute_with_llm`): A hung Gemini call will permanently block a worker thread. Wrap in `concurrent.futures.ThreadPoolExecutor` with `future.result(timeout=LLM_TIMEOUT_SECONDS)`.

3. **🔴 Fix Now — Log Watcher exceptions** (`scheduler.py` line 109): Replace `pass` with `logger.error(..., exc_info=True)`. Silent errors are production debugging nightmares.

4. **🟡 Fix Soon — Paginate `task_list`** (`mcp_server.py`): Add `limit`/`offset` parameters before the job count exceeds a few hundred rows.

5. **🟡 Fix Soon — Handle or explicitly reject multi-dependency `nlp_task_create`** (`mcp_server.py` line 130): Log a warning or add a validation error when `len(schema.dependencies) > 1`.

6. **🟡 Fix Soon — Replace `datetime.utcnow()` with `_utcnow()`** (`llm_parser.py` lines 98, 132): Eliminates deprecation warning. Trivial one-line fix.

7. **🟢 Scale When Needed — Spawn N worker threads**: After fixing items 1 and 2, parameterize `start_scheduler(num_workers: int)` and fan out to N workers for parallel LLM execution.

8. **🟢 Scale When Needed — Migrate SQLite → PostgreSQL**: The ORM layer is already database-agnostic. Swap the `DATABASE_URL` and `poolclass` when write throughput becomes measurable (>100 writes/second sustained).

9. **🟢 Scale When Needed — Migrate `queue.Queue` → Redis**: Essential before horizontal scaling (multiple server processes) or when crash-recovery guarantees become a product requirement.

10. **🟢 Scale When Needed — Implement the Reaper thread**: Recovers jobs stuck in `running` state after crashes. Target: `status == "running" AND updated_at < now - 5min`. Add as a third daemon thread in `start_scheduler()`.
