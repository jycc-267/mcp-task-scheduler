# Implementation Plan: Migrate SQLite → Google Cloud SQL (PostgreSQL)

## Overview

The MCP task scheduler currently uses a file-based SQLite database whose relative path fails in containerised environments (Horizon) where the working directory is read-only. This plan migrates the persistence layer to **Google Cloud SQL for PostgreSQL**, giving the Horizon-hosted MCP server a durable, managed database while keeping the existing SQLAlchemy ORM, threading model, and all business logic intact. Local development continues to work against either SQLite or a local PostgreSQL instance via the `DATABASE_URL` environment variable.

---

## Codebase Context

| Item | Detail |
|---|---|
| **Language / Runtime** | Python 3.13, managed with `uv` |
| **ORM** | SQLAlchemy 2.0.36 (Core + ORM) |
| **Current DB** | SQLite via `sqlite:///./chatgpt_task.db` (relative path, breaks on Horizon) |
| **DB Layer** | `app/database.py` — engine, session factory, WAL pragma listener |
| **Models** | `app/models.py` — single `jobs` table with two partial/composite indexes |
| **Concurrency** | `app/scheduler.py` — watcher, reaper, worker threads; `QueuePool` with `pool_size=5, max_overflow=10` |
| **Tests** | In-memory SQLite (`sqlite:///:memory:`) with `StaticPool`; patching `SessionLocal` |
| **SQLite-specific code** | `set_sqlite_pragma` event listener (WAL/synchronous) in `database.py`; `sqlite_where` partial index in `models.py` |

---

## Requirements

- The Horizon-hosted MCP server must connect to Cloud SQL PostgreSQL using the **Cloud SQL Auth Proxy** (recommended by Google for Horizon/Cloud Run workloads).
- Local development must continue to work with **zero friction** — developers should not need to run the proxy locally.
- The `DATABASE_URL` must be injected via an environment variable / secret, never hardcoded.
- All existing SQLAlchemy ORM queries must continue to work unchanged (both databases are SQL-compatible).
- The partial index (`sqlite_where`) must be replaced with a PostgreSQL-compatible `WHERE` clause partial index.
- Unit tests must remain fast and database-agnostic (in-memory SQLite or PostgreSQL).
- No mutation of existing objects; all new patterns must follow the established immutability and error-handling conventions.

---

## Architecture Changes

| # | File | Change |
|---|---|---|
| 1 | `app/database.py` | Remove SQLite WAL pragma listener; make engine fully configurable from `DATABASE_URL`; add `pg8000` / `psycopg2` dialect support; ensure graceful fallback for local SQLite |
| 2 | `app/models.py` | Replace `sqlite_where=` partial index with a dialect-agnostic `postgresql_where=` (+ keep SQLite compat for local dev) |
| 3 | `pyproject.toml` | Add `pg8000` (pure-Python, no system lib needed) as production dependency; keep SQLite for dev/test |
| 4 | `app/mcp_server.py` | No logic changes; environment variable loading already present via `dotenv` |
| 5 | `tests/conftest.py` *(new)* | Centralise the test `engine`/`SessionLocal` fixture so both test files share one source of truth |
| 6 | `tests/test_scheduler.py` | Minor import cleanup to use `conftest` fixtures |
| 7 | `tests/test_concurrency.py` | Same fixture migration |
| 8 | `.env.example` *(new)* | Document all required environment variables for new contributors |
| 9 | `Dockerfile` *(new)* | Production container: Cloud SQL Auth Proxy as a sidecar OR inline startup script |
| 10 | `cloudsql/startup.sh` *(new)* | Startup script that launches the Auth Proxy then the MCP server process |

---

## Implementation Steps

### Phase 1: Cloud SQL Infrastructure Provisioning (GCP Console / gcloud CLI)

> **No code changes in this phase.** This is a one-time GCP setup. Can be done in parallel with Phase 2.

1. **Create Cloud SQL Instance** (GCP Console or `gcloud`)
   - Action: `gcloud sql instances create mcp-scheduler-db --database-version=POSTGRES_16 --tier=db-f1-micro --region=us-central1 --storage-type=SSD`
   - Why: Establishes the managed PostgreSQL host.
   - Dependencies: GCP project with billing enabled.
   - Risk: Low — standard GCP operation.

2. **Create Database and User**
   - Action:
     ```bash
     gcloud sql databases create chatgpt_task --instance=mcp-scheduler-db
     gcloud sql users create mcp_user --instance=mcp-scheduler-db --password=<STRONG_RANDOM_PW>
     ```
   - Why: Isolates the application's schema and credentials.
   - Dependencies: Step 1.
   - Risk: Low.

3. **Store Credentials in Google Secret Manager**
   - Action: Create secret `MCP_DATABASE_URL` with value `postgresql+pg8000://mcp_user:<PW>@localhost:5432/chatgpt_task` (note: `localhost` because the Auth Proxy runs as a sidecar on the same pod/container).
   - Why: Avoids hardcoded secrets; Horizon can inject them natively.
   - Dependencies: Steps 1–2.
   - Risk: Low.

4. **Grant Cloud SQL Client Role to the Horizon Service Account**
   - Action: `gcloud projects add-iam-policy-binding <PROJECT_ID> --member=serviceAccount:<HORIZON_SA> --role=roles/cloudsql.client`
   - Why: The Auth Proxy authenticates with IAM; no database password is needed in the connection string when using IAM auth (optional advanced step).
   - Dependencies: Step 3.
   - Risk: Low–Medium — requires knowing the Horizon service account email.

---

### Phase 2: Python Dependency Update

5. **Add `pg8000` driver** (`pyproject.toml`)
   - File: `pyproject.toml`
   - Action: `uv add pg8000`
   - Why: `pg8000` is a pure-Python PostgreSQL driver — no `libpq` system library required, so it works in any container without extra OS packages. Avoids the complexity of `psycopg2-binary`.
   - Dependencies: None.
   - Risk: Low. `pg8000` is fully SQLAlchemy 2.x compatible.

   ```toml
   # pyproject.toml — after change
   dependencies = [
       "croniter>=6.2.2",
       "fastmcp>=3.2.4",
       "google-genai>=2.0.1",
       "mcp>=1.0.0",
       "pg8000>=1.31.2",          # <-- ADD
       "python-dotenv>=1.2.2",
       "sqlalchemy==2.0.36",
   ]
   ```

---

### Phase 3: `app/database.py` Refactor

6. **Make engine fully dialect-aware via `DATABASE_URL`** (`app/database.py`)
   - File: `app/database.py`
   - Action: Replace the hardcoded SQLite URL with `os.environ.get("DATABASE_URL")`. Guard the WAL pragma listener so it only fires for SQLite connections (keep it for local dev). Remove relative-path assumption. Add parent-directory auto-creation only for `sqlite:///` URLs.
   - Why: A single code path that adapts to SQLite (local/test) or PostgreSQL (production) based purely on the URL dialect prefix.
   - Dependencies: Step 5.
   - Risk: Medium — this is the core change; test thoroughly.

   ```python
   # app/database.py — target implementation
   import logging
   import os
   import sqlite3
   from pathlib import Path

   from sqlalchemy import create_engine, event
   from sqlalchemy.orm import sessionmaker, DeclarativeBase
   from sqlalchemy.pool import QueuePool

   logger = logging.getLogger(__name__)

   DATABASE_URL = os.environ.get("DATABASE_URL")
   if not DATABASE_URL:
       raise RuntimeError(
           "DATABASE_URL environment variable is not set. "
           "Set it to a SQLite path (local dev) or a Cloud SQL PostgreSQL URL (production)."
       )

   # Auto-create parent directory for SQLite file-based URLs only
   if DATABASE_URL.startswith("sqlite:///") and not DATABASE_URL.startswith("sqlite:///:memory:"):
       _db_file = DATABASE_URL.replace("sqlite:///", "", 1)
       Path(_db_file).parent.mkdir(parents=True, exist_ok=True)
       logger.info("SQLite database path: %s", _db_file)

   _is_sqlite = DATABASE_URL.startswith("sqlite")

   engine = create_engine(
       DATABASE_URL,
       connect_args={"check_same_thread": False} if _is_sqlite else {},
       poolclass=QueuePool,
       pool_size=5,
       max_overflow=10,
       pool_timeout=30,
   )

   @event.listens_for(engine, "connect")
   def set_sqlite_pragma(dbapi_connection, connection_record):
       """Enable WAL mode for SQLite only — improves concurrent read/write performance."""
       if isinstance(dbapi_connection, sqlite3.Connection):
           cursor = dbapi_connection.cursor()
           cursor.execute("PRAGMA journal_mode=WAL")
           cursor.execute("PRAGMA synchronous=NORMAL")
           cursor.close()

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

   > **Note:** The `RuntimeError` on missing `DATABASE_URL` is intentional — fail fast so misconfiguration is caught at startup, not mid-request.

---

### Phase 4: `app/models.py` — Dialect-Agnostic Indexes

7. **Replace `sqlite_where` partial index with dialect-aware version** (`app/models.py`)
   - File: `app/models.py`
   - Action: Duplicate the partial index definition — keep `sqlite_where` for SQLite and add `postgresql_where` for PostgreSQL. SQLAlchemy will pick the correct dialect-specific argument at DDL generation time.
   - Why: `sqlite_where=text(...)` is a dialect-specific keyword argument. PostgreSQL partial indexes use `postgresql_where=`. Both are needed to support local SQLite dev and production PostgreSQL.
   - Dependencies: Step 6.
   - Risk: Low — purely DDL; no runtime query changes.

   ```python
   # app/models.py — __table_args__ replacement
   __table_args__ = (
       Index(
           "idx_pending_scheduler",
           "time_bucket",
           "scheduled_at",
           sqlite_where=text("status = 'pending'"),        # SQLite (local dev)
           postgresql_where=text("status = 'pending'"),    # PostgreSQL (production)
       ),
       Index("idx_bucket_status", "time_bucket", "status"),
   )
   ```

---

### Phase 5: Test Infrastructure — `conftest.py`

8. **Create `tests/conftest.py`** to centralise fixture setup
   - File: `tests/conftest.py` *(new file)*
   - Action: Move the shared `test_engine`, `TestSessionLocal`, and `setup_database` fixture out of `test_scheduler.py` into `conftest.py`. Both test files will auto-discover it.
   - Why: Eliminates fixture duplication and ensures `test_concurrency.py` also benefits from a clean, shared in-memory engine.
   - Dependencies: None (pure refactor within tests).
   - Risk: Low.

   ```python
   # tests/conftest.py
   import pytest
   from unittest.mock import patch
   from sqlalchemy import create_engine
   from sqlalchemy.orm import sessionmaker
   from sqlalchemy.pool import StaticPool

   from app.database import Base

   @pytest.fixture(scope="function", autouse=True)
   def setup_database():
       test_engine = create_engine(
           "sqlite:///:memory:",
           connect_args={"check_same_thread": False},
           poolclass=StaticPool,
       )
       TestSessionLocal = sessionmaker(bind=test_engine)
       Base.metadata.create_all(bind=test_engine)

       with patch("app.scheduler.SessionLocal", new=TestSessionLocal):
           db = TestSessionLocal()
           yield db
           db.close()

       Base.metadata.drop_all(bind=test_engine)
   ```

9. **Update `tests/test_scheduler.py`** — remove duplicated fixture
   - File: `tests/test_scheduler.py`
   - Action: Delete the `test_engine`, `TestSessionLocal`, and `setup_database` definitions at the top of the file. The `conftest.py` fixture is auto-applied.
   - Dependencies: Step 8.
   - Risk: Low.

10. **Update `tests/test_concurrency.py`** — same cleanup
    - File: `tests/test_concurrency.py`
    - Action: Same as Step 9 — remove any duplicated engine/fixture setup.
    - Dependencies: Step 8.
    - Risk: Low.

---

### Phase 6: `.env.example` and Local Dev Ergonomics

11. **Create `.env.example`**
    - File: `.env.example` *(new file)*
    - Action: Document all required and optional environment variables.
    - Why: Removes onboarding friction; makes it obvious what secrets are needed.
    - Risk: None.

    ```dotenv
    # .env.example — copy to .env and fill in values

    # Required: Database connection string
    # Local SQLite (dev/test):
    DATABASE_URL=sqlite:///./chatgpt_task.db
    # Production (Cloud SQL via Auth Proxy):
    # DATABASE_URL=postgresql+pg8000://mcp_user:PASSWORD@localhost:5432/chatgpt_task

    # Required: Gemini API key for LLM task execution
    GEMINI_API_KEY=your-api-key-here
    ```

12. **Update `.env`** (local developer copy — NOT committed)
    - File: `.env`
    - Action: Add `DATABASE_URL=sqlite:///./chatgpt_task.db` to ensure the app starts correctly with the new mandatory `DATABASE_URL` check.
    - Dependencies: Step 6.
    - Risk: Low — purely local.

---

### Phase 7: Production Container & Cloud SQL Auth Proxy

13. **Create `Dockerfile`**
    - File: `Dockerfile` *(new file)*
    - Action: Multi-stage build: install Python deps with `uv`, copy source, download the Cloud SQL Auth Proxy binary, write a startup script that launches the proxy in the background then starts the MCP server.
    - Why: The Auth Proxy establishes a secure, IAM-authenticated tunnel to Cloud SQL on `localhost:5432` — no TLS certificates or DB passwords needed in the container.
    - Dependencies: Steps 5–6.
    - Risk: Medium — the proxy must start before the Python app attempts to connect.

    ```dockerfile
    FROM python:3.13-slim

    WORKDIR /app

    RUN pip install uv

    COPY pyproject.toml uv.lock ./
    RUN uv sync --frozen --no-dev

    COPY app/ ./app/

    # Download Cloud SQL Auth Proxy v2
    ADD https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.14.1/cloud-sql-proxy.linux.amd64 /usr/local/bin/cloud-sql-proxy
    RUN chmod +x /usr/local/bin/cloud-sql-proxy

    COPY cloudsql/startup.sh /startup.sh
    RUN chmod +x /startup.sh

    ENV PORT=8000
    EXPOSE 8000

    CMD ["/startup.sh"]
    ```

14. **Create `cloudsql/startup.sh`**
    - File: `cloudsql/startup.sh` *(new file)*
    - Action: Shell script that starts the Auth Proxy in the background, waits for the socket to be ready, then exec's the MCP server.
    - Dependencies: Step 13, Phase 1 (Cloud SQL instance connection name).
    - Risk: Medium — race condition between proxy startup and app startup; mitigated by a wait loop.

    ```bash
    #!/bin/sh
    set -e

    # CLOUD_SQL_INSTANCE must be set as an environment variable on Horizon
    # Format: PROJECT:REGION:INSTANCE  e.g. my-project:us-central1:mcp-scheduler-db
    echo "Starting Cloud SQL Auth Proxy for instance: ${CLOUD_SQL_INSTANCE}"
    /usr/local/bin/cloud-sql-proxy "${CLOUD_SQL_INSTANCE}" --port=5432 &

    # Wait until the proxy is ready (max 30s)
    WAIT_SECONDS=30
    ELAPSED=0
    until nc -z localhost 5432 || [ "$ELAPSED" -ge "$WAIT_SECONDS" ]; do
        sleep 1
        ELAPSED=$((ELAPSED + 1))
    done

    if ! nc -z localhost 5432; then
        echo "ERROR: Cloud SQL Auth Proxy did not start within ${WAIT_SECONDS}s" >&2
        exit 1
    fi

    echo "Proxy ready. Starting MCP server..."
    exec uv run python -m app.mcp_server --transport sse --host 0.0.0.0 --port "${PORT:-8000}"
    ```

---

### Phase 8: Horizon Deployment Configuration

15. **Set environment variables on Horizon**
    - Action: In the Horizon dashboard, set:
      - `DATABASE_URL` → value from Secret Manager (`MCP_DATABASE_URL`)
      - `CLOUD_SQL_INSTANCE` → `<PROJECT>:<REGION>:mcp-scheduler-db`
      - `GEMINI_API_KEY` → existing secret
    - Why: Secrets must never be in the Docker image or source control.
    - Dependencies: Phases 1 and 7.
    - Risk: Low.

16. **Run schema migration on first deploy**
    - Action: `Base.metadata.create_all(bind=engine)` is already called in `mcp_server.main()` — this will auto-create tables in Cloud SQL on first startup. No separate migration runner needed at this stage.
    - Why: The existing `create_all` call is sufficient for a greenfield Cloud SQL instance.
    - Dependencies: Step 15.
    - Risk: Low — idempotent; won't drop existing tables.

    > **Note:** If schema changes (column additions, renames) become necessary in future, introduce **Alembic** as a follow-up. For the current single-table schema, `create_all` is sufficient.

---

## Testing Strategy

| Type | Target | Tool |
|---|---|---|
| **Unit (unchanged)** | Watcher, reaper, worker logic | `pytest` + `sqlite:///:memory:` + `StaticPool` |
| **Dialect validation** | Index DDL generates correctly for both dialects | `sqlalchemy.dialects` DDL rendering in a new `test_models.py` |
| **Integration (new)** | Full create → schedule → execute flow against a local PostgreSQL | `pytest` + `docker compose` with `postgres:16` |
| **Smoke test (deploy)** | Call `task_create` via Claude Code Desktop after deploy | Manual / Claude MCP client |

---

## Risks & Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Auth Proxy race condition at container startup | Medium | `startup.sh` health-check loop with 30s timeout + hard failure |
| Cloud SQL connection limit exhaustion | Medium | `pool_size=5, max_overflow=10` already capped; `db-f1-micro` supports ~25 connections |
| `pg8000` incompatibility with a specific SQLAlchemy query | Low | `pg8000` is the officially recommended pure-Python driver for SQLAlchemy 2.x on Cloud SQL |
| Partial index `postgresql_where` silently ignored on SQLite | None | SQLAlchemy silently ignores unknown dialect keywords — both kwargs coexist safely |
| Missing `DATABASE_URL` at startup | Low | `RuntimeError` at import time fails fast with a clear message |
| Data loss when `/tmp` SQLite is used during transition | None | Plan removes any `/tmp` default — `DATABASE_URL` is now mandatory |
| Test fixture `autouse=True` affecting unrelated tests | Low | `scope="function"` ensures full isolation per test |

---

## Technical Debt Flagged

- `app/scheduler.py` — `worker_loop` is 110+ lines; should be split into `_start_job()`, `_finalize_job()`, and `_handle_cron_recurrence()` helpers (>50 line rule violation).
- `app/models.py` — comment in Chinese should be translated or removed for broader team readability.
- `tests/test_scheduler.py` — `setup_database` fixture duplicated between test files (resolved in Phase 5).
- No `Alembic` migration tool — acceptable for MVP but required before any schema changes in production.

---

## Success Criteria

- [ ] `uv run python -m app.mcp_server --transport sse` starts without error when `DATABASE_URL` points to Cloud SQL via Auth Proxy on Horizon.
- [ ] `task_create` MCP tool successfully inserts a row into Cloud SQL PostgreSQL.
- [ ] `task_status` and `task_list` correctly read from Cloud SQL.
- [ ] All 5 existing unit tests pass (`uv run pytest tests/ -q`).
- [ ] `DATABASE_URL=sqlite:///./chatgpt_task.db uv run python -m app.mcp_server` continues to work locally.
- [ ] No database credentials appear in source code, Docker image layers, or Horizon logs.
- [ ] Container startup fails within 30s with a clear error message if the Auth Proxy does not come up.
