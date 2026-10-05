"""Small, idempotent schema migrations for the two supported databases."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3


SQLITE_STAGE5_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS telegram_users (
        chat_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        registered_at TEXT NOT NULL, active INTEGER NOT NULL,
        is_owner INTEGER NOT NULL, last_manual_check_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS telegram_invites (
        id INTEGER PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
        created_by_chat_id INTEGER NOT NULL REFERENCES telegram_users(chat_id),
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        used_at TEXT, used_by_chat_id INTEGER REFERENCES telegram_users(chat_id))""",
    """CREATE TABLE IF NOT EXISTS check_locks (
        lock_name TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
        locked_until TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS telegram_users_active_idx ON telegram_users(active)",
    "CREATE INDEX IF NOT EXISTS telegram_invites_expiry_idx ON telegram_invites(expires_at)",
)


def apply_sqlite_migrations(db: sqlite3.Connection) -> None:
    with db:
        for statement in SQLITE_STAGE5_SCHEMA:
            db.execute(statement)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(?,?)",
                   (1, datetime.now(timezone.utc).isoformat()))


POSTGRES_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS search_runs (
        id BIGSERIAL PRIMARY KEY, started_at TEXT NOT NULL,
        completed_at TEXT, status TEXT NOT NULL,
        source TEXT NOT NULL, error_message TEXT)""",
    """CREATE TABLE IF NOT EXISTS offers (
        id BIGSERIAL PRIMARY KEY, search_run_id BIGINT NOT NULL REFERENCES search_runs(id),
        checked_at_utc TEXT NOT NULL, source TEXT NOT NULL,
        marketing_carrier TEXT NOT NULL, operating_carrier TEXT,
        flight_number TEXT NOT NULL, flight_key TEXT NOT NULL,
        origin_airport TEXT NOT NULL, destination_airport TEXT NOT NULL,
        departure_local TEXT NOT NULL, arrival_local TEXT NOT NULL,
        timezone TEXT NOT NULL, direct BOOLEAN NOT NULL,
        price TEXT NOT NULL, currency TEXT NOT NULL,
        seats_available INTEGER, search_id TEXT, offer_id TEXT,
        confirmed BOOLEAN NOT NULL DEFAULT FALSE,
        baggage_category TEXT, baggage_code TEXT)""",
    "CREATE INDEX IF NOT EXISTS offers_flight_idx ON offers(flight_key)",
    "CREATE INDEX IF NOT EXISTS offers_confirmed_idx ON offers(confirmed)",
    """CREATE TABLE IF NOT EXISTS notifications (
        id BIGSERIAL PRIMARY KEY, timestamp TEXT NOT NULL,
        type TEXT NOT NULL, flight_key TEXT NOT NULL,
        price TEXT NOT NULL, previous_price TEXT,
        status TEXT NOT NULL, error_message TEXT)""",
    """CREATE TABLE IF NOT EXISTS telegram_users (
        chat_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
        registered_at TIMESTAMPTZ NOT NULL, active BOOLEAN NOT NULL,
        is_owner BOOLEAN NOT NULL, last_manual_check_at TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS telegram_invites (
        id BIGSERIAL PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
        created_by_chat_id BIGINT NOT NULL REFERENCES telegram_users(chat_id),
        created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
        used_at TIMESTAMPTZ, used_by_chat_id BIGINT REFERENCES telegram_users(chat_id))""",
    """CREATE TABLE IF NOT EXISTS check_locks (
        lock_name TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
        locked_until TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS telegram_users_active_idx ON telegram_users(active)",
    "CREATE INDEX IF NOT EXISTS telegram_invites_expiry_idx ON telegram_invites(expires_at)",
)


def apply_postgres_migrations(db) -> None:
    with db.transaction():
        db.execute("SELECT pg_advisory_xact_lock(572393812)")
        for statement in POSTGRES_SCHEMA:
            db.execute(statement)
        db.execute("""INSERT INTO schema_migrations(version,applied_at) VALUES(%s,%s)
            ON CONFLICT(version) DO NOTHING""", (1, datetime.now(timezone.utc)))
