"""
FastAPI application.

Endpoints:
  POST /admin/clients          create a new client (returns an api_key)
  GET  /api/demo/orders        example protected endpoint (rate limited)
  GET  /api/demo/products      another example protected endpoint
  GET  /analytics/top-abusers  which clients hit their limit most
  GET  /analytics/tier-rates   average request volume per tier
  GET  /analytics/client/{id}/hourly   per-client hourly request volume

Rate limiting is enforced via middleware so EVERY /api/* route is
protected automatically — you don't have to remember to add a
dependency to each new endpoint individually.

Run locally:
    uvicorn app.main:app --reload

Try it:
    curl -X POST localhost:8000/admin/clients \
      -H "Content-Type: application/json" \
      -d '{"name": "Test Co", "tier": "FREE"}'

    curl localhost:8000/api/demo/orders -H "X-API-Key: <key_from_above>"
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import db
from app.gateway import RateLimitGateway

DB_PATH = os.environ.get("GATEWAY_DB_PATH", "gateway.db")
ALGORITHM = os.environ.get("GATEWAY_ALGORITHM", "token_bucket")

app = FastAPI(title="Rate-Limited API Gateway")
gateway = RateLimitGateway(algorithm=ALGORITHM)


def get_conn():
    """Open one connection per call; SQLite handles concurrent reads fine
    for a project of this scale. Swap for a psycopg2/asyncpg pool for
    the PostgreSQL version in Phase 4."""
    return db.connect(DB_PATH)


# ---------------------------------------------------------------------
# Middleware: enforces the rate limit on every /api/* route.
# ---------------------------------------------------------------------


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)

    api_key = request.headers.get("X-API-Key")
    if not api_key:
        return JSONResponse(
            status_code=401, content={"detail": "Missing X-API-Key header"}
        )

    conn = get_conn()
    try:
        decision = gateway.check(conn, api_key, endpoint=request.url.path)
    finally:
        conn.close()

    if decision is None:
        return JSONResponse(status_code=401, content={"detail": "Invalid API key"})

    if not decision.allowed:
        return JSONResponse(
            status_code=429,
            content={
                "detail": "Rate limit exceeded",
                "retry_after_seconds": round(decision.retry_after_seconds, 2),
            },
            headers={"Retry-After": str(int(decision.retry_after_seconds) + 1)},
        )

    response = await call_next(request)
    response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
    return response


# ---------------------------------------------------------------------
# Admin: client management
# ---------------------------------------------------------------------


class CreateClientRequest(BaseModel):
    name: str
    tier: str = "FREE"


@app.post("/admin/clients")
def create_client(payload: CreateClientRequest):
    import secrets

    api_key = secrets.token_urlsafe(24)
    conn = get_conn()
    try:
        client_id = db.create_client(conn, api_key, payload.name, payload.tier)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()
    return {"client_id": client_id, "api_key": api_key, "tier": payload.tier}


# ---------------------------------------------------------------------
# Demo protected endpoints — these exist purely to demonstrate the
# gateway enforcing limits on real routes.
# ---------------------------------------------------------------------


@app.get("/api/demo/orders")
def list_orders():
    return {"orders": ["order_1", "order_2", "order_3"]}


@app.get("/api/demo/products")
def list_products():
    return {"products": ["widget", "gadget", "gizmo"]}


# ---------------------------------------------------------------------
# Analytics — surfaces the SQL queries from db.py as real endpoints.
# ---------------------------------------------------------------------


@app.get("/analytics/top-abusers")
def analytics_top_abusers(limit: int = 5):
    conn = get_conn()
    try:
        rows = db.top_abusers(conn, limit=limit)
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/analytics/tier-rates")
def analytics_tier_rates():
    conn = get_conn()
    try:
        rows = db.average_rate_by_tier(conn)
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/analytics/client/{client_id}/hourly")
def analytics_client_hourly(client_id: int):
    conn = get_conn()
    try:
        rows = db.requests_per_hour(conn, client_id)
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/health")
def health():
    return {"status": "ok"}
