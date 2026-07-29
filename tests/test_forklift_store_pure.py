"""DB-free unit tests for forklift_store's pure parsing helpers. Unlike
tests/test_forklift_store.py (whole-module, DATABASE_URL-gated), these exercise
the JSONB-parsing logic directly so they run everywhere."""
import pytest

from zira_dashboard import forklift_store


def test_recent_driver_throughput_pairs_daily_demand_with_on_call_time(monkeypatch):
    from zira_dashboard import db

    seen = {}

    def fake_query(sql, params):
        seen["sql"] = sql
        seen["params"] = params
        if "JOIN forklift_calls_daily" in sql:
            return [{"calls": 11_536, "ms": 2_183_328_000}]
        # Production failure shape: only today's driver calls divided by all
        # reconstructed on-call hours.
        return [{"calls": 437, "ms": 2_183_328_000}]

    monkeypatch.setattr(db, "query", fake_query)

    rate = forklift_store.recent_driver_throughput(days=28)

    assert rate == pytest.approx(19.02, abs=0.01)
    assert "JOIN forklift_calls_daily" in seen["sql"]
    assert seen["params"] == (28,)


def test_recent_driver_throughput_returns_none_for_thin_paired_history(monkeypatch):
    from zira_dashboard import db

    monkeypatch.setattr(
        db,
        "query",
        lambda *args, **kwargs: [{"calls": 10, "ms": 3_600_000}],
    )

    assert forklift_store.recent_driver_throughput(days=28) is None


def test_recent_claim_seconds_calls_weighted_mean(monkeypatch):
    from zira_dashboard import db
    monkeypatch.setattr(db, "query", lambda *a, **k: [{"wms": 3_000_000, "calls": 20}])
    assert forklift_store.recent_claim_seconds(90) == 150.0


def test_recent_claim_seconds_none_on_no_calls(monkeypatch):
    from zira_dashboard import db
    monkeypatch.setattr(db, "query", lambda *a, **k: [{"wms": 0, "calls": 0}])
    assert forklift_store.recent_claim_seconds(90) is None
