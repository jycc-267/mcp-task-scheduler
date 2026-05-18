# Implementation Plan: Migrate Persistence Layer to Supabase PostgreSQL

## Overview

The current SQLite-based persistence layer fails in the Horizon container environment because the container runtime does not grant write access to the directory where the `.db` file is being created (`sqlite:///./chatgpt_task.db` resolves to a read-only path). This plan migrates the persistence layer to a Supabase-hosted PostgreSQL database using SQLAlchemy's `postgresql+psycopg2` dialect, replacing all SQLite-specific configuration while keeping the rest of the application (MCP tools, scheduler, worker threads) fully intact.

## Codebase Context

- **Entry point**: `app/mcp_server.py` — calls `Base.metadata.create_all()` and `start_scheduler()` on startup
- **Database layer**: `app/database.py` — defines engine, `SessionLocal`, `Base`, and `get_db()`
- **Models**: `app/models.py` — defines the `Job` ORM model; contains a SQLite-specific partial index (`sqlite_where=...`)
- **Scheduler**: `app/scheduler.py` — uses `SessionLocal()` directly for watcher, worker, and reaper threads
- **Tests**: `tests/test_scheduler.py` and `tests/test_concurrency.py` — currently use in-memory SQLite (`sqlite:///:memory:`) with `StaticPool` for isolation
- **Config**: `pyproject.toml` manages dependencies via `uv`; `.env` holds `GEMINI_API_KEY`

## Requirements

- Connect to Supabase PostgreSQL using env vars: `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `DB_NAME`
- Build the `DATABASE_URL` dynamically at runtime from those env vars (never hardcode credentials)
- Remove all SQLite-specific code: `sqlite3` import, `PRAGMA` event listener, `check_same_thread`, `QueuePool` tuning for SQLite, and `sqlite_where` partial index
- Add `psycopg2-binary` as a production dependency
- Provide a `supabase/` directory with schema migration SQL that matches the current `Job` model
- Update `.env` to hold the new Postgres connection variables
- Keep tests passing using in-memory SQLite (no change to test DB — tests are unit tests and should not connect to Supabase)
- The production container must connect to Supabase on startup via `Base.metadata.create_all()`

## Architecture Changes

- **`app/database.py`**: Replace SQLite engine + PRAGMA listener with a PostgreSQL engine built from env vars; remove `sqlite3` import; use `NullPool` (safest for threaded daemon environments on Horizon)
- **`app/models.py`**: Remove `sqlite_where` from the partial index in `__table_args__`; replace with `postgresql_where`
- **`.env`**: Add five Supabase connection variables (`DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `DB_NAME`)
- **`pyproject.toml`**: Add `psycopg2-binary` as a production dependency
- **`supabase/migrations/001_initial_schema.sql`**: New file — raw SQL to create the `jobs` table and all indexes on Supabase (for audit trail / manual re-run)
- **`supabase/README.md`**: Document the Supabase folder purpose and how to apply migrations

## Technical Debt Flagged

- **`app/models.py` L38**: `sqlite_where=text("status = 'pending'")` is SQLite-only; silently ignored by PostgreSQL — must be replaced with `postgresql_where`
- **`app/scheduler.py` L130–L142**: Uses legacy `db.query()` style ORM for the reaper; inconsistent with `select()` style used in `find_due_jobs()` — flag for future cleanup
- **`tests/test_scheduler.py` L27`**: The `SessionLocal` mock patches `app.scheduler.SessionLocal` but not `app.mcp_server.SessionLocal` — acceptable for now but worth noting

---

## Implementation Steps

### Phase 1: Dependencies & Environment

1. **Add `psycopg2-binary` dependency** (File: `pyproject.toml`)
   - Action: Run `uv add psycopg2-binary` from the project root
   - Why: SQLAlchemy's `postgresql+psycopg2` dialect requires a compiled C adapter; `psycopg2-binary` bundles it without any system-level build steps — ideal for containers
   - Dependencies: None
   - Risk: Low

2. **Update `.env` with Supabase connection variables** (File: `.env`)
   - Action: Append the five DB env vars below. Keep `GEMINI_API_KEY` in place.
     ```
     DB_USER=postgres
     DB_PASSWORD=<YOUR-PASSWORD>
     DB_HOST=db.hiinyqhqzfsbmcawdsdz.supabase.co
     DB_PORT=5432
     DB_NAME=postgres
     ```
   - Why: `app/database.py` will read these at startup; they must also be set as environment variables in the Horizon deployment dashboard
   - Dependencies: None
   - Risk: Low — confirm `.env` is in `.gitignore` before committing

> [!CAUTION]
> Verify `.gitignore` includes `.env` before committing. Never commit Supabase credentials to version control.

---

### Phase 2: Core Database Layer Refactor

3. **Rewrite `app/database.py`** (File: `app/database.py`)
   - Action: Replace the entire file content with the implementation below
   - Why: Removes all SQLite-specific machinery (sqlite3 import, PRAGMA event, `check_same_thread`, `QueuePool`). Uses `NullPool` because the container's threaded daemon workers each hold long-running loops — avoids pool state corruption across threads in a containerized environment.
   - Dependencies: Step 1 (psycopg2-binary installed), Step 2 (env vars present)
   - Risk: Medium — most impactful single change; verify with connectivity smoke test before proceeding

   ```python
   import logging
   import os

   from dotenv import load_dotenv
   from sqlalchemy import create_engine
   from sqlalchemy.orm import sessionmaker, DeclarativeBase
   from sqlalchemy.pool import NullPool

   load_dotenv()

   logger = logging.getLogger(__name__)

   _DB_USER = os.environ["DB_USER"]
   _DB_PASSWORD = os.environ["DB_PASSWORD"]
   _DB_HOST = os.environ["DB_HOST"]
   _DB_PORT = os.environ["DB_PORT"]
   _DB_NAME = os.environ["DB_NAME"]

   DATABASE_URL = (
       f"postgresql+psycopg2://{_DB_USER}:{_DB_PASSWORD}"
       f"@{_DB_HOST}:{_DB_PORT}/{_DB_NAME}"
   )

   engine = create_engine(
       DATABASE_URL,
       poolclass=NullPool,   # No persistent pool: each thread opens/closes its own connection
       echo=False,
   )

   SessionLocal = sessionmaker(bind=engine)


   class Base(DeclarativeBase):
       pass


   def get_db():
       db = SessionLocal()
       try:
           yield db
       finally:
           db.close()
   ```

   > **Note on `NullPool`:** Horizon runs the scheduler's watcher/worker/reaper as daemon threads with long-running loops. Using a persistent `QueuePool` risks connections being checked back to the pool in an inconsistent state after a thread crash. `NullPool` gives each `SessionLocal()` call a fresh, auto-closed connection.

4. **Fix the partial index in `app/models.py`** (File: `app/models.py`)
   - Action: Replace `sqlite_where=text("status = 'pending'")` with the PostgreSQL-compatible equivalent
   - Why: The `sqlite_where` kwarg is a SQLite dialect extension. PostgreSQL uses `postgresql_where`. Without this fix, `Base.metadata.create_all()` will silently skip the partial index.
   - Dependencies: Step 3
   - Risk: Low

   Change `__table_args__` from:
   ```python
   __table_args__ = (
       Index(
           "idx_pending_scheduler",
           "time_bucket",
           "scheduled_at",
           sqlite_where=text("status = 'pending'")
       ),
       Index("idx_bucket_status", "time_bucket", "status"),
   )
   ```
   To:
   ```python
   __table_args__ = (
       Index(
           "idx_pending_scheduler",
           "time_bucket",
           "scheduled_at",
           postgresql_where=text("status = 'pending'"),
       ),
       Index("idx_bucket_status", "time_bucket", "status"),
   )
   ```

---

### Phase 3: Supabase Folder & Migration SQL

5. **Create `supabase/` directory structure** (Files: `supabase/README.md`, `supabase/migrations/001_initial_schema.sql`)
   - Action: Create the folder and two files described below
   - Why: Provides a human-readable audit trail of the schema applied to Supabase. If the project is ever reset or tables are dropped, this SQL can be replayed manually via the Supabase SQL Editor.
   - Dependencies: Step 4 (model finalized before writing DDL)
   - Risk: Low

   **`supabase/README.md`**:
   ```markdown
   # Supabase Migrations

   This folder contains raw SQL migration files that mirror the SQLAlchemy
   ORM models in `app/models.py`. They are applied automatically by
   `Base.metadata.create_all()` on first server startup, but are kept here
   as a human-readable audit trail.

   ## Applying Manually

   1. Open the Supabase Dashboard → SQL Editor
   2. Paste and run `migrations/001_initial_schema.sql`

   ## Connection Details

   The application connects using env vars defined in `.env`:
   DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
   ```

   **`supabase/migrations/001_initial_schema.sql`**:
   ```sql
   -- MCP Task Scheduler: Initial Schema
   -- Mirrors app/models.py Job model

   CREATE TABLE IF NOT EXISTS jobs (
       id              SERIAL PRIMARY KEY,
       time_bucket     VARCHAR(10)  NOT NULL,
       description     TEXT         NOT NULL,
       scheduled_at    TIMESTAMP    NOT NULL,
       status          VARCHAR(20)  DEFAULT 'pending',
       result          TEXT,
       cron_expr       VARCHAR(100),
       parent_job_id   INTEGER REFERENCES jobs(id),
       logs            TEXT,
       created_at      TIMESTAMP    DEFAULT NOW(),
       updated_at      TIMESTAMP    DEFAULT NOW()
   );

   -- Partial index: only indexes pending jobs (mirrors ORM model)
   CREATE INDEX IF NOT EXISTS idx_pending_scheduler
       ON jobs (time_bucket, scheduled_at)
       WHERE status = 'pending';

   -- Composite index for bucket + status queries
   CREATE INDEX IF NOT EXISTS idx_bucket_status
       ON jobs (time_bucket, status);
   ```

---

### Phase 4: Test Suite Update

6. **Verify `tests/` still pass** (File: `tests/test_scheduler.py`)
   - Action: No change needed — tests already use `sqlite:///:memory:` which is correct for unit tests. Run `uv run pytest tests/` to confirm.
   - Why: Unit tests must remain hermetic and never connect to Supabase.
   - Dependencies: Steps 3–4
   - Risk: Low

7. **Add connectivity smoke test** (File: `connection_tests/test_pg_connect.py`)
   - Action: Create a lightweight script that imports `engine` and executes `SELECT 1`; run manually before deploying to Horizon
   - Why: Validates Supabase credentials, SSL, and network routing before the full container deployment
   - Dependencies: Steps 1–4
   - Risk: Low

   ```python
   """Manual connectivity smoke test — not part of the pytest suite.
   Run with: uv run python connection_tests/test_pg_connect.py
   """
   import logging
   from sqlalchemy import text
   from app.database import engine

   logging.basicConfig(level=logging.INFO)
   logger = logging.getLogger(__name__)

   def main() -> None:
       with engine.connect() as conn:
           result = conn.execute(text("SELECT 1"))
           logger.info("Supabase connection OK: %s", result.scalar())

   if __name__ == "__main__":
       main()
   ```

---

## Testing Strategy

| Layer | Tool | Notes |
|---|---|---|
| Unit tests | `uv run pytest tests/` | Use in-memory SQLite — no Supabase dependency |
| Connectivity | `uv run python connection_tests/test_pg_connect.py` | Manual; run after Phase 1–2 |
| Schema validation | Supabase Dashboard → Table Editor | Confirm `jobs` table created after first startup |
| End-to-end | Claude Code Desktop → `task_create` | Success = valid `job_id` with no `OperationalError` |

---

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Supabase requires TLS and connection is rejected | Append `?sslmode=require` to `DATABASE_URL` in `app/database.py` |
| `DB_PASSWORD` not set in Horizon → `KeyError` on startup | `os.environ["DB_*"]` raises `KeyError` with a clear var name; add Horizon env vars before redeploy |
| Existing SQLite data not migrated | Not in scope — Horizon is a fresh environment; no data migration needed |
| `Base.metadata.create_all()` race on cold start | Already handled — `mcp_server.py` calls `create_all()` synchronously before `start_scheduler()` |
| `psycopg2-binary` incompatibility with Python 3.13 | Use `psycopg2-binary>=2.9.9`; `uv add` resolves a compatible version automatically |

---

## Success Criteria

- [ ] `uv add psycopg2-binary` completes without error and `uv.lock` is updated
- [ ] `connection_tests/test_pg_connect.py` prints `Supabase connection OK: 1`
- [ ] `uv run pytest tests/` passes with 0 failures
- [ ] `jobs` table visible in Supabase Dashboard → Table Editor after first Horizon startup
- [ ] `task_create` via Claude Code Desktop returns a valid `job_id` with no `OperationalError`
- [ ] `task_list` returns jobs that persist across container restarts (confirming Supabase durability)
