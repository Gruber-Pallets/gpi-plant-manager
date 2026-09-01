"""Canonical mirror read ownership for shadow/live attendance consumers."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from zira_dashboard import (
    app,
    attendance,
    attendance_mirror,
    attendance_state,
    auto_lunch,
    auto_lunch_settings,
    live_cache,
    plant_day,
    scheduler_time_off,
    shift_config,
    staffing,
    staffing_attendance,
    timeclock_windows,
    work_centers_store,
)


DAY = date(2026, 8, 31)
FRESH_AT = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
FIRST_IN = datetime(2026, 8, 31, 11, 0, tzinfo=UTC)


def _config(mode: str):
    return SimpleNamespace(mode=mode)


def _health(
    *,
    complete: bool = True,
    refreshed_at: datetime | None = FRESH_AT,
    error: str | None = None,
):
    return attendance_mirror.MirrorHealth(
        last_incremental_completed_at=refreshed_at,
        last_full_sweep_completed_at=FRESH_AT,
        baseline_completed_at=FRESH_AT if complete else None,
        oldest_recalc_requested_at=None,
        last_error=error,
    )


def test_off_mode_keeps_legacy_day_and_open_cache_reads(monkeypatch):
    monkeypatch.setattr(live_cache.attendance_location_policy, "get_rollout_config", lambda: _config("off"))
    mirror_health = MagicMock(side_effect=AssertionError("off must not inspect mirror"))
    monkeypatch.setattr(live_cache.attendance_mirror, "health_snapshot", mirror_health)
    monkeypatch.setattr(
        live_cache,
        "_read",
        lambda table, day: ({"legacy": table}, FRESH_AT),
    )
    monkeypatch.setattr(
        live_cache,
        "_read_open_attendance_legacy",
        lambda: ({"legacy": {"att_id": 7}}, FRESH_AT),
    )

    day_source = live_cache.read_attendance_source(DAY)
    open_source = live_cache.read_open_attendance_source()

    assert day_source.mirror_owned is False
    assert day_source.payload == {"legacy": "today_attendance_cache"}
    assert open_source.mirror_owned is False
    assert open_source.payload == {"legacy": {"att_id": 7}}
    mirror_health.assert_not_called()


@pytest.mark.parametrize("mode", ["shadow", "live"])
def test_complete_baseline_makes_shadow_and_live_read_only_the_mirror(monkeypatch, mode):
    monkeypatch.setattr(live_cache.attendance_location_policy, "get_rollout_config", lambda: _config(mode))
    monkeypatch.setattr(live_cache.attendance_mirror, "health_snapshot", lambda: _health())
    monkeypatch.setattr(
        live_cache.attendance_mirror,
        "day_presence",
        lambda day: {"5": {"first_check_in": FIRST_IN.isoformat(), "currently_open": True}},
    )
    monkeypatch.setattr(
        live_cache.attendance_mirror,
        "current_open_attendance",
        lambda: (
            {
                "odoo_attendance_id": 90,
                "employee_odoo_id": 5,
                "check_in_utc": FIRST_IN,
                "odoo_work_center_id": 8,
                "odoo_work_center_name": "Luke Bay 8",
                "odoo_department_id": 4,
                "odoo_department_name": "Supervisor",
            },
        ),
    )
    monkeypatch.setattr(
        live_cache.work_centers_store,
        "app_work_center_name_for_odoo_id",
        lambda wc_id: "Bay 8" if wc_id == 8 else None,
    )
    monkeypatch.setattr(
        live_cache,
        "_read",
        lambda *_args: (_ for _ in ()).throw(AssertionError("legacy day cache consulted")),
    )
    monkeypatch.setattr(
        live_cache,
        "_read_open_attendance_legacy",
        lambda: (_ for _ in ()).throw(AssertionError("legacy open cache consulted")),
    )

    day_source = live_cache.read_attendance_source(DAY)
    open_source = live_cache.read_open_attendance_source()

    assert day_source.mirror_owned is True and day_source.available is True
    assert day_source.refreshed_at == FRESH_AT
    assert day_source.payload["5"]["currently_open"] is True
    assert open_source.mirror_owned is True and open_source.available is True
    assert open_source.refreshed_at == FRESH_AT
    assert open_source.payload == {
        "5": {
            "att_id": 90,
            "check_in": FIRST_IN.isoformat(),
            "wc_name": "Bay 8",
            "raw_odoo_wc_name": "Luke Bay 8",
            "odoo_department_id": 4,
            "odoo_department_name": "Supervisor",
        }
    }


@pytest.mark.parametrize("mode", ["shadow", "live"])
def test_incomplete_baseline_keeps_legacy_cache_path(monkeypatch, mode):
    monkeypatch.setattr(live_cache.attendance_location_policy, "get_rollout_config", lambda: _config(mode))
    monkeypatch.setattr(live_cache.attendance_mirror, "health_snapshot", lambda: _health(complete=False))
    monkeypatch.setattr(
        live_cache,
        "_read",
        lambda table, day: ({"legacy": table}, FRESH_AT),
    )

    source = live_cache.read_attendance_source(DAY)

    assert source.mirror_owned is False
    assert source.available is True
    assert source.payload == {"legacy": "today_attendance_cache"}


def test_owned_mirror_read_failure_is_unavailable_without_legacy_fallback(monkeypatch):
    monkeypatch.setattr(live_cache.attendance_location_policy, "get_rollout_config", lambda: _config("shadow"))
    monkeypatch.setattr(live_cache.attendance_mirror, "health_snapshot", lambda: _health())
    monkeypatch.setattr(
        live_cache.attendance_mirror,
        "day_presence",
        lambda _day: (_ for _ in ()).throw(RuntimeError("mirror read failed")),
    )
    monkeypatch.setattr(
        live_cache,
        "_read",
        lambda *_args: (_ for _ in ()).throw(AssertionError("legacy fallback forbidden")),
    )

    source = live_cache.read_attendance_source(DAY)

    assert source.mirror_owned is True
    assert source.available is False
    assert source.payload is None
    assert source.refreshed_at == FRESH_AT


def test_owned_unavailable_source_never_fabricates_no_punch_status(monkeypatch):
    source = SimpleNamespace(
        payload=None,
        refreshed_at=FRESH_AT,
        mirror_owned=True,
        available=False,
    )
    monkeypatch.setattr(live_cache, "read_attendance_source", lambda _day: source)
    monkeypatch.setattr(
        attendance,
        "punches_for_day",
        lambda _day: (_ for _ in ()).throw(AssertionError("Odoo fallback forbidden")),
    )

    assert attendance.status_for_day(DAY, ["5"], FIRST_IN, FIRST_IN) == {}
    assert staffing_attendance._attendance_with_fallback(DAY, ["5"]) is None


def test_mirror_health_failure_in_shadow_stays_unavailable(monkeypatch):
    monkeypatch.setattr(live_cache.attendance_location_policy, "get_rollout_config", lambda: _config("shadow"))
    monkeypatch.setattr(
        live_cache.attendance_mirror,
        "health_snapshot",
        lambda: (_ for _ in ()).throw(RuntimeError("health unavailable")),
    )
    monkeypatch.setattr(
        live_cache,
        "_read",
        lambda *_args: (_ for _ in ()).throw(AssertionError("legacy fallback forbidden")),
    )

    source = live_cache.read_attendance_source(DAY)

    assert source.mirror_owned is True
    assert source.available is False
    assert source.payload is None


def test_mirror_policy_distinguishes_stale_age_and_health_error(monkeypatch):
    now = FRESH_AT + live_cache.STALE_THRESHOLD + timedelta(seconds=1)
    monkeypatch.setattr(
        live_cache.attendance_location_policy,
        "get_rollout_config",
        lambda: _config("shadow"),
    )
    monkeypatch.setattr(
        live_cache.attendance_mirror,
        "health_snapshot",
        lambda: _health(error="incremental sync failed"),
    )

    policy = live_cache.attendance_read_policy(now_utc=now)

    assert policy.mirror_owned is True
    assert policy.available is True
    assert policy.stale is True
    assert policy.error == "incremental sync failed"


def test_health_read_failure_is_unavailable_not_merely_stale(monkeypatch):
    monkeypatch.setattr(
        live_cache.attendance_location_policy,
        "get_rollout_config",
        lambda: _config("shadow"),
    )
    monkeypatch.setattr(
        live_cache.attendance_mirror,
        "health_snapshot",
        lambda: (_ for _ in ()).throw(RuntimeError("health unavailable")),
    )

    policy = live_cache.attendance_read_policy(now_utc=FRESH_AT)

    assert policy.mirror_owned is True
    assert policy.available is False
    assert policy.stale is False
    assert policy.error == "health unavailable"


def test_migrated_day_readers_use_mirror_source_and_never_fetch_odoo(monkeypatch):
    source = SimpleNamespace(
        payload={"5": {"first_check_in": FIRST_IN.isoformat(), "currently_open": True}},
        refreshed_at=FRESH_AT,
        mirror_owned=True,
        available=True,
    )
    monkeypatch.setattr(live_cache, "read_attendance_source", lambda _day: source)
    monkeypatch.setattr(
        attendance,
        "punches_for_day",
        lambda _day: (_ for _ in ()).throw(AssertionError("on-request Odoo fetch forbidden")),
    )
    monkeypatch.setattr(
        live_cache,
        "read_attendance",
        lambda _day: (_ for _ in ()).throw(AssertionError("legacy cache forbidden")),
    )
    monkeypatch.setattr(attendance_state, "latest_punches_bulk", lambda _ids: {})

    status = attendance.status_for_day(DAY, ["5"], FIRST_IN, FIRST_IN)
    staffing_punches = staffing_attendance._attendance_with_fallback(DAY, ["5"])

    assert status["5"]["currently_open"] is True
    assert staffing_punches == source.payload


def test_migrated_open_readers_share_mirror_freshness_without_refresh(monkeypatch):
    source = SimpleNamespace(
        payload={
            "5": {
                "att_id": 90,
                "check_in": FIRST_IN.isoformat(),
                "wc_name": "Bay 8",
                "raw_odoo_wc_name": "Luke Bay 8",
            }
        },
        refreshed_at=FRESH_AT,
        mirror_owned=True,
        available=True,
    )
    monkeypatch.setattr(live_cache, "read_open_attendance_source", lambda: source)
    monkeypatch.setattr(
        live_cache,
        "read_open_attendance",
        lambda: (_ for _ in ()).throw(AssertionError("legacy cache forbidden")),
    )
    monkeypatch.setattr(
        live_cache,
        "refresh_odoo_open_attendance",
        lambda: (_ for _ in ()).throw(AssertionError("on-request Odoo refresh forbidden")),
    )
    monkeypatch.setattr(attendance_state, "latest_punch", lambda _person_id: None)
    monkeypatch.setattr(attendance, "person_id_to_name", lambda: {"5": "Maria"})
    monkeypatch.setattr(live_cache, "is_stale", lambda _at: False)

    state = attendance_state.current_state(5)
    windows, refreshed_at = timeclock_windows.current_attendance_windows()

    assert state["current_wc"] == "Bay 8"
    assert windows == {"Maria": [("Bay 8", FIRST_IN, FRESH_AT)]}
    assert refreshed_at == FRESH_AT


def test_mirror_owned_current_windows_cap_at_frozen_freshness_downstream(monkeypatch):
    from zira_dashboard import assignment_windows

    source = SimpleNamespace(
        payload={
            "5": {
                "att_id": 90,
                "check_in": FIRST_IN.isoformat(),
                "wc_name": "Bay 8",
                "raw_odoo_wc_name": "Luke Bay 8",
            }
        },
        refreshed_at=FRESH_AT,
        mirror_owned=True,
        available=True,
        stale=True,
        error="sync stalled",
    )
    monkeypatch.setattr(live_cache, "read_open_attendance_source", lambda: source)
    monkeypatch.setattr(attendance, "person_id_to_name", lambda: {"5": "Maria"})

    windows, refreshed_at = timeclock_windows.current_attendance_windows()
    segments = assignment_windows.resolve_segments(
        assignments={},
        attributions=[],
        punch_windows=windows,
        shift_start_utc=FIRST_IN,
        cap_utc=FRESH_AT + timedelta(hours=1),
    )

    assert refreshed_at == FRESH_AT
    assert windows == {"Maria": [("Bay 8", FIRST_IN, FRESH_AT)]}
    assert len(segments) == 1
    assert segments[0].end_utc == FRESH_AT


def test_legacy_current_windows_preserve_open_end(monkeypatch):
    source = SimpleNamespace(
        payload={
            "5": {
                "att_id": 90,
                "check_in": FIRST_IN.isoformat(),
                "wc_name": "Bay 8",
            }
        },
        refreshed_at=FRESH_AT,
        mirror_owned=False,
        available=True,
        stale=False,
        error=None,
    )
    monkeypatch.setattr(live_cache, "read_open_attendance_source", lambda: source)
    monkeypatch.setattr(live_cache, "is_stale", lambda _at: False)
    monkeypatch.setattr(attendance, "person_id_to_name", lambda: {"5": "Maria"})

    windows, _ = timeclock_windows.current_attendance_windows()

    assert windows == {"Maria": [("Bay 8", FIRST_IN, None)]}


def test_legacy_refresh_adopts_mirror_ownership_and_verified_cap(monkeypatch):
    """Activation between legacy refresh reads must not open-end mirror rows."""
    legacy_source = live_cache.AttendanceSourceSnapshot(
        payload=None,
        refreshed_at=None,
        mirror_owned=False,
        available=True,
        stale=True,
    )
    mirror_source = live_cache.AttendanceSourceSnapshot(
        payload={
            "5": {
                "att_id": 90,
                "check_in": FIRST_IN.isoformat(),
                "wc_name": "Bay 8",
            }
        },
        refreshed_at=FRESH_AT,
        mirror_owned=True,
        available=True,
        stale=False,
    )
    sources = iter((legacy_source, mirror_source))
    source_calls = []
    refresh_calls = []
    monkeypatch.setattr(
        live_cache,
        "read_open_attendance_source",
        lambda: source_calls.append(True) or next(sources),
    )
    monkeypatch.setattr(
        live_cache,
        "refresh_odoo_open_attendance",
        lambda: refresh_calls.append(True),
    )
    monkeypatch.setattr(live_cache, "is_stale", lambda at: at is None)
    monkeypatch.setattr(attendance, "person_id_to_name", lambda: {"5": "Maria"})

    windows, refreshed_at = timeclock_windows.current_attendance_windows()

    assert source_calls == [True, True]
    assert refresh_calls == [True]
    assert refreshed_at == FRESH_AT
    assert windows == {"Maria": [("Bay 8", FIRST_IN, FRESH_AT)]}


def test_mirror_owned_current_windows_never_open_end_without_verified_cap(
    monkeypatch,
):
    source = live_cache.AttendanceSourceSnapshot(
        payload={
            "5": {
                "att_id": 90,
                "check_in": FIRST_IN.isoformat(),
                "wc_name": "Bay 8",
            }
        },
        refreshed_at=None,
        mirror_owned=True,
        available=True,
    )
    monkeypatch.setattr(live_cache, "read_open_attendance_source", lambda: source)
    monkeypatch.setattr(attendance, "person_id_to_name", lambda: {"5": "Maria"})

    windows, refreshed_at = timeclock_windows.current_attendance_windows()

    assert windows == {}
    assert refreshed_at is None


def test_owned_unavailable_open_source_is_not_replaced_by_synced_local_log(monkeypatch):
    monkeypatch.setattr(
        live_cache,
        "read_open_attendance_source",
        lambda: SimpleNamespace(
            payload=None,
            refreshed_at=FRESH_AT,
            mirror_owned=True,
            available=False,
        ),
    )
    monkeypatch.setattr(
        attendance_state,
        "latest_punch",
        lambda _person_id: {
            "action": "clock_in",
            "wc_name": "Bay 3",
            "occurred_at": FIRST_IN,
            "odoo_attendance_id": 90,
            "synced_to_odoo": True,
            "synced_at": FIRST_IN,
        },
    )

    state = attendance_state.current_state(5)

    assert state["is_clocked_in"] is None
    assert state["attendance_source_unavailable"] is True


def test_owned_stale_open_source_keeps_last_verified_odoo_state(monkeypatch):
    stale_at = FIRST_IN + timedelta(minutes=10)
    monkeypatch.setattr(
        live_cache,
        "read_open_attendance_source",
        lambda: SimpleNamespace(
            payload={
                "5": {
                    "att_id": 91,
                    "check_in": FIRST_IN.isoformat(),
                    "wc_name": "Bay 8",
                }
            },
            refreshed_at=stale_at,
            mirror_owned=True,
            available=True,
        ),
    )
    monkeypatch.setattr(live_cache, "is_stale", lambda _at: True)
    monkeypatch.setattr(
        attendance_state,
        "latest_punch",
        lambda _person_id: {
            "action": "clock_in",
            "wc_name": "Bay 3",
            "occurred_at": FIRST_IN,
            "odoo_attendance_id": 90,
            "synced_to_odoo": True,
            "synced_at": FIRST_IN,
        },
    )

    state = attendance_state.current_state(5)

    assert state["is_clocked_in"] is True
    assert state["current_wc"] == "Bay 8"
    assert state["attendance_source_stale"] is True


def test_owned_open_state_uses_the_frozen_source_stale_decision(monkeypatch):
    monkeypatch.setattr(
        live_cache,
        "read_open_attendance_source",
        lambda: SimpleNamespace(
            payload={
                "5": {
                    "att_id": 91,
                    "check_in": FIRST_IN.isoformat(),
                    "wc_name": "Bay 8",
                }
            },
            refreshed_at=FRESH_AT,
            mirror_owned=True,
            available=True,
            stale=True,
            error="sync stalled",
        ),
    )
    monkeypatch.setattr(live_cache, "is_stale", lambda _at: False)
    monkeypatch.setattr(attendance_state, "latest_punch", lambda _person_id: None)

    state = attendance_state.current_state(5)

    assert state["current_wc"] == "Bay 8"
    assert state["attendance_source_stale"] is True


def test_mirror_day_windows_cap_open_rows_at_verified_freshness(monkeypatch):
    policy = live_cache.AttendanceReadPolicy(
        mirror_owned=True,
        available=True,
        refreshed_at=FRESH_AT,
        mode="shadow",
    )
    monkeypatch.setattr(live_cache, "attendance_read_policy", lambda: policy)
    monkeypatch.setattr(
        attendance_mirror,
        "rows_overlapping",
        lambda *_args: (
            {
                "employee_odoo_id": 5,
                "check_in_utc": FIRST_IN,
                "check_out_utc": None,
                "odoo_work_center_id": 8,
            },
        ),
    )
    monkeypatch.setattr(attendance, "person_id_to_name", lambda: {"5": "Maria"})
    monkeypatch.setattr(
        work_centers_store,
        "app_work_center_name_for_odoo_id",
        lambda _wc_id: "Bay 8",
    )

    windows, available = timeclock_windows.attendance_windows_for_day_with_availability(
        DAY
    )

    assert available is True
    assert windows == {"Maria": [("Bay 8", FIRST_IN, FRESH_AT)]}


@pytest.mark.parametrize(
    ("day", "day_start_utc", "day_end_utc"),
    [
        (
            date(2026, 3, 8),
            datetime(2026, 3, 8, 6, 0, tzinfo=UTC),
            datetime(2026, 3, 9, 5, 0, tzinfo=UTC),
        ),
        (
            date(2026, 11, 1),
            datetime(2026, 11, 1, 5, 0, tzinfo=UTC),
            datetime(2026, 11, 2, 6, 0, tzinfo=UTC),
        ),
    ],
)
def test_mirror_day_windows_clip_rows_to_exact_dst_plant_day(
    monkeypatch, day, day_start_utc, day_end_utc
):
    """An overlapping row must never produce work outside the selected day."""
    policy = live_cache.AttendanceReadPolicy(
        mirror_owned=True,
        available=True,
        refreshed_at=day_end_utc + timedelta(hours=2),
        mode="live",
    )
    monkeypatch.setattr(live_cache, "attendance_read_policy", lambda: policy)
    requested = []

    def rows_overlapping(start_utc, end_utc):
        requested.append((start_utc, end_utc))
        return (
            {
                "employee_odoo_id": 5,
                "check_in_utc": day_start_utc - timedelta(hours=1),
                "check_out_utc": day_end_utc + timedelta(hours=1),
                "odoo_work_center_id": 8,
            },
        )

    monkeypatch.setattr(attendance_mirror, "rows_overlapping", rows_overlapping)
    monkeypatch.setattr(attendance, "person_id_to_name", lambda: {"5": "Maria"})
    monkeypatch.setattr(
        work_centers_store,
        "app_work_center_name_for_odoo_id",
        lambda _wc_id: "Bay 8",
    )

    windows, available = timeclock_windows.attendance_windows_for_day_with_availability(
        day
    )

    assert available is True
    assert requested == [(day_start_utc, day_end_utc)]
    assert windows == {"Maria": [("Bay 8", day_start_utc, day_end_utc)]}


def test_stale_owned_day_source_never_infers_missing_people_are_absent(monkeypatch):
    source = live_cache.AttendanceSourceSnapshot(
        payload={
            "5": {
                "first_check_in": FIRST_IN.isoformat(),
                "currently_open": True,
            }
        },
        refreshed_at=FRESH_AT,
        mirror_owned=True,
        available=True,
        error="incremental sync failed",
        stale=True,
    )
    monkeypatch.setattr(live_cache, "read_attendance_source", lambda _day: source)

    statuses = attendance.status_for_day(
        DAY, ["5", "6"], FIRST_IN, FIRST_IN
    )

    assert statuses["5"]["status"] == "on_time"
    assert "6" not in statuses


def test_stale_owned_open_source_never_infers_missing_person_is_clocked_out(
    monkeypatch,
):
    source = live_cache.AttendanceSourceSnapshot(
        payload={},
        refreshed_at=FRESH_AT,
        mirror_owned=True,
        available=True,
        error="incremental sync failed",
        stale=True,
    )
    monkeypatch.setattr(live_cache, "read_open_attendance_source", lambda: source)
    monkeypatch.setattr(attendance_state, "latest_punch", lambda _person_id: None)

    state = attendance_state.current_state(6)

    assert state["is_clocked_in"] is None
    assert state["attendance_source_unavailable"] is False
    assert state["attendance_source_stale"] is True
    assert state["attendance_source_error"] == "incremental sync failed"


def test_stale_owned_day_source_never_derives_absence_from_a_missing_row(
    monkeypatch,
):
    source = live_cache.AttendanceSourceSnapshot(
        payload={
            "5": {
                "first_check_in": FIRST_IN.isoformat(),
                "currently_open": True,
            }
        },
        refreshed_at=FRESH_AT,
        mirror_owned=True,
        available=True,
        error="incremental sync failed",
        stale=True,
    )
    monkeypatch.setattr(plant_day, "today", lambda: DAY)
    monkeypatch.setattr(
        plant_day,
        "now",
        lambda: datetime.combine(DAY, time(12, 0), tzinfo=shift_config.SITE_TZ),
    )
    monkeypatch.setattr(shift_config, "shift_start_for", lambda _day: time(6, 0))
    monkeypatch.setattr(scheduler_time_off, "time_off_entries_for_day", lambda _day: [])
    monkeypatch.setattr(
        attendance, "name_to_person_id", lambda: {"Missing": "6", "Seen": "5"}
    )
    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: (
            SimpleNamespace(name="Missing", active=True, reserve=False),
            SimpleNamespace(name="Seen", active=True, reserve=False),
        ),
    )
    monkeypatch.setattr(live_cache, "read_attendance_source", lambda _day: source)

    assert attendance.derived_absent_names(DAY) == set()


def test_auto_lunch_stops_on_owned_mirror_unavailability(monkeypatch):
    monkeypatch.setattr(
        auto_lunch_settings,
        "current",
        lambda: SimpleNamespace(enabled=True, observe_only=False),
    )
    monkeypatch.setattr(
        live_cache,
        "read_open_attendance_source",
        lambda **_kwargs: SimpleNamespace(
            payload=None,
            refreshed_at=FRESH_AT,
            mirror_owned=True,
            available=False,
            stale=False,
            error="mirror unavailable",
        ),
    )
    monkeypatch.setattr(
        live_cache,
        "read_open_attendance",
        lambda: (_ for _ in ()).throw(AssertionError("legacy cache forbidden")),
    )
    monkeypatch.setattr(auto_lunch.shift_config, "is_workday", lambda _day: False)
    monkeypatch.setattr(
        auto_lunch.db,
        "query",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unknown state must stop tick")),
    )

    auto_lunch.run_tick(datetime.combine(DAY, time(11, 0), tzinfo=UTC))


def test_auto_lunch_freezes_one_policy_and_one_day_snapshot_for_all_flex_people(
    monkeypatch,
):
    policy = live_cache.AttendanceReadPolicy(
        mirror_owned=True,
        available=True,
        refreshed_at=FRESH_AT,
        mode="live",
    )
    policy_calls = []
    open_calls = []
    day_calls = []
    first_by_id = {
        "5": {
            "first_check_in": datetime(2026, 8, 31, 10, 0, tzinfo=UTC).isoformat(),
            "currently_open": True,
        },
        "6": {
            "first_check_in": datetime(2026, 8, 31, 10, 30, tzinfo=UTC).isoformat(),
            "currently_open": True,
        },
    }
    open_source = live_cache.AttendanceSourceSnapshot(
        payload={
            "5": {"att_id": 51, "check_in": first_by_id["5"]["first_check_in"]},
            "6": {"att_id": 61, "check_in": first_by_id["6"]["first_check_in"]},
        },
        refreshed_at=FRESH_AT,
        mirror_owned=True,
        available=True,
    )
    day_source = live_cache.AttendanceSourceSnapshot(
        payload=first_by_id,
        refreshed_at=FRESH_AT,
        mirror_owned=True,
        available=True,
    )
    monkeypatch.setattr(
        auto_lunch_settings,
        "current",
        lambda: SimpleNamespace(
            enabled=True,
            observe_only=False,
            flex_after_hours=5.0,
            flex_minutes=30,
        ),
    )
    monkeypatch.setattr(
        live_cache,
        "attendance_read_policy",
        lambda **_kwargs: policy_calls.append(True) or policy,
    )
    monkeypatch.setattr(
        live_cache,
        "read_open_attendance_source",
        lambda **kwargs: open_calls.append(kwargs["policy"]) or open_source,
    )
    monkeypatch.setattr(
        live_cache,
        "read_attendance_source",
        lambda day, **kwargs: day_calls.append((day, kwargs["policy"])) or day_source,
    )
    monkeypatch.setattr(auto_lunch.shift_config, "is_workday", lambda _day: False)
    monkeypatch.setattr(auto_lunch, "_flex_person_ids", lambda: {5, 6})
    monkeypatch.setattr(auto_lunch, "_get_runs_bulk", lambda *_args: {})
    monkeypatch.setattr(auto_lunch.attendance_state, "latest_punches_bulk", lambda _ids: {})
    monkeypatch.setattr(auto_lunch, "_fixed_windows_for_candidates", lambda *_args: {})
    monkeypatch.setattr(
        auto_lunch.db,
        "query",
        lambda sql, *_args: []
        if "auto_lunch_runs" in sql or "wage_type" in sql
        else pytest.fail(f"unexpected query: {sql}"),
    )
    advanced = []
    monkeypatch.setattr(
        auto_lunch,
        "_advance_person",
        lambda person_id, *_args, **kwargs: advanced.append(
            (person_id, kwargs["first_clock_in"], kwargs["source"])
        ),
    )

    auto_lunch.run_tick(datetime(2026, 8, 31, 7, 0, tzinfo=UTC))

    assert policy_calls == [True]
    assert open_calls == [policy]
    assert day_calls == [(DAY, policy)]
    assert sorted((pid, first) for pid, first, _source in advanced) == [
        (5, datetime(2026, 8, 31, 10, 0, tzinfo=UTC)),
        (6, datetime(2026, 8, 31, 10, 30, tzinfo=UTC)),
    ]
    assert all(source is open_source for _pid, _first, source in advanced)


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_auto_lunch_batches_legacy_attendance_inputs_once_per_tick(monkeypatch, mode):
    """Legacy/off and pre-baseline shadow ticks share one local-day payload."""
    policy = live_cache.AttendanceReadPolicy(
        mirror_owned=False,
        available=True,
        refreshed_at=FRESH_AT,
        mode=mode,
    )
    open_source = live_cache.AttendanceSourceSnapshot(
        payload={
            "5": {"att_id": 51, "check_in": FIRST_IN.isoformat(), "wc_name": None},
            "6": {"att_id": 61, "check_in": FIRST_IN.isoformat(), "wc_name": None},
        },
        refreshed_at=FRESH_AT,
        mirror_owned=False,
        available=True,
        stale=False,
    )
    first_by_pid = {
        5: datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
        6: datetime(2026, 8, 31, 10, 30, tzinfo=UTC),
    }
    latest_wc_by_pid = {5: "Bay 3", 6: "Repair 1"}
    policy_calls = []
    open_calls = []
    legacy_payload_calls = []
    monkeypatch.setattr(
        auto_lunch_settings,
        "current",
        lambda: SimpleNamespace(
            enabled=True,
            observe_only=False,
            flex_after_hours=5.0,
            flex_minutes=30,
        ),
    )
    monkeypatch.setattr(
        live_cache,
        "attendance_read_policy",
        lambda **_kwargs: policy_calls.append(True) or policy,
    )
    monkeypatch.setattr(
        live_cache,
        "read_open_attendance_source",
        lambda **kwargs: open_calls.append(kwargs["policy"]) or open_source,
    )
    monkeypatch.setattr(
        live_cache,
        "read_attendance_source",
        lambda *_args, **_kwargs: pytest.fail("legacy tick must not choose a second source"),
    )
    monkeypatch.setattr(live_cache, "is_stale", lambda _at: False)
    monkeypatch.setattr(auto_lunch.shift_config, "is_workday", lambda _day: False)
    monkeypatch.setattr(auto_lunch, "_flex_person_ids", lambda: {5, 6})
    monkeypatch.setattr(auto_lunch, "_get_runs_bulk", lambda *_args: {})
    monkeypatch.setattr(auto_lunch.attendance_state, "latest_punches_bulk", lambda _ids: {})
    monkeypatch.setattr(auto_lunch, "_fixed_windows_for_candidates", lambda *_args: {})

    def query(sql, params=None):
        if "state NOT IN" in sql or "wage_type" in sql:
            return []
        if "GROUP BY person_odoo_id" in sql and "ARRAY_AGG" in sql:
            legacy_payload_calls.append(params)
            return [
                {
                    "person_odoo_id": pid,
                    "first_in": first_by_pid[pid],
                    "latest_wc": latest_wc_by_pid[pid],
                }
                for pid in (5, 6)
            ]
        pytest.fail(f"unexpected query: {sql}")

    monkeypatch.setattr(auto_lunch.db, "query", query)
    advanced = []
    monkeypatch.setattr(
        auto_lunch,
        "_advance_person",
        lambda person_id, *_args, **kwargs: advanced.append(
            (
                person_id,
                kwargs["first_clock_in"],
                kwargs["latest_in_wc"],
                kwargs["source"],
            )
        ),
    )

    auto_lunch.run_tick(datetime(2026, 8, 31, 7, 0, tzinfo=UTC))

    assert policy_calls == [True]
    assert open_calls == [policy]
    assert len(legacy_payload_calls) == 1
    assert set(legacy_payload_calls[0][0]) == {5, 6}
    assert sorted(advanced, key=lambda item: item[0]) == [
        (5, first_by_pid[5], "Bay 3", open_source),
        (6, first_by_pid[6], "Repair 1", open_source),
    ]


@pytest.mark.parametrize(
    ("day", "expected_hours"),
    ((date(2026, 3, 8), 23), (date(2026, 11, 1), 25)),
)
def test_auto_lunch_legacy_batch_uses_exact_dst_plant_day(
    monkeypatch, day, expected_hours
):
    query_params = []
    monkeypatch.setattr(
        auto_lunch.db,
        "query",
        lambda _sql, params: query_params.append(params) or [],
    )

    first_clock_ins, latest_in_wcs = auto_lunch._legacy_attendance_inputs_bulk(
        {5, 6}, day
    )

    assert first_clock_ins == {}
    assert latest_in_wcs == {}
    ids, start, end = query_params[0]
    assert set(ids) == {5, 6}
    assert start.date() == day
    assert end.date() == day + timedelta(days=1)
    assert (end.astimezone(UTC) - start.astimezone(UTC)) == timedelta(
        hours=expected_hours
    )


def test_auto_lunch_clock_out_uses_prefetched_wc_without_per_action_read(monkeypatch):
    captured = []
    monkeypatch.setattr(
        auto_lunch,
        "_latest_in_wc",
        lambda *_args, **_kwargs: pytest.fail("per-action local punch query"),
    )
    monkeypatch.setattr(
        auto_lunch,
        "_upsert_run",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )
    window = auto_lunch.Window(FIRST_IN, FIRST_IN + timedelta(minutes=30))

    auto_lunch._apply(
        5,
        DAY,
        "flex",
        None,
        auto_lunch.Transition("auto_out", "clock_out", FIRST_IN),
        {"current_wc": None},
        window,
        SimpleNamespace(observe_only=True),
        latest_in_wc="Bay 3",
    )

    assert captured[0][1]["wc_name"] == "Bay 3"


def test_legacy_attendance_warmers_noop_only_after_mirror_owns(monkeypatch):
    owns = iter([True, True, False, False])
    monkeypatch.setattr(live_cache, "mirror_owns_attendance_reads", lambda: next(owns))
    attendance_refresh = MagicMock()
    open_refresh = MagicMock()
    production_refresh = MagicMock()
    monkeypatch.setattr(live_cache, "refresh_attendance", attendance_refresh)
    monkeypatch.setattr(live_cache, "refresh_odoo_open_attendance", open_refresh)
    monkeypatch.setattr(live_cache, "refresh_production", production_refresh)
    monkeypatch.setattr(app, "plant_today", lambda: DAY)
    monkeypatch.setattr(app, "_zira_client", lambda: object())

    asyncio.run(app._tick_live_cache())
    asyncio.run(app._tick_odoo_attendance())
    asyncio.run(app._tick_live_cache())
    asyncio.run(app._tick_odoo_attendance())

    attendance_refresh.assert_called_once_with(DAY)
    open_refresh.assert_called_once_with()
    assert production_refresh.call_count == 2


def test_day_presence_and_current_open_queries_are_bounded(monkeypatch):
    calls = []

    def query(sql, params=None):
        calls.append((" ".join(sql.split()), params))
        if "GROUP BY employee_odoo_id" in sql:
            return [
                {
                    "employee_odoo_id": 5,
                    "first_check_in": FIRST_IN,
                    "currently_open": True,
                }
            ]
        return [
            {
                "odoo_attendance_id": 90,
                "employee_odoo_id": 5,
                "check_in_utc": FIRST_IN,
                "odoo_work_center_id": 8,
                "odoo_work_center_name": "Luke Bay 8",
                "odoo_department_id": 4,
                "odoo_department_name": "Supervisor",
            }
        ]

    monkeypatch.setattr(attendance_mirror.db, "query", query)

    assert attendance_mirror.day_presence(DAY) == {
        "5": {"first_check_in": FIRST_IN.isoformat(), "currently_open": True}
    }
    open_row = attendance_mirror.current_open_attendance()[0]
    assert open_row["odoo_attendance_id"] == 90
    assert open_row["odoo_department_id"] == 4
    assert open_row["odoo_department_name"] == "Supervisor"
    assert "check_in_utc >= %s" in calls[0][0]
    assert "check_in_utc < %s" in calls[0][0]
    assert "check_out_utc IS NULL" in calls[1][0]
    assert "odoo_department_id" in calls[1][0]
    assert "odoo_department_name" in calls[1][0]


def test_injected_policy_prevents_a_second_ownership_health_decision(monkeypatch):
    policy = live_cache.AttendanceReadPolicy(
        mirror_owned=True,
        available=True,
        refreshed_at=FRESH_AT,
        mode="shadow",
    )
    monkeypatch.setattr(
        live_cache,
        "attendance_read_policy",
        lambda: (_ for _ in ()).throw(AssertionError("policy must stay frozen")),
    )
    monkeypatch.setattr(
        live_cache.attendance_mirror,
        "day_presence",
        lambda _day: {"5": {"first_check_in": FIRST_IN.isoformat(), "currently_open": True}},
    )

    source = live_cache.read_attendance_source(DAY, policy=policy)

    assert source.mirror_owned is True
    assert source.refreshed_at == FRESH_AT
