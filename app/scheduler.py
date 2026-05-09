import queue
import threading
import time
from datetime import datetime

from sqlalchemy.orm import Session
from sqlalchemy import select

from .database import SessionLocal
from .models import Job, _utcnow

# In-memory queue (simulates SQS for prototype)
job_queue: queue.Queue[int] = queue.Queue()


def get_time_bucket(scheduled_at: datetime) -> str:
    """Convert scheduled time to time bucket — used as DB partition key.

    The time bucket groups jobs into hourly windows so the watcher can
    efficiently query only the relevant partition instead of scanning
    the entire jobs table.
    """
    # TODO: Implement this function
    #
    # Design decision: Time-based partitioning for efficient job lookup
    #
    # Hints:
    # 1. Format the datetime into a string that represents an hourly bucket
    # 2. Use strftime with a format like "%Y%m%d%H" (e.g., "2025030114")
    # 3. This bucket string becomes the partition key in the jobs table
    # 將 get_time_bucket 改為「純日期」（%Y%m%d），雖然增加了容錯空間，但會讓單個分區變得太大。最好的方式是維持「小時」分區，但在查詢端做範圍掃描。

    return scheduled_at.strftime("%Y%m%d%H")


def find_due_jobs(current_time: datetime, db: Session) -> list[Job]:
    """Watcher calls every minute: find due jobs in current time bucket.

    Queries the jobs table using the time bucket as a partition key,
    then filters for jobs that are due (scheduled_at <= now) and still
    in 'pending' status.
    """
    # TODO: Implement this function
    #
    # Design decision: Watcher pattern — poll DB for due jobs using
    #   the time bucket as a partition key to avoid full table scans
    #
    # Hints:
    # 1. Compute the current time bucket using get_time_bucket()
    # 2. Query Job where time_bucket matches AND scheduled_at <= current_time
    # 3. Only include jobs with status == "pending"
    # 4. Return the list of matching Job objects
    # time_bucket <= current_bucket 在 SQLite 或大表中可能太慢，建立一個部分索引 (Partial Index)：
    current_time_bucket = get_time_bucket(current_time)
    query = (
        select(Job)
        .where(Job.status == "pending")
        .where(Job.time_bucket <= current_time_bucket) # fault-tolerance: 包含過去小時的 bucket，假設有做partition，資料庫會直接跳過「未來」的分區
        .where(Job.scheduled_at <= current_time)   # 過濾「當前小時」中尚未到來的時間點
        .order_by(Job.scheduled_at.asc())               # 優先處理最早的任務 (FIFO), prevent starvation; partial index ensure sorting efficiency
        .limit(500) # worker execute this query in background polling, aviod list[Job] fills memory
    )
    return list(db.execute(query).scalars().all())

# reaper pattern
# """
# 如果你的系統對「絕對不能漏掉任務」有極高要求，建議除了每分鐘的 Watcher 外，
# 再增加一個 Reaper（收割者） 流程：Watcher：每分鐘跑，只專注於 current_bucket（追求極致效能）。
# Reaper：每 10 分鐘或每小時跑一次，專門執行 Job.time_bucket < current_bucket AND Job.status == "pending"，
# 把所有因為異常而卡住的「陳年舊案」撈出來重啟。
# """
def watcher_loop(interval: int = 10):
    """Watcher scans DB for due jobs and pushes them to the queue."""
    while True:
        db = SessionLocal()
        try:
            now = _utcnow()
            due_jobs = find_due_jobs(now, db)
            for job in due_jobs:
                job.status = "queued"
                db.commit()
                job_queue.put(job.id)
        finally:
            db.close()
        time.sleep(interval)


# scale out with this pattern, does it solve starvation when worker scale out ? 
# do we still need order_by at DB level? or reaper pattern ?
def worker_loop():
    """Worker pulls jobs from queue and executes them."""
    while True:
        job_id = job_queue.get()
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job is None or job.status == "cancelled":
                continue

            job.status = "running"
            db.commit()

            # Simulate execution — in production this would call LLM
            job.result = f"Executed: {job.description}"
            job.status = "completed"
            db.commit()
        except Exception as e:
            job.status = "failed"
            job.result = str(e)
            db.commit()
        finally:
            db.close()
            job_queue.task_done()


def start_scheduler():
    """Start watcher and worker threads."""
    watcher = threading.Thread(target=watcher_loop, daemon=True)
    worker = threading.Thread(target=worker_loop, daemon=True)
    watcher.start()
    worker.start()
