import argparse
import logging
import sys
import os
from datetime import datetime

from dotenv import load_dotenv
from fastmcp import FastMCP

# 1. Dynamically calculate the project root (the outer '/app' dir in Horizon)
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 2. Inject the project root into sys.path
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app.database import Base, SessionLocal, engine
from app.models import Job
from app.scheduler import get_time_bucket, start_scheduler, enqueued_job_ids

load_dotenv()

logger = logging.getLogger(__name__)

# ===================================================================
# MCP server wiring
# ===================================================================

mcp = FastMCP("task-scheduler")

@mcp.tool()
def task_create(description: str, scheduled_at: str, user_timezone: str = "UTC", cron_expr: str | None = None, parent_job_id: int | None = None) -> dict:
    """Schedule a new task for future execution.
    
    Args:
        description: What the task should do
        scheduled_at: When to run, ISO 8601 format (e.g. 2026-05-03T10:00:00)
        user_timezone: The IANA timezone name of the user (e.g., 'America/New_York', 'Asia/Taipei', 'Europe/London'). Deduce this from the user's request, context or system instructions. If unknown, default to 'UTC'.
        cron_expr: Optional cron expression for recurring tasks (e.g. '0 9 * * *' for daily at 9am)
        parent_job_id: Optional ID of a parent job that must complete before this job runs
    """
    with SessionLocal() as db:
        dt = datetime.fromisoformat(scheduled_at)
        job = Job(
            description=description,
            scheduled_at=dt,
            time_bucket=get_time_bucket(dt),
            cron_expr=cron_expr,
            parent_job_id=parent_job_id,
            timezone=user_timezone
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return {
            "job_id": job.id, 
            "status": job.status, 
            "scheduled_at": str(job.scheduled_at),
            "cron_expr": job.cron_expr,
            "parent_job_id": job.parent_job_id,
            "user_timezone": job.timezone
        }

@mcp.tool()
def task_status(job_id: int) -> dict:
    """Get the status of a scheduled task by job_id.
    
    Args:
        job_id: The job ID returned by task_create
    """
    with SessionLocal() as db:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job is None:
            return {"error": f"Job {job_id} not found"}
            
        current_status = job.status
        if current_status == "pending" and job.id in enqueued_job_ids:
            current_status = "queued"
            
        return {
            "job_id": job.id,
            "description": job.description,
            "status": current_status,
            "scheduled_at": str(job.scheduled_at),
            "cron_expr": job.cron_expr,
            "parent_job_id": job.parent_job_id,
            "user_timezone": job.timezone,
            "result": job.result,
        }

@mcp.tool()
def task_logs(job_id: int) -> dict:
    """Get the execution logs of a scheduled task by job_id.
    
    Args:
        job_id: The job ID to fetch logs for
    """
    with SessionLocal() as db:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job is None:
            return {"error": f"Job {job_id} not found"}
            
        return {
            "job_id": job.id,
            "logs": job.logs or "No logs available for this job."
        }

@mcp.tool()
def task_list(limit: int = 100, offset: int = 0) -> dict:
    """List scheduled tasks with pagination.

    Args:
        limit: Maximum number of tasks to return (default 100)
        offset: Number of tasks to skip for pagination (default 0)
    """
    with SessionLocal() as db:
        jobs = (
            db.query(Job)
            .order_by(Job.scheduled_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return {
            "jobs": [
                {
                    "job_id": j.id,
                    "description": j.description,
                    "status": "queued" if j.status == "pending" and j.id in enqueued_job_ids else j.status,
                    "scheduled_at": str(j.scheduled_at),
                    "cron_expr": j.cron_expr,
                    "parent_job_id": j.parent_job_id,
                    "user_timezone": j.timezone
                }
                for j in jobs
            ]
        }

@mcp.tool()
def task_cancel(job_id: int) -> dict:
    """Cancel a scheduled task that hasn't completed yet.
    
    Args:
        job_id: The job ID to cancel
    """
    with SessionLocal() as db:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job is None:
            return {"error": f"Job {job_id} not found"}
        if job.status in ("completed", "failed"):
            return {"error": f"Cannot cancel job in '{job.status}' state"}
        job.status = "cancelled"
        db.commit()
        return {"job_id": job.id, "status": "cancelled"}



# ===================================================================
# Resources & Prompts (Phase 4)
# ===================================================================

@mcp.resource("job://{job_id}/logs")
def get_job_logs(job_id: int) -> str:
    """Return live logs and details of a specific job."""
    with SessionLocal() as db:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job is None:
            return f"Job {job_id} not found."
        return job.logs or "No logs available for this job."

@mcp.resource("system://database-schema")
def get_database_schema() -> str:
    """Provide read-only access to the database schema (app/models.py)."""
    models_path = os.path.join(project_root, "app", "models.py")
    try:
        with open(models_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Could not read schema: {e}"

@mcp.prompt("daily_review")
def daily_review_prompt() -> str:
    """Aggregate recently completed tasks and upcoming chains for a daily standup/review."""
    with SessionLocal() as db:
        recent_jobs = (
            db.query(Job)
            .filter(Job.status.in_(["completed", "failed"]))
            .order_by(Job.updated_at.desc())
            .limit(10)
            .all()
        )
        upcoming_jobs = (
            db.query(Job)
            .filter(Job.status.in_(["pending", "queued"]))
            .order_by(Job.scheduled_at.asc())
            .limit(10)
            .all()
        )
        
    context = "Here is the data from the task scheduler database.\\n\\nRECENTLY COMPLETED/FAILED JOBS:\\n"
    if not recent_jobs:
        context += "- None\\n"
    for j in recent_jobs:
        context += f"- Job {j.id}: {j.description} (Status: {j.status}, Executed: {j.updated_at})\\n"
        if j.result:
            result_excerpt = j.result[:100].replace('\\n', ' ')
            context += f"  Result excerpt: {result_excerpt}...\\n"
            
    context += "\\nUPCOMING PENDING JOBS:\\n"
    if not upcoming_jobs:
        context += "- None\\n"
    for j in upcoming_jobs:
        context += f"- Job {j.id}: {j.description} (Scheduled: {j.scheduled_at}, Cron: {j.cron_expr})\\n"
        
    prompt_text = (
        f"{context}\\n\\n"
        "Based on this data, please provide a daily review/standup report for me. "
        "Highlight any failures or important completed tasks, and summarize what I have coming up."
    )
    
    return prompt_text


# ===================================================================
# Entry point
# ===================================================================

def main() -> None:
    # 1. Check for the environment variable first (standard for Horizon/Cloud)
    default_port = int(os.environ.get("PORT", 8000))

    parser = argparse.ArgumentParser(description="MCP server for the task scheduler.")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio", help="Transport to use (stdio or sse)")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to for SSE transport")
    parser.add_argument("--port", type=int, default=default_port, help="Port to bind to for SSE transport")
    args = parser.parse_args()

    start_scheduler()

    if args.transport == "sse":
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
