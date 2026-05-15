import logging
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import NullPool

load_dotenv()

logger = logging.getLogger(__name__)

_DB_USER = os.environ["DB_USER"]
_DB_PASSWORD = os.environ["DB_PASSWORD"]
_DB_HOST = os.environ["DB_HOST"]
_DB_PORT = os.environ["DB_PORT"]
_DB_NAME = os.environ["DB_NAME"]

DATABASE_URL = (
    f"postgresql+psycopg2://{_DB_USER}:{_DB_PASSWORD}"
    f"@{_DB_HOST}:{_DB_PORT}/{_DB_NAME}?sslmode=require"
)

engine = create_engine(
    DATABASE_URL,
    poolclass=NullPool,   # No persistent pool: each thread opens/closes its own connection
    echo=False,
)

SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
