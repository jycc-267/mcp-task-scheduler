import logging
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import NullPool

load_dotenv()

logger = logging.getLogger(__name__)

from urllib.parse import quote_plus

_DB_USER = quote_plus(os.environ["DB_USER"])
_DB_PASSWORD = quote_plus(os.environ["DB_PASSWORD"])
_DB_HOST = os.environ["DB_HOST"]
_DB_PORT = os.environ["DB_PORT"]
_DB_NAME = quote_plus(os.environ["DB_NAME"])

DATABASE_URL = (
    f"postgresql+psycopg2://{_DB_USER}:{_DB_PASSWORD}"
    f"@{_DB_HOST}:{_DB_PORT}/{_DB_NAME}?sslmode=require"
)

engine = create_engine(
    DATABASE_URL,
    poolclass=NullPool,   # No persistent pool: each thread opens/closes its own connection
    echo=False,
)

# Optional: Add a simple check to ensure the engine can connect
try:
    with engine.connect() as connection:
        logger.info("Successfully connected to the database.")
except Exception as e:
    logger.error(f"Database connection failed: {e}")
    # We don't raise here to allow the app to potentially start, 
    # but the error will be visible in the logs.

SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
