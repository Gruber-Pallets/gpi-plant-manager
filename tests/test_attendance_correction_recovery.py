from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from zira_dashboard import attendance_corrections


NOW = datetime(2026, 8, 31, 15, tzinfo=UTC)


def test_exact_after_state_can_be_adopted_after_update_timeout():
    source = {
        "odoo_attendance_id": 11,
        "employee_odoo_id": 7,
        "check_in_utc": NOW,
        "check_out_utc": None,
        "odoo_work_center_id": 80,
        "odoo_department_id": 9,
        "odoo_write_date": NOW,
    }
    operation = attendance_corrections.CorrectionOperation(
        key="attendance-correction-v2:7:" + "1" * 64,
        kind="update",
        attendance_id=11,
        employee_odoo_id=7,
        before={"odoo_work_center_id": 80},
        after={"odoo_work_center_id": 81},
    )
    after = dict(source, odoo_work_center_id=81)

    assert (
        attendance_corrections._operation_source_state(
            operation, source_row=source, current_row=after
        )
        == "after"
    )


def test_source_version_or_before_change_fails_closed():
    source = {
        "odoo_attendance_id": 11,
        "employee_odoo_id": 7,
        "check_in_utc": NOW,
        "check_out_utc": None,
        "odoo_work_center_id": 80,
        "odoo_department_id": 9,
        "odoo_write_date": NOW,
    }
    operation = attendance_corrections.CorrectionOperation(
        key="attendance-correction-v2:7:" + "1" * 64,
        kind="update",
        attendance_id=11,
        employee_odoo_id=7,
        before={"odoo_work_center_id": 80},
        after={"odoo_work_center_id": 81},
    )
    changed = dict(
        source,
        odoo_work_center_id=82,
        odoo_write_date=datetime(2026, 8, 31, 15, 1, tzinfo=UTC),
    )

    assert (
        attendance_corrections._operation_source_state(
            operation, source_row=source, current_row=changed
        )
        == "source_changed"
    )


def test_created_id_is_required_when_materializing_verified_expected_rows():
    plan = attendance_corrections.plan_correction(
        rows=[],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=datetime(2026, 8, 31, 16, tzinfo=UTC),
        odoo_work_center_id=81,
        odoo_department_id=9,
    )

    try:
        attendance_corrections._expected_with_created_ids(plan, ())
    except ValueError as error:
        assert "created attendance id" in str(error)
    else:
        raise AssertionError("unconfirmed create was accepted")


def test_completed_create_id_is_used_for_exact_verification():
    plan = attendance_corrections.plan_correction(
        rows=[],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=datetime(2026, 8, 31, 16, tzinfo=UTC),
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    completed = ({"operation_key": plan.operations[0].key, "attendance_id": 99},)

    expected = attendance_corrections._expected_with_created_ids(plan, completed)

    assert expected[0]["odoo_attendance_id"] == 99


def test_warmer_is_registered_once_at_fifteen_seconds():
    from zira_dashboard import app

    entries = [entry for entry in app._WARMERS if entry[0] == "attendance corrections"]
    assert entries == [("attendance corrections", app._tick_attendance_corrections, 15)]


def _source(*, end=None, work_center=80, write_date=NOW):
    return {
        "odoo_attendance_id": 11,
        "employee_odoo_id": 7,
        "check_in_utc": NOW,
        "check_out_utc": end,
        "odoo_work_center_id": work_center,
        "odoo_department_id": 9,
        "odoo_write_date": write_date,
    }


class _UpdateTimeoutFacade:
    def __init__(self):
        self.row = _source()
        self.writes = 0

    def fetch_attendance_rows_by_ids(self, _ids):
        return [dict(self.row)]

    def update_attendance_interval(self, _attendance_id, *, values):
        self.writes += 1
        self.row.update(values)
        self.row["odoo_write_date"] = datetime(2026, 8, 31, 15, 1, tzinfo=UTC)
        raise TimeoutError("lost response")


def test_update_timeout_is_reread_and_adopted_without_second_write():
    facade = _UpdateTimeoutFacade()
    operation = attendance_corrections.CorrectionOperation(
        key="attendance-correction-v2:7:" + "1" * 64,
        kind="update",
        attendance_id=11,
        employee_odoo_id=7,
        before={"odoo_work_center_id": 80},
        after={"odoo_work_center_id": 81},
    )

    record, result = attendance_corrections._perform_operation(facade, operation, (_source(),))

    assert result == "adopted_timeout"
    assert record["attendance_id"] == 11
    assert facade.writes == 1


class _DeleteTimeoutFacade:
    def __init__(self):
        self.row = _source(end=datetime(2026, 8, 31, 16, tzinfo=UTC))
        self.deletes = 0

    def fetch_attendance_rows_by_ids(self, _ids):
        return [] if self.row is None else [dict(self.row)]

    def delete_attendance_interval(self, _attendance_id):
        self.deletes += 1
        self.row = None
        raise TimeoutError("lost response")


def test_delete_timeout_adopts_exact_absence():
    facade = _DeleteTimeoutFacade()
    source = _source(end=datetime(2026, 8, 31, 16, tzinfo=UTC))
    operation = attendance_corrections.CorrectionOperation(
        key="attendance-correction-v2:7:" + "1" * 64,
        kind="delete",
        attendance_id=11,
        employee_odoo_id=7,
        before={
            key: source[key]
            for key in (
                "employee_odoo_id",
                "check_in_utc",
                "check_out_utc",
                "odoo_work_center_id",
                "odoo_department_id",
            )
        },
        after=None,
    )

    _record, result = attendance_corrections._perform_operation(facade, operation, (source,))

    assert result == "adopted_timeout"
    assert facade.deletes == 1


class _CreateTimeoutFacade:
    def __init__(self):
        self.rows = []
        self.creates = 0

    def fetch_employee_attendance_rows(self, _employee, _start, _end):
        return [dict(row) for row in self.rows]

    def create_attendance_interval(self, **values):
        self.creates += 1
        self.rows.append(
            {
                "odoo_attendance_id": 99,
                **values,
                "odoo_write_date": datetime(2026, 8, 31, 15, 1, tzinfo=UTC),
            }
        )
        raise TimeoutError("lost response")


def test_create_timeout_adopts_one_exact_row_and_never_duplicates():
    facade = _CreateTimeoutFacade()
    plan = attendance_corrections.plan_correction(
        rows=[],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=datetime(2026, 8, 31, 16, tzinfo=UTC),
        odoo_work_center_id=81,
        odoo_department_id=9,
    )

    record, result = attendance_corrections._perform_operation(facade, plan.operations[0], ())

    assert result == "adopted_timeout"
    assert record["attendance_id"] == 99
    assert facade.creates == 1


def test_operation_reservation_is_persisted_with_a_fresh_job_lease(monkeypatch):
    from zira_dashboard import db

    plan = attendance_corrections.plan_correction(
        rows=[],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=datetime(2026, 8, 31, 16, tzinfo=UTC),
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    claim = _claim_for(plan)
    writes = []
    store = {
        "id": 5,
        "status": "applying",
        "attempt_count": 1,
        "completed_operations": [],
        "updated_at": NOW,
        "created_at": NOW - timedelta(hours=1),
    }

    class Cursor:
        def __init__(self):
            self.result = None

        def execute(self, sql, params=()):
            normalized = " ".join(sql.split())
            writes.append((normalized, params))
            if normalized.startswith("SELECT status, attempt_count, completed_operations"):
                self.result = dict(store)
            elif normalized.startswith("SELECT * FROM attendance_correction_jobs"):
                self.result = dict(store) if store["updated_at"] <= params[0] else None
            elif normalized.startswith(
                "UPDATE attendance_correction_jobs SET completed_operations"
            ):
                store["completed_operations"] = json.loads(params[0])
                store["updated_at"] = params[1]
                self.result = {"id": 5}
            elif normalized.startswith("UPDATE attendance_correction_jobs SET status"):
                store["status"] = params[0]
                store["attempt_count"] = params[1]
                store["updated_at"] = params[2]
                self.result = dict(store)
            else:
                self.result = None

        def fetchone(self):
            result = self.result
            self.result = None
            return result

    @contextmanager
    def cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", cursor)

    reservation = attendance_corrections._reserve_operation(claim, plan.operations[0], now_utc=NOW)

    update = next(
        item for item in writes if item[0].startswith("UPDATE attendance_correction_jobs")
    )
    persisted = json.loads(update[1][0])
    assert persisted[-1]["reservation_token"] == reservation.token
    assert persisted[-1]["operation_key"] == plan.operations[0].key
    assert reservation.reserved_until == NOW + attendance_corrections._CLAIM_LEASE
    assert update[1][1] == reservation.reserved_until
    assert (
        attendance_corrections._claim_job(
            job_id=5,
            now_utc=reservation.reserved_until - timedelta(microseconds=1),
        )
        is None
    )
    recovered = attendance_corrections._claim_job(
        job_id=5,
        now_utc=reservation.reserved_until,
    )
    assert recovered.attempt_count == 2
    replacement = attendance_corrections._reserve_operation(
        recovered,
        plan.operations[0],
        now_utc=reservation.reserved_until,
    )
    assert replacement.token != reservation.token


def test_newer_fence_stops_stale_create_before_write_and_rejects_local_progress(
    monkeypatch,
):
    from zira_dashboard import db

    plan = attendance_corrections.plan_correction(
        rows=[],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=datetime(2026, 8, 31, 16, tzinfo=UTC),
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    operation = plan.operations[0]
    remote_calls = []
    fence_checks = 0

    class Facade:
        def fetch_employee_attendance_rows(self, *_args):
            remote_calls.append("read")
            return []

        def create_attendance_interval(self, **_values):
            remote_calls.append("create")
            return 99

    def fence():
        nonlocal fence_checks
        fence_checks += 1
        if fence_checks == 2:
            raise attendance_corrections._StaleClaim("new worker owns the job")

    with pytest.raises(attendance_corrections._StaleClaim):
        attendance_corrections._perform_operation(Facade(), operation, (), before_remote_call=fence)

    assert remote_calls == ["read"]

    old_claim = _claim_for(plan)
    reservation = attendance_corrections._OperationReservation(
        job_id=5,
        attempt_count=1,
        operation_key=operation.key,
        token="a" * 32,
        reserved_until=NOW + timedelta(minutes=15),
    )
    updates = []

    class Cursor:
        def __init__(self):
            self.result = None

        def execute(self, sql, params=()):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT status, attempt_count, completed_operations"):
                self.result = {
                    "status": "applying",
                    "attempt_count": 2,
                    "completed_operations": [],
                }
            elif normalized.startswith("UPDATE attendance_correction_jobs"):
                updates.append((normalized, params))
                self.result = {"id": 5}
            else:
                self.result = None

        def fetchone(self):
            result = self.result
            self.result = None
            return result

    @contextmanager
    def cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", cursor)

    assert (
        attendance_corrections._complete_reserved_operation(
            old_claim,
            reservation,
            {
                "operation_key": operation.key,
                "kind": "create",
                "attendance_id": 99,
            },
            result="confirmed",
            detail=attendance_corrections._event_detail(job_id=5),
        )
        is False
    )
    assert updates == []


@pytest.mark.parametrize("record_kind", ["completed_later", "reserved_later"])
def test_out_of_order_saved_operation_progress_fails_before_odoo(monkeypatch, record_kind):
    source = _source()
    correction_start = NOW + timedelta(minutes=30)
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=correction_start,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    ordered = attendance_corrections._ordered_operations(plan.operations, source_rows=(source,))
    later = ordered[-1]
    if record_kind == "completed_later":
        record = {
            "operation_key": later.key,
            "kind": later.kind,
            "attendance_id": later.attendance_id or 99,
        }
    else:
        record = {
            "operation_key": later.key,
            "reservation_token": "a" * 32,
            "reservation_attempt_count": 1,
            "reservation_until": "2026-08-31T15:15:00Z",
        }
    preview = _preview_for(plan, start=correction_start, end=None)
    claim = _claim_for(plan, completed=(record,))
    claim.row["start_utc"] = correction_start
    claim.row["source_snapshot"] = attendance_corrections._snapshot_payload(preview)
    claim.row["operations"] = attendance_corrections._plans_payload(preview)
    claim = attendance_corrections._JobClaim(
        job_id=claim.job_id,
        attempt_count=2,
        lease_until=claim.lease_until,
        row=claim.row,
    )
    transitions = []
    monkeypatch.setattr(
        attendance_corrections,
        "_default_facade",
        lambda: pytest.fail("corrupt progress reached Odoo"),
    )

    def transition(local_claim, **kwargs):
        transitions.append(kwargs)
        if kwargs.get("status"):
            local_claim.row["status"] = kwargs["status"]
        return True

    monkeypatch.setattr(attendance_corrections, "_transition", transition)

    result = attendance_corrections._process_claim(claim, now_utc=NOW)

    assert result.status == "failed"
    assert transitions[-1]["phase"] == "planning"
    assert transitions[-1]["result"] == "invalid_plan"


def test_saved_reservation_from_current_or_future_attempt_fails_before_odoo(
    monkeypatch,
):
    plan = attendance_corrections.plan_correction(
        rows=[],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=datetime(2026, 8, 31, 16, tzinfo=UTC),
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    operation = plan.operations[0]
    claim = _claim_for(
        plan,
        completed=(
            {
                "operation_key": operation.key,
                "reservation_token": "a" * 32,
                "reservation_attempt_count": 2,
                "reservation_until": "2026-08-31T15:15:00Z",
            },
        ),
    )
    claim = attendance_corrections._JobClaim(
        job_id=claim.job_id,
        attempt_count=2,
        lease_until=claim.lease_until,
        row=claim.row,
    )
    transitions = []
    monkeypatch.setattr(
        attendance_corrections,
        "_default_facade",
        lambda: pytest.fail("bad reservation attempt reached Odoo"),
    )
    monkeypatch.setattr(
        attendance_corrections,
        "_transition",
        lambda _claim, **kwargs: transitions.append(kwargs) or True,
    )

    result = attendance_corrections._process_claim(claim, now_utc=NOW)

    assert result.status == "failed"
    assert transitions[-1]["result"] == "invalid_plan"


def test_stale_source_before_first_write_makes_zero_odoo_writes():
    facade = _UpdateTimeoutFacade()
    facade.row["odoo_write_date"] = datetime(2026, 8, 31, 15, 2, tzinfo=UTC)
    operation = attendance_corrections.CorrectionOperation(
        key="attendance-correction-v2:7:" + "1" * 64,
        kind="update",
        attendance_id=11,
        employee_odoo_id=7,
        before={"odoo_work_center_id": 80},
        after={"odoo_work_center_id": 81},
    )

    try:
        attendance_corrections._perform_operation(facade, operation, (_source(),))
    except RuntimeError as error:
        assert "changed" in str(error)
    else:
        raise AssertionError("stale source was written")
    assert facade.writes == 0


def test_later_stale_source_is_found_before_any_earlier_write():
    first = _source(end=datetime(2026, 8, 31, 16, tzinfo=UTC))
    second = {
        **_source(end=datetime(2026, 8, 31, 17, tzinfo=UTC)),
        "odoo_attendance_id": 12,
        "check_in_utc": datetime(2026, 8, 31, 16, tzinfo=UTC),
    }
    plan = attendance_corrections.plan_correction(
        rows=[first, second],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=datetime(2026, 8, 31, 17, tzinfo=UTC),
        odoo_work_center_id=81,
        odoo_department_id=9,
    )

    class Facade:
        def __init__(self):
            self.rows = {
                11: dict(first),
                12: dict(
                    second,
                    odoo_write_date=datetime(2026, 8, 31, 15, 1, tzinfo=UTC),
                ),
            }

        def fetch_attendance_rows_by_ids(self, ids):
            return [dict(self.rows[item]) for item in ids if item in self.rows]

        def fetch_employee_attendance_rows(self, *_args):
            return list(self.rows.values())

    facade = Facade()

    try:
        attendance_corrections._preflight_operations(facade, plan.operations, (first, second))
    except RuntimeError as error:
        assert "changed" in str(error)
    else:
        raise AssertionError("later stale source passed preflight")


def test_verification_expands_to_include_split_shoulders():
    source = _source(end=datetime(2026, 8, 31, 18, tzinfo=UTC))
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=datetime(2026, 8, 31, 16, tzinfo=UTC),
        end_utc=datetime(2026, 8, 31, 17, tzinfo=UTC),
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    completed = []
    next_id = 90
    expected_rows = []
    for operation in plan.operations:
        if operation.kind == "create":
            next_id += 1
            completed.append({"operation_key": operation.key, "attendance_id": next_id})
    for row in attendance_corrections._expected_with_created_ids(plan, completed):
        expected_rows.append(dict(row, odoo_write_date=NOW))

    class Facade:
        def __init__(self):
            self.calls = []

        def fetch_employee_attendance_rows(self, employee, start, end):
            self.calls.append((employee, start, end))
            return expected_rows

    facade = Facade()

    attendance_corrections._verification_rows(
        facade,
        {7: plan},
        completed,
        datetime(2026, 8, 31, 16, tzinfo=UTC),
        datetime(2026, 8, 31, 17, tzinfo=UTC),
    )

    assert facade.calls == [
        (
            7,
            datetime(2026, 8, 31, 15, tzinfo=UTC),
            datetime(2026, 8, 31, 18, tzinfo=UTC),
        )
    ]


def test_verified_display_names_reach_mirror_upsert(monkeypatch):
    from zira_dashboard import attendance_mirror, db

    source = {
        **_source(),
        "employee_name": "Alex Smith",
        "odoo_work_center_name": "Repair 1",
        "odoo_department_name": "Production",
    }
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=80,
        odoo_department_id=9,
    )

    class Facade:
        def fetch_employee_attendance_rows(self, *_args):
            return [dict(source)]

    verified = attendance_corrections._verification_rows(Facade(), {7: plan}, (), NOW, None)
    claim = _claim_for(plan, status="recalculating")
    captured = []

    class Cursor:
        def __init__(self):
            self.response = None

        def execute(self, sql, _params=None):
            if sql.startswith("SELECT status, attempt_count"):
                self.response = {"status": "recalculating", "attempt_count": 1}
            elif sql.startswith("UPDATE attendance_correction_jobs"):
                self.response = {"id": 5}
            else:
                self.response = None

        def fetchone(self):
            response = self.response
            self.response = None
            return response

    @contextmanager
    def cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", cursor)
    monkeypatch.setattr(
        attendance_mirror,
        "_locked_sync_state",
        lambda _cur: {"baseline_completed_at": NOW},
    )
    monkeypatch.setattr(
        attendance_mirror,
        "_upsert_rows_cur",
        lambda _cur, rows, **_kwargs: captured.extend(rows),
    )
    monkeypatch.setattr(attendance_corrections, "_append_event_cur", lambda *_a, **_k: None)

    assert attendance_corrections._mirror_verified_rows(
        claim,
        verified,
        {7: (source,)},
        (),
        completed_at=NOW,
    )
    assert captured[0]["employee_name"] == "Alex Smith"
    assert captured[0]["odoo_work_center_name"] == "Repair 1"
    assert captured[0]["odoo_department_name"] == "Production"


def _preview_for(plan, *, start=NOW, end=None):
    return attendance_corrections.CorrectionPreview(
        item_key="production_unassigned_run:repair-1:1",
        employee_odoo_ids=(7,),
        target_work_center_name="Repair 1",
        target_odoo_work_center_id=81,
        target_odoo_department_id=9,
        start_utc=start,
        end_utc=end,
        plans=(plan,),
    )


def _claim_for(plan, *, status="applying", completed=()):
    preview = _preview_for(plan, start=NOW, end=None)
    row = {
        "id": 5,
        "item_key": preview.item_key,
        "status": status,
        "target_work_center_name": "Repair 1",
        "target_odoo_work_center_id": 81,
        "employee_odoo_ids": [7],
        "source_snapshot": attendance_corrections._snapshot_payload(preview),
        "operations": attendance_corrections._plans_payload(preview),
        "completed_operations": list(completed),
        "start_utc": NOW,
        "end_utc": None,
        "actor_email": "manager@example.com",
        "actor_name": "Manager",
    }
    return attendance_corrections._JobClaim(
        job_id=5,
        attempt_count=1,
        lease_until=datetime(2026, 8, 31, 15, 15, tzinfo=UTC),
        row=row,
    )


def test_job_rejects_source_snapshot_that_disagrees_with_authenticated_plan(monkeypatch):
    source = _source()
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    changed_source = dict(source, odoo_work_center_id=82)
    changed_plan = attendance_corrections.plan_correction(
        rows=[changed_source],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    claim = _claim_for(plan)
    claim.row["source_snapshot"] = attendance_corrections._snapshot_payload(
        _preview_for(changed_plan)
    )
    transitions = []
    monkeypatch.setattr(
        attendance_corrections,
        "_transition",
        lambda _claim, **kwargs: transitions.append(kwargs) or True,
    )
    monkeypatch.setattr(
        attendance_corrections,
        "_default_facade",
        lambda: (_ for _ in ()).throw(AssertionError("invalid job reached Odoo")),
    )

    result = attendance_corrections._process_claim(claim, now_utc=NOW)

    assert result.status == "failed"
    assert transitions[-1]["result"] == "invalid_plan"


def test_job_request_must_match_authenticated_plan_before_odoo(monkeypatch):
    plan = attendance_corrections.plan_correction(
        rows=[_source()],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    claim = _claim_for(plan)
    claim.row["start_utc"] = datetime(2026, 8, 31, 15, 30, tzinfo=UTC)
    transitions = []
    monkeypatch.setattr(
        attendance_corrections,
        "_transition",
        lambda _claim, **kwargs: transitions.append(kwargs) or True,
    )
    monkeypatch.setattr(
        attendance_corrections,
        "_default_facade",
        lambda: (_ for _ in ()).throw(AssertionError("invalid job reached Odoo")),
    )

    result = attendance_corrections._process_claim(claim, now_utc=NOW)

    assert result.status == "failed"
    assert transitions[-1]["result"] == "invalid_plan"


def test_malformed_saved_employee_ids_fail_closed_before_odoo(monkeypatch):
    plan = attendance_corrections.plan_correction(
        rows=[_source()],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    claim = _claim_for(plan)
    claim.row["employee_odoo_ids"] = "{not-json"
    transitions = []
    monkeypatch.setattr(
        attendance_corrections,
        "_transition",
        lambda _claim, **kwargs: transitions.append(kwargs) or True,
    )
    monkeypatch.setattr(
        attendance_corrections,
        "_default_facade",
        lambda: (_ for _ in ()).throw(AssertionError("invalid job reached Odoo")),
    )

    result = attendance_corrections._process_claim(claim, now_utc=NOW)

    assert result.status == "failed"
    assert transitions[-1]["result"] == "invalid_plan"


class _OpenCorrectionFacade:
    def __init__(self, source):
        self.rows = {source["odoo_attendance_id"]: dict(source)}
        self.calls = []

    def fetch_attendance_rows_by_ids(self, ids):
        return [dict(self.rows[item]) for item in ids if item in self.rows]

    def fetch_employee_attendance_rows(self, _employee, start, end):
        infinity = datetime.max.replace(tzinfo=UTC)
        return [
            dict(row)
            for row in self.rows.values()
            if row["check_in_utc"] < (end or infinity)
            and (row["check_out_utc"] or infinity) > start
        ]

    def update_attendance_interval(self, attendance_id, *, values):
        self.calls.append(("update", attendance_id, dict(values)))
        self.rows[attendance_id].update(values)
        self.rows[attendance_id]["odoo_write_date"] = datetime(2026, 8, 31, 15, 1, tzinfo=UTC)

    def create_attendance_interval(self, **values):
        self.calls.append(("create", dict(values)))
        attendance_id = 99
        self.rows[attendance_id] = {
            "odoo_attendance_id": attendance_id,
            **values,
            "odoo_write_date": datetime(2026, 8, 31, 15, 2, tzinfo=UTC),
        }
        return attendance_id


def _install_flow_fakes(monkeypatch, facade, *, recalc=True):
    transitions = []
    completed = []

    def transition(claim, **kwargs):
        transitions.append(kwargs)
        if kwargs.get("status"):
            claim.row["status"] = kwargs["status"]
        return True

    def complete_record(claim, record, **_kwargs):
        if record.get("stage") == "recalc_horizon":
            claim.row["completed_operations"].insert(0, dict(record))
        else:
            claim.row["completed_operations"].append(dict(record))
        completed.append(dict(record))
        return True

    monkeypatch.setattr(attendance_corrections, "_default_facade", lambda: facade)
    monkeypatch.setattr(
        attendance_corrections,
        "_validate_applying_targets",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(attendance_corrections, "_claim_is_current", lambda _claim: True)
    monkeypatch.setattr(attendance_corrections, "_heartbeat_claim", lambda _claim: None)
    monkeypatch.setattr(attendance_corrections, "_transition", transition)
    monkeypatch.setattr(attendance_corrections, "_complete_record", complete_record)

    def reserve(claim, operation, **_kwargs):
        return attendance_corrections._OperationReservation(
            job_id=claim.job_id,
            attempt_count=claim.attempt_count,
            operation_key=operation.key,
            token="a" * 32,
            reserved_until=claim.lease_until,
        )

    monkeypatch.setattr(attendance_corrections, "_reserve_operation", reserve)
    monkeypatch.setattr(
        attendance_corrections,
        "_renew_operation_reservation",
        lambda _claim, reservation: reservation,
    )

    def complete_reserved(claim, _reservation, record, **kwargs):
        return complete_record(claim, record, **kwargs)

    monkeypatch.setattr(
        attendance_corrections,
        "_complete_reserved_operation",
        complete_reserved,
    )

    def mirror(claim, *_args, **_kwargs):
        claim.row["completed_operations"].append({"stage": "mirror_complete"})
        return True

    def enqueue(claim, days, *_args, **_kwargs):
        claim.row["completed_operations"].append(
            {
                "stage": "recalc_enqueued",
                "recalc_ids": [day.isoformat() for day in days],
            }
        )
        return True

    monkeypatch.setattr(attendance_corrections, "_mirror_verified_rows", mirror)
    monkeypatch.setattr(attendance_corrections, "_enqueue_recalculation", enqueue)
    monkeypatch.setattr(attendance_corrections, "_run_recalculation", lambda _days: recalc)
    monkeypatch.setattr(attendance_corrections, "_complete_with_audit", lambda *_a, **_k: True)
    return transitions, completed


def test_full_flow_closes_old_open_row_before_creating_new_open_row(monkeypatch):
    source = _source()
    correction_start = datetime(2026, 8, 31, 15, 30, tzinfo=UTC)
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=correction_start,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    preview = _preview_for(plan, start=correction_start, end=None)
    claim = _claim_for(plan)
    claim.row["start_utc"] = correction_start
    claim.row["source_snapshot"] = attendance_corrections._snapshot_payload(preview)
    claim.row["operations"] = attendance_corrections._plans_payload(preview)
    facade = _OpenCorrectionFacade(source)
    transitions, _completed = _install_flow_fakes(monkeypatch, facade)

    result = attendance_corrections._process_claim(claim, now_utc=NOW)

    assert result.status == "complete"
    assert [call[0] for call in facade.calls] == ["update", "create"]
    assert facade.calls[0][2]["check_out_utc"] == correction_start
    assert facade.calls[1][1]["check_out_utc"] is None
    assert any(item["result"] == "verified" for item in transitions)


def test_partial_resume_skips_persisted_operation(monkeypatch):
    source = _source()
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    operation = plan.operations[0]
    facade = _OpenCorrectionFacade(dict(source, odoo_work_center_id=81))
    claim = _claim_for(
        plan,
        completed=(
            {
                "operation_key": operation.key,
                "kind": operation.kind,
                "attendance_id": 11,
            },
        ),
    )
    _install_flow_fakes(monkeypatch, facade)

    result = attendance_corrections._process_claim(claim, now_utc=NOW)

    assert result.status == "complete"
    assert facade.calls == []


def test_expired_reservation_is_not_mistaken_for_completed_operation(monkeypatch):
    source = _source()
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    operation = plan.operations[0]
    claim = _claim_for(
        plan,
        completed=(
            {
                "operation_key": operation.key,
                "reservation_token": "a" * 32,
                "reservation_attempt_count": 1,
                "reservation_until": "2026-08-31T15:00:00Z",
            },
        ),
    )
    claim = attendance_corrections._JobClaim(
        job_id=claim.job_id,
        attempt_count=2,
        lease_until=claim.lease_until,
        row=claim.row,
    )
    facade = _OpenCorrectionFacade(source)
    _install_flow_fakes(monkeypatch, facade)

    result = attendance_corrections._process_claim(claim, now_utc=NOW)

    assert result.status == "complete"
    assert [call[0] for call in facade.calls] == ["update"]


def test_positive_target_department_change_stops_before_preflight_or_write(monkeypatch):
    source = _source()
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=5,
    )
    claim = _claim_for(plan)
    preflight_calls = []
    transitions = []

    class Facade:
        def fetch_employee_statuses(self):
            return [{"id": 7, "active": True}]

    monkeypatch.setattr(attendance_corrections, "_default_facade", Facade)
    monkeypatch.setattr(
        attendance_corrections,
        "_resolve_mapping",
        lambda *_args, **_kwargs: (81, 6),
    )
    monkeypatch.setattr(attendance_corrections, "_heartbeat_claim", lambda _claim: None)
    monkeypatch.setattr(
        attendance_corrections,
        "_freeze_recalc_horizon",
        lambda _claim, _days: True,
    )

    def transition(local_claim, **kwargs):
        transitions.append(kwargs)
        if kwargs.get("status"):
            local_claim.row["status"] = kwargs["status"]
        return True

    monkeypatch.setattr(attendance_corrections, "_transition", transition)

    def preflight(*_args, **_kwargs):
        preflight_calls.append(True)
        raise AssertionError("preflight must not run after target identity changes")

    monkeypatch.setattr(attendance_corrections, "_preflight_operations", preflight)

    result = attendance_corrections._process_claim(claim, now_utc=NOW)

    assert result.status == "failed"
    assert preflight_calls == []
    assert transitions[-1]["result"] == "source_changed"


def test_default_facade_rechecks_cached_preview_department_before_apply(monkeypatch):
    from zira_dashboard import db, odoo_client, staffing

    source = _source()
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=5,
    )
    claim = _claim_for(plan)
    preflight_calls = []
    transitions = []
    odoo_reads = []

    monkeypatch.setattr(
        db,
        "query",
        lambda *_args, **_kwargs: [
            {"odoo_work_center_id": 81, "odoo_work_center_name": "Odoo Repair 1"}
        ],
    )
    monkeypatch.setattr(
        odoo_client,
        "fetch_manufacturing_work_centers",
        lambda **_kwargs: [{"id": 81, "name": "Odoo Repair 1"}],
    )
    monkeypatch.setattr(
        odoo_client,
        "fetch_employee_statuses",
        lambda: [{"id": 7, "active": True}],
    )
    monkeypatch.setattr(odoo_client, "_wc_dept_id_cache", {"Repair 1": 5})
    monkeypatch.setattr(odoo_client, "_app_wc_name_for_odoo_id", lambda _work_center_id: "Repair 1")
    monkeypatch.setattr(
        staffing,
        "LOCATIONS",
        [type("Location", (), {"name": "Repair 1", "department": "Recycled"})()],
    )
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda *_args, **_kwargs: odoo_reads.append(True) or [{"id": 6}],
    )
    monkeypatch.setattr(attendance_corrections, "_heartbeat_claim", lambda _claim: None)
    monkeypatch.setattr(
        attendance_corrections,
        "_freeze_recalc_horizon",
        lambda _claim, _days: True,
    )

    def transition(local_claim, **kwargs):
        transitions.append(kwargs)
        if kwargs.get("status"):
            local_claim.row["status"] = kwargs["status"]
        return True

    monkeypatch.setattr(attendance_corrections, "_transition", transition)

    def preflight(*_args, **_kwargs):
        preflight_calls.append(True)
        raise AssertionError("preflight must not run after target identity changes")

    monkeypatch.setattr(attendance_corrections, "_preflight_operations", preflight)

    result = attendance_corrections._process_claim(claim, now_utc=NOW)

    assert result.status == "failed"
    assert odoo_reads == [True]
    assert preflight_calls == []
    assert transitions[-1]["result"] == "source_changed"


def test_recalculation_failure_resumes_without_replaying_odoo(monkeypatch):
    source = _source()
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    facade = _OpenCorrectionFacade(source)
    claim = _claim_for(plan)
    transitions, _completed = _install_flow_fakes(monkeypatch, facade, recalc=False)

    first = attendance_corrections._process_claim(claim, now_utc=NOW)
    write_count = len(facade.calls)

    assert first.status == "recoverable"
    assert claim.row["status"] == "recalculating"
    assert any(item["result"] == "failed" for item in transitions)

    monkeypatch.setattr(attendance_corrections, "_run_recalculation", lambda _days: True)
    second = attendance_corrections._process_claim(claim, now_utc=NOW)

    assert second.status == "complete"
    assert len(facade.calls) == write_count


def test_recalculation_retry_reuses_first_durable_open_horizon(monkeypatch):
    source = _source(work_center=81)
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    claim = _claim_for(plan)
    facade = _OpenCorrectionFacade(source)
    _install_flow_fakes(monkeypatch, facade, recalc=False)
    first_now = NOW + timedelta(hours=1)

    first = attendance_corrections._process_claim(claim, now_utc=first_now)
    persisted = next(
        item for item in claim.row["completed_operations"] if item.get("stage") == "recalc_enqueued"
    )
    assert persisted["recalc_ids"] == ["2026-08-31"]

    attempted_days = []
    monkeypatch.setattr(
        attendance_corrections,
        "_run_recalculation",
        lambda days: attempted_days.append(tuple(days)) or False,
    )
    second = attendance_corrections._process_claim(
        claim,
        now_utc=datetime(2026, 9, 2, 15, tzinfo=UTC),
    )

    assert first.status == "recoverable"
    assert second.status == "recoverable"
    assert attempted_days == [(date(2026, 8, 31),)]


def test_recalc_enqueue_keeps_101_durable_days_but_bounds_event_ids(monkeypatch):
    from zira_dashboard import attendance_mirror, db

    claim = _claim_for(
        attendance_corrections.plan_correction(
            rows=[],
            employee_odoo_id=7,
            start_utc=NOW,
            end_utc=NOW + timedelta(hours=1),
            odoo_work_center_id=81,
            odoo_department_id=9,
        ),
        status="recalculating",
    )
    days = tuple(date(2026, 1, 1) + timedelta(days=index) for index in range(101))
    persisted = []
    events = []

    class Cursor:
        def __init__(self):
            self.result = None

        def execute(self, sql, params=()):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT status, attempt_count"):
                self.result = {"status": "recalculating", "attempt_count": 1}
            elif normalized.startswith("UPDATE attendance_correction_jobs"):
                persisted.extend(json.loads(params[0]))
                self.result = {"id": claim.job_id}
            else:
                raise AssertionError(f"unexpected SQL: {normalized}")

        def fetchone(self):
            return self.result

    @contextmanager
    def cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", cursor)
    monkeypatch.setattr(attendance_mirror, "_enqueue_recalc_cur", lambda *_a, **_k: None)
    monkeypatch.setattr(
        attendance_corrections,
        "_append_event_cur",
        lambda _cur, _job_id, _phase, _result, detail: events.append(detail),
    )

    assert attendance_corrections._enqueue_recalculation(
        claim,
        days,
        (),
        requested_at=NOW,
    )
    assert len(persisted[-1]["recalc_ids"]) == 101
    assert len(events[-1]["recalc_ids"]) == attendance_corrections._MAX_EVENT_IDS


def _open_claim_spanning_local_days(day_count):
    from zira_dashboard import shift_config

    local_end = NOW.astimezone(shift_config.SITE_TZ)
    local_start = local_end - timedelta(days=day_count - 1)
    source = _source(work_center=81, write_date=local_start.astimezone(UTC))
    source["check_in_utc"] = local_start.astimezone(UTC)
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=source["check_in_utc"],
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    preview = _preview_for(plan, start=source["check_in_utc"], end=None)
    claim = _claim_for(plan)
    claim.row["start_utc"] = source["check_in_utc"]
    claim.row["source_snapshot"] = attendance_corrections._snapshot_payload(preview)
    claim.row["operations"] = attendance_corrections._plans_payload(preview)
    return claim


def test_open_horizon_over_max_fails_before_facade_or_odoo(monkeypatch):
    claim = _open_claim_spanning_local_days(501)
    transitions = []
    monkeypatch.setattr(
        attendance_corrections,
        "_default_facade",
        lambda: pytest.fail("oversized horizon reached Odoo"),
    )
    monkeypatch.setattr(
        attendance_corrections,
        "_transition",
        lambda _claim, **kwargs: transitions.append(kwargs) or True,
    )

    result = attendance_corrections._process_claim(claim, now_utc=NOW)

    assert result.status == "failed"
    assert transitions[-1]["phase"] == "planning"
    assert transitions[-1]["result"] == "invalid_plan"
    assert "horizon" in result.error


def test_exact_max_open_horizon_is_frozen_before_work_and_resumes(monkeypatch):
    claim = _open_claim_spanning_local_days(500)
    source = attendance_corrections._source_rows_from_json(claim.row["source_snapshot"], (7,))[7][0]
    facade = _OpenCorrectionFacade(source)
    _install_flow_fakes(monkeypatch, facade, recalc=False)

    first = attendance_corrections._process_claim(claim, now_utc=NOW)
    horizon = next(
        item for item in claim.row["completed_operations"] if item.get("stage") == "recalc_horizon"
    )
    attempted = []
    monkeypatch.setattr(
        attendance_corrections,
        "_run_recalculation",
        lambda days: attempted.append(tuple(days)) or False,
    )
    second = attendance_corrections._process_claim(
        claim,
        now_utc=NOW + timedelta(days=2),
    )

    assert first.status == "recoverable"
    assert second.status == "recoverable"
    assert len(horizon["recalc_ids"]) == 500
    assert len(attempted[-1]) == 500
    assert attempted[-1][0].isoformat() == horizon["recalc_ids"][0]
    assert attempted[-1][-1].isoformat() == horizon["recalc_ids"][-1]


def test_101_day_correction_completes_with_full_horizon_and_bounded_events(monkeypatch):
    claim = _open_claim_spanning_local_days(101)
    source = attendance_corrections._source_rows_from_json(claim.row["source_snapshot"], (7,))[7][0]
    facade = _OpenCorrectionFacade(source)
    _transitions, completed = _install_flow_fakes(monkeypatch, facade, recalc=True)
    complete_record = attendance_corrections._complete_record
    record_calls = []

    def capture_record(local_claim, record, **kwargs):
        record_calls.append(kwargs)
        return complete_record(local_claim, record, **kwargs)

    monkeypatch.setattr(attendance_corrections, "_complete_record", capture_record)

    result = attendance_corrections._process_claim(claim, now_utc=NOW)

    horizon = next(
        item for item in claim.row["completed_operations"] if item.get("stage") == "recalc_horizon"
    )
    enqueued = next(
        item for item in claim.row["completed_operations"] if item.get("stage") == "recalc_enqueued"
    )
    recalc_complete = next(item for item in completed if item.get("stage") == "recalc_complete")
    completion_event = next(
        item
        for item in record_calls
        if item.get("phase") == "recalculation" and item.get("result") == "complete"
    )

    assert result.status == "complete"
    assert len(horizon["recalc_ids"]) == 101
    assert enqueued["recalc_ids"] == horizon["recalc_ids"]
    assert recalc_complete == {"stage": "recalc_complete"}
    assert len(completion_event["detail"]["recalc_ids"]) == 100


def test_verification_mismatch_is_terminal_and_increments_failure(monkeypatch):
    source = _source()
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    completed = (
        {
            "operation_key": plan.operations[0].key,
            "kind": "update",
            "attendance_id": 11,
        },
    )
    claim = _claim_for(plan, status="verifying", completed=completed)
    facade = _OpenCorrectionFacade(source)
    transitions, _completed = _install_flow_fakes(monkeypatch, facade)

    result = attendance_corrections._process_claim(claim, now_utc=NOW)

    assert result.status == "failed"
    failure = transitions[-1]
    assert failure["verification_increment"] is True
    assert failure["status"] == "failed"
    assert facade.calls == []


def test_verification_source_outage_is_recoverable_not_a_mismatch(monkeypatch):
    source = _source()
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    completed = (
        {
            "operation_key": plan.operations[0].key,
            "kind": "update",
            "attendance_id": 11,
        },
    )
    claim = _claim_for(plan, status="verifying", completed=completed)

    class UnavailableFacade:
        def fetch_employee_attendance_rows(self, *_args):
            raise TimeoutError("Odoo unavailable")

    transitions, _completed = _install_flow_fakes(monkeypatch, UnavailableFacade())

    result = attendance_corrections._process_claim(claim, now_utc=NOW)

    assert result.status == "recoverable"
    assert transitions[-1]["result"] == "odoo_failure"
    assert transitions[-1].get("verification_increment") is None


def test_audit_failure_retries_without_correction_owned_cache_refresh(monkeypatch):
    source = _source()
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    facade = _OpenCorrectionFacade(source)
    claim = _claim_for(plan)
    _transitions, _completed = _install_flow_fakes(monkeypatch, facade)
    audit_calls = []

    def audit(*_args, **_kwargs):
        audit_calls.append(True)
        if len(audit_calls) == 1:
            raise RuntimeError("audit database unavailable")
        return True

    monkeypatch.setattr(attendance_corrections, "_complete_with_audit", audit)

    first = attendance_corrections._process_claim(claim, now_utc=NOW)
    second = attendance_corrections._process_claim(claim, now_utc=NOW)

    assert first.status == "recoverable"
    assert second.status == "complete"
    assert len(audit_calls) == 2


def test_touched_days_include_both_sides_of_plant_midnight():
    from zira_dashboard import shift_config

    local_start = datetime(2026, 8, 31, 23, 30, tzinfo=shift_config.SITE_TZ)
    local_end = datetime(2026, 9, 1, 0, 30, tzinfo=shift_config.SITE_TZ)
    source = _source(
        end=local_end.astimezone(UTC),
        write_date=local_start.astimezone(UTC),
    )
    source["check_in_utc"] = local_start.astimezone(UTC)
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=local_start.astimezone(UTC),
        end_utc=local_end.astimezone(UTC),
        odoo_work_center_id=81,
        odoo_department_id=9,
    )

    days = attendance_corrections._touched_days(
        {7: (source,)},
        {7: plan},
        open_end=local_end.astimezone(UTC),
    )

    assert [day.isoformat() for day in days] == ["2026-08-31", "2026-09-01"]


def test_open_touched_days_use_claim_time_across_many_days_with_half_open_midnight():
    from zira_dashboard import shift_config

    local_start = datetime(2026, 8, 28, 23, 30, tzinfo=shift_config.SITE_TZ)
    local_midnight = datetime(2026, 9, 1, 0, 0, tzinfo=shift_config.SITE_TZ)
    source = _source(end=None, write_date=local_start.astimezone(UTC))
    source["check_in_utc"] = local_start.astimezone(UTC)
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=local_start.astimezone(UTC),
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )

    days = attendance_corrections._touched_days(
        {7: (source,)}, {7: plan}, open_end=local_midnight.astimezone(UTC)
    )

    assert [day.isoformat() for day in days] == [
        "2026-08-28",
        "2026-08-29",
        "2026-08-30",
        "2026-08-31",
    ]


def test_verified_rows_keep_validated_odoo_labels_for_the_mirror():
    from zira_dashboard import attendance_mirror

    source = {
        **_source(end=datetime(2026, 8, 31, 16, tzinfo=UTC), work_center=81),
        "employee_name": "Adrian A.",
        "odoo_work_center_name": "Repair 1",
        "odoo_department_name": "Recycled",
    }
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=datetime(2026, 8, 31, 16, tzinfo=UTC),
        odoo_work_center_id=81,
        odoo_department_id=9,
    )

    class Facade:
        def fetch_employee_attendance_rows(self, *_args):
            return [dict(source)]

    verified = attendance_corrections._verification_rows(
        Facade(), {7: plan}, (), NOW, datetime(2026, 8, 31, 16, tzinfo=UTC)
    )
    mirror_rows = attendance_mirror._normalized_rows(list(verified))

    assert mirror_rows[0]["employee_name"] == "Adrian A."
    assert mirror_rows[0]["odoo_work_center_name"] == "Repair 1"
    assert mirror_rows[0]["odoo_department_name"] == "Recycled"


def test_real_recalc_boundary_refreshes_each_cache_once_and_audit_retry_reuses_marker(
    monkeypatch,
):
    from zira_dashboard import _http_cache, attendance_recalc, staffing

    source = _source(work_center=81)
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    claim = _claim_for(
        plan,
        status="recalculating",
        completed=(
            {"stage": "mirror_complete"},
            {"stage": "recalc_enqueued", "recalc_ids": ["2026-08-31"]},
        ),
    )
    facade = _OpenCorrectionFacade(source)
    cache_ready = False
    cache_calls = []
    audit_calls = []

    monkeypatch.setattr(attendance_corrections, "_default_facade", lambda: facade)
    monkeypatch.setattr(attendance_corrections, "_claim_is_current", lambda _claim: True)
    monkeypatch.setattr(attendance_corrections, "_heartbeat_claim", lambda _claim: None)

    def transition(local_claim, **kwargs):
        if kwargs.get("status"):
            local_claim.row["status"] = kwargs["status"]
        return True

    def complete_record(local_claim, record, **_kwargs):
        if record.get("stage") == "recalc_horizon":
            local_claim.row["completed_operations"].insert(0, dict(record))
        else:
            local_claim.row["completed_operations"].append(dict(record))
        return True

    monkeypatch.setattr(attendance_corrections, "_transition", transition)
    monkeypatch.setattr(attendance_corrections, "_complete_record", complete_record)
    monkeypatch.setattr(
        attendance_corrections,
        "_recalc_complete",
        lambda _days: cache_ready,
    )
    recalc_claim = attendance_recalc.RecalcClaim(
        day=date(2026, 8, 31),
        attempt_count=1,
        lease_until=NOW + timedelta(minutes=15),
    )
    claims = [recalc_claim]
    monkeypatch.setattr(attendance_recalc, "_claim_pending_cache", lambda _now: None)
    monkeypatch.setattr(
        attendance_recalc, "_claim_next", lambda _now: claims.pop(0) if claims else None
    )
    monkeypatch.setattr(
        attendance_recalc,
        "_precompute_module",
        lambda: SimpleNamespace(prepare_day=lambda day, _client: SimpleNamespace(day=day)),
    )
    monkeypatch.setattr(attendance_recalc, "_default_production_client", lambda: object())

    def complete_claim(_claim, _prepared, _completed_at):
        return 1

    monkeypatch.setattr(attendance_recalc, "_complete_claim", complete_claim)

    def mark_cache_ready(_claim, _ready_at):
        nonlocal cache_ready
        cache_ready = True
        return True

    monkeypatch.setattr(attendance_recalc, "_mark_cache_ready", mark_cache_ready)
    monkeypatch.setattr(
        staffing,
        "invalidate_schedule_cache",
        lambda day: cache_calls.append(("staffing", day)),
    )
    monkeypatch.setattr(
        _http_cache,
        "invalidate_all_cache",
        lambda: cache_calls.append(("http", None)),
    )

    def audit(*_args, **_kwargs):
        audit_calls.append(True)
        if len(audit_calls) == 1:
            raise RuntimeError("audit database unavailable")
        return True

    monkeypatch.setattr(attendance_corrections, "_complete_with_audit", audit)

    first = attendance_corrections._process_claim(claim, now_utc=NOW)
    second = attendance_corrections._process_claim(claim, now_utc=NOW)

    assert first.status == "recoverable"
    assert second.status == "complete"
    assert cache_calls == [
        ("staffing", date(2026, 8, 31)),
        ("http", None),
    ]
    assert {item.get("stage") for item in claim.row["completed_operations"]} >= {"cache_refreshed"}
    assert len(audit_calls) == 2


def test_touched_days_bound_open_interval_at_correction_time():
    from zira_dashboard import shift_config

    local_start = datetime(2026, 8, 30, 23, 30, tzinfo=shift_config.SITE_TZ)
    local_now = datetime(2026, 9, 1, 0, 30, tzinfo=shift_config.SITE_TZ)
    source = _source(end=None, write_date=local_start.astimezone(UTC))
    source["check_in_utc"] = local_start.astimezone(UTC)
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=local_start.astimezone(UTC),
        end_utc=None,
        odoo_work_center_id=80,
        odoo_department_id=9,
    )

    days = attendance_corrections._touched_days(
        {7: (source,)},
        {7: plan},
        open_end=local_now.astimezone(UTC),
    )

    assert [day.isoformat() for day in days] == [
        "2026-08-30",
        "2026-08-31",
        "2026-09-01",
    ]
