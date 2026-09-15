"""
Rate limiting algorithms implemented from scratch.

Two strategies are provided, both exposed through the same interface
(RateLimiterStrategy), so callers can swap algorithms without changing
any other code (Strategy design pattern).

1. TokenBucket
   - O(1) time per request, O(1) space per client.
   - Allows short bursts up to `capacity`, then enforces a steady
     refill rate.
   - Good default choice for most APIs.

2. SlidingWindowLog
   - O(k) time per request where k = number of requests in the
     current window (bounded by max_requests, so still small/constant
     in practice).
   - O(k) space per client.
   - More precise than a fixed window (no boundary-burst problem: a
     client can't send max_requests at 0:59 and another max_requests
     at 1:01 to effectively double their allowance).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: float = 0.0


class RateLimiterStrategy(ABC):
    """Common interface every algorithm implements."""

    @abstractmethod
    def allow_request(self, client_id: str) -> RateLimitResult:
        """Return whether a request from client_id is allowed right now."""
        raise NotImplementedError


class TokenBucket(RateLimiterStrategy):
    """
    Each client gets a bucket that holds up to `capacity` tokens.
    Tokens refill continuously at `refill_rate` tokens/second.
    Each request costs 1 token. If the bucket is empty, the request
    is rejected.

    Why O(1): we don't loop to add tokens every tick. Instead, on each
    request we compute how much time has passed since the last check
    and lazily top up the bucket by that amount. This avoids a
    background thread or a per-second job entirely.
    """

    def __init__(self, capacity: int, refill_rate: float):
        """
        capacity: max tokens a bucket can hold (burst size)
        refill_rate: tokens added per second (steady-state rate)
        """
        self.capacity = capacity
        self.refill_rate = refill_rate
        # client_id -> (current_tokens, last_checked_timestamp)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = Lock()

    def allow_request(self, client_id: str) -> RateLimitResult:
        now = time.monotonic()
        with self._lock:
            tokens, last_checked = self._buckets.get(
                client_id, (self.capacity, now)
            )

            # Lazily refill based on elapsed time since last check.
            elapsed = now - last_checked
            tokens = min(self.capacity, tokens + elapsed * self.refill_rate)

            if tokens >= 1:
                tokens -= 1
                self._buckets[client_id] = (tokens, now)
                return RateLimitResult(allowed=True, remaining=int(tokens))

            # Not enough tokens — compute how long until one is available.
            self._buckets[client_id] = (tokens, now)
            deficit = 1 - tokens
            retry_after = deficit / self.refill_rate
            return RateLimitResult(
                allowed=False, remaining=0, retry_after_seconds=retry_after
            )


class SlidingWindowLog(RateLimiterStrategy):
    """
    Keeps a timestamp log (deque) per client of every request in the
    last `window_seconds`. A request is allowed only if the number of
    timestamps still inside the window is below `max_requests`.

    This avoids the classic fixed-window boundary-burst bug: with a
    fixed window, a client could send max_requests right before a
    window boundary and max_requests right after, getting 2x the
    intended rate in a short span. A sliding log always looks at a
    continuously moving window, so that exploit doesn't work.

    Trade-off vs TokenBucket: uses more memory (stores every timestamp
    in the window instead of a single counter), but gives an exact
    rolling-window guarantee rather than an averaged rate.
    """

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._logs: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow_request(self, client_id: str) -> RateLimitResult:
        now = time.monotonic()
        with self._lock:
            log = self._logs.setdefault(client_id, deque())

            # Evict timestamps that have fallen out of the window.
            # Amortized O(1) per request: each timestamp is popped
            # exactly once over its lifetime.
            while log and now - log[0] > self.window_seconds:
                log.popleft()

            if len(log) < self.max_requests:
                log.append(now)
                remaining = self.max_requests - len(log)
                return RateLimitResult(allowed=True, remaining=remaining)

            # Rejected — earliest request in window tells us when a
            # slot frees up.
            retry_after = self.window_seconds - (now - log[0])
            return RateLimitResult(
                allowed=False, remaining=0, retry_after_seconds=retry_after
            )


def build_strategy(
    algorithm: str, **kwargs
) -> RateLimiterStrategy:
    """
    Factory function so the FastAPI layer (Phase 3) can pick an
    algorithm by name (e.g. from a config file or a DB column) without
    importing every class directly.
    """
    algorithm = algorithm.lower()
    if algorithm == "token_bucket":
        return TokenBucket(
            capacity=kwargs["capacity"], refill_rate=kwargs["refill_rate"]
        )
    if algorithm == "sliding_window":
        return SlidingWindowLog(
            max_requests=kwargs["max_requests"],
            window_seconds=kwargs["window_seconds"],
        )
    raise ValueError(f"Unknown algorithm: {algorithm}")
