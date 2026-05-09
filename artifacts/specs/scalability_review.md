# Scalability Review: ChatGPT Task Scheduler

## Summary

Your design thinking is strong — the partial index, range-scan on `time_bucket <=`, FIFO ordering, `limit(500)`, and the Reaper concept all show you're reasoning about the right failure modes. However, **three hard ceilings in your current dependency stack will block scalability before any of those optimizations matter**.

---

## What You Got Right

| Decision | Why It's Good |
|---|---|
| `time_bucket <= current_bucket` range scan | Catches stale jobs from past hours — proper fault tolerance |
| Partial index `idx_pending_scheduler` with `sqlite_where` | Brilliant for this workload — index stays tiny as completed jobs fall out |
| `.limit(500)` on watcher query | Prevents OOM from unbounded result sets |
| `.order_by(scheduled_at.asc())` | Guarantees FIFO, prevents starvation of old jobs |
| Reaper concept in comments | Correct separation of "hot path" vs. "catch-up" concerns |

---

## Hard Scalability Ceilings

### 🔴 Ceiling 1: SQLite — Single-Writer Lock

**The Problem:** SQLite uses a file-level write lock. Only one connection can write at a time. Your system has **three concurrent writers**: Watcher (sets `queued`), Worker (sets `running` → `completed`), and MCP server (creates jobs). Under load, they'll serialize on the write lock and you'll see `database is locked` errors.

**Why it matters now:** Even at ~50 jobs/second, the contention becomes measurable. Your `check_same_thread=False` flag only permits cross-thread *access* — it doesn't solve write contention.

**Migration path:**
```
SQLite (prototype) → PostgreSQL (production)
```
- Swap `DATABASE_URL` to `postgresql+psycopg://...`
- Add `psycopg[binary]` to dependencies
- Your SQLAlchemy ORM code stays ~95% the same
- The partial index syntax changes slightly (`postgresql_where` instead of `sqlite_where`)

### 🔴 Ceiling 2: `queue.Queue` — Single-Process, No Persistence

**The Problem:** Your in-memory `job_queue` is:
1. **Lost on crash** — if the process dies after `status=queued` but before the worker processes the job, those jobs are orphaned (stuck in `queued` forever).
2. **Single-process only** — you can't scale to multiple worker processes or machines.

**Why your Reaper idea partially solves this:** A Reaper that sweeps `status == "queued" AND updated_at < now - 5min` would recover orphaned jobs. But it's a band-aid.

**Migration path:**
```
queue.Queue (prototype) → Redis/SQS/RabbitMQ (production)
```
- Redis with `rpush`/`blpop` is the simplest drop-in
- SQS if you want managed + at-least-once delivery guarantees

### 🔴 Ceiling 3: Single Watcher + Single Worker Threads

**The Problem:** `start_scheduler()` launches exactly **one** watcher and **one** worker thread. The worker processes jobs sequentially — one at a time.

**Your comment asks the right question:**
> *"scale out with this pattern, does it solve starvation when worker scale out?"*

**Answer:** With the in-memory queue, scaling workers within the same process is trivial (spawn N worker threads). But scaling **across processes/machines** requires replacing `queue.Queue` (see Ceiling 2). The `order_by` at DB level is still valuable — it ensures the watcher **enqueues** jobs in the right order. The queue preserves insertion order, so DB-level ordering flows through to execution order.

---

## Watcher Loop — Mutation Bug

```python
# scheduler.py L79-82 — current code
for job in due_jobs:
    job.status = "queued"    # ← mutates the ORM object directly
    db.commit()              # ← commits ONE job at a time (N commits for N jobs)
    job_queue.put(job.id)
```

**Two issues:**

1. **Violates your coding standard's immutability rule.** You're mutating `job.status` in-place. For ORM objects, this is idiomatic SQLAlchemy and hard to avoid entirely, but you should at least batch the operation.

2. **N individual commits.** If there are 500 due jobs, that's 500 separate transactions (500 `fsync` calls on SQLite). This will be extremely slow.

**Suggested fix — batch commit:**
```python
for job in due_jobs:
    job.status = "queued"
db.commit()  # single commit for all status changes
for job in due_jobs:
    job_queue.put(job.id)
```

> ⚠️ **Warning:** If the process crashes between `db.commit()` and enqueueing all IDs, some jobs will be stuck in `queued`. This is exactly where the Reaper pattern you noted becomes essential.

---

## Worker Loop — Silent Failure on Exception

```python
# scheduler.py L107-110
except Exception as e:
    job.status = "failed"
    job.result = str(e)
    db.commit()          # ← what if THIS commit fails? (e.g., DB locked)
```

If the `db.commit()` inside the `except` block itself fails, the exception propagates unhandled, the `finally` block calls `db.close()` (discarding the change), and the job stays in `running` forever. Add a nested try:

```python
except Exception as e:
    try:
        job.status = "failed"
        job.result = str(e)
        db.commit()
    except Exception:
        db.rollback()
        # log the error — job stays "running", Reaper should catch it
```

---

## Partial Index — Verify It Actually Works

Your partial index definition:
```python
sqlite_where = (status == "pending")
```

> ℹ️ **Important:** At class body scope, `status` here resolves to the **`mapped_column` descriptor**, not a SQL expression. SQLAlchemy *should* handle this correctly for index definitions, but you should verify the generated DDL. Run:
> ```bash
> uv run python -c "from app.database import Base, engine; Base.metadata.create_all(engine)"
> ```
> Then inspect the schema with `sqlite3 chatgpt_task.db ".schema jobs"` to confirm the `WHERE` clause appears in the index DDL.

---

## Answers to Inline Design Questions

### *"do we still need `order_by` at DB level?"*
**Yes.** Even if you switch to a FIFO queue (Redis `rpush`/`blpop`), the `order_by` ensures the watcher **enqueues** jobs in the right order. The queue preserves insertion order, so DB-level ordering flows through to execution order.

### *"or reaper pattern?"*
**Both.** They solve different problems:
- `order_by` → **correctness** (FIFO guarantee)
- Reaper → **reliability** (recover from crashes, stuck jobs)

### *"soft delete? or should I set `deleted_at`?"* (from `mcp_server.py`)
For a scheduler, `status = "cancelled"` is the right approach (soft-delete by status). Adding `deleted_at` is useful if you need TTL-based cleanup later (e.g., `DELETE FROM jobs WHERE deleted_at < now() - interval '30 days'`). It's a good addition but not urgent.

---

## Scalability Verdict

| Component | Current | Scales To | Ceiling |
|---|---|---|---|
| Database | SQLite | ~100 writes/sec | Single-writer lock |
| Queue | `queue.Queue` | 1 process | Lost on crash, no multi-process |
| Workers | 1 thread | 1 job at a time | CPU-bound on single thread |
| Watcher | 1 thread | Adequate | N/A (polling is lightweight) |
| Indexing | Partial index | 1M+ rows | ✅ Scales well |
| Query design | Range scan + limit | 1M+ rows | ✅ Scales well |

**Bottom line:** Your **query-layer design** (indexing, partitioning, range scans) scales well. Your **infrastructure layer** (SQLite, in-memory queue, single worker) is the bottleneck. The good news: swapping SQLite → PostgreSQL and `queue.Queue` → Redis are isolated changes that don't require rearchitecting your application logic.
