import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from zira_dashboard import attendance_corrections


NOW = datetime(2026, 8, 31, 15, tzinfo=UTC)


def test_public_job_values_are_immutable_and_validate_inputs():
    preview_type = attendance_corrections.CorrectionPreview
    result_type = attendance_corrections.CorrectionJobResult

    preview = preview_type(
        item_key="production_unassigned_run:repair-1:1",
        employee_odoo_ids=(7,),
        target_work_center_name="Repair 1",
        target_odoo_work_center_id=81,
        target_odoo_department_id=9,
        start_utc=NOW,
        end_utc=None,
        plans=(),
    )
    result = result_type(job_id=4, status="planned", attempt_count=0)

    assert preview.employee_odoo_ids == (7,)
    assert result.status == "planned"
    with pytest.raises(AttributeError):
        preview.item_key = "changed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("item_key", " "),
        ("target_work_center_name", ""),
        ("employee_odoo_ids", [True]),
        ("employee_odoo_ids", [0]),
        ("start_utc", datetime(2026, 8, 31, 15)),
        ("end_utc", NOW),
    ],
)
def test_correction_preview_rejects_invalid_manager_input(monkeypatch, field, value):
    kwargs = {
        "item_key": "production_unassigned_run:repair-1:1",
        "employee_odoo_ids": [7],
        "target_work_center_name": "Repair 1",
        "start_utc": NOW,
        "end_utc": None,
    }
    kwargs[field] = value
    monkeypatch.setattr(
        attendance_corrections,
        "_build_preview",
        lambda **_kwargs: pytest.fail("invalid input reached Odoo"),
        raising=False,
    )

    with pytest.raises((TypeError, ValueError)):
        attendance_corrections.correction_preview(**kwargs)


def test_operation_order_puts_any_open_producing_update_last():
    closed = datetime(2026, 8, 31, 16, tzinfo=UTC)
    operations = (
        attendance_corrections.CorrectionOperation(
            key="attendance-correction-v2:7:" + "1" * 64,
            kind="update",
            attendance_id=11,
            employee_odoo_id=7,
            before={"check_out_utc": closed},
            after={"check_out_utc": None},
        ),
        attendance_corrections.CorrectionOperation(
            key="attendance-correction-v2:7:" + "2" * 64,
            kind="create",
            attendance_id=None,
            employee_odoo_id=7,
            before=None,
            after={
                "employee_odoo_id": 7,
                "check_in_utc": NOW,
                "check_out_utc": closed,
                "odoo_work_center_id": 81,
                "odoo_department_id": 9,
            },
        ),
    )

    ordered = attendance_corrections._ordered_operations(
        operations,
        source_rows=(
            {
                "odoo_attendance_id": 11,
                "employee_odoo_id": 7,
                "check_in_utc": NOW,
                "check_out_utc": closed,
                "odoo_work_center_id": 80,
                "odoo_department_id": 9,
                "odoo_write_date": NOW,
            },
        ),
    )

    assert [operation.kind for operation in ordered] == ["create", "update"]
    assert ordered[-1].after["check_out_utc"] is None


def test_event_details_reject_pii_and_are_bounded():
    with pytest.raises(ValueError, match="allowlisted"):
        attendance_corrections._event_detail(actor_email="manager@example.com")
    with pytest.raises(ValueError, match="bounded"):
        attendance_corrections._event_detail(reason_code="x" * 201)


def test_preview_canonicalizes_employee_ids_before_any_source_read(monkeypatch):
    seen = []

    def build(**kwargs):
        seen.append(kwargs)
        return attendance_corrections.CorrectionPreview(
            item_key=kwargs["item_key"],
            employee_odoo_ids=kwargs["employee_odoo_ids"],
            target_work_center_name=kwargs["target_work_center_name"],
            target_odoo_work_center_id=81,
            target_odoo_department_id=None,
            start_utc=kwargs["start_utc"],
            end_utc=kwargs["end_utc"],
            plans=(),
        )

    monkeypatch.setattr(attendance_corrections, "_build_preview", build)

    result = attendance_corrections.correction_preview(
        item_key="  production_unassigned_run:repair-1:1  ",
        employee_odoo_ids=[9, 7, 9],
        target_work_center_name="  Repair 1  ",
        start_utc=NOW,
        end_utc=None,
    )

    assert result.employee_odoo_ids == (7, 9)
    assert seen[0]["item_key"] == "production_unassigned_run:repair-1:1"
    assert seen[0]["target_work_center_name"] == "Repair 1"


def _row(
    attendance_id=11,
    *,
    start=NOW,
    end=datetime(2026, 8, 31, 17, tzinfo=UTC),
    work_center=80,
    write_date=NOW,
):
    return {
        "odoo_attendance_id": attendance_id,
        "employee_odoo_id": 7,
        "check_in_utc": start,
        "check_out_utc": end,
        "odoo_work_center_id": work_center,
        "odoo_department_id": 9,
        "odoo_write_date": write_date,
    }


def test_source_snapshot_reconstructs_exact_before_state_for_split_update():
    start = datetime(2026, 8, 31, 16, tzinfo=UTC)
    end = datetime(2026, 8, 31, 16, 30, tzinfo=UTC)
    plan = attendance_corrections.plan_correction(
        rows=[_row()],
        employee_odoo_id=7,
        start_utc=start,
        end_utc=end,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    preview = attendance_corrections.CorrectionPreview(
        item_key="production_unassigned_run:repair-1:1",
        employee_odoo_ids=(7,),
        target_work_center_name="Repair 1",
        target_odoo_work_center_id=81,
        target_odoo_department_id=9,
        start_utc=start,
        end_utc=end,
        plans=(plan,),
    )

    payload = attendance_corrections._snapshot_payload(preview)
    decoded = attendance_corrections._source_rows_from_json(payload, (7,))

    assert decoded[7] == (_row(),)


def test_source_snapshot_integrity_and_old_plan_schema_fail_closed():
    plan = attendance_corrections.plan_correction(
        rows=[],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=None,
    )
    preview = attendance_corrections.CorrectionPreview(
        item_key="production_unassigned_run:repair-1:1",
        employee_odoo_ids=(7,),
        target_work_center_name="Repair 1",
        target_odoo_work_center_id=81,
        target_odoo_department_id=None,
        start_utc=NOW,
        end_utc=None,
        plans=(plan,),
    )
    payload = attendance_corrections._snapshot_payload(preview)
    payload["integrity"] = "attendance-correction-source-v1:" + "0" * 64

    with pytest.raises(ValueError, match="integrity"):
        attendance_corrections._source_rows_from_json(payload, (7,))
    with pytest.raises(ValueError, match="schema"):
        attendance_corrections._plans_from_json({"schema_version": 1, "plans": []}, (7,))


def test_job_plan_wrapper_round_trips_hardened_request_and_full_source():
    source = {
        **_row(),
        "employee_name": "Display-only name",
        "unrelated_raw_field": "kept inside the authenticated plan",
    }
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    preview = attendance_corrections.CorrectionPreview(
        item_key="production_unassigned_run:repair-1:1",
        employee_odoo_ids=(7,),
        target_work_center_name="Repair 1",
        target_odoo_work_center_id=81,
        target_odoo_department_id=9,
        start_utc=NOW,
        end_utc=None,
        plans=(plan,),
    )

    payload = attendance_corrections._plans_payload(preview)
    decoded = attendance_corrections._plans_from_json(payload, (7,))[7]

    assert attendance_corrections.plan_to_json(decoded) == payload["plans"][0]["plan"]
    assert decoded.request == plan.request
    assert decoded.source_intervals == plan.source_intervals


def test_plan_wrapper_employee_must_match_authenticated_request():
    plan = attendance_corrections.plan_correction(
        rows=[_row()],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    payload = {
        "schema_version": 2,
        "plans": [
            {
                "employee_odoo_id": 8,
                "plan": attendance_corrections.plan_to_json(plan),
            }
        ],
    }

    with pytest.raises(ValueError, match="employee"):
        attendance_corrections._plans_from_json(payload, (8,))


def test_active_duplicate_returns_before_preview_or_write(monkeypatch):
    monkeypatch.setattr(attendance_corrections, "_active_job_id", lambda _key: 44)
    monkeypatch.setattr(
        attendance_corrections,
        "_build_preview",
        lambda **_kwargs: pytest.fail("duplicate re-read Odoo"),
    )

    job_id = attendance_corrections.create_job(
        item_key="production_unassigned_run:repair-1:1",
        employee_odoo_ids=[7],
        target_work_center_name="Repair 1",
        start_utc=NOW,
        end_utc=None,
        actor_email="manager@example.com",
        actor_name="Manager",
    )

    assert job_id == 44


def test_create_job_from_preview_persists_exact_plan_without_rereading_odoo(
    monkeypatch,
):
    from zira_dashboard import db

    plan = attendance_corrections.plan_correction(
        rows=[],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    preview = attendance_corrections.CorrectionPreview(
        item_key="production_unassigned_run:repair-1:1",
        employee_odoo_ids=(7,),
        target_work_center_name="Repair 1",
        target_odoo_work_center_id=81,
        target_odoo_department_id=9,
        start_utc=NOW,
        end_utc=None,
        plans=(plan,),
    )
    statements = []

    class Cursor:
        def __init__(self):
            self.response = None

        def execute(self, sql, params=None):
            statements.append((" ".join(sql.split()), params))
            self.response = {"id": 51} if sql.startswith(
                "INSERT INTO attendance_correction_jobs"
            ) else None

        def fetchone(self):
            response = self.response
            self.response = None
            return response

    @contextmanager
    def cursor():
        yield Cursor()

    monkeypatch.setattr(attendance_corrections, "_active_job_id", lambda _key: None)
    monkeypatch.setattr(
        attendance_corrections,
        "_build_preview",
        lambda **kwargs: pytest.fail(f"persisted preview re-read Odoo: {kwargs}"),
    )
    monkeypatch.setattr(db, "cursor", cursor)

    job_id = attendance_corrections.create_job_from_preview(
        preview=preview,
        actor_email="manager@example.com",
        actor_name="Manager",
    )

    insert = next(item for item in statements if item[0].startswith(
        "INSERT INTO attendance_correction_jobs"
    ))
    assert job_id == 51
    assert insert[1][7] == json.dumps(
        attendance_corrections._plans_payload(preview), separators=(",", ":")
    )
    assert insert[1][8:] == ("manager@example.com", "Manager")


def test_create_job_from_preview_rejects_request_plan_mismatch_before_dedupe(
    monkeypatch,
):
    plan = attendance_corrections.plan_correction(
        rows=[],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=81,
        odoo_department_id=9,
    )
    mismatched = attendance_corrections.CorrectionPreview(
        item_key="production_unassigned_run:repair-1:1",
        employee_odoo_ids=(7,),
        target_work_center_name="Repair 1",
        target_odoo_work_center_id=82,
        target_odoo_department_id=9,
        start_utc=NOW,
        end_utc=None,
        plans=(plan,),
    )
    monkeypatch.setattr(
        attendance_corrections,
        "_active_job_id",
        lambda _key: pytest.fail("invalid preview reached durable dedupe"),
    )

    with pytest.raises(ValueError, match="validated request"):
        attendance_corrections.create_job_from_preview(
            preview=mismatched,
            actor_email="manager@example.com",
            actor_name="Manager",
        )


def test_oldest_claim_uses_skip_locked_attempt_fence_and_durable_event(monkeypatch):
    from zira_dashboard import db

    statements = []

    class Cursor:
        def __init__(self):
            self.response = None

        def execute(self, sql, params=None):
            statements.append((" ".join(sql.split()), params))
            if sql.startswith("SELECT * FROM attendance_correction_jobs"):
                self.response = {
                    "id": 5,
                    "status": "planned",
                    "attempt_count": 0,
                    "created_at": NOW - timedelta(hours=1),
                }
            elif sql.startswith("UPDATE attendance_correction_jobs SET status"):
                self.response = {
                    "id": 5,
                    "status": "applying",
                    "attempt_count": 1,
                }
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

    claim = attendance_corrections._claim_job(job_id=None, now_utc=NOW)

    assert claim.attempt_count == 1
    assert claim.lease_until == NOW + timedelta(minutes=15)
    assert "ORDER BY created_at ASC, id ASC" in statements[0][0]
    assert "FOR UPDATE SKIP LOCKED" in statements[0][0]
    assert any(
        sql.startswith("INSERT INTO attendance_correction_job_events")
        and "manager@example.com" not in str(params)
        for sql, params in statements
    )


def test_default_odoo_facade_resolves_target_department_and_plan_never_clears_it(
    monkeypatch,
):
    from zira_dashboard import db, odoo_client

    source = _row(work_center=80)
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
    monkeypatch.setattr(odoo_client, "fetch_employee_statuses", lambda: [{"id": 7, "active": True}])
    monkeypatch.setattr(
        odoo_client,
        "fetch_employee_attendance_rows",
        lambda *_args: [dict(source)],
    )
    monkeypatch.setattr(odoo_client, "_app_wc_name_for_odoo_id", lambda _wc_id: "Repair 1")
    monkeypatch.setattr(odoo_client, "_department_id_for_wc", lambda _name, **_kwargs: 44)
    monkeypatch.setattr(attendance_corrections, "_default_facade", lambda: odoo_client)

    preview = attendance_corrections.correction_preview(
        item_key="production_unassigned_run:repair-1:1",
        employee_odoo_ids=[7],
        target_work_center_name="Repair 1",
        start_utc=NOW,
        end_utc=None,
    )

    assert preview.target_odoo_department_id == 44
    updates = [operation for operation in preview.plans[0].operations if operation.kind == "update"]
    assert updates
    assert all(operation.after.get("odoo_department_id") == 44 for operation in updates)


def test_missing_target_department_fails_before_source_read_or_job_persistence(
    monkeypatch,
):
    from zira_dashboard import db, odoo_client

    source_reads = []
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
    monkeypatch.setattr(odoo_client, "fetch_employee_statuses", lambda: [{"id": 7, "active": True}])
    monkeypatch.setattr(
        odoo_client,
        "fetch_employee_attendance_rows",
        lambda *_args: source_reads.append(True) or [],
    )
    monkeypatch.setattr(odoo_client, "_app_wc_name_for_odoo_id", lambda _wc_id: "Repair 1")
    monkeypatch.setattr(odoo_client, "_department_id_for_wc", lambda _name, **_kwargs: None)
    monkeypatch.setattr(attendance_corrections, "_default_facade", lambda: odoo_client)

    with pytest.raises(ValueError, match="department"):
        attendance_corrections.correction_preview(
            item_key="production_unassigned_run:repair-1:1",
            employee_odoo_ids=[7],
            target_work_center_name="Repair 1",
            start_utc=NOW,
            end_utc=None,
        )

    assert source_reads == []


def test_active_operation_reservation_is_not_reported_as_completed_progress():
    claim = attendance_corrections._JobClaim(
        job_id=5,
        attempt_count=2,
        lease_until=NOW + timedelta(minutes=15),
        row={
            "completed_operations": [
                {
                    "operation_key": "attendance-correction-v2:7:YQ:" + "1" * 64,
                    "reservation_token": "a" * 32,
                    "reservation_attempt_count": 2,
                    "reservation_until": "2026-08-31T15:15:00Z",
                }
            ]
        },
    )

    result = attendance_corrections._result(claim, "recoverable")

    assert result.completed_operation_count == 0
