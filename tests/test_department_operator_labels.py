from datetime import date, datetime, time, timezone
from types import SimpleNamespace


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
    dismantler = Station("dismantler-2", "Dismantler 2", "Dismantler", "Recycling")
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
            "Jesus G.": [
                ("Repair 2", datetime(2026, 6, 2, 12, tzinfo=timezone.utc),
                 datetime(2026, 6, 2, 12, 5, tzinfo=timezone.utc)),
                ("Dismantler 2", datetime(2026, 6, 2, 12, 5, tzinfo=timezone.utc), None),
            ]
        },
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
        "Repair 2": None,
        "Dismantler 2": "Jesus G.",
    }
    assert live["active_wc_names"] == {"Repair 2", "Dismantler 2"}
    assert live["total_recycling_people"] == 1
    assert live["total_man_hours"] == 7.0

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
