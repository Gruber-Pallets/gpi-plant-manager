import json
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import os
from threading import Barrier

import pytest

from zira_dashboard import attendance_corrections, db


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


def _job_preview(
    *,
    item_key="production_unassigned_run:repair-1:1",
    target_name="Repair 1",
    target_id=81,
    source_write_date=NOW,
):
    plan = attendance_corrections.plan_correction(
        rows=[_row(write_date=source_write_date)],
        employee_odoo_id=7,
        start_utc=NOW,
        end_utc=None,
        odoo_work_center_id=target_id,
        odoo_department_id=9,
    )
    return attendance_corrections.CorrectionPreview(
        item_key=item_key,
        employee_odoo_ids=(7,),
        target_work_center_name=target_name,
        target_odoo_work_center_id=target_id,
        target_odoo_department_id=9,
        start_utc=NOW,
        end_utc=None,
        plans=(plan,),
    )


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


def test_exact_preview_job_creation_never_rebuilds_the_plan(monkeypatch):
    preview = _job_preview()
    statements = []

    class Cursor:
        response = None

        def execute(self, sql, params=None):
            statements.append((" ".join(sql.split()), params))
            self.response = {"id": 44} if "RETURNING id" in sql else None

        def fetchone(self):
            return self.response

    @contextmanager
    def cursor():
        yield Cursor()

    from zira_dashboard import db

    monkeypatch.setattr(db, "cursor", cursor)
    monkeypatch.setattr(
        attendance_corrections,
        "_build_preview",
        lambda **_kwargs: pytest.fail("verified preview was rebuilt"),
    )

    job_id = attendance_corrections.create_job_from_preview(
        preview=preview,
        actor_email="manager@example.com",
        actor_name="Manager",
    )

    assert job_id == 44
    insert = next(
        item for item in statements if item[0].startswith("INSERT INTO attendance_correction_jobs")
    )
    assert json.loads(insert[1][5]) == [7]
    assert json.loads(insert[1][6]) == attendance_corrections._snapshot_payload(preview)
    assert json.loads(insert[1][7]) == attendance_corrections._plans_payload(preview)


def test_legacy_create_job_builds_once_then_delegates_exact_preview(monkeypatch):
    preview = _job_preview()
    builds = []
    persisted = []
    monkeypatch.setattr(
        attendance_corrections,
        "_build_preview",
        lambda **kwargs: builds.append(kwargs) or preview,
    )
    monkeypatch.setattr(
        attendance_corrections,
        "create_job_from_preview",
        lambda **kwargs: persisted.append(kwargs) or 144,
    )

    job_id = attendance_corrections.create_job(
        item_key=preview.item_key,
        employee_odoo_ids=preview.employee_odoo_ids,
        target_work_center_name=preview.target_work_center_name,
        start_utc=preview.start_utc,
        end_utc=preview.end_utc,
        actor_email="manager@example.com",
        actor_name="Manager",
    )

    assert job_id == 144
    assert len(builds) == 1
    assert persisted == [
        {
            "preview": preview,
            "actor_email": "manager@example.com",
            "actor_name": "Manager",
        }
    ]


def test_dedupe_winner_is_validated_by_id_even_after_becoming_terminal(monkeypatch):
    preview = _job_preview()
    source_snapshot = attendance_corrections._snapshot_payload(preview)
    plans = attendance_corrections._plans_payload(preview)
    statements = []

    class Cursor:
        response = None

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            statements.append(normalized)
            if normalized.startswith("INSERT INTO attendance_correction_jobs"):
                self.response = None
            elif normalized.startswith("SELECT id, status"):
                self.response = {
                    "id": 45,
                    "status": "complete",
                    "target_work_center_name": preview.target_work_center_name,
                    "target_odoo_work_center_id": preview.target_odoo_work_center_id,
                    "start_utc": preview.start_utc,
                    "end_utc": preview.end_utc,
                    "employee_odoo_ids": list(preview.employee_odoo_ids),
                    "source_snapshot": source_snapshot,
                    "operations": plans,
                }
            else:
                self.response = None

        def fetchone(self):
            return self.response

    @contextmanager
    def cursor():
        yield Cursor()

    from zira_dashboard import db

    monkeypatch.setattr(db, "cursor", cursor)

    assert (
        attendance_corrections.create_job_from_preview(
            preview=preview,
            actor_email="manager@example.com",
            actor_name="Manager",
        )
        == 45
    )
    assert any("WHERE item_key = %s" in sql and "status IN" not in sql for sql in statements)


def test_reusable_job_binding_is_reloaded_by_id_after_becoming_terminal(monkeypatch):
    preview = _job_preview()
    binding = attendance_corrections.preview_job_binding(preview)
    source_snapshot = attendance_corrections._snapshot_payload(preview)
    plans = attendance_corrections._plans_payload(preview)
    queries = []

    def query(sql, params=()):
        normalized = " ".join(sql.split())
        queries.append((normalized, params))
        if "status IN" in normalized:
            return [{"id": 47}]
        return [
            {
                "id": 47,
                "status": "complete",
                "item_key": preview.item_key,
                "target_work_center_name": preview.target_work_center_name,
                "target_odoo_work_center_id": preview.target_odoo_work_center_id,
                "start_utc": preview.start_utc,
                "end_utc": preview.end_utc,
                "employee_odoo_ids": list(preview.employee_odoo_ids),
                "source_snapshot": source_snapshot,
                "operations": plans,
            }
        ]

    monkeypatch.setattr(db, "query", query)

    assert (
        attendance_corrections.find_reusable_job_for_binding(
            item_key=preview.item_key,
            binding=binding,
        )
        == 47
    )
    assert "status IN" in queries[0][0]
    assert "WHERE id = %s" in queries[1][0]
    assert "status IN" not in queries[1][0]


@pytest.mark.parametrize(
    "changed",
    [
        _job_preview(target_name="Repair 1", target_id=82),
        _job_preview(source_write_date=NOW + timedelta(minutes=1)),
    ],
    ids=("mapping", "source"),
)
def test_reusable_job_binding_rejects_changed_mapping_or_source(monkeypatch, changed):
    preview = _job_preview()

    def query(sql, params=()):
        if "status IN" in sql:
            return [{"id": 48}]
        return [
            {
                "id": 48,
                "status": "planned",
                "item_key": changed.item_key,
                "target_work_center_name": changed.target_work_center_name,
                "target_odoo_work_center_id": changed.target_odoo_work_center_id,
                "start_utc": changed.start_utc,
                "end_utc": changed.end_utc,
                "employee_odoo_ids": list(changed.employee_odoo_ids),
                "source_snapshot": attendance_corrections._snapshot_payload(changed),
                "operations": attendance_corrections._plans_payload(changed),
            }
        ]

    monkeypatch.setattr(db, "query", query)

    with pytest.raises(attendance_corrections.CorrectionRequestConflict) as conflict:
        attendance_corrections.find_reusable_job_for_binding(
            item_key=preview.item_key,
            binding=attendance_corrections.preview_job_binding(preview),
        )
    assert conflict.value.job_id == 48
    assert conflict.value.source_changed is True


@pytest.mark.parametrize(
    "changed",
    [
        _job_preview(target_name="Repair 1", target_id=82),
        _job_preview(source_write_date=NOW + timedelta(minutes=1)),
    ],
    ids=("mapping", "source"),
)
def test_dedupe_winner_rejects_changed_mapping_or_source(monkeypatch, changed):
    preview = _job_preview()

    class Cursor:
        response = None

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("INSERT INTO attendance_correction_jobs"):
                self.response = None
            elif normalized.startswith("SELECT id, status"):
                self.response = {
                    "id": 46,
                    "status": "planned",
                    "target_work_center_name": changed.target_work_center_name,
                    "target_odoo_work_center_id": changed.target_odoo_work_center_id,
                    "start_utc": changed.start_utc,
                    "end_utc": changed.end_utc,
                    "employee_odoo_ids": list(changed.employee_odoo_ids),
                    "source_snapshot": attendance_corrections._snapshot_payload(changed),
                    "operations": attendance_corrections._plans_payload(changed),
                }

        def fetchone(self):
            return self.response

    @contextmanager
    def cursor():
        yield Cursor()

    from zira_dashboard import db

    monkeypatch.setattr(db, "cursor", cursor)

    with pytest.raises(attendance_corrections.CorrectionRequestConflict) as conflict:
        attendance_corrections.create_job_from_preview(
            preview=preview,
            actor_email="manager@example.com",
            actor_name="Manager",
        )
    assert conflict.value.source_changed is True


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_concurrent_exact_preview_creation_has_one_validated_winner(monkeypatch):
    from zira_dashboard import db

    item_key = "production_unassigned_run:task-10-concurrency:2098-08-31T15:00:00+00:00"
    previews = (
        _job_preview(item_key=item_key, target_name="Repair 1", target_id=81),
        _job_preview(item_key=item_key, target_name="Repair 2", target_id=82),
    )
    real_cursor = db.cursor
    both_ready = Barrier(2)

    @contextmanager
    def synchronized_cursor():
        with real_cursor() as cursor:
            first_insert = True

            class Cursor:
                def execute(self, sql, params=None):
                    nonlocal first_insert
                    if first_insert and sql.startswith("INSERT INTO attendance_correction_jobs"):
                        first_insert = False
                        both_ready.wait(timeout=3)
                    return cursor.execute(sql, params)

                def __getattr__(self, name):
                    return getattr(cursor, name)

            yield Cursor()

    db.init_pool()
    db.bootstrap_schema()
    db.execute(
        "DELETE FROM attendance_correction_job_events WHERE correction_job_id IN "
        "(SELECT id FROM attendance_correction_jobs WHERE item_key = %s)",
        (item_key,),
    )
    db.execute("DELETE FROM attendance_correction_jobs WHERE item_key = %s", (item_key,))
    try:
        monkeypatch.setattr(db, "cursor", synchronized_cursor)

        def create(preview):
            try:
                return attendance_corrections.create_job_from_preview(
                    preview=preview,
                    actor_email="manager@example.com",
                    actor_name="Manager",
                )
            except attendance_corrections.CorrectionRequestConflict as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create, previews))

        monkeypatch.setattr(db, "cursor", real_cursor)
        assert sum(isinstance(value, int) for value in results) == 1
        assert (
            sum(
                isinstance(value, attendance_corrections.CorrectionRequestConflict)
                for value in results
            )
            == 1
        )
        assert db.query(
            "SELECT COUNT(*) AS count FROM attendance_correction_jobs "
            "WHERE item_key = %s AND status IN ('planned','applying','verifying','recalculating')",
            (item_key,),
        ) == [{"count": 1}]
    finally:
        monkeypatch.setattr(db, "cursor", real_cursor)
        db.execute(
            "DELETE FROM attendance_correction_job_events WHERE correction_job_id IN "
            "(SELECT id FROM attendance_correction_jobs WHERE item_key = %s)",
            (item_key,),
        )
        db.execute("DELETE FROM attendance_correction_jobs WHERE item_key = %s", (item_key,))


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
            self.response = (
                {"id": 51} if sql.startswith("INSERT INTO attendance_correction_jobs") else None
            )

        def fetchone(self):
            response = self.response
            self.response = None
            return response

    @contextmanager
    def cursor():
        yield Cursor()

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

    insert = next(
        item for item in statements if item[0].startswith("INSERT INTO attendance_correction_jobs")
    )
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
        db,
        "cursor",
        lambda: pytest.fail("invalid preview reached durable dedupe"),
    )

    with pytest.raises(ValueError, match="authenticated plan"):
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
