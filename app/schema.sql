-- =====================================================================
-- Rate Limiter / API Gateway — Schema
-- =====================================================================
-- Design notes (put these in your README / say them out loud in
-- interviews):
--
-- 1. Normalized to 3NF: tiers are their own table so limits live in
--    ONE place. If a "PRO" tier's limit changes, you update one row,
--    not every client row.
--
-- 2. request_log is intentionally append-only and narrow (few columns)
--    because it's the highest-write-volume table in the system — every
--    single API call inserts a row here. Keeping it narrow keeps
--    inserts cheap and indexes small.
--
-- 3. violations is a separate table from request_log, even though a
--    violation IS a request. Why: violations are low-volume and
--    high-signal (you query "who is abusing us" much more often than
--    "show me every request"), so splitting them lets you index and
--    query violations without scanning the entire request_log.
--
-- 4. Indexes are added specifically to support the analytics queries
--    in analytics.sql — not added blindly. Each index has a comment
--    explaining which query it serves.
-- =====================================================================

CREATE TABLE IF NOT EXISTS tiers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,      -- 'FREE', 'PRO', 'ENTERPRISE'
    max_requests    INTEGER NOT NULL,          -- limit per window
    window_seconds  INTEGER NOT NULL,          -- window size in seconds
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS clients (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key         TEXT NOT NULL UNIQUE,      -- what the caller sends us
    name            TEXT NOT NULL,
    tier_id         INTEGER NOT NULL REFERENCES tiers(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Every single request that hits the gateway, allowed or not.
-- Append-only. No UPDATEs, no DELETEs except the retention cleanup job.
CREATE TABLE IF NOT EXISTS request_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       INTEGER NOT NULL REFERENCES clients(id),
    endpoint        TEXT NOT NULL,
    allowed         INTEGER NOT NULL,          -- 1 = allowed, 0 = rejected
    requested_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per REJECTED request. Redundant with request_log by design
-- (see note #3 above) — this is a deliberate denormalization for
-- read performance on the "who's abusing us" query path.
CREATE TABLE IF NOT EXISTS violations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       INTEGER NOT NULL REFERENCES clients(id),
    endpoint        TEXT NOT NULL,
    occurred_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Serves: "how many requests has this client made recently" and the
-- per-client time-series queries in analytics.sql.
CREATE INDEX IF NOT EXISTS idx_request_log_client_time
    ON request_log (client_id, requested_at);

-- Serves: "top N abusers this week" — grouping/counting by client
-- over a time range.
CREATE INDEX IF NOT EXISTS idx_violations_client_time
    ON violations (client_id, occurred_at);

-- Serves: fast api_key -> client lookup on every single request.
-- (UNIQUE constraint above already creates this in SQLite/Postgres,
-- kept here explicitly for readability/documentation.)
-- CREATE UNIQUE INDEX idx_clients_api_key ON clients(api_key);

-- Seed the standard tiers.
INSERT OR IGNORE INTO tiers (name, max_requests, window_seconds) VALUES
    ('FREE', 100, 60),
    ('PRO', 1000, 60),
    ('ENTERPRISE', 10000, 60);
