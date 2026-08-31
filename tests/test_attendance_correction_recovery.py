from contextlib import contextmanager
from datetime import UTC, datetime

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
        claim.row["completed_operations"].append(dict(record))
        completed.append(dict(record))
        return True

    monkeypatch.setattr(attendance_corrections, "_default_facade", lambda: facade)
    monkeypatch.setattr(attendance_corrections, "_validate_applying_targets", lambda *_a: None)
    monkeypatch.setattr(attendance_corrections, "_claim_is_current", lambda _claim: True)
    monkeypatch.setattr(attendance_corrections, "_transition", transition)
    monkeypatch.setattr(attendance_corrections, "_complete_record", complete_record)

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
    monkeypatch.setattr(attendance_corrections, "_refresh_after_correction", lambda _days: None)
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


def test_audit_failure_retries_without_repeating_cache_refresh(monkeypatch):
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
    cache_calls = []
    audit_calls = []
    monkeypatch.setattr(
        attendance_corrections,
        "_refresh_after_correction",
        lambda days: cache_calls.append(tuple(days)),
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
    assert len(cache_calls) == 1
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
