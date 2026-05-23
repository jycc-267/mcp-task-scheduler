# Implementation Plan: Migrate SQLite to Google Cloud SQL (PostgreSQL) on Cloud Run

## Overview
To transition the MCP task scheduler from a local SQLite setup to a production-grade Google Cloud Run and Cloud SQL architecture, this plan details the shift away from Prefect Horizon. The new strategy leverages Cloud Run’s native integration with Cloud SQL via Unix sockets, eliminating the need for an Auth Proxy sidecar, and introduces best practices for serverless connection pooling and schema migrations.

## Codebase Context
- **Codebase structure**: The persistence layer is localized in `app/database.py` and `app/models.py`.
- **Codebase features**: A background scheduler running task queues and an API layer for task execution.
- **Codebase dependencies**: Currently relies on `sqlalchemy` and built-in `sqlite3`.
- **Current DB Configuration**: `app/database.py` uses a `QueuePool` with `pool_size=5` and `max_overflow=10`, which is dangerous in a serverless environment like Cloud Run that autoscales.
- **Current Model Configuration**: `app/models.py` uses `sqlite_where` for partial indexing, which is incompatible with PostgreSQL.

## Requirements
- Switch the target deployment to Google Cloud Run, utilizing native Unix Socket connections to Cloud SQL.
- Update `app/database.py` to support `DATABASE_URL` dynamic loading without breaking local SQLite development.
- Refactor connection pooling to prevent Cloud SQL connection exhaustion during Cloud Run autoscaling.
- Implement dialect-agnostic models in `app/models.py` that support both PostgreSQL (production) and SQLite (local).
- Establish a reliable pattern for writing model schemas into GCP Cloud SQL without risking race conditions.

## Architecture Changes
- **`app/database.py`**: Conditionally configure the `create_engine` settings. Use `NullPool` or a heavily restricted `QueuePool` (e.g., `pool_size=1, max_overflow=2`) for PostgreSQL to avoid connection limit exhaustion when Cloud Run scales out.
- **`app/models.py`**: Update `__table_args__` in the `Job` model to include `postgresql_where` alongside `sqlite_where`.
- **`pyproject.toml`**: Add `pg8000` to dependencies.
- **`Dockerfile`**: Transition to a lightweight multi-stage Docker build using `uv`.
- **Migration Script (New)**: Extract `Base.metadata.create_all()` into a standalone execution path (e.g., a Cloud Run Job) to safely run schema migrations.

## Implementation Steps

### Phase 1: Database Refactor & Schema Strategy
1. **Update Connection Pooling** (File: `app/database.py`)
   - Action: Read `DATABASE_URL` from the environment. If connecting to PostgreSQL (Cloud SQL), configure `create_engine` with `pool_size=1`, `max_overflow=2`, and `pool_recycle=1800`.
   - Why: A `db-f1-micro` Cloud SQL instance has a hard limit of ~25-50 connections. If Cloud Run autoscales to 10 instances with the current `pool_size=5, max_overflow=10`, it will immediately exhaust database connections and crash.
   - Dependencies: None
   - Risk: High (Critical for stability)

2. **Update Partial Indexes** (File: `app/models.py`)
   - Action: Add `postgresql_where=text("status = 'pending'")` to the `idx_pending_scheduler` index.
   - Why: `sqlite_where` is silently ignored by PostgreSQL. We must explicitly define the PostgreSQL equivalent for the partial index to remain optimized.
   - Dependencies: None
   - Risk: Low

3. **Add PostgreSQL Driver** (File: `pyproject.toml`)
   - Action: Add `pg8000` as a project dependency using `uv add pg8000`.
   - Why: `pg8000` is a pure-Python PostgreSQL driver, minimizing system-level dependency issues inside containers.
   - Dependencies: None
   - Risk: Low

### Phase 2: Schema Migration & Infrastructure Provisioning
4. **Provision Cloud SQL (Already Done)**
   - Action: Create a PostgreSQL 16 instance and target database. Store the Unix Socket connection string in Google Secret Manager (`postgresql+pg8000://<USER>:<PASS>@/task_scheduler?unix_sock=/cloudsql/<PROJECT>:<REGION>:<INSTANCE>/.s.PGSQL.5432`).
   - Why: Sets up the managed persistence layer.
   - Dependencies: None
   - Risk: Low

5. **Create Schema Migration Job** (File: `app/migrate.py`)
   - Action: Create a new short-lived script `app/migrate.py` that simply imports models, engine, and runs `Base.metadata.create_all(bind=engine)`. Remove `Base.metadata.create_all(bind=engine)` from `app/mcp_server.py`.
   - Why: **When and how do I write the model schema?** Running `create_all()` on application startup inside `app/mcp_server.py` creates a severe race condition in Cloud Run. If multiple instances spin up simultaneously, they will clash trying to create the same tables. The schema should be executed exactly once via a standalone **Cloud Run Job** running `uv run python -m app.migrate` before deploying the main service.
   - Dependencies: Step 1, 2
   - Risk: Medium

### Phase 3: Containerization & Deployment
6. **Create Cloud Run Dockerfile** (File: `Dockerfile`)
   - Action: Write a lightweight multi-stage Dockerfile that installs dependencies via `uv sync --frozen --no-dev` and sets `CMD ["uv", "run", "python", "-m", "app.mcp_server", "--transport", "sse", "--host", "0.0.0.0", "--port", "8080"]`.
   - Why: Replaces the Prefect Horizon Auth Proxy setup with a lean runtime.
   - Dependencies: None
   - Risk: Low

7. **Deploy Main Service** (File: N/A - gcloud CLI)
   - Action: Deploy the web application to Cloud Run with CPU Throttling disabled (`--no-cpu-throttling`) and map the Cloud SQL instance (`--add-cloudsql-instances`).
   - Why: Cloud Run requires explicit permission to mount the Unix socket. Disabling CPU throttling ensures background worker threads (reaper, scheduler) remain active between HTTP requests.
   - Dependencies: Step 4, 6
   - Risk: Medium

## Testing Strategy
- Unit tests: Run existing tests in `tests/` using local in-memory SQLite to verify the `app/database.py` conditional logic didn't break local development.
- Integration tests: Locally run `uv run python -m app.migrate` and test the server connecting to a local PostgreSQL instance via Docker to verify `pg8000` compatibility.

## Risks & Mitigations
- **Risk**: Connection exhaustion leading to 500 errors or application crashes. -> Mitigation: Drastically reduce SQLAlchemy's `pool_size` and `max_overflow` for PostgreSQL. Monitor connections in the GCP Cloud SQL dashboard.
- **Risk**: Missing dependencies for PostgreSQL. -> Mitigation: Use `pg8000` (pure python) to avoid OS-level `libpq` requirements.
- **Risk**: Background threads freezing. -> Mitigation: Ensure `--no-cpu-throttling` is explicitly set during Cloud Run deployment.

## Success Criteria
- [ ] Application connects seamlessly to PostgreSQL on Cloud Run via Unix Sockets.
- [ ] Schema is reliably generated using a separate `app.migrate` execution step.
- [ ] Local development works out-of-the-box using the existing `chatgpt_task.db` SQLite database.
- [ ] Cloud SQL connections remain stable and within instance limits during Cloud Run load spikes.

## Best Practices to Enforce
1. **Be Specific**: Use `postgresql+pg8000` for connection dialects.
2. **Consider Edge Cases**: Protect against concurrent DDL operations by decoupling schema migration.
3. **Minimize Changes**: Keep the local SQLite logic intact so other developers don't have friction.
4. **Maintain Patterns**: Use environment variables for `DATABASE_URL` routing.

## Technical Debt (if any)
- `app/models.py`: The comment `建立專為 Watcher 設計的「部分索引」` should be translated to English to align with the rest of the codebase.
- No formal migration framework (e.g., `alembic`): While `Base.metadata.create_all()` is sufficient for v1, future schema updates (like adding columns) will require Alembic or similar.
