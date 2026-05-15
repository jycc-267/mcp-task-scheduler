# Supabase Migrations

This folder contains raw SQL migration files that mirror the SQLAlchemy
ORM models in `app/models.py`. They are applied automatically by
`Base.metadata.create_all()` on first server startup, but are kept here
as a human-readable audit trail.

## Applying Manually

1. Open the Supabase Dashboard → **SQL Editor**
2. Paste and run `migrations/001_initial_schema.sql`

## Connection Details

The application connects using env vars defined in `.env`:

| Variable | Description |
|---|---|
| `DB_USER` | Database user (default: `postgres`) |
| `DB_PASSWORD` | Database password |
| `DB_HOST` | Supabase database host |
| `DB_PORT` | Database port (default: `5432`) |
| `DB_NAME` | Database name (default: `postgres`) |
