-- MCP Task Scheduler: Initial Schema
-- Mirrors app/models.py Job model
-- Applied automatically by Base.metadata.create_all() on first startup.
-- Kept here as an audit trail for manual replay via Supabase SQL Editor.

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

-- Single-column index on time_bucket (mirrors index=True on the column)
CREATE INDEX IF NOT EXISTS ix_jobs_time_bucket
    ON jobs (time_bucket);

-- Partial index: only indexes pending jobs for efficient watcher queries
CREATE INDEX IF NOT EXISTS idx_pending_scheduler
    ON jobs (time_bucket, scheduled_at)
    WHERE status = 'pending';

-- Composite index for bucket + status queries
CREATE INDEX IF NOT EXISTS idx_bucket_status
    ON jobs (time_bucket, status);
