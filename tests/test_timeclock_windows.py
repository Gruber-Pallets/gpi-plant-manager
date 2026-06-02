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
