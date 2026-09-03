"""Local readiness, shadow comparison, and serialized attendance cutover.

Readiness is deliberately local-only: request and CLI reads use the durable
mirror, queue, exception, production, correction, repair, and rollout rows in
one PostgreSQL snapshot.  The shadow warmer is the only path that consults the
production meter client, and it stores aggregate counters rather than a second
person-level attendance truth table.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
import hashlib
import json
import logging
import math
from typing import Literal

from . import (
    app_settings,
    attendance_location_policy,
    attendance_mirror,
    attendance_timeline,
    db,
    saturday_schedule_store,
    schedule_store,
    shift_config,
)


_SHADOW_SETTING = "odoo_attendance_location_shadow_health"
_SHADOW_ERROR_SETTING = "odoo_attendance_location_shadow_error"
_SHADOW_EPOCH_SETTING = "odoo_attendance_location_shadow_epoch"
_BLOCKED_SETTING = "odoo_attendance_location_cutover_blocked"
_CUTOVER_STATUS_SETTING = "odoo_attendance_location_cutover_status"
_ROLLOUT_SETTING = "odoo_attendance_location"
_READINESS_LOCK_ID = 7_210_131_013
_READINESS_CONFIG_TABLES = (
    "global_schedule",
    "saturday_schedule",
    "schedules",
    "company_holidays",
    "saturday_recruitments",
    "work_centers",
    "departments",
    "people",
    "wc_time_attributions",
)

_MIRROR_MAX_AGE = timedelta(seconds=90)
_PROJECTION_MAX_AGE = timedelta(seconds=90)
_SWEEP_MAX_AGE = timedelta(hours=2)
_RECALC_STUCK_AGE = timedelta(minutes=15)
_REPORT_MAX_AGE = timedelta(minutes=5)
_ERROR_LIMIT = 500

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    mirror_age_seconds: float | None
    last_full_sweep_age_seconds: float | None
    open_rows_not_refreshed: int
    last_sweep_deletion_count: int
    projection_lag_seconds: float | None
    recalc_queue_age_seconds: float | None
    recalc_queue_depth: int
    open_conflicts: int
    conflict_minutes_today: float
    open_unmapped: int
    unmapped_minutes_today: float
    open_missing_required: int
    missing_minutes_today: float
    unassigned_units_today: float
    oldest_unassigned_age_seconds: float | None
    shadow_changed_worker_units: float
    failed_corrections: int
    correction_retries_today: int
    correction_verification_failures_today: int
    failed_department_repairs: int
    blockers: tuple[str, ...]
    checked_at: datetime


@dataclass(frozen=True)
class _ReadinessInputs:
    rollout_mode: str
    rollout_valid: bool
    baseline_completed_at: datetime | None
    last_incremental_completed_at: datetime | None
    last_full_sweep_completed_at: datetime | None
    last_sweep_deletion_count: int
    open_rows_not_refreshed: int
    projection_completed_at: datetime | None
    recalc_queue_requested_at: datetime | None
    recalc_queue_depth: int
    open_conflicts: int
    conflict_seconds_today: float
    open_unmapped: int
    unmapped_seconds_today: float
    open_missing_required: int
    missing_seconds_today: float
    unassigned_units_today: float
    oldest_unassigned_at: datetime | None
    shadow_changed_worker_units: float
    failed_corrections: int
    correction_retries_today: int
    correction_verification_failures_today: int
    failed_department_repairs: int
    shadow_day: date | None
    shadow_complete_days: int
    shadow_error: str | None
    source_error: str | None = None
    recalc_queue_invalid: int = 0

    @classmethod
    def unavailable(cls) -> _ReadinessInputs:
        return cls(
            rollout_mode="off",
            rollout_valid=False,
            baseline_completed_at=None,
            last_incremental_completed_at=None,
            last_full_sweep_completed_at=None,
            last_sweep_deletion_count=0,
            open_rows_not_refreshed=0,
            projection_completed_at=None,
            recalc_queue_requested_at=None,
            recalc_queue_depth=0,
            open_conflicts=0,
            conflict_seconds_today=0.0,
            open_unmapped=0,
            unmapped_seconds_today=0.0,
            open_missing_required=0,
            missing_seconds_today=0.0,
            unassigned_units_today=0.0,
            oldest_unassigned_at=None,
            shadow_changed_worker_units=0.0,
            failed_corrections=0,
            correction_retries_today=0,
            correction_verification_failures_today=0,
            failed_department_repairs=0,
            shadow_day=None,
            shadow_complete_days=0,
            shadow_error="local readiness state is unavailable",
            source_error="local readiness snapshot is unavailable",
        )


@dataclass(frozen=True)
class ShadowRefreshResult:
    status: Literal["stored", "skipped", "failed"]
    day: date | None = None
    error: str | None = None


@dataclass(frozen=True)
class _ValidatedShadowHealth:
    day: date
    computed_at: datetime
    config_digest: str
    day_config_digest: str
    mirror_verified_through: datetime
    mirror_full_sweep_completed_at: datetime
    mirror_full_sweep_generation: int
    shadow_epoch_at: datetime
    clean_days: tuple[date, ...]
    clean_completed_at: tuple[tuple[date, datetime], ...]
    day_health_digests: tuple[tuple[date, str], ...]
    unassigned_units: float
    oldest_unassigned_at: datetime | None
    changed_worker_units: float
    stored_error: str | None


@dataclass(frozen=True)
class _ShadowMeteredLocation:
    name: str
    meter_id: str
    skill: str
    bay: str | None


@dataclass(frozen=True)
class _ShadowConfigSnapshot:
    day: date
    digest: str
    day_digest: str
    work_center_names: Mapping[int, str]
    department_requirements: Mapping[str, bool]
    employee_departments: Mapping[int, str | None]
    employee_wage_types: Mapping[int, str | None]
    attribution_rows: tuple[Mapping[str, object], ...]
    metered_locations: tuple[_ShadowMeteredLocation, ...]
    workday: bool
    shift_start_utc: datetime
    shift_end_utc: datetime
    break_windows: tuple[tuple[datetime, datetime], ...]


@dataclass(frozen=True)
class _ShadowRefreshOrigin:
    rollout: attendance_location_policy.RolloutConfig
    shadow_epoch_at: datetime | None
    config: _ShadowConfigSnapshot


@dataclass(frozen=True)
class CutoverActivationResult:
    status: Literal[
        "not_scheduled",
        "not_due",
        "activated",
        "recalculation_pending",
        "active",
        "rolled_back",
        "blocked",
    ]
    cutover_at: datetime | None = None
    blockers: tuple[str, ...] = ()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_utc(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("stored datetime must be timezone-aware")
    return value.astimezone(UTC)


def _age_seconds(now: datetime, value: datetime | None) -> float | None:
    if value is None:
        return None
    return (now - _aware_utc(value, "readiness timestamp")).total_seconds()


def _json_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _canonical_config_value(value: object) -> object:
    """Return a JSON-safe deterministic value for curated configuration only."""
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_config_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_config_value(item) for item in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _config_rows_cur(cur, sql: str, params: tuple = ()) -> list[dict]:
    cur.execute(sql, params or None)
    return [dict(row) for row in cur.fetchall()]


def _break_pairs(raw: object, fallback: Sequence) -> tuple[tuple[time, time], ...]:
    source = _json_value(raw)
    if not isinstance(source, list):
        source = list(fallback)
    pairs = []
    for item in source:
        if isinstance(item, Mapping):
            raw_start = item.get("start")
            raw_end = item.get("end")
        else:
            raw_start = getattr(item, "start", None)
            raw_end = getattr(item, "end", None)

        def strict_time(value: object) -> time | None:
            if isinstance(value, time):
                return value.replace(tzinfo=None)
            if isinstance(value, str):
                try:
                    return time.fromisoformat(value).replace(tzinfo=None)
                except ValueError:
                    return None
            return None

        start = strict_time(raw_start)
        end = strict_time(raw_end)
        if start is None or end is None:
            continue
        if end > start:
            pairs.append((start, end))
    return tuple(pairs)


def _shadow_config_snapshot_cur(cur, day: date) -> _ShadowConfigSnapshot:
    """Read the exact aggregate inputs through one cursor and bind their digest."""
    if type(day) is not date:
        raise TypeError("day must be a date")
    work_centers = _config_rows_cur(
        cur,
        "SELECT name, meter_id, odoo_work_center_id, odoo_work_center_name, "
        "category, cell FROM work_centers ORDER BY lower(name), id",
    )
    departments = _config_rows_cur(
        cur,
        "SELECT name, requires_work_center, requires_work_center_explicit "
        "FROM departments ORDER BY lower(name)",
    )
    people = _config_rows_cur(
        cur,
        "SELECT odoo_id, department_name, wage_type FROM people "
        "WHERE odoo_id IS NOT NULL ORDER BY odoo_id",
    )
    global_rows = _config_rows_cur(
        cur,
        "SELECT shift_start, shift_end, work_weekdays, breaks FROM global_schedule WHERE id = 1",
    )
    saturday_rows = _config_rows_cur(
        cur,
        "SELECT shift_start, shift_end, breaks FROM saturday_schedule WHERE id = 1",
    )
    schedule_rows = _config_rows_cur(
        cur,
        "SELECT day, published, custom_hours FROM schedules WHERE day = %s",
        (day,),
    )
    holiday_rows = _config_rows_cur(
        cur,
        "SELECT odoo_id, date_from, date_to FROM company_holidays "
        "WHERE date_from <= %s AND date_to >= %s ORDER BY odoo_id",
        (day, day),
    )
    recruitment_rows = _config_rows_cur(
        cur,
        "SELECT day, day_kind, holiday_odoo_id, status, shift_start, shift_end "
        "FROM saturday_recruitments WHERE day = %s",
        (day,),
    )
    attribution_rows = _config_rows_cur(
        cur,
        "SELECT id, wc_name, person_name, employee_odoo_id, start_utc, end_utc, "
        "source, breakdown_id FROM wc_time_attributions WHERE day = %s "
        "ORDER BY wc_name, start_utc, id",
        (day,),
    )

    normalized_people = sorted(
        (
            int(row["odoo_id"]),
            attendance_location_policy._normalized_department_name(  # noqa: SLF001
                row.get("department_name")
            ),
            str(row.get("wage_type") or "").strip().casefold() or None,
        )
        for row in people
    )
    payload = {
        "version": 1,
        "work_centers": sorted(
            work_centers,
            key=lambda row: (
                str(row.get("name") or "").casefold(),
                int(row.get("odoo_work_center_id") or 0),
            ),
        ),
        "departments": sorted(
            departments,
            key=lambda row: str(row.get("name") or "").casefold(),
        ),
        # Odoo ID + normalized department and wage type are the only person fallback truth.
        "employee_profiles": normalized_people,
        "global_schedule": global_rows,
        "saturday_schedule": saturday_rows,
    }
    encoded = json.dumps(
        _canonical_config_value(payload),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    day_payload = {
        "version": 1,
        "day": day,
        "schedule": [
            {
                "day": row.get("day"),
                "published": bool(row.get("published")),
                "custom_hours": _json_value(row.get("custom_hours")),
            }
            for row in schedule_rows
        ],
        "holidays": holiday_rows,
        "recruitment": recruitment_rows,
        # These rows stay in memory. Only their canonical digest is persisted.
        "attributions": attribution_rows,
    }
    day_encoded = json.dumps(
        _canonical_config_value(day_payload),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    day_digest = hashlib.sha256(day_encoded.encode("utf-8")).hexdigest()

    global_row = global_rows[0] if global_rows else {}
    saturday_row = saturday_rows[0] if saturday_rows else {}
    schedule_row = schedule_rows[0] if schedule_rows else {}
    recruitment = recruitment_rows[0] if recruitment_rows else None
    global_start = _boundary_time(
        global_row.get("shift_start"), schedule_store.DEFAULT_SCHEDULE.shift_start
    )
    global_end = _boundary_time(
        global_row.get("shift_end"), schedule_store.DEFAULT_SCHEDULE.shift_end
    )
    saturday_start = _boundary_time(
        saturday_row.get("shift_start"), saturday_schedule_store.DEFAULT.shift_start
    )
    saturday_end = _boundary_time(
        saturday_row.get("shift_end"), saturday_schedule_store.DEFAULT.shift_end
    )
    weekdays = {
        int(value)
        for value in (global_row.get("work_weekdays") or ())
        if not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= 6
    } or set(schedule_store.DEFAULT_SCHEDULE.work_weekdays)
    published = bool(schedule_row.get("published"))
    custom = _json_value(schedule_row.get("custom_hours"))
    holiday_id = int(holiday_rows[0]["odoo_id"]) if holiday_rows else None
    if holiday_id is not None:
        workday = bool(
            published
            and recruitment is not None
            and recruitment.get("day_kind") == "holiday"
            and recruitment.get("holiday_odoo_id") is not None
            and int(recruitment["holiday_odoo_id"]) == holiday_id
            and recruitment.get("status") == "published"
        )
    else:
        workday = day.weekday() in weekdays or published
    optional_default = (day.weekday() == 5 or holiday_id is not None) and workday
    shift_start = saturday_start if optional_default else global_start
    shift_end = saturday_end if optional_default else global_end
    fallback_breaks = (
        saturday_schedule_store.DEFAULT.breaks
        if optional_default and not saturday_row
        else (
            schedule_store.DEFAULT_SCHEDULE.breaks
            if not optional_default and not global_row
            else ()
        )
    )
    raw_breaks = saturday_row.get("breaks") if optional_default else global_row.get("breaks")
    if published and isinstance(custom, Mapping):
        shift_start = _boundary_time(custom.get("start"), shift_start)
        shift_end = _boundary_time(custom.get("end"), shift_end)
        if isinstance(custom.get("breaks"), list):
            raw_breaks = custom["breaks"]
            fallback_breaks = ()
    break_pairs = _break_pairs(raw_breaks, fallback_breaks)
    start_utc = datetime.combine(day, shift_start, tzinfo=shift_config.SITE_TZ).astimezone(UTC)
    end_utc = datetime.combine(day, shift_end, tzinfo=shift_config.SITE_TZ).astimezone(UTC)
    if end_utc <= start_utc:
        raise ValueError("shadow configuration has an invalid shift window")
    break_windows = tuple(
        (
            datetime.combine(day, start, tzinfo=shift_config.SITE_TZ).astimezone(UTC),
            datetime.combine(day, end, tzinfo=shift_config.SITE_TZ).astimezone(UTC),
        )
        for start, end in break_pairs
    )
    work_center_names = {
        int(row["odoo_work_center_id"]): str(row["name"])
        for row in work_centers
        if row.get("odoo_work_center_id") is not None and str(row.get("name") or "").strip()
    }
    requirements = {
        attendance_location_policy._normalized_department_name(row.get("name")): bool(  # noqa: SLF001
            row.get("requires_work_center")
        )
        for row in departments
    }
    employee_departments = {
        int(row["odoo_id"]): attendance_location_policy.effective_department_name(
            None, row.get("department_name")
        )
        for row in people
    }
    employee_wage_types = {
        int(row["odoo_id"]): str(row.get("wage_type") or "").strip().casefold() or None
        for row in people
    }
    metered = tuple(
        _ShadowMeteredLocation(
            name=str(row["name"]),
            meter_id=str(row["meter_id"]),
            skill=str(row.get("category") or ""),
            bay=(str(row["cell"]) if row.get("cell") is not None else None),
        )
        for row in work_centers
        if str(row.get("name") or "").strip() and str(row.get("meter_id") or "").strip()
    )
    return _ShadowConfigSnapshot(
        day=day,
        digest=digest,
        day_digest=day_digest,
        work_center_names=work_center_names,
        department_requirements=requirements,
        employee_departments=employee_departments,
        employee_wage_types=employee_wage_types,
        attribution_rows=tuple(attribution_rows),
        metered_locations=metered,
        workday=workday,
        shift_start_utc=start_utc,
        shift_end_utc=end_utc,
        break_windows=break_windows,
    )


def _shadow_config_digest_cur(cur, day: date) -> str:
    return _shadow_config_snapshot_cur(cur, day).digest


def _shadow_config_snapshot(day: date) -> _ShadowConfigSnapshot:
    with db.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return _shadow_config_snapshot_cur(cur, day)


def _shadow_config_digest(day: date) -> str:
    return _shadow_config_snapshot(day).digest


def _project_shadow_snapshot(
    config: _ShadowConfigSnapshot,
    mirror: attendance_mirror.AttendanceMirrorSnapshot,
    now_utc: datetime,
) -> tuple[attendance_timeline.LocationSpan, ...]:
    """Project detached rows using only the configuration bound by ``digest``."""
    verified = mirror.health.last_incremental_completed_at
    if mirror.health.baseline_completed_at is None or verified is None:
        raise RuntimeError("attendance mirror has no verified freshness")
    rows = []
    for raw in mirror.rows:
        row = dict(raw)
        if not str(row.get("odoo_department_name") or "").strip():
            row["odoo_department_name"] = attendance_location_policy.effective_department_name(
                None,
                config.employee_departments.get(int(row["employee_odoo_id"])),
            )
        row["employee_wage_type"] = config.employee_wage_types.get(
            int(row["employee_odoo_id"])
        )
        rows.append(row)

    def requires_work_center(department_name: str | None) -> bool:
        normalized = attendance_location_policy._normalized_department_name(  # noqa: SLF001
            department_name
        )
        if normalized in config.department_requirements:
            return config.department_requirements[normalized]
        return attendance_location_policy.default_department_requires_work_center(department_name)

    spans = attendance_timeline.project_rows(
        rows,
        as_of_utc=now_utc,
        verified_through_utc=min(verified, now_utc),
        map_work_center=config.work_center_names.get,
        requires_work_center=requires_work_center,
        expected_department_id=lambda _work_center: None,
    )
    clipped = [
        replace(
            span,
            start_utc=max(config.shift_start_utc, span.start_utc),
            end_utc=min(config.shift_end_utc, span.end_utc),
        )
        for span in spans
        if min(config.shift_end_utc, span.end_utc) > max(config.shift_start_utc, span.start_utc)
    ]
    return attendance_timeline._merge_adjacent(clipped)  # noqa: SLF001


def _bounded_error(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:_ERROR_LIMIT]


def _plant_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=shift_config.SITE_TZ)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=shift_config.SITE_TZ)
    return start.astimezone(UTC), end.astimezone(UTC)


def _duration_for(spans, status: str) -> float:
    return sum(
        (span.end_utc - span.start_utc).total_seconds() for span in spans if span.status == status
    )


def _open_count(spans, status: str, as_of: datetime, open_ids: set[int]) -> int:
    employees = {
        span.employee_odoo_id
        for span in spans
        if span.status == status
        and span.start_utc < as_of
        and span.end_utc == as_of
        and open_ids.intersection(span.attendance_ids)
    }
    return len(employees)


def _complete_shadow_day_count(
    values: object,
    *,
    rollout_mode: str,
    rollout_updated_at: datetime | None,
    completed_at_by_day: Mapping[date, datetime] | None = None,
) -> int:
    """Count complete days belonging to the current shadow observation epoch."""
    if not isinstance(values, list):
        raise ValueError("complete days must be a list")
    days = {date.fromisoformat(str(value)) for value in values}
    if rollout_mode != "shadow":
        return len(days)
    if rollout_updated_at is None:
        return 0
    epoch = _aware_utc(rollout_updated_at, "rollout updated timestamp")
    if completed_at_by_day is None:
        return 0
    return sum(
        _aware_utc(completed_at_by_day[day], "shadow day completion") >= epoch
        for day in days
        if day in completed_at_by_day
    )


def _timeline_metrics_cur(
    cur,
    *,
    now_utc: datetime,
    verified_through: datetime | None,
) -> tuple[int, float, int, float, int, float]:
    if verified_through is None:
        raise RuntimeError("attendance mirror has no verified freshness")
    local_day = now_utc.astimezone(shift_config.SITE_TZ).date()
    day_start, day_end = _plant_day_bounds(local_day)
    as_of = min(max(now_utc, day_start), day_end)
    if as_of <= day_start:
        return 0, 0.0, 0, 0.0, 0, 0.0

    cur.execute(
        "SELECT odoo_attendance_id, employee_odoo_id, employee_name, "
        "check_in_utc, check_out_utc, odoo_work_center_id, "
        "odoo_work_center_name, odoo_department_id, odoo_department_name, "
        "odoo_write_date FROM odoo_attendance_mirror "
        "WHERE deleted_at IS NULL AND check_in_utc < %s "
        "AND (check_out_utc IS NULL OR check_out_utc > %s) "
        "ORDER BY employee_odoo_id, check_in_utc, odoo_attendance_id",
        (day_end, day_start),
    )
    rows = [dict(row) for row in cur.fetchall()]
    if not rows:
        return 0, 0.0, 0, 0.0, 0, 0.0
    for row in rows:
        for field in ("check_in_utc", "check_out_utc", "odoo_write_date"):
            if row.get(field) is not None:
                row[field] = _optional_utc(row[field])

    employee_ids = sorted({int(row["employee_odoo_id"]) for row in rows})
    home_by_id: dict[int, Mapping[str, object]] = {}
    if employee_ids:
        cur.execute(
            "SELECT odoo_id, department_name, wage_type FROM people WHERE odoo_id = ANY(%s)",
            (employee_ids,),
        )
        home_by_id = {int(row["odoo_id"]): row for row in cur.fetchall()}
    for row in rows:
        profile = home_by_id.get(int(row["employee_odoo_id"]), {})
        row["odoo_department_name"] = attendance_location_policy.effective_department_name(
            row.get("odoo_department_name"), profile.get("department_name")
        )
        row["employee_wage_type"] = profile.get("wage_type") or None

    cur.execute(
        "SELECT odoo_work_center_id, name FROM work_centers "
        "WHERE odoo_work_center_id IS NOT NULL ORDER BY odoo_work_center_id"
    )
    mapping = {int(row["odoo_work_center_id"]): str(row["name"]) for row in cur.fetchall()}
    cur.execute("SELECT name, requires_work_center FROM departments ORDER BY lower(name)")
    requirements = {
        attendance_location_policy._normalized_department_name(row["name"]): bool(
            row["requires_work_center"]
        )
        for row in cur.fetchall()
    }

    def requires(department_name: str | None) -> bool:
        key = attendance_location_policy._normalized_department_name(department_name)
        if key in requirements:
            return requirements[key]
        return attendance_location_policy.default_department_requires_work_center(department_name)

    projected = attendance_timeline.project_rows(
        rows,
        as_of_utc=as_of,
        verified_through_utc=min(_aware_utc(verified_through, "mirror freshness"), as_of),
        map_work_center=mapping.get,
        requires_work_center=requires,
        expected_department_id=lambda _wc: None,
    )
    open_ids = {int(row["odoo_attendance_id"]) for row in rows if row.get("check_out_utc") is None}
    return (
        _open_count(projected, "conflicting_location", as_of, open_ids),
        _duration_for(projected, "conflicting_location"),
        _open_count(projected, "unmapped_location", as_of, open_ids),
        _duration_for(projected, "unmapped_location"),
        _open_count(projected, "missing_required_location", as_of, open_ids),
        _duration_for(projected, "missing_required_location"),
    )


def _correction_health_cur(cur, day_start: datetime, day_end: datetime) -> dict:
    """Return health from the latest immutable intent for each inbox item."""
    cur.execute(
        "WITH latest AS ("
        "SELECT DISTINCT ON (item_key) item_key, status, attempt_count, "
        "verification_failure_count, updated_at FROM attendance_correction_jobs "
        "ORDER BY item_key, created_at DESC, id DESC"
        ") SELECT COUNT(*) FILTER (WHERE status = 'failed') AS failed, "
        "COALESCE(SUM(GREATEST(attempt_count - 1, 0)) "
        "FILTER (WHERE updated_at >= %s AND updated_at < %s), 0) AS retries, "
        "COALESCE(SUM(verification_failure_count) "
        "FILTER (WHERE updated_at >= %s AND updated_at < %s), 0) AS verification_failures "
        "FROM latest",
        (day_start, day_end, day_start, day_end),
    )
    return dict(cur.fetchone() or {})


def _recalculation_health_cur(cur) -> dict:
    """Return queue depth plus impossible state count from the same snapshot."""
    pending = "(completed_at IS NULL OR cache_ready_at IS NULL)"
    invalid = (
        "(attempt_count < 0 "
        "OR (completed_at IS NULL AND "
        "(cache_started_at IS NOT NULL OR cache_ready_at IS NOT NULL)) "
        "OR (completed_at IS NOT NULL AND "
        "(started_at IS NOT NULL OR last_error IS NOT NULL)) "
        "OR (cache_ready_at IS NOT NULL AND "
        "(cache_started_at IS NOT NULL OR completed_at IS NULL "
        "OR cache_ready_at < completed_at)))"
    )
    cur.execute(
        f"SELECT COUNT(*) FILTER (WHERE {pending}) AS depth, "
        f"MIN(requested_at) FILTER (WHERE {pending}) AS oldest, "
        f"COUNT(*) FILTER (WHERE {invalid}) AS invalid "
        "FROM attendance_recalc_queue"
    )
    return dict(cur.fetchone() or {})


def _validate_shadow_health(raw: object, now_utc: datetime) -> _ValidatedShadowHealth:
    """Parse one bounded aggregate and reject every cross-field contradiction."""
    try:
        if not isinstance(raw, Mapping):
            raise ValueError("shadow health must be a mapping")
        now = _aware_utc(now_utc, "now_utc")
        current_day = now.astimezone(shift_config.SITE_TZ).date()
        shadow_day = date.fromisoformat(str(raw["day"]))
        computed_at = _aware_utc(
            datetime.fromisoformat(str(raw["computed_at"])),
            "shadow computed_at",
        )
        config_digest = str(raw["config_digest"])
        day_config_digest = str(raw["day_config_digest"])
        if any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in (config_digest, day_config_digest)
        ):
            raise ValueError("shadow configuration digest is malformed")
        mirror_verified = _aware_utc(
            datetime.fromisoformat(str(raw["mirror_verified_through"])),
            "shadow mirror origin",
        )
        mirror_sweep = _aware_utc(
            datetime.fromisoformat(str(raw["mirror_full_sweep_completed_at"])),
            "shadow full-sweep origin",
        )
        mirror_generation = int(raw["mirror_full_sweep_generation"])
        shadow_epoch = _aware_utc(
            datetime.fromisoformat(str(raw["shadow_epoch_at"])),
            "shadow observation epoch",
        )
        if isinstance(raw["mirror_full_sweep_generation"], bool) or mirror_generation < 0:
            raise ValueError("shadow mirror generation is malformed")
        if (
            shadow_day > current_day
            or computed_at > now
            or mirror_verified > computed_at
            or mirror_sweep > computed_at
            or shadow_epoch > computed_at
            or computed_at.astimezone(shift_config.SITE_TZ).date() != shadow_day
        ):
            raise ValueError("shadow day is incoherent")

        changed_units = float(raw["changed_worker_units"])
        unassigned_units = float(raw["unassigned_units_today"])
        if any(
            not math.isfinite(value) or value < 0 for value in (changed_units, unassigned_units)
        ):
            raise ValueError("shadow metric is invalid")

        raw_oldest = raw["oldest_unassigned_at"]
        oldest = None
        if raw_oldest is not None:
            oldest = _aware_utc(
                datetime.fromisoformat(str(raw_oldest)),
                "oldest unassigned timestamp",
            )
        if (unassigned_units == 0) != (oldest is None):
            raise ValueError("oldest unassigned timestamp is incoherent")
        if oldest is not None and (
            oldest > computed_at or oldest.astimezone(shift_config.SITE_TZ).date() != shadow_day
        ):
            raise ValueError("oldest unassigned timestamp is incoherent")

        raw_complete_days = raw["complete_days"]
        raw_health = raw["complete_day_health"]
        if not isinstance(raw_complete_days, list) or not isinstance(raw_health, list):
            raise ValueError("complete day evidence must be lists")
        if len(raw_complete_days) > 30 or len(raw_health) > 30:
            raise ValueError("complete day evidence is unbounded")
        clean_days = tuple(date.fromisoformat(str(value)) for value in raw_complete_days)
        if len(set(clean_days)) != len(clean_days):
            raise ValueError("complete days must be unique")
        latest_allowed = min(shadow_day, current_day)
        if any(value > latest_allowed for value in clean_days):
            raise ValueError("complete day is in the future")

        evidence_days: set[date] = set()
        evidence_clean_days: set[date] = set()
        evidence_completed: dict[date, datetime] = {}
        evidence_digests: dict[date, str] = {}
        for item in raw_health:
            if not isinstance(item, Mapping):
                raise ValueError("complete day health is malformed")
            health_day = date.fromisoformat(str(item["day"]))
            if health_day in evidence_days or health_day > latest_allowed:
                raise ValueError("complete day health date is invalid")
            evidence_days.add(health_day)
            completed_at = _aware_utc(
                datetime.fromisoformat(str(item["completed_at"])),
                "shadow day completion",
            )
            schedule_digest = str(item["schedule_digest"])
            if (
                len(schedule_digest) != 64
                or any(character not in "0123456789abcdef" for character in schedule_digest)
                or completed_at > computed_at
                or completed_at.astimezone(shift_config.SITE_TZ).date() != health_day
            ):
                raise ValueError("complete day origin is malformed")
            evidence_completed[health_day] = completed_at
            evidence_digests[health_day] = schedule_digest
            workday = item["workday"]
            clean = item["clean"]
            if not isinstance(workday, bool) or not isinstance(clean, bool):
                raise ValueError("complete day health flags are malformed")
            values = tuple(
                float(item[field])
                for field in (
                    "unassigned_units",
                    "conflict_minutes",
                    "unmapped_minutes",
                    "missing_minutes",
                )
            )
            if any(not math.isfinite(value) or value < 0 for value in values):
                raise ValueError("complete day health metric is malformed")
            if clean != (workday and not any(values)):
                raise ValueError("complete day health clean flag is inconsistent")
            if clean:
                evidence_clean_days.add(health_day)
        if set(clean_days) != evidence_clean_days:
            raise ValueError("complete days lack same-day clean evidence")

        stored_error = raw.get("error")
        return _ValidatedShadowHealth(
            day=shadow_day,
            computed_at=computed_at,
            config_digest=config_digest,
            day_config_digest=day_config_digest,
            mirror_verified_through=mirror_verified,
            mirror_full_sweep_completed_at=mirror_sweep,
            mirror_full_sweep_generation=mirror_generation,
            shadow_epoch_at=shadow_epoch,
            clean_days=tuple(sorted(clean_days)),
            clean_completed_at=tuple(
                sorted((day, evidence_completed[day]) for day in evidence_clean_days)
            ),
            day_health_digests=tuple(sorted(evidence_digests.items())),
            unassigned_units=unassigned_units,
            oldest_unassigned_at=oldest,
            changed_worker_units=changed_units,
            stored_error=(str(stored_error)[:_ERROR_LIMIT] if stored_error else None),
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("shadow comparison is malformed") from exc


def _read_inputs_cur(cur, now_utc: datetime) -> _ReadinessInputs:
    """Read every readiness dependency through the caller's one transaction."""
    now = _aware_utc(now_utc, "now_utc")
    cur.execute(
        "SELECT value, updated_at FROM app_settings WHERE key = %s",
        (_ROLLOUT_SETTING,),
    )
    rollout_row = cur.fetchone()
    rollout_raw = _json_value(rollout_row["value"]) if rollout_row else None
    try:
        rollout = attendance_location_policy._parse_config(rollout_raw)  # noqa: SLF001
        rollout_valid = True
    except (TypeError, ValueError):
        rollout = attendance_location_policy.RolloutConfig("off", None, None)
        rollout_valid = False

    cur.execute(
        "SELECT last_incremental_completed_at, last_full_sweep_completed_at, "
        "last_full_sweep_deletion_count, full_sweep_generation, "
        "baseline_completed_at, last_error "
        "FROM odoo_attendance_sync_state WHERE singleton = TRUE"
    )
    sync = cur.fetchone()
    if sync is None:
        sync = {}
    last_incremental = _optional_utc(sync.get("last_incremental_completed_at"))
    last_sweep = _optional_utc(sync.get("last_full_sweep_completed_at"))
    baseline = _optional_utc(sync.get("baseline_completed_at"))

    cur.execute(
        "SELECT COUNT(*) AS count FROM odoo_attendance_mirror "
        "WHERE deleted_at IS NULL AND check_out_utc IS NULL AND last_seen_at < %s",
        (now - _MIRROR_MAX_AGE,),
    )
    open_rows = cur.fetchone() or {}

    recalc = _recalculation_health_cur(cur)

    local_day = now.astimezone(shift_config.SITE_TZ).date()
    day_start, day_end = _plant_day_bounds(local_day)
    corrections = _correction_health_cur(cur, day_start, day_end)
    cur.execute(
        "SELECT COUNT(*) FILTER (WHERE status = 'failed') AS failed "
        "FROM attendance_department_repairs"
    )
    repairs = cur.fetchone() or {}

    cur.execute("SELECT value FROM app_settings WHERE key = %s", (_SHADOW_SETTING,))
    shadow_row = cur.fetchone()
    shadow = _json_value(shadow_row["value"]) if shadow_row else None
    cur.execute("SELECT value FROM app_settings WHERE key = %s", (_SHADOW_ERROR_SETTING,))
    shadow_failure_row = cur.fetchone()
    shadow_failure = _json_value(shadow_failure_row["value"]) if shadow_failure_row else None
    shadow_error = None
    shadow_day = None
    projection_completed_at = None
    unassigned_units = 0.0
    oldest_unassigned = None
    changed_units = 0.0
    complete_days = 0
    if shadow is None:
        shadow_error = "shadow comparison is unavailable"
    else:
        try:
            validated_shadow = _validate_shadow_health(shadow, now)
            current_config = _shadow_config_snapshot_cur(cur, validated_shadow.day)
            current_epoch = _shadow_epoch_cur(cur)
            current_mirror_origin = (
                last_incremental,
                last_sweep,
                int(sync.get("full_sweep_generation") or 0),
            )
            stored_mirror_origin = (
                validated_shadow.mirror_verified_through,
                validated_shadow.mirror_full_sweep_completed_at,
                validated_shadow.mirror_full_sweep_generation,
            )
            if (
                current_epoch is None
                or validated_shadow.shadow_epoch_at != current_epoch
                or validated_shadow.config_digest != current_config.digest
                or validated_shadow.day_config_digest != current_config.day_digest
                or stored_mirror_origin != current_mirror_origin
            ):
                raise ValueError("shadow comparison origin changed")
            health_digests = dict(validated_shadow.day_health_digests)
            current_clean_days = []
            for clean_day in validated_shadow.clean_days:
                clean_config = _shadow_config_snapshot_cur(cur, clean_day)
                if (
                    health_digests.get(clean_day) == clean_config.day_digest
                    and current_epoch <= clean_config.shift_start_utc
                ):
                    current_clean_days.append(clean_day)
            shadow_day = validated_shadow.day
            projection_completed_at = validated_shadow.computed_at
            unassigned_units = validated_shadow.unassigned_units
            oldest_unassigned = validated_shadow.oldest_unassigned_at
            changed_units = validated_shadow.changed_worker_units
            complete_days = _complete_shadow_day_count(
                [value.isoformat() for value in current_clean_days],
                rollout_mode=rollout.mode,
                rollout_updated_at=current_epoch,
                completed_at_by_day=dict(validated_shadow.clean_completed_at),
            )
            shadow_error = validated_shadow.stored_error
        except ValueError:
            shadow_error = "shadow comparison is malformed"
            projection_completed_at = None
    if isinstance(shadow_failure, Mapping):
        try:
            _optional_utc(datetime.fromisoformat(str(shadow_failure["failed_at"])))
            shadow_error = "latest shadow comparison failed"
        except (KeyError, TypeError, ValueError):
            shadow_error = "shadow comparison failure state is malformed"

    try:
        (
            conflicts,
            conflict_seconds,
            unmapped,
            unmapped_seconds,
            missing,
            missing_seconds,
        ) = _timeline_metrics_cur(cur, now_utc=now, verified_through=last_incremental)
    except Exception as exc:  # noqa: BLE001 - projection uncertainty blocks readiness
        conflicts = unmapped = missing = 0
        conflict_seconds = unmapped_seconds = missing_seconds = 0.0
        projection_completed_at = None
        source_error = f"timeline projection unavailable: {_bounded_error(exc)}"
    else:
        source_error = str(sync.get("last_error") or "").strip() or None

    return _ReadinessInputs(
        rollout_mode=rollout.mode,
        rollout_valid=rollout_valid,
        baseline_completed_at=baseline,
        last_incremental_completed_at=last_incremental,
        last_full_sweep_completed_at=last_sweep,
        last_sweep_deletion_count=int(sync.get("last_full_sweep_deletion_count") or 0),
        open_rows_not_refreshed=int(open_rows.get("count") or 0),
        projection_completed_at=projection_completed_at,
        recalc_queue_requested_at=_optional_utc(recalc.get("oldest")),
        recalc_queue_depth=int(recalc.get("depth") or 0),
        open_conflicts=conflicts,
        conflict_seconds_today=conflict_seconds,
        open_unmapped=unmapped,
        unmapped_seconds_today=unmapped_seconds,
        open_missing_required=missing,
        missing_seconds_today=missing_seconds,
        unassigned_units_today=unassigned_units,
        oldest_unassigned_at=oldest_unassigned,
        shadow_changed_worker_units=changed_units,
        failed_corrections=int(corrections.get("failed") or 0),
        correction_retries_today=int(corrections.get("retries") or 0),
        correction_verification_failures_today=int(corrections.get("verification_failures") or 0),
        failed_department_repairs=int(repairs.get("failed") or 0),
        shadow_day=shadow_day,
        shadow_complete_days=complete_days,
        shadow_error=shadow_error,
        source_error=source_error,
        recalc_queue_invalid=int(recalc.get("invalid") or 0),
    )


def _read_inputs(now_utc: datetime) -> _ReadinessInputs:
    with db.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return _read_inputs_cur(cur, now_utc)


def _report_from_inputs(inputs: _ReadinessInputs, now_utc: datetime) -> ReadinessReport:
    now = _aware_utc(now_utc, "now_utc")
    mirror_age = _age_seconds(now, inputs.last_incremental_completed_at)
    sweep_age = _age_seconds(now, inputs.last_full_sweep_completed_at)
    projection_age = _age_seconds(now, inputs.projection_completed_at)
    recalc_age = _age_seconds(now, inputs.recalc_queue_requested_at)
    unassigned_age = _age_seconds(now, inputs.oldest_unassigned_at)
    blockers: set[str] = set()
    if not inputs.rollout_valid:
        blockers.add("rollout_state_unavailable")
    if inputs.rollout_mode not in ("shadow", "live"):
        blockers.add("rollout_not_in_shadow")
    if inputs.baseline_completed_at is None:
        blockers.add("attendance_baseline_incomplete")
    if mirror_age is None:
        blockers.add("attendance_mirror_unavailable")
    elif mirror_age < 0:
        blockers.add("attendance_mirror_invalid")
    elif mirror_age > _MIRROR_MAX_AGE.total_seconds():
        blockers.add("attendance_mirror_stale")
    if sweep_age is None:
        blockers.add("attendance_full_sweep_unavailable")
    elif sweep_age < 0:
        blockers.add("attendance_full_sweep_invalid")
    elif sweep_age > _SWEEP_MAX_AGE.total_seconds():
        blockers.add("attendance_full_sweep_stale")
    if inputs.open_rows_not_refreshed:
        blockers.add("attendance_open_rows_not_refreshed")
    if projection_age is None:
        blockers.add("attendance_projection_unavailable")
    elif projection_age < 0:
        blockers.add("attendance_projection_invalid")
    elif projection_age > _PROJECTION_MAX_AGE.total_seconds():
        blockers.add("attendance_projection_stale")
    if inputs.recalc_queue_depth:
        blockers.add("attendance_recalculation_pending")
        if recalc_age is not None and recalc_age < 0:
            blockers.add("attendance_recalculation_invalid")
        elif recalc_age is None or recalc_age > _RECALC_STUCK_AGE.total_seconds():
            blockers.add("attendance_recalculation_stuck")
    if inputs.recalc_queue_invalid:
        blockers.add("attendance_recalculation_invalid")
    if inputs.failed_corrections:
        blockers.add("attendance_correction_failed")
    if inputs.correction_verification_failures_today:
        blockers.add("attendance_correction_verification_failed")
    if inputs.failed_department_repairs:
        blockers.add("attendance_department_repair_failed")
    if inputs.open_conflicts or inputs.conflict_seconds_today:
        blockers.add("attendance_conflicts_open")
    if inputs.open_unmapped or inputs.unmapped_seconds_today:
        blockers.add("attendance_unmapped_location")
    if inputs.open_missing_required or inputs.missing_seconds_today:
        blockers.add("attendance_required_location_missing")
    if inputs.unassigned_units_today:
        blockers.add("unassigned_production")
    if inputs.shadow_complete_days < 1:
        blockers.add("shadow_comparison_day_incomplete")
    if inputs.shadow_error is not None:
        blockers.add("shadow_comparison_unavailable")
    if inputs.source_error is not None:
        blockers.add("attendance_source_error")
    return ReadinessReport(
        ready=not blockers,
        mirror_age_seconds=mirror_age,
        last_full_sweep_age_seconds=sweep_age,
        open_rows_not_refreshed=inputs.open_rows_not_refreshed,
        last_sweep_deletion_count=inputs.last_sweep_deletion_count,
        projection_lag_seconds=projection_age,
        recalc_queue_age_seconds=recalc_age,
        recalc_queue_depth=inputs.recalc_queue_depth,
        open_conflicts=inputs.open_conflicts,
        conflict_minutes_today=inputs.conflict_seconds_today / 60.0,
        open_unmapped=inputs.open_unmapped,
        unmapped_minutes_today=inputs.unmapped_seconds_today / 60.0,
        open_missing_required=inputs.open_missing_required,
        missing_minutes_today=inputs.missing_seconds_today / 60.0,
        unassigned_units_today=inputs.unassigned_units_today,
        oldest_unassigned_age_seconds=unassigned_age,
        shadow_changed_worker_units=inputs.shadow_changed_worker_units,
        failed_corrections=inputs.failed_corrections,
        correction_retries_today=inputs.correction_retries_today,
        correction_verification_failures_today=(inputs.correction_verification_failures_today),
        failed_department_repairs=inputs.failed_department_repairs,
        blockers=tuple(sorted(blockers)),
        checked_at=now,
    )


def _unavailable_report(now_utc: datetime) -> ReadinessReport:
    return _report_from_inputs(_ReadinessInputs.unavailable(), now_utc)


def build_report(now_utc: datetime) -> ReadinessReport:
    """Return one bounded, read-only, local readiness snapshot."""
    now = _aware_utc(now_utc, "now_utc")
    try:
        inputs = _read_inputs(now)
    except Exception:  # noqa: BLE001 - all unavailable/malformed state fails closed
        _log.warning(
            "attendance readiness local snapshot unavailable",
            extra={"event": "attendance_readiness_unavailable"},
        )
        return _unavailable_report(now)
    return _report_from_inputs(inputs, now)


def _build_report_cur(cur, now_utc: datetime) -> ReadinessReport:
    try:
        return _report_from_inputs(_read_inputs_cur(cur, now_utc), now_utc)
    except Exception:  # noqa: BLE001 - atomic schedule/activation must fail closed
        return _unavailable_report(now_utc)


def _canonical_report(report: ReadinessReport) -> dict:
    payload = asdict(report)
    payload["checked_at"] = report.checked_at.isoformat()
    payload["blockers"] = list(report.blockers)
    return payload


def report_digest(report: ReadinessReport, cutover_at: datetime) -> str:
    cutover = _aware_utc(cutover_at, "cutover_at")
    payload = {"cutover_at": cutover.isoformat(), "report": _canonical_report(report)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def report_json(report: ReadinessReport) -> str:
    return json.dumps(_canonical_report(report), sort_keys=True, separators=(",", ":"))


class _MissingRolloutConfig(ValueError):
    """The locked rollout setting row is absent, not merely unparsable."""


def _lock_rollout_config_cur(cur) -> attendance_location_policy.RolloutConfig:
    cur.execute("SELECT value FROM app_settings WHERE key = %s FOR UPDATE", (_ROLLOUT_SETTING,))
    row = cur.fetchone()
    if row is None:
        raise _MissingRolloutConfig("rollout_state_unavailable")
    raw = _json_value(row["value"])
    try:
        return attendance_location_policy._parse_config(raw)  # noqa: SLF001
    except (TypeError, ValueError) as exc:
        raise ValueError("rollout_state_unavailable") from exc


def _lock_optional_rollout_config_cur(
    cur,
) -> attendance_location_policy.RolloutConfig | None:
    """Return ``None`` only when the locked setting row truly does not exist."""
    try:
        return _lock_rollout_config_cur(cur)
    except _MissingRolloutConfig:
        return None


def _shadow_epoch_cur(cur) -> datetime | None:
    cur.execute("SELECT value FROM app_settings WHERE key = %s", (_SHADOW_EPOCH_SETTING,))
    row = cur.fetchone()
    raw = _json_value(row["value"]) if row is not None else None
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("shadow_epoch_unavailable")
    try:
        return _aware_utc(datetime.fromisoformat(str(raw["entered_at"])), "shadow epoch")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("shadow_epoch_unavailable") from exc


def _set_shadow_epoch_cur(cur, entered_at: datetime) -> None:
    app_settings.set_setting(
        _SHADOW_EPOCH_SETTING,
        {"entered_at": _aware_utc(entered_at, "shadow epoch").isoformat()},
        cur=cur,
    )


def _read_rollout_config_cur(
    cur,
    *,
    for_update: bool = False,
) -> attendance_location_policy.RolloutConfig:
    suffix = " FOR UPDATE" if for_update else ""
    cur.execute(
        f"SELECT value FROM app_settings WHERE key = %s{suffix}",
        (_ROLLOUT_SETTING,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError("rollout_state_unavailable")
    try:
        return attendance_location_policy._parse_config(  # noqa: SLF001
            _json_value(row["value"])
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("rollout_state_unavailable") from exc


def _shadow_refresh_origin(day: date) -> _ShadowRefreshOrigin:
    with db.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        rollout = _read_rollout_config_cur(cur)
        epoch = _shadow_epoch_cur(cur)
        return _ShadowRefreshOrigin(
            rollout=rollout,
            shadow_epoch_at=epoch,
            config=_shadow_config_snapshot_cur(cur, day),
        )


def _lock_readiness_configuration_cur(cur) -> None:
    """Lock readiness configuration in one fixed order to avoid deadlocks."""
    cur.execute(f"LOCK TABLE {', '.join(_READINESS_CONFIG_TABLES)} IN SHARE MODE")


def _boundary_time(value: object, fallback: time) -> time:
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        try:
            return time.fromisoformat(value).replace(tzinfo=None)
        except ValueError:
            pass
    return fallback


def _configured_boundary_cur(cur, day: date) -> tuple[time, bool]:
    """Resolve the exact operational boundary only from rows locked by ``cur``."""
    cur.execute("SELECT shift_start, work_weekdays FROM global_schedule WHERE id = 1")
    global_row = cur.fetchone() or {}
    global_start = _boundary_time(
        global_row.get("shift_start"),
        schedule_store.DEFAULT_SCHEDULE.shift_start,
    )
    raw_weekdays = global_row.get("work_weekdays") or ()
    weekdays = {
        int(value)
        for value in raw_weekdays
        if not isinstance(value, bool) and isinstance(value, int) and 0 <= int(value) <= 6
    }
    if not weekdays:
        weekdays = set(schedule_store.DEFAULT_SCHEDULE.work_weekdays)

    cur.execute("SELECT shift_start FROM saturday_schedule WHERE id = 1")
    saturday_row = cur.fetchone() or {}
    saturday_start = _boundary_time(
        saturday_row.get("shift_start"),
        saturday_schedule_store.DEFAULT.shift_start,
    )

    cur.execute("SELECT published, custom_hours FROM schedules WHERE day = %s", (day,))
    schedule_row = cur.fetchone() or {}
    published = bool(schedule_row.get("published"))
    custom_hours = _json_value(schedule_row.get("custom_hours"))

    cur.execute(
        "SELECT odoo_id FROM company_holidays "
        "WHERE date_from <= %s AND date_to >= %s ORDER BY odoo_id LIMIT 1",
        (day, day),
    )
    holiday_row = cur.fetchone()
    holiday_id = int(holiday_row["odoo_id"]) if holiday_row is not None else None

    cur.execute(
        "SELECT day_kind, holiday_odoo_id, status FROM saturday_recruitments WHERE day = %s",
        (day,),
    )
    recruitment = cur.fetchone()
    if holiday_id is not None:
        workday = bool(
            published
            and recruitment is not None
            and recruitment.get("day_kind") == "holiday"
            and recruitment.get("holiday_odoo_id") is not None
            and int(recruitment["holiday_odoo_id"]) == holiday_id
            and recruitment.get("status") == "published"
        )
    else:
        workday = day.weekday() in weekdays or published

    if published and isinstance(custom_hours, Mapping):
        raw_custom_start = custom_hours.get("start")
        if isinstance(raw_custom_start, str):
            try:
                return time.fromisoformat(raw_custom_start).replace(tzinfo=None), workday
            except ValueError:
                pass
    if (day.weekday() == 5 or holiday_id is not None) and workday:
        return saturday_start, workday
    return global_start, workday


def _validate_configured_boundary_cur(cur, cutover_at: datetime) -> datetime:
    cutover = _aware_utc(cutover_at, "cutover_at")
    local = cutover.astimezone(shift_config.SITE_TZ)
    try:
        boundary, is_workday = _configured_boundary_cur(cur, local.date())
    except Exception as exc:  # noqa: BLE001 - locked schedule uncertainty blocks cutover
        raise ValueError("cutover_workday_unavailable") from exc
    if local.time().replace(tzinfo=None) != boundary:
        raise ValueError("cutover_boundary_required")
    if not is_workday:
        raise ValueError("cutover_workday_required")
    return cutover


def _validate_future_boundary_cur(
    cur,
    cutover_at: datetime,
    now_utc: datetime,
) -> datetime:
    cutover = _aware_utc(cutover_at, "cutover_at")
    now = _aware_utc(now_utc, "now_utc")
    if cutover <= now:
        raise ValueError("cutover_future_boundary_required")
    return _validate_configured_boundary_cur(cur, cutover)


def _validate_configured_boundary(cutover_at: datetime) -> datetime:
    cutover = _aware_utc(cutover_at, "cutover_at")
    local = cutover.astimezone(shift_config.SITE_TZ)
    if local.time().replace(tzinfo=None) != shift_config.shift_start_for(local.date()):
        raise ValueError("cutover_boundary_required")
    try:
        is_workday = shift_config.is_workday(local.date())
    except Exception as exc:  # noqa: BLE001 - schedule uncertainty blocks cutover
        raise ValueError("cutover_workday_unavailable") from exc
    if not is_workday:
        raise ValueError("cutover_workday_required")
    return cutover


def _validate_future_boundary(cutover_at: datetime, now_utc: datetime) -> datetime:
    cutover = _aware_utc(cutover_at, "cutover_at")
    now = _aware_utc(now_utc, "now_utc")
    if cutover <= now:
        raise ValueError("cutover_future_boundary_required")
    return _validate_configured_boundary(cutover)


def schedule_live_cutover(
    cutover_at: datetime,
    *,
    now_utc: datetime | None = None,
    department_requirements: Mapping[str, bool] | None = None,
) -> attendance_location_policy.RolloutConfig:
    """Atomically bind one fresh report to a future live boundary."""
    if not isinstance(cutover_at, datetime) or cutover_at.utcoffset() is None:
        raise ValueError("cutover_timezone_required")
    checked_at = _aware_utc(now_utc or _utc_now(), "now_utc")
    with db.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        _lock_readiness_configuration_cur(cur)
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_READINESS_LOCK_ID,))
        cutover = _validate_future_boundary_cur(cur, cutover_at, checked_at)
        current = _lock_rollout_config_cur(cur)
        if current.mode == "live":
            raise ValueError("live_cutover_already_scheduled")
        if current.mode != "shadow":
            raise ValueError("live_cutover_requires_shadow")
        if department_requirements is not None:
            normalized = {
                str(name).strip(): bool(required)
                for name, required in department_requirements.items()
                if str(name).strip()
            }
            cur.execute("SELECT name, requires_work_center FROM departments ORDER BY lower(name)")
            existing = {
                str(row["name"]): bool(row["requires_work_center"]) for row in cur.fetchall()
            }
            if normalized != existing:
                raise ValueError("department_policy_requires_shadow_refresh")
        report = _build_report_cur(cur, checked_at)
        if not report.ready:
            raise ValueError("live_readiness_blocked")
        report_age = _utc_now() - checked_at
        if report_age < timedelta(0) or report_age > _REPORT_MAX_AGE:
            raise ValueError("live_readiness_stale")
        config = attendance_location_policy.RolloutConfig(
            mode="live",
            cutover_at=cutover,
            live_gate=attendance_location_policy.LiveGate(
                checked_at=checked_at,
                report_digest=report_digest(report, cutover),
                activated_at=None,
            ),
        )
        attendance_location_policy.set_rollout_config(config, cur=cur)
        app_settings.set_setting(
            _CUTOVER_STATUS_SETTING,
            {
                "status": "scheduled",
                "cutover_at": cutover.isoformat(),
                "checked_at": checked_at.isoformat(),
                "report_digest": config.live_gate.report_digest,
            },
            cur=cur,
        )
    return config


def save_non_live_rollout(
    config: attendance_location_policy.RolloutConfig,
    department_requirements: Mapping[str, bool],
    *,
    now_utc: datetime | None = None,
) -> attendance_location_policy.RolloutConfig:
    """Serialize a non-live save with activation and its policy dependencies."""
    if config.mode not in ("off", "shadow"):
        raise ValueError("invalid_rollout_mode")
    now = _aware_utc(now_utc or _utc_now(), "now_utc")
    normalized_departments = {
        str(name).strip(): bool(required)
        for name, required in department_requirements.items()
        if str(name).strip()
    }
    with db.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        _lock_readiness_configuration_cur(cur)
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_READINESS_LOCK_ID,))
        current = _lock_optional_rollout_config_cur(cur) or (
            attendance_location_policy.RolloutConfig("off", None, None)
        )
        resolved = config
        gate = current.live_gate

        if current.mode == "live":
            if current.cutover_at is None or gate is None:
                raise ValueError("rollout_state_unavailable")
            cutover = current.cutover_at.astimezone(UTC)
            if gate.activated_at is None:
                if now >= cutover:
                    raise ValueError("cutover_decision_pending")
                # A super-admin may explicitly cancel a future pending start.
            else:
                activated_at = _aware_utc(gate.activated_at, "activated_at")
                if activated_at > now:
                    raise ValueError("cutover_decision_pending")
                if config.mode == "off" or config.cutover_at is None:
                    raise ValueError("rollback_boundary_required")
                resolved = attendance_location_policy.RolloutConfig(
                    "shadow",
                    config.cutover_at,
                    gate,
                )
        elif current.mode == "shadow" and gate is not None and gate.activated_at is not None:
            if current.cutover_at is None:
                raise ValueError("rollout_state_unavailable")
            if now >= current.cutover_at.astimezone(UTC):
                raise ValueError("cutover_decision_pending")
            if config.mode == "off" or config.cutover_at is None:
                raise ValueError("rollback_boundary_required")
            resolved = attendance_location_policy.RolloutConfig(
                "shadow",
                config.cutover_at,
                gate,
            )

        attendance_location_policy.set_rollout_config(resolved, cur=cur)
        if resolved.mode == "off":
            cur.execute("DELETE FROM app_settings WHERE key = %s", (_SHADOW_EPOCH_SETTING,))
        elif resolved.mode == "shadow" and resolved.live_gate is None:
            if current.mode == "off" or _shadow_epoch_cur(cur) is None:
                _set_shadow_epoch_cur(cur, now)
        for department_name in sorted(normalized_departments, key=str.casefold):
            attendance_location_policy.set_department_requirement(
                department_name,
                normalized_departments[department_name],
                cur=cur,
            )
    return resolved


def _enqueue_cutover_cur(cur, cutover_day: date, now_utc: datetime) -> None:
    cur.execute(
        "INSERT INTO attendance_strict_days (day, reason, source_changed_at) "
        "VALUES (%s, %s, %s) ON CONFLICT (day) DO NOTHING",
        (cutover_day, "attendance_live_cutover", now_utc),
    )
    cur.execute(
        "INSERT INTO attendance_recalc_queue "
        "(day, reason, requested_at, started_at, completed_at, cache_started_at, "
        "cache_ready_at, attempt_count, last_error) "
        "VALUES (%s, %s, %s, NULL, NULL, NULL, NULL, 0, NULL) "
        "ON CONFLICT (day) DO UPDATE SET reason = EXCLUDED.reason, "
        "requested_at = LEAST(attendance_recalc_queue.requested_at, EXCLUDED.requested_at), "
        "started_at = NULL, completed_at = NULL, cache_started_at = NULL, "
        "cache_ready_at = NULL, attempt_count = 0, last_error = NULL",
        (cutover_day, "attendance_live_cutover", now_utc),
    )


def _recalculation_row_valid(row: Mapping[str, object]) -> bool:
    attempt_count = row.get("attempt_count")
    if isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or attempt_count < 0:
        return False
    started_at = row.get("started_at")
    completed_at = row.get("completed_at")
    cache_started_at = row.get("cache_started_at")
    cache_ready_at = row.get("cache_ready_at")
    last_error = row.get("last_error")
    if completed_at is None:
        return cache_started_at is None and cache_ready_at is None
    if started_at is not None or last_error is not None:
        return False
    if cache_ready_at is None:
        return True
    return bool(
        cache_started_at is None
        and isinstance(completed_at, datetime)
        and isinstance(cache_ready_at, datetime)
        and cache_ready_at >= completed_at
    )


def _cutover_queue_state_cur(
    cur,
    cutover_day: date,
    *,
    for_share: bool = False,
) -> Literal["pending", "ready", "invalid"]:
    suffix = " FOR SHARE" if for_share else ""
    cur.execute(
        "SELECT attempt_count, started_at, completed_at, cache_started_at, "
        "cache_ready_at, last_error FROM attendance_recalc_queue WHERE day = %s"
        f"{suffix}",
        (cutover_day,),
    )
    row = cur.fetchone()
    if row is None:
        return "pending"
    if not _recalculation_row_valid(row):
        return "invalid"
    return (
        "ready"
        if row.get("completed_at") is not None and row.get("cache_ready_at") is not None
        else "pending"
    )


def _cutover_queue_ready_cur(cur, cutover_day: date, *, for_share: bool = False) -> bool:
    return _cutover_queue_state_cur(cur, cutover_day, for_share=for_share) == "ready"


def _cutover_status_cur(cur) -> dict | None:
    cur.execute("SELECT value FROM app_settings WHERE key = %s", (_CUTOVER_STATUS_SETTING,))
    row = cur.fetchone()
    raw = _json_value(row["value"]) if row is not None else None
    return dict(raw) if isinstance(raw, Mapping) else None


def _store_blocked_cur(
    cur,
    *,
    cutover_at: datetime,
    report: ReadinessReport,
) -> None:
    app_settings.set_setting(
        _BLOCKED_SETTING,
        {
            "scheduled_at": cutover_at.isoformat(),
            "checked_at": report.checked_at.isoformat(),
            "report_digest": report_digest(report, cutover_at),
            "blockers": list(report.blockers),
        },
        cur=cur,
    )


def _clear_blocked_cur(cur) -> None:
    cur.execute("DELETE FROM app_settings WHERE key = %s", (_BLOCKED_SETTING,))


def activate_due_cutover(now_utc: datetime | None = None) -> CutoverActivationResult:
    """Make one serialized, idempotent boundary decision from local state."""
    now = _aware_utc(now_utc or _utc_now(), "now_utc")
    with db.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        _lock_readiness_configuration_cur(cur)
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_READINESS_LOCK_ID,))
        try:
            config = _lock_rollout_config_cur(cur)
        except ValueError:
            return CutoverActivationResult("not_scheduled")
        gate = config.live_gate
        cutover = config.cutover_at.astimezone(UTC) if config.cutover_at else None

        if (
            config.mode == "shadow"
            and gate is not None
            and gate.activated_at is not None
            and cutover is not None
        ):
            if now < cutover:
                return CutoverActivationResult("not_due", cutover)
            try:
                _validate_configured_boundary_cur(cur, cutover)
            except ValueError as exc:
                boundary_blocker = str(exc)
                report = _build_report_cur(cur, now)
                report = replace(
                    report,
                    ready=False,
                    blockers=tuple(sorted(set(report.blockers) | {boundary_blocker})),
                )
                # The status row is display/warmer state and may be stale or
                # malformed.  The activated gate carried into the scheduled
                # rollback is the durable ownership boundary; never let the
                # optional status row expand or shrink historical strict days.
                original_cutover = gate.activated_at.astimezone(UTC)
                restored = attendance_location_policy.RolloutConfig(
                    "live",
                    original_cutover,
                    attendance_location_policy.LiveGate(
                        checked_at=report.checked_at,
                        report_digest=report_digest(report, original_cutover),
                        activated_at=gate.activated_at,
                    ),
                )
                attendance_location_policy.restore_active_after_rejected_rollback(
                    restored,
                    cur=cur,
                )
                _store_blocked_cur(cur, cutover_at=cutover, report=report)
                app_settings.set_setting(
                    _CUTOVER_STATUS_SETTING,
                    {
                        "status": "rollback_blocked",
                        "cutover_at": cutover.isoformat(),
                        "blockers": list(report.blockers),
                    },
                    cur=cur,
                )
                return CutoverActivationResult("blocked", cutover, report.blockers)
            attendance_location_policy.set_rollout_config(
                attendance_location_policy.RolloutConfig("shadow", None, None),
                cur=cur,
            )
            _set_shadow_epoch_cur(cur, now)
            _clear_blocked_cur(cur)
            app_settings.set_setting(
                _CUTOVER_STATUS_SETTING,
                {"status": "rolled_back", "cutover_at": cutover.isoformat()},
                cur=cur,
            )
            return CutoverActivationResult("rolled_back", cutover)

        if config.mode != "live" or gate is None or cutover is None:
            return CutoverActivationResult("not_scheduled")
        if now < cutover:
            return CutoverActivationResult("not_due", cutover)
        cutover_day = cutover.astimezone(shift_config.SITE_TZ).date()

        if gate.activated_at is not None:
            queue_state = _cutover_queue_state_cur(cur, cutover_day)
            status = "active" if queue_state == "ready" else "recalculation_pending"
            blockers = ("attendance_recalculation_invalid",) if queue_state == "invalid" else ()
            status_value = {"status": status, "cutover_at": cutover.isoformat()}
            if blockers:
                status_value["blockers"] = list(blockers)
            app_settings.set_setting(
                _CUTOVER_STATUS_SETTING,
                status_value,
                cur=cur,
            )
            return CutoverActivationResult(status, cutover, blockers)

        report = _build_report_cur(cur, now)
        try:
            _validate_configured_boundary_cur(cur, cutover)
        except ValueError as exc:
            boundary_blocker = str(exc)
            report = replace(
                report,
                ready=False,
                blockers=tuple(sorted(set(report.blockers) | {boundary_blocker})),
            )
        if not report.ready:
            rolled_back = attendance_location_policy.RolloutConfig("shadow", None, None)
            attendance_location_policy.set_rollout_config(rolled_back, cur=cur)
            _set_shadow_epoch_cur(cur, now)
            _store_blocked_cur(cur, cutover_at=cutover, report=report)
            app_settings.set_setting(
                _CUTOVER_STATUS_SETTING,
                {
                    "status": "blocked",
                    "cutover_at": cutover.isoformat(),
                    "blockers": list(report.blockers),
                },
                cur=cur,
            )
            _log.warning(
                "attendance cutover blocked",
                extra={
                    "event": "attendance_cutover_blocked",
                    "cutover_at": cutover.isoformat(),
                    "blocker_ids": list(report.blockers),
                },
            )
            return CutoverActivationResult("blocked", cutover, report.blockers)

        _enqueue_cutover_cur(cur, cutover_day, now)
        activated = attendance_location_policy.RolloutConfig(
            "live",
            cutover,
            attendance_location_policy.LiveGate(
                checked_at=report.checked_at,
                report_digest=report_digest(report, cutover),
                activated_at=now,
            ),
        )
        attendance_location_policy.set_rollout_config(activated, cur=cur)
        _clear_blocked_cur(cur)
        app_settings.set_setting(
            _CUTOVER_STATUS_SETTING,
            {"status": "recalculation_pending", "cutover_at": cutover.isoformat()},
            cur=cur,
        )
        _log.info(
            "attendance cutover activated for strict recomputation",
            extra={
                "event": "attendance_cutover_activated",
                "cutover_at": cutover.isoformat(),
                "recalculation_day": cutover_day.isoformat(),
            },
        )
        return CutoverActivationResult("activated", cutover)


def _strict_units_by_identity(attribution: Mapping) -> dict[tuple[str, str], float]:
    values: dict[tuple[str, str], float] = {}
    for identity, work_centers in attribution.items():
        if isinstance(identity, tuple):
            employee_key = str(identity[0])
        else:
            employee_key = str(identity)
        for work_center, totals in work_centers.items():
            units = float(totals.get("units") or 0.0)
            if not math.isfinite(units) or units < 0:
                raise ValueError("strict shadow units are invalid")
            values[(employee_key, str(work_center))] = units
    return values


def _shadow_productive_minutes(
    config: _ShadowConfigSnapshot,
    start_utc: datetime,
    end_utc: datetime,
) -> float:
    start = max(config.shift_start_utc, _aware_utc(start_utc, "start_utc"))
    end = min(config.shift_end_utc, _aware_utc(end_utc, "end_utc"))
    if end <= start or not config.workday:
        return 0.0
    seconds = (end - start).total_seconds()
    for break_start, break_end in config.break_windows:
        overlap_start = max(start, break_start)
        overlap_end = min(end, break_end)
        if overlap_end > overlap_start:
            seconds -= (overlap_end - overlap_start).total_seconds()
    return max(0.0, seconds / 60.0)


def _compute_shadow_aggregate(
    day: date,
    now_utc: datetime,
    production_client,
    *,
    config_snapshot: _ShadowConfigSnapshot | None = None,
    mirror_snapshot: attendance_mirror.AttendanceMirrorSnapshot | None = None,
    location_spans: Sequence[attendance_timeline.LocationSpan] | None = None,
) -> dict:
    from . import production_history

    strict_kwargs = {}
    if config_snapshot is not None and mirror_snapshot is not None:
        strict_kwargs = {
            "location_spans": tuple(location_spans or ()),
            "mirror_health": mirror_snapshot.health,
            "shift_bounds": (
                config_snapshot.shift_start_utc,
                config_snapshot.shift_end_utc,
            ),
            "break_windows": config_snapshot.break_windows,
            "metered_locations": config_snapshot.metered_locations,
            "attribution_rows": config_snapshot.attribution_rows,
            "productive_minutes_in_window": (
                lambda _day, start, end: _shadow_productive_minutes(config_snapshot, start, end)
            ),
            "effective_now_utc": min(now_utc, config_snapshot.shift_end_utc),
        }
    inputs = production_history._strict_inputs_for_day(  # noqa: SLF001
        day,
        production_client,
        now_utc=now_utc,
        **strict_kwargs,
    )
    strict = production_history.attribute_for_segments(
        inputs.segments,
        wc_totals=inputs.wc_totals,
        samples_by_wc=inputs.samples_by_wc,
        productive_minutes=lambda _person, _wc_name, start, end: (
            _shadow_productive_minutes(config_snapshot, start, end)
            if config_snapshot is not None
            else shift_config.productive_minutes_in_window(day, start, end)
        ),
        excluded_minutes=inputs.excluded_minutes,
        strict=True,
    )
    strict_units = _strict_units_by_identity(strict)
    rows = db.query(
        "SELECT emp_id, wc_name, units FROM production_daily WHERE day = %s "
        "ORDER BY emp_id, wc_name",
        (day,),
    )
    saved_units = {
        (str(row["emp_id"]), str(row["wc_name"])): float(row.get("units") or 0.0) for row in rows
    }
    changed = 0.0
    for work_center in {key[1] for key in set(strict_units).union(saved_units)}:
        keys = {key for key in set(strict_units).union(saved_units) if key[1] == work_center}
        removed = sum(
            max(0.0, saved_units.get(key, 0.0) - strict_units.get(key, 0.0)) for key in keys
        )
        added = sum(
            max(0.0, strict_units.get(key, 0.0) - saved_units.get(key, 0.0)) for key in keys
        )
        changed += min(removed, added)

    runs = []
    if config_snapshot is not None:
        shift_end = config_snapshot.shift_end_utc
    else:
        _shift_start, shift_end = production_history._strict_shift_bounds(day)  # noqa: SLF001
    for wc_name, samples in inputs.samples_by_wc.items():
        exclusions = [*inputs.break_windows]
        exclusions.extend(inputs.testing_windows.get(wc_name, ()))
        for raw_key, windows in inputs.breakdown_windows.items():
            _employee_id, _person_name, breakdown_wc = (
                production_history._breakdown_window_identity(raw_key)  # noqa: SLF001
            )
            if breakdown_wc != wc_name:
                continue
            exclusions.extend(
                (start, end if end is not None else min(now_utc, shift_end))
                for start, end in windows
            )
        active_intervals = production_history._subtract_intervals(  # noqa: SLF001
            inputs.active_intervals_by_wc.get(wc_name, ()), exclusions
        )
        assigned_times = production_history._assigned_sample_times(  # noqa: SLF001
            samples, inputs.segments, wc_name
        )
        runs.extend(
            production_history.unassigned_runs_for_samples(
                samples,
                assigned_times,
                active_intervals,
                wc_name=wc_name,
            )
        )
    return {
        "changed_worker_units": changed,
        "unassigned_units_today": sum(float(run.units) for run in runs),
        "oldest_unassigned_at": (min((run.start_utc for run in runs), default=None)),
    }


def _shadow_day_health(
    day: date,
    now_utc: datetime,
    *,
    unassigned_units: float,
    config_snapshot: _ShadowConfigSnapshot | None = None,
    location_spans: Sequence[attendance_timeline.LocationSpan] | None = None,
) -> dict:
    """Return aggregate-only cleanliness for this exact production day."""
    workday = bool(
        config_snapshot.workday if config_snapshot is not None else shift_config.is_workday(day)
    )
    conflict_minutes = unmapped_minutes = missing_minutes = 0.0
    if workday:
        shift_start = (
            config_snapshot.shift_start_utc
            if config_snapshot is not None
            else datetime.combine(
                day,
                shift_config.shift_start_for(day),
                tzinfo=shift_config.SITE_TZ,
            ).astimezone(UTC)
        )
        shift_end = (
            config_snapshot.shift_end_utc
            if config_snapshot is not None
            else datetime.combine(
                day,
                shift_config.shift_end_for(day),
                tzinfo=shift_config.SITE_TZ,
            ).astimezone(UTC)
        )
        spans = (
            tuple(location_spans)
            if location_spans is not None
            else attendance_timeline.timeline_for_range(
                shift_start,
                shift_end,
                as_of_utc=min(_aware_utc(now_utc, "now_utc"), shift_end),
            )
        )
        conflict_minutes = _duration_for(spans, "conflicting_location") / 60.0
        unmapped_minutes = _duration_for(spans, "unmapped_location") / 60.0
        missing_minutes = _duration_for(spans, "missing_required_location") / 60.0
    values = {
        "workday": workday,
        "conflict_minutes": conflict_minutes,
        "unmapped_minutes": unmapped_minutes,
        "missing_minutes": missing_minutes,
        "unassigned_units": float(unassigned_units),
    }
    values["clean"] = workday and not any(
        values[field]
        for field in (
            "unassigned_units",
            "conflict_minutes",
            "unmapped_minutes",
            "missing_minutes",
        )
    )
    return values


def _mirror_origin_from_health(
    health: attendance_mirror.MirrorHealth,
    *,
    as_of_utc: datetime | None = None,
) -> tuple[datetime, datetime, int]:
    if health.last_incremental_completed_at is None or health.last_full_sweep_completed_at is None:
        raise ValueError("attendance mirror origin is unavailable")
    generation = int(health.full_sweep_generation)
    if generation < 0:
        raise ValueError("attendance mirror origin is invalid")
    incremental = _aware_utc(health.last_incremental_completed_at, "mirror incremental origin")
    sweep = _aware_utc(health.last_full_sweep_completed_at, "mirror sweep origin")
    if as_of_utc is not None:
        as_of = _aware_utc(as_of_utc, "mirror origin boundary")
        if incremental > as_of or sweep > as_of:
            raise ValueError("attendance mirror origin is in the future")
    return incremental, sweep, generation


def _mirror_origin_cur(cur) -> tuple[datetime, datetime, int]:
    cur.execute(
        "SELECT last_incremental_completed_at, last_full_sweep_completed_at, "
        "full_sweep_generation FROM odoo_attendance_sync_state WHERE singleton = TRUE"
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError("attendance mirror origin is unavailable")
    return (
        _aware_utc(row["last_incremental_completed_at"], "mirror incremental origin"),
        _aware_utc(row["last_full_sweep_completed_at"], "mirror sweep origin"),
        int(row["full_sweep_generation"]),
    )


def _shadow_failure_value(now: datetime, reason: str, error_type: str) -> dict:
    return {
        "failed_at": now.isoformat(),
        "reason": reason,
        "error_type": error_type[:100],
    }


def refresh_shadow_comparison(
    now_utc: datetime | None = None,
    *,
    production_client=None,
) -> ShadowRefreshResult:
    """Compute and store aggregate-only strict-vs-saved shadow health."""
    now = _aware_utc(now_utc or _utc_now(), "now_utc")
    day = now.astimezone(shift_config.SITE_TZ).date()
    try:
        origin = _shadow_refresh_origin(day)
    except Exception as exc:  # noqa: BLE001
        try:
            app_settings.set_setting(
                _SHADOW_ERROR_SETTING,
                _shadow_failure_value(now, "configuration_unavailable", type(exc).__name__),
            )
        except Exception:  # noqa: BLE001 - the source failure remains the primary result
            pass
        return ShadowRefreshResult("failed", error=_bounded_error(exc))
    if origin.rollout.mode not in ("shadow", "live"):
        return ShadowRefreshResult("skipped")
    if origin.shadow_epoch_at is None:
        app_settings.set_setting(
            _SHADOW_ERROR_SETTING,
            _shadow_failure_value(now, "shadow_epoch_unavailable", "ValueError"),
        )
        return ShadowRefreshResult("failed", day=day, error="shadow epoch is unavailable")
    try:
        if production_client is None:
            from .deps import client as production_client
        day_start, _day_end = _plant_day_bounds(day)
        mirror_snapshot = attendance_mirror.snapshot_overlapping(
            day_start,
            origin.config.shift_end_utc,
        )
        mirror_origin = _mirror_origin_from_health(mirror_snapshot.health, as_of_utc=now)
        location_spans = _project_shadow_snapshot(origin.config, mirror_snapshot, now)
        aggregate = _compute_shadow_aggregate(
            day,
            now,
            production_client,
            config_snapshot=origin.config,
            mirror_snapshot=mirror_snapshot,
            location_spans=location_spans,
        )
        day_health = _shadow_day_health(
            day,
            now,
            unassigned_units=aggregate["unassigned_units_today"],
            config_snapshot=origin.config,
            location_spans=location_spans,
        )
        day_health = {
            **day_health,
            "unassigned_units": float(aggregate["unassigned_units_today"]),
        }
    except Exception as exc:  # noqa: BLE001 - preserve the last verified aggregate
        _log.warning(
            "attendance shadow comparison failed",
            extra={"event": "attendance_shadow_failed", "error_type": type(exc).__name__},
        )
        app_settings.set_setting(
            _SHADOW_ERROR_SETTING,
            _shadow_failure_value(now, "production_source_unavailable", type(exc).__name__),
        )
        return ShadowRefreshResult("failed", day=day, error=_bounded_error(exc))

    with db.cursor() as cur:
        # READ COMMITTED is intentional here. Advisory lock SELECTs can create
        # a repeatable-read snapshot before a sync owner commits; after waiting
        # on the common mirror lock this transaction must see that commit. All
        # readiness config tables are SHARE-locked below, and the two advisory
        # locks serialize rollout and mirror writers through the short store.
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        _lock_readiness_configuration_cur(cur)
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_READINESS_LOCK_ID,))
        # Acquire the mirror's common logical-run lock before the first MVCC
        # table read. A sync that started first commits its new generation
        # before this snapshot; a later sync waits until this aggregate store
        # commits, so an earlier advisory-lock query cannot hide generation B.
        attendance_mirror.lock_sync_generation_cur(cur)
        try:
            current_rollout = _read_rollout_config_cur(cur, for_update=True)
            current_epoch = _shadow_epoch_cur(cur)
            current_config = _shadow_config_snapshot_cur(cur, day)
        except Exception:
            app_settings.set_setting(
                _SHADOW_ERROR_SETTING,
                _shadow_failure_value(now, "configuration_changed", "ShadowConfigurationChanged"),
                cur=cur,
            )
            return ShadowRefreshResult("failed", day=day, error="configuration changed")
        if (
            current_rollout not in (origin.rollout,)
            or current_rollout.mode not in ("shadow", "live")
            or current_epoch != origin.shadow_epoch_at
            or current_config.digest != origin.config.digest
            or current_config.day_digest != origin.config.day_digest
        ):
            app_settings.set_setting(
                _SHADOW_ERROR_SETTING,
                _shadow_failure_value(now, "configuration_changed", "ShadowConfigurationChanged"),
                cur=cur,
            )
            return ShadowRefreshResult("failed", day=day, error="configuration changed")
        try:
            current_mirror_origin = _mirror_origin_cur(cur)
        except Exception:
            current_mirror_origin = None
        if current_mirror_origin != mirror_origin:
            app_settings.set_setting(
                _SHADOW_ERROR_SETTING,
                _shadow_failure_value(now, "mirror_changed", "ShadowMirrorChanged"),
                cur=cur,
            )
            return ShadowRefreshResult("failed", day=day, error="attendance mirror changed")

        cur.execute(
            "SELECT value FROM app_settings WHERE key = %s FOR UPDATE",
            (_SHADOW_SETTING,),
        )
        existing_row = cur.fetchone()
        existing = _json_value(existing_row["value"]) if existing_row is not None else None
        complete_health: dict[date, dict] = {}
        if isinstance(existing, Mapping):
            try:
                validated_existing = _validate_shadow_health(existing, now)
            except ValueError:
                validated_existing = None
            if (
                validated_existing is not None
                and validated_existing.config_digest == current_config.digest
                and validated_existing.shadow_epoch_at == current_epoch
            ):
                for raw_health in existing.get("complete_day_health") or []:
                    if not isinstance(raw_health, Mapping):
                        continue
                    health_day = date.fromisoformat(str(raw_health["day"]))
                    day_config = _shadow_config_snapshot_cur(cur, health_day)
                    if (
                        raw_health.get("schedule_digest") == day_config.day_digest
                        and current_epoch is not None
                        and current_epoch <= day_config.shift_start_utc
                    ):
                        complete_health[health_day] = dict(raw_health)

        if (
            now >= current_config.shift_end_utc
            and current_epoch is not None
            and current_epoch <= current_config.shift_start_utc
        ):
            complete_health[day] = {
                "day": day.isoformat(),
                "completed_at": now.isoformat(),
                "schedule_digest": current_config.day_digest,
                **day_health,
            }
        retained_days = sorted(complete_health)[-30:]
        complete_health = {value: complete_health[value] for value in retained_days}
        complete_days = {
            value for value, health in complete_health.items() if health.get("clean") is True
        }
        oldest = aggregate["oldest_unassigned_at"]
        assert current_epoch is not None
        value = {
            "day": day.isoformat(),
            "computed_at": now.isoformat(),
            "config_digest": current_config.digest,
            "day_config_digest": current_config.day_digest,
            "mirror_verified_through": mirror_origin[0].isoformat(),
            "mirror_full_sweep_completed_at": mirror_origin[1].isoformat(),
            "mirror_full_sweep_generation": mirror_origin[2],
            "shadow_epoch_at": current_epoch.isoformat(),
            "complete_days": [value.isoformat() for value in sorted(complete_days)],
            "complete_day_health": [complete_health[value] for value in sorted(complete_health)],
            "changed_worker_units": aggregate["changed_worker_units"],
            "unassigned_units_today": aggregate["unassigned_units_today"],
            "oldest_unassigned_at": oldest.isoformat() if oldest else None,
        }
        app_settings.set_setting(_SHADOW_SETTING, value, cur=cur)
        cur.execute("DELETE FROM app_settings WHERE key = %s", (_SHADOW_ERROR_SETTING,))
    return ShadowRefreshResult("stored", day=day)


def blocked_cutover_snapshot() -> dict | None:
    """Return one validated, privacy-safe blocked-cutover aggregate."""
    raw = app_settings.get_setting(_BLOCKED_SETTING)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("blocked cutover state is malformed")
    try:
        scheduled = datetime.fromisoformat(str(raw["scheduled_at"]))
        checked = datetime.fromisoformat(str(raw["checked_at"]))
        scheduled = _aware_utc(scheduled, "scheduled_at")
        checked = _aware_utc(checked, "checked_at")
        blockers = raw.get("blockers")
        if not isinstance(blockers, Sequence) or isinstance(blockers, (str, bytes)):
            raise ValueError("blockers must be a sequence")
        normalized = tuple(sorted({str(value)[:100] for value in blockers if str(value)}))
        if not normalized:
            raise ValueError("blocked cutover must include blocker ids")
        return {
            "scheduled_at": scheduled,
            "checked_at": checked,
            "report_digest": str(raw.get("report_digest") or "")[:64],
            "blockers": normalized,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("blocked cutover state is malformed") from exc


def cutover_status_snapshot() -> dict | None:
    try:
        raw = app_settings.get_setting(_CUTOVER_STATUS_SETTING)
    except Exception:  # noqa: BLE001 - Settings must remain visibly fail-closed
        return {"status": "unavailable"}
    return dict(raw) if isinstance(raw, Mapping) else None


def _ordinary_refresh_ready_cur(day: date, cur, *, lock_queue: bool = False) -> bool:
    """Read the ordinary-writer fence through one caller-owned transaction."""
    cur.execute(
        "SELECT value FROM app_settings WHERE key = %s",
        (_ROLLOUT_SETTING,),
    )
    row = cur.fetchone()
    if row is None:
        # A genuinely unconfigured installation starts in Off. A present but
        # malformed row is parsed below and therefore remains fail-closed.
        config = attendance_location_policy.RolloutConfig("off", None, None)
    else:
        config = attendance_location_policy._parse_config(  # noqa: SLF001
            _json_value(row["value"])
        )

    if config.cutover_at is None:
        return True
    cutover_day = config.cutover_at.astimezone(shift_config.SITE_TZ).date()
    if day < cutover_day:
        return True

    gate = config.live_gate
    if config.mode == "shadow":
        # A scheduled rollback remains operationally live until its serialized
        # boundary decision turns it into plain Shadow.
        return gate is None
    if config.mode != "live" or gate is None or gate.activated_at is None:
        # Pending Live must not let an ordinary writer race activation or
        # enqueue a spurious source-failure recalculation.
        return False

    raw = _cutover_status_cur(cur)
    if not isinstance(raw, Mapping) or raw.get("status") != "active":
        return False
    status_cutover = _aware_utc(
        datetime.fromisoformat(str(raw["cutover_at"])),
        "cutover status timestamp",
    )
    if status_cutover != config.cutover_at.astimezone(UTC):
        return False
    # At store time FOR SHARE holds the exact terminal row through the
    # production write. A sync requeue either commits before this read and
    # closes the fence, or waits until the already-ready write commits.
    return _cutover_queue_ready_cur(cur, cutover_day, for_share=lock_queue)


def ordinary_refresh_ready(day: date) -> bool:
    """Fence non-worker writes until the activated cutover rebuild is visible."""
    try:
        with db.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return _ordinary_refresh_ready_cur(day, cur)
    except RuntimeError as exc:
        # Preserve repository unit-only legacy callers without a configured DB.
        if "DATABASE_URL is not set" in str(exc):
            return True
        return False
    except Exception:  # noqa: BLE001 - local rollout uncertainty fails closed
        return False


def tick(now_utc: datetime | None = None) -> CutoverActivationResult:
    """One bounded warmer tick: compare in shadow, then decide any due gate."""
    now = _aware_utc(now_utc or _utc_now(), "now_utc")
    refresh_shadow_comparison(now)
    return activate_due_cutover(now)


__all__ = [
    "CutoverActivationResult",
    "ReadinessReport",
    "ShadowRefreshResult",
    "activate_due_cutover",
    "blocked_cutover_snapshot",
    "build_report",
    "cutover_status_snapshot",
    "ordinary_refresh_ready",
    "refresh_shadow_comparison",
    "report_digest",
    "report_json",
    "save_non_live_rollout",
    "schedule_live_cutover",
    "tick",
]
