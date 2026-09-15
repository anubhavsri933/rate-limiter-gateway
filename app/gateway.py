"""
The gateway layer: glues the DSA algorithms (algorithms.py) to the
SQL persistence layer (db.py).

Key design decision: we build ONE strategy instance PER TIER (not per
client), because each algorithm class already tracks per-client state
internally via an internal dict keyed by client_id (see algorithms.py).
Tiers differ in their limits (FREE vs PRO vs ENTERPRISE), so each tier
needs its own configured instance; clients within the same tier safely
share that instance since it isolates them internally.

This means: 3 tiers -> 3 strategy objects total, no matter how many
thousands of clients you have. Memory grows with (active clients x
algorithm state), not with (tiers), which is the efficient shape.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app import db
from app.algorithms import RateLimiterStrategy, build_strategy


@dataclass
class GatewayDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: float
    client_name: str


class RateLimitGateway:
    """
    Public entry point used by the FastAPI layer. Call `check()` once
    per incoming request.
    """

    def __init__(self, algorithm: str = "token_bucket"):
        self.algorithm = algorithm
        # tier_name -> configured strategy instance for that tier
        self._strategies: dict[str, RateLimiterStrategy] = {}

    def _get_strategy_for_tier(
        self, tier_name: str, max_requests: int, window_seconds: int
    ) -> RateLimiterStrategy:
        """Lazily build (once) and cache a strategy instance per tier."""
        if tier_name not in self._strategies:
            if self.algorithm == "token_bucket":
                # Convert "max_requests per window_seconds" into a
                # steady-state refill rate, while allowing a burst up
                # to max_requests (the bucket capacity).
                refill_rate = max_requests / window_seconds
                self._strategies[tier_name] = build_strategy(
                    "token_bucket",
                    capacity=max_requests,
                    refill_rate=refill_rate,
                )
            else:
                self._strategies[tier_name] = build_strategy(
                    "sliding_window",
                    max_requests=max_requests,
                    window_seconds=window_seconds,
                )
        return self._strategies[tier_name]

    def check(
        self, conn: sqlite3.Connection, api_key: str, endpoint: str
    ) -> GatewayDecision | None:
        """
        Returns None if the api_key doesn't correspond to any known
        client (caller should respond 401), otherwise a GatewayDecision.
        Also logs the outcome to the database as a side effect, so
        every checked request is auditable.
        """
        client = db.get_client_by_api_key(conn, api_key)
        if client is None:
            return None

        strategy = self._get_strategy_for_tier(
            client.tier_name, client.max_requests, client.window_seconds
        )
        result = strategy.allow_request(client.api_key)

        db.log_request(conn, client.id, endpoint, allowed=result.allowed)

        return GatewayDecision(
            allowed=result.allowed,
            remaining=result.remaining,
            retry_after_seconds=result.retry_after_seconds,
            client_name=client.name,
        )
