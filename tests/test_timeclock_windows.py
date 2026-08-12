from datetime import datetime, timezone
from zira_dashboard.timeclock_windows import _segments_from_rows

UTC = timezone.utc
def t(h, m=0): return datetime(2026, 6, 2, h, m, tzinfo=UTC)


def test_clock_in_then_out_one_window():
    rows = [
        {"action": "clock_in", "wc_name": "Dismantler 4", "at": t(13)},
        {"action": "clock_out", "wc_name": None, "at": t(17)},
    ]
    assert _segments_from_rows(rows) == [("Dismantler 4", t(13), t(17))]


def test_transfer_splits_into_two_windows():
    rows = [
        {"action": "clock_in", "wc_name": "Dismantler 4", "at": t(13)},
        {"action": "transfer_out", "wc_name": "Dismantler 4", "at": t(15)},
        {"action": "transfer_in", "wc_name": "Repair 1", "at": t(15)},
        {"action": "clock_out", "wc_name": None, "at": t(17)},
    ]
    assert _segments_from_rows(rows) == [
        ("Dismantler 4", t(13), t(15)),
        ("Repair 1", t(15), t(17)),
    ]


def test_current_odoo_attendance_overrides_an_earlier_window_for_live_labels():
    """The latest Odoo work center wins over an earlier mistaken sign-in."""
    from zira_dashboard import assignment_windows, timeclock_windows

    odoo_windows = {
        "Jesus Ma.": [("Dismantler 3", t(12), None)],
        "Christian C.": [("Dismantler 3", t(12), None)],
    }
    current_odoo_windows = {
        "Christian C.": [("Repair 2", t(12, 20), None)],
    }

    windows = timeclock_windows.with_current_attendance_overrides(
        odoo_windows, current_odoo_windows
    )
    segments = assignment_windows.resolve_segments(
        assignments={}, attributions=[], punch_windows=windows,
        shift_start_utc=t(12), cap_utc=t(13), time_off_key="__time_off",
    )

    assert assignment_windows.dashboard_who_by_wc(
        segments, cap_utc=t(13), is_live=True
    ) == {
        "Dismantler 3": "Jesus Ma.",
        "Repair 2": "Christian C.",
    }


def test_current_attendance_windows_uses_the_live_odoo_work_center(monkeypatch):
    from zira_dashboard import attendance, live_cache, timeclock_windows

    refreshed_at = t(12, 21)
    monkeypatch.setattr(
        live_cache,
        "read_open_attendance",
        lambda: ({
            "101": {
                "att_id": 88,
                "check_in": t(12, 20).isoformat(),
                "wc_name": "Repair 2",
            },
            "102": {
                "att_id": 89,
                "check_in": t(12, 15).isoformat(),
                "wc_name": None,
            },
        }, refreshed_at),
    )
    monkeypatch.setattr(live_cache, "is_stale", lambda _refreshed_at: False)
    monkeypatch.setattr(attendance, "person_id_to_name", lambda: {"101": "Christian C."})

    windows, version = timeclock_windows.current_attendance_windows()

    assert windows == {"Christian C.": [("Repair 2", t(12, 20), None)]}
    assert version == refreshed_at


def test_still_clocked_in_trailing_window_is_open():
    rows = [{"action": "clock_in", "wc_name": "Dismantler 4", "at": t(13)}]
    assert _segments_from_rows(rows) == [("Dismantler 4", t(13), None)]


def test_window_without_wc_dropped():
    rows = [
        {"action": "clock_in", "wc_name": None, "at": t(13)},
        {"action": "clock_out", "wc_name": None, "at": t(17)},
    ]
    assert _segments_from_rows(rows) == []


from datetime import date


def test_punch_windows_omits_people_with_no_resolved_window(monkeypatch):
    from zira_dashboard import timeclock_windows, db, attendance
    # Two people: 101 has a real clock_in at a WC; 102 has only a WC-less punch.
    rows = [
        {"person_odoo_id": 101, "action": "clock_in", "wc_name": "Dismantler 4", "at": t(13)},
        {"person_odoo_id": 102, "action": "clock_in", "wc_name": None, "at": t(13)},
        {"person_odoo_id": 102, "action": "clock_out", "wc_name": None, "at": t(17)},
    ]
    monkeypatch.setattr(db, "query", lambda *a, **k: rows)
    monkeypatch.setattr(attendance, "name_to_person_id",
                        lambda: {"Eulogio": "101", "Ghost": "102"})
    out = timeclock_windows.punch_windows_for_day(date(2026, 6, 2))
    assert out == {"Eulogio": [("Dismantler 4", t(13), None)]}
    assert "Ghost" not in out  # no real window -> omitted, so fallback isn't suppressed


# ---- attendance_windows_for_day (Odoo hr.attendance source -- the goal's truth) ----

from zira_dashboard.timeclock_windows import _windows_from_intervals


def test_windows_morning_and_afternoon_same_wc():
    """Auto-lunch splits the day into two records at the same WC -> two windows,
    so the goal spans the whole day instead of truncating at the morning."""
    intervals = [
        {"wc_name": "Dismantler 1", "start": t(12), "end": t(16)},      # morning
        {"wc_name": "Dismantler 1", "start": t(17), "end": t(20, 30)},  # post-lunch
    ]
    assert _windows_from_intervals(intervals) == [
        ("Dismantler 1", t(12), t(16)),
        ("Dismantler 1", t(17), t(20, 30)),
    ]


def test_windows_untagged_record_inherits_prior_wc():
    intervals = [
        {"wc_name": "Dismantler 1", "start": t(12), "end": t(16)},
        {"wc_name": None, "start": t(17), "end": t(20, 30)},  # no WC -> inherit D1
    ]
    assert _windows_from_intervals(intervals) == [
        ("Dismantler 1", t(12), t(16)),
        ("Dismantler 1", t(17), t(20, 30)),
    ]


def test_windows_transfer_changes_wc():
    intervals = [
        {"wc_name": "Dismantler 1", "start": t(12), "end": t(15)},
        {"wc_name": "Repair 1", "start": t(15), "end": t(20)},
    ]
    assert _windows_from_intervals(intervals) == [
        ("Dismantler 1", t(12), t(15)),
        ("Repair 1", t(15), t(20)),
    ]


def test_windows_leading_untagged_skipped_and_open_end_preserved():
    intervals = [
        {"wc_name": None, "start": t(12), "end": t(13)},           # no prior WC -> skip
        {"wc_name": "Dismantler 1", "start": t(13), "end": None},  # still open
    ]
    assert _windows_from_intervals(intervals) == [("Dismantler 1", t(13), None)]


def test_windows_sorted_by_start():
    intervals = [
        {"wc_name": "Repair 1", "start": t(15), "end": t(20)},
        {"wc_name": "Dismantler 1", "start": t(12), "end": t(15)},
    ]
    assert _windows_from_intervals(intervals) == [
        ("Dismantler 1", t(12), t(15)),
        ("Repair 1", t(15), t(20)),
    ]


def test_attendance_windows_maps_names_and_drops_no_wc(monkeypatch):
    from zira_dashboard import timeclock_windows, odoo_client, attendance
    timeclock_windows._past_cache.clear()  # 2026-06-02 is a past day -> cacheable
    timeclock_windows._today_cache.clear()
    intervals = [
        {"employee_odoo_id": 101, "check_in": t(12).isoformat(),
         "check_out": t(16).isoformat(), "wc_name": "Dismantler 1"},
        {"employee_odoo_id": 101, "check_in": t(17).isoformat(),
         "check_out": t(20, 30).isoformat(), "wc_name": "Dismantler 1"},
        {"employee_odoo_id": 102, "check_in": t(12).isoformat(),
         "check_out": t(16).isoformat(), "wc_name": None},  # never tagged -> dropped
    ]
    monkeypatch.setattr(odoo_client, "fetch_attendance_intervals_for_day", lambda day: intervals)
    monkeypatch.setattr(attendance, "name_to_person_id",
                        lambda: {"Jose Cabezas": "101", "Ghost": "102"})
    out = timeclock_windows.attendance_windows_for_day(date(2026, 6, 2))
    assert out == {"Jose Cabezas": [("Dismantler 1", t(12), t(16)),
                                    ("Dismantler 1", t(17), t(20, 30))]}
    assert "Ghost" not in out


def test_attendance_windows_keep_reverse_mapped_app_work_center(monkeypatch):
    """Odoo's ``Repair #1`` display name must not leak past Task 3's boundary."""
    from zira_dashboard import timeclock_windows, odoo_client, attendance

    timeclock_windows._past_cache.clear()
    timeclock_windows._today_cache.clear()
    monkeypatch.setattr(
        odoo_client,
        "fetch_attendance_intervals_for_day",
        lambda _day: [{
            "employee_odoo_id": 101,
            "check_in": t(12).isoformat(),
            "check_out": t(16).isoformat(),
            # The Odoo client already reverse-mapped [41, "Repair #1"].
            "wc_name": "Repair 1",
        }],
    )
    monkeypatch.setattr(attendance, "person_id_to_name", lambda: {"101": "Jose Cabezas"})

    assert timeclock_windows.attendance_windows_for_day(date(2026, 6, 2)) == {
        "Jose Cabezas": [("Repair 1", t(12), t(16))]
    }


def test_attendance_windows_past_day_fetched_once(monkeypatch):
    """Past days are immutable -> the Odoo pull is cached after the first call."""
    from zira_dashboard import timeclock_windows, odoo_client, attendance
    timeclock_windows._past_cache.clear()
    timeclock_windows._today_cache.clear()
    calls = {"n": 0}

    def _fetch(day):
        calls["n"] += 1
        return []

    monkeypatch.setattr(odoo_client, "fetch_attendance_intervals_for_day", _fetch)
    monkeypatch.setattr(attendance, "name_to_person_id", lambda: {})
    d = date(2026, 6, 3)
    assert timeclock_windows.attendance_windows_for_day(d) == {}
    assert timeclock_windows.attendance_windows_for_day(d) == {}
    assert calls["n"] == 1


def test_attendance_windows_reports_unavailable_when_odoo_read_fails(monkeypatch):
    from zira_dashboard import timeclock_windows, odoo_client, attendance
    timeclock_windows._past_cache.clear()
    timeclock_windows._today_cache.clear()
    monkeypatch.setattr(
        odoo_client,
        "fetch_attendance_intervals_for_day",
        lambda day: (_ for _ in ()).throw(RuntimeError("Odoo unavailable")),
    )
    monkeypatch.setattr(attendance, "person_id_to_name", lambda: {})

    windows, available = timeclock_windows.attendance_windows_for_day_with_availability(
        date(2026, 6, 3)
    )

    assert windows == {}
    assert available is False
