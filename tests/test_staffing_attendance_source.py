from datetime import datetime, date, time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")  # routes import FastAPI; skip locally where it's absent

from zira_dashboard import attendance, staffing, shift_config, staffing_attendance, plant_day


def test_safe_attendance_keys_by_odoo_id(monkeypatch):
    """_safe_attendance maps roster names -> Odoo ids via the people table,
    splits scheduled vs unscheduled, and returns a status dict keyed by
    str(person_odoo_id)."""
    # Freeze the plant clock to noon on a fixed day so the "past shift start"
    # guard is deterministic. _safe_attendance reads plant_day.now(), which
    # diverges from the UTC date in the evening (Central tz) and made this
    # test flaky when "now" was computed from real wall-clock.
    d = date(2026, 6, 1)
    monkeypatch.setattr(plant_day, "now", lambda: datetime.combine(d, time(12, 0), tzinfo=shift_config.SITE_TZ))
    monkeypatch.setattr(attendance, "name_to_person_id", lambda: {"Ana": "1", "Bob": "2"})
    monkeypatch.setattr(staffing_attendance, "_timeoff_names_with_fallback", lambda day: set())
    monkeypatch.setattr(staffing, "load_roster", lambda: [
        SimpleNamespace(name="Ana", active=True, reserve=False),
        SimpleNamespace(name="Bob", active=True, reserve=False),
    ])
    # No punches -> everyone no_punch (robust regardless of clock).
    monkeypatch.setattr(staffing_attendance, "_attendance_with_fallback", lambda day, ids: {})
    # Force "past shift start" so _safe_attendance doesn't early-return empty.
    monkeypatch.setattr(shift_config, "shift_start_for", lambda day: time(0, 0))

    sched = SimpleNamespace(assignments={"Baler": ["Ana"]})  # Ana scheduled, Bob not
    pkg = staffing_attendance._safe_attendance(d, sched, d)

    assert pkg["name_to_id"] == {"Ana": "1", "Bob": "2"}
    assert pkg["scheduled_ids"] == ["1"]
    assert pkg["unscheduled_ids"] == ["2"]
    assert pkg["by_id"]["1"]["status"] == "no_punch"
    assert pkg["by_name"]["Ana"]["status"] == "no_punch"
