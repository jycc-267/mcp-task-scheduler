import sqlite3
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import QueuePool

DATABASE_URL = "sqlite:///./chatgpt_task.db"

# Enforce strict connection pooling to handle concurrent DB access
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 15},
    poolclass=QueuePool,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
