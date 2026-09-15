# Rate-Limited API Gateway

A small API gateway that enforces per-client, per-tier rate limits using
algorithms implemented from scratch (no rate-limiting library), backed
by a relational schema for persistence and analytics.

Built to demonstrate three things together: algorithmic reasoning (DSA),
relational data modeling (SQL), and production practices (Docker, CI,
tests) — the combination most fresher SDE interviews probe for.

---

## Architecture

```
Client request
      │
      ▼
┌─────────────────────┐
│  FastAPI middleware   │  ← reads X-API-Key, applies to every /api/* route
└─────────┬────────────┘
          │
          ▼
┌─────────────────────┐      ┌──────────────────────┐
│   RateLimitGateway    │◄────►│  SQLite / Postgres    │
│  (app/gateway.py)     │      │  (app/db.py)          │
│                       │      │                       │
│  1 strategy instance  │      │  clients, tiers,      │
│  per TIER, holding    │      │  request_log,         │
│  per-CLIENT state     │      │  violations           │
└─────────┬────────────┘      └──────────────────────┘
          │
          ▼
┌─────────────────────┐
│  TokenBucket /        │
│  SlidingWindowLog     │  ← app/algorithms.py, pure Python, no dependencies
└─────────────────────┘
```

## Schema

```
tiers                    clients                   request_log
────────                  ─────────                  ────────────
id (PK)                   id (PK)                    id (PK)
name (FREE/PRO/ENT)       api_key (UNIQUE)           client_id (FK → clients)
max_requests              name                       endpoint
window_seconds            tier_id (FK → tiers)       allowed (0/1)
                          created_at                 requested_at

                                                     violations
                                                     ────────────
                                                     id (PK)
                                                     client_id (FK → clients)
                                                     endpoint
                                                     occurred_at
```

**Why this shape:**
- `tiers` is separate from `clients` so a limit change (e.g. FREE goes
  from 100 → 150 req/min) is a single-row `UPDATE`, not a mass update
  across every client row. Classic normalization to avoid update
  anomalies.
- `violations` duplicates a subset of `request_log`'s data on purpose.
  `request_log` is the highest-write-volume table in the system (one
  insert per request), so it's kept narrow and rarely filtered. Abuse
  queries ("who's hitting limits the most") are common enough to
  deserve their own smaller, purpose-indexed table — a deliberate
  denormalization traded for read performance on a hot query path.
- Every index in `schema.sql` is commented with the specific query it
  serves — indexes were added to match query patterns, not guessed.

## Algorithms

| Algorithm | Time / request | Space | Behavior |
|---|---|---|---|
| Token Bucket | O(1) | O(1) per client | Allows bursts up to `capacity`, then a steady refill rate. Lazy refill (computed from elapsed time) — no background thread needed. |
| Sliding Window Log | O(k) amortized (k = requests in window, bounded by `max_requests`) | O(k) per client | Exact rolling window. Immune to the fixed-window boundary-burst exploit (sending `max_requests` right before a window boundary and another `max_requests` right after, doubling the effective rate). |

Both share one interface (`RateLimiterStrategy`) via the Strategy
pattern, so swapping algorithms is a one-line config change
(`GATEWAY_ALGORITHM=token_bucket` or `sliding_window`), not a rewrite.

One strategy instance is created **per tier**, not per client, since
memory usage should scale with (active tiers × per-client state) —
constant in the number of tiers, growing only with actual traffic.

## Running locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

```bash
# create a client
curl -X POST localhost:8000/admin/clients \
  -H "Content-Type: application/json" \
  -d '{"name": "Test Co", "tier": "FREE"}'

# use the returned api_key
curl localhost:8000/api/demo/orders -H "X-API-Key: <key>"

# check analytics
curl localhost:8000/analytics/top-abusers
curl localhost:8000/analytics/tier-rates
```

## Running with Docker

```bash
docker compose up --build
```

Data persists in a named volume across restarts (`gateway_data`).

## Running tests

```bash
pytest tests/ -v
```

29 tests covering: algorithm edge cases (burst limits, refill timing,
per-client isolation, the boundary-burst exploit specifically),
persistence layer (client creation, request logging, all three
analytics queries), and full HTTP integration tests (auth, rate
limiting, independent client state).

CI runs this automatically on every push via GitHub Actions
(`.github/workflows/ci.yml`), plus a Docker build to catch container
issues before they reach a reviewer.

## What's intentionally left out (documented, not hidden)

- **PostgreSQL**: the project uses SQLite for simplicity and zero
  external setup. `db.py` is written so a Postgres swap only touches
  that one file — function signatures used by `gateway.py` and
  `main.py` would stay identical. This was left as documented future
  work rather than shipped as untested database code.
- **Distributed rate limiting (Redis)**: this gateway's algorithm
  state lives in-process, so it works correctly for a single instance
  but wouldn't share state across multiple replicas. A production
  multi-instance deployment would move the bucket/window state into
  Redis (e.g. via a Lua script for atomicity). Worth mentioning as
  the natural next scaling step.

## Interview talking points (things you should be able to explain cold)

1. **Why token bucket over sliding window as the default?** Token
   bucket allows legitimate bursts (a client that's been idle can
   briefly send more than the steady-state rate), which matches how
   most real APIs behave. Sliding window is stricter/more precise but
   less forgiving of bursty-but-legitimate traffic.

2. **Why is `allow_request` O(1) for token bucket but O(k) for sliding
   window?** Token bucket only ever stores two numbers per client
   (token count, last-checked time) and does arithmetic. Sliding
   window must store and prune a timestamp per request in the window,
   so its cost scales with how many requests are currently in-window.

3. **What happens if two requests from the same client arrive at
   the exact same instant?** Both algorithms use a `threading.Lock`
   around the check-and-update, so requests are serialized per
   process — no race condition where two concurrent requests both
   read "1 token left" and both proceed.

4. **Why middleware instead of a per-route dependency?** New `/api/*`
   routes are protected automatically without remembering to add a
   dependency — reduces the chance of shipping an unprotected endpoint
   by accident.

5. **How would this scale to multiple server instances?** It
   currently wouldn't share rate-limit state across instances (see
   "what's left out" above) — the fix is moving algorithm state to
   Redis so all instances check against the same counters.
