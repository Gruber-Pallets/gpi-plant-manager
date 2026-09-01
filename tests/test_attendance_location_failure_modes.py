"""Release-blocking degraded-source scenarios for attendance locations."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
import os
import pytest

from zira_dashboard import (
    attendance_corrections,
    attendance_exceptions,
    attendance_mirror,
    attendance_readiness,
    attendance_sync,
    db,
)
from zira_dashboard import precompute, production_history


NOW = datetime(2026, 8, 31, 15, tzinfo=UTC)


class _RunBackend:
    def __init__(self):
        self.store_calls = 0
        self.rows = ()

    def sync_state(self):
        return attendance_sync.SyncState(None, None, None, None, 0, None)

    def record_incremental_started(self, _started_at):
        return None

    def store_incremental_cycle(self, rows, *_args, **_kwargs):
        self.store_calls += 1
        self.rows = tuple(dict(row) for row in rows)
        return set()

    def active_attendance_ids(self):
        return {901}

    def tombstoned_attendance_ids(self, _ids):
        return set()

    def store_full_sweep(self, *_args, **_kwargs):
        self.store_calls += 1
        return attendance_sync.SweepStoreResult(frozenset(), 0)


class _Backend:
    def __init__(self):
        self.run = _RunBackend()
        self.failures = []

    @contextmanager
    def logical_run(self):
        before_rows = self.run.rows
        before_calls = self.run.store_calls
        try:
            yield self.run
        except Exception:
            self.run.rows = before_rows
            self.run.store_calls = before_calls
            raise

    def record_failure(self, owner, error):
        self.failures.append((owner, str(error)))


def test_step_12_timeout_and_partial_sweep_never_commit_or_delete_mirror(monkeypatch):
    backend = _Backend()

    class Source:
        def fetch_attendance_changes(self, **_kwargs):
            raise TimeoutError("Odoo timed out")

        def fetch_open_attendance_rows(self):
            return []

        def fetch_complete_attendance_id_sweep(self):
            return attendance_sync.AttendanceIdSweepSnapshot((901,), complete=False)

    monkeypatch.setattr(attendance_sync, "_backend", backend)
    monkeypatch.setattr(attendance_sync, "_source", Source())

    incremental = attendance_sync.run_incremental_sync(now_utc=NOW)
    sweep = attendance_sync.run_full_sweep(now_utc=NOW)

    assert incremental.success is False
    assert sweep.success is False
    assert backend.run.store_calls == 0
    assert [owner for owner, _error in backend.failures] == ["incremental", "sweep"]


def test_step_12_last_good_mirror_survives_a_later_source_timeout(monkeypatch):
    backend = _Backend()
    row = {
        "odoo_attendance_id": 901,
        "employee_odoo_id": 44,
        "employee_name": "Worker 44",
        "check_in_utc": NOW - timedelta(hours=1),
        "check_out_utc": None,
        "odoo_work_center_id": 72,
        "odoo_work_center_name": "Raw Repair",
        "odoo_department_id": 8,
        "odoo_department_name": "01 Recycled",
        "odoo_write_date": NOW - timedelta(minutes=1),
    }

    class Source:
        failing = False

        def fetch_attendance_changes(self, **_kwargs):
            if self.failing:
                raise TimeoutError("Odoo timed out after a good cycle")
            return [row]

        def fetch_open_attendance_rows(self):
            return [row]

    source = Source()
    monkeypatch.setattr(attendance_sync, "_backend", backend)
    monkeypatch.setattr(attendance_sync, "_source", source)
    monkeypatch.setattr(
        attendance_sync,
        "_enqueue_department_repairs_after_sync",
        lambda *_args, **_kwargs: None,
    )

    assert attendance_sync.run_incremental_sync(now_utc=NOW).success is True
    last_good = backend.run.rows
    source.failing = True

    assert attendance_sync.run_incremental_sync(now_utc=NOW).success is False
    assert backend.run.rows == last_good


def test_step_12_failed_correction_verification_reread_keeps_last_good_mirror():
    source_row = {
        "odoo_attendance_id": 901,
        "employee_odoo_id": 44,
        "check_in_utc": NOW - timedelta(hours=1),
        "check_out_utc": NOW,
        "odoo_work_center_id": 999,
        "odoo_department_id": 8,
        "odoo_write_date": NOW - timedelta(minutes=1),
    }
    plan = attendance_corrections.plan_correction(
        rows=[source_row],
        employee_odoo_id=44,
        start_utc=source_row["check_in_utc"],
        end_utc=source_row["check_out_utc"],
        odoo_work_center_id=72,
        odoo_department_id=8,
    )
    operation = plan.operations[0]
    last_good_mirror = dict(source_row)

    class Facade:
        reads = 0
        writes = 0

        def fetch_attendance_rows_by_ids(self, _ids):
            self.reads += 1
            if self.reads >= 2:
                raise TimeoutError("verification reread timed out")
            return [dict(source_row)]

        def update_attendance_interval(self, _attendance_id, *, values):
            self.writes += 1
            source_row.update(values)

    facade = Facade()

    with pytest.raises(TimeoutError, match="verification reread timed out"):
        attendance_corrections._perform_operation(  # noqa: SLF001
            facade,
            operation,
            [last_good_mirror],
        )

    assert facade.writes == 1
    assert last_good_mirror["odoo_work_center_id"] == 999


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_step_12_stale_preview_fails_durable_job_before_any_odoo_write(monkeypatch):
    db.init_pool()
    start = datetime(2098, 2, 3, 13, tzinfo=UTC)
    day = start.date()
    employee_id = 991201
    attendance_id = 991202
    item_key = f"attendance_unmapped_location:{employee_id}:{attendance_id}:stale"
    preview_row = {
        "odoo_attendance_id": attendance_id,
        "employee_odoo_id": employee_id,
        "employee_name": f"Worker {employee_id}",
        "check_in_utc": start,
        "check_out_utc": start + timedelta(hours=2),
        "odoo_work_center_id": 999,
        "odoo_work_center_name": "Unknown",
        "odoo_department_id": 8,
        "odoo_department_name": "Production",
        "odoo_write_date": start,
    }
    plan = attendance_corrections.plan_correction(
        rows=[preview_row],
        employee_odoo_id=employee_id,
        start_utc=start,
        end_utc=start + timedelta(hours=2),
        odoo_work_center_id=72,
        odoo_department_id=8,
    )
    preview = attendance_corrections.CorrectionPreview(
        item_key=item_key,
        employee_odoo_ids=(employee_id,),
        target_work_center_name="WC B",
        target_odoo_work_center_id=72,
        target_odoo_department_id=8,
        start_utc=start,
        end_utc=start + timedelta(hours=2),
        plans=(plan,),
    )
    stale_row = {
        **preview_row,
        "odoo_work_center_id": 998,
        "odoo_write_date": start + timedelta(minutes=1),
    }

    class Facade:
        writes = 0

        def fetch_attendance_rows_by_ids(self, _ids):
            return [dict(stale_row)]

        def fetch_employee_attendance_rows(self, *_args, **_kwargs):
            return [dict(stale_row)]

        def update_attendance_interval(self, *_args, **_kwargs):
            self.writes += 1

        def create_attendance_interval(self, *_args, **_kwargs):
            self.writes += 1

        def delete_attendance_interval(self, *_args, **_kwargs):
            self.writes += 1

    facade = Facade()
    monkeypatch.setattr(attendance_corrections, "_default_facade", lambda: facade)
    monkeypatch.setattr(
        attendance_corrections,
        "_validate_applying_targets",
        lambda *_args, **_kwargs: None,
    )

    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM attendance_correction_job_events WHERE correction_job_id IN "
            "(SELECT id FROM attendance_correction_jobs WHERE item_key = %s)",
            (item_key,),
        )
        cur.execute("DELETE FROM attendance_correction_jobs WHERE item_key = %s", (item_key,))
        cur.execute("DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s", (attendance_id,))
        cur.execute("DELETE FROM production_daily WHERE day = %s AND emp_id = %s", (day, str(employee_id)))
        cur.execute("DELETE FROM inbox_open_items WHERE item_key = %s", (item_key,))
        cur.execute("DELETE FROM inbox_events WHERE item_key = %s", (item_key,))
        cur.execute(
            "INSERT INTO odoo_attendance_mirror "
            "(odoo_attendance_id, employee_odoo_id, employee_name, check_in_utc, "
            "check_out_utc, odoo_work_center_id, odoo_work_center_name, "
            "odoo_department_id, odoo_department_name, odoo_write_date, "
            "first_seen_at, last_seen_at) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                attendance_id,
                employee_id,
                preview_row["employee_name"],
                start,
                preview_row["check_out_utc"],
                999,
                "Unknown",
                8,
                "Production",
                start,
                start,
                start,
            ),
        )
        cur.execute(
            "INSERT INTO production_daily "
            "(day, emp_id, name, wc_name, units, downtime, hours, days_worked, "
            "excluded_minutes) VALUES (%s, %s, %s, %s, 7, 0, 2, 1, 0)",
            (day, str(employee_id), preview_row["employee_name"], "Legacy WC"),
        )
        cur.execute(
            "INSERT INTO inbox_open_items "
            "(item_key, item_kind, category_label, priority, first_seen, last_seen) "
            "VALUES (%s, 'attendance_unmapped_location', 'Unknown', 'urgent', %s, %s)",
            (item_key, start, start),
        )

    try:
        job_id = attendance_corrections.create_job_from_preview(
            preview=preview,
            actor_email="manager@example.com",
            actor_name="Manager",
        )
        result = attendance_corrections.process_job(job_id)

        assert result.status == "failed"
        assert facade.writes == 0
        assert db.query(
            "SELECT status FROM attendance_correction_jobs WHERE id = %s",
            (job_id,),
        ) == [{"status": "failed"}]
        assert db.query(
            "SELECT result FROM attendance_correction_job_events "
            "WHERE correction_job_id = %s AND phase = 'applying' ORDER BY id DESC LIMIT 1",
            (job_id,),
        ) == [{"result": "source_changed"}]
        assert db.query(
            "SELECT odoo_work_center_id FROM odoo_attendance_mirror "
            "WHERE odoo_attendance_id = %s",
            (attendance_id,),
        ) == [{"odoo_work_center_id": 999}]
        assert db.query(
            "SELECT units FROM production_daily WHERE day = %s AND emp_id = %s",
            (day, str(employee_id)),
        ) == [{"units": 7.0}]
        assert db.query(
            "SELECT item_key FROM inbox_open_items WHERE item_key = %s",
            (item_key,),
        ) == [{"item_key": item_key}]
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM inbox_open_items WHERE item_key = %s", (item_key,))
            cur.execute("DELETE FROM inbox_events WHERE item_key = %s", (item_key,))
            cur.execute(
                "DELETE FROM attendance_correction_job_events WHERE correction_job_id IN "
                "(SELECT id FROM attendance_correction_jobs WHERE item_key = %s)",
                (item_key,),
            )
            cur.execute("DELETE FROM attendance_correction_jobs WHERE item_key = %s", (item_key,))
            cur.execute("DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s", (attendance_id,))
            cur.execute("DELETE FROM production_daily WHERE day = %s AND emp_id = %s", (day, str(employee_id)))


def test_step_12_stale_or_partial_projection_is_blocked_not_reported_as_zero(monkeypatch):
    monkeypatch.setattr(
        attendance_readiness,
        "_collect_inputs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("failed verification reread")
        ),
    )

    report = attendance_readiness.build_report(NOW)

    assert report.ready is False
    assert "projection_incomplete" in report.blockers
    assert "baseline_incomplete" in report.blockers
    assert report.shadow_changed_worker_units == 0.0


def test_step_13_positive_total_without_samples_preserves_snapshot_and_raises_source_issue(
    monkeypatch,
):
    prior_snapshot = [{"emp_id": "101", "wc_name": "WC A", "units": 9.0}]
    stored = list(prior_snapshot)
    day = date(2026, 8, 31)

    with pytest.raises(
        production_history.ProductionSourceUnavailable,
        match="do not match",
    ):
        production_history._validate_strict_sample_totals(
            {"WC A": (9.0, 0.0)},
            {"WC A": []},
        )

    monkeypatch.setattr(
        precompute,
        "prepare_day",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            production_history.ProductionSourceUnavailable(
                "strict production samples for WC A do not match the adjusted total"
            )
        ),
    )
    monkeypatch.setattr(
        precompute,
        "store_prepared_day",
        lambda *_args, **_kwargs: stored.clear(),
    )
    monkeypatch.setattr(
        attendance_mirror,
        "enqueue_recalc",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(production_history.ProductionSourceUnavailable):
        precompute.precompute_day(day, object())
    issue = attendance_exceptions._production_unavailable_issue(
        day,
        NOW,
        "timestamped production samples are incomplete",
        comparison_only=False,
    )

    assert stored == prior_snapshot
    assert issue.kind == "production_source_unavailable"
    assert issue.priority == "urgent"
