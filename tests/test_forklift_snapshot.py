from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from zira_dashboard import forklift_snapshot

# 1782484200000 = 2026-06-26 09:30 America/Chicago (within the snapshot day)
# 1782565200000 = 2026-06-27 08:00 America/Chicago (the NEXT plant-local day)
DAY = date(2026, 6, 26)


def test_snapshot_today_fetches_aggregates_and_stores(monkeypatch):
    captured = {"events": None}
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_completions",
                        lambda since=0: [
                            {"id": "c1", "workstationName": "Prosaw #4",
                             "completedBy": "fk-1", "createdAt": 1782484200000,
                             "responseMs": 120000, "handlingMs": 300000},
                            # next plant-local day -> must be filtered out of today
                            {"id": "c2", "workstationName": "Junior #3",
                             "completedBy": "fk-2", "createdAt": 1782565200000,
                             "responseMs": 60000, "handlingMs": 90000},
                        ])
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_drivers",
                        lambda: [{"id": "fk-1", "name": "Trent", "isOverloadResponder": False},
                                 {"id": "fk-2", "name": "Louie", "isOverloadResponder": True}])
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_calls_daily",
                        lambda row: captured.setdefault("calls", row))
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_driver_daily",
                        lambda rows: captured.setdefault("drivers", rows) or len(rows))
    monkeypatch.setattr(
        forklift_snapshot.forklift_event_store,
        "upsert_completion_events",
        lambda events: captured.update(events=events) or len(events),
    )
    covered_at = datetime(2026, 6, 26, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(forklift_snapshot, "_utc_now", lambda: covered_at)
    monkeypatch.setattr(
        forklift_snapshot.forklift_event_store,
        "record_completion_coverage",
        lambda day, *, covered_through_utc, raw_event_count: captured.update(
            coverage=(day, covered_through_utc, raw_event_count)
        ),
    )
    saved = {}
    monkeypatch.setattr(forklift_snapshot.app_settings, "set_setting",
                        lambda k, v: saved.update({k: v}))

    out = forklift_snapshot.snapshot_today(client=None, day=DAY)

    # only the same-day call counts; the next-day call is filtered out
    assert captured["calls"]["total_calls"] == 1
    assert captured["calls"]["by_station"] == {"Prosaw #4": 1}
    assert captured["calls"]["by_hour"]["9"]["calls"] == 1
    assert [d["driver_id"] for d in captured["drivers"]] == ["fk-1"]
    assert captured["drivers"][0]["name"] == "Trent"
    assert [event.event_id for event in captured["events"]] == ["c1", "c2"]
    assert captured["coverage"] == (DAY, covered_at, 1)
    assert saved["forklift_overload_responders"] == ["Louie"]
    assert out["day"] == "2026-06-26"
    assert out["calls"] == 1


def test_snapshot_today_preserves_overload_responders_without_flag(monkeypatch):
    """External /drivers is {id, name} only — do not clobber saved backups."""
    saved = {}
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_completions",
                        lambda since=0: [])
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_drivers",
                        lambda: [{"id": "fk-1", "name": "Trent"}])
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_calls_daily",
                        lambda row: None)
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_driver_daily",
                        lambda rows: 0)
    monkeypatch.setattr(forklift_snapshot.forklift_event_store,
                        "upsert_completion_events", lambda events: len(events))
    monkeypatch.setattr(
        forklift_snapshot.forklift_event_store,
        "record_completion_coverage",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(forklift_snapshot.app_settings, "set_setting",
                        lambda k, v: saved.update({k: v}))

    forklift_snapshot.snapshot_today(client=None, day=DAY)

    assert "forklift_overload_responders" not in saved


def test_snapshot_today_empty_day_writes_zero_row(monkeypatch):
    captured = {}
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_completions",
                        lambda since=0: [])
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_drivers", lambda: [])
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_calls_daily",
                        lambda row: captured.setdefault("calls", row))
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_driver_daily",
                        lambda rows: len(rows))
    monkeypatch.setattr(forklift_snapshot.forklift_event_store,
                        "upsert_completion_events", lambda events: len(events))
    monkeypatch.setattr(
        forklift_snapshot.forklift_event_store,
        "record_completion_coverage",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(forklift_snapshot.app_settings, "set_setting", lambda k, v: None)

    out = forklift_snapshot.snapshot_today(client=None, day=DAY)

    assert captured["calls"]["total_calls"] == 0
    assert captured["calls"]["day"] == DAY
    assert out["calls"] == 0


def test_snapshot_today_requests_since_day_start(monkeypatch):
    seen = {}
    def record_since(since=0):
        seen["since"] = since
        return []

    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_completions", record_since)
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_drivers", lambda: [])
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_calls_daily", lambda row: None)
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_driver_daily", lambda rows: 0)
    monkeypatch.setattr(forklift_snapshot.forklift_event_store,
                        "upsert_completion_events", lambda events: len(events))
    monkeypatch.setattr(
        forklift_snapshot.forklift_event_store,
        "record_completion_coverage",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(forklift_snapshot.app_settings, "set_setting", lambda k, v: None)

    forklift_snapshot.snapshot_today(client=None, day=DAY)
    # 2026-06-26 00:00 America/Chicago == 1782450000000 ms
    assert seen["since"] == 1782450000000


def test_snapshot_coverage_stops_at_fetch_start_not_later_write_time(monkeypatch):
    order = []
    fetch_started = datetime(2026, 6, 26, 17, 0, tzinfo=UTC)
    monkeypatch.setattr(
        forklift_snapshot,
        "_utc_now",
        lambda: order.append("clock") or fetch_started,
    )
    monkeypatch.setattr(
        forklift_snapshot.forklift_client,
        "fetch_completions",
        lambda since=0: order.append("fetch") or [],
    )
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_drivers", lambda: [])
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_calls_daily", lambda row: None)
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_driver_daily", lambda rows: 0)
    monkeypatch.setattr(
        forklift_snapshot.forklift_event_store,
        "upsert_completion_events",
        lambda events: 0,
    )
    captured = {}
    monkeypatch.setattr(
        forklift_snapshot.forklift_event_store,
        "record_completion_coverage",
        lambda day, **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(forklift_snapshot.app_settings, "set_setting", lambda *args: None)

    forklift_snapshot.snapshot_today(client=None, day=DAY)

    assert order[:2] == ["clock", "fetch"]
    assert captured["covered_through_utc"] == fetch_started


def test_snapshot_refuses_coverage_when_any_completion_cannot_be_stored(monkeypatch):
    writes = []
    monkeypatch.setattr(
        forklift_snapshot.forklift_client,
        "fetch_completions",
        lambda since=0: [{"id": "missing-driver", "createdAt": 1782484200000}],
    )
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_drivers", lambda: [])
    monkeypatch.setattr(
        forklift_snapshot.forklift_store,
        "upsert_calls_daily",
        lambda row: writes.append("calls"),
    )
    monkeypatch.setattr(
        forklift_snapshot.forklift_store,
        "upsert_driver_daily",
        lambda rows: writes.append("drivers"),
    )
    monkeypatch.setattr(
        forklift_snapshot.forklift_event_store,
        "upsert_completion_events",
        lambda events: writes.append("events"),
    )
    monkeypatch.setattr(
        forklift_snapshot.forklift_event_store,
        "record_completion_coverage",
        lambda *args, **kwargs: writes.append("coverage"),
    )

    with pytest.raises(ValueError, match="completion feed"):
        forklift_snapshot.snapshot_today(client=None, day=DAY)

    assert writes == []


def test_day_is_finalized_requires_coverage_through_next_local_midnight(monkeypatch):
    _start, day_end = forklift_snapshot._day_bounds_utc(DAY)
    settlement = day_end + forklift_snapshot.LATE_COMPLETION_WINDOW
    coverage = SimpleNamespace(covered_through_utc=settlement - timedelta(seconds=1))
    monkeypatch.setattr(
        forklift_snapshot.forklift_event_store,
        "completion_coverage_for_day",
        lambda day: coverage,
    )

    assert forklift_snapshot.day_is_finalized(DAY) is False

    coverage.covered_through_utc = settlement
    assert forklift_snapshot.day_is_finalized(DAY) is True


def test_past_day_snapshot_keeps_late_completion_watermark(monkeypatch):
    _start, day_end = forklift_snapshot._day_bounds_utc(DAY)
    checked_at = day_end + timedelta(hours=3)
    captured = {}
    monkeypatch.setattr(forklift_snapshot, "_utc_now", lambda: checked_at)
    monkeypatch.setattr(
        forklift_snapshot.forklift_client,
        "fetch_completions",
        lambda since=0: [],
    )
    monkeypatch.setattr(forklift_snapshot.forklift_client, "fetch_drivers", lambda: [])
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_calls_daily", lambda row: None)
    monkeypatch.setattr(forklift_snapshot.forklift_store, "upsert_driver_daily", lambda rows: 0)
    monkeypatch.setattr(
        forklift_snapshot.forklift_event_store,
        "upsert_completion_events",
        lambda events: 0,
    )
    monkeypatch.setattr(
        forklift_snapshot.forklift_event_store,
        "record_completion_coverage",
        lambda day, **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(forklift_snapshot.app_settings, "set_setting", lambda *args: None)

    forklift_snapshot.snapshot_today(client=None, day=DAY)

    assert captured["covered_through_utc"] == checked_at
