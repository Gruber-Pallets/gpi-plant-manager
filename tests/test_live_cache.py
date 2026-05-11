import os
from datetime import date, datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="Postgres tests need DATABASE_URL",
)


def _reset_caches():
    from zira_dashboard import db
    db.execute("DELETE FROM today_attendance_cache")
    db.execute("DELETE FROM today_timeoff_cache")
    db.execute("DELETE FROM today_production_cache")


def test_write_then_read_attendance():
    from zira_dashboard import db, live_cache
    db.init_pool(); db.bootstrap_schema(); _reset_caches()

    payload = {"some": "data", "list": [1, 2, 3]}
    live_cache.write_attendance(date(2099, 9, 1), payload)
    got, refreshed_at = live_cache.read_attendance(date(2099, 9, 1))
    assert got == payload
    assert refreshed_at is not None


def test_read_missing_returns_none():
    from zira_dashboard import db, live_cache
    db.init_pool(); db.bootstrap_schema(); _reset_caches()
    got, refreshed_at = live_cache.read_attendance(date(2099, 9, 2))
    assert got is None
    assert refreshed_at is None


def test_write_then_overwrite_attendance():
    from zira_dashboard import db, live_cache
    db.init_pool(); db.bootstrap_schema(); _reset_caches()

    live_cache.write_attendance(date(2099, 9, 3), {"v": 1})
    live_cache.write_attendance(date(2099, 9, 3), {"v": 2})
    got, _ = live_cache.read_attendance(date(2099, 9, 3))
    assert got == {"v": 2}


def test_is_stale_threshold():
    from zira_dashboard import live_cache
    fresh = datetime.now(timezone.utc) - timedelta(seconds=30)
    stale = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert live_cache.is_stale(fresh) is False
    assert live_cache.is_stale(stale) is True
    assert live_cache.is_stale(None) is True


def test_refresh_attendance_calls_stratustime_and_writes_cache(monkeypatch):
    from zira_dashboard import db, live_cache
    db.init_pool(); db.bootstrap_schema(); _reset_caches()

    called = {}

    def fake_attendance(day):
        called["day"] = day
        return {"E1": {"status": "no_punch"}}

    monkeypatch.setattr(
        "zira_dashboard.stratustime_client.attendance_for_day", fake_attendance
    )

    live_cache.refresh_attendance(date(2099, 9, 4))
    got, _ = live_cache.read_attendance(date(2099, 9, 4))
    assert called["day"] == date(2099, 9, 4)
    assert got == {"E1": {"status": "no_punch"}}


def test_refresh_attendance_swallows_errors(monkeypatch):
    from zira_dashboard import db, live_cache
    db.init_pool(); db.bootstrap_schema(); _reset_caches()

    def boom(day):
        raise RuntimeError("stratustime down")

    monkeypatch.setattr(
        "zira_dashboard.stratustime_client.attendance_for_day", boom
    )

    # Must not raise — warmer relies on this.
    live_cache.refresh_attendance(date(2099, 9, 5))
    # No row was written (the failure happened before write).
    got, _ = live_cache.read_attendance(date(2099, 9, 5))
    assert got is None
