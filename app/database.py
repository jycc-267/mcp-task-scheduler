import logging
import os
import sqlite3
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import QueuePool

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./chatgpt_task.db")
SERVICE_ACCOUNT = os.environ.get("SERVICE_ACCOUNT")
PROJECT_ID = os.environ.get("PROJECT_ID")
DB_REGION = os.environ.get("DB_REGION", "us-central1")
DB_INSTANCE_NAME = os.environ.get("DB_INSTANCE_NAME", "jycchien-mcp-scheduler")
DB_NAME = os.environ.get("DB_NAME", "task-scheduler-db")

def init_connection_engine() -> create_engine:
    # 1. Cloud SQL IAM Auth Mode (GCP)
    if SERVICE_ACCOUNT:
        logger.info("Initializing Cloud SQL connection engine with IAM authentication...")
        try:
            from google.cloud.sql.connector import Connector
            
            connector = Connector()
            project_id = PROJECT_ID or ""
            instance_connection_name = f"{project_id}:{DB_REGION}:{DB_INSTANCE_NAME}"
            db_user = SERVICE_ACCOUNT.replace(".gserviceaccount.com", "")
            
            def getconn():
                return connector.connect(
                    instance_connection_name,
                    "pg8000",
                    user=db_user,
                    db=DB_NAME,
                    enable_iam_auth=True
                )
                
            return create_engine(
                "postgresql+pg8000://",
                creator=getconn,
                pool_size=1,
                max_overflow=2,
                pool_recycle=1800,
            )
        except Exception as error:
            logger.error("Failed to initialize Cloud SQL Connector with IAM: %s. Falling back.", error)

    # 2. Standard URL fallback (useful for local PostgreSQL tests)
    if DATABASE_URL.startswith("postgresql"):
        logger.info("Initializing standard PostgreSQL database engine using DATABASE_URL...")
        return create_engine(
            DATABASE_URL,
            pool_size=1,
            max_overflow=2,
            pool_recycle=1800,
        )

    # 3. Local SQLite fallback (default)
    logger.info("Initializing SQLite database engine...")
    return create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False, "timeout": 15},
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
    )

engine = init_connection_engine()

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
