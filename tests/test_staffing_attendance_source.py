from datetime import UTC, datetime, date, time, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")  # routes import FastAPI; skip locally where it's absent

from zira_dashboard import (
    attendance,
    attendance_state,
    live_cache,
    plant_day,
    shift_config,
    staffing,
    staffing_attendance,
)


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


def test_recent_local_clock_in_overrides_older_attendance_cache(monkeypatch):
    """A correction must not be re-flagged while the Odoo cache catches up."""
    day = date(2026, 6, 1)
    cached_at = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)
    correction_at = datetime.combine(day, time(6, 10), tzinfo=shift_config.SITE_TZ)
    monkeypatch.setattr(live_cache, "read_attendance", lambda _day: ({}, cached_at))
    monkeypatch.setattr(live_cache, "is_stale", lambda _refreshed_at: False)
    monkeypatch.setattr(
        attendance_state,
        "latest_punches_bulk",
        lambda _ids: {
            1: {
                "action": "clock_in",
                "wc_name": "Baler",
                "occurred_at": correction_at,
                "synced_to_odoo": True,
                "synced_at": cached_at + timedelta(seconds=1),
            }
        },
    )

    punches = staffing_attendance._attendance_with_fallback(day, ["1"])

    assert punches.get("1") == {
        "first_check_in": correction_at.isoformat(),
        "currently_open": True,
    }


def test_stale_owned_snapshot_keeps_positive_status_without_missing_no_punch(
    monkeypatch,
):
    d = date(2026, 6, 1)
    now_local = datetime.combine(d, time(12, 0), tzinfo=shift_config.SITE_TZ)
    source = live_cache.AttendanceSourceSnapshot(
        payload={
            "1": {
                "first_check_in": datetime(2026, 6, 1, 11, 0, tzinfo=UTC).isoformat(),
                "currently_open": True,
            }
        },
        refreshed_at=datetime(2026, 6, 1, 16, 0, tzinfo=UTC),
        mirror_owned=True,
        available=True,
        error="incremental sync failed",
        stale=True,
    )
    monkeypatch.setattr(plant_day, "now", lambda: now_local)
    monkeypatch.setattr(shift_config, "shift_start_for", lambda _day: time(6, 0))
    monkeypatch.setattr(
        attendance, "name_to_person_id", lambda: {"Ana": "1", "Bob": "2"}
    )
    monkeypatch.setattr(
        staffing_attendance, "_timeoff_names_with_fallback", lambda _day: set()
    )
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [
            SimpleNamespace(name="Ana", active=True, reserve=False),
            SimpleNamespace(name="Bob", active=True, reserve=False),
        ],
    )

    pkg = staffing_attendance._safe_attendance(
        d,
        SimpleNamespace(assignments={"Baler": ["Ana", "Bob"]}),
        d,
        attendance_source=source,
    )

    assert pkg["by_id"]["1"]["status"] == "on_time"
    assert "2" not in pkg["by_id"]
    assert "Bob" not in pkg["by_name"]


def test_stale_owned_snapshot_is_frozen_for_direct_staffing_reader(monkeypatch):
    d = date(2026, 6, 1)
    now_local = datetime.combine(d, time(12, 0), tzinfo=shift_config.SITE_TZ)
    policy = live_cache.AttendanceReadPolicy(
        mirror_owned=True,
        available=True,
        refreshed_at=datetime(2026, 6, 1, 16, 0, tzinfo=UTC),
        error="incremental sync failed",
        mode="live",
        stale=True,
    )
    source = live_cache.AttendanceSourceSnapshot(
        payload={
            "1": {
                "first_check_in": datetime(2026, 6, 1, 11, 0, tzinfo=UTC).isoformat(),
                "currently_open": True,
            }
        },
        refreshed_at=policy.refreshed_at,
        mirror_owned=True,
        available=True,
        error=policy.error,
        stale=True,
    )
    policy_calls = []
    source_calls = []
    monkeypatch.setattr(plant_day, "now", lambda: now_local)
    monkeypatch.setattr(shift_config, "shift_start_for", lambda _day: time(6, 0))
    monkeypatch.setattr(
        attendance, "name_to_person_id", lambda: {"Ana": "1", "Bob": "2"}
    )
    monkeypatch.setattr(
        staffing_attendance, "_timeoff_names_with_fallback", lambda _day: set()
    )
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [
            SimpleNamespace(name="Ana", active=True, reserve=False),
            SimpleNamespace(name="Bob", active=True, reserve=False),
        ],
    )
    monkeypatch.setattr(
        live_cache,
        "attendance_read_policy",
        lambda: policy_calls.append(True) or policy,
    )
    monkeypatch.setattr(
        live_cache,
        "read_attendance_source",
        lambda day, **kwargs: source_calls.append((day, kwargs["policy"])) or source,
    )

    pkg = staffing_attendance._safe_attendance(
        d, SimpleNamespace(assignments={"Baler": ["Ana", "Bob"]}), d
    )

    assert policy_calls == [True]
    assert source_calls == [(d, policy)]
    assert pkg["by_id"]["1"]["status"] == "on_time"
    assert "2" not in pkg["by_id"]


def test_frozen_staffing_snapshot_does_not_apply_a_later_local_clock_in(monkeypatch):
    day = date(2026, 6, 1)
    cap = datetime(2026, 6, 1, 18, 0, tzinfo=UTC)
    later_local = cap + timedelta(seconds=1)
    source = SimpleNamespace(
        payload={},
        refreshed_at=cap,
        mirror_owned=True,
        available=True,
        frozen=True,
    )
    monkeypatch.setattr(
        attendance_state,
        "latest_punches_bulk",
        lambda _ids: {
            1: {
                "action": "clock_in",
                "wc_name": None,
                "occurred_at": later_local,
                "synced_to_odoo": False,
                "synced_at": None,
            }
        },
    )

    punches = staffing_attendance._attendance_with_fallback(
        day, ["1"], source=source
    )

    assert punches == {}
