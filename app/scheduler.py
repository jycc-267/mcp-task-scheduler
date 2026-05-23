import concurrent.futures
import logging
import os
import queue
import threading
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv
from google import genai
from sqlalchemy.orm import Session, aliased
from sqlalchemy import select
from croniter import croniter

from app.database import SessionLocal
from app.models import Job, _utcnow

load_dotenv()

logger = logging.getLogger(__name__)

GEMINI_API_KEY_VAR = "GEMINI_API_KEY"
GEMINI_MODEL = "gemini-2.5-flash"
LLM_TIMEOUT_SECONDS = 60

# In-memory, thread-safe, global job queue (simulates SQS for prototype)
GLOBAL_JOB_QUEUE: queue.Queue[int] = queue.Queue()

class ThreadSafeSet:
    """A thread-safe wrapper around a Python set to prevent race conditions independent of the GIL."""
    def __init__(self):
        self._set = set()
        self._lock = threading.Lock()

    def add(self, item: int):
        with self._lock:
            self._set.add(item)

    def discard(self, item: int):
        with self._lock:
            self._set.discard(item)

    def __contains__(self, item: int) -> bool:
        with self._lock:
            return item in self._set

# Mimic SQS's visibility timeout, preventing duplicate job processing.
enqueued_job_ids = ThreadSafeSet()


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
    ParentJob = aliased(Job)

    current_time_bucket = get_time_bucket(current_time)
    query = (
        select(Job)
        .outerjoin(ParentJob, Job.parent_job_id == ParentJob.id)
        .where(Job.status == "pending")
        .where(Job.time_bucket <= current_time_bucket) # fault-tolerance: 包含過去小時的 bucket，假設有做partition，資料庫會直接跳過「未來」的分區
        .where(Job.scheduled_at <= current_time)   # 過濾「當前小時」中尚未到來的時間點
        .where(
            (Job.parent_job_id.is_(None)) | (ParentJob.status == "completed")
        )
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
        try:
            with SessionLocal() as db:
                now = _utcnow()
                due_jobs = find_due_jobs(now, db)
                for job in due_jobs:
                    if job.id not in enqueued_job_ids:
                        enqueued_job_ids.add(job.id)
                        GLOBAL_JOB_QUEUE.put(job.id)
        except Exception as e:
            logger.error("Watcher loop error: %s", e, exc_info=True)
        time.sleep(interval)


def reaper_loop(interval: int = 300):
    """The Reaper ensures 'Visibility Timeout' by recovering stuck jobs.

    If a worker crashes or a thread dies, jobs might stay in 'running'
    for too long. The Reaper finds jobs that have been 'running'
    for more than 5 minutes and resets them to 'pending'.
    """
    while True:
        try:
            with SessionLocal() as db:
                stuck_threshold = _utcnow() - timedelta(minutes=5)
                stuck_jobs = (
                    db.query(Job.id)
                    .filter(Job.status == "running")
                    .filter(Job.updated_at < stuck_threshold)
                    .all()
                )
                if stuck_jobs:
                    stuck_ids = [j[0] for j in stuck_jobs]
                    # Atomic update: prevents race condition where a worker completes the job 
                    # right after the Reaper queries it.
                    updated_count = (
                        db.query(Job)
                        .filter(Job.id.in_(stuck_ids), Job.status == "running")
                        .update({"status": "pending"}, synchronize_session=False)
                    )
                    db.commit()
                    if updated_count > 0:
                        for j_id in stuck_ids:
                            logger.warning(
                                "Reaper recovering stuck job %d (resetting to pending)", j_id
                            )
                            # Ensure ID is removed from virtual set so Watcher can re-enqueue
                            enqueued_job_ids.discard(j_id)
        except Exception as e:
            logger.error("Reaper error: %s", e)
        time.sleep(interval)


def _get_gemini_client_sync() -> genai.Client | None:
    """Create a synchronous Gemini client from the environment API key.

    Returns None if the API key is not configured, allowing the worker
    to fall back to a no-op execution mode.
    """
    api_key = os.environ.get(GEMINI_API_KEY_VAR)
    if not api_key:
        logger.warning(
            "GEMINI_API_KEY not set — worker will use placeholder execution."
        )
        return None
    return genai.Client(api_key=api_key)


def _execute_with_llm(client: genai.Client, description: str) -> str:
    """Send a job description to the Gemini API and return the response.

    Uses a ThreadPoolExecutor to enforce LLM_TIMEOUT_SECONDS, preventing
    a hung Gemini call from permanently blocking the worker thread.

    Args:
        client: An initialized Gemini client.
        description: The job description to process.

    Returns:
        The LLM's text response.

    Raises:
        TimeoutError: If the Gemini API call exceeds LLM_TIMEOUT_SECONDS.
        Exception: Propagated from the Gemini SDK on API failures.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.models.generate_content,
            model=GEMINI_MODEL,
            contents=f"Execute the following task and provide the result:\n\n{description}",
            config=genai.types.GenerateContentConfig(
                temperature=0.3,
            ),
        )
        try:
            response = future.result(timeout=LLM_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(
                f"Gemini API call timed out after {LLM_TIMEOUT_SECONDS}s"
            ) from None
    return response.text


# scale out with this pattern, does it solve starvation when worker scale out ? 
# do we still need order_by at DB level? or reaper pattern ?
def worker_loop():
    """Worker pulls jobs from queue and executes them via Gemini LLM.

    The DB session is split into two short-lived transactions:
    1. Transaction 1: Read job details + mark 'running' (fast)
    2. LLM call runs OUTSIDE any DB session (pool connection released)
    3. Transaction 2: Write result + mark 'completed' (fast)

    This prevents QueuePool exhaustion when LLM calls take 5-60+ seconds.
    """
    gemini_client = _get_gemini_client_sync()

    while True:
        job_id = GLOBAL_JOB_QUEUE.get() # should we persist failed job_id in the queue?
        description = None
        cron_expr = None
        scheduled_at = None
        parent_job_id = None
        timezone = None

        # Transaction 1: Fast read + mark running
        try:
            with SessionLocal() as db:
                job = db.query(Job).filter(Job.id == job_id).first()
                if job is None or job.status != "pending":
                    enqueued_job_ids.discard(job_id)
                    GLOBAL_JOB_QUEUE.task_done()
                    continue

                description = job.description
                cron_expr = job.cron_expr
                scheduled_at = job.scheduled_at
                parent_job_id = job.parent_job_id
                timezone = job.timezone
                job.status = "running"
                db.commit()
        except Exception as e:
            logger.error("Failed to start job %d: %s", job_id, e)
            enqueued_job_ids.discard(job_id)
            GLOBAL_JOB_QUEUE.task_done()
            continue

        # LLM call runs OUTSIDE any DB session — pool connection is fully released
        result_text = f"Executed (no LLM): {description}"
        llm_log = None
        if gemini_client is not None:
            try:
                result_text = _execute_with_llm(gemini_client, description)
                logger.info("Job %d executed via LLM successfully.", job_id)
            except Exception as llm_err:
                logger.error(
                    "LLM execution failed for job %d: %s", job_id, llm_err
                )
                result_text = f"LLM error: {llm_err}"
                llm_log = f"LLM error: {llm_err}"

        # Transaction 2: Fast write of result
        try:
            with SessionLocal() as db:
                job = db.query(Job).filter(Job.id == job_id).first()
                if job is None:
                    logger.error("Job %d disappeared between transactions.", job_id)
                    continue

                # Check if job was cancelled by UI during the long LLM call
                if job.status == "cancelled":
                    logger.info("Job %d was cancelled during execution. Discarding result.", job_id)
                    continue

                job.result = result_text
                job.status = "completed"
                if llm_log:
                    job.logs = (job.logs or "") + f"\n{llm_log}"

                # Check if job is recurring
                if cron_expr and scheduled_at:
                    try:
                        import zoneinfo
                        # Use the user's timezone if present, otherwise default to UTC
                        tz_name = timezone or "UTC"
                        tz = zoneinfo.ZoneInfo(tz_name)
                        
                        # Convert naive scheduled_at (which is stored in UTC) to a timezone-aware datetime in user's timezone
                        local_scheduled_at = scheduled_at.replace(tzinfo=zoneinfo.ZoneInfo("UTC")).astimezone(tz)
                        
                        # Calculate the next occurrence in local time
                        iter_cron = croniter(cron_expr, local_scheduled_at)
                        next_local = iter_cron.get_next(datetime)
                        
                        # Convert the next local time back to naive UTC for storage
                        next_utc = next_local.astimezone(zoneinfo.ZoneInfo("UTC")).replace(tzinfo=None)
                        
                        new_job = Job(
                            description=description,
                            scheduled_at=next_utc,
                            time_bucket=get_time_bucket(next_utc),
                            cron_expr=cron_expr,
                            parent_job_id=parent_job_id,
                            timezone=tz_name,
                        )
                        db.add(new_job)
                    except Exception as cron_err:
                        job.logs = (job.logs or "") + f"\nCron scheduling failed: {cron_err}"

                db.commit()
        except Exception as e:
            logger.error("Worker error writing result for job %d: %s", job_id, e)
            try:
                with SessionLocal() as db:
                    job = db.query(Job).filter(Job.id == job_id).first()
                    # Only reset to pending if it wasn't cancelled by the user
                    if job and job.status == "running":
                        # Visibility Reset: return to pending so Watcher can retry
                        # after enqueued_job_ids.discard() in finally block
                        job.status = "pending"
                        job.logs = (job.logs or "") + f"\nTransient worker error: {e}"
                        db.commit()
                        logger.info("Job %d reset to 'pending' for retry.", job_id)
            except Exception:
                logger.error(
                    "Failed to reset job %d to 'pending' — will require Reaper.",
                    job_id,
                    exc_info=True,
                )
        finally:
            enqueued_job_ids.discard(job_id)
            GLOBAL_JOB_QUEUE.task_done()


DEFAULT_NUM_WORKERS = 1


def start_scheduler(num_workers: int = DEFAULT_NUM_WORKERS):
    """Start watcher, worker, and reaper threads.

    Args:
        num_workers: Number of worker threads to spawn for parallel LLM execution.
    """
    watcher = threading.Thread(target=watcher_loop, daemon=True, name="watcher")
    reaper = threading.Thread(target=reaper_loop, daemon=True, name="reaper")
    watcher.start()
    reaper.start()
    for i in range(num_workers):
        worker = threading.Thread(target=worker_loop, daemon=True, name=f"worker-{i}")
        worker.start()
    logger.info("Scheduler started: 1 watcher, 1 reaper, %d worker(s).", num_workers)
