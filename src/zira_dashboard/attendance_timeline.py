"""Pure projection of mirrored Odoo attendance into atomic location spans."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
import re
from typing import Literal, TypeAlias

from . import (
    attendance_location_policy,
    attendance_mirror,
    db,
    shift_config,
    work_centers_store,
)


LocationStatus: TypeAlias = Literal[
    "valid",
    "pending_first_location",
    "exempt_no_location",
    "missing_required_location",
    "unmapped_location",
    "conflicting_location",
    "stale_open_location",
]


@dataclass(frozen=True)
class LocationSpan:
    employee_odoo_id: int
    employee_name: str
    start_utc: datetime
    end_utc: datetime
    status: LocationStatus
    app_work_center_name: str | None
    odoo_work_center_id: int | None
    odoo_work_center_name: str | None
    attendance_ids: tuple[int, ...]
    department_repair: tuple[int, int, datetime] | None


@dataclass(frozen=True)
class _SourceRow:
    attendance_id: int
    employee_id: int
    employee_name: str
    check_in: datetime
    check_out: datetime | None
    work_center_id: int | None
    work_center_name: str | None
    department_id: int | None
    department_name: str | None
    wage_type: str | None
    write_date: datetime

    def effective_end(self, as_of_utc: datetime) -> datetime:
        return self.check_out if self.check_out is not None else as_of_utc


@dataclass(frozen=True)
class _DayState:
    start_utc: datetime
    end_utc: datetime
    grace_boundary: datetime
    first_work_center_at: datetime | None


_ROW_FIELDS = (
    "odoo_attendance_id",
    "employee_odoo_id",
    "employee_name",
    "check_in_utc",
    "check_out_utc",
    "odoo_work_center_id",
    "odoo_work_center_name",
    "odoo_department_id",
    "odoo_department_name",
    "odoo_write_date",
)
_NUMBERED_DEPARTMENT_PREFIX = re.compile(r"^\s*[0-9]+\s*")


def _aware_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise TypeError(f"{field_name} must be an aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be an aware UTC datetime")
    return value.astimezone(UTC)


def _optional_aware_utc(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _aware_utc(value, field_name)


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _optional_positive_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field_name)


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field_name} must be non-empty text")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text or None")
    return value


def _duration(value: object, field_name: str) -> timedelta:
    if not isinstance(value, timedelta):
        raise TypeError(f"{field_name} must be a timedelta")
    if value < timedelta(0):
        raise ValueError(f"{field_name} cannot be negative")
    return value


def _source_row(raw: object) -> _SourceRow:
    if not isinstance(raw, Mapping):
        raise TypeError("attendance row must be a mapping")
    missing = [field for field in _ROW_FIELDS if field not in raw]
    if missing:
        raise ValueError("attendance row omitted required field(s): " + ", ".join(missing))
    check_in = _aware_utc(raw["check_in_utc"], "check_in_utc")
    check_out = _optional_aware_utc(raw["check_out_utc"], "check_out_utc")
    if check_out is not None and check_out <= check_in:
        raise ValueError("check_out_utc must be after check_in_utc")
    return _SourceRow(
        attendance_id=_positive_int(raw["odoo_attendance_id"], "odoo_attendance_id"),
        employee_id=_positive_int(raw["employee_odoo_id"], "employee_odoo_id"),
        employee_name=_text(raw["employee_name"], "employee_name"),
        check_in=check_in,
        check_out=check_out,
        work_center_id=_optional_positive_int(raw["odoo_work_center_id"], "odoo_work_center_id"),
        work_center_name=_optional_text(raw["odoo_work_center_name"], "odoo_work_center_name"),
        department_id=_optional_positive_int(raw["odoo_department_id"], "odoo_department_id"),
        department_name=_optional_text(raw["odoo_department_name"], "odoo_department_name"),
        wage_type=_optional_text(raw.get("employee_wage_type"), "employee_wage_type"),
        write_date=_aware_utc(raw["odoo_write_date"], "odoo_write_date"),
    )


def _normalize_rows(rows: Sequence[Mapping[str, object]]) -> tuple[_SourceRow, ...]:
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise TypeError("rows must be a sequence")
    normalized: list[_SourceRow] = []
    attendance_ids: set[int] = set()
    employee_names: dict[int, str] = {}
    for raw in rows:
        source = _source_row(raw)
        if source.attendance_id in attendance_ids:
            raise ValueError(f"duplicate odoo_attendance_id {source.attendance_id}")
        attendance_ids.add(source.attendance_id)
        prior_name = employee_names.setdefault(source.employee_id, source.employee_name)
        if prior_name != source.employee_name:
            raise ValueError(
                f"inconsistent employee identity for employee_odoo_id {source.employee_id}"
            )
        normalized.append(source)
    return tuple(
        sorted(
            normalized,
            key=lambda item: (
                item.employee_id,
                item.check_in,
                item.attendance_id,
            ),
        )
    )


def _raw_work_center_name(active: Sequence[_SourceRow], work_center_id: int) -> str | None:
    named = [
        source
        for source in active
        if source.work_center_id == work_center_id and source.work_center_name is not None
    ]
    if not named:
        return None
    source = max(named, key=lambda item: (item.write_date, item.attendance_id))
    return source.work_center_name


def _validated_mapping_result(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("map_work_center must return text or None")
    return value


def _validated_requirement(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("requires_work_center must return bool")
    return value


def _requires_work_center(
    source: _SourceRow,
    department_requirement: Callable[[str | None], bool],
) -> bool:
    """Salaried Odoo employees need no location unless one is recorded."""
    if source.wage_type == "monthly":
        return False
    return _validated_requirement(department_requirement(source.department_name))


def _validated_expected_department(value: object) -> int | None:
    if value is None:
        return None
    try:
        return _positive_int(value, "expected_department_id result")
    except ValueError as exc:
        raise TypeError("expected_department_id must return a positive integer or None") from exc


def _department_repair(
    active: Sequence[_SourceRow],
    *,
    work_center_id: int,
    target_department_id: int | None,
) -> tuple[int, int, datetime] | None:
    if target_department_id is None:
        return None
    candidates = sorted(
        (
            source
            for source in active
            if source.work_center_id == work_center_id
            and source.department_id != target_department_id
        ),
        key=lambda source: source.attendance_id,
    )
    if len(candidates) > 1:
        ids = ", ".join(str(source.attendance_id) for source in candidates)
        raise ValueError(f"multiple department repairs required for {ids}")
    if not candidates:
        return None
    source = candidates[0]
    return source.attendance_id, target_department_id, source.write_date


def _span(
    source_rows: Sequence[_SourceRow],
    *,
    left: datetime,
    right: datetime,
    status: LocationStatus,
    app_work_center_name: str | None = None,
    odoo_work_center_id: int | None = None,
    odoo_work_center_name: str | None = None,
    department_repair: tuple[int, int, datetime] | None = None,
) -> LocationSpan:
    first = source_rows[0]
    return LocationSpan(
        employee_odoo_id=first.employee_id,
        employee_name=first.employee_name,
        start_utc=left,
        end_utc=right,
        status=status,
        app_work_center_name=app_work_center_name,
        odoo_work_center_id=odoo_work_center_id,
        odoo_work_center_name=odoo_work_center_name,
        attendance_ids=tuple(sorted(source.attendance_id for source in source_rows)),
        department_repair=department_repair,
    )


def _semantic_key(span: LocationSpan) -> tuple[object, ...]:
    return (
        span.employee_odoo_id,
        span.employee_name,
        span.status,
        span.app_work_center_name,
        span.odoo_work_center_id,
        span.odoo_work_center_name,
        span.attendance_ids,
        span.department_repair,
    )


def _merge_adjacent(spans: Sequence[LocationSpan]) -> tuple[LocationSpan, ...]:
    merged: list[LocationSpan] = []
    for span in spans:
        if span.end_utc <= span.start_utc:
            raise ValueError("location spans must have positive duration")
        if (
            merged
            and merged[-1].end_utc == span.start_utc
            and _semantic_key(merged[-1]) == _semantic_key(span)
        ):
            merged[-1] = replace(merged[-1], end_utc=span.end_utc)
        else:
            merged.append(span)
    return tuple(merged)


def _plant_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=shift_config.SITE_TZ)
    end = datetime.combine(
        day + timedelta(days=1),
        time.min,
        tzinfo=shift_config.SITE_TZ,
    )
    return start.astimezone(UTC), end.astimezone(UTC)


def _plant_days_for_interval(start_utc: datetime, end_utc: datetime) -> set[date]:
    first = start_utc.astimezone(shift_config.SITE_TZ).date()
    last = (end_utc - timedelta(microseconds=1)).astimezone(shift_config.SITE_TZ).date()
    days: set[date] = set()
    current = first
    while current <= last:
        days.add(current)
        current += timedelta(days=1)
    return days


def _day_states(
    covered: Sequence[_SourceRow],
    *,
    as_of_utc: datetime,
    grace: timedelta,
) -> dict[date, _DayState]:
    days: set[date] = set()
    for source in covered:
        days.update(
            _plant_days_for_interval(
                source.check_in,
                source.effective_end(as_of_utc),
            )
        )

    states: dict[date, _DayState] = {}
    for day in sorted(days):
        day_start, day_end = _plant_day_bounds(day)
        active = [
            source
            for source in covered
            if source.check_in < day_end and source.effective_end(as_of_utc) > day_start
        ]
        first_clock_in = min(max(source.check_in, day_start) for source in active)
        first_work_center_at = min(
            (
                max(source.check_in, day_start)
                for source in active
                if source.work_center_id is not None
            ),
            default=None,
        )
        states[day] = _DayState(
            start_utc=day_start,
            end_utc=day_end,
            grace_boundary=first_clock_in + grace,
            first_work_center_at=first_work_center_at,
        )
    return states


def _project_employee(
    employee_rows: Sequence[_SourceRow],
    *,
    as_of_utc: datetime,
    verified_through_utc: datetime,
    map_work_center: Callable[[int], str | None],
    requires_work_center: Callable[[str | None], bool],
    expected_department_id: Callable[[str], int | None],
    grace: timedelta,
    stale_after: timedelta,
) -> tuple[LocationSpan, ...]:
    covered = [
        source for source in employee_rows if source.effective_end(as_of_utc) > source.check_in
    ]
    if not covered:
        return ()

    day_states = _day_states(covered, as_of_utc=as_of_utc, grace=grace)
    boundaries = {
        boundary
        for source in covered
        for boundary in (source.check_in, source.effective_end(as_of_utc))
    }
    for state in day_states.values():
        if any(
            source.work_center_id is None
            and max(source.check_in, state.start_utc)
            < state.grace_boundary
            < min(source.effective_end(as_of_utc), state.end_utc)
            for source in covered
        ):
            boundaries.add(state.grace_boundary)
        boundaries.update((state.start_utc, state.end_utc))

    source_is_stale = as_of_utc - verified_through_utc > stale_after
    if source_is_stale:
        for source in covered:
            if source.check_out is not None:
                continue
            stale_boundary = min(as_of_utc, max(source.check_in, verified_through_utc))
            if source.check_in < stale_boundary < as_of_utc:
                boundaries.add(stale_boundary)

    projected: list[LocationSpan] = []
    for left, right in pairwise(sorted(boundaries)):
        active = [
            source
            for source in covered
            if source.check_in < right and source.effective_end(as_of_utc) > left
        ]
        if not active:
            continue

        stale_active = source_is_stale and any(
            source.check_out is None
            and left >= min(as_of_utc, max(source.check_in, verified_through_utc))
            for source in active
        )
        distinct_work_centers = sorted(
            {source.work_center_id for source in active if source.work_center_id is not None}
        )
        if stale_active:
            work_center_id = distinct_work_centers[0] if len(distinct_work_centers) == 1 else None
            work_center_name = (
                _raw_work_center_name(active, work_center_id)
                if work_center_id is not None
                else None
            )
            projected.append(
                _span(
                    active,
                    left=left,
                    right=right,
                    status="stale_open_location",
                    odoo_work_center_id=work_center_id,
                    odoo_work_center_name=work_center_name,
                )
            )
            continue

        if len(distinct_work_centers) > 1:
            projected.append(
                _span(
                    active,
                    left=left,
                    right=right,
                    status="conflicting_location",
                )
            )
            continue

        if not distinct_work_centers:
            day_state = day_states[left.astimezone(shift_config.SITE_TZ).date()]
            required = any(
                _requires_work_center(source, requires_work_center)
                for source in active
            )
            if not required:
                status: LocationStatus = "exempt_no_location"
            elif left < day_state.grace_boundary and (
                day_state.first_work_center_at is None or left < day_state.first_work_center_at
            ):
                status = "pending_first_location"
            else:
                status = "missing_required_location"
            projected.append(_span(active, left=left, right=right, status=status))
            continue

        work_center_id = distinct_work_centers[0]
        work_center_name = _raw_work_center_name(active, work_center_id)
        app_work_center_name = _validated_mapping_result(map_work_center(work_center_id))
        if app_work_center_name is None:
            projected.append(
                _span(
                    active,
                    left=left,
                    right=right,
                    status="unmapped_location",
                    odoo_work_center_id=work_center_id,
                    odoo_work_center_name=work_center_name,
                )
            )
            continue

        target_department_id = _validated_expected_department(
            expected_department_id(app_work_center_name)
        )
        projected.append(
            _span(
                active,
                left=left,
                right=right,
                status="valid",
                app_work_center_name=app_work_center_name,
                odoo_work_center_id=work_center_id,
                odoo_work_center_name=work_center_name,
                department_repair=_department_repair(
                    active,
                    work_center_id=work_center_id,
                    target_department_id=target_department_id,
                ),
            )
        )

    return _merge_adjacent(projected)


def project_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    as_of_utc: datetime,
    verified_through_utc: datetime,
    map_work_center: Callable[[int], str | None],
    requires_work_center: Callable[[str | None], bool],
    expected_department_id: Callable[[str], int | None],
    grace: timedelta = timedelta(minutes=5),
    stale_after: timedelta = timedelta(seconds=90),
) -> tuple[LocationSpan, ...]:
    """Project normalized mirror rows into deterministic half-open spans."""
    as_of = _aware_utc(as_of_utc, "as_of_utc")
    verified = _aware_utc(verified_through_utc, "verified_through_utc")
    grace_duration = _duration(grace, "grace")
    stale_duration = _duration(stale_after, "stale_after")
    for dependency, name in (
        (map_work_center, "map_work_center"),
        (requires_work_center, "requires_work_center"),
        (expected_department_id, "expected_department_id"),
    ):
        if not callable(dependency):
            raise TypeError(f"{name} must be callable")

    normalized = _normalize_rows(rows)
    by_employee: dict[int, list[_SourceRow]] = {}
    for source in normalized:
        by_employee.setdefault(source.employee_id, []).append(source)

    result: list[LocationSpan] = []
    for employee_id in sorted(by_employee):
        result.extend(
            _project_employee(
                by_employee[employee_id],
                as_of_utc=as_of,
                verified_through_utc=verified,
                map_work_center=map_work_center,
                requires_work_center=requires_work_center,
                expected_department_id=expected_department_id,
                grace=grace_duration,
                stale_after=stale_duration,
            )
        )
    return tuple(result)


def _expected_department_id_for_app_work_center(
    _app_work_center_name: str,
) -> int | None:
    """Return no repair target until a local Odoo department-ID store exists.

    This request path must stay local-only. The later background repair flow
    supplies its Odoo-facade-owned resolver directly to :func:`project_rows`.
    """
    return None


def _department_requires_work_center_for_mirror(
    department_name: str | None,
) -> bool:
    """Resolve raw numbered Odoo labels against the clean local registry."""
    if department_name is None:
        return attendance_location_policy.department_requires_work_center(None)
    normalized = _NUMBERED_DEPARTMENT_PREFIX.sub("", department_name).strip()
    return attendance_location_policy.department_requires_work_center(
        normalized or department_name.strip()
    )


def _rows_with_employee_department_fallback(
    rows: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    missing_ids = sorted(
        {
            int(row["employee_odoo_id"])
            for row in rows
            if not str(row.get("odoo_department_name") or "").strip()
        }
    )
    if not missing_ids:
        return tuple(rows)
    home_rows = db.query(
        "SELECT odoo_id, department_name FROM people WHERE odoo_id = ANY(%s)",
        (missing_ids,),
    )
    home_by_id = {int(row["odoo_id"]): row.get("department_name") for row in home_rows}
    enriched = []
    for row in rows:
        if str(row.get("odoo_department_name") or "").strip():
            enriched.append(row)
            continue
        employee_department = home_by_id.get(int(row["employee_odoo_id"]))
        effective = attendance_location_policy.effective_department_name(
            None,
            employee_department,
        )
        enriched.append({**row, "odoo_department_name": effective})
    return tuple(enriched)


def timeline_for_range(
    start_utc: datetime,
    end_utc: datetime,
    *,
    as_of_utc: datetime | None = None,
) -> tuple[LocationSpan, ...]:
    """Read active mirror rows and return spans clipped to ``[start, end)``."""
    start = _aware_utc(start_utc, "start_utc")
    end = _aware_utc(end_utc, "end_utc")
    if end <= start:
        raise ValueError("end_utc must be after start_utc")
    as_of = _aware_utc(
        datetime.now(UTC) if as_of_utc is None else as_of_utc,
        "as_of_utc",
    )

    context_start, _context_end = _plant_day_bounds(start.astimezone(shift_config.SITE_TZ).date())
    verified_through = attendance_mirror.health_snapshot().last_incremental_completed_at
    rows = attendance_mirror.rows_overlapping(context_start, end)
    if not rows:
        return ()
    rows = _rows_with_employee_department_fallback(rows)
    if verified_through is None:
        raise RuntimeError("attendance mirror has no verified freshness")

    projected = project_rows(
        rows,
        as_of_utc=as_of,
        verified_through_utc=verified_through,
        map_work_center=work_centers_store.app_work_center_name_for_odoo_id,
        requires_work_center=_department_requires_work_center_for_mirror,
        expected_department_id=(_expected_department_id_for_app_work_center),
    )
    clipped = [
        replace(
            span,
            start_utc=max(start, span.start_utc),
            end_utc=min(end, span.end_utc),
        )
        for span in projected
        if min(end, span.end_utc) > max(start, span.start_utc)
    ]
    return _merge_adjacent(clipped)


__all__ = [
    "LocationSpan",
    "LocationStatus",
    "project_rows",
    "timeline_for_range",
]
