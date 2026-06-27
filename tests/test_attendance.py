from datetime import datetime, date, time, timezone, timedelta
from zira_dashboard import attendance
from zira_dashboard.shift_config import SITE_TZ as TZ


def _shift_start():
    return datetime.combine(date(2026, 6, 1), time(8, 0), tzinfo=TZ)


def _utc_iso(local_dt):
    return local_dt.astimezone(timezone.utc).isoformat()


def test_compute_status_classifies_punches():
    shift_start = _shift_start()
    now = shift_start + timedelta(hours=1)
    punches = {
        "10": {"first_check_in": _utc_iso(shift_start + timedelta(minutes=3)), "currently_open": True},   # on_time (<=+7)
        "11": {"first_check_in": _utc_iso(shift_start + timedelta(minutes=25)), "currently_open": True},  # late
        "12": {"first_check_in": _utc_iso(shift_start - timedelta(minutes=5)), "currently_open": False},  # clocked_out
    }
    out = attendance.compute_status(punches, ["10", "11", "12", "13"], now, shift_start, grace_minutes=7)
    assert out["10"]["status"] == "on_time"
    assert out["11"]["status"] == "late" and out["11"]["minutes_late"] == 25
    assert out["12"]["status"] == "clocked_out"
    assert out["13"]["status"] == "no_punch"


def test_punches_for_day_keys_by_str_id(monkeypatch):
    from zira_dashboard import odoo_client
    monkeypatch.setattr(odoo_client, "fetch_attendances_for_day", lambda d: [
        {"employee_odoo_id": 7, "first_check_in": "2026-06-01T12:02:00+00:00", "currently_open": True},
    ])
    out = attendance.punches_for_day(date(2026, 6, 1))
    assert out == {"7": {"first_check_in": "2026-06-01T12:02:00+00:00", "currently_open": True}}


def test_name_to_person_id_maps_active_people(monkeypatch):
    from zira_dashboard import db
    monkeypatch.setattr(db, "query", lambda *a, **k: [
        {"name": "Jose Luis", "odoo_id": 42},
        {"name": "Maria", "odoo_id": 7},
    ])
    assert attendance.name_to_person_id() == {"Jose Luis": "42", "Maria": "7"}


def test_full_day_absent_union(monkeypatch):
    from zira_dashboard import scheduler_time_off, late_report
    monkeypatch.setattr(scheduler_time_off, "full_day_off_names", lambda d: {"Ana"})
    monkeypatch.setattr(late_report, "absent_names_for_day", lambda d: {"Bob"})
    monkeypatch.setattr(attendance, "derived_absent_names", lambda d: {"Carl"})
    assert attendance.full_day_absent_names(date(2026, 6, 1)) == {"Ana", "Bob", "Carl"}


def test_derived_absent_flags_unpunched_after_buffer(monkeypatch):
    from types import SimpleNamespace
    from zira_dashboard import staffing, scheduler_time_off, shift_config, live_cache, plant_day
    # Freeze the plant clock to noon on a fixed day so the wall-clock-dependent
    # guards (today-only + "past shift_start + buffer") are deterministic no
    # matter when the suite runs. derived_absent_names reads plant_day, so we
    # pin that rather than real now() (which diverges from the Central date in
    # the evening and made this test flaky).
    day = date(2026, 6, 1)
    frozen_now = datetime.combine(day, time(12, 0), tzinfo=TZ)
    monkeypatch.setattr(plant_day, "today", lambda *a, **k: day)
    monkeypatch.setattr(plant_day, "now", lambda: frozen_now)
    monkeypatch.setattr(staffing, "load_roster", lambda: [
        SimpleNamespace(name="Ana", active=True, reserve=False),   # no punch -> absent
        SimpleNamespace(name="Bob", active=True, reserve=False),   # punched -> present
        SimpleNamespace(name="Cy", active=True, reserve=True),     # reserve -> ignored
    ])
    monkeypatch.setattr(scheduler_time_off, "time_off_entries_for_day", lambda d: [])
    monkeypatch.setattr(attendance, "name_to_person_id", lambda: {"Ana": "1", "Bob": "2", "Cy": "3"})
    # Cold cache -> falls back to the direct Odoo pull (cache-first behavior).
    monkeypatch.setattr(live_cache, "read_attendance", lambda d: (None, None))
    monkeypatch.setattr(attendance, "punches_for_day", lambda d: {"2": {"first_check_in": "x", "currently_open": True}})
    # Force "well past shift start" by stubbing shift_start_for to midnight.
    monkeypatch.setattr(shift_config, "shift_start_for", lambda d: time(0, 0))
    assert attendance.derived_absent_names(day) == {"Ana"}


def test_derived_absent_prefers_cached_punches(monkeypatch):
    """Cache-first: when live_cache has the warmer's payload, derived_absent_names
    must NOT make a direct Odoo pull."""
    from types import SimpleNamespace
    from zira_dashboard import staffing, scheduler_time_off, shift_config, live_cache, plant_day
    # Freeze the plant clock (see sibling test) so the today/buffer guards are
    # deterministic regardless of wall-clock time.
    day = date(2026, 6, 1)
    frozen_now = datetime.combine(day, time(12, 0), tzinfo=TZ)
    monkeypatch.setattr(plant_day, "today", lambda *a, **k: day)
    monkeypatch.setattr(plant_day, "now", lambda: frozen_now)
    monkeypatch.setattr(staffing, "load_roster", lambda: [
        SimpleNamespace(name="Ana", active=True, reserve=False),
        SimpleNamespace(name="Bob", active=True, reserve=False),
    ])
    monkeypatch.setattr(scheduler_time_off, "time_off_entries_for_day", lambda d: [])
    monkeypatch.setattr(attendance, "name_to_person_id", lambda: {"Ana": "1", "Bob": "2"})
    monkeypatch.setattr(live_cache, "read_attendance", lambda d: (
        {"2": {"first_check_in": "x", "currently_open": True}}, frozen_now))

    def _boom(d):
        raise AssertionError("punches_for_day must not be called on a cache hit")
    monkeypatch.setattr(attendance, "punches_for_day", _boom)
    monkeypatch.setattr(shift_config, "shift_start_for", lambda d: time(0, 0))
    assert attendance.derived_absent_names(day) == {"Ana"}


def test_partial_off_intervals_builds_utc_spans(monkeypatch):
    from zira_dashboard import scheduler_time_off
    # late_arrival: off from shift_start (6.0) until arrival (8.5) -> hour_from=6, hour_to=8.5
    monkeypatch.setattr(scheduler_time_off, "_rows_for_day", lambda d: [
        {"name": "Ana", "shape": "late_arrival", "hour_from": 6.0, "hour_to": 8.5,
         "state": "validate", "pay_type": "Custom Hours"},
        {"name": "Bob", "shape": "full_day", "hour_from": None, "hour_to": None,
         "state": "validate", "pay_type": "PTO"},  # full-day excluded
    ])
    out = attendance.partial_off_intervals(date(2026, 6, 1))
    assert "Bob" not in out
    assert len(out["Ana"]) == 1
    s, e = out["Ana"][0]
    assert s.tzinfo is not None and e > s
