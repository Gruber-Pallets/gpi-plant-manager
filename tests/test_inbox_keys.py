"""Canonical Exception Inbox item keys."""
from zira_dashboard import inbox_keys


def test_canonical_keys():
    assert inbox_keys.time_off(55) == "time_off:55"
    assert inbox_keys.missing_wc(48213) == "missing_wc:48213"
    assert inbox_keys.missed_punch_out(48213) == "missed_punch_out:48213"
    assert inbox_keys.late("42", "2026-06-26") == "late:42:2026-06-26"
    assert inbox_keys.assignment("Saw 1", "2026-06-26T13:00:00") == "assignment:Saw 1:2026-06-26T13:00:00"
    assert inbox_keys.plant_schedule("2026-06-29") == "plant_schedule:2026-06-29"
    assert inbox_keys.odoo_roster_sync() == "odoo_roster_sync:active_status"
