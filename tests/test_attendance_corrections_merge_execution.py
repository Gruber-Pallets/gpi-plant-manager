"""Correction-worker execution: no-overlap order, fixer retry cap, fixer audit.

Production Odoo has had zero overlapping ``hr.attendance`` rows in 90 days: it
rejects any create or write that would overlap another row of the same
employee. ``_NoOverlapOdoo`` enforces that rule the way Odoo does, by raising
an XML-RPC ``Fault`` (Odoo's ``ValidationError``), so these tests prove the
worker's write order never asks Odoo for an overlap.

The in-memory tests run the real worker (``_process_claim``), including the
real status transitions and completion audit, against a recording stand-in
for the job table. The ``DATABASE_URL`` tests repeat the key flows against a
real Postgres job table through ``create_job_from_preview`` and
``process_job``.
"""

import json
import os
import uuid
import xmlrpc.client
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from zira_dashboard import attendance_corrections, db, inbox_log

CHRISTIAN = 8
DISMANTLER_2 = 71
DISMANTLER_3 = 72
DEPARTMENT = 9
# 07:00 America/Chicago on 2026-09-18, the day Christian's taps were seen.
SEVEN_AM = datetime(2026, 9, 18, 12, tzinfo=UTC)
NOW = SEVEN_AM + timedelta(hours=3)
FIXER_UPN = "system:quick-punch"
FIXER_NAME = "Quick-punch auto-fix"
SUMMARY = {
    "person_name": "Christian C.",
    "before": "D3 7:00–7:02 · D2 7:02–7:04 · D3 7:04–now",
    "after": "D3 7:00–now",
}
requires_postgres = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs local Postgres"
)


def at(minutes, seconds=0, *, base=SEVEN_AM):
    return base + timedelta(minutes=minutes, seconds=seconds)


def _row(attendance_id, start, end, work_center, *, written=None):
    return {
        "odoo_attendance_id": attendance_id,
        "employee_odoo_id": CHRISTIAN,
        "check_in_utc": start,
        "check_out_utc": end,
        "odoo_work_center_id": work_center,
        "odoo_department_id": DEPARTMENT,
        "odoo_write_date": written or start - timedelta(hours=1),
    }


def christian_rows(base=SEVEN_AM):
    """D3 7:00-7:02, D2 7:02-7:04, then back on D3 and still clocked in."""
    return [
        _row(6190, at(0, base=base), at(2, 10, base=base), DISMANTLER_3),
        _row(6207, at(2, 10, base=base), at(4, 27, base=base), DISMANTLER_2),
        _row(6208, at(4, 27, base=base), None, DISMANTLER_3),
    ]


def gap_rows(base=SEVEN_AM):
    """Two D3 rows with a three-minute sign-out gap between them."""
    return [
        _row(101, at(60, base=base), at(120, base=base), DISMANTLER_3),
        _row(102, at(123, base=base), at(180, base=base), DISMANTLER_3),
    ]


def legacy_rows(base=SEVEN_AM):
    """Two touching rows a manager reassigns to D3 as one closed interval."""
    return [
        _row(201, at(60, base=base), at(120, base=base), 11),
        _row(202, at(120, base=base), at(180, base=base), 99),
    ]


def _overlaps(first, second):
    infinity = datetime.max.replace(tzinfo=UTC)
    return first["check_in_utc"] < (second["check_out_utc"] or infinity) and second[
        "check_in_utc"
    ] < (first["check_out_utc"] or infinity)


def _has_overlap(rows):
    ordered = list(rows)
    return any(
        _overlaps(first, second)
        for index, first in enumerate(ordered)
        for second in ordered[index + 1 :]
        if first["employee_odoo_id"] == second["employee_odoo_id"]
    )


class _NoOverlapOdoo:
    """In-memory ``hr.attendance`` that refuses overlaps exactly like Odoo."""

    def __init__(self, rows, *, first_created_id=9000):
        self.rows = {row["odoo_attendance_id"]: dict(row) for row in rows}
        assert not _has_overlap(self.rows.values())
        self.next_id = first_created_id
        self.writes = []
        self.rejected = []
        self._clock = 0

    def _stamp(self):
        self._clock += 1
        return datetime(2026, 9, 18, 16, tzinfo=UTC) + timedelta(seconds=self._clock)

    def _refuse_overlap(self, candidate, *, ignore_id, action):
        for other in self.rows.values():
            if other["odoo_attendance_id"] == ignore_id:
                continue
            if other["employee_odoo_id"] != candidate["employee_odoo_id"]:
                continue
            if _overlaps(candidate, other):
                self.rejected.append((action, other["odoo_attendance_id"]))
                raise xmlrpc.client.Fault(
                    2,
                    "odoo.exceptions.ValidationError: Cannot create new attendance "
                    f"record for employee {candidate['employee_odoo_id']}, the employee "
                    "was already checked in on an overlapping attendance.",
                )

    def _after_write(self, write):
        self.writes.append(write)
        assert not _has_overlap(self.rows.values())

    def fetch_attendance_rows_by_ids(self, ids):
        return [dict(self.rows[item]) for item in ids if item in self.rows]

    def fetch_employee_attendance_rows(self, employee_odoo_id, start, end):
        window = {"check_in_utc": start, "check_out_utc": end}
        return [
            dict(row)
            for row in sorted(self.rows.values(), key=lambda item: item["check_in_utc"])
            if row["employee_odoo_id"] == employee_odoo_id and _overlaps(row, window)
        ]

    def update_attendance_interval(self, attendance_id, *, values):
        candidate = {**self.rows[attendance_id], **dict(values)}
        self._refuse_overlap(candidate, ignore_id=attendance_id, action=("update", attendance_id))
        candidate["odoo_write_date"] = self._stamp()
        self.rows[attendance_id] = candidate
        self._after_write(("update", attendance_id))

    def create_attendance_interval(self, **values):
        attendance_id = self.next_id
        candidate = {"odoo_attendance_id": attendance_id, **values}
        self._refuse_overlap(candidate, ignore_id=None, action=("create", None))
        self.next_id += 1
        candidate["odoo_write_date"] = self._stamp()
        self.rows[attendance_id] = candidate
        self._after_write(("create", attendance_id))
        return attendance_id

    def delete_attendance_interval(self, attendance_id):
        del self.rows[attendance_id]
        self._after_write(("delete", attendance_id))


class _BusyOdoo(_NoOverlapOdoo):
    """Odoo that answers reads but refuses every write with a transient error."""

    def update_attendance_interval(self, attendance_id, *, values):
        raise xmlrpc.client.Fault(1, "Odoo is busy, please retry")

    def create_attendance_interval(self, **values):
        raise xmlrpc.client.Fault(1, "Odoo is busy, please retry")

    def delete_attendance_interval(self, attendance_id):
        raise xmlrpc.client.Fault(1, "Odoo is busy, please retry")


class _ReadOutageOdoo(_NoOverlapOdoo):
    """Odoo that cannot be reached at all."""

    def fetch_attendance_rows_by_ids(self, ids):
        raise ConnectionError("Odoo is unreachable")


def _preview(rows, *, item_key, start, end, target=DISMANTLER_3, merge):
    plan = attendance_corrections.plan_correction(
        rows=rows,
        employee_odoo_id=CHRISTIAN,
        start_utc=start,
        end_utc=end,
        odoo_work_center_id=target,
        odoo_department_id=DEPARTMENT,
        merge=merge,
    )
    return attendance_corrections.CorrectionPreview(
        item_key=item_key,
        employee_odoo_ids=(CHRISTIAN,),
        target_work_center_name="Dismantler 3",
        target_odoo_work_center_id=target,
        target_odoo_department_id=DEPARTMENT,
        start_utc=start,
        end_utc=end,
        plans=(plan,),
    )


def christian_preview(base=SEVEN_AM, *, item_key="quick-punch:8:6190,6207,6208"):
    return _preview(
        christian_rows(base), item_key=item_key, start=at(0, base=base), end=None, merge=True
    )


def gap_preview(base=SEVEN_AM, *, item_key="quick-punch:8:101,102"):
    return _preview(
        gap_rows(base),
        item_key=item_key,
        start=at(60, base=base),
        end=at(180, base=base),
        merge=True,
    )


def legacy_preview(base=SEVEN_AM, *, item_key="production_unassigned_run:dismantler-3:201"):
    return _preview(
        legacy_rows(base),
        item_key=item_key,
        start=at(60, base=base),
        end=at(180, base=base),
        merge=False,
    )


def _claim(preview, *, attempt=1, status="applying", fixer=None, summary=None):
    fixer = preview.item_key.startswith("quick-punch:") if fixer is None else fixer
    row = {
        "id": 5,
        "item_key": preview.item_key,
        "status": status,
        "target_work_center_name": preview.target_work_center_name,
        "target_odoo_work_center_id": preview.target_odoo_work_center_id,
        "employee_odoo_ids": list(preview.employee_odoo_ids),
        "source_snapshot": attendance_corrections._snapshot_payload(preview),
        "operations": attendance_corrections._plans_payload(preview),
        "completed_operations": [],
        "start_utc": preview.start_utc,
        "end_utc": preview.end_utc,
        "actor_email": FIXER_UPN if fixer else "manager@example.com",
        "actor_name": FIXER_NAME if fixer else "Manager",
        "audit_summary": summary if summary is not None else (SUMMARY if fixer else None),
    }
    return attendance_corrections._JobClaim(
        job_id=5,
        attempt_count=attempt,
        lease_until=NOW + timedelta(minutes=15),
        row=row,
    )


def _next_attempt(claim):
    return attendance_corrections._JobClaim(
        job_id=claim.job_id,
        attempt_count=claim.attempt_count + 1,
        lease_until=claim.lease_until,
        row=claim.row,
    )


class _JobTable:
    """Recording stand-in for the job row's transactions.

    Every fenced write succeeds and the one locked read returns the claim's
    current state, so the real ``_transition``, ``_complete_record`` and
    ``_complete_with_audit`` run unmodified.
    """

    def __init__(self):
        self.claim = None
        self.statements = []

    @contextmanager
    def cursor(self):
        yield _JobCursor(self)

    def statuses(self):
        found = []
        for sql, params in self.statements:
            if sql.startswith("UPDATE attendance_correction_jobs SET status = COALESCE"):
                if params[0] is not None:
                    found.append(params[0])
            elif sql.startswith("UPDATE attendance_correction_jobs SET status = 'complete'"):
                found.append("complete")
        return found

    def job_events(self):
        return [
            (params[1], params[2], json.loads(params[3]))
            for sql, params in self.statements
            if sql.startswith("INSERT INTO attendance_correction_job_events")
        ]


class _JobCursor:
    def __init__(self, table):
        self.table = table
        self.response = None

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        self.table.statements.append((text, params))
        claim = self.table.claim
        if text.startswith("SELECT status, attempt_count, completed_operations FROM"):
            self.response = {
                "status": claim.row["status"],
                "attempt_count": claim.attempt_count,
                "completed_operations": [dict(item) for item in claim.row["completed_operations"]],
            }
        elif "RETURNING id" in text:
            self.response = {"id": claim.job_id}
        else:
            self.response = None

    def fetchone(self):
        response, self.response = self.response, None
        return response


class _Worker:
    def __init__(self, monkeypatch, odoo):
        self.odoo = odoo
        self.table = _JobTable()
        self.inbox = []
        monkeypatch.setattr(db, "cursor", self.table.cursor)
        monkeypatch.setattr(
            inbox_log,
            "record_event_with_cursor",
            lambda _cursor, **kwargs: self.inbox.append(kwargs) or len(self.inbox),
        )
        monkeypatch.setattr(attendance_corrections, "_default_facade", lambda: odoo)
        monkeypatch.setattr(
            attendance_corrections, "_validate_applying_targets", lambda *_a, **_k: None
        )
        monkeypatch.setattr(attendance_corrections, "_claim_is_current", lambda _claim: True)

        def reserve(claim, operation, **_kwargs):
            return attendance_corrections._OperationReservation(
                job_id=claim.job_id,
                attempt_count=claim.attempt_count,
                operation_key=operation.key,
                token="a" * 32,
                reserved_until=claim.lease_until,
            )

        def complete_reserved(claim, _reservation, record, **_kwargs):
            claim.row["completed_operations"].append(dict(record))
            return True

        def mirror(claim, *_args, **_kwargs):
            claim.row["completed_operations"].append({"stage": "mirror_complete"})
            return True

        def enqueue(claim, days, *_args, **_kwargs):
            claim.row["completed_operations"].append(
                {"stage": "recalc_enqueued", "recalc_ids": [day.isoformat() for day in days]}
            )
            return True

        monkeypatch.setattr(attendance_corrections, "_reserve_operation", reserve)
        monkeypatch.setattr(
            attendance_corrections,
            "_renew_operation_reservation",
            lambda _claim, reservation: reservation,
        )
        monkeypatch.setattr(
            attendance_corrections, "_complete_reserved_operation", complete_reserved
        )
        monkeypatch.setattr(attendance_corrections, "_mirror_verified_rows", mirror)
        monkeypatch.setattr(attendance_corrections, "_enqueue_recalculation", enqueue)
        monkeypatch.setattr(attendance_corrections, "_run_recalculation", lambda _days: True)

    def run(self, claim):
        self.table.claim = claim
        return attendance_corrections._process_claim(claim, now_utc=NOW)


def _short_keys(preview):
    return sorted(
        operation.key.rsplit(":", 1)[-1]
        for plan in preview.plans
        for operation in plan.operations
    )


# ---------------------------------------------------------------------------
# No-overlap write order


def test_christian_open_merge_completes_keeping_the_live_row_id_without_overlaps(monkeypatch):
    preview = christian_preview()
    odoo = _NoOverlapOdoo(christian_rows())
    worker = _Worker(monkeypatch, odoo)

    result = worker.run(_claim(preview))

    assert result.status == "complete"
    assert odoo.rejected == []
    assert odoo.writes == [("delete", 6190), ("delete", 6207), ("update", 6208)]
    assert list(odoo.rows) == [6208]
    live = odoo.rows[6208]
    assert (live["check_in_utc"], live["check_out_utc"], live["odoo_work_center_id"]) == (
        at(0),
        None,
        DISMANTLER_3,
    )


def test_closed_same_station_gap_merge_deletes_before_it_extends(monkeypatch):
    preview = gap_preview()
    odoo = _NoOverlapOdoo(gap_rows())
    worker = _Worker(monkeypatch, odoo)

    result = worker.run(_claim(preview))

    assert result.status == "complete"
    assert odoo.rejected == []
    assert odoo.writes == [("delete", 102), ("update", 101)]
    assert [
        (row["odoo_attendance_id"], row["check_in_utc"], row["check_out_utc"])
        for row in odoo.rows.values()
    ] == [(101, at(60), at(180))]


def test_legacy_manager_closed_correction_that_extends_a_row_now_completes(monkeypatch):
    preview = legacy_preview()
    odoo = _NoOverlapOdoo(legacy_rows())
    worker = _Worker(monkeypatch, odoo)

    result = worker.run(_claim(preview))

    assert result.status == "complete"
    assert odoo.rejected == []
    assert odoo.writes == [("delete", 202), ("update", 201)]
    assert [
        (
            row["odoo_attendance_id"],
            row["check_in_utc"],
            row["check_out_utc"],
            row["odoo_work_center_id"],
        )
        for row in odoo.rows.values()
    ] == [(201, at(60), at(180), DISMANTLER_3)]


def test_operation_phases_shrink_create_delete_then_grow_then_open():
    source_rows = (
        _row(1, at(0), at(30), 11),
        _row(2, at(30), at(40), 11),
        _row(3, at(50), at(60), 11),
        _row(4, at(70), None, 11),
    )
    by_id = {row["odoo_attendance_id"]: row for row in source_rows}
    mutable = (
        "employee_odoo_id",
        "check_in_utc",
        "check_out_utc",
        "odoo_work_center_id",
        "odoo_department_id",
    )

    def operation(kind, attendance_id, after, suffix):
        if kind == "create":
            before = None
        elif kind == "delete":
            before = {field: by_id[attendance_id][field] for field in mutable}
        else:
            before = {field: by_id[attendance_id][field] for field in after}
        return attendance_corrections.CorrectionOperation(
            key="attendance-correction-v2:1:" + suffix * 64,
            kind=kind,
            attendance_id=attendance_id,
            employee_odoo_id=CHRISTIAN,
            before=before,
            after=after,
        )

    grow = operation("update", 3, {"check_in_utc": at(45)}, "1")
    shrink = operation("update", 1, {"check_out_utc": at(20)}, "2")
    delete = operation("delete", 2, None, "3")
    create = operation(
        "create",
        None,
        {
            "employee_odoo_id": CHRISTIAN,
            "check_in_utc": at(20),
            "check_out_utc": at(30),
            "odoo_work_center_id": DISMANTLER_3,
            "odoo_department_id": DEPARTMENT,
        },
        "4",
    )
    close_open = operation("update", 4, {"check_out_utc": at(80)}, "5")
    reopen = operation("update", 3, {"check_out_utc": None}, "6")

    ordered = attendance_corrections._ordered_operations(
        (reopen, grow, delete, create, shrink, close_open), source_rows=source_rows
    )

    assert ordered == (close_open, shrink, create, delete, grow, reopen)


# ---------------------------------------------------------------------------
# Fixer attempt cap and failure hook


def test_fixer_job_fails_after_six_recoverable_errors_and_records_the_failure(monkeypatch):
    preview = christian_preview()
    worker = _Worker(monkeypatch, _BusyOdoo(christian_rows()))
    claim = _claim(preview)
    results = []

    for attempt in range(1, 7):
        if attempt > 1:
            claim = _next_attempt(claim)
        assert claim.attempt_count == attempt
        results.append(worker.run(claim).status)
        if attempt < 6:
            assert worker.inbox == []

    assert attendance_corrections.QUICK_PUNCH_MAX_ATTEMPTS == 6
    assert results == ["recoverable"] * 5 + ["failed"]
    assert worker.table.statuses() == ["failed"]
    phase, result, detail = worker.table.job_events()[-1]
    assert (phase, result) == ("applying", "failed")
    assert detail["reason_code"] == "attempt_limit"
    assert detail["attempt_count"] == 6
    assert worker.inbox == [
        {
            "item_kind": "quick_punch_fix",
            "item_key": preview.item_key,
            "person_name": "Christian C.",
            "category_label": "Quick-punch auto-fix",
            "action": "quick_punch_failed",
            "outcome": "attempt_limit",
            "before_value": SUMMARY["before"],
            "after_value": SUMMARY["after"],
            "actor_upn": FIXER_UPN,
            "actor_name": FIXER_NAME,
            "source": "auto",
            "reversible": False,
            "detail": {
                "job_id": 5,
                "phase": "applying",
                "result": "failed",
                "reason_code": "attempt_limit",
            },
        }
    ]


def test_manager_job_keeps_retrying_under_the_same_errors(monkeypatch):
    preview = legacy_preview()
    worker = _Worker(monkeypatch, _BusyOdoo(legacy_rows()))
    claim = _claim(preview)
    results = []

    for attempt in range(1, 11):
        if attempt > 1:
            claim = _next_attempt(claim)
        result = worker.run(claim)
        results.append(result.status)
        assert result.retry_at is not None

    assert results == ["recoverable"] * 10
    assert worker.table.statuses() == []
    assert all(detail.get("reason_code") != "attempt_limit" for *_, detail in worker.table.job_events())
    assert worker.inbox == []


def test_fixer_attempt_cap_also_covers_recoverable_failures_outside_writes(monkeypatch):
    preview = christian_preview()
    worker = _Worker(monkeypatch, _ReadOutageOdoo(christian_rows()))
    claim = _claim(preview, attempt=5)

    assert worker.run(claim).status == "recoverable"
    result = worker.run(_next_attempt(claim))

    assert result.status == "failed"
    phase, outcome, detail = worker.table.job_events()[-1]
    assert (phase, outcome, detail["reason_code"]) == ("applying", "failed", "attempt_limit")
    assert [event["outcome"] for event in worker.inbox] == ["attempt_limit"]


def _change_live_row_in_odoo(odoo, _claim_value):
    odoo.rows[6208]["odoo_write_date"] = NOW


def _corrupt_saved_request(_odoo, claim_value):
    claim_value.row["start_utc"] = at(1)


def _verify_without_writing(_odoo, claim_value):
    claim_value.row["status"] = "verifying"


@pytest.mark.parametrize(
    ("break_job", "reason"),
    [
        (_change_live_row_in_odoo, "preflight_source_changed"),
        (_corrupt_saved_request, "invalid_plan"),
        (_verify_without_writing, "verified_intervals_mismatch"),
    ],
)
def test_every_fixer_failure_records_one_failure_event(monkeypatch, break_job, reason):
    preview = christian_preview()
    odoo = _NoOverlapOdoo(christian_rows())
    worker = _Worker(monkeypatch, odoo)
    claim = _claim(preview)
    break_job(odoo, claim)

    result = worker.run(claim)

    assert result.status == "failed"
    assert worker.table.statuses() == ["failed"]
    assert [
        (event["action"], event["outcome"], event["item_kind"], event["person_name"])
        for event in worker.inbox
    ] == [("quick_punch_failed", reason, "quick_punch_fix", "Christian C.")]


@pytest.mark.parametrize(
    "break_job", [_change_live_row_in_odoo, _corrupt_saved_request, _verify_without_writing]
)
def test_manager_failures_record_no_fixer_event(monkeypatch, break_job):
    preview = christian_preview(item_key="production_unassigned_run:dismantler-3:6190")
    odoo = _NoOverlapOdoo(christian_rows())
    worker = _Worker(monkeypatch, odoo)
    claim = _claim(preview)
    break_job(odoo, claim)

    result = worker.run(claim)

    assert result.status == "failed"
    assert worker.table.statuses() == ["failed"]
    assert worker.inbox == []


def test_fixer_failure_without_a_stored_summary_still_alerts(monkeypatch):
    table = _JobTable()
    claim = _claim(christian_preview())
    claim.row["audit_summary"] = None
    table.claim = claim
    events = []
    monkeypatch.setattr(db, "cursor", table.cursor)
    monkeypatch.setattr(
        inbox_log,
        "record_event_with_cursor",
        lambda _cursor, **kwargs: events.append(kwargs) or 1,
    )

    assert attendance_corrections._transition(
        claim,
        status="failed",
        phase="applying",
        result="source_changed",
        detail=attendance_corrections._event_detail(
            job_id=5, reason_code="fresh_preview_required"
        ),
        last_error="source_changed: fresh preview required",
    )

    assert [
        (event["action"], event["outcome"], event["person_name"], event["before_value"])
        for event in events
    ] == [("quick_punch_failed", "fresh_preview_required", None, None)]


# ---------------------------------------------------------------------------
# Completion audit


def test_fixer_completion_records_the_quick_punch_merge(monkeypatch):
    preview = christian_preview()
    worker = _Worker(monkeypatch, _NoOverlapOdoo(christian_rows()))

    assert worker.run(_claim(preview)).status == "complete"

    assert len(worker.inbox) == 1
    event = dict(worker.inbox[0])
    detail = event.pop("detail")
    assert event == {
        "item_kind": "quick_punch_fix",
        "item_key": preview.item_key,
        "person_name": "Christian C.",
        "category_label": "Quick-punch auto-fix",
        "action": "quick_punch_merged",
        "outcome": "Merged in Odoo",
        "before_value": SUMMARY["before"],
        "after_value": SUMMARY["after"],
        "actor_upn": FIXER_UPN,
        "actor_name": FIXER_NAME,
        "source": "auto",
        "reversible": False,
        "resolved_at": NOW,
    }
    assert detail["before_attendance_ids"] == [6190, 6207, 6208]
    assert detail["after_attendance_ids"] == [6208]
    assert worker.table.statuses()[-1] == "complete"


def test_manager_completion_still_records_the_exact_legacy_event(monkeypatch):
    preview = legacy_preview()
    worker = _Worker(monkeypatch, _NoOverlapOdoo(legacy_rows()))

    assert worker.run(_claim(preview)).status == "complete"

    assert len(worker.inbox) == 1
    event = dict(worker.inbox[0])
    detail = dict(event.pop("detail"))
    operation_keys = detail.pop("operation_keys")
    assert event == {
        "item_kind": "attendance_correction",
        "item_key": preview.item_key,
        "person_name": None,
        "category_label": "Odoo attendance correction",
        "action": "corrected_odoo_attendance",
        "outcome": "Verified and recalculated",
        "actor_upn": "manager@example.com",
        "actor_name": "Manager",
        "source": "inbox",
        "reversible": False,
        "resolved_at": NOW,
    }
    assert detail == {
        "job_id": 5,
        "employee_ids": [CHRISTIAN],
        "before_attendance_ids": [201, 202],
        "after_attendance_ids": [201],
    }
    assert sorted(operation_keys) == _short_keys(preview)


# ---------------------------------------------------------------------------
# Job creation stores the fixer's audit summary


def _insert_statements(monkeypatch, *, job_id=44):
    statements = []

    class Cursor:
        response = None

        def execute(self, sql, params=None):
            statements.append((" ".join(sql.split()), params))
            self.response = {"id": job_id} if "RETURNING id" in sql else None

        def fetchone(self):
            return self.response

    @contextmanager
    def cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", cursor)
    return statements


def test_fixer_job_creation_stores_the_audit_summary(monkeypatch):
    statements = _insert_statements(monkeypatch)

    job_id = attendance_corrections.create_job_from_preview(
        preview=christian_preview(),
        actor_email=FIXER_UPN,
        actor_name=FIXER_NAME,
        audit_summary=SUMMARY,
    )

    sql, params = next(item for item in statements if item[0].startswith("INSERT INTO"))
    assert job_id == 44
    assert "audit_summary" in sql
    assert params[8:10] == (FIXER_UPN, FIXER_NAME)
    assert json.loads(params[10]) == SUMMARY


def test_fixer_summary_may_omit_the_person_name(monkeypatch):
    statements = _insert_statements(monkeypatch)
    summary = {**SUMMARY, "person_name": None}

    attendance_corrections.create_job_from_preview(
        preview=christian_preview(),
        actor_email=FIXER_UPN,
        actor_name=FIXER_NAME,
        audit_summary=summary,
    )

    sql, params = next(item for item in statements if item[0].startswith("INSERT INTO"))
    assert json.loads(params[10]) == summary


def test_job_creation_without_a_summary_keeps_the_legacy_insert(monkeypatch):
    statements = _insert_statements(monkeypatch)

    attendance_corrections.create_job_from_preview(
        preview=legacy_preview(),
        actor_email="manager@example.com",
        actor_name="Manager",
    )

    sql, params = next(item for item in statements if item[0].startswith("INSERT INTO"))
    assert "audit_summary" not in sql
    assert params[8:] == ("manager@example.com", "Manager")


@pytest.mark.parametrize(
    "summary",
    [
        "D3 7:00–now",
        ["Christian C.", "before", "after"],
        {"person_name": "Christian C.", "before": "x"},
        {**SUMMARY, "extra": "field"},
        {**SUMMARY, "before": 7},
        {**SUMMARY, "after": "  "},
        {**SUMMARY, "person_name": 12},
        {**SUMMARY, "before": "x" * 1001},
    ],
)
def test_job_creation_rejects_a_malformed_summary_before_the_database(monkeypatch, summary):
    monkeypatch.setattr(db, "cursor", lambda: pytest.fail("bad summary reached the database"))

    with pytest.raises((TypeError, ValueError)):
        attendance_corrections.create_job_from_preview(
            preview=christian_preview(),
            actor_email=FIXER_UPN,
            actor_name=FIXER_NAME,
            audit_summary=summary,
        )


def test_the_fixer_item_key_prefix_has_one_owner():
    assert attendance_corrections.QUICK_PUNCH_ITEM_KEY_PREFIX == "quick-punch:"


# ---------------------------------------------------------------------------
# Real Postgres job table


@pytest.fixture
def job_table(monkeypatch):
    db.init_pool()
    db.bootstrap_schema()
    keys = []
    yield keys
    for key in keys:
        db.execute("DELETE FROM inbox_events WHERE item_key = %s", (key,))
        db.execute(
            "DELETE FROM attendance_correction_job_events WHERE correction_job_id IN "
            "(SELECT id FROM attendance_correction_jobs WHERE item_key = %s)",
            (key,),
        )
        db.execute("DELETE FROM attendance_correction_jobs WHERE item_key = %s", (key,))


def _install_real_worker(monkeypatch, odoo):
    """Real claims, fences, reservations, transitions and audit; fake Odoo only.

    The attendance mirror and recalculation queue are outside this change, so
    their stages only record their durable markers.
    """

    def mirror(claim, *_args, **_kwargs):
        claim.row["completed_operations"].append({"stage": "mirror_complete"})
        return True

    def enqueue(claim, days, *_args, **_kwargs):
        claim.row["completed_operations"].append(
            {"stage": "recalc_enqueued", "recalc_ids": [day.isoformat() for day in days]}
        )
        return True

    monkeypatch.setattr(attendance_corrections, "_default_facade", lambda: odoo)
    monkeypatch.setattr(
        attendance_corrections, "_validate_applying_targets", lambda *_a, **_k: None
    )
    monkeypatch.setattr(attendance_corrections, "_mirror_verified_rows", mirror)
    monkeypatch.setattr(attendance_corrections, "_enqueue_recalculation", enqueue)
    monkeypatch.setattr(attendance_corrections, "_run_recalculation", lambda _days: True)


def _recent_base():
    # Open intervals are bounded by the real clock in ``process_job``.
    return datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)


def _unique(prefix):
    return f"{prefix}{uuid.uuid4().hex}"


def _make_retryable_now(job_id):
    db.execute(
        "UPDATE attendance_correction_jobs SET updated_at = now() - interval '1 minute' "
        "WHERE id = %s",
        (job_id,),
    )


def _job(job_id):
    return db.query(
        "SELECT status, attempt_count, last_error, audit_summary, actor_email "
        "FROM attendance_correction_jobs WHERE id = %s",
        (job_id,),
    )[0]


def _job_event_rows(job_id):
    return db.query(
        "SELECT phase, result, detail FROM attendance_correction_job_events "
        "WHERE correction_job_id = %s ORDER BY id",
        (job_id,),
    )


def _inbox_rows(item_key):
    return db.query(
        "SELECT item_kind, person_name, category_label, action, outcome, before_value, "
        "after_value, actor_upn, actor_name, source FROM inbox_events "
        "WHERE item_key = %s ORDER BY id",
        (item_key,),
    )


@requires_postgres
def test_postgres_fixer_open_merge_completes_and_records_the_merge(monkeypatch, job_table):
    base = _recent_base()
    key = _unique("quick-punch:8:")
    job_table.append(key)
    odoo = _NoOverlapOdoo(christian_rows(base))
    _install_real_worker(monkeypatch, odoo)

    job_id = attendance_corrections.create_job_from_preview(
        preview=christian_preview(base, item_key=key),
        actor_email=FIXER_UPN,
        actor_name=FIXER_NAME,
        audit_summary=SUMMARY,
    )
    result = attendance_corrections.process_job(job_id)

    assert result.status == "complete"
    assert odoo.rejected == []
    assert list(odoo.rows) == [6208]
    assert odoo.rows[6208]["check_in_utc"] == at(0, base=base)
    assert odoo.rows[6208]["check_out_utc"] is None
    job = _job(job_id)
    assert job["status"] == "complete"
    assert job["audit_summary"] == SUMMARY
    assert _inbox_rows(key) == [
        {
            "item_kind": "quick_punch_fix",
            "person_name": "Christian C.",
            "category_label": "Quick-punch auto-fix",
            "action": "quick_punch_merged",
            "outcome": "Merged in Odoo",
            "before_value": SUMMARY["before"],
            "after_value": SUMMARY["after"],
            "actor_upn": FIXER_UPN,
            "actor_name": FIXER_NAME,
            "source": "auto",
        }
    ]


@requires_postgres
def test_postgres_closed_gap_merge_completes(monkeypatch, job_table):
    base = _recent_base() - timedelta(hours=3)
    key = _unique("quick-punch:8:")
    job_table.append(key)
    odoo = _NoOverlapOdoo(gap_rows(base))
    _install_real_worker(monkeypatch, odoo)

    job_id = attendance_corrections.create_job_from_preview(
        preview=gap_preview(base, item_key=key),
        actor_email=FIXER_UPN,
        actor_name=FIXER_NAME,
        audit_summary=SUMMARY,
    )

    assert attendance_corrections.process_job(job_id).status == "complete"
    assert odoo.rejected == []
    assert [(row["check_in_utc"], row["check_out_utc"]) for row in odoo.rows.values()] == [
        (at(60, base=base), at(180, base=base))
    ]


@requires_postgres
def test_postgres_manager_extension_completes_with_the_legacy_audit_event(
    monkeypatch, job_table
):
    base = _recent_base() - timedelta(hours=3)
    key = _unique("production_unassigned_run:dismantler-3:")
    job_table.append(key)
    odoo = _NoOverlapOdoo(legacy_rows(base))
    _install_real_worker(monkeypatch, odoo)

    job_id = attendance_corrections.create_job_from_preview(
        preview=legacy_preview(base, item_key=key),
        actor_email="manager@example.com",
        actor_name="Manager",
    )

    assert attendance_corrections.process_job(job_id).status == "complete"
    assert odoo.rejected == []
    assert _job(job_id)["audit_summary"] is None
    assert _inbox_rows(key) == [
        {
            "item_kind": "attendance_correction",
            "person_name": None,
            "category_label": "Odoo attendance correction",
            "action": "corrected_odoo_attendance",
            "outcome": "Verified and recalculated",
            "before_value": None,
            "after_value": None,
            "actor_upn": "manager@example.com",
            "actor_name": "Manager",
            "source": "inbox",
        }
    ]


@requires_postgres
def test_postgres_fixer_retry_cap_fails_and_alerts_while_manager_keeps_retrying(
    monkeypatch, job_table
):
    base = _recent_base()
    fixer_key = _unique("quick-punch:8:")
    manager_key = _unique("production_unassigned_run:dismantler-3:")
    job_table.extend([fixer_key, manager_key])
    _install_real_worker(monkeypatch, _ReadOutageOdoo(christian_rows(base)))
    fixer_id = attendance_corrections.create_job_from_preview(
        preview=christian_preview(base, item_key=fixer_key),
        actor_email=FIXER_UPN,
        actor_name=FIXER_NAME,
        audit_summary=SUMMARY,
    )
    manager_id = attendance_corrections.create_job_from_preview(
        preview=christian_preview(base, item_key=manager_key),
        actor_email="manager@example.com",
        actor_name="Manager",
    )

    fixer_results = []
    for _attempt in range(6):
        _make_retryable_now(fixer_id)
        fixer_results.append(attendance_corrections.process_job(fixer_id).status)
    manager_results = []
    for _attempt in range(8):
        _make_retryable_now(manager_id)
        manager_results.append(attendance_corrections.process_job(manager_id).status)

    assert fixer_results == ["recoverable"] * 5 + ["failed"]
    fixer = _job(fixer_id)
    assert (fixer["status"], fixer["attempt_count"]) == ("failed", 6)
    assert fixer["last_error"].startswith("attempt_limit")
    terminal = _job_event_rows(fixer_id)[-1]
    assert (terminal["phase"], terminal["result"]) == ("applying", "failed")
    assert terminal["detail"]["reason_code"] == "attempt_limit"
    assert _inbox_rows(fixer_key) == [
        {
            "item_kind": "quick_punch_fix",
            "person_name": "Christian C.",
            "category_label": "Quick-punch auto-fix",
            "action": "quick_punch_failed",
            "outcome": "attempt_limit",
            "before_value": SUMMARY["before"],
            "after_value": SUMMARY["after"],
            "actor_upn": FIXER_UPN,
            "actor_name": FIXER_NAME,
            "source": "auto",
        }
    ]

    assert manager_results == ["recoverable"] * 8
    manager = _job(manager_id)
    assert (manager["status"], manager["attempt_count"]) == ("applying", 8)
    assert _inbox_rows(manager_key) == []


@requires_postgres
def test_postgres_fixer_source_change_records_the_failure_event(monkeypatch, job_table):
    base = _recent_base()
    key = _unique("quick-punch:8:")
    job_table.append(key)
    odoo = _NoOverlapOdoo(christian_rows(base))
    _install_real_worker(monkeypatch, odoo)
    job_id = attendance_corrections.create_job_from_preview(
        preview=christian_preview(base, item_key=key),
        actor_email=FIXER_UPN,
        actor_name=FIXER_NAME,
        audit_summary=SUMMARY,
    )
    odoo.rows[6208]["odoo_write_date"] = datetime.now(UTC)

    assert attendance_corrections.process_job(job_id).status == "failed"
    assert odoo.writes == []
    assert _job(job_id)["status"] == "failed"
    assert [(row["action"], row["outcome"]) for row in _inbox_rows(key)] == [
        ("quick_punch_failed", "preflight_source_changed")
    ]
