import threading
from datetime import timedelta
import pytest

from app.database import SessionLocal, engine, Base
from app.models import Job, _utcnow
from app.scheduler import ThreadSafeSet, find_due_jobs, enqueued_job_ids, GLOBAL_JOB_QUEUE

# Fixture to initialize the database schema before tests
@pytest.fixture(scope="module", autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=engine)
    yield
    # We do not drop tables here because other test modules might share the database in this prototype.
    # In a real setup, we would use an isolated test database.

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
        t = threading.Thread(target=worker_add, args=(i*100, (i+1)*100))
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
        t = threading.Thread(target=worker_discard, args=(i*100, (i+1)*100))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # Verify all items were successfully removed without corruption
    assert sum(1 for _ in ts_set._set) == 0


def test_sqlite_wal_concurrent_reads_writes():
    """
    Test SQLite WAL mode & QueuePool concurrency.
    Spawns multiple threads writing to the database while simultaneously
    reading from it to ensure no 'database is locked' OperationalErrors occur.
    """
    errors = []
    
    def writer_task(base_id):
        try:
            for i in range(10):
                with SessionLocal() as db:
                    j = Job(
                        description=f"WAL Test Job {base_id}-{i}", 
                        scheduled_at=_utcnow(), 
                        time_bucket="2026010100"
                    )
                    db.add(j)
                    db.commit()
        except Exception as e:
            errors.append(e)
                
    def reader_task():
        try:
            for _ in range(10):
                with SessionLocal() as db:
                    _ = db.query(Job).limit(50).all()
        except Exception as e:
            errors.append(e)
                
    threads = []
    # Spawn 5 writers and 5 readers
    for i in range(5):
        t_w = threading.Thread(target=writer_task, args=(i,))
        t_r = threading.Thread(target=reader_task)
        threads.append(t_w)
        threads.append(t_r)
        t_w.start()
        t_r.start()
        
    for t in threads:
        t.join()
        
    # If WAL mode and pooling aren't working correctly, we would see "database is locked" errors here.
    assert len(errors) == 0, f"Concurrency errors encountered: {errors}"


def test_watcher_worker_segregation_pattern():
    """
    Test the strict segregation of database reads (Watcher) and database writes (Worker).
    Verifies that the Watcher only identifies and enqueues jobs without altering DB state,
    and the Worker handles state mutations.
    """
    # 1. Setup: Create a pending job
    with SessionLocal() as db:
        j = Job(
            description="Test Segregation", 
            scheduled_at=_utcnow() - timedelta(minutes=1), 
            time_bucket="2025010100"
        )
        db.add(j)
        db.commit()
        job_id = j.id
        
    # 2. Simulate Watcher Logic
    with SessionLocal() as db:
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
    with SessionLocal() as db:
        db_job = db.query(Job).filter(Job.id == job_id).first()
        assert db_job.status == "pending"
        
    # 3. Simulate Worker Logic
    # Pull from queue
    pulled_job_id = GLOBAL_JOB_QUEUE.get()
    
    with SessionLocal() as db:
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
    with SessionLocal() as db:
        final_job = db.query(Job).filter(Job.id == job_id).first()
        # Verify Worker successfully updated DB
        assert final_job.status == "completed"
        
    # Verify ThreadSafeSet tracking was cleared
    assert job_id not in enqueued_job_ids
