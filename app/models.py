from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    """Naive UTC datetime — replacement for deprecated datetime.utcnow()."""
    return datetime.now(UTC).replace(tzinfo=None)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    time_bucket: Mapped[str] = mapped_column(String(10), nullable=False, index=True) # add partition
    description: Mapped[str] = mapped_column(Text, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, queued, running, completed, failed, cancelled
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    cron_expr: Mapped[str | None] = mapped_column(String(100), nullable=True)
    parent_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    logs: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        # 建立專為 Watcher 設計的「部分索引」
        # 只有 pending 的任務會進入索引，優化故障恢復時的範圍查詢 (<=)
        Index(
            "idx_pending_scheduler",
            "time_bucket", 
            "scheduled_at",
            sqlite_where=text("status = 'pending'")  # 一旦任務被queued，它就會從索引中被剔除。
        ),
        Index("idx_bucket_status", "time_bucket", "status"),
    )
