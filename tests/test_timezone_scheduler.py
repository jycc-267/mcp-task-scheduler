import pytest
import zoneinfo
import threading
from datetime import datetime
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from croniter import croniter

from app.database import Base
from app.models import Job
from app.scheduler import get_time_bucket, worker_loop, GLOBAL_JOB_QUEUE
from app.mcp_server import task_create, task_status

# Create an in-memory database for testing, shared across threads
test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)
TestSessionLocal = sessionmaker(bind=test_engine)

@pytest.fixture(autouse=True)
def setup_database():
    Base.metadata.create_all(bind=test_engine)
    
    # Patch the SessionLocal in mcp_server and scheduler functions
    with patch("app.scheduler.SessionLocal", new=TestSessionLocal), \
         patch("app.mcp_server.SessionLocal", new=TestSessionLocal):
        db = TestSessionLocal()
        yield db
        db.close()
        
    Base.metadata.drop_all(bind=test_engine)

def test_timezone_storage(setup_database):
    """Verify that task_create stores and returns the user_timezone parameter properly."""
    res = task_create(
        description="Timezone Test Job",
        scheduled_at="2026-05-23T12:00:00",
        user_timezone="America/New_York",
        cron_expr="0 9 * * *"
    )
    
    assert res["user_timezone"] == "America/New_York"
    job_id = res["job_id"]
    
    # Fetch status to verify
    status_res = task_status(job_id=job_id)
    assert status_res["user_timezone"] == "America/New_York"

def test_timezone_cron_dst_spring_forward(setup_database):
    """
    Test Spring Forward transition (Sunday, March 8, 2026):
    - America/New_York transitions from EST (UTC-5) to EDT (UTC-4) at 2:00 AM.
    - A daily task scheduled for 9:00 AM local time should run at:
      - Saturday, March 7: 9:00 AM EST -> 14:00 UTC
      - Sunday, March 8: 9:00 AM EDT -> 13:00 UTC (1 hour earlier in UTC time!)
    """
    db = setup_database
    
    # 1. Create a job scheduled for Saturday, March 7, 2026 at 9:00 AM EST (14:00 UTC)
    scheduled_utc = datetime(2026, 3, 7, 14, 0, 0)
    cron_job = Job(
        description="Daily Morning Sync (Spring Forward)",
        scheduled_at=scheduled_utc,
        time_bucket=get_time_bucket(scheduled_utc),
        cron_expr="0 9 * * *",
        timezone="America/New_York"
    )
    db.add(cron_job)
    db.commit()
    db.refresh(cron_job)
    
    # 2. Push the job to the queue manually to trigger worker loop execution
    GLOBAL_JOB_QUEUE.put(cron_job.id)
    
    # 3. Start a worker thread to execute the job
    worker = threading.Thread(target=worker_loop, daemon=True)
    worker.start()
    
    # 4. Wait for job to finish processing
    GLOBAL_JOB_QUEUE.join()
    
    # 5. Verify the original job was completed
    db.refresh(cron_job)
    assert cron_job.status == "completed"
    
    # 6. Verify that the new job spawned for the next day correctly adjusted its UTC time for DST
    new_jobs = db.query(Job).filter(
        Job.description == "Daily Morning Sync (Spring Forward)",
        Job.status == "pending"
    ).all()
    
    assert len(new_jobs) == 1
    next_job = new_jobs[0]
    
    # Sunday March 8, 9:00 AM local New York time (EDT) is 13:00 UTC.
    expected_utc = datetime(2026, 3, 8, 13, 0, 0)
    assert next_job.scheduled_at == expected_utc
    assert next_job.timezone == "America/New_York"

def test_timezone_cron_dst_fall_back(setup_database):
    """
    Test Fall Back transition (Sunday, November 1, 2026):
    - America/New_York transitions from EDT (UTC-4) to EST (UTC-5) at 2:00 AM.
    - A daily task scheduled for 9:00 AM local time should run at:
      - Saturday, Oct 31: 9:00 AM EDT -> 13:00 UTC
      - Sunday, Nov 1: 9:00 AM EST -> 14:00 UTC (1 hour later in UTC time!)
    """
    db = setup_database
    
    # 1. Create a job scheduled for Saturday, October 31, 2026 at 9:00 AM EDT (13:00 UTC)
    scheduled_utc = datetime(2026, 10, 31, 13, 0, 0)
    cron_job = Job(
        description="Daily Morning Sync (Fall Back)",
        scheduled_at=scheduled_utc,
        time_bucket=get_time_bucket(scheduled_utc),
        cron_expr="0 9 * * *",
        timezone="America/New_York"
    )
    db.add(cron_job)
    db.commit()
    db.refresh(cron_job)
    
    # 2. Push the job to the queue
    GLOBAL_JOB_QUEUE.put(cron_job.id)
    
    # 3. Start worker
    worker = threading.Thread(target=worker_loop, daemon=True)
    worker.start()
    
    # 4. Wait
    GLOBAL_JOB_QUEUE.join()
    
    # 5. Verify original job is complete
    db.refresh(cron_job)
    assert cron_job.status == "completed"
    
    # 6. Verify that the new job spawned for the next day correctly adjusted its UTC time for DST
    new_jobs = db.query(Job).filter(
        Job.description == "Daily Morning Sync (Fall Back)",
        Job.status == "pending"
    ).all()
    
    assert len(new_jobs) == 1
    next_job = new_jobs[0]
    
    # Sunday November 1, 9:00 AM local New York time (EST) is 14:00 UTC.
    expected_utc = datetime(2026, 11, 1, 14, 0, 0)
    assert next_job.scheduled_at == expected_utc
    assert next_job.timezone == "America/New_York"
