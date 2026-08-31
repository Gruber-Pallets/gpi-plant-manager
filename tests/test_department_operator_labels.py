from datetime import date, datetime, time, timezone
from types import SimpleNamespace

import pytest


def test_who_by_wc_excludes_absent_people_from_schedule_and_attributions(monkeypatch):
    from zira_dashboard.routes import departments
    from zira_dashboard import wc_attributions

    day = date(2026, 6, 29)
    monkeypatch.setattr(
        wc_attributions,
        "people_by_wc",
        lambda d: {"Repair 1": ["Bob", "Cara"], "Repair 3": ["Bob"]},
    )

    out = departments._who_by_wc(
        {
            "Repair 1": ["Ana", "Bob"],
            "Repair 2": ["Bob"],
        },
        day,
        absent_names={"Bob"},
    )

    assert out == {"Repair 1": "Ana + Cara"}


def test_department_day_data_shows_transfer_at_current_wc_but_keeps_both_active(monkeypatch):
    """A live transfer changes the label, without losing either station's data."""
    from zira_dashboard import (
        attendance,
        attendance_timeline,
        live_cache,
        machine_breakdown,
        settings_store,
        shift_config,
        staffing,
        timeclock_windows,
        wc_attributions,
    )
    from zira_dashboard.routes import departments, staffing as staffing_routes
    from zira_dashboard.stations import Station

    day = date(2026, 6, 2)
    verified_cap = datetime(2026, 6, 2, 18, 59, tzinfo=timezone.utc)
    repair = Station("repair-2", "Repair 2", "Repair", "Recycling")
    dismantler = Station("dismantler-2", "Dismantler 2", "Dismantler", "Recycling")
    rows = [
        SimpleNamespace(
            station=repair,
            units=34,
            downtime_minutes=0,
            active_intervals=(),
            last_reading_at=None,
            samples=((datetime(2026, 6, 2, 12, 2, tzinfo=timezone.utc), 34),),
        ),
        SimpleNamespace(
            station=dismantler,
            units=391,
            downtime_minutes=0,
            active_intervals=(),
            last_reading_at=None,
            samples=(
                (datetime(2026, 6, 2, 12, 10, tzinfo=timezone.utc), 384),
                (datetime(2026, 6, 2, 18, 59, 30, tzinfo=timezone.utc), 7),
            ),
        ),
    ]

    monkeypatch.setattr(departments, "leaderboard", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda d: staffing.Schedule(day=d, published=True, assignments={}),
    )
    monkeypatch.setattr(departments, "_absent_names", lambda _d: set())
    monkeypatch.setattr(departments, "shift_elapsed_minutes", lambda *_args: 420)
    monkeypatch.setattr(shift_config, "shift_start_for", lambda _d: time(7))
    monkeypatch.setattr(shift_config, "shift_end_for", lambda _d: time(15))
    monkeypatch.setattr(shift_config, "breaks_for", lambda _d: ())
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _d, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(settings_store, "station_target", lambda _station: 60)
    monkeypatch.setattr(departments, "progress_buckets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        attendance,
        "partial_off_intervals",
        lambda _d: {
            "Jesus G.": [
                (
                    datetime(2026, 6, 2, 13, tzinfo=timezone.utc),
                    datetime(2026, 6, 2, 13, 30, tzinfo=timezone.utc),
                )
            ]
        },
    )

    def canonical_spans(_start, end, **_kwargs):
        first_start = datetime(2026, 6, 2, 12, tzinfo=timezone.utc)
        transfer = datetime(2026, 6, 2, 12, 5, tzinfo=timezone.utc)
        common = {
            "employee_odoo_id": 101,
            "employee_name": "Jesus G.",
            "status": "valid",
            "odoo_work_center_id": 41,
            "odoo_work_center_name": "Repair #2",
            "attendance_ids": (88,),
            "department_repair": None,
        }
        return (
            attendance_timeline.LocationSpan(
                **common,
                start_utc=first_start,
                end_utc=transfer,
                app_work_center_name="Repair 2",
            ),
            attendance_timeline.LocationSpan(
                **{
                    **common,
                    "odoo_work_center_id": 42,
                    "odoo_work_center_name": "Dismantler #2",
                },
                start_utc=transfer,
                end_utc=end,
                app_work_center_name="Dismantler 2",
            ),
        )

    monkeypatch.setattr(attendance_timeline, "timeline_for_range", canonical_spans)
    monkeypatch.setattr(
        staffing_routes,
        "_read_staffing_response_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(
            policy=live_cache.AttendanceReadPolicy(
                True,
                True,
                verified_cap,
                mode="shadow",
            ),
            spans=canonical_spans(None, verified_cap),
            verified_cap_utc=verified_cap,
        ),
    )
    monkeypatch.setattr(
        timeclock_windows,
        "attendance_windows_for_day",
        lambda _d: pytest.fail(
            "mirror-owned departments must keep canonical Odoo identity"
        ),
    )
    monkeypatch.setattr(wc_attributions, "creditable_for_day", lambda _d: [])
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_windows_for_day",
        lambda _d: {
            (101, "Jesus G.", "Repair 2"): [
                (
                    datetime(2026, 6, 2, 12, tzinfo=timezone.utc),
                    datetime(2026, 6, 2, 12, 2, tzinfo=timezone.utc),
                )
            ]
        },
    )
    monkeypatch.setattr(
        machine_breakdown,
        "excluded_minutes_overlapping",
        lambda windows, *_args, **_kwargs: 2.0 if windows else 0.0,
    )

    live = departments._department_day_data(
        day,
        datetime(2026, 6, 2, 19, tzinfo=timezone.utc),
        True,
        stations=[repair, dismantler],
        labor_department="Recycled",
        group_categories=("Repair", "Dismantler"),
    )
    assert live["per_wc_who"] == {
        "Repair 2": None,
        "Dismantler 2": "Jesus G.",
    }
    assert live["active_wc_names"] == {"Repair 2", "Dismantler 2"}
    assert live["total_recycling_people"] == 1
    assert live["total_man_hours"] == pytest.approx(389 / 60)

    repair_score = live["per_wc_segments"]["Repair 2"][0]
    assert repair_score["person_name"] == "Jesus G."
    assert repair_score["actual_units"] == 34.0
    assert repair_score["goal_units"] == 3.0
    assert repair_score["is_active"] is False
    assert repair_score["time_label"] == "7-7:05a"

    dismantler_score = live["per_wc_segments"]["Dismantler 2"][0]
    assert len(live["per_wc_segments"]["Dismantler 2"]) == 1
    assert dismantler_score["person_name"] == "Jesus G."
    assert dismantler_score["actual_units"] == 384.0
    assert dismantler_score["goal_units"] == 414.0
    assert dismantler_score["is_active"] is True
    assert dismantler_score["time_label"] == "since 7:05a"
    assert live["is_live_dashboard"] is True
    assert live["per_wc_expected"] == {
        "Repair 2": 3.0,
        "Dismantler 2": 414.0,
    }
    assert live["per_wc_segment_display"] == {
        "Repair 2": True,
        "Dismantler 2": True,
    }
    assert live["per_wc_producers"] == {
        "Repair 2": ("Jesus G.",),
        "Dismantler 2": ("Jesus G.",),
    }

    after_shift = departments._department_day_data(
        day,
        datetime(2026, 6, 2, 21, tzinfo=timezone.utc),
        True,
        stations=[repair, dismantler],
        labor_department="Recycled",
        group_categories=("Repair", "Dismantler"),
    )
    assert after_shift["per_wc_who"] == {
        "Repair 2": "Jesus G.",
        "Dismantler 2": "Jesus G.",
    }

    from zira_dashboard import production_segments

    def fail_credit(*_args, **_kwargs):
        raise RuntimeError("sample source unavailable")

    monkeypatch.setattr(production_segments, "credit_work_segments", fail_credit)
    fallback = departments._department_day_data(
        day,
        datetime(2026, 6, 2, 19, tzinfo=timezone.utc),
        True,
        stations=[repair, dismantler],
        labor_department="Recycled",
        group_categories=("Repair", "Dismantler"),
    )
    assert fallback["per_wc_segments"] == {}
    assert fallback["per_wc_units"] == {"Repair 2": 34, "Dismantler 2": 391}


def test_department_breakdown_lookup_keeps_same_name_odoo_ids_separate():
    from zira_dashboard import assignment_windows
    from zira_dashboard.routes import departments

    start = datetime(2026, 6, 2, 12, tzinfo=timezone.utc)
    end = start.replace(hour=13)
    alex_101 = assignment_windows.WorkSegment(
        "Repair 2", "Alex", start, end, "odoo", person_odoo_id=101
    )
    alex_202 = assignment_windows.WorkSegment(
        "Repair 2", "Alex", start, end, "odoo", person_odoo_id=202
    )
    legacy_alex = assignment_windows.WorkSegment(
        "Repair 2", "Alex", start, end, "schedule"
    )
    windows_101 = [(start, start.replace(minute=30))]
    windows_202 = [(start.replace(minute=30), end)]
    breakdown_windows = {
        (101, "Alex", "Repair 2"): windows_101,
        (202, "Alex", "Repair 2"): windows_202,
        ("Alex", "Repair 2"): [(start, end)],
    }

    assert departments._breakdown_windows_for_segment(  # noqa: SLF001
        breakdown_windows, alex_101
    ) == windows_101
    assert departments._breakdown_windows_for_segment(  # noqa: SLF001
        breakdown_windows, alex_202
    ) == windows_202
    assert departments._breakdown_windows_for_segment(  # noqa: SLF001
        breakdown_windows, legacy_alex
    ) == [(start, end)]


def test_department_breakdown_lookup_uses_odoo_id_across_display_name_change():
    from zira_dashboard import assignment_windows
    from zira_dashboard.routes import departments

    start = datetime(2026, 6, 2, 12, tzinfo=timezone.utc)
    end = start.replace(hour=13)
    segment = assignment_windows.WorkSegment(
        "Repair 2", "Alexandra", start, end, "odoo", person_odoo_id=101
    )
    windows = [(start, start.replace(minute=30))]

    assert departments._breakdown_windows_for_segment(  # noqa: SLF001
        {(101, "Alex", "Repair 2"): windows}, segment
    ) == windows


def test_department_canonical_segment_selection_honors_strict_day_and_staleness(
    monkeypatch,
):
    from zira_dashboard import (
        attendance_location_policy,
        attendance_timeline,
        live_cache,
    )
    from zira_dashboard.routes import departments, staffing as staffing_routes

    day = date(2026, 6, 2)
    start = datetime(2026, 6, 2, 12, tzinfo=timezone.utc)
    end = datetime(2026, 6, 2, 13, tzinfo=timezone.utc)
    span = attendance_timeline.LocationSpan(
        employee_odoo_id=202,
        employee_name="Alex",
        start_utc=start,
        end_utc=end,
        status="valid",
        app_work_center_name="Repair 2",
        odoo_work_center_id=41,
        odoo_work_center_name="Repair #2",
        attendance_ids=(88,),
        department_repair=None,
    )
    policy = {"value": live_cache.AttendanceReadPolicy(False, True, None)}
    monkeypatch.setattr(
        staffing_routes,
        "_read_staffing_response_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(
            policy=policy["value"], spans=(span,), verified_cap_utc=end
        ),
    )
    monkeypatch.setattr(
        attendance_location_policy, "day_is_strict", lambda _day: True
    )
    timeline_calls = []
    monkeypatch.setattr(
        attendance_timeline,
        "timeline_for_range",
        lambda *_args, **_kwargs: timeline_calls.append(True) or (span,),
    )

    strict_projection = departments._canonical_department_segments(  # noqa: SLF001
        day, start, end, now_utc=end
    )

    assert [
        (segment.person_odoo_id, segment.person_name)
        for segment in strict_projection.segments
    ] == [(202, "Alex")]
    assert strict_projection.cap_utc == end
    assert timeline_calls == [True]

    policy["value"] = live_cache.AttendanceReadPolicy(
        True,
        True,
        end,
        mode="shadow",
        stale=True,
    )
    monkeypatch.setattr(
        attendance_location_policy, "day_is_strict", lambda _day: False
    )
    timeline_calls.clear()

    stale_projection = departments._canonical_department_segments(  # noqa: SLF001
        day, start, end, now_utc=end
    )
    assert stale_projection.segments == ()
    assert stale_projection.cap_utc == end
    assert timeline_calls == []


def test_department_segment_display_keeps_scheduled_lunch_continuous():
    from zira_dashboard.production_segments import SegmentScore
    from zira_dashboard.routes import departments

    def score(segment_id, start, end, actual, goal, *, active=False):
        return SegmentScore(
            segment_id=segment_id,
            wc_name="Dismantler 1",
            person_name="Jesus G.",
            start_utc=start,
            end_utc=end,
            source="punch",
            productive_minutes=goal,
            actual_units=actual,
            goal_units=goal,
            runway_units=max(actual, goal),
            is_active=active,
            result="ahead" if actual >= goal else "behind",
        )

    morning = score(
        0,
        datetime(2026, 6, 2, 12, tzinfo=timezone.utc),
        datetime(2026, 6, 2, 16, tzinfo=timezone.utc),
        311,
        260,
    )
    afternoon = score(
        1,
        datetime(2026, 6, 2, 16, 30, tzinfo=timezone.utc),
        datetime(2026, 6, 2, 19, 30, tzinfo=timezone.utc),
        256,
        260,
        active=True,
    )

    views, decisions, producers, live_workers = departments._prepare_segment_display(
        {"Dismantler 1": (morning, afternoon)},
        break_windows=((
            datetime(2026, 6, 2, 16, tzinfo=timezone.utc),
            datetime(2026, 6, 2, 16, 30, tzinfo=timezone.utc),
        ),),
        window_start_utc=datetime(2026, 6, 2, 12, tzinfo=timezone.utc),
        window_end_utc=datetime(2026, 6, 2, 19, 30, tzinfo=timezone.utc),
        is_live=True,
    )

    assert len(views["Dismantler 1"]) == 1
    assert views["Dismantler 1"][0]["time_label"] == "since 7a"
    assert views["Dismantler 1"][0]["goal_units"] == 520
    assert decisions == {"Dismantler 1": False}
    assert producers == {"Dismantler 1": ("Jesus G.",)}
    assert live_workers == {"Dismantler 1": "Jesus G."}


def test_department_day_data_uses_latest_open_odoo_work_center(monkeypatch):
    """A current tablet sign-in corrects an earlier mistaken station today."""
    from zira_dashboard import (
        attendance,
        machine_breakdown,
        settings_store,
        shift_config,
        staffing,
        timeclock_windows,
        wc_attributions,
    )
    from zira_dashboard.routes import departments
    from zira_dashboard.stations import Station

    day = date(2026, 6, 2)
    repair = Station("repair-2", "Repair 2", "Repair", "Recycling")
    dismantler = Station("dismantler-3", "Dismantler 3", "Dismantler", "Recycling")
    rows = [
        SimpleNamespace(
            station=repair, units=34, downtime_minutes=0, active_intervals=(), last_reading_at=None
        ),
        SimpleNamespace(
            station=dismantler, units=384, downtime_minutes=0, active_intervals=(), last_reading_at=None
        ),
    ]

    monkeypatch.setattr(departments, "leaderboard", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda d: staffing.Schedule(day=d, published=True, assignments={}),
    )
    monkeypatch.setattr(departments, "_absent_names", lambda _d: set())
    monkeypatch.setattr(departments, "shift_elapsed_minutes", lambda *_args: 420)
    monkeypatch.setattr(shift_config, "shift_start_for", lambda _d: time(7))
    monkeypatch.setattr(shift_config, "shift_end_for", lambda _d: time(15))
    monkeypatch.setattr(shift_config, "breaks_for", lambda _d: ())
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _d, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(settings_store, "station_target", lambda _station: 60)
    monkeypatch.setattr(departments, "progress_buckets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(attendance, "partial_off_intervals", lambda _d: {})
    monkeypatch.setattr(
        timeclock_windows,
        "attendance_windows_for_day",
        lambda _d: {
            "Jesus Ma.": [("Dismantler 3", datetime(2026, 6, 2, 12, tzinfo=timezone.utc), None)],
            "Christian C.": [("Dismantler 3", datetime(2026, 6, 2, 12, tzinfo=timezone.utc), None)],
        },
    )
    monkeypatch.setattr(
        timeclock_windows,
        "current_attendance_windows",
        lambda: (
            {"Christian C.": [("Repair 2", datetime(2026, 6, 2, 12, 20, tzinfo=timezone.utc), None)]},
            datetime(2026, 6, 2, 12, 21, tzinfo=timezone.utc),
        ),
    )
    monkeypatch.setattr(wc_attributions, "creditable_for_day", lambda _d: [])
    monkeypatch.setattr(wc_attributions, "breakdown_windows_for_day", lambda _d: {})
    monkeypatch.setattr(
        machine_breakdown, "excluded_minutes_overlapping", lambda *_args, **_kwargs: 0.0
    )

    live = departments._department_day_data(
        day,
        datetime(2026, 6, 2, 19, tzinfo=timezone.utc),
        True,
        stations=[repair, dismantler],
        labor_department="Recycled",
        group_categories=("Repair", "Dismantler"),
    )

    assert live["per_wc_who"] == {
        "Repair 2": "Christian C.",
        "Dismantler 3": "Jesus Ma.",
    }
    assert live["per_wc_expected"] == {"Repair 2": 400.0, "Dismantler 3": 440.0}
