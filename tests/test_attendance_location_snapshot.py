"""Canonical attendance-location snapshot reads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from zira_dashboard import attendance_location_snapshot, attendance_mirror
from zira_dashboard.attendance_timeline import LocationSpan

NOW = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
DAY = NOW.date()


def _span(
    name: str,
    status: str,
    *,
    app_wc: str | None = None,
    raw_wc: str | None = None,
    start_minutes: int = 30,
    employee_id: int | None = None,
    end_utc: datetime | None = None,
    attendance_ids: tuple[int, ...] = (91,),
) -> LocationSpan:
    return LocationSpan(
        employee_odoo_id=employee_id or abs(hash(name)) % 10000 + 1,
        employee_name=name,
        start_utc=NOW - timedelta(minutes=start_minutes),
        end_utc=end_utc or NOW + timedelta(minutes=1),
        status=status,
        app_work_center_name=app_wc,
        odoo_work_center_id=8 if (app_wc or raw_wc) else None,
        odoo_work_center_name=raw_wc,
        attendance_ids=attendance_ids,
        department_repair=None,
    )


def test_read_location_snapshot_uses_one_atomic_generation(monkeypatch):
    verified_at = NOW - timedelta(seconds=10)
    first_row = {
        "odoo_attendance_id": 91,
        "employee_odoo_id": 101,
        "check_in_utc": NOW - timedelta(hours=1),
        "check_out_utc": None,
    }
    atomic_snapshot = SimpleNamespace(
        health=attendance_mirror.MirrorHealth(
            last_incremental_completed_at=verified_at,
            last_full_sweep_completed_at=verified_at,
            baseline_completed_at=verified_at,
            oldest_recalc_requested_at=None,
            last_error=None,
        ),
        rows=(first_row,),
    )
    calls = []
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_location_policy,
        "get_rollout_config",
        lambda: SimpleNamespace(mode="shadow"),
    )
    monkeypatch.setattr(
        attendance_mirror,
        "snapshot_overlapping",
        lambda *_args: calls.append(True) or atomic_snapshot,
    )
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_timeline,
        "_plant_day_bounds",
        lambda _day: (NOW - timedelta(hours=16), NOW + timedelta(hours=8)),
    )
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_timeline,
        "_rows_with_employee_department_fallback",
        lambda rows, **_kwargs: rows,
    )
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_timeline,
        "project_rows",
        lambda rows, **_kwargs: (
            _span(
                "Alex",
                "valid",
                app_wc="Bay 3",
                employee_id=rows[0]["employee_odoo_id"],
            ),
        ),
    )
    monkeypatch.setattr(
        attendance_location_snapshot.attendance,
        "person_id_to_name",
        lambda: {"101": "Alex"},
    )

    snapshot = attendance_location_snapshot.read_location_snapshot(
        DAY, as_of_utc=NOW
    )

    assert calls == [True]
    assert snapshot.verified_cap_utc == verified_at
    assert snapshot.current_attendance_ids == frozenset({91})
    assert snapshot.spans[0].employee_odoo_id == 101


def test_exact_cap_current_selection_uses_half_open_interval():
    cap = NOW
    rows = (
        {
            "odoo_attendance_id": 1,
            "check_in_utc": cap - timedelta(hours=1),
            "check_out_utc": None,
        },
        {
            "odoo_attendance_id": 2,
            "check_in_utc": cap - timedelta(hours=1),
            "check_out_utc": cap + timedelta(minutes=1),
        },
        {
            "odoo_attendance_id": 3,
            "check_in_utc": cap - timedelta(hours=1),
            "check_out_utc": cap,
        },
        {
            "odoo_attendance_id": 4,
            "check_in_utc": cap,
            "check_out_utc": None,
        },
    )

    current_attendance_ids = attendance_location_snapshot.current_attendance_ids_at(
        rows, cap
    )

    assert current_attendance_ids == frozenset({1, 2, 4})


def test_read_location_snapshot_derives_policy_presence_and_spans_from_atomic_snapshot(
    monkeypatch,
):
    verified_at = NOW - timedelta(seconds=10)
    raw_row = {
        "odoo_attendance_id": 91,
        "employee_odoo_id": 101,
        "check_in_utc": NOW - timedelta(hours=1),
        "check_out_utc": None,
    }
    atomic = SimpleNamespace(
        health=attendance_mirror.MirrorHealth(
            last_incremental_completed_at=verified_at,
            last_full_sweep_completed_at=verified_at,
            baseline_completed_at=verified_at,
            oldest_recalc_requested_at=None,
            last_error=None,
        ),
        rows=(raw_row,),
    )
    atomic_calls = []
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_location_policy,
        "get_rollout_config",
        lambda: SimpleNamespace(mode="shadow"),
    )
    monkeypatch.setattr(
        attendance_mirror,
        "snapshot_overlapping",
        lambda *args: atomic_calls.append(args) or atomic,
    )
    monkeypatch.setattr(
        attendance_location_snapshot.live_cache,
        "attendance_read_policy",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("snapshot must not perform a separate health read")
        ),
    )
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_timeline,
        "_plant_day_bounds",
        lambda _day: (NOW - timedelta(hours=16), NOW + timedelta(hours=8)),
    )
    monkeypatch.setattr(
        attendance_location_snapshot,
        "project_location_spans",
        lambda _day, **_kwargs: (
            _span("Alex", "valid", app_wc="Bay 3", employee_id=101, end_utc=verified_at),
        ),
    )

    snapshot = attendance_location_snapshot.read_location_snapshot(
        DAY, as_of_utc=NOW
    )

    assert len(atomic_calls) == 1
    assert snapshot.policy.mirror_owned is True
    assert snapshot.policy.refreshed_at == verified_at
    assert snapshot.attendance_source.payload["101"]["currently_open"] is True
    assert snapshot.spans[0].employee_odoo_id == 101
    assert snapshot.current_attendance_ids == frozenset({91})


def test_read_location_snapshot_caps_open_span_and_mirror_freshness(monkeypatch):
    verified_at = NOW
    health_refreshed_at = NOW + timedelta(seconds=10)
    row = {
        "odoo_attendance_id": 91,
        "employee_odoo_id": 101,
        "check_in_utc": NOW - timedelta(hours=1),
        "check_out_utc": None,
    }
    closed_row = {
        "odoo_attendance_id": 92,
        "employee_odoo_id": 202,
        "check_in_utc": NOW - timedelta(hours=1),
        "check_out_utc": verified_at,
    }
    projected_as_of = []
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_timeline,
        "_plant_day_bounds",
        lambda _day: (NOW - timedelta(hours=16), NOW + timedelta(hours=8)),
    )
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_location_policy,
        "get_rollout_config",
        lambda: SimpleNamespace(mode="shadow"),
    )
    monkeypatch.setattr(
        attendance_mirror,
        "snapshot_overlapping",
        lambda *_args: SimpleNamespace(
            health=attendance_mirror.MirrorHealth(
                last_incremental_completed_at=health_refreshed_at,
                last_full_sweep_completed_at=health_refreshed_at,
                baseline_completed_at=health_refreshed_at,
                oldest_recalc_requested_at=None,
                last_error=None,
            ),
            rows=(row, closed_row),
        ),
    )
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_timeline,
        "_rows_with_employee_department_fallback",
        lambda rows, **_kwargs: rows,
    )

    def project_rows(_rows, **kwargs):
        projected_as_of.append(kwargs["as_of_utc"])
        span = _span(
            "Alex",
            "valid",
            app_wc="Bay 3",
            employee_id=101,
            attendance_ids=(91,),
        )
        return (
            LocationSpan(
                **{
                    **span.__dict__,
                    "start_utc": NOW - timedelta(hours=1),
                    "end_utc": kwargs["as_of_utc"] + timedelta(minutes=1),
                }
            ),
        )

    monkeypatch.setattr(
        attendance_location_snapshot.attendance_timeline, "project_rows", project_rows
    )
    monkeypatch.setattr(
        attendance_location_snapshot.attendance,
        "person_id_to_name",
        lambda: {"101": "Alex"},
    )

    snapshot = attendance_location_snapshot.read_location_snapshot(
        DAY, as_of_utc=NOW
    )

    assert projected_as_of == [verified_at]
    assert snapshot.spans[0].end_utc == verified_at
    assert snapshot.verified_cap_utc == verified_at
    assert snapshot.policy.refreshed_at == verified_at
    assert snapshot.attendance_source.refreshed_at == verified_at
    assert snapshot.attendance_source.payload["101"]["currently_open"] is True
    assert snapshot.attendance_source.payload["202"]["currently_open"] is False
    assert snapshot.current_attendance_ids == frozenset({91})


def test_project_location_spans_use_canonical_roster_name_by_employee_id(monkeypatch):
    raw_span = _span("Alice Full Odoo Name", "valid", app_wc="Bay 8")
    policy = SimpleNamespace(refreshed_at=NOW - timedelta(seconds=10))
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_timeline,
        "_plant_day_bounds",
        lambda _day: (NOW - timedelta(hours=8), NOW + timedelta(hours=8)),
    )
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_mirror,
        "rows_overlapping",
        lambda *_args: ({},),
    )
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_timeline,
        "_rows_with_employee_department_fallback",
        lambda rows, **_kwargs: rows,
    )
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_timeline,
        "project_rows",
        lambda *_args, **_kwargs: (raw_span,),
    )
    monkeypatch.setattr(
        attendance_location_snapshot.attendance,
        "person_id_to_name",
        lambda: {str(raw_span.employee_odoo_id): "Alice A."},
    )

    spans = attendance_location_snapshot.project_location_spans(
        DAY, as_of_utc=NOW, policy=policy
    )

    assert spans[0].employee_name == "Alice A."


def test_read_location_snapshot_keeps_stale_but_available_data(monkeypatch):
    verified_at = NOW - timedelta(minutes=4)
    row = {
        "odoo_attendance_id": 91,
        "employee_odoo_id": 101,
        "check_in_utc": NOW - timedelta(hours=1),
        "check_out_utc": None,
    }
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_location_policy,
        "get_rollout_config",
        lambda: SimpleNamespace(mode="live"),
    )
    monkeypatch.setattr(
        attendance_mirror,
        "snapshot_overlapping",
        lambda *_args: SimpleNamespace(
            health=attendance_mirror.MirrorHealth(
                last_incremental_completed_at=verified_at,
                last_full_sweep_completed_at=verified_at,
                baseline_completed_at=verified_at,
                oldest_recalc_requested_at=None,
                last_error="incremental sync failed",
            ),
            rows=(row,),
        ),
    )
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_timeline,
        "_plant_day_bounds",
        lambda _day: (NOW - timedelta(hours=16), NOW + timedelta(hours=8)),
    )
    monkeypatch.setattr(
        attendance_location_snapshot,
        "project_location_spans",
        lambda _day, **_kwargs: (
            _span("Alice", "valid", app_wc="Bay 8", employee_id=101),
        ),
    )

    snapshot = attendance_location_snapshot.read_location_snapshot(
        DAY, as_of_utc=NOW
    )

    assert snapshot.policy.stale is True
    assert snapshot.policy.available is True
    assert snapshot.attendance_source.available is True
    assert snapshot.spans[0].employee_odoo_id == 101
    assert snapshot.attendance_source.error == "incremental sync failed"


def test_read_location_snapshot_unavailable_on_mirror_read_failure(monkeypatch):
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_location_policy,
        "get_rollout_config",
        lambda: SimpleNamespace(mode="shadow"),
    )
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_timeline,
        "_plant_day_bounds",
        lambda _day: (NOW - timedelta(hours=16), NOW + timedelta(hours=8)),
    )
    monkeypatch.setattr(
        attendance_mirror,
        "snapshot_overlapping",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("mirror read failed")),
    )

    snapshot = attendance_location_snapshot.read_location_snapshot(
        DAY, as_of_utc=NOW
    )

    assert snapshot.policy.available is False
    assert snapshot.spans == ()
    assert snapshot.attendance_source.available is False
    assert "mirror read failed" in snapshot.attendance_source.error


def test_read_location_snapshot_unavailable_when_projection_fails(monkeypatch):
    verified_at = NOW - timedelta(seconds=10)
    row = {
        "odoo_attendance_id": 91,
        "employee_odoo_id": 101,
        "check_in_utc": NOW - timedelta(hours=1),
        "check_out_utc": None,
    }
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_location_policy,
        "get_rollout_config",
        lambda: SimpleNamespace(mode="shadow"),
    )
    monkeypatch.setattr(
        attendance_mirror,
        "snapshot_overlapping",
        lambda *_args: SimpleNamespace(
            health=attendance_mirror.MirrorHealth(
                last_incremental_completed_at=verified_at,
                last_full_sweep_completed_at=verified_at,
                baseline_completed_at=verified_at,
                oldest_recalc_requested_at=None,
                last_error=None,
            ),
            rows=(row,),
        ),
    )
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_timeline,
        "_plant_day_bounds",
        lambda _day: (NOW - timedelta(hours=16), NOW + timedelta(hours=8)),
    )
    monkeypatch.setattr(
        attendance_location_snapshot,
        "project_location_spans",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("projection failed")
        ),
    )

    snapshot = attendance_location_snapshot.read_location_snapshot(
        DAY, as_of_utc=NOW
    )

    assert snapshot.policy.available is False
    assert snapshot.spans == ()
    assert snapshot.attendance_source.available is False
    assert "projection failed" in snapshot.attendance_source.error


def test_read_location_snapshot_off_mode_returns_empty_snapshot(monkeypatch):
    monkeypatch.setattr(
        attendance_location_snapshot.attendance_location_policy,
        "get_rollout_config",
        lambda: SimpleNamespace(mode="off"),
    )

    snapshot = attendance_location_snapshot.read_location_snapshot(
        DAY, as_of_utc=NOW
    )

    assert snapshot.policy.mirror_owned is False
    assert snapshot.spans == ()
    assert snapshot.attendance_source is None


def test_current_attendance_ids_at_requires_timezone_aware_cap():
    with pytest.raises(ValueError, match="timezone-aware"):
        attendance_location_snapshot.current_attendance_ids_at(
            (), datetime(2026, 8, 31, 16, 0)
        )
