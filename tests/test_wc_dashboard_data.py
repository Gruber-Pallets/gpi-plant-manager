"""Unit tests for wc_dashboard_data helpers.

Pure functions only — these tests don't need a DB and run unconditionally.
"""
from __future__ import annotations


def test_slug_simple():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("Repair 1") == "repair-1"


def test_slug_lowercases():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("REPAIR 1") == "repair-1"


def test_slug_collapses_punctuation():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("Hand Build #1") == "hand-build-1"


def test_slug_strips_leading_trailing_hyphens():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("  Bay 4  ") == "bay-4"
    assert slug_for_wc("--repair-1--") == "repair-1"


def test_slug_collapses_runs_of_hyphens():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("Repair   1") == "repair-1"
    assert slug_for_wc("Hand // Build") == "hand-build"


def test_slug_keeps_digits():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("Trim Saw 12") == "trim-saw-12"


def test_slug_empty_input():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("") == ""
    assert slug_for_wc("   ") == ""


import os
from datetime import date as _date

import pytest


def test_wc_by_slug_resolves_known_slug(monkeypatch):
    from zira_dashboard import wc_dashboard_data

    class _Loc:
        def __init__(self, name): self.name = name

    from zira_dashboard import staffing
    monkeypatch.setattr(
        staffing, "LOCATIONS",
        [_Loc("Repair 1"), _Loc("Hand Build #1")],
    )
    loc = wc_dashboard_data.wc_by_slug("repair-1")
    assert loc is not None and loc.name == "Repair 1"
    loc2 = wc_dashboard_data.wc_by_slug("hand-build-1")
    assert loc2 is not None and loc2.name == "Hand Build #1"


def test_wc_by_slug_unknown_returns_none(monkeypatch):
    from zira_dashboard import wc_dashboard_data, staffing
    monkeypatch.setattr(staffing, "LOCATIONS", [])
    assert wc_dashboard_data.wc_by_slug("ghost") is None


def test_assigned_operators_for_wc(monkeypatch):
    from zira_dashboard import wc_dashboard_data, staffing

    monkeypatch.setattr(staffing, "load_schedule", lambda d: staffing.Schedule(
        day=d, published=True,
        assignments={"Repair 1": ["Christian", "Jose L"], "Repair 2": ["Alice"]},
    ))
    out = wc_dashboard_data.assigned_operators_for_wc("Repair 1", _date(2026, 5, 13))
    assert out == ["Christian", "Jose L"]


def test_assigned_operators_unassigned_returns_empty(monkeypatch):
    from zira_dashboard import wc_dashboard_data, staffing
    monkeypatch.setattr(staffing, "load_schedule", lambda d: staffing.Schedule(
        day=d, published=True, assignments={},
    ))
    assert wc_dashboard_data.assigned_operators_for_wc("Repair 1", _date(2026, 5, 13)) == []


def test_pallets_banner_data(monkeypatch):
    """Pallets banner: today's units vs prorated target for THIS WC."""
    from zira_dashboard import wc_dashboard_data, work_centers_store
    # 200-unit/day WC; shift is half elapsed → prorated target 100.
    class _Loc:
        name = "Repair 1"
    monkeypatch.setattr(
        wc_dashboard_data, "_load_wc", lambda nm: _Loc() if nm == "Repair 1" else None
    )
    monkeypatch.setattr(work_centers_store, "goal_per_day", lambda loc: 200)
    monkeypatch.setattr(wc_dashboard_data, "_units_today_for_wc", lambda nm, d: 87)
    monkeypatch.setattr(wc_dashboard_data, "_shift_elapsed_fraction", lambda d: 0.5)

    out = wc_dashboard_data.pallets_banner("Repair 1", _date(2026, 5, 13))
    assert out["units_today"] == 87
    assert out["target_today"] == 100  # 200 * 0.5
    assert out["target_full_day"] == 200
    assert out["pct_of_target"] == pytest.approx(87.0)  # 87/100*100


def test_monthly_ribbons_uses_group(monkeypatch):
    """Monthly ribbons come from the WC's group, not the WC itself."""
    from zira_dashboard import wc_dashboard_data, work_centers_store, awards

    class _Loc:
        name = "Repair 1"
    monkeypatch.setattr(wc_dashboard_data, "_load_wc", lambda nm: _Loc() if nm == "Repair 1" else None)
    monkeypatch.setattr(work_centers_store, "groups", lambda loc: ["Repairs"])
    monkeypatch.setattr(
        awards, "monthly_badges",
        lambda group, year, month: [
            {"position": 1, "name": "Christian", "day": _date(2026, 5, 4), "units": 145, "pph": 16.1},
            {"position": 2, "name": "Lauro",     "day": _date(2026, 5, 9), "units": 132, "pph": 14.7},
        ] if group == "Repairs" else [],
    )
    out = wc_dashboard_data.monthly_ribbons("Repair 1", 2026, 5)
    assert out["group"] == "Repairs"
    assert len(out["entries"]) == 2
    assert out["entries"][0]["name"] == "Christian"


def test_goat_race_uses_group(monkeypatch):
    """GOAT race compares against the WC's group's all-time GOAT."""
    from zira_dashboard import wc_dashboard_data, work_centers_store, awards

    class _Loc:
        name = "Repair 1"
    monkeypatch.setattr(wc_dashboard_data, "_load_wc", lambda nm: _Loc() if nm == "Repair 1" else None)
    monkeypatch.setattr(work_centers_store, "groups", lambda loc: ["Repairs"])
    monkeypatch.setattr(
        awards, "goat",
        lambda group: {"name": "Christian", "day": _date(2026, 2, 15), "units": 145, "pph": 16.1}
            if group == "Repairs" else None,
    )
    # 87 units today vs GOAT's 145-units day, half elapsed → GOAT pace today = 145 * 0.5 = 72.5
    monkeypatch.setattr(wc_dashboard_data, "_units_today_for_wc", lambda nm, d: 87)
    monkeypatch.setattr(wc_dashboard_data, "_shift_elapsed_fraction", lambda d: 0.5)

    out = wc_dashboard_data.goat_race("Repair 1", _date(2026, 5, 13))
    assert out["group"] == "Repairs"
    assert out["units_today"] == 87
    assert out["goat"]["name"] == "Christian"
    assert out["goat"]["units"] == 145
    assert out["goat_pace_today"] == pytest.approx(72.5)
    assert out["status"] == "AHEAD"  # 87 > 72.5


def test_goat_race_status_on_pace_when_within_5pct(monkeypatch):
    from zira_dashboard import wc_dashboard_data, work_centers_store, awards

    class _Loc:
        name = "Repair 1"
    monkeypatch.setattr(wc_dashboard_data, "_load_wc", lambda nm: _Loc())
    monkeypatch.setattr(work_centers_store, "groups", lambda loc: ["Repairs"])
    monkeypatch.setattr(
        awards, "goat",
        lambda group: {"name": "Christian", "day": _date(2026, 2, 15), "units": 100, "pph": 12.5},
    )
    # 50 today, GOAT pace = 100 * 0.5 = 50 — exactly on pace.
    monkeypatch.setattr(wc_dashboard_data, "_units_today_for_wc", lambda nm, d: 50)
    monkeypatch.setattr(wc_dashboard_data, "_shift_elapsed_fraction", lambda d: 0.5)

    out = wc_dashboard_data.goat_race("Repair 1", _date(2026, 5, 13))
    assert out["status"] == "ON_PACE"


def test_goat_race_no_goat_yet(monkeypatch):
    from zira_dashboard import wc_dashboard_data, work_centers_store, awards

    class _Loc:
        name = "Repair 1"
    monkeypatch.setattr(wc_dashboard_data, "_load_wc", lambda nm: _Loc())
    monkeypatch.setattr(work_centers_store, "groups", lambda loc: ["Repairs"])
    monkeypatch.setattr(awards, "goat", lambda group: None)
    monkeypatch.setattr(wc_dashboard_data, "_units_today_for_wc", lambda nm, d: 30)
    monkeypatch.setattr(wc_dashboard_data, "_shift_elapsed_fraction", lambda d: 0.5)

    out = wc_dashboard_data.goat_race("Repair 1", _date(2026, 5, 13))
    assert out["goat"] is None
    assert out["status"] is None


def _fake_readings(per_minute):
    """Helper: build a fake list of {ts_utc, units} dicts where
    `per_minute` is a list of (minute_offset_from_shift_start, units) tuples.
    """
    from datetime import datetime, timezone, timedelta
    from zira_dashboard.shift_config import shift_start_for, SITE_TZ
    today = datetime.now(SITE_TZ).date()
    shift_start = datetime.combine(today, shift_start_for(today), tzinfo=SITE_TZ)
    return [
        {
            "ts_utc": shift_start.astimezone(timezone.utc) + timedelta(minutes=m),
            "units": u,
        }
        for m, u in per_minute
    ]


def test_daily_progress_cumulative(monkeypatch):
    """Daily progress: list of (minute_offset, cumulative_units) at 15-min granularity."""
    from zira_dashboard import wc_dashboard_data, staffing
    from datetime import datetime, timezone
    from zira_dashboard.shift_config import SITE_TZ
    # Stub the schedule DB lookup that shift_start_for / productive_minutes_for need.
    monkeypatch.setattr(staffing, "load_schedule",
        lambda d: staffing.Schedule(
            day=d, published=False,
            custom_hours={"start": "07:00", "end": "15:30", "breaks": []},
        ))
    today = datetime.now(SITE_TZ).date()

    # Three readings at minutes 5, 30, 80 with 10 / 20 / 5 units each.
    readings = _fake_readings([(5, 10), (30, 20), (80, 5)])
    monkeypatch.setattr(
        wc_dashboard_data, "_readings_for_wc_today",
        lambda nm, d: readings,
    )
    out = wc_dashboard_data.daily_progress("Repair 1", today)
    # At least 6 buckets returned (shift is many hours long).
    assert len(out) >= 6
    # Reading at minute 5 → bucket 0 (0-14m); cumulative 10.
    assert out[0]["cumulative_units"] == 10
    # Reading at minute 30 → bucket 2 (30-44m); cumulative 30.
    assert out[2]["cumulative_units"] == 30
    # Reading at minute 80 → bucket 5 (75-89m); cumulative 35.
    assert out[5]["cumulative_units"] == 35


def test_fifteen_min_increments_color_coded(monkeypatch):
    """Each 15-min bucket: units in that interval + green/amber/red flag."""
    from zira_dashboard import wc_dashboard_data, staffing
    from datetime import datetime
    from zira_dashboard.shift_config import SITE_TZ
    monkeypatch.setattr(staffing, "load_schedule",
        lambda d: staffing.Schedule(
            day=d, published=False,
            custom_hours={"start": "07:00", "end": "15:30", "breaks": []},
        ))
    today = datetime.now(SITE_TZ).date()

    # Target = 8 units / bucket — control via _wc_target_per_bucket.
    monkeypatch.setattr(wc_dashboard_data, "_wc_target_per_bucket", lambda nm, d: 8)
    # bucket 0 → 10 units (green), bucket 1 → 6 (amber, ≥ 75% of 8 = 6),
    # bucket 2 → 4 (red, < 75% = 6).
    readings = _fake_readings([(5, 10), (20, 6), (35, 4)])
    monkeypatch.setattr(
        wc_dashboard_data, "_readings_for_wc_today",
        lambda nm, d: readings,
    )
    out = wc_dashboard_data.fifteen_min_increments("Repair 1", today)
    assert out[0]["units"] == 10 and out[0]["color"] == "green"
    assert out[1]["units"] == 6  and out[1]["color"] == "amber"
    assert out[2]["units"] == 4  and out[2]["color"] == "red"


def test_downtime_report(monkeypatch):
    """Downtime: list of {time, duration_minutes} events derived from
    gaps in active_intervals, plus an authoritative total from
    StationTotal.downtime_minutes."""
    from zira_dashboard import wc_dashboard_data
    from datetime import datetime
    from zira_dashboard.shift_config import SITE_TZ
    today = datetime.now(SITE_TZ).date()

    class _Total:
        downtime_minutes = 11
    monkeypatch.setattr(wc_dashboard_data, "_station_total_for_wc",
                        lambda nm, d: _Total())
    monkeypatch.setattr(
        wc_dashboard_data, "_downtime_events_for_wc",
        lambda nm, d: [
            {"time": "9:42a",  "duration_minutes": 3},
            {"time": "11:15a", "duration_minutes": 8},
        ],
    )
    out = wc_dashboard_data.downtime_report("Repair 1", today)
    assert out["total_minutes"] == 11
    assert len(out["events"]) == 2
    assert "reason" not in out["events"][0]


def test_fifteen_min_progress_buckets_truncates_at_now(monkeypatch):
    """On today, buckets stop at the current 15-min slot — no future buckets."""
    from datetime import date
    from zira_dashboard import wc_dashboard_data

    fake_raw = [
        {"minute_offset": off, "units": 5, "target": 10}
        for off in range(0, 480, 15)  # 32 buckets across an 8-hour shift
    ]
    monkeypatch.setattr(wc_dashboard_data, "fifteen_min_increments",
                        lambda wc, d: fake_raw)
    # Half the shift elapsed.
    monkeypatch.setattr(wc_dashboard_data, "_shift_elapsed_fraction",
                        lambda d: 0.5)

    result = wc_dashboard_data.fifteen_min_progress_buckets("Repair 1", date(2026, 5, 14))
    buckets = result["buckets"]
    # 0.5 * 480 = 240 minutes elapsed -> offsets 0..240 inclusive => 17 buckets
    assert len(buckets) == 17, f"expected 17 buckets, got {len(buckets)}"
    assert all(b["offset"] <= 240 for b in buckets)
    assert buckets[-1]["offset"] == 240
    # Exactly one bucket marked in_progress.
    in_progress = [b for b in buckets if b["in_progress"]]
    assert len(in_progress) == 1
    assert in_progress[0]["offset"] == 240


def test_fifteen_min_progress_buckets_past_day_full_shift(monkeypatch):
    """On past days, shift elapsed fraction is 1.0 — all buckets returned."""
    from datetime import date
    from zira_dashboard import wc_dashboard_data

    fake_raw = [
        {"minute_offset": off, "units": 7, "target": 10}
        for off in range(0, 480, 15)
    ]
    monkeypatch.setattr(wc_dashboard_data, "fifteen_min_increments",
                        lambda wc, d: fake_raw)
    monkeypatch.setattr(wc_dashboard_data, "_shift_elapsed_fraction",
                        lambda d: 1.0)

    result = wc_dashboard_data.fifteen_min_progress_buckets("Repair 1", date(2026, 5, 1))
    assert len(result["buckets"]) == 32
    # No bucket should be flagged in_progress on a past day.
    assert not any(b["in_progress"] for b in result["buckets"])
