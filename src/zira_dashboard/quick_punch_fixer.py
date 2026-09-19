"""I/O tick that turns settled quick-punch merges into Odoo correction jobs.

``run_for_day`` is shared by the 60-second warmer and the pay-period backfill.
It never talks to Odoo itself for the merge: Preview writes an archive event,
and Live creates a ``merge`` correction job for the existing worker.

Pure planning lives in ``quick_punch_fixes``. This module owns freshness,
payroll, active-job, and dedupe guards.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
import logging

from . import (
    attendance_corrections,
    attendance_location_snapshot,
    inbox_log,
    odoo_client,
    plant_day,
    production_history,
    quick_punch_fix_settings,
    quick_punch_fixes,
    quick_punch_smoothing,
    shift_config,
    staffing_hours,
)
from .attendance_timeline import LocationSpan
from .quick_punch_fixes import FixScan, MeterFreshness, PlannedFix

log = logging.getLogger(__name__)

_PREVIEW_ACTION = "quick_punch_would_merge"
_PREVIEW_OUTCOME = "Preview — Odoo not changed"


@dataclass(frozen=True)
class FixRun:
    scan: FixScan
    payroll_skipped: frozenset[int]
    active_job_skipped: frozenset[int]
    deduped_item_keys: frozenset[str]
    eligible: tuple[PlannedFix, ...]
    applied_item_keys: frozenset[str]


def _empty_run(scan: FixScan | None = None) -> FixRun:
    return FixRun(
        scan=scan if scan is not None else FixScan((), ()),
        payroll_skipped=frozenset(),
        active_job_skipped=frozenset(),
        deduped_item_keys=frozenset(),
        eligible=(),
        applied_item_keys=frozenset(),
    )


def _breaks_utc(day: date) -> tuple[tuple[datetime, datetime], ...]:
    windows: list[tuple[datetime, datetime]] = []
    for break_window in shift_config.breaks_for(day):
        start = datetime.combine(day, break_window.start, tzinfo=shift_config.SITE_TZ).astimezone(
            UTC
        )
        end = datetime.combine(day, break_window.end, tzinfo=shift_config.SITE_TZ).astimezone(UTC)
        if end > start:
            windows.append((start, end))
    return tuple(windows)


def _meter_inputs(client, day: date, now_utc: datetime, today: date):
    results = production_history._metered_leaderboard(
        client, day, now_utc=now_utc if day == today else None
    )
    samples = {row.station.name: list(row.samples) for row in results}
    production_times = quick_punch_smoothing.production_times_from_samples(samples)
    freshness = {
        row.station.name: MeterFreshness(row.last_reading_at, bool(row.truncated))
        for row in results
    }
    return production_times, freshness


def _spans_for_day(
    day: date,
    now_utc: datetime,
    spans: Sequence[LocationSpan] | None,
) -> tuple[LocationSpan, ...] | None:
    if spans is not None:
        return tuple(spans)
    snapshot = attendance_location_snapshot.read_location_snapshot(day, as_of_utc=now_utc)
    policy = snapshot.policy
    if not policy.mirror_owned or not policy.available or policy.stale:
        return None
    return tuple(snapshot.spans)


def _payroll_blocked(employee_ids: Sequence[int], day: date) -> set[int] | None:
    """Employees payroll has locked, or None when that status cannot be proven."""
    if not employee_ids:
        return set()
    try:
        entries = odoo_client.fetch_payroll_work_entries(list(employee_ids), day, day)
    except Exception:
        log.warning("quick-punch fixer skipped payroll check for %s", day)
        return None
    blocked: set[int] = set()
    for entry in entries:
        employee = entry.get("employee_id")
        if not isinstance(employee, int):
            continue
        if entry.get("state") != "draft" or entry.get("conflict"):
            blocked.add(employee)
    return blocked


def _interval_overlaps(
    start: datetime,
    end: datetime | None,
    range_start: datetime,
    range_end: datetime | None,
) -> bool:
    if end is not None and end <= range_start:
        return False
    if range_end is not None and start >= range_end:
        return False
    return True


def _source_rows_match_fix(plan, fix: PlannedFix) -> bool:
    """Whether live Odoo still has the same rows the mirror-based fix saw."""
    ids: set[int] = set()
    has_open = False
    for row in plan.source_intervals:
        row_start = row["check_in_utc"]
        row_end = row.get("check_out_utc")
        if not _interval_overlaps(row_start, row_end, fix.start_utc, fix.end_utc):
            continue
        ids.add(int(row["odoo_attendance_id"]))
        if row_end is None:
            has_open = True
    return ids == set(fix.source_attendance_ids) and has_open is (fix.end_utc is None)


def _record_preview(fix: PlannedFix) -> None:
    inbox_log.record_event(
        item_kind=attendance_corrections.QUICK_PUNCH_ITEM_KIND,
        item_key=fix.item_key,
        person_name=fix.person_name,
        category_label=attendance_corrections.QUICK_PUNCH_CATEGORY_LABEL,
        action=_PREVIEW_ACTION,
        outcome=_PREVIEW_OUTCOME,
        before_value=quick_punch_fixes.before_summary(fix),
        after_value=quick_punch_fixes.after_summary(fix),
        actor_upn=attendance_corrections.QUICK_PUNCH_ACTOR_UPN,
        actor_name=attendance_corrections.QUICK_PUNCH_ACTOR_NAME,
        source="auto",
    )


def _create_live_job(fix: PlannedFix) -> bool:
    """Create the merge job. False when live Odoo no longer matches, or no-op."""
    try:
        preview = attendance_corrections.correction_preview(
            item_key=fix.item_key,
            employee_odoo_ids=[fix.employee_odoo_id],
            target_work_center_name=fix.wc_name,
            start_utc=fix.start_utc,
            end_utc=fix.end_utc,
            merge=True,
        )
    except ValueError:
        log.info(
            "quick-punch fixer skipped %s: live Odoo no longer matches the detected merge",
            fix.item_key,
        )
        return False
    if not preview.plans:
        return False
    plan = preview.plans[0]
    if not plan.operations:
        return False
    if not _source_rows_match_fix(plan, fix):
        log.info(
            "quick-punch fixer skipped %s: live source rows do not match the mirror",
            fix.item_key,
        )
        return False
    attendance_corrections.create_job_from_preview(
        preview=preview,
        actor_email=attendance_corrections.QUICK_PUNCH_ACTOR_UPN,
        actor_name=attendance_corrections.QUICK_PUNCH_ACTOR_NAME,
        audit_summary={
            "person_name": fix.person_name,
            "before": quick_punch_fixes.before_summary(fix),
            "after": quick_punch_fixes.after_summary(fix),
        },
    )
    return True


def _apply_fix(fix: PlannedFix, mode: str) -> bool:
    if mode == "preview":
        _record_preview(fix)
        return True
    if mode == "live":
        return _create_live_job(fix)
    return False


def run_for_day(
    day: date,
    *,
    now_utc: datetime,
    mode: str,
    client,
    apply: bool,
    spans: Sequence[LocationSpan] | None = None,
) -> FixRun:
    """Scan one plant day and optionally preview or create merge jobs.

    ``spans`` overrides the attendance-mirror snapshot so the backfill can
    supply a past day's timeline. When it is omitted the fixer requires a
    fresh, mirror-owned snapshot.
    """
    if mode not in ("preview", "live"):
        return _empty_run()
    day_spans = _spans_for_day(day, now_utc, spans)
    if day_spans is None:
        return _empty_run()
    today = plant_day.today(now_utc)
    try:
        production_times, freshness = _meter_inputs(client, day, now_utc, today)
    except Exception:
        log.warning("quick-punch fixer skipped %s: meter leaderboard failed", day)
        return _empty_run()
    scan = quick_punch_fixes.find_fixes(
        day_spans,
        production_times_by_wc=production_times,
        meter_freshness_by_wc=freshness,
        breaks=_breaks_utc(day),
        now_utc=now_utc,
    )
    if day < staffing_hours.current_pay_period_bounds(today)[0]:
        return _empty_run(scan)
    if not scan.fixes:
        return _empty_run(scan)
    employee_ids = tuple(dict.fromkeys(fix.employee_odoo_id for fix in scan.fixes))
    payroll_blocked = _payroll_blocked(employee_ids, day)
    if payroll_blocked is None:
        return FixRun(
            scan=scan,
            payroll_skipped=frozenset(employee_ids),
            active_job_skipped=frozenset(),
            deduped_item_keys=frozenset(),
            eligible=(),
            applied_item_keys=frozenset(),
        )
    active_employees = attendance_corrections.active_job_employee_ids()
    item_keys = [fix.item_key for fix in scan.fixes]
    already_jobbed = attendance_corrections.existing_job_item_keys(item_keys)
    already_previewed = (
        inbox_log.item_keys_with_action(item_keys, _PREVIEW_ACTION) if mode == "preview" else set()
    )
    already_seen = already_jobbed | already_previewed

    payroll_skipped: set[int] = set()
    active_skipped: set[int] = set()
    deduped: set[str] = set()
    eligible: list[PlannedFix] = []
    for fix in scan.fixes:
        if fix.employee_odoo_id in payroll_blocked:
            payroll_skipped.add(fix.employee_odoo_id)
            continue
        if fix.employee_odoo_id in active_employees:
            active_skipped.add(fix.employee_odoo_id)
            continue
        if fix.item_key in already_seen:
            deduped.add(fix.item_key)
            continue
        eligible.append(fix)

    applied: set[str] = set()
    if apply:
        for fix in eligible:
            try:
                if _apply_fix(fix, mode):
                    applied.add(fix.item_key)
            except Exception:
                log.exception("quick-punch fixer skipped %s after an error", fix.item_key)

    return FixRun(
        scan=scan,
        payroll_skipped=frozenset(payroll_skipped),
        active_job_skipped=frozenset(active_skipped),
        deduped_item_keys=frozenset(deduped),
        eligible=tuple(eligible),
        applied_item_keys=frozenset(applied),
    )


def tick(now_utc: datetime | None = None) -> None:
    """Read the setting and, unless Off, scan today's punches once.

    Any exception is logged as a warning naming its type so the warmer lives.
    """
    try:
        now = now_utc or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        mode = quick_punch_fix_settings.current().mode
        if mode == "off":
            return
        from . import deps

        run_for_day(
            plant_day.today(now),
            now_utc=now,
            mode=mode,
            client=deps.client,
            apply=True,
        )
    except Exception as exc:
        log.warning("quick-punch fixer tick failed: %s", type(exc).__name__)
