"""
Unit tests for the DSA core.

Run with:  pytest tests/test_algorithms.py -v

These tests specifically target the edge cases interviewers ask about:
- burst behavior
- boundary conditions (exactly at the limit)
- window expiry / refill over time
- per-client isolation (client A's usage shouldn't affect client B)
"""

import time

import pytest

from app.algorithms import SlidingWindowLog, TokenBucket, build_strategy


class TestTokenBucket:
    def test_allows_requests_up_to_capacity(self):
        bucket = TokenBucket(capacity=3, refill_rate=1)
        results = [bucket.allow_request("client_a") for _ in range(3)]
        assert all(r.allowed for r in results)

    def test_rejects_once_capacity_exhausted(self):
        bucket = TokenBucket(capacity=2, refill_rate=0.001)  # near-zero refill
        assert bucket.allow_request("client_a").allowed is True
        assert bucket.allow_request("client_a").allowed is True
        result = bucket.allow_request("client_a")
        assert result.allowed is False
        assert result.retry_after_seconds > 0

    def test_refills_over_time(self):
        bucket = TokenBucket(capacity=1, refill_rate=10)  # fast refill for test
        assert bucket.allow_request("client_a").allowed is True
        assert bucket.allow_request("client_a").allowed is False
        time.sleep(0.15)  # should refill ~1.5 tokens
        assert bucket.allow_request("client_a").allowed is True

    def test_clients_are_isolated(self):
        bucket = TokenBucket(capacity=1, refill_rate=0.001)
        assert bucket.allow_request("client_a").allowed is True
        # client_b has never made a request, should have a full bucket
        assert bucket.allow_request("client_b").allowed is True
        # client_a should now be rejected, independent of client_b
        assert bucket.allow_request("client_a").allowed is False


class TestSlidingWindowLog:
    def test_allows_requests_up_to_max(self):
        limiter = SlidingWindowLog(max_requests=3, window_seconds=5)
        results = [limiter.allow_request("client_a") for _ in range(3)]
        assert all(r.allowed for r in results)

    def test_rejects_once_max_reached_within_window(self):
        limiter = SlidingWindowLog(max_requests=2, window_seconds=5)
        limiter.allow_request("client_a")
        limiter.allow_request("client_a")
        result = limiter.allow_request("client_a")
        assert result.allowed is False
        assert result.retry_after_seconds > 0

    def test_no_boundary_burst_exploit(self):
        """
        The classic fixed-window bug: 2x max_requests could slip through
        near a window boundary. A sliding log must prevent this.
        """
        limiter = SlidingWindowLog(max_requests=2, window_seconds=0.2)
        limiter.allow_request("client_a")
        limiter.allow_request("client_a")
        time.sleep(0.15)  # still inside the original window
        result = limiter.allow_request("client_a")
        assert result.allowed is False  # would incorrectly pass with fixed-window

    def test_old_requests_expire_out_of_window(self):
        limiter = SlidingWindowLog(max_requests=1, window_seconds=0.1)
        assert limiter.allow_request("client_a").allowed is True
        assert limiter.allow_request("client_a").allowed is False
        time.sleep(0.15)
        assert limiter.allow_request("client_a").allowed is True

    def test_clients_are_isolated(self):
        limiter = SlidingWindowLog(max_requests=1, window_seconds=5)
        assert limiter.allow_request("client_a").allowed is True
        assert limiter.allow_request("client_b").allowed is True
        assert limiter.allow_request("client_a").allowed is False


class TestFactory:
    def test_build_token_bucket(self):
        strategy = build_strategy("token_bucket", capacity=5, refill_rate=1)
        assert isinstance(strategy, TokenBucket)

    def test_build_sliding_window(self):
        strategy = build_strategy(
            "sliding_window", max_requests=5, window_seconds=10
        )
        assert isinstance(strategy, SlidingWindowLog)

    def test_unknown_algorithm_raises(self):
        with pytest.raises(ValueError):
            build_strategy("made_up_algorithm")
