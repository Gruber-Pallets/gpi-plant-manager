"""Unit tests for cached_leaderboard's per-meter cache keying and the
today-persist throttle.

fetch_station_day and _zira_persist are stubbed so no Zira/Postgres is
needed; these tests exercise the cache assembly logic only.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from zira_dashboard import leaderboard as lb
from zira_dashboard.stations import Station


@pytest.fixture(autouse=True)
def _fresh_caches():
    lb._TODAY_CACHE.invalidate()
    lb._PAST_CACHE.invalidate()
    lb._LAST_PERSIST.clear()
    yield
    lb._TODAY_CACHE.invalidate()
    lb._PAST_CACHE.invalidate()
    lb._LAST_PERSIST.clear()


def _wire(monkeypatch):
    """Stub the Zira fetch + Postgres persist. Returns (calls, saved)."""
    calls: list[str] = []
    saved: list[tuple[list[str], date]] = []

    def fake_fetch(client, station, start_iso, end_iso, now_utc=None):
        calls.append(station.meter_id)
        return lb.StationTotal(
            station=station,
            units=int(station.meter_id),
            reading_count=1,
            truncated=False,
            downtime_minutes=0,
            active_minutes=0,
            last_reading_at=None,
            last_status=None,
            samples=(),
            active_intervals=(),
        )

    from zira_dashboard import _zira_persist
    monkeypatch.setattr(lb, "fetch_station_day", fake_fetch)
    monkeypatch.setattr(
        _zira_persist, "save_day",
        lambda totals, day: saved.append(([t.station.meter_id for t in totals], day)),
    )
    monkeypatch.setattr(_zira_persist, "load_day", lambda stations, day: None)
    return calls, saved


def _station(meter_id: str, name: str | None = None) -> Station:
    return Station(meter_id=meter_id, name=name or f"WC {meter_id}",
                   category="Repair", cell="Recycling")


def test_overlapping_sets_share_per_meter_entries(monkeypatch):
    """A second request only fetches the meters the first one didn't."""
    calls, _ = _wire(monkeypatch)
    today = datetime.now(timezone.utc).date()
    s1, s2, s3 = _station("1"), _station("2"), _station("3")

    out = lb.cached_leaderboard(None, [s1, s2], today)
    assert sorted(calls) == ["1", "2"]
    # Contract: one StationTotal per station, sorted by (-units, name).
    assert [t.station.meter_id for t in out] == ["2", "1"]

    out2 = lb.cached_leaderboard(None, [s2, s3], today)
    assert sorted(calls) == ["1", "2", "3"]  # only meter 3 was fetched
    assert [t.station.meter_id for t in out2] == ["3", "2"]


def test_single_station_request_is_a_cache_hit_after_set_fetch(monkeypatch):
    calls, _ = _wire(monkeypatch)
    today = datetime.now(timezone.utc).date()
    s1, s2 = _station("1"), _station("2")

    lb.cached_leaderboard(None, [s1, s2], today)
    n = len(calls)
    out = lb.cached_leaderboard(None, [s1], today)
    assert len(calls) == n  # no new fetch
    assert len(out) == 1 and out[0].station.meter_id == "1"


def test_cached_entry_relabeled_to_requested_station(monkeypatch):
    """A cache entry warmed via one call site's Station object is returned
    under the *requested* Station so name-matching callers still work."""
    calls, _ = _wire(monkeypatch)
    today = datetime.now(timezone.utc).date()
    lb.cached_leaderboard(None, [_station("1", name="WC 1")], today)

    other_label = Station(meter_id="1", name="WC 1", category="Other", cell="Other")
    out = lb.cached_leaderboard(None, [other_label], today)
    assert len(calls) == 1  # cache hit
    assert out[0].station == other_label


def test_today_persist_throttled_per_meter(monkeypatch):
    """Refetches within the throttle window don't re-upsert to Postgres."""
    calls, saved = _wire(monkeypatch)
    today = datetime.now(timezone.utc).date()
    s1, s2 = _station("1"), _station("2")

    lb.cached_leaderboard(None, [s1, s2], today)
    assert [(sorted(m), d) for m, d in saved] == [(["1", "2"], today)]

    # Expire the in-process cache to force a refetch; persist stays throttled.
    lb._TODAY_CACHE.invalidate()
    lb.cached_leaderboard(None, [s1, s2], today)
    assert len(calls) == 4
    assert len(saved) == 1

    # Once the throttle window has passed, the next fetch persists again.
    for k in lb._LAST_PERSIST:
        lb._LAST_PERSIST[k] -= lb._PERSIST_INTERVAL_SECONDS
    lb._TODAY_CACHE.invalidate()
    lb.cached_leaderboard(None, [s1, s2], today)
    assert [(sorted(m), d) for m, d in saved] == [(["1", "2"], today), (["1", "2"], today)]


def test_past_day_persist_not_throttled(monkeypatch):
    calls, saved = _wire(monkeypatch)
    past = date(2024, 5, 1)
    s1 = _station("1")

    lb.cached_leaderboard(None, [s1], past)
    lb._PAST_CACHE.invalidate()
    lb.cached_leaderboard(None, [s1], past)
    assert saved == [(["1"], past), (["1"], past)]


def test_past_day_postgres_hit_skips_fetch(monkeypatch):
    calls, saved = _wire(monkeypatch)
    past = date(2024, 5, 2)
    s1 = _station("1")
    hit = lb.StationTotal(
        station=s1, units=42, reading_count=5, truncated=False,
        downtime_minutes=0, active_minutes=0, last_reading_at=None,
        last_status=None, samples=(), active_intervals=(),
    )
    from zira_dashboard import _zira_persist
    monkeypatch.setattr(_zira_persist, "load_day", lambda stations, day: [hit])

    out = lb.cached_leaderboard(None, [s1], past)
    assert calls == []  # no Zira fetch
    assert saved == []  # nothing re-persisted
    assert out[0].units == 42


def test_today_semantics_use_plant_local_date_from_supplied_now(monkeypatch):
    calls, _saved = _wire(monkeypatch)
    from zira_dashboard import _zira_persist

    loaded = []
    monkeypatch.setattr(
        _zira_persist,
        "load_day",
        lambda stations, day: loaded.append(day) or None,
    )
    local_now = datetime(2026, 8, 28, 23, 30, tzinfo=ZoneInfo("America/Chicago"))

    lb.cached_leaderboard(
        None,
        [_station("1")],
        local_now.date(),
        now_utc=local_now.astimezone(timezone.utc),
    )

    assert calls == ["1"]
    assert loaded == []


def test_station_total_for_returns_single_total(monkeypatch):
    _wire(monkeypatch)
    today = datetime.now(timezone.utc).date()
    s2 = _station("2")
    total = lb.station_total_for(None, s2, today)
    assert total is not None
    assert total.units == 2
    assert total.station == s2


def test_station_total_for_swallows_fetch_errors(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("zira down")
    monkeypatch.setattr(lb, "leaderboard", boom)
    today = datetime.now(timezone.utc).date()
    assert lb.station_total_for(None, _station("9"), today) is None
