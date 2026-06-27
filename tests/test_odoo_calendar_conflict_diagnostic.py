"""Unit tests for the Odoo work-schedule conflict classifier.

The classifier is pure (no Odoo/DB), so these run locally without creds.
See docs/superpowers/specs/2026-06-27-odoo-calendar-conflict-diagnostic-design.md
"""

from scripts.diagnose_odoo_calendar_conflicts import classify_conflict

# Plant runs Mon-Fri (0=Mon .. 6=Sun, Python weekday()).
MON_FRI = frozenset({0, 1, 2, 3, 4})


def test_covers_every_plant_weekday_is_ok():
    assert classify_conflict(MON_FRI, {0, 1, 2, 3, 4}, is_flexible=False, has_calendar=True) == "ok"


def test_extra_weekend_coverage_still_ok():
    assert classify_conflict(MON_FRI, {0, 1, 2, 3, 4, 5}, is_flexible=False, has_calendar=True) == "ok"


def test_missing_friday_is_missing_days():
    assert classify_conflict(MON_FRI, {0, 1, 2, 3}, is_flexible=False, has_calendar=True) == "missing_days"


def test_flexible_flag_is_flexible():
    # Even if the covered weekdays look complete, a flexible schedule has no
    # fixed hours Odoo can place a leave against.
    assert classify_conflict(MON_FRI, {0, 1, 2, 3, 4}, is_flexible=True, has_calendar=True) == "flexible"


def test_calendar_with_no_covered_weekdays_is_flexible():
    assert classify_conflict(MON_FRI, set(), is_flexible=False, has_calendar=True) == "flexible"


def test_no_calendar_is_no_calendar():
    assert classify_conflict(MON_FRI, set(), is_flexible=False, has_calendar=False) == "no_calendar"


def test_no_calendar_takes_precedence_over_flexible():
    # If there's no calendar at all we report that, not the flex bucket.
    assert classify_conflict(MON_FRI, set(), is_flexible=True, has_calendar=False) == "no_calendar"
