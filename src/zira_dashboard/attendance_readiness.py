"""Read-only health checks and atomic rollout for Odoo attendance locations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
import logging
import math
from typing import Literal

from . import (
    app_settings,
    attendance_exceptions,
    attendance_location_policy,
    attendance_mirror,
    db,
    inbox_keys,
    production_history,
    shift_config,
)


_MIRROR_MAX_AGE = 90.0
_FULL_SWEEP_MAX_AGE = 2 * 60 * 60.0
_RECALC_MAX_AGE = 15 * 60.0
_LIVE_GATE_MAX_AGE = timedelta(minutes=5)
_ACTIVATION_BOUNDARY_WINDOW = timedelta(seconds=90)
_ROLLOUT_SETTING_KEY = "odoo_attendance_location"
_SHADOW_SETTING_KEY = "odoo_attendance_shadow_health"
_SHADOW_EPOCH_SETTING_KEY = "odoo_attendance_shadow_epoch"
_CUTOVER_BLOCKED_SETTING_KEY = "odoo_attendance_cutover_blocked"
_ACTIVATION_ADVISORY_LOCK_KEY = 0x5A4952414355544F
_SHADOW_ADVISORY_LOCK_KEY = 0x5A49524153484457
_READINESS_REPORT_SETTING_KEY = "odoo_attendance_readiness_report"
_ROLLOUT_AUDIT_EVENTS = {
    "shadow_started",
    "off",
    "live_scheduled",
    "live_cancelled",
    "live_activated",
    "live_blocked",
    "rollback_scheduled",
    "rolled_back",
}

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


@dataclass(frozen=True)
class _ReadinessInputs:
    baseline_complete: bool
    mirror_error: str | None
    projection_complete: bool
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
    unmapped_affects_production: bool
    missing_affects_production: bool
    comparison_identity_available: bool = True
    shadow_day_complete: bool = True
    frozen_production_fingerprint: str = ""


@dataclass(frozen=True)
class _IssueMetrics:
    projection_complete: bool
    open_conflicts: int
    conflict_minutes_today: float
    open_unmapped: int
    unmapped_minutes_today: float
    open_missing_required: int
    missing_minutes_today: float
    unassigned_units_today: float
    oldest_unassigned_age_seconds: float | None
    unmapped_affects_production: bool
    missing_affects_production: bool
    comparison_identity_available: bool = True


@dataclass(frozen=True)
class ShadowComparison:
    day: date
    checked_at: datetime
    complete: bool
    changed_worker_units: float
    comparison_keys: int
    strict_worker_units: float
    current_worker_units: float
    error: str | None
    source_mirror_at: datetime | None = None


@dataclass(frozen=True)
class DecisionSnapshot:
    """Private proof binding one public report to the local rows it checked."""

    report: ReadinessReport
    report_digest: str
    local_source_fingerprint: str
    production_fingerprint: str
    frozen_production_fingerprint: str = ""
    source_binding_digest: str = ""
    checked_at: datetime | None = None
    valid_until: datetime | None = None


class DecisionSourceChanged(RuntimeError):
    """The source changed while a readiness report was being assembled."""


_FrozenProductionDay = production_history.StrictSourceSnapshot


_SOURCE_FINGERPRINT_SQL = """
WITH sync AS (
  SELECT baseline_completed_at IS NOT NULL AS baseline_complete,
         last_error AS mirror_error,
         COALESCE(last_incremental_completed_at, baseline_completed_at)
           AS mirror_completed_at,
         COALESCE(last_incremental_observed_at, baseline_completed_at)
           AS mirror_observed_at,
         last_full_sweep_completed_at AS full_sweep_completed_at,
         last_full_sweep_deletion_count
  FROM odoo_attendance_sync_state
  WHERE singleton = TRUE
), open_refresh AS (
  SELECT COUNT(*) FILTER (
           WHERE m.deleted_at IS NULL AND m.check_out_utc IS NULL
             AND (sync.mirror_observed_at IS NULL
                  OR m.last_seen_at < sync.mirror_observed_at)
         ) AS open_rows_not_refreshed
  FROM sync LEFT JOIN odoo_attendance_mirror m ON TRUE
), recalc AS (
  SELECT md5(COALESCE((jsonb_agg(jsonb_build_array(
      day, requested_at, completed_at, cache_ready_at
    ) ORDER BY day) FILTER (
      WHERE completed_at IS NULL OR cache_ready_at IS NULL
    ))::text, '[]')) AS fp
  FROM attendance_recalc_queue
), latest_corrections AS (
  SELECT DISTINCT ON (item_key) id, item_key, status
  FROM attendance_correction_jobs
  ORDER BY item_key, created_at DESC, id DESC
), corrections AS (
  SELECT md5(COALESCE((jsonb_agg(jsonb_build_array(id, item_key, status)
      ORDER BY id) FILTER (WHERE status = 'failed'))::text, '[]')) AS fp
  FROM latest_corrections
), correction_events AS (
  SELECT md5(COALESCE(jsonb_agg(jsonb_build_array(
      id, correction_job_id, phase, result,
      COALESCE((detail->>'attempt_count')::int, 1), created_at
    ) ORDER BY id)::text, '[]')) AS fp
  FROM attendance_correction_job_events
  WHERE created_at >= %s AND created_at < %s
), repairs AS (
  SELECT md5(COALESCE(jsonb_agg(jsonb_build_array(
      odoo_attendance_id, status
    ) ORDER BY odoo_attendance_id)::text, '[]')) AS fp
  FROM attendance_department_repairs WHERE status = 'failed'
), shadow_proof AS (
  SELECT md5(COALESCE(jsonb_agg(jsonb_build_array(key, value)
      ORDER BY key)::text, '[]')) AS fp
  FROM app_settings
  WHERE key IN ('odoo_attendance_shadow_health', 'odoo_attendance_shadow_epoch')
), production AS (
  SELECT md5(COALESCE(jsonb_agg(jsonb_build_array(
      day, COALESCE(a.canonical_emp_id, p.emp_id), p.wc_name, p.units,
      p.downtime, p.hours, p.days_worked
    ) ORDER BY COALESCE(a.canonical_emp_id, p.emp_id), p.wc_name)::text, '[]')) AS fp
  FROM production_daily p
  LEFT JOIN production_identity_aliases a ON a.legacy_emp_id = p.emp_id
  WHERE p.day = %s
)
SELECT md5(concat_ws('|',
         jsonb_build_array(
           sync.baseline_complete, sync.mirror_error,
           sync.mirror_completed_at, sync.mirror_observed_at,
           sync.full_sweep_completed_at, sync.last_full_sweep_deletion_count,
           open_refresh.open_rows_not_refreshed
         )::text,
         recalc.fp, corrections.fp, correction_events.fp, repairs.fp,
         shadow_proof.fp))
         AS local_source_fingerprint,
       production.fp AS production_fingerprint
FROM sync CROSS JOIN open_refresh CROSS JOIN recalc CROSS JOIN corrections
CROSS JOIN correction_events CROSS JOIN repairs CROSS JOIN shadow_proof
CROSS JOIN production
"""

_SHADOW_SOURCE_FINGERPRINT_SQL = """
WITH mirror AS (
  SELECT md5(COALESCE(jsonb_agg(jsonb_build_array(
      odoo_attendance_id, employee_odoo_id, employee_name,
      check_in_utc, check_out_utc,
      odoo_work_center_id, odoo_department_id, odoo_department_name,
      odoo_write_date, deleted_at
    ) ORDER BY odoo_attendance_id)::text, '[]')) AS fp
  FROM odoo_attendance_mirror
  WHERE check_in_utc < %s AND COALESCE(check_out_utc, 'infinity'::timestamptz) > %s
), mappings AS (
  SELECT md5(COALESCE(jsonb_agg(jsonb_build_array(
      name, odoo_work_center_id, odoo_work_center_name, meter_id,
      category, cell, department
    ) ORDER BY name)::text, '[]')) AS fp
  FROM work_centers
), policies AS (
  SELECT md5(COALESCE(jsonb_agg(jsonb_build_array(
      name, requires_work_center, requires_work_center_explicit
    ) ORDER BY name)::text, '[]')) AS fp
  FROM departments
), people_home AS (
  SELECT md5(COALESCE(jsonb_agg(jsonb_build_array(
      odoo_id, department_name, active
    ) ORDER BY odoo_id)::text, '[]')) AS fp
  FROM people WHERE odoo_id IS NOT NULL
), auxiliary AS (
  SELECT md5(concat_ws('|',
    (SELECT COALESCE(jsonb_agg(to_jsonb(s) ORDER BY day)::text, '[]')
       FROM schedules s WHERE day = %s),
    (SELECT COALESCE(jsonb_agg(jsonb_build_array(
       id, wc_name, start_utc, end_utc, source, employee_odoo_id,
       person_name, breakdown_id
     ) ORDER BY id)::text, '[]') FROM wc_time_attributions WHERE day = %s),
    (SELECT COALESCE(jsonb_agg(jsonb_build_array(
       id, wc_name, detected_stop_utc, resolved_at, resolution, resume_utc
     ) ORDER BY id)::text, '[]') FROM machine_breakdowns WHERE day = %s)
  )) AS fp
), calendar AS (
  SELECT md5(concat_ws('|',
    (SELECT COALESCE(jsonb_agg(to_jsonb(g) ORDER BY id)::text, '[]')
       FROM global_schedule g),
    (SELECT COALESCE(jsonb_agg(to_jsonb(s) ORDER BY id)::text, '[]')
       FROM saturday_schedule s),
    (SELECT COALESCE(jsonb_agg(to_jsonb(h) ORDER BY odoo_id)::text, '[]')
       FROM company_holidays h),
    (SELECT COALESCE(jsonb_agg(to_jsonb(r) ORDER BY day)::text, '[]')
       FROM saturday_recruitments r)
  )) AS fp
), production AS (
  SELECT md5(COALESCE(jsonb_agg(jsonb_build_array(
      COALESCE(a.canonical_emp_id, p.emp_id), p.wc_name, p.units,
      p.downtime, p.hours, p.days_worked, p.computed_at
    ) ORDER BY COALESCE(a.canonical_emp_id, p.emp_id), p.wc_name)::text, '[]')) AS fp
  FROM production_daily p
  LEFT JOIN production_identity_aliases a ON a.legacy_emp_id = p.emp_id
  WHERE p.day = %s
)
SELECT md5(concat_ws('|', mirror.fp, mappings.fp, policies.fp,
                     people_home.fp, auxiliary.fp, calendar.fp, production.fp))
       AS shadow_source_fingerprint
FROM mirror CROSS JOIN mappings CROSS JOIN policies CROSS JOIN people_home
CROSS JOIN auxiliary CROSS JOIN calendar CROSS JOIN production
"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _seconds_since(now_utc: datetime, value: datetime | None) -> float | None:
    if value is None:
        return None
    return max(0.0, (now_utc - _aware_utc(value, "snapshot timestamp")).total_seconds())


def _record_rollout_audit_cur(
    cur,
    *,
    event_kind: str,
    rollout_mode: str,
    checked_at: datetime,
    cutover_at: datetime | None = None,
    report_fingerprint: str | None = None,
    blocker_codes=(),
) -> None:
    if event_kind not in _ROLLOUT_AUDIT_EVENTS:
        raise ValueError("invalid rollout audit event")
    if rollout_mode not in {"off", "shadow", "live"}:
        raise ValueError("invalid rollout audit mode")
    blockers = tuple(str(value) for value in blocker_codes)
    if len(blockers) > 32 or any(not value or len(value) > 100 for value in blockers):
        raise ValueError("invalid rollout audit blockers")
    cur.execute(
        "INSERT INTO attendance_rollout_audit "
        "(event_kind, rollout_mode, cutover_at, checked_at, report_digest, blocker_codes) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
        (
            event_kind,
            rollout_mode,
            cutover_at,
            _aware_utc(checked_at, "checked_at"),
            report_fingerprint,
            json.dumps(blockers),
        ),
    )


def _bounded_identifiers(values, *, limit: int = 100) -> tuple:
    return tuple(sorted(set(values), key=str)[:limit])


def _log_readiness_identifiers(
    segments,
    db_metrics: dict,
    *,
    now_utc: datetime,
) -> None:
    """Emit only bounded source identifiers needed to investigate blockers."""
    relevant = [
        segment
        for segment in segments
        if getattr(segment, "status", None)
        in {"conflicting_location", "unmapped_location", "missing_required_location"}
    ]
    attendance_ids = _bounded_identifiers(
        value
        for segment in relevant
        for value in getattr(segment, "attendance_ids", ())
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    )
    employee_ids = _bounded_identifiers(
        value
        for segment in relevant
        if isinstance((value := getattr(segment, "employee_odoo_id", None)), int)
        and not isinstance(value, bool)
        and value > 0
    )
    work_center_ids = _bounded_identifiers(
        value
        for segment in relevant
        if isinstance((value := getattr(segment, "odoo_work_center_id", None)), int)
        and not isinstance(value, bool)
        and value > 0
    )
    actual_issues = attendance_exceptions._timeline_issues(  # noqa: SLF001
        tuple(segments),
        raw_by_id={},
        now_utc=_aware_utc(now_utc, "now_utc"),
    )
    exception_ids = _bounded_identifiers(
        issue.item_key
        for issue in actual_issues
        if issue.kind
        in {
            "attendance_conflicting_location",
            "attendance_unmapped_location",
            "attendance_missing_location",
        }
    )
    evidence = {
        "attendance_ids": attendance_ids,
        "employee_ids": employee_ids,
        "work_center_ids": work_center_ids,
        "exception_ids": exception_ids,
        "correction_ids": _bounded_identifiers(
            db_metrics.get("correction_job_ids", ())
        ),
        "repair_ids": _bounded_identifiers(
            db_metrics.get("repair_attendance_ids", ())
        ),
        "recalculation_ids": _bounded_identifiers(
            db_metrics.get("recalculation_ids", ())
        ),
    }
    if any(evidence.values()):
        _log.info("attendance readiness identifier evidence", extra=evidence)


def _report_blockers(
    inputs: _ReadinessInputs,
    *,
    cutover_at: datetime | None,
    now_utc: datetime,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if not inputs.baseline_complete:
        blockers.append("baseline_incomplete")
    if inputs.mirror_age_seconds is None or inputs.mirror_age_seconds > _MIRROR_MAX_AGE:
        blockers.append("mirror_stale")
    if inputs.mirror_error:
        blockers.append("mirror_sync_failed")
    if inputs.open_rows_not_refreshed:
        blockers.append("open_rows_not_refreshed")
    if (
        inputs.last_full_sweep_age_seconds is None
        or inputs.last_full_sweep_age_seconds > _FULL_SWEEP_MAX_AGE
    ):
        blockers.append("full_sweep_stale")
    if not inputs.projection_complete:
        blockers.append("projection_incomplete")
    if inputs.recalc_queue_depth and (
        inputs.recalc_queue_age_seconds is None
        or inputs.recalc_queue_age_seconds > _RECALC_MAX_AGE
    ):
        blockers.append("recalculation_stuck")
    if inputs.open_conflicts:
        blockers.append("unresolved_conflicts")
    if inputs.failed_corrections:
        blockers.append("failed_corrections")
    if inputs.failed_department_repairs:
        blockers.append("failed_department_repairs")
    if not inputs.shadow_day_complete:
        blockers.append("shadow_day_incomplete")
    if not inputs.comparison_identity_available:
        blockers.append("comparison_identity_unavailable")
    if inputs.unassigned_units_today > 0:
        blockers.append("unassigned_production")
    if inputs.unmapped_affects_production:
        blockers.append("unmapped_location_affects_production")
    if inputs.missing_affects_production:
        blockers.append("missing_location_affects_production")
    if cutover_at is not None:
        try:
            validate_cutover(cutover_at, now_utc=now_utc)
        except ValueError as exc:
            blockers.append(str(exc))
    return tuple(blockers)


def _failed_readiness_inputs() -> _ReadinessInputs:
    return _ReadinessInputs(
        baseline_complete=False,
        mirror_error="source_unavailable",
        projection_complete=False,
        mirror_age_seconds=None,
        last_full_sweep_age_seconds=None,
        open_rows_not_refreshed=0,
        last_sweep_deletion_count=0,
        projection_lag_seconds=None,
        recalc_queue_age_seconds=None,
        recalc_queue_depth=0,
        open_conflicts=0,
        conflict_minutes_today=0.0,
        open_unmapped=0,
        unmapped_minutes_today=0.0,
        open_missing_required=0,
        missing_minutes_today=0.0,
        unassigned_units_today=0.0,
        oldest_unassigned_age_seconds=None,
        shadow_changed_worker_units=0.0,
        failed_corrections=0,
        correction_retries_today=0,
        correction_verification_failures_today=0,
        failed_department_repairs=0,
        unmapped_affects_production=False,
        missing_affects_production=False,
        comparison_identity_available=False,
        shadow_day_complete=False,
    )


def _collect_or_failed(
    now: datetime,
    production_client,
    *,
    frozen_leaderboard_rows=None,
    frozen_shadow_day: date | None = None,
    frozen_shadow_fingerprint: str | None = None,
    frozen_production_day: _FrozenProductionDay | None = None,
    frozen_shadow_production_day: _FrozenProductionDay | None = None,
) -> _ReadinessInputs:
    try:
        kwargs = {}
        if frozen_leaderboard_rows is not None:
            kwargs["frozen_leaderboard_rows"] = frozen_leaderboard_rows
        if frozen_shadow_day is not None:
            kwargs["frozen_shadow_day"] = frozen_shadow_day
        if frozen_shadow_fingerprint is not None:
            kwargs["frozen_shadow_fingerprint"] = frozen_shadow_fingerprint
        if frozen_production_day is not None:
            kwargs["frozen_production_day"] = frozen_production_day
        if frozen_shadow_production_day is not None:
            kwargs["frozen_shadow_production_day"] = frozen_shadow_production_day
        return _collect_inputs(now, production_client=production_client, **kwargs)
    except Exception:  # noqa: BLE001 - the public decision must fail closed
        _log.warning("attendance readiness source unavailable")
        return _failed_readiness_inputs()


def _report_from_inputs(
    inputs: _ReadinessInputs,
    *,
    cutover_at: datetime | None,
    now_utc: datetime,
) -> ReadinessReport:
    now = _aware_utc(now_utc, "now_utc")
    blockers = _report_blockers(inputs, cutover_at=cutover_at, now_utc=now)
    return ReadinessReport(
        ready=not blockers,
        mirror_age_seconds=inputs.mirror_age_seconds,
        last_full_sweep_age_seconds=inputs.last_full_sweep_age_seconds,
        open_rows_not_refreshed=inputs.open_rows_not_refreshed,
        last_sweep_deletion_count=inputs.last_sweep_deletion_count,
        projection_lag_seconds=inputs.projection_lag_seconds,
        recalc_queue_age_seconds=inputs.recalc_queue_age_seconds,
        recalc_queue_depth=inputs.recalc_queue_depth,
        open_conflicts=inputs.open_conflicts,
        conflict_minutes_today=inputs.conflict_minutes_today,
        open_unmapped=inputs.open_unmapped,
        unmapped_minutes_today=inputs.unmapped_minutes_today,
        open_missing_required=inputs.open_missing_required,
        missing_minutes_today=inputs.missing_minutes_today,
        unassigned_units_today=inputs.unassigned_units_today,
        oldest_unassigned_age_seconds=inputs.oldest_unassigned_age_seconds,
        shadow_changed_worker_units=inputs.shadow_changed_worker_units,
        failed_corrections=inputs.failed_corrections,
        correction_retries_today=inputs.correction_retries_today,
        correction_verification_failures_today=(
            inputs.correction_verification_failures_today
        ),
        failed_department_repairs=inputs.failed_department_repairs,
        blockers=blockers,
    )


def build_report(
    now_utc: datetime,
    *,
    cutover_at: datetime | None = None,
    production_client=None,
) -> ReadinessReport:
    """Build one deterministic fail-closed report without changing rollout state."""
    now = _aware_utc(now_utc, "now_utc")
    try:
        (
            resolved_client,
            frozen_rows,
            shadow_day,
            shadow_fingerprint,
            frozen_production_day,
            frozen_shadow_production_day,
        ) = (
            _freeze_readiness_production_sources(now, production_client)
        )
    except Exception:  # noqa: BLE001 - public health is fail-closed
        resolved_client = production_client
        frozen_rows = None
        shadow_day = None
        shadow_fingerprint = None
        frozen_production_day = None
        frozen_shadow_production_day = None
    collect_kwargs = {
        "frozen_leaderboard_rows": frozen_rows,
        "frozen_shadow_day": shadow_day,
        "frozen_shadow_fingerprint": shadow_fingerprint,
        "frozen_production_day": frozen_production_day,
        "frozen_shadow_production_day": frozen_shadow_production_day,
    }
    if frozen_production_day is None:
        inputs = _collect_or_failed(now, resolved_client, **collect_kwargs)
    else:
        with _frozen_local_sources():
            current = _snapshot_production_day(frozen_production_day.day)
            if current.source_fingerprint != frozen_production_day.source_fingerprint:
                return _report_from_inputs(
                    _failed_readiness_inputs(),
                    cutover_at=cutover_at,
                    now_utc=now,
                )
            inputs = _collect_or_failed(now, resolved_client, **collect_kwargs)
    return _report_from_inputs(inputs, cutover_at=cutover_at, now_utc=now)


def _snapshot_production_day(day: date) -> _FrozenProductionDay:
    return production_history.strict_source_snapshot(day)


def _freeze_leaderboard_rows(
    day: date,
    production_client,
    *,
    now_utc: datetime,
    frozen_production_day: _FrozenProductionDay | None = None,
):
    resolved_client = production_client
    if resolved_client is None:
        from .deps import client

        resolved_client = client
    kwargs = {}
    if frozen_production_day is not None:
        kwargs = {
            "stations": frozen_production_day.stations,
            "shift_by_day": frozen_production_day.shift_by_day,
            "cache_variant": frozen_production_day.source_fingerprint,
            "persist": False,
        }
    rows = production_history._metered_leaderboard(  # noqa: SLF001
        resolved_client, day, now_utc=now_utc, **kwargs
    )
    return resolved_client, tuple(rows)


def _saved_shadow_day() -> date | None:
    raw = app_settings.get_setting(_SHADOW_SETTING_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return date.fromisoformat(str(raw["day"]))
    except (KeyError, TypeError, ValueError):
        return None


def _freeze_readiness_production_sources(now_utc: datetime, production_client):
    """Freeze current and saved-shadow meter facts before opening a DB snapshot."""
    now = _aware_utc(now_utc, "now_utc")
    current_day = now.astimezone(shift_config.SITE_TZ).date()
    with db.read_snapshot():
        shadow_day = _saved_shadow_day()
        frozen_production_day = _snapshot_production_day(current_day)
        frozen_shadow_production_day = (
            _snapshot_production_day(shadow_day)
            if shadow_day is not None
            else None
        )
    resolved_client, current_rows = _freeze_leaderboard_rows(
        current_day,
        production_client,
        now_utc=now,
        frozen_production_day=frozen_production_day,
    )
    shadow_fingerprint = None
    if shadow_day is not None:
        try:
            _resolved, shadow_rows = _freeze_leaderboard_rows(
                shadow_day,
                resolved_client,
                now_utc=now,
                frozen_production_day=frozen_shadow_production_day,
            )
            shadow_fingerprint = _leaderboard_rows_fingerprint(
                shadow_day,
                shadow_rows,
                frozen_production_day=frozen_shadow_production_day,
            )
        except Exception:  # noqa: BLE001 - stale/unavailable proof fails closed
            shadow_fingerprint = None
    return (
        resolved_client,
        current_rows,
        shadow_day,
        shadow_fingerprint,
        frozen_production_day,
        frozen_shadow_production_day,
    )


def _leaderboard_rows_fingerprint(
    day: date,
    leaderboard_rows,
    *,
    frozen_production_day: _FrozenProductionDay | None = None,
) -> str:
    """Hash only normalized meter facts used by strict attribution."""
    shift_start, shift_end = (
        (
            frozen_production_day.shift_start_utc,
            frozen_production_day.shift_end_utc,
        )
        if frozen_production_day is not None
        else production_history._strict_shift_bounds(day)  # noqa: SLF001
    )
    totals, samples, active = production_history._normalize_strict_leaderboard(  # noqa: SLF001
        leaderboard_rows,
        shift_start_utc=shift_start,
        shift_end_utc=shift_end,
    )
    payload = {
        "version": 1,
        "totals": [
            [str(wc), f"{float(values[0]):.6f}", f"{float(values[1]):.6f}"]
            for wc, values in sorted(totals.items())
        ],
        "samples": [
            [str(wc), timestamp.astimezone(UTC).isoformat(), f"{float(units):.6f}"]
            for wc, values in sorted(samples.items())
            for timestamp, units in values
        ],
        "active": [
            [str(wc), start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()]
            for wc, values in sorted(active.items())
            for start, end in values
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _combined_shadow_source_binding(local_fingerprint: str, meter_fingerprint: str) -> str:
    encoded = json.dumps(
        {
            "version": 1,
            "local": str(local_fingerprint),
            "meter": str(meter_fingerprint),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _snapshot_work_center_mapper():
    """Read the WC mapping from the current DB snapshot, bypassing TTL state."""
    rows = db.query(
        "SELECT name, odoo_work_center_id FROM work_centers "
        "WHERE odoo_work_center_id IS NOT NULL"
    )
    by_id = {int(row["odoo_work_center_id"]): str(row["name"]) for row in rows}

    def mapped(odoo_work_center_id: int) -> str | None:
        if isinstance(odoo_work_center_id, bool) or not isinstance(
            odoo_work_center_id, int
        ):
            return None
        return by_id.get(odoo_work_center_id)

    return mapped


def _source_fingerprints(*, now_utc: datetime, cur=None) -> tuple[str, str]:
    now = _aware_utc(now_utc, "now_utc")
    day = now.astimezone(shift_config.SITE_TZ).date()
    today_start = datetime.combine(
        day, datetime.min.time(), tzinfo=shift_config.SITE_TZ
    ).astimezone(UTC)
    tomorrow_start = datetime.combine(
        day + timedelta(days=1),
        datetime.min.time(),
        tzinfo=shift_config.SITE_TZ,
    ).astimezone(UTC)
    source_snapshot = production_history.strict_source_snapshot(day, cur=cur)
    strict_fingerprint = production_history.strict_local_source_fingerprint(
        day,
        cur=cur,
        source_snapshot=source_snapshot,
    )
    params = (today_start, tomorrow_start, day)
    if cur is None:
        rows = db.query(_SOURCE_FINGERPRINT_SQL, params)
        row = rows[0] if rows else None
    else:
        cur.execute(_SOURCE_FINGERPRINT_SQL, params)
        row = cur.fetchone()
    if not row or not row.get("local_source_fingerprint") or not row.get(
        "production_fingerprint"
    ):
        raise RuntimeError("readiness_source_fingerprint_unavailable")
    local_payload = json.dumps(
        {
            "version": 2,
            "strict": strict_fingerprint,
            "health": str(row["local_source_fingerprint"]),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        hashlib.sha256(local_payload.encode("utf-8")).hexdigest(),
        str(row["production_fingerprint"]),
    )


def _shadow_source_fingerprint(
    day: date,
    *,
    cur=None,
    frozen_production_day: _FrozenProductionDay | None = None,
) -> str:
    resolved = frozen_production_day or _snapshot_production_day(day)
    shift_start = resolved.shift_start_utc
    shift_end = resolved.shift_end_utc
    params = (shift_end, shift_start, day, day, day, day)
    if cur is None:
        rows = db.query(_SHADOW_SOURCE_FINGERPRINT_SQL, params)
        row = rows[0] if rows else None
    else:
        cur.execute(_SHADOW_SOURCE_FINGERPRINT_SQL, params)
        row = cur.fetchone()
    if not row or not row.get("shadow_source_fingerprint"):
        raise RuntimeError("shadow_source_fingerprint_unavailable")
    return str(row["shadow_source_fingerprint"])


def _lock_readiness_sources_cur(cur) -> None:
    """Freeze every local input after the rollout fence and before queue work."""
    cur.execute(
        "LOCK TABLE app_settings, odoo_attendance_mirror, odoo_attendance_sync_state, "
        "work_centers, departments, people, schedules, global_schedule, "
        "saturday_schedule, company_holidays, saturday_recruitments, "
        "wc_time_attributions, "
        "machine_breakdowns, attendance_correction_jobs, "
        "attendance_correction_job_events, "
        "attendance_department_repairs, production_daily, "
        "production_identity_aliases, attendance_recalc_queue IN SHARE MODE"
    )


def _lock_production_config_sources_cur(cur) -> None:
    cur.execute(
        "LOCK TABLE work_centers, schedules, global_schedule, saturday_schedule, "
        "company_holidays, saturday_recruitments IN SHARE MODE"
    )


@contextmanager
def _frozen_local_sources():
    """Read every local readiness source from one MVCC snapshot."""
    with db.read_snapshot() as cur:
        yield cur


def build_decision_snapshot(
    now_utc: datetime,
    *,
    cutover_at: datetime | None = None,
    production_client=None,
) -> DecisionSnapshot:
    """Build one report and prove its local source did not move underneath it."""
    now = _aware_utc(now_utc, "now_utc")
    try:
        (
            resolved_client,
            frozen_rows,
            shadow_day,
            shadow_fingerprint,
            frozen_production_day,
            frozen_shadow_production_day,
        ) = _freeze_readiness_production_sources(now, production_client)
    except Exception:  # noqa: BLE001 - external source failure blocks readiness
        frozen_rows = None
        frozen_production_day = None
        frozen_shadow_production_day = None
    with _frozen_local_sources() as cur:
        if frozen_production_day is not None:
            current_production_day = _snapshot_production_day(
                frozen_production_day.day
            )
            if (
                current_production_day.source_fingerprint
                != frozen_production_day.source_fingerprint
            ):
                raise DecisionSourceChanged(
                    "readiness production configuration changed during collection"
                )
        inputs = (
            _failed_readiness_inputs()
            if frozen_rows is None
            else _collect_or_failed(
                now,
                resolved_client,
                frozen_leaderboard_rows=frozen_rows,
                frozen_shadow_day=shadow_day,
                frozen_shadow_fingerprint=shadow_fingerprint,
                frozen_production_day=frozen_production_day,
                frozen_shadow_production_day=frozen_shadow_production_day,
            )
        )
        report = _report_from_inputs(
            inputs,
            cutover_at=cutover_at,
            now_utc=now,
        )
        sources = _source_fingerprints(now_utc=now, cur=cur)
    digest = report_digest(report)
    frozen_fingerprint = inputs.frozen_production_fingerprint
    valid_until = _decision_valid_until(now, inputs)
    return DecisionSnapshot(
        report=report,
        report_digest=digest,
        local_source_fingerprint=sources[0],
        production_fingerprint=sources[1],
        frozen_production_fingerprint=frozen_fingerprint,
        source_binding_digest=_decision_binding_digest(
            digest,
            sources[0],
            sources[1],
            frozen_fingerprint,
            checked_at=now,
            valid_until=valid_until,
        ),
        checked_at=now,
        valid_until=valid_until,
    )


def _decision_valid_until(now_utc: datetime, inputs: _ReadinessInputs) -> datetime:
    """Earliest instant at which a moving readiness threshold can turn red."""
    now = _aware_utc(now_utc, "now_utc")
    remaining = [_LIVE_GATE_MAX_AGE]
    if inputs.mirror_age_seconds is not None:
        remaining.append(
            timedelta(seconds=max(0.0, _MIRROR_MAX_AGE - inputs.mirror_age_seconds))
        )
    if inputs.last_full_sweep_age_seconds is not None:
        remaining.append(
            timedelta(
                seconds=max(
                    0.0,
                    _FULL_SWEEP_MAX_AGE - inputs.last_full_sweep_age_seconds,
                )
            )
        )
    if inputs.recalc_queue_depth and inputs.recalc_queue_age_seconds is not None:
        remaining.append(
            timedelta(
                seconds=max(
                    0.0,
                    _RECALC_MAX_AGE - inputs.recalc_queue_age_seconds,
                )
            )
        )
    return now + min(remaining)


def _decision_binding_digest(
    report_fingerprint: str,
    local_fingerprint: str,
    production_fingerprint: str,
    frozen_production_fingerprint: str,
    *,
    checked_at: datetime | None = None,
    valid_until: datetime | None = None,
) -> str:
    payload = {
        "version": 1,
        "report": report_fingerprint,
        "local": local_fingerprint,
        "saved_production": production_fingerprint,
        "frozen_production": frozen_production_fingerprint,
        "checked_at": (
            _aware_utc(checked_at, "checked_at").isoformat()
            if checked_at is not None
            else None
        ),
        "valid_until": (
            _aware_utc(valid_until, "valid_until").isoformat()
            if valid_until is not None
            else None
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decision_matches_sources(
    decision: DecisionSnapshot,
    current_sources: tuple[str, str],
) -> bool:
    if current_sources != (
        decision.local_source_fingerprint,
        decision.production_fingerprint,
    ):
        return False
    return decision.source_binding_digest == _decision_binding_digest(
        decision.report_digest,
        decision.local_source_fingerprint,
        decision.production_fingerprint,
        decision.frozen_production_fingerprint,
        checked_at=decision.checked_at,
        valid_until=decision.valid_until,
    )


def _decision_is_fresh(decision: DecisionSnapshot, accepted_at: datetime) -> bool:
    if decision.checked_at is None or decision.valid_until is None:
        return False
    checked = _aware_utc(decision.checked_at, "checked_at")
    valid_until = _aware_utc(decision.valid_until, "valid_until")
    accepted = _aware_utc(accepted_at, "accepted_at")
    return checked <= accepted <= valid_until


def report_json(report: ReadinessReport) -> str:
    _validate_report_finite(report)
    return json.dumps(
        asdict(report),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate_report_finite(report: ReadinessReport) -> None:
    for value in asdict(report).values():
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("nonfinite_readiness_value")


def report_digest(report: ReadinessReport) -> str:
    """Hash only stable decision inputs, never moving display ages or PII."""

    _validate_report_finite(report)

    def quantized(value: float) -> str:
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("nonfinite_readiness_value")
        return f"{numeric:.6f}"

    decision = {
        "version": 1,
        "ready": bool(report.ready),
        "blockers": sorted(report.blockers),
        "counts": {
            "open_rows_not_refreshed": int(report.open_rows_not_refreshed),
            "last_sweep_deletion_count": int(report.last_sweep_deletion_count),
            "recalc_queue_depth": int(report.recalc_queue_depth),
            "open_conflicts": int(report.open_conflicts),
            "open_unmapped": int(report.open_unmapped),
            "open_missing_required": int(report.open_missing_required),
            "failed_corrections": int(report.failed_corrections),
            "correction_retries_today": int(report.correction_retries_today),
            "correction_verification_failures_today": int(
                report.correction_verification_failures_today
            ),
            "failed_department_repairs": int(report.failed_department_repairs),
        },
        "units": {
            "unassigned": quantized(report.unassigned_units_today),
            "shadow_changed": quantized(report.shadow_changed_worker_units),
        },
    }
    canonical = json.dumps(
        decision,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _overlap_seconds(left, right) -> float:
    left_end = left.end_utc
    right_end = right.end_utc
    if left_end is None or right_end is None:
        return 0.0
    return max(
        0.0,
        (min(left_end, right_end) - max(left.start_utc, right.start_utc)).total_seconds(),
    )


def _issue_minutes(issue, *, now_utc: datetime) -> float:
    end = issue.end_utc or now_utc
    return max(0.0, (min(end, now_utc) - min(issue.start_utc, now_utc)).total_seconds() / 60)


def _issue_metrics(snapshot, *, now_utc: datetime) -> _IssueMetrics:
    conflicts = [i for i in snapshot.issues if i.kind == "attendance_conflicting_location"]
    unmapped = [i for i in snapshot.issues if i.kind == "attendance_unmapped_location"]
    missing = [i for i in snapshot.issues if i.kind == "attendance_missing_location"]
    unassigned = [i for i in snapshot.issues if i.kind == "production_unassigned_run"]

    def affects_production(issues) -> bool:
        return any(_overlap_seconds(issue, run) > 0 for issue in issues for run in unassigned)

    starts = [issue.start_utc for issue in unassigned]
    return _IssueMetrics(
        projection_complete=bool(snapshot.complete and not snapshot.source_errors),
        open_conflicts=len(conflicts),
        conflict_minutes_today=sum(_issue_minutes(i, now_utc=now_utc) for i in conflicts),
        open_unmapped=len(unmapped),
        unmapped_minutes_today=sum(_issue_minutes(i, now_utc=now_utc) for i in unmapped),
        open_missing_required=len(missing),
        missing_minutes_today=sum(_issue_minutes(i, now_utc=now_utc) for i in missing),
        unassigned_units_today=sum(float(i.units or 0) for i in unassigned),
        oldest_unassigned_age_seconds=(
            max(0.0, (now_utc - min(starts)).total_seconds()) if starts else None
        ),
        unmapped_affects_production=affects_production(unmapped),
        missing_affects_production=affects_production(missing),
        comparison_identity_available=True,
    )


def _strict_units_by_identity(attribution) -> dict[tuple[str, str], float]:
    values: dict[tuple[str, str], float] = {}
    for identity, by_work_center in attribution.items():
        if (
            not isinstance(identity, tuple)
            or len(identity) != 2
            or isinstance(identity[0], bool)
            or not isinstance(identity[0], int)
            or identity[0] <= 0
        ):
            raise ValueError("noncanonical_strict_employee_id")
        employee_id = str(identity[0])
        for work_center, totals in by_work_center.items():
            key = (employee_id, str(work_center))
            values[key] = values.get(key, 0.0) + float(totals.get("units") or 0)
    return values


def _frozen_production_fingerprint(strict_inputs) -> str:
    """Hash the one normalized meter response used by this decision."""
    payload = {
        "version": 1,
        "totals": [
            [str(wc), f"{float(values[0]):.6f}", f"{float(values[1]):.6f}"]
            for wc, values in sorted(getattr(strict_inputs, "wc_totals", {}).items())
        ],
        "samples": [
            [str(wc), timestamp.astimezone(UTC).isoformat(), f"{float(units):.6f}"]
            for wc, samples in sorted(
                getattr(strict_inputs, "samples_by_wc", {}).items()
            )
            for timestamp, units in samples
        ],
        "active": [
            [str(wc), start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()]
            for wc, intervals in sorted(
                getattr(strict_inputs, "active_intervals_by_wc", {}).items()
            )
            for start, end in intervals
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def shadow_comparison_json(comparison: ShadowComparison) -> str:
    return json.dumps(asdict(comparison), sort_keys=True, default=str, separators=(",", ":"))


def _shadow_error_code(exc: Exception) -> str:
    value = str(exc)
    if value in {
        "noncanonical_strict_employee_id",
        "noncanonical_current_employee_id",
    }:
        return value
    return "shadow_comparison_failed"


def compute_shadow_comparison(
    day: date,
    production_client,
    *,
    now_utc: datetime,
    leaderboard_rows=None,
    map_work_center=None,
    shift_bounds: tuple[datetime, datetime] | None = None,
    break_windows: tuple[tuple[datetime, datetime], ...] | None = None,
    source_config_fingerprint: str | None = None,
) -> ShadowComparison:
    """Compare strict credit with saved current credit by canonical employee ID."""
    now = _aware_utc(now_utc, "now_utc")
    try:
        if leaderboard_rows is None:
            strict_attribution = production_history._strict_attribution_for(  # noqa: SLF001
                day,
                production_client,
                now_utc=now,
            )
        else:
            strict_inputs = production_history._strict_inputs_for_day(  # noqa: SLF001
                day,
                production_client,
                now_utc=now,
                leaderboard_rows=leaderboard_rows,
                map_work_center=map_work_center,
                shift_bounds=shift_bounds,
                break_windows=break_windows,
                source_config_fingerprint=source_config_fingerprint,
            )
            strict_attribution = production_history._strict_attribution_from_inputs(  # noqa: SLF001
                day,
                strict_inputs,
            )
        return _compare_strict_to_current(day, strict_attribution, now_utc=now)
    except Exception as exc:  # noqa: BLE001 - readiness is fail-closed
        return ShadowComparison(
            day, now, False, 0.0, 0, 0.0, 0.0, _shadow_error_code(exc)
        )


def _compare_strict_to_current(
    day: date,
    strict_attribution,
    *,
    now_utc: datetime,
) -> ShadowComparison:
    now = _aware_utc(now_utc, "now_utc")
    try:
        strict = _strict_units_by_identity(strict_attribution)
        current_rows = db.query(
            "SELECT COALESCE(a.canonical_emp_id, p.emp_id) AS emp_id, "
            "p.wc_name, p.units "
            "FROM production_daily p "
            "LEFT JOIN production_identity_aliases a ON a.legacy_emp_id = p.emp_id "
            "WHERE p.day = %s ORDER BY 1, 2",
            (day,),
        )
        current: dict[tuple[str, str], float] = {}
        for row in current_rows:
            employee_id = str(row["emp_id"])
            if not employee_id.isdigit() or int(employee_id) <= 0:
                raise ValueError("noncanonical_current_employee_id")
            key = (employee_id, str(row["wc_name"]))
            current[key] = current.get(key, 0.0) + float(row["units"] or 0)
    except Exception as exc:  # noqa: BLE001 - readiness is fail-closed
        return ShadowComparison(
            day, now, False, 0.0, 0, 0.0, 0.0, _shadow_error_code(exc)
        )

    keys = set(strict) | set(current)
    added = sum(max(0.0, strict.get(key, 0.0) - current.get(key, 0.0)) for key in keys)
    removed = sum(max(0.0, current.get(key, 0.0) - strict.get(key, 0.0)) for key in keys)
    return ShadowComparison(
        day=day,
        checked_at=now,
        complete=True,
        changed_worker_units=max(added, removed),
        comparison_keys=len(keys),
        strict_worker_units=sum(strict.values()),
        current_worker_units=sum(current.values()),
        error=None,
    )


def _strict_issue_metrics(segments, runs, *, now_utc: datetime) -> _IssueMetrics:
    issues = []
    kind_by_status = {
        "conflicting_location": "attendance_conflicting_location",
        "unmapped_location": "attendance_unmapped_location",
        "missing_required_location": "attendance_missing_location",
    }
    for segment in segments:
        kind = kind_by_status.get(segment.status)
        if kind is None:
            continue
        issues.append(
            type(
                "ReadinessIssue",
                (),
                {
                    "kind": kind,
                    "start_utc": segment.start_utc,
                    "end_utc": segment.end_utc,
                    "units": None,
                    "app_work_center_name": segment.app_work_center_name,
                },
            )()
        )
    for run in runs:
        issues.append(
            type(
                "ReadinessIssue",
                (),
                {
                    "kind": "production_unassigned_run",
                    "start_utc": run.start_utc,
                    "end_utc": run.end_utc,
                    "units": run.units,
                    "app_work_center_name": run.wc_name,
                },
            )()
        )
    snapshot = type(
        "ReadinessIssueSnapshot",
        (),
        {"complete": True, "source_errors": (), "issues": tuple(issues)},
    )()
    metrics = _issue_metrics(snapshot, now_utc=now_utc)

    # A WC-wide meter sample has no employee identity.  It can be tied to a
    # bad attendance span only when exactly one canonical employee at the same
    # WC intersects the run.  Anything less precise fails closed instead of
    # guessing across employees or work centers.
    affects = {"unmapped_location": False, "missing_required_location": False}
    identity_available = True
    bad_segments = [segment for segment in segments if segment.status in affects]
    for run in runs:
        if float(run.units or 0) <= 0:
            continue
        candidates = [
            segment
            for segment in bad_segments
            if segment.app_work_center_name
            and segment.app_work_center_name == run.wc_name
            and _overlap_seconds(segment, run) > 0
            and isinstance(getattr(segment, "employee_odoo_id", None), int)
            and not isinstance(getattr(segment, "employee_odoo_id", None), bool)
            and getattr(segment, "employee_odoo_id", 0) > 0
        ]
        if len(candidates) != 1:
            identity_available = False
            continue
        affects[candidates[0].status] = True
    return _IssueMetrics(
        projection_complete=metrics.projection_complete,
        open_conflicts=metrics.open_conflicts,
        conflict_minutes_today=metrics.conflict_minutes_today,
        open_unmapped=metrics.open_unmapped,
        unmapped_minutes_today=metrics.unmapped_minutes_today,
        open_missing_required=metrics.open_missing_required,
        missing_minutes_today=metrics.missing_minutes_today,
        unassigned_units_today=metrics.unassigned_units_today,
        oldest_unassigned_age_seconds=metrics.oldest_unassigned_age_seconds,
        unmapped_affects_production=affects["unmapped_location"],
        missing_affects_production=affects["missing_required_location"],
        comparison_identity_available=identity_available,
    )


def _shadow_setting(
    comparison: ShadowComparison,
    *,
    shadow_entered_at: datetime | None = None,
    shift_start_utc: datetime,
    shift_end_utc: datetime,
    source_binding: str,
    production_source_fingerprint: str,
) -> dict:
    return {
        "schema_version": 1,
        "day": comparison.day.isoformat(),
        "checked_at": comparison.checked_at.isoformat(),
        "shadow_entered_at": (
            _aware_utc(shadow_entered_at, "shadow_entered_at").isoformat()
            if shadow_entered_at is not None
            else None
        ),
        "shift_start_utc": _aware_utc(shift_start_utc, "shift_start_utc").isoformat(),
        "shift_end_utc": _aware_utc(shift_end_utc, "shift_end_utc").isoformat(),
        "source_binding": str(source_binding),
        "production_source_fingerprint": str(production_source_fingerprint),
        "complete": comparison.complete,
        "projection_complete": comparison.complete,
        "source_mirror_at": (
            comparison.source_mirror_at.isoformat()
            if comparison.source_mirror_at is not None
            else None
        ),
        "changed_worker_units": comparison.changed_worker_units,
        "comparison_keys": comparison.comparison_keys,
        "strict_worker_units": comparison.strict_worker_units,
        "current_worker_units": comparison.current_worker_units,
        "error": comparison.error,
    }


def start_shadow_epoch_cur(cur, *, entered_at: datetime) -> None:
    """Start a new Shadow observation generation and discard older proof."""
    entered = _aware_utc(entered_at, "entered_at")
    app_settings.set_setting(
        _SHADOW_EPOCH_SETTING_KEY,
        {"schema_version": 1, "entered_at": entered.isoformat()},
        cur=cur,
    )
    cur.execute(
        "DELETE FROM app_settings WHERE key IN (%s, %s)",
        (_SHADOW_SETTING_KEY, _READINESS_REPORT_SETTING_KEY),
    )


def clear_shadow_evidence_cur(cur) -> None:
    cur.execute(
        "DELETE FROM app_settings WHERE key IN (%s, %s, %s)",
        (
            _SHADOW_EPOCH_SETTING_KEY,
            _SHADOW_SETTING_KEY,
            _READINESS_REPORT_SETTING_KEY,
        ),
    )


def clear_cutover_blocked_cur(cur) -> None:
    """Clear the singleton failed-cutover alert inside the rollout fence."""
    cur.execute(
        "DELETE FROM app_settings WHERE key = %s",
        (_CUTOVER_BLOCKED_SETTING_KEY,),
    )


def _shadow_epoch_entered_at() -> datetime | None:
    raw = app_settings.get_setting(_SHADOW_EPOCH_SETTING_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return _aware_utc(datetime.fromisoformat(str(raw["entered_at"])), "entered_at")
    except (KeyError, TypeError, ValueError):
        return None


def _shadow_epoch_entered_at_cur(cur) -> datetime | None:
    cur.execute(
        "SELECT value FROM app_settings WHERE key = %s",
        (_SHADOW_EPOCH_SETTING_KEY,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    raw = row["value"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict):
        return None
    try:
        return _aware_utc(datetime.fromisoformat(str(raw["entered_at"])), "entered_at")
    except (KeyError, TypeError, ValueError):
        return None


def _rollout_persistence_is_current(
    cur,
    *,
    expected_config: attendance_location_policy.RolloutConfig,
    expected_shadow_entered_at: datetime | None,
) -> bool:
    current = attendance_location_policy.get_rollout_config_cur(cur)
    if current != expected_config:
        return False
    if expected_shadow_entered_at is None:
        return True
    return _shadow_epoch_entered_at_cur(cur) == _aware_utc(
        expected_shadow_entered_at,
        "expected_shadow_entered_at",
    )


def refresh_shadow_comparison(
    day: date,
    production_client,
    *,
    now_utc: datetime,
    shadow_entered_at: datetime | None = None,
) -> ShadowComparison | None:
    """Persist only aggregate shadow health; never write production credit."""
    config = attendance_location_policy.get_rollout_config()
    pending_live = bool(
        config.mode == "live"
        and getattr(config, "live_gate", None) is not None
        and config.live_gate.activated_at is None
    )
    if config.mode != "shadow" and not pending_live:
        return None
    with db.read_snapshot():
        frozen_production_day = _snapshot_production_day(day)
    resolved_client, frozen_rows = _freeze_leaderboard_rows(
        day,
        production_client,
        now_utc=now_utc,
        frozen_production_day=frozen_production_day,
    )
    with db.read_snapshot() as cur:
        # Health, projection, saved production, and binding all come from the
        # same MVCC view. The external meter response was frozen beforehand.
        current_production_day = _snapshot_production_day(day)
        if (
            current_production_day.source_fingerprint
            != frozen_production_day.source_fingerprint
        ):
            return None
        health = attendance_mirror.health_snapshot()
        local_source_fingerprint = _shadow_source_fingerprint(
            day,
            cur=cur,
            frozen_production_day=frozen_production_day,
        )
        production_source_fingerprint = _leaderboard_rows_fingerprint(
            day,
            frozen_rows,
            frozen_production_day=frozen_production_day,
        )
        source_binding = _combined_shadow_source_binding(
            local_source_fingerprint,
            production_source_fingerprint,
        )
        comparison = compute_shadow_comparison(
            day,
            resolved_client,
            now_utc=now_utc,
            leaderboard_rows=frozen_rows,
            map_work_center=lambda odoo_id: frozen_production_day.work_center_by_odoo_id.get(
                odoo_id
            ),
            shift_bounds=(
                frozen_production_day.shift_start_utc,
                frozen_production_day.shift_end_utc,
            ),
            break_windows=frozen_production_day.break_windows,
            source_config_fingerprint=frozen_production_day.source_fingerprint,
        )
    comparison = ShadowComparison(
        day=comparison.day,
        checked_at=comparison.checked_at,
        complete=comparison.complete,
        changed_worker_units=comparison.changed_worker_units,
        comparison_keys=comparison.comparison_keys,
        strict_worker_units=comparison.strict_worker_units,
        current_worker_units=comparison.current_worker_units,
        error=comparison.error,
        source_mirror_at=(
            health.last_incremental_observed_at or health.baseline_completed_at
        ),
    )
    entered_at = shadow_entered_at or _shadow_epoch_entered_at()
    shift_start_utc = frozen_production_day.shift_start_utc
    shift_end_utc = frozen_production_day.shift_end_utc
    payload = _shadow_setting(
        comparison,
        shadow_entered_at=entered_at,
        shift_start_utc=shift_start_utc,
        shift_end_utc=shift_end_utc,
        source_binding=source_binding,
        production_source_fingerprint=production_source_fingerprint,
    )
    if entered_at is None:
        return None
    with db.cursor() as cur:
        attendance_location_policy.lock_rollout_decision_cur(cur)
        _lock_production_config_sources_cur(cur)
        if not _rollout_persistence_is_current(
            cur,
            expected_config=config,
            expected_shadow_entered_at=entered_at,
        ):
            return None
        if (
            _snapshot_production_day(day).source_fingerprint
            != frozen_production_day.source_fingerprint
        ):
            return None
        app_settings.set_setting(_SHADOW_SETTING_KEY, payload, cur=cur)
    return comparison


def _collect_db_metrics(now_utc: datetime) -> dict:
    local_day = now_utc.astimezone(shift_config.SITE_TZ).date()
    today_start = datetime.combine(
        local_day,
        datetime.min.time(),
        tzinfo=shift_config.SITE_TZ,
    ).astimezone(UTC)
    tomorrow_start = datetime.combine(
        local_day + timedelta(days=1),
        datetime.min.time(),
        tzinfo=shift_config.SITE_TZ,
    ).astimezone(UTC)
    rows = db.query(
        """
        WITH sync AS (
          SELECT baseline_completed_at IS NOT NULL AS baseline_complete,
                 last_error AS mirror_error,
                 COALESCE(last_incremental_completed_at, baseline_completed_at)
                   AS mirror_completed_at,
                 COALESCE(last_incremental_observed_at, baseline_completed_at)
                   AS mirror_observed_at,
                 last_full_sweep_completed_at AS full_sweep_completed_at,
                 last_full_sweep_deletion_count
          FROM odoo_attendance_sync_state WHERE singleton = TRUE
        ), recalc AS (
          SELECT MIN(requested_at) FILTER (
                   WHERE completed_at IS NULL OR cache_ready_at IS NULL)
                   AS oldest_recalc_requested_at,
                 COUNT(*) FILTER (
                   WHERE completed_at IS NULL OR cache_ready_at IS NULL)
                   AS recalc_queue_depth,
                 COALESCE(array_agg(day::text ORDER BY day) FILTER (
                   WHERE completed_at IS NULL OR cache_ready_at IS NULL),
                   ARRAY[]::text[]) AS recalculation_ids
          FROM attendance_recalc_queue
        ), latest_corrections AS (
          SELECT DISTINCT ON (item_key) id, item_key, status
          FROM attendance_correction_jobs
          ORDER BY item_key, created_at DESC, id DESC
        ), correction_events AS (
          SELECT COUNT(*) FILTER (
                   WHERE phase = 'claim' AND result = 'claimed'
                     AND COALESCE((detail->>'attempt_count')::int, 1) > 1)
                   AS correction_retries_today,
                 COUNT(*) FILTER (
                   WHERE phase = 'verifying'
                     AND result IN ('mismatch', 'odoo_failure'))
                   AS correction_verification_failures_today,
                 COALESCE(array_agg(DISTINCT correction_job_id
                   ORDER BY correction_job_id) FILTER (
                     WHERE (phase = 'claim' AND result = 'claimed'
                       AND COALESCE((detail->>'attempt_count')::int, 1) > 1)
                       OR (phase = 'verifying'
                         AND result IN ('mismatch', 'odoo_failure'))),
                   ARRAY[]::bigint[]) AS correction_event_job_ids
          FROM attendance_correction_job_events
          WHERE created_at >= %s AND created_at < %s
        ), corrections AS (
          SELECT COUNT(*) FILTER (WHERE status = 'failed') AS failed_corrections,
                 COALESCE(array_agg(id ORDER BY id) FILTER (WHERE status = 'failed'),
                   ARRAY[]::bigint[]) || COALESCE((SELECT correction_event_job_ids
                   FROM correction_events), ARRAY[]::bigint[]) AS correction_job_ids,
                 COALESCE((SELECT correction_retries_today
                   FROM correction_events), 0) AS correction_retries_today,
                 COALESCE((SELECT correction_verification_failures_today
                   FROM correction_events), 0)
                   AS correction_verification_failures_today
          FROM latest_corrections
        ), repairs AS (
          SELECT COUNT(*) FILTER (WHERE status = 'failed') AS failed_department_repairs,
                 COALESCE(array_agg(odoo_attendance_id ORDER BY odoo_attendance_id)
                   FILTER (WHERE status = 'failed'), ARRAY[]::bigint[])
                   AS repair_attendance_ids
          FROM attendance_department_repairs
        )
        SELECT sync.*, recalc.*, corrections.*, repairs.*,
               (SELECT value FROM app_settings WHERE key = %s) AS shadow_health,
               (SELECT value FROM app_settings WHERE key = %s) AS shadow_epoch,
               (SELECT COUNT(*) FROM odoo_attendance_mirror m
                 WHERE m.deleted_at IS NULL AND m.check_out_utc IS NULL
                   AND (sync.mirror_observed_at IS NULL
                        OR m.last_seen_at < sync.mirror_observed_at))
                 AS open_rows_not_refreshed
        FROM sync CROSS JOIN recalc CROSS JOIN corrections CROSS JOIN repairs
        """,
        (
            today_start,
            tomorrow_start,
            _SHADOW_SETTING_KEY,
            _SHADOW_EPOCH_SETTING_KEY,
        ),
    )
    if not rows:
        raise RuntimeError("attendance readiness state is missing")
    row = rows[0]
    shadow = row.get("shadow_health")
    shadow_epoch = row.get("shadow_epoch")
    if isinstance(shadow, str):
        try:
            shadow = json.loads(shadow)
        except json.JSONDecodeError:
            shadow = None
    if isinstance(shadow_epoch, str):
        try:
            shadow_epoch = json.loads(shadow_epoch)
        except json.JSONDecodeError:
            shadow_epoch = None
    shadow_day_complete = False
    shadow_source_day = None
    shadow_source_binding = None
    shadow_production_fingerprint = None
    projection_lag_seconds = None
    if isinstance(shadow, dict):
        try:
            shadow_day = date.fromisoformat(str(shadow.get("day")))
            shadow_source_day = shadow_day
            binding_value = shadow.get("source_binding")
            if not isinstance(binding_value, str) or not binding_value:
                raise ValueError("shadow source binding missing")
            shadow_source_binding = binding_value
            production_binding_value = shadow.get("production_source_fingerprint")
            if not isinstance(production_binding_value, str) or not production_binding_value:
                raise ValueError("shadow production source fingerprint missing")
            shadow_production_fingerprint = production_binding_value
            checked_at = _aware_utc(
                datetime.fromisoformat(str(shadow.get("checked_at"))),
                "shadow checked_at",
            )
            if not isinstance(shadow_epoch, dict):
                raise ValueError("shadow epoch missing")
            shadow_entered_at = _aware_utc(
                datetime.fromisoformat(str(shadow.get("shadow_entered_at"))),
                "shadow entered_at",
            )
            current_entered_at = _aware_utc(
                datetime.fromisoformat(str(shadow_epoch.get("entered_at"))),
                "current shadow entered_at",
            )
            source_mirror_at = datetime.fromisoformat(str(shadow.get("source_mirror_at")))
            source_mirror_at = _aware_utc(source_mirror_at, "source_mirror_at")
            shift_start = _aware_utc(
                datetime.fromisoformat(str(shadow.get("shift_start_utc"))),
                "shadow shift_start_utc",
            )
            shift_end = _aware_utc(
                datetime.fromisoformat(str(shadow.get("shift_end_utc"))),
                "shadow shift_end_utc",
            )
            shadow_day_complete = bool(
                shadow.get("projection_complete", shadow.get("complete", False))
                and shadow_day < local_day
                and shadow_entered_at == current_entered_at
                and shadow_entered_at <= shift_start
                and checked_at >= shift_end
                and source_mirror_at >= shift_end
            )
            mirror_completed_at = row.get("mirror_completed_at")
            if mirror_completed_at is not None:
                projection_lag_seconds = max(
                    0.0,
                    (
                        _aware_utc(mirror_completed_at, "mirror_completed_at")
                        - source_mirror_at
                    ).total_seconds(),
                )
        except (TypeError, ValueError):
            shadow_day_complete = False
            projection_lag_seconds = None
    return {
        "baseline_complete": bool(row["baseline_complete"]),
        "mirror_error": row.get("mirror_error"),
        "mirror_age_seconds": _seconds_since(now_utc, row.get("mirror_completed_at")),
        "last_full_sweep_age_seconds": _seconds_since(
            now_utc, row.get("full_sweep_completed_at")
        ),
        "open_rows_not_refreshed": int(row.get("open_rows_not_refreshed") or 0),
        "last_sweep_deletion_count": int(row.get("last_sweep_deletion_count") or 0),
        "recalc_queue_age_seconds": _seconds_since(
            now_utc, row.get("oldest_recalc_requested_at")
        ),
        "recalc_queue_depth": int(row.get("recalc_queue_depth") or 0),
        "failed_corrections": int(row.get("failed_corrections") or 0),
        "correction_retries_today": int(row.get("correction_retries_today") or 0),
        "correction_verification_failures_today": int(
            row.get("correction_verification_failures_today") or 0
        ),
        "failed_department_repairs": int(row.get("failed_department_repairs") or 0),
        "correction_job_ids": tuple(row.get("correction_job_ids") or ()),
        "repair_attendance_ids": tuple(row.get("repair_attendance_ids") or ()),
        "recalculation_ids": tuple(row.get("recalculation_ids") or ()),
        "shadow_day_complete": shadow_day_complete,
        "projection_lag_seconds": projection_lag_seconds,
        "shadow_source_day": shadow_source_day,
        "shadow_source_binding": shadow_source_binding,
        "shadow_production_fingerprint": shadow_production_fingerprint,
    }


def validate_cutover(cutover_at: datetime, *, now_utc: datetime) -> datetime:
    """Require an exact, representable future local workday boundary."""
    now = _aware_utc(now_utc, "now_utc")
    if not isinstance(cutover_at, datetime) or cutover_at.utcoffset() is None:
        raise ValueError("cutover_timezone_required")
    local = cutover_at.astimezone(shift_config.SITE_TZ)
    # A UTC round trip rejects nonexistent local wall times. Ambiguous times
    # retain their explicit fold through the round trip.
    if local.astimezone(UTC).astimezone(shift_config.SITE_TZ) != local:
        raise ValueError("cutover_invalid_local_time")
    shift = shift_config.snapshot_for(local.date())
    if local.time().replace(tzinfo=None) != shift.shift_start:
        raise ValueError("cutover_boundary_required")
    if not shift.is_workday:
        raise ValueError("cutover_workday_required")
    if local.astimezone(UTC) <= now:
        raise ValueError("cutover_future_required")
    return local


def parse_local_cutover(raw_value: str) -> datetime:
    """Parse an HTML local datetime without guessing through DST edges."""
    try:
        parsed = datetime.fromisoformat(str(raw_value).strip())
    except ValueError as exc:
        raise ValueError("cutover_invalid") from exc
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return parsed.astimezone(shift_config.SITE_TZ)
    candidates = []
    for fold in (0, 1):
        candidate = parsed.replace(tzinfo=shift_config.SITE_TZ, fold=fold)
        round_trip = candidate.astimezone(UTC).astimezone(shift_config.SITE_TZ)
        if round_trip.replace(tzinfo=None) == parsed and round_trip.fold == fold:
            candidates.append(candidate)
    unique = {candidate.astimezone(UTC) for candidate in candidates}
    if not unique:
        raise ValueError("cutover_invalid_local_time")
    if len(unique) > 1:
        raise ValueError("cutover_ambiguous_local_time")
    return candidates[0]


def _store_pending_cutover(
    config: attendance_location_policy.RolloutConfig,
    *,
    checked_at: datetime,
    expected_config: attendance_location_policy.RolloutConfig | None = None,
    decision: DecisionSnapshot | None = None,
) -> None:
    if _utc_now() - checked_at > _LIVE_GATE_MAX_AGE:
        raise ValueError("live_readiness_expired")
    with db.cursor() as cur:
        attendance_location_policy.lock_rollout_decision_cur(cur)
        if expected_config is not None:
            cur.execute(
                "SELECT value FROM app_settings WHERE key = %s FOR UPDATE",
                (_ROLLOUT_SETTING_KEY,),
            )
            row = cur.fetchone()
            try:
                if row is None:
                    current = attendance_location_policy.RolloutConfig("off", None, None)
                else:
                    raw = row["value"]
                    if isinstance(raw, str):
                        raw = json.loads(raw)
                    current = attendance_location_policy._parse_config(raw)  # noqa: SLF001
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("rollout_config_invalid") from exc
            if current != expected_config:
                raise ValueError("live_schedule_superseded")
        if decision is not None:
            _lock_readiness_sources_cur(cur)
            if not _decision_matches_sources(
                decision,
                _source_fingerprints(now_utc=checked_at, cur=cur),
            ):
                raise ValueError("live_readiness_superseded")
        cur.execute("SELECT clock_timestamp() AS accepted_at")
        accepted_row = cur.fetchone()
        accepted_at = _aware_utc(accepted_row["accepted_at"], "accepted_at")
        age = accepted_at - checked_at
        if age < timedelta(0) or age > _LIVE_GATE_MAX_AGE:
            raise ValueError("live_readiness_expired")
        if decision is not None and not _decision_is_fresh(decision, accepted_at):
            raise ValueError("live_readiness_expired")
        if config.cutover_at is None or config.cutover_at.astimezone(UTC) <= accepted_at:
            raise ValueError("cutover_future_required")
        attendance_location_policy.set_rollout_config(config, cur=cur)
        if decision is not None:
            _record_rollout_audit_cur(
                cur,
                event_kind="live_scheduled",
                rollout_mode="live",
                cutover_at=config.cutover_at,
                checked_at=checked_at,
                report_fingerprint=decision.report_digest,
            )


def schedule_live_cutover(
    cutover_at: datetime,
    *,
    now_utc: datetime,
    production_client=None,
) -> attendance_location_policy.RolloutConfig:
    """Freshly check and store one pending live gate in the same request."""
    checked_at = _aware_utc(now_utc, "now_utc")
    local_cutover = validate_cutover(cutover_at, now_utc=checked_at)
    expected_config = attendance_location_policy.get_rollout_config_strict()
    if attendance_location_policy._live_is_active(  # noqa: SLF001
        expected_config,
        checked_at,
    ):
        raise ValueError("live_already_active")
    decision = build_decision_snapshot(
        checked_at,
        cutover_at=local_cutover,
        production_client=production_client,
    )
    report = decision.report
    if not report.ready:
        raise ValueError(f"live_readiness_blocked:{','.join(report.blockers)}")
    if _utc_now() - checked_at > _LIVE_GATE_MAX_AGE:
        raise ValueError("live_readiness_expired")
    config = attendance_location_policy.RolloutConfig(
        mode="live",
        cutover_at=local_cutover,
        live_gate=attendance_location_policy.LiveGate(
            checked_at=checked_at,
            report_digest=decision.report_digest,
            activated_at=None,
        ),
    )
    _store_pending_cutover(
        config,
        checked_at=checked_at,
        expected_config=expected_config,
        decision=decision,
    )
    return config


ActivationResult = Literal[
    "not_due", "busy", "activated", "rolled_back", "superseded"
]


@contextmanager
def _activation_claim():
    """Let only one app process perform the expensive due-boundary recheck."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_xact_lock(%s) AS claimed",
            (_ACTIVATION_ADVISORY_LOCK_KEY,),
        )
        row = cur.fetchone()
        yield bool(row and row["claimed"])


@contextmanager
def _shadow_refresh_claim():
    with db.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_xact_lock(%s) AS claimed",
            (_SHADOW_ADVISORY_LOCK_KEY,),
        )
        row = cur.fetchone()
        yield bool(row and row["claimed"])


def _previous_complete_workday(now_utc: datetime) -> date:
    candidate = _aware_utc(now_utc, "now_utc").astimezone(shift_config.SITE_TZ).date()
    for _attempt in range(8):
        candidate -= timedelta(days=1)
        if shift_config.is_workday(candidate):
            return candidate
    raise RuntimeError("previous_workday_unavailable")


def _persist_readiness_report(
    report: ReadinessReport,
    *,
    checked_at: datetime,
    cur=None,
    expected_config: attendance_location_policy.RolloutConfig | None = None,
    expected_shadow_entered_at: datetime | None = None,
) -> bool:
    payload = {
        "schema_version": 1,
        "checked_at": _aware_utc(checked_at, "checked_at").isoformat(),
        "report_digest": report_digest(report),
        **asdict(report),
    }
    if cur is not None:
        app_settings.set_setting(_READINESS_REPORT_SETTING_KEY, payload, cur=cur)
        return True
    if expected_config is None:
        raise ValueError("expected_rollout_config_required")
    with db.cursor() as write_cur:
        attendance_location_policy.lock_rollout_decision_cur(write_cur)
        if not _rollout_persistence_is_current(
            write_cur,
            expected_config=expected_config,
            expected_shadow_entered_at=expected_shadow_entered_at,
        ):
            return False
        app_settings.set_setting(
            _READINESS_REPORT_SETTING_KEY,
            payload,
            cur=write_cur,
        )
    return True


def _boundary_matches_current_schedule(cutover_at: datetime, *, cur) -> bool:
    """Validate a saved boundary against the exact locked canonical schedule."""
    cutover = _aware_utc(cutover_at, "cutover_at")
    local = cutover.astimezone(shift_config.SITE_TZ)
    shift = shift_config.snapshot_for(local.date(), cur=cur)
    if not shift.is_workday:
        return False
    expected = datetime.combine(
        local.date(),
        shift.shift_start,
        tzinfo=shift_config.SITE_TZ,
    ).astimezone(UTC)
    return expected == cutover


def _settle_due_rollback(
    expected_config: attendance_location_policy.RolloutConfig,
    *,
    now_utc: datetime,
) -> ActivationResult:
    now = _aware_utc(now_utc, "now_utc")
    if expected_config.cutover_at is None or expected_config.cutover_at.astimezone(UTC) > now:
        return "not_due"
    with db.cursor() as cur:
        attendance_location_policy.lock_rollout_decision_cur(cur)
        _lock_production_config_sources_cur(cur)
        cur.execute(
            "SELECT value FROM app_settings WHERE key = %s FOR UPDATE",
            (_ROLLOUT_SETTING_KEY,),
        )
        row = cur.fetchone()
        if row is None:
            return "superseded"
        raw = row["value"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        try:
            current = attendance_location_policy._parse_config(raw)  # noqa: SLF001
        except (TypeError, ValueError, json.JSONDecodeError):
            return "superseded"
        if current != expected_config:
            return "superseded"
        if not _boundary_matches_current_schedule(
            expected_config.cutover_at,
            cur=cur,
        ):
            blocked = {
                "item_key": cutover_blocked_item_key(expected_config.cutover_at),
                "cutover_at": expected_config.cutover_at.astimezone(UTC).isoformat(),
                "checked_at": now.isoformat(),
                "report_digest": (
                    expected_config.live_gate.report_digest
                    if expected_config.live_gate is not None
                    else "rollback_boundary_changed"
                ),
                "blockers": ["rollback_boundary_changed"],
                "priority": "urgent",
            }
            app_settings.set_setting(_CUTOVER_BLOCKED_SETTING_KEY, blocked, cur=cur)
            _log.warning(
                "attendance rollback boundary changed",
                extra={"cutover_at": blocked["cutover_at"]},
            )
            return "superseded"
        attendance_location_policy.set_rollout_config(
            attendance_location_policy.RolloutConfig("shadow", None, None),
            cur=cur,
        )
        start_shadow_epoch_cur(cur, entered_at=now)
        _record_rollout_audit_cur(
            cur,
            event_kind="rolled_back",
            rollout_mode="shadow",
            cutover_at=expected_config.cutover_at,
            checked_at=now,
            report_fingerprint=(
                expected_config.live_gate.report_digest
                if expected_config.live_gate is not None
                else None
            ),
        )
        return "rolled_back"


def run_warmer_tick(now_utc: datetime, production_client) -> ActivationResult:
    """One multi-process-safe 30-second shadow/cutover maintenance tick."""
    now = _aware_utc(now_utc, "now_utc")
    config = attendance_location_policy.get_rollout_config_strict()
    gate = config.live_gate
    pending_active_rollback = False
    if (
        config.mode == "shadow"
        and config.cutover_at is not None
        and gate is not None
        and gate.activated_at is not None
    ):
        rollback_result = _settle_due_rollback(config, now_utc=now)
        if rollback_result != "not_due":
            return rollback_result
        pending_active_rollback = True
    if config.mode == "shadow" and not pending_active_rollback:
        with _shadow_refresh_claim() as claimed:
            if not claimed:
                return "busy"
            day = _previous_complete_workday(now)
            shadow_entered_at = _shadow_epoch_entered_at()
            try:
                comparison = refresh_shadow_comparison(
                    day,
                    production_client,
                    now_utc=now,
                    shadow_entered_at=shadow_entered_at,
                )
                if comparison is None:
                    return "not_due"
                _persist_readiness_report(
                    build_report(now, production_client=production_client),
                    checked_at=now,
                    expected_config=config,
                    expected_shadow_entered_at=shadow_entered_at,
                )
            except Exception:  # noqa: BLE001 - warmer remains fail-closed/alive
                _log.warning(
                    "attendance readiness shadow refresh failed",
                    extra={"day": day.isoformat()},
                )
            return "not_due"
    result = (
        "not_due"
        if pending_active_rollback
        else activate_due_cutover(now, production_client=production_client)
    )
    if result != "not_due":
        return result
    if gate is None:
        return result
    with _shadow_refresh_claim() as claimed:
        if not claimed:
            return "busy"
        try:
            shadow_entered_at = (
                _shadow_epoch_entered_at() if gate.activated_at is None else None
            )
            if gate.activated_at is None:
                comparison = refresh_shadow_comparison(
                    _previous_complete_workday(now),
                    production_client,
                    now_utc=now,
                    shadow_entered_at=shadow_entered_at,
                )
                if comparison is None:
                    return "not_due"
            _persist_readiness_report(
                build_report(now, production_client=production_client),
                checked_at=now,
                expected_config=config,
                expected_shadow_entered_at=shadow_entered_at,
            )
        except Exception:  # noqa: BLE001 - monitoring must remain fail-closed/alive
            _log.warning("attendance readiness live refresh failed")
    return result


def activate_due_cutover(
    now_utc: datetime,
    *,
    production_client=None,
) -> ActivationResult:
    """Recheck and atomically settle a due pending live cutover."""
    now = _aware_utc(now_utc, "now_utc")
    with _activation_claim() as claimed:
        if not claimed:
            return "busy"
        config = attendance_location_policy.get_rollout_config_strict()
        gate = config.live_gate
        if (
            config.mode != "live"
            or config.cutover_at is None
            or config.cutover_at.astimezone(UTC) > now
            or gate is None
            or gate.activated_at is not None
        ):
            return "not_due"
        try:
            decision = build_decision_snapshot(
                now,
                cutover_at=None,
                production_client=production_client,
            )
        except DecisionSourceChanged:
            return "superseded"
        return _settle_due_cutover(
            expected_config=config,
            report=decision.report,
            decision=decision,
            now_utc=now,
        )


def _collect_inputs(
    now_utc: datetime,
    production_client=None,
    *,
    frozen_leaderboard_rows=None,
    frozen_shadow_day: date | None = None,
    frozen_shadow_fingerprint: str | None = None,
    frozen_production_day: _FrozenProductionDay | None = None,
    frozen_shadow_production_day: _FrozenProductionDay | None = None,
) -> _ReadinessInputs:
    """Collect one bounded database/strict-source readiness snapshot."""
    db_metrics = _collect_db_metrics(now_utc)
    shadow_source_day = db_metrics.pop("shadow_source_day", None)
    shadow_source_binding = db_metrics.pop("shadow_source_binding", None)
    shadow_production_fingerprint = db_metrics.pop(
        "shadow_production_fingerprint", None
    )
    if db_metrics.get("shadow_day_complete"):
        try:
            db_metrics["shadow_day_complete"] = bool(
                isinstance(shadow_source_day, date)
                and shadow_source_binding
                and shadow_production_fingerprint
                and frozen_shadow_day == shadow_source_day
                and frozen_shadow_fingerprint == shadow_production_fingerprint
                and _combined_shadow_source_binding(
                    _shadow_source_fingerprint(
                        shadow_source_day,
                        frozen_production_day=frozen_shadow_production_day,
                    ),
                    frozen_shadow_fingerprint,
                )
                == shadow_source_binding
            )
        except Exception:  # noqa: BLE001 - stale/unreadable proof fails closed
            db_metrics["shadow_day_complete"] = False
    identifier_metrics = {
        key: db_metrics.pop(key, ())
        for key in (
            "correction_job_ids",
            "repair_attendance_ids",
            "recalculation_ids",
        )
    }
    db_metrics.setdefault("projection_lag_seconds", None)
    db_metrics.setdefault("shadow_day_complete", False)
    day = now_utc.astimezone(shift_config.SITE_TZ).date()
    resolved_client = production_client
    if resolved_client is None:
        from .deps import client

        resolved_client = client
    strict_kwargs = {}
    if frozen_leaderboard_rows is not None:
        strict_kwargs["leaderboard_rows"] = frozen_leaderboard_rows
    if frozen_production_day is not None:
        strict_kwargs["shift_bounds"] = (
            frozen_production_day.shift_start_utc,
            frozen_production_day.shift_end_utc,
        )
        strict_kwargs["break_windows"] = frozen_production_day.break_windows
        strict_kwargs["source_config_fingerprint"] = (
            frozen_production_day.source_fingerprint
        )
    strict_inputs = production_history._strict_inputs_for_day(  # noqa: SLF001
        day,
        resolved_client,
        now_utc=now_utc,
        map_work_center=_snapshot_work_center_mapper(),
        **strict_kwargs,
    )
    strict_attribution = production_history._strict_attribution_from_inputs(  # noqa: SLF001
        day,
        strict_inputs,
    )
    unassigned_runs = production_history._strict_unassigned_runs_from_inputs(  # noqa: SLF001
        day,
        strict_inputs,
        now_utc=now_utc,
    )
    issue_metrics = _strict_issue_metrics(
        getattr(strict_inputs, "location_spans", strict_inputs.segments),
        unassigned_runs,
        now_utc=now_utc,
    )
    _log_readiness_identifiers(
        getattr(strict_inputs, "location_spans", strict_inputs.segments),
        identifier_metrics,
        now_utc=now_utc,
    )
    comparison = _compare_strict_to_current(
        day,
        strict_attribution,
        now_utc=now_utc,
    )
    projection_complete = issue_metrics.projection_complete and comparison.complete
    return _ReadinessInputs(
        **db_metrics,
        projection_complete=projection_complete,
        open_conflicts=issue_metrics.open_conflicts,
        conflict_minutes_today=issue_metrics.conflict_minutes_today,
        open_unmapped=issue_metrics.open_unmapped,
        unmapped_minutes_today=issue_metrics.unmapped_minutes_today,
        open_missing_required=issue_metrics.open_missing_required,
        missing_minutes_today=issue_metrics.missing_minutes_today,
        unassigned_units_today=issue_metrics.unassigned_units_today,
        oldest_unassigned_age_seconds=issue_metrics.oldest_unassigned_age_seconds,
        shadow_changed_worker_units=comparison.changed_worker_units,
        unmapped_affects_production=issue_metrics.unmapped_affects_production,
        missing_affects_production=issue_metrics.missing_affects_production,
        comparison_identity_available=issue_metrics.comparison_identity_available,
        frozen_production_fingerprint=_frozen_production_fingerprint(strict_inputs),
    )


def _settle_due_cutover(
    *,
    expected_config: attendance_location_policy.RolloutConfig,
    report: ReadinessReport,
    decision: DecisionSnapshot | None = None,
    now_utc: datetime,
) -> ActivationResult:
    """Atomically activate or roll back one exact pending gate."""
    now = _aware_utc(now_utc, "now_utc")
    expected_gate = expected_config.live_gate
    if expected_config.cutover_at is None or expected_gate is None:
        return "superseded"
    cutover = expected_config.cutover_at.astimezone(UTC)
    cutover_day = expected_config.cutover_at.astimezone(shift_config.SITE_TZ).date()
    with db.cursor() as cur:
        attendance_location_policy.lock_rollout_decision_cur(cur)
        cur.execute(
            "SELECT value FROM app_settings WHERE key = %s FOR UPDATE",
            (_ROLLOUT_SETTING_KEY,),
        )
        row = cur.fetchone()
        if row is None:
            return "superseded"
        raw = row["value"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        try:
            current = attendance_location_policy._parse_config(raw)  # noqa: SLF001
        except (TypeError, ValueError, json.JSONDecodeError):
            return "superseded"
        if current != expected_config:
            return "superseded"
        if decision is not None:
            _lock_readiness_sources_cur(cur)
            if not _decision_matches_sources(
                decision,
                _source_fingerprints(now_utc=now, cur=cur),
            ):
                return "superseded"
            cur.execute("SELECT clock_timestamp() AS accepted_at")
            accepted_row = cur.fetchone()
            accepted_at = _aware_utc(accepted_row["accepted_at"], "accepted_at")
            if accepted_at < now or not _decision_is_fresh(decision, accepted_at):
                return "superseded"
            now = accepted_at
        else:
            _lock_production_config_sources_cur(cur)

        boundary_matches = _boundary_matches_current_schedule(
            expected_config.cutover_at,
            cur=cur,
        )

        boundary_missed = now > cutover + _ACTIVATION_BOUNDARY_WINDOW
        blocked_codes = list(report.blockers)
        if boundary_missed and "cutover_boundary_missed" not in blocked_codes:
            blocked_codes.append("cutover_boundary_missed")
        if not boundary_matches and "cutover_boundary_changed" not in blocked_codes:
            blocked_codes.append("cutover_boundary_changed")
        blocked_codes = tuple(blocked_codes)

        if report.ready and not boundary_missed and boundary_matches:
            digest = decision.report_digest if decision is not None else report_digest(report)
            activated = attendance_location_policy.RolloutConfig(
                mode="live",
                cutover_at=expected_config.cutover_at,
                live_gate=attendance_location_policy.LiveGate(
                    checked_at=now,
                    report_digest=digest,
                    activated_at=now,
                ),
            )
            attendance_location_policy.set_rollout_config(activated, cur=cur)
            cur.execute(
                "INSERT INTO attendance_strict_days (day, reason, source_changed_at) "
                "VALUES (%s, %s, %s) ON CONFLICT (day) DO NOTHING",
                (cutover_day, "live_cutover", now),
            )
            attendance_mirror._enqueue_recalc_cur(  # noqa: SLF001
                cur,
                (cutover_day,),
                "live_cutover",
                requested_at=now,
            )
            _persist_readiness_report(report, checked_at=now, cur=cur)
            _record_rollout_audit_cur(
                cur,
                event_kind="live_activated",
                rollout_mode="live",
                cutover_at=cutover,
                checked_at=now,
                report_fingerprint=digest,
            )
            cur.execute(
                "DELETE FROM app_settings WHERE key = %s",
                (_CUTOVER_BLOCKED_SETTING_KEY,),
            )
            _log.info(
                "attendance cutover activated",
                extra={
                    "cutover_at": cutover.isoformat(),
                    "recalculation_day": cutover_day.isoformat(),
                    "report_digest": digest,
                },
            )
            return "activated"

        rolled_back = attendance_location_policy.RolloutConfig("shadow", None, None)
        attendance_location_policy.set_rollout_config(rolled_back, cur=cur)
        blocked = {
            "item_key": cutover_blocked_item_key(cutover),
            "cutover_at": cutover.isoformat(),
            "checked_at": now.isoformat(),
            "report_digest": report_digest(report),
            "blockers": list(blocked_codes),
            "priority": "urgent",
        }
        app_settings.set_setting(_CUTOVER_BLOCKED_SETTING_KEY, blocked, cur=cur)
        _record_rollout_audit_cur(
            cur,
            event_kind="live_blocked",
            rollout_mode="shadow",
            cutover_at=cutover,
            checked_at=now,
            report_fingerprint=blocked["report_digest"],
            blocker_codes=blocked_codes,
        )
        _log.warning(
            "attendance cutover blocked",
            extra={
                "cutover_at": cutover.isoformat(),
                "exception_key": blocked["item_key"],
                "report_digest": blocked["report_digest"],
            },
        )
        return "rolled_back"


def cutover_blocked_item_key(cutover_at: datetime) -> str:
    return inbox_keys.attendance_cutover_blocked(cutover_at)


__all__ = [
    "ReadinessReport",
    "DecisionSnapshot",
    "DecisionSourceChanged",
    "activate_due_cutover",
    "build_decision_snapshot",
    "build_report",
    "report_digest",
    "report_json",
    "run_warmer_tick",
    "parse_local_cutover",
    "schedule_live_cutover",
    "validate_cutover",
]
