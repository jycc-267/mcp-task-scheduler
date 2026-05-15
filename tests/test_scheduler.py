import pytest
from datetime import datetime, timedelta
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Job, _utcnow
from app.scheduler import find_due_jobs, get_time_bucket, worker_loop, GLOBAL_JOB_QUEUE

import threading

# Create an in-memory database for testing, shared across threads
test_engine = create_engine(
    "sqlite:///:memory:", # create a temporary database directly in your computer's RAM
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)
TestSessionLocal = sessionmaker(bind=test_engine)

@pytest.fixture(autouse=True)
def setup_database():
    Base.metadata.create_all(bind=test_engine)
    
    # Patch SessionLocal in all modules to prevent any test from reaching Supabase
    with patch("app.scheduler.SessionLocal", new=TestSessionLocal), \
         patch("app.mcp_server.SessionLocal", new=TestSessionLocal):
        db = TestSessionLocal()
        yield db
        db.close()
        
    Base.metadata.drop_all(bind=test_engine)

def test_dag_resolution(setup_database):
    db = setup_database
    now = _utcnow()
    
    # Create a parent job
    parent_job = Job(
        description="Parent Job",
        scheduled_at=now - timedelta(minutes=5),
        time_bucket=get_time_bucket(now - timedelta(minutes=5))
    )
    db.add(parent_job)
    db.commit()
    db.refresh(parent_job)
    
    # Create a child job
    child_job = Job(
        description="Child Job",
        scheduled_at=now - timedelta(minutes=5),
        time_bucket=get_time_bucket(now - timedelta(minutes=5)),
        parent_job_id=parent_job.id
    )
    db.add(child_job)
    db.commit()
    db.refresh(child_job)
    
    # Check if child is due. It should not be due since parent is pending
    due_jobs = find_due_jobs(now, db)
    assert parent_job in due_jobs, "Parent should be queued since it's past due"
    assert child_job not in due_jobs, "Child should not be queued since parent is not completed"
    
    # Mark parent as completed
    parent_job.status = "completed"
    db.commit()
    
    # Check if child is due now
    due_jobs = find_due_jobs(now, db)
    assert child_job in due_jobs, "Child should be queued since parent is completed"



def test_cron_calculation(setup_database):
    """
    When you use sqlite:///:memory:, you are indeed redirecting everything to a temporary RAM database.
    But by default, SQLite creates a brand new, completely separate RAM database for every single connection.

    Here is exactly what was happening before we added StaticPool:

    The Test Thread:
    - TestSessionLocal() asks SQLAlchemy for a connection.
    - SQLAlchemy opens Connection A.
    - SQLite creates RAM Database A.
    - The test creates the tables and inserts the cron_job into RAM Database A.
    
    The Background Worker Thread:
    - The worker thread starts and runs db = SessionLocal(). Thanks to our mock, this correctly calls TestSessionLocal().
    - TestSessionLocal() asks SQLAlchemy for a connection.
    - Because it's a new request from a different thread, SQLAlchemy opens Connection B.
    - Because it's a new connection, SQLite creates a brand new, completely empty RAM Database B.
    - The worker thread looks for the job in RAM Database B, finds nothing, and goes back to sleep.
    
    Meanwhile, back in the Test Thread, the test checks RAM Database A and sees that the job is still stuck in "pending".
    Why StaticPool fixed it: Adding poolclass=StaticPool changes SQLAlchemy's behavior. It forces SQLAlchemy to open exactly one single connection and keep it open.
    With StaticPool, when the background worker calls TestSessionLocal(), SQLAlchemy says "Oh, I already have Connection A open, here you go." Because both threads are now sharing Connection A, they are both finally talking to the exact same RAM Database A.
    """
    db = setup_database
    now = _utcnow()
    
    # 1. Create a job with a cron expression
    cron_job = Job(
        description="Daily Report",
        scheduled_at=now,
        time_bucket=get_time_bucket(now),
        cron_expr="0 9 * * *" # Every day at 9 AM
    )
    db.add(cron_job)
    db.commit()
    db.refresh(cron_job)
    
    # 2. Push the job to the queue manually (bypassing the watcher)
    GLOBAL_JOB_QUEUE.put(cron_job.id)
    
    # 3. Start a single worker thread in the background
    worker = threading.Thread(target=worker_loop, daemon=True)
    worker.start()
    
    # 4. Wait for the queue to empty (blocks until task_done() is called)
    GLOBAL_JOB_QUEUE.join()
    
    # 5. Check the original job is completed
    db.refresh(cron_job)
    assert cron_job.status == "completed", "Original job should be marked complete"
    
    # 6. Check that a new job was spawned for the next iteration
    new_jobs = db.query(Job).filter(Job.description == "Daily Report", Job.status == "pending").all()
    assert len(new_jobs) == 1, "A new pending job should have been created"
    next_job = new_jobs[0]
    
    # 7. Verify the new job carried over the cron expression and scheduled it for the future
    assert next_job.scheduled_at > cron_job.scheduled_at
    assert next_job.cron_expr == "0 9 * * *"
