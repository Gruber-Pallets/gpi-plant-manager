"""Safely reconcile the known payroll regular-hours overage."""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from . import odoo_client
from . import payroll_work_entry_alert as alert
from . import payroll_work_entry_store as store
from .payroll_work_entry_rules import Decision, TOLERANCE_HOURS, classify_day


_log = logging.getLogger(__name__)
LOOKBACK = timedelta(days=90)
_DISABLED_VALUES = {"0", "false", "no"}


def enabled() -> bool:
    """Return whether the payroll correction guard is enabled."""
    value = os.environ.get("PAYROLL_WORK_ENTRY_GUARD_ENABLED", "1")
    return value.strip().lower() not in _DISABLED_VALUES


def _as_review(decision: Decision, reason: str) -> Decision:
    return replace(decision, kind="review", reason_codes=(reason,), action=None)


def _same_identity(row: dict | None, decision: Decision) -> bool:
    return bool(
        row
        and row.get("id") == decision.work_entry_id
        and row.get("employee_id") == decision.employee_id
        and row.get("date") == decision.work_date
        and row.get("active") is True
        and row.get("state") == "draft"
        and row.get("conflict") is False
        and row.get("type_code") == "WORK100"
        and row.get("attendance_id") == decision.attendance_id
    )


def _duration_matches(
    row: dict | None, decision: Decision, expected: float | None
) -> bool:
    if not _same_identity(row, decision) or expected is None:
        return False
    try:
        return abs(float(row["duration"]) - expected) <= TOLERANCE_HOURS
    except (KeyError, TypeError, ValueError):
        return False


def _classify_candidates(candidates: list[dict]) -> list[Decision]:
    keys = sorted({(row["employee_id"], row["date"]) for row in candidates})
    employee_ids = sorted({employee_id for employee_id, _work_date in keys})
    work_rows, attendance_rows = odoo_client.fetch_payroll_inputs(
        employee_ids,
        min(work_date for _employee_id, work_date in keys),
        max(work_date for _employee_id, work_date in keys),
    )
    work_by_key = defaultdict(list)
    attendance_by_key = defaultdict(list)
    for row in work_rows:
        work_by_key[(row["employee_id"], row["date"])].append(row)
    for row in attendance_rows:
        attendance_by_key[(row["employee_id"], row["date"])].append(row)

    names = {
        (row["employee_id"], row["date"]): row["employee_name"]
        for row in candidates
    }
    decisions = []
    for employee_id, work_date in keys:
        grouped_work = work_by_key.get((employee_id, work_date), [])
        if not grouped_work:
            decisions.append(
                Decision(
                    kind="review",
                    employee_id=employee_id,
                    employee_name=names[(employee_id, work_date)],
                    work_date=work_date,
                    reason_codes=("missing_candidate_group",),
                    action=None,
                    work_entry_id=None,
                    attendance_id=None,
                    before_duration=0,
                    after_duration=None,
                    attendance_regular=0,
                    attendance_overtime=0,
                    work_regular=0,
                    work_overtime=0,
                )
            )
            continue
        decisions.append(
            classify_day(
                employee_id,
                names[(employee_id, work_date)],
                work_date,
                grouped_work,
                attendance_by_key.get((employee_id, work_date), []),
            )
        )
    return decisions


def _correct_decisions(
    decisions: list[Decision], now: datetime
) -> tuple[int, list[Decision]]:
    review_issues = [item for item in decisions if item.kind == "review"]
    corrected_count = 0

    for decision in [item for item in decisions if item.kind == "correct"]:
        try:
            fresh = odoo_client.fetch_payroll_work_entry(decision.work_entry_id)
        except Exception:
            _log.warning(
                "payroll guard: fresh read failed for entry %s",
                decision.work_entry_id,
                exc_info=True,
            )
            review_issues.append(_as_review(decision, "fresh_read_failed"))
            continue
        if not _duration_matches(fresh, decision, decision.before_duration):
            review_issues.append(_as_review(decision, "fresh_state_changed"))
            continue

        try:
            if decision.action == "duration_update":
                odoo_client.set_payroll_work_entry_duration(
                    decision.work_entry_id, decision.after_duration
                )
            elif decision.action == "delete_zero_regular":
                odoo_client.delete_payroll_work_entry(decision.work_entry_id)
            else:
                raise RuntimeError(
                    f"unsupported correction action {decision.action!r}"
                )
        except Exception:
            _log.warning(
                "payroll guard: mutation failed for entry %s",
                decision.work_entry_id,
                exc_info=True,
            )
            review_issues.append(_as_review(decision, "write_failed"))
            continue

        try:
            if decision.action == "duration_update":
                verified = odoo_client.fetch_payroll_work_entry(
                    decision.work_entry_id
                )
                verification_ok = _duration_matches(
                    verified, decision, decision.after_duration
                )
                detail = "duration reread matched"
            else:
                verification_ok = not odoo_client.payroll_work_entry_exists(
                    decision.work_entry_id
                )
                detail = "zero-target draft regular row absent"
        except Exception:
            verification_ok = False
            _log.warning(
                "payroll guard: verification read failed for entry %s",
                decision.work_entry_id,
                exc_info=True,
            )
        if not verification_ok:
            review_issues.append(_as_review(decision, "verification_failed"))
            continue

        corrected_count += 1
        try:
            store.append_correction(decision, detail, now)
        except Exception:
            _log.warning(
                "payroll guard: audit failed for corrected entry %s",
                decision.work_entry_id,
                exc_info=True,
            )
            review_issues.append(_as_review(decision, "audit_failed"))

    return corrected_count, review_issues


def _run_enabled(now: datetime) -> dict[str, int]:
    candidates = odoo_client.fetch_recent_payroll_candidates(now - LOOKBACK)
    if not candidates:
        try:
            alert.sync_review_task([], now)
        except Exception:
            _log.warning(
                "payroll guard: could not clear review task", exc_info=True
            )
        _log.warning(
            "payroll guard: corrected=0 review=0 noop=0 candidates=0"
        )
        return {"corrected": 0, "review": 0, "noop": 0}

    decisions = _classify_candidates(candidates)
    corrected_count, review_issues = _correct_decisions(decisions, now)
    noop_count = sum(item.kind == "noop" for item in decisions)

    try:
        alert.sync_review_task(review_issues, now)
    except Exception:
        _log.warning("payroll guard: could not sync review task", exc_info=True)

    candidate_count = len(
        {(row["employee_id"], row["date"]) for row in candidates}
    )
    _log.warning(
        "payroll guard: corrected=%d review=%d noop=%d candidates=%d",
        corrected_count,
        len(review_issues),
        noop_count,
        candidate_count,
    )
    return {
        "corrected": corrected_count,
        "review": len(review_issues),
        "noop": noop_count,
    }


def run_once(now: datetime | None = None) -> dict:
    """Run one serialized payroll guard pass when enabled."""
    if not enabled():
        return {"skipped": "disabled"}

    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    with store.guard_lock():
        return _run_enabled(current_time)
