"""Safely reconcile the known payroll regular-hours overage."""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

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


def _pending_review(decision: Decision, reason: str) -> Decision:
    reasons = ("pending_correction",)
    if reason != "pending_correction":
        reasons += (reason,)
    return replace(decision, kind="review", reason_codes=reasons, action=None)


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


def _mark_pending(
    attempt: store.CorrectionAttempt,
    reason: str,
    detail: str,
    now: datetime,
) -> Decision:
    try:
        store.mark_attempt_issue(attempt.attempt_id, reason, detail, now)
    except Exception:
        _log.warning(
            "payroll guard: could not persist %s for attempt %s",
            reason,
            attempt.attempt_id,
            exc_info=True,
        )
    return _pending_review(attempt.decision, reason)


def _finalize_verified(
    attempt: store.CorrectionAttempt,
    detail: str,
    now: datetime,
    *,
    mutated_this_run: bool,
) -> tuple[int, Decision | None]:
    try:
        store.finalize_attempt(attempt.attempt_id, detail, now)
    except Exception:
        _log.warning(
            "payroll guard: audit finalization failed for attempt %s",
            attempt.attempt_id,
            exc_info=True,
        )
        return int(mutated_this_run), _mark_pending(
            attempt,
            "audit_failed",
            "verified Odoo change is waiting for permanent audit history",
            now,
        )
    return int(mutated_this_run), None


def _read_after_mutation(
    attempt: store.CorrectionAttempt,
    mutation_error: Exception | None,
    now: datetime,
) -> tuple[int, Decision | None]:
    decision = attempt.decision
    try:
        verified = odoo_client.fetch_payroll_work_entry(decision.work_entry_id)
    except Exception:
        _log.warning(
            "payroll guard: verification read failed for attempt %s",
            attempt.attempt_id,
            exc_info=True,
        )
        return 0, _mark_pending(
            attempt,
            "verification_failed",
            "could not reread Odoo after the mutation request",
            now,
        )

    if decision.action == "duration_update":
        if _duration_matches(verified, decision, decision.after_duration):
            return _finalize_verified(
                attempt,
                "duration reread matched",
                now,
                mutated_this_run=True,
            )
        unchanged = _duration_matches(verified, decision, decision.before_duration)
    else:
        if verified is None:
            return _finalize_verified(
                attempt,
                "zero-target draft regular row absent",
                now,
                mutated_this_run=True,
            )
        unchanged = _duration_matches(verified, decision, decision.before_duration)

    if unchanged:
        reason = "write_failed" if mutation_error is not None else "verification_failed"
        detail = (
            "Odoo still has the saved original after an unclear mutation response"
            if mutation_error is not None
            else "Odoo still has the saved original after the mutation request"
        )
        return 0, _mark_pending(attempt, reason, detail, now)
    return 0, _mark_pending(
        attempt,
        "fresh_state_changed",
        "Odoo matches neither the saved original nor the correction target",
        now,
    )


def _reconcile_attempt(
    attempt: store.CorrectionAttempt, now: datetime
) -> tuple[int, Decision | None]:
    decision = attempt.decision
    try:
        fresh = odoo_client.fetch_payroll_work_entry(decision.work_entry_id)
    except Exception:
        _log.warning(
            "payroll guard: fresh read failed for attempt %s",
            attempt.attempt_id,
            exc_info=True,
        )
        return 0, _mark_pending(
            attempt,
            "fresh_read_failed",
            "could not read the pending Work Entry from Odoo",
            now,
        )

    if decision.action == "duration_update" and _duration_matches(
        fresh, decision, decision.after_duration
    ):
        return _finalize_verified(
            attempt,
            "target observed during recovery; actor unknown",
            now,
            mutated_this_run=False,
        )
    if decision.action == "delete_zero_regular" and fresh is None:
        return _finalize_verified(
            attempt,
            "row absent during recovery; actor unknown",
            now,
            mutated_this_run=False,
        )
    if not _duration_matches(fresh, decision, decision.before_duration):
        return 0, _mark_pending(
            attempt,
            "fresh_state_changed",
            "Odoo matches neither the saved original nor the correction target",
            now,
        )

    mutation_error = None
    try:
        if decision.action == "duration_update":
            odoo_client.set_payroll_work_entry_duration(
                decision.work_entry_id, decision.after_duration
            )
        elif decision.action == "delete_zero_regular":
            odoo_client.delete_payroll_work_entry(decision.work_entry_id)
        else:
            raise RuntimeError(f"unsupported correction action {decision.action!r}")
    except Exception as error:
        mutation_error = error
        _log.warning(
            "payroll guard: mutation response failed for attempt %s",
            attempt.attempt_id,
            exc_info=True,
        )
    return _read_after_mutation(attempt, mutation_error, now)


def _reconcile_attempts(
    attempts: list[store.CorrectionAttempt], now: datetime
) -> tuple[int, list[Decision], set[int]]:
    corrected_count = 0
    review_issues = []
    still_pending_entry_ids = set()
    for attempt in attempts:
        try:
            corrected, review = _reconcile_attempt(attempt, now)
        except Exception:
            _log.warning(
                "payroll guard: unexpected attempt failure for %s",
                attempt.attempt_id,
                exc_info=True,
            )
            corrected = 0
            review = _mark_pending(
                attempt,
                "verification_failed",
                "unexpected error while checking the saved correction",
                now,
            )
        corrected_count += corrected
        if review is not None:
            review_issues.append(review)
            still_pending_entry_ids.add(attempt.decision.work_entry_id)
    return corrected_count, review_issues, still_pending_entry_ids


def _run_enabled(now: datetime) -> dict[str, int]:
    pending_attempts = store.load_pending_attempts()
    corrected_count, pending_reviews, pending_entry_ids = _reconcile_attempts(
        pending_attempts, now
    )
    candidates = odoo_client.fetch_recent_payroll_candidates(now - LOOKBACK)
    if not candidates:
        try:
            alert.sync_review_task(pending_reviews, now)
        except Exception:
            _log.warning(
                "payroll guard: could not clear review task", exc_info=True
            )
        _log.warning(
            "payroll guard: corrected=%d review=%d noop=0 candidates=0",
            corrected_count,
            len(pending_reviews),
        )
        return {
            "corrected": corrected_count,
            "review": len(pending_reviews),
            "noop": 0,
        }

    decisions = _classify_candidates(candidates)
    review_issues = pending_reviews + [
        item for item in decisions if item.kind == "review"
    ]
    noop_count = sum(item.kind == "noop" for item in decisions)
    for decision in [item for item in decisions if item.kind == "correct"]:
        if decision.work_entry_id in pending_entry_ids:
            continue
        try:
            attempt = store.create_attempt(uuid4(), decision, now)
        except Exception:
            _log.warning(
                "payroll guard: could not persist correction intent for entry %s",
                decision.work_entry_id,
                exc_info=True,
            )
            review_issues.append(_as_review(decision, "intent_failed"))
            continue
        corrected, review = _reconcile_attempt(attempt, now)
        corrected_count += corrected
        if review is not None:
            review_issues.append(review)
            pending_entry_ids.add(decision.work_entry_id)

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
