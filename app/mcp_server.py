import argparse
from datetime import datetime

from fastmcp import FastMCP

from app.database import Base, SessionLocal, engine
from app.models import Job
from app.scheduler import get_time_bucket, start_scheduler

import os

# ===================================================================
# MCP server wiring
# ===================================================================

mcp = FastMCP("task-scheduler")

@mcp.tool()
def task_create(description: str, scheduled_at: str) -> dict:
    """Schedule a new task for future execution.
    
    Args:
        description: What the task should do
        scheduled_at: When to run, ISO 8601 format (e.g. 2026-05-03T10:00:00)
    """
    with SessionLocal() as db:
        dt = datetime.fromisoformat(scheduled_at)
        job = Job(
            description=description,
            scheduled_at=dt,
            time_bucket=get_time_bucket(dt),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return {"job_id": job.id, "status": job.status, "scheduled_at": str(job.scheduled_at)}

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
        return {
            "job_id": job.id,
            "description": job.description,
            "status": job.status,
            "scheduled_at": str(job.scheduled_at),
            "result": job.result,
        }

@mcp.tool()
def task_list() -> dict:
    """List all scheduled tasks."""
    with SessionLocal() as db:
        jobs = db.query(Job).order_by(Job.scheduled_at.desc()).all()
        return {
            "jobs": [
                {
                    "job_id": j.id,
                    "description": j.description,
                    "status": j.status,
                    "scheduled_at": str(j.scheduled_at),
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
