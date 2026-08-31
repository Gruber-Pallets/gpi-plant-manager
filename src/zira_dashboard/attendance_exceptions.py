"""Stable, local-only exceptions from the canonical attendance timeline."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from . import (
    attendance_location_policy,
    attendance_mirror,
    attendance_timeline,
    db,
    inbox_keys,
    shift_config,
    wc_attributions,
)


ExceptionPriority = Literal["urgent", "warn", "muted"]
ProductionMode = Literal["legacy", "shadow", "strict", "pending"]

_SOURCE_STALE_AFTER = timedelta(seconds=90)
_FIRST_LOCATION_GRACE = timedelta(minutes=5)
_TIMELINE_SOURCE = "Attendance Timeline"
_PRODUCTION_SOURCE = "Strict Production"
_DATABASE_URL_MISSING = "DATABASE_URL is not set. Postgres connection cannot be initialized."


@dataclass(frozen=True)
class AttendanceException:
    kind: str
    item_key: str
    employee_odoo_id: int | None
    employee_name: str | None
    attendance_ids: tuple[int, ...]
    start_utc: datetime
    end_utc: datetime | None
    raw_work_center_labels: tuple[str, ...]
    odoo_work_center_ids: tuple[int, ...]
    affected_workers: tuple[tuple[int, str], ...]
    app_work_center_name: str | None
    units: float | None
    sample_count: int | None
    reason: str
    priority: ExceptionPriority
    comparison_only: bool
    target_odoo_department_id: int | None
    end_is_open: bool = False


@dataclass(frozen=True)
class AttendanceExceptionSnapshot:
    day: date
    mode: attendance_location_policy.Mode
    production_mode: ProductionMode
    baseline_complete: bool
    fresh: bool
    complete: bool
    issues: tuple[AttendanceException, ...]
    source_errors: tuple[str, ...]

    def issues_for(self, kind: str) -> tuple[AttendanceException, ...]:
        return tuple(issue for issue in self.issues if issue.kind == kind)


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _plant_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=shift_config.SITE_TZ)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=shift_config.SITE_TZ)
    return start.astimezone(UTC), end.astimezone(UTC)


def _is_database_less(exc: Exception) -> bool:
    return str(exc) == _DATABASE_URL_MISSING


def _policy_snapshot_for_day(
    day: date,
    *,
    now_utc: datetime,
) -> tuple[
    attendance_location_policy.RolloutConfig,
    attendance_location_policy.MatchState,
    str | None,
]:
    """Read policy inputs once and derive one immutable day decision."""
    policy_error: str | None = None
    try:
        config = attendance_location_policy.get_rollout_config()
    except Exception as exc:  # noqa: BLE001 - uncertainty must stay actionable
        config = attendance_location_policy.RolloutConfig("off", None, None)
        if not _is_database_less(exc):
            policy_error = str(exc) or "attendance rollout config is unavailable"

    try:
        strict_day = day in attendance_location_policy.strict_days()
    except Exception as exc:  # noqa: BLE001 - uncertainty must stay actionable
        strict_day = False
        if not _is_database_less(exc) and policy_error is None:
            policy_error = str(exc) or "attendance strict-day marker is unavailable"

    if policy_error is not None:
        return config, "pending", policy_error
    if strict_day:
        return config, "strict", None
    try:
        match_state = attendance_location_policy._match_state_from_config(  # noqa: SLF001
            day,
            config=config,
            now_utc=now_utc,
        )
    except Exception as exc:  # noqa: BLE001 - uncertainty must stay actionable
        return config, "pending", str(exc) or "attendance rollout state is unavailable"
    return config, match_state, None


def _raw_by_attendance_id(rows: Sequence[Mapping]) -> dict[int, Mapping]:
    return {
        int(row["odoo_attendance_id"]): row
        for row in rows
        if row.get("odoo_attendance_id") is not None
    }


def _raw_identity(
    attendance_ids: Sequence[int], raw_by_id: Mapping[int, Mapping]
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    labels = {
        str(row["odoo_work_center_name"])
        for attendance_id in attendance_ids
        if (row := raw_by_id.get(attendance_id)) is not None
        and row.get("odoo_work_center_name") not in (None, "")
    }
    work_center_ids = {
        int(row["odoo_work_center_id"])
        for attendance_id in attendance_ids
        if (row := raw_by_id.get(attendance_id)) is not None
        and row.get("odoo_work_center_id") is not None
    }
    return tuple(sorted(labels)), tuple(sorted(work_center_ids))


def _issue_for_span(
    kind: str,
    span: attendance_timeline.LocationSpan,
    *,
    raw_by_id: Mapping[int, Mapping],
    reason: str,
    priority: ExceptionPriority,
    now_utc: datetime,
) -> AttendanceException:
    labels, work_center_ids = _raw_identity(span.attendance_ids, raw_by_id)
    if span.odoo_work_center_name and span.odoo_work_center_name not in labels:
        labels = tuple(sorted((*labels, span.odoo_work_center_name)))
    if span.odoo_work_center_id is not None and span.odoo_work_center_id not in work_center_ids:
        work_center_ids = tuple(sorted((*work_center_ids, span.odoo_work_center_id)))
    end_is_open = bool(
        span.end_utc >= now_utc
        and any(
            (row := raw_by_id.get(attendance_id)) is not None and row.get("check_out_utc") is None
            for attendance_id in span.attendance_ids
        )
    )
    return AttendanceException(
        kind=kind,
        item_key=inbox_keys.attendance_issue_key(
            kind, span.employee_odoo_id, span.attendance_ids, span.start_utc
        ),
        employee_odoo_id=span.employee_odoo_id,
        employee_name=span.employee_name,
        attendance_ids=span.attendance_ids,
        start_utc=span.start_utc,
        end_utc=span.end_utc,
        raw_work_center_labels=labels,
        odoo_work_center_ids=work_center_ids,
        affected_workers=((span.employee_odoo_id, span.employee_name),),
        app_work_center_name=span.app_work_center_name,
        units=None,
        sample_count=None,
        reason=reason,
        priority=priority,
        comparison_only=False,
        target_odoo_department_id=None,
        end_is_open=end_is_open,
    )


def _missing_location_issues(
    spans: Sequence[attendance_timeline.LocationSpan],
    *,
    raw_by_id: Mapping[int, Mapping],
    now_utc: datetime,
) -> list[AttendanceException]:
    issues: list[AttendanceException] = []
    by_employee: dict[int, list[attendance_timeline.LocationSpan]] = {}
    for span in spans:
        by_employee.setdefault(span.employee_odoo_id, []).append(span)

    for employee_spans in by_employee.values():
        ordered = sorted(employee_spans, key=lambda value: (value.start_utc, value.end_utc))
        seen_real_location = False
        index = 0
        while index < len(ordered):
            span = ordered[index]
            if span.status not in ("pending_first_location", "missing_required_location"):
                _raw_labels, raw_work_center_ids = _raw_identity(span.attendance_ids, raw_by_id)
                if span.odoo_work_center_id is not None or raw_work_center_ids:
                    seen_real_location = True
                index += 1
                continue

            group = [span]
            index += 1
            while index < len(ordered):
                candidate = ordered[index]
                if (
                    candidate.status not in ("pending_first_location", "missing_required_location")
                    or candidate.start_utc != group[-1].end_utc
                    or candidate.attendance_ids != group[-1].attendance_ids
                ):
                    break
                group.append(candidate)
                index += 1

            contains_required = any(value.status == "missing_required_location" for value in group)
            is_still_open = group[-1].end_utc >= now_utc
            if not contains_required and not is_still_open:
                # Luke supplied the first location inside grace. There is no
                # location correction to make; any production gap stands on
                # its own as a run exception.
                continue

            start = group[0].start_utc
            end = group[-1].end_utc
            later_gap = seen_real_location
            urgent = later_gap or now_utc >= start + _FIRST_LOCATION_GRACE
            merged = replace(group[0], start_utc=start, end_utc=end)
            issue = _issue_for_span(
                "attendance_missing_location",
                merged,
                raw_by_id=raw_by_id,
                reason=(
                    "required_work_center_missing_after_location"
                    if later_gap
                    else "first_required_work_center_missing"
                ),
                priority="urgent" if urgent else "warn",
                now_utc=now_utc,
            )
            issues.append(issue)
    return issues


def _timeline_issues(
    spans: Sequence[attendance_timeline.LocationSpan],
    *,
    raw_by_id: Mapping[int, Mapping],
    now_utc: datetime,
) -> list[AttendanceException]:
    issues = _missing_location_issues(spans, raw_by_id=raw_by_id, now_utc=now_utc)
    for span in spans:
        if span.status == "unmapped_location":
            issues.append(
                _issue_for_span(
                    "attendance_unmapped_location",
                    span,
                    raw_by_id=raw_by_id,
                    reason="odoo_work_center_is_not_mapped",
                    priority="urgent",
                    now_utc=now_utc,
                )
            )
        elif span.status == "conflicting_location":
            issues.append(
                _issue_for_span(
                    "attendance_conflicting_location",
                    span,
                    raw_by_id=raw_by_id,
                    reason="different_work_centers_overlap",
                    priority="urgent",
                    now_utc=now_utc,
                )
            )
        elif span.status == "valid" and span.odoo_work_center_id is not None:
            duplicate_ids = tuple(
                attendance_id
                for attendance_id in span.attendance_ids
                if (
                    (row := raw_by_id.get(attendance_id)) is not None
                    and row.get("odoo_work_center_id") == span.odoo_work_center_id
                )
            )
            if len(duplicate_ids) < 2:
                continue
            issues.append(
                _issue_for_span(
                    "attendance_duplicate_location",
                    replace(span, attendance_ids=duplicate_ids),
                    raw_by_id=raw_by_id,
                    reason="same_work_center_duplicate_overlap",
                    priority="muted",
                    now_utc=now_utc,
                )
            )
    return issues


def _failed_department_repairs(start_utc: datetime, end_utc: datetime) -> tuple[Mapping, ...]:
    return tuple(
        db.query(
            "SELECT r.odoo_attendance_id, r.target_odoo_department_id, r.last_error, "
            "m.employee_odoo_id, m.employee_name, m.check_in_utc, m.check_out_utc, "
            "m.odoo_work_center_id, m.odoo_work_center_name, "
            "w.name AS app_work_center_name "
            "FROM attendance_department_repairs r "
            "JOIN odoo_attendance_mirror m "
            "ON m.odoo_attendance_id = r.odoo_attendance_id "
            "LEFT JOIN work_centers w "
            "ON w.odoo_work_center_id = m.odoo_work_center_id "
            "WHERE r.status = 'failed' AND m.deleted_at IS NULL "
            "AND m.check_in_utc < %s "
            "AND (m.check_out_utc IS NULL OR m.check_out_utc > %s) "
            "ORDER BY r.odoo_attendance_id",
            (end_utc, start_utc),
        )
    )


def _repair_issues(rows: Sequence[Mapping]) -> list[AttendanceException]:
    issues = []
    for row in rows:
        attendance_id = int(row["odoo_attendance_id"])
        employee_id = int(row["employee_odoo_id"])
        start = _aware_utc(row["check_in_utc"], "repair check_in_utc")
        end = row.get("check_out_utc")
        if end is not None:
            end = _aware_utc(end, "repair check_out_utc")
        label = row.get("odoo_work_center_name")
        wc_id = row.get("odoo_work_center_id")
        issues.append(
            AttendanceException(
                kind="attendance_department_repair_failed",
                item_key=inbox_keys.attendance_issue_key(
                    "attendance_department_repair_failed",
                    employee_id,
                    (attendance_id,),
                    start,
                ),
                employee_odoo_id=employee_id,
                employee_name=str(row.get("employee_name") or f"Worker #{employee_id}"),
                attendance_ids=(attendance_id,),
                start_utc=start,
                end_utc=end,
                raw_work_center_labels=(str(label),) if label else (),
                odoo_work_center_ids=(int(wc_id),) if wc_id is not None else (),
                affected_workers=(
                    (employee_id, str(row.get("employee_name") or f"Worker #{employee_id}")),
                ),
                app_work_center_name=row.get("app_work_center_name"),
                units=None,
                sample_count=None,
                reason=str(row.get("last_error") or "department repair failed"),
                priority="urgent",
                comparison_only=False,
                target_odoo_department_id=int(row["target_odoo_department_id"]),
                end_is_open=end is None,
            )
        )
    return issues


def _stale_issue(
    health: attendance_mirror.MirrorHealth, *, day: date, now_utc: datetime
) -> AttendanceException:
    verified = health.last_incremental_completed_at or health.baseline_completed_at
    start = (
        _aware_utc(verified, "verified freshness") + _SOURCE_STALE_AFTER
        if verified is not None
        else _plant_day_bounds(day)[0]
    )
    return AttendanceException(
        kind="attendance_source_stale",
        item_key=inbox_keys.attendance_source_stale_key(),
        employee_odoo_id=None,
        employee_name=None,
        attendance_ids=(),
        start_utc=start,
        end_utc=now_utc,
        raw_work_center_labels=(),
        odoo_work_center_ids=(),
        affected_workers=(),
        app_work_center_name=None,
        units=None,
        sample_count=None,
        reason="odoo_attendance_mirror_is_stale",
        priority="urgent",
        comparison_only=False,
        target_odoo_department_id=None,
        end_is_open=True,
    )


def _production_issues(
    day: date,
    *,
    now_utc: datetime,
    config: attendance_location_policy.RolloutConfig,
    match_state: attendance_location_policy.MatchState,
    production_client,
    spans: Sequence[attendance_timeline.LocationSpan],
) -> tuple[ProductionMode, list[AttendanceException], str | None]:
    if match_state == "legacy":
        if config.mode != "shadow":
            return "legacy", [], None
        try:
            runs = wc_attributions.shadow_unassigned_runs_for_day(
                day, production_client, now_utc=now_utc
            )
        except Exception as exc:  # noqa: BLE001 - failed comparison stays visible
            reason = str(exc) or "strict shadow production source is unavailable"
            return (
                "shadow",
                [_production_unavailable_issue(day, now_utc, reason, comparison_only=True)],
                reason,
            )
        return "shadow", _run_issues(runs, spans=spans, comparison=True), None
    if match_state == "pending":
        error = f"strict production cutover is pending for {day.isoformat()}"
        return (
            "pending",
            [_production_unavailable_issue(day, now_utc, error, comparison_only=False)],
            error,
        )

    # A recorded strict day stays live-strict even while the global setting is
    # shadow (for example, after a clean-boundary rollback). Only the explicit
    # legacy-state branch above is a shadow comparison.
    comparison = False
    production_mode: ProductionMode = "strict"
    try:
        runs = wc_attributions.shadow_unassigned_runs_for_day(
            day, production_client, now_utc=now_utc
        )
    except Exception as exc:  # noqa: BLE001 - any strict read failure is actionable
        reason = str(exc) or "strict production source is unavailable"
        return (
            production_mode,
            [_production_unavailable_issue(day, now_utc, reason, comparison_only=False)],
            reason,
        )

    return production_mode, _run_issues(runs, spans=spans, comparison=comparison), None


def _run_issues(
    runs,
    *,
    spans: Sequence[attendance_timeline.LocationSpan],
    comparison: bool,
) -> list[AttendanceException]:
    issues: list[AttendanceException] = []
    for run in runs:
        if run.start_utc == run.end_utc:
            overlaps = [span for span in spans if span.start_utc <= run.start_utc < span.end_utc]
        else:
            overlaps = [
                span
                for span in spans
                if span.start_utc < run.end_utc and span.end_utc > run.start_utc
            ]
        affected_workers = tuple(
            sorted(
                {(span.employee_odoo_id, span.employee_name) for span in overlaps},
                key=lambda value: value[0],
            )
        )
        issues.append(
            AttendanceException(
                kind="production_unassigned_run",
                item_key=inbox_keys.production_run_key(run.wc_name, run.start_utc),
                employee_odoo_id=None,
                employee_name=None,
                attendance_ids=(),
                start_utc=run.start_utc,
                end_utc=run.end_utc,
                raw_work_center_labels=(),
                odoo_work_center_ids=(),
                affected_workers=affected_workers,
                app_work_center_name=run.wc_name,
                units=float(run.units),
                sample_count=int(run.sample_count),
                reason="positive_production_has_no_valid_odoo_worker",
                priority="urgent",
                comparison_only=comparison,
                target_odoo_department_id=None,
                end_is_open=False,
            )
        )
    return issues


def _production_unavailable_issue(
    day: date,
    now_utc: datetime,
    reason: str,
    *,
    comparison_only: bool,
) -> AttendanceException:
    return AttendanceException(
        kind="production_source_unavailable",
        item_key=inbox_keys.production_source_unavailable(day),
        employee_odoo_id=None,
        employee_name=None,
        attendance_ids=(),
        start_utc=_plant_day_bounds(day)[0],
        end_utc=now_utc,
        raw_work_center_labels=(),
        odoo_work_center_ids=(),
        affected_workers=(),
        app_work_center_name=None,
        units=None,
        sample_count=None,
        reason=reason,
        priority="urgent",
        comparison_only=comparison_only,
        target_odoo_department_id=None,
        end_is_open=True,
    )


def _shared_production_client():
    from .deps import client

    return client


def _production_mode_for(
    config: attendance_location_policy.RolloutConfig,
    match_state: attendance_location_policy.MatchState,
) -> ProductionMode:
    if match_state == "strict":
        return "strict"
    if match_state == "pending":
        return "pending"
    return "shadow" if config.mode == "shadow" else "legacy"


def _strict_source_problem(
    day: date,
    now_utc: datetime,
    reason: str,
) -> AttendanceException:
    return _production_unavailable_issue(
        day,
        now_utc,
        reason,
        comparison_only=False,
    )


def build_snapshot(
    day: date,
    *,
    now_utc: datetime,
    production_client=None,
) -> AttendanceExceptionSnapshot:
    """Build one deterministic, degraded-aware local exception snapshot."""
    if not isinstance(day, date) or isinstance(day, datetime):
        raise TypeError("day must be a date")
    now = _aware_utc(now_utc, "now_utc")
    config, match_state, policy_error = _policy_snapshot_for_day(day, now_utc=now)
    production_mode = _production_mode_for(config, match_state)
    if config.mode == "off" and match_state == "legacy":
        return AttendanceExceptionSnapshot(day, "off", "legacy", False, False, False, (), ())

    issues: list[AttendanceException] = []
    source_errors: list[str] = []
    if policy_error is not None:
        issues.append(_strict_source_problem(day, now, policy_error))
        source_errors.append(_PRODUCTION_SOURCE)

    try:
        health = attendance_mirror.health_snapshot()
    except Exception as exc:  # noqa: BLE001 - degraded state must be explicit and non-resolving
        if production_mode in ("strict", "pending") and policy_error is None:
            issues.append(
                _strict_source_problem(
                    day, now, str(exc) or "attendance mirror health is unavailable"
                )
            )
            source_errors.append(_PRODUCTION_SOURCE)
        source_errors.append(_TIMELINE_SOURCE)
        return AttendanceExceptionSnapshot(
            day,
            config.mode,
            production_mode,
            False,
            False,
            False,
            tuple(issues),
            tuple(dict.fromkeys(source_errors)),
        )
    baseline_complete = health.baseline_completed_at is not None
    if not baseline_complete:
        if production_mode in ("strict", "pending") and policy_error is None:
            issues.append(
                _strict_source_problem(
                    day, now, "Odoo attendance baseline is unavailable for strict production"
                )
            )
            source_errors.append(_PRODUCTION_SOURCE)
        return AttendanceExceptionSnapshot(
            day,
            config.mode,
            production_mode,
            False,
            False,
            False,
            tuple(issues),
            tuple(dict.fromkeys(source_errors)),
        )

    verified = health.last_incremental_completed_at
    source_age_stale = bool(
        verified is None
        or now - _aware_utc(verified, "last_incremental_completed_at") > _SOURCE_STALE_AFTER
    )
    fresh = not source_age_stale and health.last_error is None
    if source_age_stale:
        issues.append(_stale_issue(health, day=day, now_utc=now))
    if not fresh:
        source_errors.append(_TIMELINE_SOURCE)

    day_start, day_end = _plant_day_bounds(day)
    as_of = min(max(now, day_start), day_end)
    spans: tuple[attendance_timeline.LocationSpan, ...] = ()
    if as_of > day_start:
        try:
            raw_rows = attendance_mirror.rows_overlapping(day_start, day_end)
            spans = attendance_timeline.timeline_for_range(day_start, day_end, as_of_utc=as_of)
            raw_by_id = _raw_by_attendance_id(raw_rows)
            issues.extend(_timeline_issues(spans, raw_by_id=raw_by_id, now_utc=as_of))
            issues.extend(_repair_issues(_failed_department_repairs(day_start, day_end)))
        except Exception:  # noqa: BLE001 - no partial projection may resolve items
            if _TIMELINE_SOURCE not in source_errors:
                source_errors.append(_TIMELINE_SOURCE)

    if fresh and _TIMELINE_SOURCE not in source_errors and policy_error is None:
        try:
            resolved_production_client = production_client
            if resolved_production_client is None and production_mode in (
                "shadow",
                "strict",
            ):
                resolved_production_client = _shared_production_client()
            production_mode, production_issues, production_error = _production_issues(
                day,
                now_utc=now,
                config=config,
                match_state=match_state,
                production_client=resolved_production_client,
                spans=spans,
            )
            issues.extend(production_issues)
            if production_error:
                source_errors.append(_PRODUCTION_SOURCE)
        except Exception as exc:  # noqa: BLE001 - unknown source errors stay visible
            if production_mode != "legacy":
                issues.append(
                    _production_unavailable_issue(
                        day,
                        now,
                        str(exc) or "strict production source is unavailable",
                        comparison_only=production_mode == "shadow",
                    )
                )
            source_errors.append(_PRODUCTION_SOURCE)
    elif production_mode in ("strict", "pending") and policy_error is None:
        issues.append(
            _strict_source_problem(
                day,
                now,
                "Odoo attendance source is unavailable for strict production",
            )
        )
        source_errors.append(_PRODUCTION_SOURCE)

    issues.sort(key=lambda issue: (issue.start_utc, issue.kind, issue.item_key))
    complete = fresh and not source_errors
    return AttendanceExceptionSnapshot(
        day=day,
        mode=config.mode,
        production_mode=production_mode,
        baseline_complete=True,
        fresh=fresh,
        complete=complete,
        issues=tuple(issues),
        source_errors=tuple(dict.fromkeys(source_errors)),
    )


__all__ = [
    "AttendanceException",
    "AttendanceExceptionSnapshot",
    "build_snapshot",
]
