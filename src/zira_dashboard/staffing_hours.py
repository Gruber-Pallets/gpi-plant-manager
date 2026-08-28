"""Pure date-range and aggregation rules for the Staffing Hours report.

The route owns Odoo reads.  This module only consumes normalized records, so
the report can be tested without network or database access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
import math
from typing import Callable, Literal, Mapping, Sequence

from . import app_settings, shift_config


PAY_PERIOD_SETTING = "staffing_hours_pay_period"
DEFAULT_PAY_PERIOD = {"anchor": "2026-08-16", "cycle_days": 14}

_PAY_PERIOD_PRESETS = frozenset({"this_pay_period", "last_pay_period"})
_NAMED_PRESETS = frozenset({
    "this_week", "last_week", "this_pay_period", "last_pay_period",
    "this_month", "last_month", "custom",
})


@dataclass(frozen=True)
class PayPeriodConfig:
    anchor: date
    cycle_days: int


@dataclass(frozen=True)
class PayrollBatch:
    name: str
    start: date
    end: date


@dataclass(frozen=True)
class PeriodResolution:
    start: date
    end: date
    verification: str
    notice: str | None
    error: str | None


def _validated_config(anchor_raw: object, cycle_raw: object) -> PayPeriodConfig:
    """Parse an administrator-supplied, safe-to-store payroll schedule."""
    try:
        anchor = date.fromisoformat(str(anchor_raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("Pay-period anchor must be a valid ISO date.") from exc

    if isinstance(cycle_raw, bool):
        raise ValueError("Pay-period cycle must be a whole number of days.")
    try:
        cycle_days = int(str(cycle_raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("Pay-period cycle must be a whole number of days.") from exc
    if str(cycle_raw).strip() != str(cycle_days):
        raise ValueError("Pay-period cycle must be a whole number of days.")
    if not 1 <= cycle_days <= 31:
        raise ValueError("Pay-period cycle must be from 1 through 31 days.")
    return PayPeriodConfig(anchor, cycle_days)


def _default_config() -> PayPeriodConfig:
    return _validated_config(
        DEFAULT_PAY_PERIOD["anchor"], DEFAULT_PAY_PERIOD["cycle_days"]
    )


def current_pay_period_config() -> PayPeriodConfig:
    """Return the saved payroll schedule, or the approved safe default."""
    raw = app_settings.get_setting(PAY_PERIOD_SETTING)
    if not isinstance(raw, dict):
        return _default_config()
    try:
        return _validated_config(raw.get("anchor", ""), raw.get("cycle_days"))
    except ValueError:
        return _default_config()


def save_pay_period_config(anchor_raw: str, cycle_raw: str) -> PayPeriodConfig:
    """Validate and persist a payroll schedule selected in Settings."""
    config = _validated_config(anchor_raw, cycle_raw)
    app_settings.set_setting(
        PAY_PERIOD_SETTING,
        {"anchor": config.anchor.isoformat(), "cycle_days": config.cycle_days},
    )
    return config


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _previous_month_start(value: date) -> date:
    return (value.replace(day=1) - timedelta(days=1)).replace(day=1)


def _custom_bounds(
    start_raw: str | None, end_raw: str | None
) -> tuple[date, date, str | None]:
    if not start_raw:
        return date.min, date.min, "Choose a custom start date."
    if not end_raw:
        return date.min, date.min, "Choose a custom end date."
    try:
        start = date.fromisoformat(start_raw)
        end = date.fromisoformat(end_raw)
    except ValueError:
        return date.min, date.min, "Custom dates must be valid ISO dates."
    if start > end:
        return start, end, "Custom start date must be on or before the end date."
    return start, end, None


def _preset_bounds(
    preset: str,
    start_raw: str | None,
    end_raw: str | None,
    today: date,
    config: PayPeriodConfig,
) -> tuple[date, date, str | None]:
    """Calculate the inclusive bounds for one supported report shortcut."""
    if preset == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6), None
    if preset == "last_week":
        end = today - timedelta(days=today.weekday() + 1)
        return end - timedelta(days=6), end, None
    if preset in _PAY_PERIOD_PRESETS:
        period_index = (today - config.anchor).days // config.cycle_days
        start = config.anchor + timedelta(days=period_index * config.cycle_days)
        if preset == "last_pay_period":
            start -= timedelta(days=config.cycle_days)
        return start, start + timedelta(days=config.cycle_days - 1), None
    if preset == "this_month":
        start = _month_start(today)
        return start, _month_start(start + timedelta(days=32)) - timedelta(days=1), None
    if preset == "last_month":
        start = _previous_month_start(today)
        return start, _month_start(today) - timedelta(days=1), None
    if preset == "custom":
        return _custom_bounds(start_raw, end_raw)
    return date.min, date.min, "Choose a valid date range."


def _as_batch(value: PayrollBatch | Mapping[str, object]) -> PayrollBatch:
    if isinstance(value, PayrollBatch):
        return value
    start = value["start"]
    end = value["end"]
    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)
    if not isinstance(start, date) or not isinstance(end, date):
        raise ValueError("Payroll batch dates must be dates.")
    if start > end:
        raise ValueError("Payroll batch start must not be after its end.")
    return PayrollBatch(str(value.get("name") or "Payroll batch"), start, end)


def _anchor_resolution(start: date, end: date, notice: str, *, verification: str) -> PeriodResolution:
    return PeriodResolution(start, end, verification, notice, None)


def _verify_pay_period_range(
    preset: str,
    start: date,
    end: date,
    load_batches: Callable[[date, date], Sequence[PayrollBatch | Mapping[str, object]]],
) -> PeriodResolution:
    if preset not in _PAY_PERIOD_PRESETS:
        return PeriodResolution(start, end, "not_applicable", None, None)

    try:
        batches = [_as_batch(value) for value in load_batches(start, end)]
    except Exception:
        return _anchor_resolution(
            start,
            end,
            "Odoo could not verify this pay period. Using the configured pay-period anchor.",
            verification="unverified",
        )

    overlaps = {
        (batch.start, batch.end): batch
        for batch in batches
        if batch.start <= end and batch.end >= start
    }
    if not overlaps:
        return _anchor_resolution(
            start,
            end,
            "Odoo has not verified this pay period yet. Using the configured pay-period anchor.",
            verification="anchor",
        )
    non_exact_overlaps = {
        bounds: batch
        for bounds, batch in overlaps.items()
        if bounds != (start, end)
    }
    if len(non_exact_overlaps) >= 2:
        return PeriodResolution(
            start,
            end,
            "conflict",
            None,
            "Multiple overlapping Odoo payroll ranges conflict. Choose a custom range or correct the payroll batches.",
        )
    if (start, end) in overlaps:
        return PeriodResolution(start, end, "odoo_verified", None, None)
    if len(non_exact_overlaps) == 1:
        override = next(iter(non_exact_overlaps.values()))
        return PeriodResolution(
            override.start,
            override.end,
            "odoo_override",
            "Odoo payroll range differs from the configured pay-period anchor; "
            f"using {override.start.isoformat()} through {override.end.isoformat()}.",
            None,
        )
    raise AssertionError("A non-empty set of payroll batches must resolve.")


def resolve_hours_range(
    preset: str,
    start_raw: str | None,
    end_raw: str | None,
    today: date,
    load_batches: Callable[[date, date], Sequence[PayrollBatch | Mapping[str, object]]],
) -> PeriodResolution:
    """Resolve an inclusive report range and verify pay-period shortcuts."""
    config = current_pay_period_config()
    start, end, error = _preset_bounds(preset, start_raw, end_raw, today, config)
    if error:
        return PeriodResolution(start, end, "invalid", None, error)
    return _verify_pay_period_range(preset, start, end, load_batches)


@dataclass(frozen=True)
class HoursRecord:
    day: date
    label: str
    hours: float
    is_open: bool


@dataclass(frozen=True)
class HoursRow:
    name: str
    employee_id: int
    department: str | None
    daily: Sequence[tuple[date, float]]
    regular_hours: float
    overtime_hours: float
    total_hours: float
    open_shift: bool
    conflicting_record: bool
    records: Sequence[HoursRecord]

    @property
    def needs_attention(self) -> bool:
        return self.open_shift or self.conflicting_record


@dataclass(frozen=True)
class HoursReport:
    rows: Sequence[HoursRow]
    team_total_hours: float
    available_departments: Sequence[str]


@dataclass
class _Aggregate:
    name: str
    employee_id: int
    department: str | None
    daily_hours: dict[date, float] = field(default_factory=dict)
    regular_hours: float = 0.0
    overtime_hours: float = 0.0
    open_shift: bool = False
    conflicting_record: bool = False
    records: list[HoursRecord] = field(default_factory=list)

    def add_daily(self, day: date, hours: float) -> None:
        self.daily_hours[day] = self.daily_hours.get(day, 0.0) + hours


def _employee_id(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _active_aggregates(
    roster: Sequence[object], departments: Mapping[int, str | None]
) -> dict[int, _Aggregate]:
    aggregates: dict[int, _Aggregate] = {}
    for person in roster:
        if not getattr(person, "active", False):
            continue
        employee_id = _employee_id(getattr(person, "employee_id", None))
        if employee_id is None or employee_id in aggregates:
            continue
        name = str(getattr(person, "name", "")).strip()
        department = departments.get(employee_id)
        aggregates[employee_id] = _Aggregate(name, employee_id, department)
    return aggregates


def _parse_attendance_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("Attendance timestamp must be an ISO datetime.")
    # Normalized Odoo timestamps are UTC.  Treat a defensive naive value the
    # same way so comparison remains on a single absolute timeline.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _as_plant_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=shift_config.SITE_TZ) if value.tzinfo is None else value.astimezone(shift_config.SITE_TZ)


def _time_label(value: datetime) -> str:
    return value.astimezone(shift_config.SITE_TZ).strftime("%I:%M %p").lstrip("0")


def _clocked_label(start: datetime, end: datetime | None) -> str:
    return f"{_time_label(start)} – {_time_label(end) if end else 'open'}"


def _add_clocked_interval(
    aggregate: _Aggregate,
    start: datetime,
    end: datetime,
    report_start: datetime,
    report_stop: datetime,
    *,
    is_open: bool,
) -> None:
    clipped_start = max(start, report_start)
    clipped_end = min(end, report_stop)
    if clipped_end <= clipped_start:
        return

    cursor = clipped_start
    label = _clocked_label(start, None if is_open else end)
    while cursor < clipped_end:
        local_cursor = cursor.astimezone(shift_config.SITE_TZ)
        next_midnight = datetime.combine(
            local_cursor.date() + timedelta(days=1), time.min,
            tzinfo=shift_config.SITE_TZ,
        )
        segment_end = min(clipped_end, next_midnight)
        elapsed_hours = (
            segment_end.astimezone(UTC) - cursor.astimezone(UTC)
        ).total_seconds() / 3600
        if elapsed_hours > 0:
            day = local_cursor.date()
            aggregate.add_daily(day, elapsed_hours)
            aggregate.records.append(HoursRecord(day, label, elapsed_hours, is_open))
        cursor = segment_end


def _aggregate_clocked(
    aggregates: Mapping[int, _Aggregate],
    start: date,
    end: date,
    now: datetime,
    attendances: Sequence[Mapping[str, object]],
) -> None:
    report_start = datetime.combine(start, time.min, tzinfo=shift_config.SITE_TZ)
    report_stop = datetime.combine(end + timedelta(days=1), time.min, tzinfo=shift_config.SITE_TZ)
    resolved_now = _as_plant_datetime(now)
    for attendance in attendances:
        employee_id = _employee_id(
            attendance.get("employee_odoo_id", attendance.get("employee_id"))
        )
        aggregate = aggregates.get(employee_id) if employee_id is not None else None
        if aggregate is None:
            continue
        try:
            check_in = _parse_attendance_datetime(attendance["check_in"])
        except (KeyError, TypeError, ValueError):
            continue
        check_out_raw = attendance.get("check_out")
        is_open = check_out_raw is None
        try:
            check_out = resolved_now if is_open else _parse_attendance_datetime(check_out_raw)
        except (TypeError, ValueError):
            continue
        if is_open:
            aggregate.open_shift = True
        if attendance.get("conflict"):
            aggregate.conflicting_record = True
        _add_clocked_interval(
            aggregate,
            check_in,
            check_out,
            report_start,
            report_stop,
            is_open=is_open,
        )


def _entry_day(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _entry_hours(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    return hours if math.isfinite(hours) and hours >= 0 else None


def _aggregate_payroll(
    aggregates: Mapping[int, _Aggregate],
    start: date,
    end: date,
    work_entries: Sequence[Mapping[str, object]],
) -> None:
    for entry in work_entries:
        employee_id = _employee_id(entry.get("employee_id"))
        aggregate = aggregates.get(employee_id) if employee_id is not None else None
        if aggregate is None or not entry.get("active", False):
            continue
        day = _entry_day(entry.get("date"))
        code = entry.get("type_code")
        if day is None or not start <= day <= end or code not in {"WORK100", "OVERTIME"}:
            continue
        if entry.get("conflict"):
            aggregate.conflicting_record = True
        if entry.get("numeric_data_valid") is False:
            continue
        entry_hours = _entry_hours(entry.get("duration"))
        if entry_hours is None:
            continue
        is_regular = code == "WORK100"
        aggregate.add_daily(day, entry_hours)
        if is_regular:
            aggregate.regular_hours += entry_hours
        else:
            aggregate.overtime_hours += entry_hours
        aggregate.records.append(
            HoursRecord(day, "Regular" if is_regular else "Overtime", entry_hours, False)
        )


def _hours_row(aggregate: _Aggregate, source: str) -> HoursRow:
    daily = tuple(sorted(aggregate.daily_hours.items()))
    records = tuple(sorted(aggregate.records, key=lambda item: item.day))
    total_hours = aggregate.regular_hours + aggregate.overtime_hours
    if source == "clocked":
        total_hours = sum(hours for _day, hours in daily)
        regular_hours = total_hours
    else:
        regular_hours = aggregate.regular_hours
    return HoursRow(
        aggregate.name,
        aggregate.employee_id,
        aggregate.department,
        daily,
        regular_hours,
        aggregate.overtime_hours,
        total_hours,
        aggregate.open_shift,
        aggregate.conflicting_record,
        records,
    )


def _filter_and_sort_rows(
    rows: Sequence[HoursRow], query: str, department: str, attention: str
) -> HoursReport:
    available_departments = tuple(sorted(
        {row.department for row in rows if row.department}, key=str.casefold
    ))
    needle = query.strip().casefold()
    selected_department = department.strip()

    def selected(row: HoursRow) -> bool:
        if needle and needle not in row.name.casefold():
            return False
        if selected_department and row.department != selected_department:
            return False
        if attention == "approaching_40":
            return 36 <= row.total_hours < 40
        if attention == "over_40":
            return row.total_hours >= 40
        if attention == "attention":
            return row.needs_attention
        return True

    selected_rows = sorted(
        (row for row in rows if selected(row)),
        key=lambda row: (-row.total_hours, row.name.casefold()),
    )
    return HoursReport(
        tuple(selected_rows),
        sum(row.total_hours for row in selected_rows),
        available_departments,
    )


def build_hours_report(
    *,
    source: Literal["clocked", "payroll"],
    roster: Sequence[object],
    start: date,
    end: date,
    now: datetime,
    attendances: Sequence[Mapping[str, object]],
    work_entries: Sequence[Mapping[str, object]],
    departments: Mapping[int, str | None],
    query: str = "",
    department: str = "",
    attention: str = "all",
) -> HoursReport:
    """Build a complete, filtered, source-neutral hours report.

    ``attendances`` and ``work_entries`` are already normalized by the Odoo
    adapter.  The method deliberately performs no I/O and never mutates them.
    """
    if source not in {"clocked", "payroll"}:
        raise ValueError("Unknown hours source.")
    if attention not in {"all", "approaching_40", "over_40", "attention"}:
        raise ValueError("Unknown hours attention filter.")
    if start > end:
        raise ValueError("Report start date must be on or before the end date.")

    aggregates = _active_aggregates(roster, departments)
    if source == "clocked":
        _aggregate_clocked(aggregates, start, end, now, attendances)
    else:
        _aggregate_payroll(aggregates, start, end, work_entries)
    return _filter_and_sort_rows(
        tuple(_hours_row(aggregate, source) for aggregate in aggregates.values()),
        query,
        department,
        attention,
    )
