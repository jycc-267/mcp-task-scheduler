import logging
from sqlalchemy import text, inspect
from app.database import engine, Base
# Import all models here so that Base has them registered before create_all
from app.models import Job

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def migrate():
    logger.info("Starting schema migration...")
    logger.info(f"Engine dialect: {engine.dialect.name}")
    logger.info(f"Engine URL: {repr(engine.url)}")
    Base.metadata.create_all(bind=engine)
    
    # Grant permissions to the public role so all other users (like mcp-user or developers) can see/query the tables
    if engine.dialect.name == "postgresql":
        try:
            logger.info("Granting all tables and sequences permissions to public role...")
            with engine.begin() as conn:
                conn.execute(text("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO public"))
                conn.execute(text("GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO public"))
            logger.info("Successfully granted schema public table permissions.")
        except Exception as e:
            logger.error("Failed to grant public database privileges: %s", e)
    
    # Dialect-agnostic column inspection (perfect for SQLite, PostgreSQL, etc.)
    inspector = inspect(engine)
    try:
        if inspector.has_table("jobs"):
            columns = [col["name"] for col in inspector.get_columns("jobs")]
            if "timezone" not in columns:
                logger.info("Adding 'timezone' column to 'jobs' table...")
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE jobs ADD COLUMN timezone VARCHAR(50) DEFAULT 'UTC'"))
                logger.info("'timezone' column added successfully.")
            else:
                logger.info("'timezone' column already exists in 'jobs' table.")
    except Exception as e:
        logger.error("Failed to run dynamic column migration: %s", e)

    logger.info("Schema migration completed successfully.")

if __name__ == "__main__":
    migrate()
