import argparse
import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from fastmcp import FastMCP

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
def task_create(description: str, scheduled_at: str, cron_expr: str | None = None, parent_job_id: int | None = None) -> dict:
    """Schedule a new task for future execution.
    
    Args:
        description: What the task should do
        scheduled_at: When to run, ISO 8601 format (e.g. 2026-05-03T10:00:00)
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
            parent_job_id=parent_job_id
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return {
            "job_id": job.id, 
            "status": job.status, 
            "scheduled_at": str(job.scheduled_at),
            "cron_expr": job.cron_expr,
            "parent_job_id": job.parent_job_id
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
            "result": job.result,
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
                    "parent_job_id": j.parent_job_id
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

@mcp.tool()
async def nlp_task_create(query: str) -> dict:
    """Create a task from a natural language description using LLM parsing.

    Takes a free-form text query (e.g., "Summarize the news every Friday at 5pm")
    and uses an LLM to extract structured scheduling parameters, then creates
    the task automatically.

    Args:
        query: Natural language description of the task to schedule
    """
    from app.llm_parser import parse_task

    try:
        schema = await parse_task(query)
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"LLM parsing failed: {e}"}

    parent_job_id = None
    if schema.dependencies:
        if len(schema.dependencies) > 1:
            logger.warning(
                "nlp_task_create: %d dependencies returned, only first will be used: %s",
                len(schema.dependencies),
                schema.dependencies,
            )
        parent_job_id = schema.dependencies[0]

    return task_create(
        description=schema.description,
        scheduled_at=schema.scheduled_at,
        cron_expr=schema.cron_expr,
        parent_job_id=parent_job_id,
    )

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

    Base.metadata.create_all(bind=engine)
    start_scheduler()

    if args.transport == "sse":
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
