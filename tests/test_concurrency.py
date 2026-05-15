import threading
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Job, _utcnow
from app.scheduler import ThreadSafeSet, find_due_jobs, enqueued_job_ids, GLOBAL_JOB_QUEUE

# Create an in-memory database for testing, shared across threads
test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(bind=test_engine)


@pytest.fixture(autouse=True)
def setup_test_db():
    """Create a fresh in-memory schema for every test, then tear it down."""
    Base.metadata.create_all(bind=test_engine)
    # Patch SessionLocal in all modules to prevent any test from reaching Supabase
    with patch("app.scheduler.SessionLocal", new=TestSessionLocal), \
         patch("app.mcp_server.SessionLocal", new=TestSessionLocal):
        yield
    Base.metadata.drop_all(bind=test_engine)


def test_thread_safe_set():
    """Test that ThreadSafeSet properly handles highly concurrent additions and removals."""
    ts_set = ThreadSafeSet()

    def worker_add(start, end):
        for i in range(start, end):
            ts_set.add(i)

    def worker_discard(start, end):
        for i in range(start, end):
            ts_set.discard(i)

    threads = []
    # 1. Spawn 10 threads, each adding 100 distinct items (0 to 999)
    for i in range(10):
        t = threading.Thread(target=worker_add, args=(i * 100, (i + 1) * 100))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Verify all 1000 items are present
    assert sum(1 for _ in ts_set._set) == 1000
    for i in range(1000):
        assert i in ts_set

    # 2. Spawn 10 threads discarding them
    threads = []
    for i in range(10):
        t = threading.Thread(target=worker_discard, args=(i * 100, (i + 1) * 100))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Verify all items were successfully removed without corruption
    assert sum(1 for _ in ts_set._set) == 0


def test_sqlite_wal_concurrent_reads_writes(tmp_path):
    """
    Test SQLite WAL mode & QueuePool concurrency.
    Spawns multiple threads writing to the database while simultaneously
    reading from it to ensure no 'database is locked' OperationalErrors occur.

    Uses a temporary file-backed SQLite DB with QueuePool — WAL requires
    multiple real connections, which StaticPool cannot provide.
    The production DB is never touched.
    """
    import sqlite3
    from sqlalchemy.pool import QueuePool

    db_path = tmp_path / "test_wal.db"
    wal_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 15},
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=10,
    )

    @event.listens_for(wal_engine, "connect")
    def _set_wal(dbapi_connection, connection_record):
        if isinstance(dbapi_connection, sqlite3.Connection):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    Base.metadata.create_all(bind=wal_engine)
    WalSessionLocal = sessionmaker(bind=wal_engine)

    errors = []

    def writer_task(base_id):
        try:
            for i in range(10):
                with WalSessionLocal() as db:
                    j = Job(
                        description=f"WAL Test Job {base_id}-{i}",
                        scheduled_at=_utcnow(),
                        time_bucket="2026010100",
                    )
                    db.add(j)
                    db.commit()
        except Exception as e:
            errors.append(e)

    def reader_task():
        try:
            for _ in range(10):
                with WalSessionLocal() as db:
                    _ = db.query(Job).limit(50).all()
        except Exception as e:
            errors.append(e)

    threads = []
    # Spawn 3 writers and 3 readers
    for i in range(3):
        t_w = threading.Thread(target=writer_task, args=(i,))
        t_r = threading.Thread(target=reader_task)
        threads.append(t_w)
        threads.append(t_r)
        t_w.start()
        t_r.start()

    for t in threads:
        t.join()

    # Cleanup
    wal_engine.dispose()

    # If WAL mode and pooling aren't working correctly, we would see "database is locked" errors here.
    assert len(errors) == 0, f"Concurrency errors encountered: {errors}"


def test_watcher_worker_segregation_pattern():
    """
    Test the strict segregation of database reads (Watcher) and database writes (Worker).
    Verifies that the Watcher only identifies and enqueues jobs without altering DB state,
    and the Worker handles state mutations.

    Uses the in-memory TestSessionLocal — the production DB is never touched.
    """
    # 1. Setup: Create a pending job
    with TestSessionLocal() as db:
        j = Job(
            description="Test Segregation",
            scheduled_at=_utcnow() - timedelta(minutes=1),
            time_bucket="2025010100",
        )
        db.add(j)
        db.commit()
        job_id = j.id

    # 2. Simulate Watcher Logic (find_due_jobs uses the patched SessionLocal internally)
    with TestSessionLocal() as db:
        now = _utcnow()
        due_jobs = find_due_jobs(now, db)

        # Verify job was found
        found_job = next((job for job in due_jobs if job.id == job_id), None)
        assert found_job is not None

        # Confirm the Watcher does NOT mutate state (job remains 'pending')
        assert found_job.status == "pending"

        # Simulate Watcher enqueuing without DB commits
        if found_job.id not in enqueued_job_ids:
            enqueued_job_ids.add(found_job.id)
            GLOBAL_JOB_QUEUE.put(found_job.id)

    # Verify DB state is strictly untouched by the Watcher's actions
    with TestSessionLocal() as db:
        db_job = db.query(Job).filter(Job.id == job_id).first()
        assert db_job.status == "pending"

    # 3. Simulate Worker Logic
    # Pull from queue
    pulled_job_id = GLOBAL_JOB_QUEUE.get()

    with TestSessionLocal() as db:
        w_job = db.query(Job).filter(Job.id == pulled_job_id).first()

        # Worker performs the mutation
        w_job.status = "running"
        db.commit()

        # Worker marks completed
        w_job.status = "completed"
        db.commit()

    # Worker cleanup
    enqueued_job_ids.discard(pulled_job_id)
    GLOBAL_JOB_QUEUE.task_done()

    # 4. Final Verifications
    with TestSessionLocal() as db:
        final_job = db.query(Job).filter(Job.id == job_id).first()
        # Verify Worker successfully updated DB
        assert final_job.status == "completed"

    # Verify ThreadSafeSet tracking was cleared
    assert job_id not in enqueued_job_ids
