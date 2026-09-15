"""
Tests for the persistence layer.

Uses an in-memory SQLite DB per test (via the `conn` fixture) so tests
are fast and fully isolated from each other — no shared state, no
cleanup needed between tests.
"""

import pytest

from app import db


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


class TestClientCreation:
    def test_create_client_assigns_correct_tier(self, conn):
        client_id = db.create_client(conn, "key_abc", "Acme Corp", "FREE")
        client = db.get_client_by_api_key(conn, "key_abc")

        assert client is not None
        assert client.id == client_id
        assert client.tier_name == "FREE"
        assert client.max_requests == 100
        assert client.window_seconds == 60

    def test_unknown_tier_raises(self, conn):
        with pytest.raises(ValueError):
            db.create_client(conn, "key_xyz", "Bad Corp", "NOT_A_REAL_TIER")

    def test_lookup_missing_api_key_returns_none(self, conn):
        assert db.get_client_by_api_key(conn, "does_not_exist") is None


class TestRequestLogging:
    def test_log_allowed_request(self, conn):
        client_id = db.create_client(conn, "key_1", "Client One", "PRO")
        db.log_request(conn, client_id, "/api/data", allowed=True)

        row = conn.execute(
            "SELECT * FROM request_log WHERE client_id = ?", (client_id,)
        ).fetchone()
        assert row["allowed"] == 1

        # A successful request should NOT create a violation row.
        violation_count = conn.execute(
            "SELECT COUNT(*) as c FROM violations WHERE client_id = ?",
            (client_id,),
        ).fetchone()["c"]
        assert violation_count == 0

    def test_log_rejected_request_creates_violation(self, conn):
        client_id = db.create_client(conn, "key_2", "Client Two", "FREE")
        db.log_request(conn, client_id, "/api/data", allowed=False)

        violation_count = conn.execute(
            "SELECT COUNT(*) as c FROM violations WHERE client_id = ?",
            (client_id,),
        ).fetchone()["c"]
        assert violation_count == 1


class TestAnalytics:
    def test_top_abusers_orders_by_violation_count(self, conn):
        heavy = db.create_client(conn, "key_heavy", "Heavy User", "FREE")
        light = db.create_client(conn, "key_light", "Light User", "FREE")

        for _ in range(3):
            db.log_request(conn, heavy, "/api/data", allowed=False)
        db.log_request(conn, light, "/api/data", allowed=False)

        results = db.top_abusers(conn, limit=5)
        assert results[0]["name"] == "Heavy User"
        assert results[0]["violation_count"] == 3
        assert results[1]["name"] == "Light User"
        assert results[1]["violation_count"] == 1

    def test_average_rate_by_tier(self, conn):
        c1 = db.create_client(conn, "key_pro1", "Pro Client 1", "PRO")
        c2 = db.create_client(conn, "key_pro2", "Pro Client 2", "PRO")

        db.log_request(conn, c1, "/x", allowed=True)
        db.log_request(conn, c1, "/x", allowed=True)
        db.log_request(conn, c2, "/x", allowed=True)

        results = db.average_rate_by_tier(conn)
        pro_row = next(r for r in results if r["tier_name"] == "PRO")
        assert pro_row["total_requests"] == 3
        assert pro_row["active_clients"] == 2
        assert pro_row["avg_requests_per_client"] == 1.5

    def test_requests_per_hour_counts_allowed_and_rejected(self, conn):
        client_id = db.create_client(conn, "key_3", "Client Three", "FREE")
        db.log_request(conn, client_id, "/x", allowed=True)
        db.log_request(conn, client_id, "/x", allowed=True)
        db.log_request(conn, client_id, "/x", allowed=False)

        results = db.requests_per_hour(conn, client_id)
        assert len(results) == 1  # all in the same hour bucket
        assert results[0]["request_count"] == 3
        assert results[0]["allowed_count"] == 2
        assert results[0]["rejected_count"] == 1


class TestCleanup:
    def test_cleanup_reports_zero_when_nothing_old(self, conn):
        client_id = db.create_client(conn, "key_4", "Fresh Client", "FREE")
        db.log_request(conn, client_id, "/x", allowed=True)

        deleted = db.cleanup_old_logs(conn, older_than_days=30)
        assert deleted == 0  # log entry is brand new, nothing to delete
