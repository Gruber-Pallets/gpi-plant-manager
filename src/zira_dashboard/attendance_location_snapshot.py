"""Canonical attendance-location snapshot reads shared across dashboard surfaces."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime

from . import (
    attendance,
    attendance_location_policy,
    attendance_mirror,
    attendance_timeline,
    live_cache,
    work_centers_store,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocationSnapshot:
    """One atomic mirror generation projected into every attendance consumer."""

    policy: live_cache.AttendanceReadPolicy
    attendance_source: live_cache.AttendanceSourceSnapshot | None
    spans: tuple[attendance_timeline.LocationSpan, ...]
    verified_cap_utc: datetime
    current_attendance_ids: frozenset[int]


def project_location_spans(
    day: date,
    *,
    as_of_utc: datetime,
    policy: live_cache.AttendanceReadPolicy,
    rows: Sequence[Mapping[str, object]] | None = None,
) -> tuple[attendance_timeline.LocationSpan, ...]:
    """Project one timeline from the already-frozen health policy."""
    if policy.refreshed_at is None:
        raise RuntimeError("attendance mirror has no verified freshness")
    start_utc, end_utc = attendance_timeline._plant_day_bounds(day)
    if rows is None:
        rows = attendance_mirror.rows_overlapping(start_utc, end_utc)
    if not rows:
        return ()
    rows = attendance_timeline._rows_with_employee_department_fallback(
        rows,
        include_wage_type=True,
    )
    verified_cap = min(as_of_utc, policy.refreshed_at)
    spans = attendance_timeline.project_rows(
        rows,
        as_of_utc=verified_cap,
        verified_through_utc=verified_cap,
        map_work_center=work_centers_store.app_work_center_name_for_odoo_id,
        requires_work_center=attendance_timeline._department_requires_work_center_for_mirror,
        expected_department_id=attendance_timeline._expected_department_id_for_app_work_center,
    )
    canonical_by_id = attendance.person_id_to_name()
    return tuple(
        replace(
            span,
            end_utc=min(span.end_utc, verified_cap),
            employee_name=canonical_by_id.get(
                str(span.employee_odoo_id), span.employee_name
            ),
        )
        for span in spans
        if span.start_utc <= verified_cap
    )


def current_attendance_ids_at(
    rows: Sequence[Mapping[str, object]], verified_cap_utc: datetime
) -> frozenset[int]:
    """Identify raw intervals that are current at the exact verified cap."""
    if verified_cap_utc.utcoffset() is None:
        raise ValueError("verified_cap_utc must be timezone-aware")
    current_ids: set[int] = set()
    for row in rows:
        check_in = row["check_in_utc"]
        if not isinstance(check_in, datetime) or check_in.utcoffset() is None:
            raise ValueError("mirror check_in_utc must be timezone-aware")
        check_out = row.get("check_out_utc")
        if check_out is not None and (
            not isinstance(check_out, datetime) or check_out.utcoffset() is None
        ):
            raise ValueError("mirror check_out_utc must be timezone-aware or null")
        if check_in <= verified_cap_utc and (
            check_out is None or verified_cap_utc < check_out
        ):
            current_ids.add(int(row["odoo_attendance_id"]))
    return frozenset(current_ids)


def read_location_snapshot(day: date, *, as_of_utc: datetime) -> LocationSnapshot:
    """Read rollout, mirror health, and rows once for one dashboard response."""
    if as_of_utc.utcoffset() is None:
        raise ValueError("as_of_utc must be timezone-aware")
    try:
        mode = attendance_location_policy.get_rollout_config().mode
    except Exception as exc:  # Preserve the legacy rollback path.
        policy = live_cache.AttendanceReadPolicy(
            False, True, None, str(exc), "off"
        )
        return LocationSnapshot(policy, None, (), as_of_utc, frozenset())
    if mode == "off":
        policy = live_cache.attendance_read_policy_from_health(
            mode, None, now_utc=as_of_utc
        )
        return LocationSnapshot(policy, None, (), as_of_utc, frozenset())

    start_utc, end_utc = attendance_timeline._plant_day_bounds(day)
    try:
        atomic = attendance_mirror.snapshot_overlapping(start_utc, end_utc)
    except Exception as exc:  # Saved shadow/live must not fall through to legacy.
        log.exception("Could not read the atomic Staffing mirror snapshot for %s", day)
        policy = live_cache.AttendanceReadPolicy(
            True, False, None, str(exc), mode
        )
        source = live_cache.AttendanceSourceSnapshot(
            None, None, True, False, str(exc), False, True
        )
        return LocationSnapshot(policy, source, (), as_of_utc, frozenset())

    policy = live_cache.attendance_read_policy_from_health(
        mode, atomic.health, now_utc=as_of_utc
    )
    if not policy.mirror_owned:
        return LocationSnapshot(policy, None, (), as_of_utc, frozenset())
    verified_cap = (
        min(as_of_utc, policy.refreshed_at)
        if policy.refreshed_at is not None
        else as_of_utc
    )
    if policy.refreshed_at is not None and policy.refreshed_at != verified_cap:
        policy = replace(policy, refreshed_at=verified_cap)
    if not policy.available or policy.refreshed_at is None:
        source = live_cache.AttendanceSourceSnapshot(
            None,
            policy.refreshed_at,
            True,
            False,
            policy.error,
            policy.stale,
            True,
        )
        return LocationSnapshot(
            policy, source, (), verified_cap, frozenset()
        )
    try:
        rows = atomic.rows
        current_attendance_ids = current_attendance_ids_at(rows, verified_cap)
        payload = attendance_mirror.day_presence_from_rows(
            day, rows, as_of_utc=verified_cap
        )
        spans = project_location_spans(
            day,
            as_of_utc=verified_cap,
            policy=policy,
            rows=rows,
        )
    except Exception as exc:  # noqa: BLE001 -- one failed snapshot stays unavailable
        log.exception("Could not read the frozen mirror snapshot for %s", day)
        failed_policy = replace(policy, available=False, error=str(exc))
        source = live_cache.AttendanceSourceSnapshot(
            None,
            policy.refreshed_at,
            True,
            False,
            str(exc),
            policy.stale,
            True,
        )
        return LocationSnapshot(
            failed_policy, source, (), verified_cap, frozenset()
        )
    return LocationSnapshot(
        policy,
        live_cache.AttendanceSourceSnapshot(
            payload,
            policy.refreshed_at,
            True,
            True,
            policy.error,
            policy.stale,
            True,
        ),
        spans,
        verified_cap,
        current_attendance_ids,
    )
