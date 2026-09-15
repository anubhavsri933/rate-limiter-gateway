"""
Persistence layer.

Uses sqlite3 for local dev/tests (zero setup, ships with Python) and
is written in plain parameterized SQL so it ports to PostgreSQL with
minimal changes for Phase 4's docker-compose setup (swap the connect()
function for psycopg2/asyncpg and this module barely changes).

Every function here takes a connection rather than opening its own,
so callers (and tests) control transaction boundaries and can use an
in-memory database for fast, isolated tests.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(db_path: str = ":memory:") -> sqlite3.Connection:
    """
    Open a connection and apply the schema if tables don't exist yet.
    db_path=':memory:' is used heavily in tests for a clean, fast,
    disposable database per test.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # access columns by name, not index
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    return conn


@contextmanager
def get_connection(db_path: str = "gateway.db"):
    """Context-manager wrapper so callers can `with get_connection() as conn:`"""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Data classes — typed shapes instead of passing raw sqlite3.Row around
# everywhere, so the FastAPI layer (Phase 3) gets clean objects.
# ---------------------------------------------------------------------


@dataclass
class Client:
    id: int
    api_key: str
    name: str
    tier_id: int
    tier_name: str
    max_requests: int
    window_seconds: int


# ---------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------


def create_client(
    conn: sqlite3.Connection, api_key: str, name: str, tier_name: str
) -> int:
    """Insert a new client under an existing tier. Returns the new client id."""
    tier = conn.execute(
        "SELECT id FROM tiers WHERE name = ?", (tier_name,)
    ).fetchone()
    if tier is None:
        raise ValueError(f"Unknown tier: {tier_name}")

    cursor = conn.execute(
        "INSERT INTO clients (api_key, name, tier_id) VALUES (?, ?, ?)",
        (api_key, name, tier["id"]),
    )
    conn.commit()
    return cursor.lastrowid


def log_request(
    conn: sqlite3.Connection, client_id: int, endpoint: str, allowed: bool
) -> None:
    """
    Record every request. This is the hottest write path in the system
    (one insert per API call), so it's a single narrow INSERT with no
    joins — kept deliberately cheap.
    """
    conn.execute(
        "INSERT INTO request_log (client_id, endpoint, allowed) VALUES (?, ?, ?)",
        (client_id, endpoint, int(allowed)),
    )
    if not allowed:
        conn.execute(
            "INSERT INTO violations (client_id, endpoint) VALUES (?, ?)",
            (client_id, endpoint),
        )
    conn.commit()


def cleanup_old_logs(conn: sqlite3.Connection, older_than_days: int = 30) -> int:
    """
    Background retention job: delete request_log rows older than N days
    so the table doesn't grow unbounded. Returns rows deleted.
    Run this periodically (e.g. daily cron / APScheduler job), not on
    every request.
    """
    cursor = conn.execute(
        "DELETE FROM request_log WHERE requested_at < datetime('now', ?)",
        (f"-{older_than_days} days",),
    )
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------


def get_client_by_api_key(conn: sqlite3.Connection, api_key: str) -> Client | None:
    """
    The lookup that runs on EVERY incoming request, so it's a single
    indexed join (api_key is UNIQUE -> indexed; tiers is tiny and
    fully cached by SQLite/Postgres in practice).
    """
    row = conn.execute(
        """
        SELECT c.id, c.api_key, c.name, c.tier_id,
               t.name AS tier_name, t.max_requests, t.window_seconds
        FROM clients c
        JOIN tiers t ON t.id = c.tier_id
        WHERE c.api_key = ?
        """,
        (api_key,),
    ).fetchone()
    if row is None:
        return None
    return Client(**dict(row))


# ---------------------------------------------------------------------
# Analytics — the queries that make this more than "just a CRUD app"
# ---------------------------------------------------------------------


def top_abusers(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    """
    'Who is hitting their rate limit the most?'
    Uses idx_violations_client_time — groups over an indexed column.
    """
    return conn.execute(
        """
        SELECT c.name, c.api_key, COUNT(v.id) AS violation_count
        FROM violations v
        JOIN clients c ON c.id = v.client_id
        GROUP BY v.client_id
        ORDER BY violation_count DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def requests_per_hour(conn: sqlite3.Connection, client_id: int) -> list[sqlite3.Row]:
    """
    Time-bucketed request volume for one client — a classic
    window-function-flavored query. SQLite lacks DATE_TRUNC, so we
    truncate the ISO timestamp string to the hour instead; the
    PostgreSQL version (Phase 4) uses DATE_TRUNC('hour', requested_at).
    """
    return conn.execute(
        """
        SELECT substr(requested_at, 1, 13) AS hour_bucket,
               COUNT(*) AS request_count,
               SUM(allowed) AS allowed_count,
               COUNT(*) - SUM(allowed) AS rejected_count
        FROM request_log
        WHERE client_id = ?
        GROUP BY hour_bucket
        ORDER BY hour_bucket
        """,
        (client_id,),
    ).fetchall()


def average_rate_by_tier(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    'Which tier generates the most traffic on average?'
    Joins three tables (tiers -> clients -> request_log) and aggregates
    — a good query to walk through in an interview to show you can
    reason about join order and what gets grouped.
    """
    return conn.execute(
        """
        SELECT t.name AS tier_name,
               COUNT(r.id) AS total_requests,
               COUNT(DISTINCT r.client_id) AS active_clients,
               ROUND(COUNT(r.id) * 1.0 / COUNT(DISTINCT r.client_id), 2)
                   AS avg_requests_per_client
        FROM tiers t
        JOIN clients c ON c.tier_id = t.id
        JOIN request_log r ON r.client_id = c.id
        GROUP BY t.id
        ORDER BY total_requests DESC
        """
    ).fetchall()
