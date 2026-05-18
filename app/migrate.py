import logging
from app.database import engine, Base
# Import all models here so that Base has them registered before create_all
from app.models import Job

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def migrate():
    logger.info("Starting schema migration...")
    Base.metadata.create_all(bind=engine)
    logger.info("Schema migration completed successfully.")

if __name__ == "__main__":
    migrate()
