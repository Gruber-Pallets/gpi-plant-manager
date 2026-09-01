from datetime import date, timedelta

import pytest

from zira_dashboard import production_history
from zira_dashboard.assignment_windows import WorkSegment
from zira_dashboard.production_history import attribute_for_day, attribute_for_segments


def test_attribute_for_day_empty_schedule_returns_empty():
    out = attribute_for_day(
        assignments={},
        wc_totals={},
        elapsed_minutes=480,
    )
    assert out == {}


def test_metered_station_totals_forwards_cap_and_all_metered_locations(monkeypatch):
    from datetime import datetime, timezone

    from zira_dashboard import leaderboard, staffing

    day = date(2026, 8, 31)
    cap = datetime(2026, 8, 31, 18, 30, tzinfo=timezone.utc)
    calls = []
    monkeypatch.setattr(
        staffing,
        "LOCATIONS",
        [
            staffing.Location(
                "Repair 1", "Repair", "Bay 1", "Recycled", "meter-1"
            ),
            staffing.Location("Office", "Support", "Office", "Office", None),
            staffing.Location(
                "Dismantler 2",
                "Dismantler",
                "Bay 2",
                "Recycled",
                "meter-2",
            ),
        ],
    )
    monkeypatch.setattr(
        leaderboard,
        "cached_leaderboard",
        lambda client, stations, selected_day, now_utc=None: calls.append(
            (client, stations, selected_day, now_utc)
        )
        or [],
    )
    client = object()

    assert production_history.metered_station_totals(client, day, cap) == []
    assert calls[0][0] is client
    assert calls[0][2:] == (day, cap)
    assert [station.meter_id for station in calls[0][1]] == ["meter-1", "meter-2"]
    assert [station.name for station in calls[0][1]] == ["Repair 1", "Dismantler 2"]


def test_timeline_scoring_rejects_positive_total_without_timestamped_samples():
    from zira_dashboard.leaderboard import StationTotal
    from zira_dashboard.stations import Station
    from tests.people_performance_fixtures import DAY, END, START, span

    total = StationTotal(
        station=Station("m1", "Repair 1", "Repair", "Bay 1"),
        units=10,
        reading_count=0,
        truncated=False,
        downtime_minutes=0,
        active_minutes=0,
        last_reading_at=None,
        last_status=None,
        samples=(),
        active_intervals=(),
    )

    with pytest.raises(
        production_history.ProductionSourceUnavailable,
        match="Timestamped samples for Repair 1",
    ):
        production_history.production_scores_for_timeline(
            object(),
            DAY,
            (span(81, "Worker", 0, 60, "Repair 1"),),
            now_utc=END,
            is_today=False,
            window_start_utc=START,
            window_end_utc=END,
            station_totals=(total,),
            attribution_rows=(),
        )


def test_timeline_scoring_removes_testing_samples_and_identity_breakdown_minutes(
    monkeypatch,
):
    from zira_dashboard import production_segments, settings_store, shift_config
    from zira_dashboard.leaderboard import StationTotal
    from zira_dashboard.stations import Station
    from tests.people_performance_fixtures import DAY, END, START, span

    total = StationTotal(
        station=Station("m1", "Repair 1", "Repair", "Bay 1"),
        units=10,
        reading_count=2,
        truncated=False,
        downtime_minutes=0,
        active_minutes=60,
        last_reading_at=START + timedelta(minutes=20),
        last_status="Working",
        samples=(
            (START + timedelta(minutes=10), 4),
            (START + timedelta(minutes=20), 6),
        ),
        active_intervals=((START, START + timedelta(minutes=60)),),
    )
    rows = (
        {
            "id": 1,
            "wc_name": "Repair 1",
            "person_name": "Testing",
            "employee_odoo_id": None,
            "start_utc": START + timedelta(minutes=5),
            "end_utc": START + timedelta(minutes=15),
            "source": "testing",
            "breakdown_id": None,
        },
        {
            "id": 2,
            "wc_name": "Repair 1",
            "person_name": "Worker",
            "employee_odoo_id": 81,
            "start_utc": START + timedelta(minutes=30),
            "end_utc": START + timedelta(minutes=40),
            "source": "breakdown",
            "breakdown_id": 9,
        },
    )
    captured = {}
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(settings_store, "station_target", lambda station: 10.0)

    def capture(segments, **kwargs):
        captured.update(kwargs)
        captured["minutes"] = {
            item.person_odoo_id: kwargs["productive_minutes_for_segment"](item)
            for item in segments
        }
        return {}

    monkeypatch.setattr(production_segments, "credit_work_segments", capture)
    monkeypatch.setattr(production_segments, "score_work_segments", lambda *a, **k: {})

    assert production_history.production_scores_for_timeline(
        object(),
        DAY,
        (span(81, "Worker", 0, 60, "Repair 1", is_open=False),),
        now_utc=END,
        is_today=False,
        window_start_utc=START,
        window_end_utc=END,
        station_totals=(total,),
        attribution_rows=rows,
    ) == ()

    assert captured["wc_totals"] == {"Repair 1": 6.0}
    assert captured["samples_by_wc"] == {
        "Repair 1": [(START + timedelta(minutes=20), 6)]
    }
    assert captured["minutes"] == {81: 50.0}
    assert captured["allow_total_fallback"] is False


def test_timeline_breakdown_identity_does_not_cross_same_display_name(monkeypatch):
    from zira_dashboard import production_segments, settings_store, shift_config
    from zira_dashboard.leaderboard import StationTotal
    from zira_dashboard.stations import Station
    from tests.people_performance_fixtures import DAY, END, START, span

    total = StationTotal(
        station=Station("m1", "Repair 1", "Repair", "Bay 1"),
        units=10,
        reading_count=1,
        truncated=False,
        downtime_minutes=0,
        active_minutes=60,
        last_reading_at=START + timedelta(minutes=20),
        last_status="Working",
        samples=((START + timedelta(minutes=20), 10),),
        active_intervals=((START, START + timedelta(minutes=60)),),
    )
    breakdown = {
        "id": 2,
        "wc_name": "Repair 1",
        "person_name": "Same Name",
        "employee_odoo_id": 81,
        "start_utc": START + timedelta(minutes=30),
        "end_utc": START + timedelta(minutes=40),
        "source": "breakdown",
        "breakdown_id": 9,
    }
    captured = {}
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(settings_store, "station_target", lambda station: 10.0)

    def capture(segments, **kwargs):
        captured.update(
            {
                item.person_odoo_id: kwargs["productive_minutes_for_segment"](item)
                for item in segments
            }
        )
        return {}

    monkeypatch.setattr(production_segments, "credit_work_segments", capture)
    monkeypatch.setattr(production_segments, "score_work_segments", lambda *a, **k: {})

    production_history.production_scores_for_timeline(
        object(),
        DAY,
        (
            span(81, "Same Name", 0, 60, "Repair 1", is_open=False),
            span(82, "Same Name", 0, 60, "Repair 1", is_open=False),
        ),
        now_utc=END,
        is_today=False,
        window_start_utc=START,
        window_end_utc=END,
        station_totals=(total,),
        attribution_rows=(breakdown,),
    )

    assert captured == {81: 50.0, 82: 60.0}


def test_timeline_scoring_uses_explicit_is_today_for_live_cap(monkeypatch):
    from zira_dashboard import production_segments, settings_store
    from zira_dashboard.leaderboard import StationTotal
    from zira_dashboard.stations import Station
    from tests.people_performance_fixtures import DAY, END, START, span

    total = StationTotal(
        station=Station("m1", "Repair 1", "Repair", "Bay 1"),
        units=10,
        reading_count=1,
        truncated=False,
        downtime_minutes=0,
        active_minutes=60,
        last_reading_at=START + timedelta(minutes=20),
        last_status="Working",
        samples=((START + timedelta(minutes=20), 10),),
        active_intervals=((START, START + timedelta(minutes=60)),),
    )
    captured = {}
    monkeypatch.setattr(settings_store, "station_target", lambda station: 10.0)
    monkeypatch.setattr(
        production_segments,
        "credit_work_segments",
        lambda *args, **kwargs: captured.update(kwargs) or {},
    )
    monkeypatch.setattr(production_segments, "score_work_segments", lambda *a, **k: {})

    production_history.production_scores_for_timeline(
        object(),
        DAY,
        (span(81, "Worker", 0, 60, "Repair 1", is_open=False),),
        now_utc=END,
        is_today=False,
        window_start_utc=START,
        window_end_utc=END,
        station_totals=(total,),
        attribution_rows=(),
    )

    assert captured["live_cap_utc"] is None


def test_timeline_scoring_rejects_naive_cap_before_reading_sources():
    from tests.people_performance_fixtures import DAY, END

    with pytest.raises(TypeError, match="now_utc"):
        production_history.production_scores_for_timeline(
            object(),
            DAY,
            (),
            now_utc=END.replace(tzinfo=None),
            is_today=True,
            station_totals=(),
            attribution_rows=(),
        )


def test_solo_operator_gets_full_credit():
    out = attribute_for_day(
        assignments={"Repair 1": ["Christian"]},
        wc_totals={"Repair 1": (80, 12)},
        elapsed_minutes=480,
    )
    assert out == {
        "Christian": {
            "Repair 1": {
                "units": 80.0,
                "downtime": 12.0,
                "hours": 8.0,
                "days_worked": 1,
                "excluded_minutes": 0.0,
            }
        }
    }


def test_two_operators_split_evenly():
    out = attribute_for_day(
        assignments={"Trim Saw 1": ["Iban", "Porfirio"]},
        wc_totals={"Trim Saw 1": (200, 6)},
        elapsed_minutes=480,
    )
    assert out["Iban"]["Trim Saw 1"]["units"] == 100.0
    assert out["Iban"]["Trim Saw 1"]["downtime"] == 3.0
    assert out["Porfirio"]["Trim Saw 1"]["units"] == 100.0
    assert out["Porfirio"]["Trim Saw 1"]["downtime"] == 3.0
    assert out["Iban"]["Trim Saw 1"]["days_worked"] == 1
    assert out["Porfirio"]["Trim Saw 1"]["days_worked"] == 1


def test_three_operators_split_evenly():
    out = attribute_for_day(
        assignments={"Hand Build #1": ["A", "B", "C"]},
        wc_totals={"Hand Build #1": (90, 9)},
        elapsed_minutes=480,
    )
    for n in ("A", "B", "C"):
        assert out[n]["Hand Build #1"]["units"] == 30.0
        assert out[n]["Hand Build #1"]["downtime"] == 3.0


def test_attendance_segments_split_units_and_hours_when_operator_moves_work_centers():
    """A tablet transfer must stop both credit and time at the old station."""
    from datetime import datetime, timezone

    utc = timezone.utc
    start = datetime(2026, 8, 12, 12, 0, tzinfo=utc)
    moved = datetime(2026, 8, 12, 17, 20, tzinfo=utc)
    end = datetime(2026, 8, 12, 20, 0, tzinfo=utc)
    segments = [
        WorkSegment("Dismantler 3", "Jesus", start, end, "punch"),
        WorkSegment("Dismantler 3", "Christian", start, moved, "punch"),
        WorkSegment("Repair 2", "Christian", moved, end, "punch"),
    ]

    out = attribute_for_segments(
        segments,
        wc_totals={"Dismantler 3": (60, 0), "Repair 2": (40, 0)},
        samples_by_wc={
            "Dismantler 3": [
                (datetime(2026, 8, 12, 12, 10, tzinfo=utc), 10),
                (datetime(2026, 8, 12, 17, 10, tzinfo=utc), 20),
                (datetime(2026, 8, 12, 17, 30, tzinfo=utc), 30),
            ],
            "Repair 2": [(datetime(2026, 8, 12, 17, 30, tzinfo=utc), 40)],
        },
        productive_minutes=lambda _person, _wc, s, e: (e - s).total_seconds() / 60,
    )

    # The two Dismantler samples before Christian's Repair tablet sign-in are
    # shared; the later Dismantler sample belongs to Jesus alone.
    assert out["Jesus"]["Dismantler 3"]["units"] == 45.0
    assert out["Christian"]["Dismantler 3"]["units"] == 15.0
    assert out["Christian"]["Repair 2"]["units"] == 40.0
    assert out["Christian"]["Dismantler 3"]["hours"] == 5 + 20 / 60
    assert out["Christian"]["Repair 2"]["hours"] == 2 + 40 / 60


def test_attribute_for_segments_aggregates_returning_worker_without_merging_credit_windows():
    from datetime import datetime, timezone

    utc = timezone.utc
    t0 = datetime(2026, 8, 20, 12, tzinfo=utc)
    t1 = datetime(2026, 8, 20, 13, tzinfo=utc)
    t2 = datetime(2026, 8, 20, 14, tzinfo=utc)
    t3 = datetime(2026, 8, 20, 15, tzinfo=utc)
    out = attribute_for_segments(
        [
            WorkSegment("Repair 4", "Humberto S.", t0, t1, "punch"),
            WorkSegment("Repair 4", "Humberto S.", t2, t3, "punch"),
        ],
        wc_totals={"Repair 4": (30, 8)},
        samples_by_wc={"Repair 4": [(t0, 10), (t2, 20)]},
        productive_minutes=lambda _person, _wc, start, end: (
            end - start
        ).total_seconds()
        / 60,
    )
    assert out["Humberto S."]["Repair 4"] == {
        "units": 30.0,
        "downtime": 8.0,
        "hours": 2.0,
        "days_worked": 1,
        "excluded_minutes": 0.0,
    }


def test_attribution_for_uses_odoo_work_center_intervals_for_historical_credit(monkeypatch):
    """Stored records retain the Odoo transfer instead of the full-day schedule."""
    from datetime import datetime, time, timezone
    from zira_dashboard import (
        shift_config,
        staffing,
        timeclock_windows,
        wc_attributions,
    )

    utc = timezone.utc
    day = date(2026, 8, 1)
    start = datetime(2026, 8, 1, 12, 0, tzinfo=utc)
    moved = datetime(2026, 8, 1, 17, 20, tzinfo=utc)
    end = datetime(2026, 8, 1, 20, 0, tzinfo=utc)
    schedule = staffing.Schedule(
        day=day,
        published=True,
        assignments={"Dismantler 3": ["Jesus", "Christian"]},
    )
    monkeypatch.setattr(staffing, "load_schedule", lambda _d: schedule)
    monkeypatch.setattr(
        timeclock_windows,
        "attendance_windows_for_day_with_availability",
        lambda _d: ({
            "Jesus": [("Dismantler 3", start, end)],
            "Christian": [
                ("Dismantler 3", start, moved),
                ("Repair 2", moved, end),
            ],
        }, True),
    )
    monkeypatch.setattr(wc_attributions, "people_by_wc", lambda _d: {})
    monkeypatch.setattr(wc_attributions, "creditable_for_day", lambda _d: [])
    monkeypatch.setattr(wc_attributions, "testing_windows_for_day", lambda _d: {})
    monkeypatch.setattr(production_history, "_fetch_wc_totals", lambda _client, _d: {
        "Dismantler 3": (60, 0), "Repair 2": (40, 0),
    })
    monkeypatch.setattr(production_history, "_fetch_wc_samples", lambda _client, _d: {
        "Dismantler 3": [
            (datetime(2026, 8, 1, 12, 10, tzinfo=utc), 10),
            (datetime(2026, 8, 1, 17, 10, tzinfo=utc), 20),
            (datetime(2026, 8, 1, 17, 30, tzinfo=utc), 30),
        ],
        "Repair 2": [(datetime(2026, 8, 1, 17, 30, tzinfo=utc), 40)],
    })
    monkeypatch.setattr(production_history, "_excluded_minutes_by_person_wc", lambda *_: {})
    monkeypatch.setattr(shift_config, "shift_start_for", lambda _d: time(7, 0))
    monkeypatch.setattr(shift_config, "shift_end_for", lambda _d: time(15, 0))
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _d, s, e: (e - s).total_seconds() / 60,
    )

    out = production_history.attribution_for(day, client=object())

    assert out["Jesus"]["Dismantler 3"]["units"] == 45.0
    assert out["Christian"]["Dismantler 3"]["hours"] == 5 + 20 / 60
    assert out["Christian"]["Repair 2"]["units"] == 40.0


def test_attribution_for_uses_current_odoo_row_when_day_intervals_are_empty(monkeypatch):
    """A fresh open tablet row is enough to override an unpublished draft."""
    from datetime import datetime, time, timedelta, UTC
    from zira_dashboard import (
        shift_config,
        staffing,
        timeclock_windows,
        wc_attributions,
    )
    from zira_dashboard.plant_day import today as plant_today

    day = plant_today()
    start = datetime.combine(day, time.min, tzinfo=shift_config.SITE_TZ).astimezone(UTC)
    schedule = staffing.Schedule(
        day=day,
        published=False,
        assignments={"Dismantler 3": ["Christian"]},
    )
    monkeypatch.setattr(staffing, "load_schedule", lambda _d: schedule)
    monkeypatch.setattr(
        timeclock_windows,
        "attendance_windows_for_day_with_availability",
        lambda _d: ({}, True),
    )
    monkeypatch.setattr(
        timeclock_windows,
        "current_attendance_windows",
        lambda: ({"Christian": [("Repair 2", start, None)]}, start),
    )
    monkeypatch.setattr(wc_attributions, "creditable_for_day", lambda _d: [])
    monkeypatch.setattr(wc_attributions, "testing_windows_for_day", lambda _d: {})
    monkeypatch.setattr(production_history, "_fetch_wc_totals", lambda *_: {"Repair 2": (30, 0)})
    monkeypatch.setattr(
        production_history,
        "_fetch_wc_samples",
        lambda *_: {"Repair 2": [(start + timedelta(minutes=1), 30)]},
    )
    monkeypatch.setattr(production_history, "_excluded_minutes_by_person_wc", lambda *_: {})
    monkeypatch.setattr(shift_config, "shift_start_for", lambda _d: time.min)
    monkeypatch.setattr(shift_config, "shift_end_for", lambda _d: time(23, 59))
    monkeypatch.setattr(shift_config, "productive_minutes_in_window", lambda _d, s, e: (e - s).total_seconds() / 60)

    out = production_history.attribution_for(day, client=object())

    assert "Dismantler 3" not in out["Christian"]
    assert out["Christian"]["Repair 2"]["units"] == 30.0


def test_attribution_for_odoo_attendance_overrides_time_off_schedule(monkeypatch):
    """A real tablet sign-in wins over a stale time-off schedule entry."""
    from datetime import datetime, time, timezone
    from zira_dashboard import shift_config, staffing, timeclock_windows, wc_attributions

    utc = timezone.utc
    day = date(2026, 8, 1)
    start = datetime(2026, 8, 1, 12, tzinfo=utc)
    end = datetime(2026, 8, 1, 13, tzinfo=utc)
    schedule = staffing.Schedule(
        day=day,
        published=True,
        assignments={staffing.TIME_OFF_KEY: ["Christian"]},
    )
    monkeypatch.setattr(staffing, "load_schedule", lambda _d: schedule)
    monkeypatch.setattr(
        timeclock_windows,
        "attendance_windows_for_day_with_availability",
        lambda _d: ({"Christian": [("Repair 2", start, end)]}, True),
    )
    monkeypatch.setattr(wc_attributions, "creditable_for_day", lambda _d: [])
    monkeypatch.setattr(wc_attributions, "testing_windows_for_day", lambda _d: {})
    monkeypatch.setattr(wc_attributions, "people_by_wc", lambda _d: {})
    monkeypatch.setattr(production_history, "_fetch_wc_totals", lambda *_: {"Repair 2": (30, 0)})
    monkeypatch.setattr(production_history, "_fetch_wc_samples", lambda *_: {"Repair 2": [(start, 30)]})
    monkeypatch.setattr(production_history, "_elapsed_minutes_for", lambda _d: 480)
    monkeypatch.setattr(production_history, "_excluded_minutes_by_person_wc", lambda *_: {})
    monkeypatch.setattr(shift_config, "shift_start_for", lambda _d: time(7))
    monkeypatch.setattr(shift_config, "shift_end_for", lambda _d: time(15))
    monkeypatch.setattr(shift_config, "productive_minutes_in_window", lambda _d, s, e: (e - s).total_seconds() / 60)

    out = production_history.attribution_for(day, client=object())

    assert out["Christian"]["Repair 2"]["units"] == 30.0
    assert out["Christian"]["Repair 2"]["hours"] == 1.0


def test_attribution_for_does_not_fall_back_when_odoo_read_fails(monkeypatch):
    """A failed Odoo read must abort before accurate saved rows are replaced."""
    from zira_dashboard import staffing, timeclock_windows, wc_attributions

    day = date(2026, 8, 1)
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda _d: staffing.Schedule(
            day=day,
            published=True,
            assignments={"Dismantler 3": ["Christian"]},
        ),
    )
    monkeypatch.setattr(production_history, "_fetch_wc_totals", lambda *_: {"Dismantler 3": (30, 0)})
    monkeypatch.setattr(wc_attributions, "testing_windows_for_day", lambda _d: {})
    monkeypatch.setattr(wc_attributions, "people_by_wc", lambda _d: {})
    monkeypatch.setattr(
        timeclock_windows,
        "attendance_windows_for_day_with_availability",
        lambda _d: ({}, False),
    )
    monkeypatch.setattr(production_history, "_elapsed_minutes_for", lambda _d: 480)
    monkeypatch.setattr(production_history, "_excluded_minutes_by_person_wc", lambda *_: {})

    with pytest.raises(RuntimeError, match="Odoo attendance"):
        production_history.attribution_for(day, client=object())


from zira_dashboard.staffing import TIME_OFF_KEY


def test_time_off_excluded():
    out = attribute_for_day(
        assignments={
            "Repair 1": ["Christian"],
            TIME_OFF_KEY: ["Iban", "Lupe"],
        },
        wc_totals={"Repair 1": (80, 12)},
        elapsed_minutes=480,
    )
    assert "Christian" in out
    assert "Iban" not in out
    assert "Lupe" not in out


def test_unmetered_wc_credits_day_but_zero_units():
    # Hand Build has no meter_id, so no entry in wc_totals.
    out = attribute_for_day(
        assignments={"Hand Build #1": ["Lupe", "Carlos"]},
        wc_totals={},  # empty — no Zira data for this WC
        elapsed_minutes=480,
    )
    assert out["Lupe"]["Hand Build #1"]["units"] == 0.0
    assert out["Lupe"]["Hand Build #1"]["downtime"] == 0.0
    assert out["Lupe"]["Hand Build #1"]["days_worked"] == 1
    assert out["Carlos"]["Hand Build #1"]["days_worked"] == 1


from zira_dashboard.production_history import attribute_for_range


def test_range_sums_units_and_days():
    day1 = {
        "Christian": {"Repair 1": {"units": 80.0, "downtime": 12.0, "hours": 8.0, "days_worked": 1}},
    }
    day2 = {
        "Christian": {"Repair 1": {"units": 95.0, "downtime": 5.0,  "hours": 8.0, "days_worked": 1}},
    }
    day3 = {
        "Christian": {"Repair 4": {"units": 70.0, "downtime": 0.0, "hours": 8.0, "days_worked": 1}},
        "Adrian":    {"Repair 1": {"units": 75.0, "downtime": 8.0, "hours": 8.0, "days_worked": 1}},
    }
    out = attribute_for_range([day1, day2, day3])
    assert out["Christian"]["Repair 1"]["units"] == 175.0
    assert out["Christian"]["Repair 1"]["days_worked"] == 2
    assert out["Christian"]["Repair 4"]["days_worked"] == 1
    assert out["Adrian"]["Repair 1"]["days_worked"] == 1
    assert out["Adrian"]["Repair 1"]["units"] == 75.0


def test_attribution_for_today_drafts_return_empty(monkeypatch):
    """Today's drafts (published=False) don't count — supervisor may be
    mid-edit and partial assignments would skew live leaderboards."""
    from datetime import datetime, timezone
    from zira_dashboard import staffing, timeclock_windows
    from zira_dashboard.plant_day import today as plant_today
    from zira_dashboard.production_history import attribution_for

    today = plant_today(datetime.now(timezone.utc))
    fake_sched = staffing.Schedule(
        day=today,
        published=False,
        assignments={"Repair 1": ["Christian"]},
    )
    monkeypatch.setattr(staffing, "load_schedule", lambda d: fake_sched)
    monkeypatch.setattr(
        timeclock_windows,
        "attendance_windows_for_day_with_availability",
        lambda _d: ({}, True),
    )
    monkeypatch.setattr(
        timeclock_windows, "current_attendance_windows", lambda: ({}, None)
    )
    out = attribution_for(today, client=object())
    assert out == {}


def test_attribution_for_past_unpublished_day_uses_assignments(monkeypatch):
    """Past days use saved assignments even if never formally published —
    by the time a day is in the past, the saved draft is the closest
    record of what actually happened."""
    from zira_dashboard import staffing, timeclock_windows
    from zira_dashboard.production_history import attribution_for

    fake_sched = staffing.Schedule(
        day=date(2026, 4, 27),
        published=False,  # never clicked Publish — but units still ran
        assignments={"Repair 1": ["Christian"]},
    )
    monkeypatch.setattr(staffing, "load_schedule", lambda d: fake_sched)
    monkeypatch.setattr(
        timeclock_windows,
        "attendance_windows_for_day_with_availability",
        lambda _d: ({}, True),
    )
    monkeypatch.setattr(production_history, "_fetch_wc_totals",
                        lambda client, day: {"Repair 1": (95, 5)})
    monkeypatch.setattr(production_history, "_elapsed_minutes_for", lambda d: 480)

    out = attribution_for(date(2026, 4, 27), client=object())
    assert out["Christian"]["Repair 1"]["units"] == 95.0
    assert out["Christian"]["Repair 1"]["downtime"] == 5.0


def test_attribution_for_uses_published_assignments(monkeypatch):
    from zira_dashboard import staffing, timeclock_windows
    from zira_dashboard.production_history import attribution_for

    fake_sched = staffing.Schedule(
        day=date(2026, 4, 27),
        published=True,
        assignments={"Trim Saw 1": ["Iban", "Porfirio"]},
    )
    monkeypatch.setattr(staffing, "load_schedule", lambda d: fake_sched)
    monkeypatch.setattr(
        timeclock_windows,
        "attendance_windows_for_day_with_availability",
        lambda _d: ({}, True),
    )

    # Stub the per-day Zira lookup so we don't hit the real API.
    def fake_wc_totals(client, day):
        return {"Trim Saw 1": (200, 6)}
    monkeypatch.setattr(production_history, "_fetch_wc_totals", fake_wc_totals)
    monkeypatch.setattr(production_history, "_elapsed_minutes_for", lambda d: 480)

    out = attribution_for(date(2026, 4, 27), client=object())
    assert out["Iban"]["Trim Saw 1"]["units"] == 100.0
    assert out["Porfirio"]["Trim Saw 1"]["units"] == 100.0


from zira_dashboard.production_history import rank_by_category


def test_rank_by_category_filters_to_category_wcs_and_threshold():
    range_out = {
        "Christian": {"Repair 1": {"units": 480.0, "downtime": 30.0, "hours": 40.0, "days_worked": 5}},
        "Adrian":    {"Repair 1": {"units": 250.0, "downtime": 10.0, "hours": 16.0, "days_worked": 2}},  # below threshold
        "Eulogio":   {"Repair 4": {"units": 385.0, "downtime": 18.0, "hours": 40.0, "days_worked": 5}},
        "Iban":      {"Trim Saw 1": {"units": 600.0, "downtime": 12.0, "hours": 40.0, "days_worked": 5}},  # different category
    }
    expected_per_wc = {"Repair 1": 100, "Repair 4": 100}

    rows = rank_by_category(
        range_out,
        category_wcs=["Repair 1", "Repair 2", "Repair 3", "Repair 4", "Repair 5"],
        expected_units_per_day_by_wc=expected_per_wc,
        min_days=3,
    )
    names = [r["name"] for r in rows]
    assert names == ["Christian", "Eulogio"]
    assert "Adrian" not in names
    assert "Iban" not in names
    assert rows[0]["pct_of_target"] == 96.0


import os
from datetime import date as _date

@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="Postgres test needs DATABASE_URL",
)
def test_daily_records_reads_from_production_daily():
    """daily_records must return rows from production_daily without
    calling production_history.attribution_for at all."""
    from zira_dashboard import db, precompute

    db.init_pool(); db.bootstrap_schema()
    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s",
               (_date(2099, 7, 1), _date(2099, 7, 31)))
    precompute.upsert_production_daily([
        {"day": _date(2099, 7, 1), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 10.0, "downtime": 1.0, "hours": 4.0,
         "days_worked": 1.0},
        {"day": _date(2099, 7, 2), "emp_id": "E2", "name": "Bob",
         "wc_name": "WC2", "units": 20.0, "downtime": 2.0, "hours": 8.0,
         "days_worked": 1.0},
    ])

    def poison(*a, **k):
        raise AssertionError("attribution_for should not be called")

    saved = production_history.attribution_for
    production_history.attribution_for = poison
    try:
        out = production_history.daily_records(
            _date(2099, 7, 1), _date(2099, 7, 31)
        )
    finally:
        production_history.attribution_for = saved

    by_day = {(r["day"], r["person"]): r for r in out}
    assert by_day[(_date(2099, 7, 1), "Alice")]["units"] == 10.0
    assert by_day[(_date(2099, 7, 2), "Bob")]["units"] == 20.0

    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s",
               (_date(2099, 7, 1), _date(2099, 7, 31)))


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="Postgres test needs DATABASE_URL",
)
def test_attribution_range_reads_from_production_daily():
    from zira_dashboard import db, precompute

    db.init_pool(); db.bootstrap_schema()
    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s",
               (_date(2099, 8, 1), _date(2099, 8, 31)))
    precompute.upsert_production_daily([
        {"day": _date(2099, 8, 1), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 10.0, "downtime": 1.0, "hours": 4.0,
         "days_worked": 1.0},
        {"day": _date(2099, 8, 2), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 5.0,  "downtime": 0.5, "hours": 2.0,
         "days_worked": 1.0},
    ])

    def poison(*a, **k):
        raise AssertionError("attribution_for should not be called")
    saved = production_history.attribution_for
    production_history.attribution_for = poison
    try:
        out = production_history.attribution_range(
            _date(2099, 8, 1), _date(2099, 8, 31)
        )
    finally:
        production_history.attribution_for = saved

    assert out["Alice"]["WC1"]["units"] == 15.0
    assert out["Alice"]["WC1"]["hours"] == 6.0
    assert out["Alice"]["WC1"]["days_worked"] == 2.0

    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s",
               (_date(2099, 8, 1), _date(2099, 8, 31)))


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="Postgres test needs DATABASE_URL",
)
def test_attribution_per_day_reads_from_production_daily():
    from zira_dashboard import db, precompute

    db.init_pool(); db.bootstrap_schema()
    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s",
               (_date(2099, 9, 1), _date(2099, 9, 30)))
    precompute.upsert_production_daily([
        {"day": _date(2099, 9, 1), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 10.0, "downtime": 1.0, "hours": 4.0,
         "days_worked": 1.0},
        {"day": _date(2099, 9, 2), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 5.0,  "downtime": 0.0, "hours": 2.0,
         "days_worked": 1.0},
        {"day": _date(2099, 9, 1), "emp_id": "E2", "name": "Bob",
         "wc_name": "WC2", "units": 20.0, "downtime": 0.0, "hours": 8.0,
         "days_worked": 1.0},
    ])

    def poison(*a, **k):
        raise AssertionError("attribution_for should not be called")
    saved = production_history.attribution_for
    production_history.attribution_for = poison
    try:
        out = production_history.attribution_per_day(
            _date(2099, 9, 1), _date(2099, 9, 30)
        )
    finally:
        production_history.attribution_for = saved

    # Date-ascending order is part of the contract — callers rely on it.
    days_emitted = [d for d, _ in out]
    assert days_emitted == sorted(days_emitted)

    by_day = dict(out)
    assert by_day[_date(2099, 9, 1)]["Alice"]["WC1"]["units"] == 10.0
    assert by_day[_date(2099, 9, 1)]["Bob"]["WC2"]["units"] == 20.0
    assert by_day[_date(2099, 9, 2)]["Alice"]["WC1"]["units"] == 5.0
    # Every day in range present (even empty days), so callers can
    # distinguish "checked and empty" from "didn't check".
    assert len(out) == 30

    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s",
               (_date(2099, 9, 1), _date(2099, 9, 30)))


# --- Declared-absent (person, day) production is excluded everywhere ---
# A stray meter unit crediting someone who was manager-declared Absent must
# not count as a worked day (leaderboards) or a stat (player card). Covers
# all three production_daily read paths.

def _insert_absence(db, day, emp_id, name):
    db.execute(
        "INSERT INTO manual_absences (day, emp_id, name) VALUES (%s, %s, %s) "
        "ON CONFLICT DO NOTHING",
        (day, emp_id, name),
    )


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="Postgres test needs DATABASE_URL",
)
def test_daily_records_excludes_declared_absent_days():
    from zira_dashboard import db, precompute

    db.init_pool(); db.bootstrap_schema()
    lo, hi = _date(2099, 10, 1), _date(2099, 10, 31)
    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s", (lo, hi))
    db.execute("DELETE FROM manual_absences WHERE day BETWEEN %s AND %s", (lo, hi))
    precompute.upsert_production_daily([
        # phantom 1-unit day on a date Alice was declared absent
        {"day": _date(2099, 10, 1), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 1.0, "downtime": 0.0, "hours": 0.0,
         "days_worked": 1.0},
        # a normal worked day that must still show
        {"day": _date(2099, 10, 2), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 800.0, "downtime": 1.0, "hours": 8.0,
         "days_worked": 1.0},
    ])
    _insert_absence(db, _date(2099, 10, 1), "E1", "Alice")

    out = production_history.daily_records(lo, hi)
    days = {(r["day"], r["person"]) for r in out}
    assert (_date(2099, 10, 1), "Alice") not in days  # phantom absent day dropped
    assert (_date(2099, 10, 2), "Alice") in days       # real day kept

    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s", (lo, hi))
    db.execute("DELETE FROM manual_absences WHERE day BETWEEN %s AND %s", (lo, hi))


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="Postgres test needs DATABASE_URL",
)
def test_attribution_range_excludes_declared_absent_days():
    from zira_dashboard import db, precompute

    db.init_pool(); db.bootstrap_schema()
    lo, hi = _date(2099, 11, 1), _date(2099, 11, 30)
    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s", (lo, hi))
    db.execute("DELETE FROM manual_absences WHERE day BETWEEN %s AND %s", (lo, hi))
    precompute.upsert_production_daily([
        {"day": _date(2099, 11, 1), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 1.0, "downtime": 0.0, "hours": 0.0,
         "days_worked": 1.0},
        {"day": _date(2099, 11, 2), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 800.0, "downtime": 1.0, "hours": 8.0,
         "days_worked": 1.0},
    ])
    _insert_absence(db, _date(2099, 11, 1), "E1", "Alice")

    out = production_history.attribution_range(lo, hi)
    # Only the non-absent day contributes — no phantom day inflating the totals.
    assert out["Alice"]["WC1"]["units"] == 800.0
    assert out["Alice"]["WC1"]["days_worked"] == 1.0

    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s", (lo, hi))
    db.execute("DELETE FROM manual_absences WHERE day BETWEEN %s AND %s", (lo, hi))


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="Postgres test needs DATABASE_URL",
)
def test_attribution_per_day_excludes_declared_absent_days():
    from zira_dashboard import db, precompute

    db.init_pool(); db.bootstrap_schema()
    lo, hi = _date(2099, 12, 1), _date(2099, 12, 31)
    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s", (lo, hi))
    db.execute("DELETE FROM manual_absences WHERE day BETWEEN %s AND %s", (lo, hi))
    precompute.upsert_production_daily([
        {"day": _date(2099, 12, 1), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 1.0, "downtime": 0.0, "hours": 0.0,
         "days_worked": 1.0},
        {"day": _date(2099, 12, 2), "emp_id": "E1", "name": "Alice",
         "wc_name": "WC1", "units": 800.0, "downtime": 1.0, "hours": 8.0,
         "days_worked": 1.0},
    ])
    _insert_absence(db, _date(2099, 12, 1), "E1", "Alice")

    by_day = dict(production_history.attribution_per_day(lo, hi))
    assert "Alice" not in by_day[_date(2099, 12, 1)]   # absent day empty
    assert by_day[_date(2099, 12, 2)]["Alice"]["WC1"]["units"] == 800.0

    db.execute("DELETE FROM production_daily WHERE day BETWEEN %s AND %s", (lo, hi))
    db.execute("DELETE FROM manual_absences WHERE day BETWEEN %s AND %s", (lo, hi))
