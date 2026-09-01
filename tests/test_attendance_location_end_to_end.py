"""One readable end-to-end proof of the attendance-location truth chain."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
import os

import pytest

from zira_dashboard import (
    assignment_windows,
    attendance_corrections,
    attendance_mirror,
    attendance_recalc,
    attendance_sync,
    attendance_timeline,
    inbox_reconcile,
    precompute,
    production_history,
    timeclock_sync,
    app_settings,
    db,
    exception_inbox,
    plant_day,
)


START = datetime(2026, 8, 31, 12, tzinfo=UTC)


def _row(
    attendance_id: int,
    employee_id: int,
    name: str,
    start: datetime,
    end: datetime | None,
    wc_id: int | None,
    wc_name: str | None,
) -> dict:
    return {
        "odoo_attendance_id": attendance_id,
        "employee_odoo_id": employee_id,
        "employee_name": name,
        "check_in_utc": start,
        "check_out_utc": end,
        "odoo_work_center_id": wc_id,
        "odoo_work_center_name": wc_name,
        "odoo_department_id": 8,
        "odoo_department_name": "01 Recycled",
        "odoo_write_date": START - timedelta(seconds=1),
    }


def _project(rows, as_of):
    return attendance_timeline.project_rows(
        rows,
        as_of_utc=as_of,
        verified_through_utc=as_of,
        map_work_center={71: "WC A", 72: "WC B"}.get,
        requires_work_center=lambda _department: True,
        expected_department_id=lambda _wc: 8,
    )


def _credit(spans, samples):
    segments = assignment_windows.work_segments_from_timeline(
        spans,
        window_start_utc=START,
        window_end_utc=START + timedelta(hours=8),
    )
    totals = {
        wc: (sum(units for _at, units in values), 0.0)
        for wc, values in samples.items()
    }
    return production_history.attribute_for_segments(
        segments,
        wc_totals=totals,
        samples_by_wc=samples,
        productive_minutes=lambda _person, _wc, start, end: (
            end - start
        ).total_seconds()
        / 60,
        strict=True,
    )


def test_exact_attendance_location_journey_from_clock_in_through_historical_edit():
    """Steps 1-11 from the Task 13 release proof, in source order."""
    # 1-2. Plant Manager opens a WC-less attendance. Grace is visible, and
    # production has no invented owner.
    open_row = _row(1, 101, "Worker One", START, None, None, None)
    grace_spans = _project([open_row], START + timedelta(minutes=4))
    assert [span.status for span in grace_spans] == ["pending_first_location"]
    unassigned = production_history.unassigned_runs_for_samples(
        [(START + timedelta(minutes=2), 5.0)],
        set(),
        ((START, START + timedelta(minutes=4)),),
        wc_name="WC A",
    )
    assert sum(run.units for run in unassigned) == 5.0

    # 3-5. Luke supplies WC A, then transfers the same durable employee to B;
    # samples on either side follow the transfer boundary.
    transferred = [
        _row(2, 101, "Worker One", START + timedelta(minutes=5), START + timedelta(hours=2), 71, "Raw A"),
        _row(3, 101, "Worker One", START + timedelta(hours=2), START + timedelta(hours=4), 72, "Raw B"),
    ]
    transfer_spans = _project(transferred, START + timedelta(hours=4))
    assert [(span.app_work_center_name, span.status) for span in transfer_spans] == [
        ("WC A", "valid"),
        ("WC B", "valid"),
    ]
    credited = _credit(
        transfer_spans,
        {
            "WC A": [(START + timedelta(hours=1), 10.0)],
            "WC B": [(START + timedelta(hours=3), 20.0)],
        },
    )
    assert credited[(101, "Worker One")]["WC A"]["units"] == 10.0
    assert credited[(101, "Worker One")]["WC B"]["units"] == 20.0

    # 6. A second canonical worker at WC B splits the same sample equally.
    shared_rows = [
        _row(4, 101, "Worker One", START + timedelta(hours=2), START + timedelta(hours=4), 72, "Raw B"),
        _row(5, 102, "Worker Two", START + timedelta(hours=2), START + timedelta(hours=4), 72, "Raw B"),
    ]
    shared = _credit(
        _project(shared_rows, START + timedelta(hours=4)),
        {"WC B": [(START + timedelta(hours=3), 20.0)]},
    )
    assert shared[(101, "Worker One")]["WC B"]["units"] == 10.0
    assert shared[(102, "Worker Two")]["WC B"]["units"] == 10.0

    # 7-8. Conflicts and unknown raw work centers remain exact and uncredited.
    conflict = _project(
        [
            _row(6, 101, "Worker One", START, START + timedelta(hours=1), 71, "Raw A"),
            _row(7, 101, "Worker One", START, START + timedelta(hours=1), 72, "Raw B"),
        ],
        START + timedelta(hours=1),
    )
    assert [span.status for span in conflict] == ["conflicting_location"]
    unknown = _project(
        [_row(8, 101, "Worker One", START, START + timedelta(hours=1), 999, "Luke Mystery")],
        START + timedelta(hours=1),
    )
    assert unknown[0].status == "unmapped_location"
    assert unknown[0].odoo_work_center_name == "Luke Mystery"

    # 9. A manager's verified interval surgery replaces the bad overlap with
    # two closed, non-overlapping source rows; the same projection now credits.
    corrected = _project(transferred, START + timedelta(hours=4))
    assert all(span.status == "valid" for span in corrected)
    assert sum(
        totals["units"]
        for wc_map in _credit(
            corrected,
            {
                "WC A": [(START + timedelta(hours=1), 8.0)],
                "WC B": [(START + timedelta(hours=3), 12.0)],
            },
        ).values()
        for totals in wc_map.values()
    ) == 20.0

    # 10. Plant Manager clock-out closes the final source interval exactly.
    assert corrected[-1].end_utc == START + timedelta(hours=4)

    # 11. A post-baseline edit targets every local day touched by the source,
    # including an overnight historical interval, for strict recalculation.
    touched = attendance_mirror.local_days_touched(
        datetime(2026, 8, 30, 23, 30, tzinfo=UTC),
        datetime(2026, 8, 31, 13, 0, tzinfo=UTC),
    )
    assert touched == {date(2026, 8, 30), date(2026, 8, 31)}


class _JourneyOdoo:
    """One stateful fake at the same facade used by punch, sync, and correction."""

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.next_id = 1
        self.version = START - timedelta(seconds=1)

    def _tick(self):
        self.version += timedelta(seconds=1)
        return self.version

    def _new(self, employee_id, start, end, wc_id, department_id=8):
        attendance_id = self.next_id
        self.next_id += 1
        self.rows[attendance_id] = _row(
            attendance_id,
            employee_id,
            f"Worker {employee_id}",
            start,
            end,
            wc_id,
            {71: "Raw A", 72: "Raw B"}.get(wc_id),
        )
        self.rows[attendance_id]["odoo_department_id"] = department_id
        self.rows[attendance_id]["odoo_write_date"] = self._tick()
        return attendance_id

    def get_current_attendance(self, employee_id):
        open_rows = [
            row
            for row in self.rows.values()
            if row["employee_odoo_id"] == employee_id and row["check_out_utc"] is None
        ]
        if not open_rows:
            return None
        row = max(open_rows, key=lambda value: value["check_in_utc"])
        return {"id": row["odoo_attendance_id"]}

    def clock_in(self, employee_id, _wc_name, at, *, odoo_department_id=None):
        return self._new(
            employee_id,
            at,
            None,
            None,
            department_id=odoo_department_id or 8,
        )

    def close_all_open_attendance_rows(self, employee_id, at):
        closed = []
        for row in self.rows.values():
            if row["employee_odoo_id"] == employee_id and row["check_out_utc"] is None:
                row["check_out_utc"] = at
                row["odoo_write_date"] = self._tick()
                closed.append(row["odoo_attendance_id"])
        return tuple(sorted(closed))

    def fetch_attendance_changes(self, **_kwargs):
        return [dict(row) for row in self.rows.values()]

    def fetch_open_attendance_rows(self):
        return [dict(row) for row in self.rows.values() if row["check_out_utc"] is None]

    def fetch_attendance_rows_by_ids(self, ids):
        return [dict(self.rows[value]) for value in ids if value in self.rows]

    def fetch_employee_attendance_rows(self, employee_id, start, end):
        infinity = datetime.max.replace(tzinfo=UTC)
        return [
            dict(row)
            for row in self.rows.values()
            if row["employee_odoo_id"] == employee_id
            and row["check_in_utc"] < (end or infinity)
            and (row["check_out_utc"] or infinity) > start
        ]

    def update_attendance_interval(self, attendance_id, *, values):
        self.rows[attendance_id].update(values)
        self.rows[attendance_id]["odoo_write_date"] = self._tick()

    def create_attendance_interval(
        self,
        *,
        employee_odoo_id,
        check_in_utc,
        check_out_utc,
        odoo_work_center_id,
        odoo_department_id,
    ):
        return self._new(
            employee_odoo_id,
            check_in_utc,
            check_out_utc,
            odoo_work_center_id,
            odoo_department_id,
        )

    def delete_attendance_interval(self, attendance_id):
        self.rows.pop(attendance_id)


class _JourneyMirror:
    def __init__(self):
        self.rows: tuple[dict, ...] = ()
        self.state = attendance_sync.SyncState(None, None, None, None, 0, START)
        self.failures = []

    @contextmanager
    def logical_run(self):
        yield self

    def sync_state(self):
        return self.state

    def record_incremental_started(self, _started_at):
        return None

    def store_incremental_cycle(
        self,
        rows,
        *,
        cursor_write_date,
        cursor_id,
        completed_at,
        observed_at,
    ):
        affected = set()
        for row in (*self.rows, *rows):
            affected.update(
                attendance_mirror.local_days_touched(
                    row["check_in_utc"], row["check_out_utc"] or completed_at
                )
            )
        self.rows = tuple(dict(row) for row in rows)
        self.state = replace(
            self.state,
            cursor_write_date=cursor_write_date,
            cursor_id=cursor_id,
            last_incremental_completed_at=completed_at,
        )
        return affected

    def record_failure(self, owner, error):
        self.failures.append((owner, error))


def test_stateful_source_round_trip_correction_recalc_and_resolution(monkeypatch):
    """The release journey crosses punch, source, mirror, correction, and cache."""
    source = _JourneyOdoo()
    mirror = _JourneyMirror()
    synced_punches = []
    monkeypatch.setattr(timeclock_sync, "odoo_client", source)
    monkeypatch.setattr(
        timeclock_sync,
        "_mark_synced",
        lambda log_id, attendance_id: synced_punches.append((log_id, attendance_id)),
    )
    monkeypatch.setattr(attendance_sync, "_source", source)
    monkeypatch.setattr(attendance_sync, "_backend", mirror)
    monkeypatch.setattr(
        attendance_sync,
        "_enqueue_department_repairs_after_sync",
        lambda *_args, **_kwargs: None,
    )

    # 1-2. The real punch retry opens the WC-less Odoo interval; the real sync
    # orchestration mirrors it and projection holds it in first-location grace.
    timeclock_sync._retry_one(
        {
            "id": 1,
            "person_odoo_id": 101,
            "action": "clock_in",
            "wc_name": None,
            "occurred_at": START,
        }
    )
    assert attendance_sync.run_incremental_sync(
        now_utc=START + timedelta(minutes=4)
    ).success
    assert _project(mirror.rows, START + timedelta(minutes=4))[0].status == (
        "pending_first_location"
    )

    # 3-8. Luke labels the first interval and transfers at one exact boundary;
    # an unknown raw destination becomes a real mirrored exception.
    first_id = synced_punches[0][1]
    source.update_attendance_interval(
        first_id,
        values={
            "check_in_utc": START,
            "check_out_utc": START + timedelta(hours=2),
            "odoo_work_center_id": 71,
            "odoo_department_id": 8,
        },
    )
    source._new(101, START + timedelta(hours=2), None, 999)
    attendance_sync.run_incremental_sync(now_utc=START + timedelta(hours=3))
    conflicted = _project(mirror.rows, START + timedelta(hours=3))
    assert any(span.status == "unmapped_location" for span in conflicted)

    # 9. The production correction planner, preflight, ordered Odoo writes,
    # verification reread, and next mirror cycle replace the overlap exactly.
    correction_start = START + timedelta(hours=2)
    correction_end = START + timedelta(hours=3, minutes=59)
    source_rows = source.fetch_employee_attendance_rows(
        101, correction_start, correction_end
    )
    plan = attendance_corrections.plan_correction(
        rows=source_rows,
        employee_odoo_id=101,
        start_utc=correction_start,
        end_utc=correction_end,
        odoo_work_center_id=72,
        odoo_department_id=8,
    )
    ordered = attendance_corrections._ordered_operations(  # noqa: SLF001
        plan.operations,
        source_rows=source_rows,
    )
    attendance_corrections._preflight_operations(  # noqa: SLF001
        source,
        ordered,
        source_rows,
    )
    completed = [
        attendance_corrections._perform_operation(  # noqa: SLF001
            source,
            operation,
            source_rows,
        )[0]
        for operation in ordered
    ]
    verified = attendance_corrections._expected_with_created_ids(  # noqa: SLF001
        plan,
        completed,
    )
    assert verified
    sync_result = attendance_sync.run_incremental_sync(
        now_utc=START + timedelta(hours=3, minutes=1)
    )
    corrected = _project(mirror.rows, START + timedelta(hours=3, minutes=1))
    assert sync_result.affected_days == frozenset({date(2026, 8, 31)})
    assert all(span.status == "valid" for span in corrected)

    # Recalculation consumes those corrected spans, stores one strict snapshot,
    # and the complete conflict section can now depart from the inbox mirror.
    credit = _credit(
        corrected,
        {"WC B": [(START + timedelta(hours=2, minutes=30), 12.0)]},
    )
    rows = (
        {
            "day": date(2026, 8, 31),
            "emp_id": "101",
            "name": "Worker 101",
            "wc_name": "WC B",
            "units": credit[(101, "Worker 101")]["WC B"]["units"],
            "downtime": 0.0,
            "hours": 1.0,
            "days_worked": 1.0,
            "excluded_minutes": 0.0,
        },
    )
    prepared = precompute.PreparedProductionDay(date(2026, 8, 31), rows, None, None)
    stored = []
    monkeypatch.setattr(precompute, "prepare_day", lambda *_args, **_kwargs: prepared)
    monkeypatch.setattr(
        precompute,
        "store_prepared_day",
        lambda value: stored.extend(value.rows) or len(value.rows),
    )
    assert precompute.precompute_day(date(2026, 8, 31), object())["rows_written"] == 1
    assert stored[0]["units"] == 12.0
    conflict_key = "attendance_unmapped_location:101:journey"
    actions = inbox_reconcile.plan_reconcile(
        {},
        {conflict_key: {"item_kind": "attendance_unmapped_location"}},
        {"attendance_unmapped_location"},
    )
    assert actions["departed"] == [conflict_key]

    # 10-11. The real day-boundary clock-out closes every open interval; a
    # historical source edit then requeues every local day it touches.
    timeclock_sync._retry_one(
        {
            "id": 2,
            "person_odoo_id": 101,
            "action": "clock_out",
            "wc_name": None,
            "occurred_at": START + timedelta(hours=4),
            "close_all_open_rows": True,
        }
    )
    attendance_sync.run_incremental_sync(now_utc=START + timedelta(hours=4, minutes=1))
    assert max(span.end_utc for span in _project(mirror.rows, START + timedelta(hours=5))) == (
        START + timedelta(hours=4)
    )
    historical = min(source.rows)
    source.rows[historical]["check_in_utc"] = datetime(2026, 8, 30, 23, 30, tzinfo=UTC)
    source.rows[historical]["odoo_write_date"] = source._tick()
    historical_sync = attendance_sync.run_incremental_sync(
        now_utc=START + timedelta(hours=5)
    )
    assert historical_sync.affected_days == frozenset(
        {date(2026, 8, 30), date(2026, 8, 31)}
    )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_durable_job_recalc_cache_and_inbox_lifecycle(monkeypatch):
    """The manager correction crosses every durable queue and audit boundary."""
    db.init_pool()
    lifecycle_start = datetime(2098, 1, 5, 13, tzinfo=UTC)
    day = lifecycle_start.astimezone(attendance_mirror.SITE_TZ).date()
    prior_day = day - timedelta(days=1)
    employee_id = 990101
    attendance_id = 990001
    item_key = (
        f"attendance_unmapped_location:{employee_id}:{attendance_id}:"
        f"{lifecycle_start.isoformat()}"
    )
    original_rollout = app_settings.get_setting("odoo_attendance_location")
    original_sync = db.query(
        "SELECT baseline_completed_at, last_incremental_completed_at, "
        "last_incremental_observed_at FROM odoo_attendance_sync_state "
        "WHERE singleton = TRUE"
    )[0]
    source = _JourneyOdoo()
    source.next_id = attendance_id
    source._new(
        employee_id,
        lifecycle_start,
        lifecycle_start + timedelta(hours=4),
        999,
    )
    source_row = dict(source.rows[attendance_id])
    plan = attendance_corrections.plan_correction(
        rows=[source_row],
        employee_odoo_id=employee_id,
        start_utc=lifecycle_start,
        end_utc=lifecycle_start + timedelta(hours=4),
        odoo_work_center_id=72,
        odoo_department_id=8,
    )
    preview = attendance_corrections.CorrectionPreview(
        item_key=item_key,
        employee_odoo_ids=(employee_id,),
        target_work_center_name="WC B",
        target_odoo_work_center_id=72,
        target_odoo_department_id=8,
        start_utc=lifecycle_start,
        end_utc=lifecycle_start + timedelta(hours=4),
        plans=(plan,),
    )
    prepared = precompute.PreparedProductionDay(
        day,
        (
            {
                "day": day,
                "emp_id": str(employee_id),
                "name": f"Worker {employee_id}",
                "wc_name": "WC B",
                "units": 12.0,
                "downtime": 0.0,
                "hours": 4.0,
                "days_worked": 1.0,
                "excluded_minutes": 0.0,
            },
        ),
        None,
        "legacy",
    )

    app_settings.set_setting(
        "odoo_attendance_location",
        {"mode": "off", "cutover_at": None, "live_gate": None},
    )
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM attendance_correction_job_events WHERE correction_job_id IN "
            "(SELECT id FROM attendance_correction_jobs WHERE item_key = %s)",
            (item_key,),
        )
        cur.execute("DELETE FROM attendance_correction_jobs WHERE item_key = %s", (item_key,))
        # The production worker claims the global oldest queue row.  This
        # disposable-Postgres release proof owns the queue so an unrelated
        # leftover fixture cannot consume its one synchronous lifecycle tick.
        cur.execute("DELETE FROM attendance_recalc_queue")
        cur.execute(
            "DELETE FROM attendance_strict_days WHERE day = ANY(%s)",
            ([prior_day, day],),
        )
        cur.execute(
            "DELETE FROM production_daily WHERE day = ANY(%s) AND emp_id = %s",
            ([prior_day, day], str(employee_id)),
        )
        cur.execute("DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s", (attendance_id,))
        cur.execute("DELETE FROM inbox_open_items WHERE item_key = %s", (item_key,))
        cur.execute("DELETE FROM inbox_events WHERE item_key = %s", (item_key,))
        cur.execute(
            "UPDATE odoo_attendance_sync_state SET baseline_completed_at = %s, "
            "last_incremental_completed_at = %s, last_incremental_observed_at = %s "
            "WHERE singleton = TRUE",
            (lifecycle_start, lifecycle_start, lifecycle_start),
        )
        cur.execute(
            "INSERT INTO inbox_open_items "
            "(item_key, item_kind, person_name, category_label, priority, first_seen, last_seen) "
            "VALUES (%s, %s, NULL, %s, %s, %s, %s)",
            (
                item_key,
                "attendance_unmapped_location",
                "Unknown Odoo Work Center",
                "urgent",
                lifecycle_start,
                lifecycle_start,
            ),
        )

    monkeypatch.setattr(attendance_corrections, "_default_facade", lambda: source)
    monkeypatch.setattr(
        attendance_corrections,
        "_validate_applying_targets",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        attendance_recalc,
        "_precompute_module",
        lambda: type("Precompute", (), {"prepare_day": staticmethod(lambda *_a: prepared)}),
    )
    monkeypatch.setattr(attendance_recalc, "_default_production_client", lambda: object())
    monkeypatch.setattr(attendance_recalc, "_refresh_caches", lambda _day: None)

    try:
        job_id = attendance_corrections.create_job_from_preview(
            preview=preview,
            actor_email="manager@example.com",
            actor_name="Manager",
        )
        result = attendance_corrections.process_job(job_id)

        assert result.status == "complete", result
        assert db.query(
            "SELECT status FROM attendance_correction_jobs WHERE id = %s",
            (job_id,),
        ) == [{"status": "complete"}]
        phases = {
            (row["phase"], row["result"])
            for row in db.query(
                "SELECT phase, result FROM attendance_correction_job_events "
                "WHERE correction_job_id = %s",
                (job_id,),
            )
        }
        assert ("verifying", "verified") in phases
        assert ("recalculation", "complete") in phases
        assert db.query(
            "SELECT completed_at IS NOT NULL AS completed, "
            "cache_ready_at IS NOT NULL AS cache_ready "
            "FROM attendance_recalc_queue WHERE day = %s",
            (day,),
        ) == [{"completed": True, "cache_ready": True}]
        assert db.query(
            "SELECT units FROM production_daily WHERE day = %s AND emp_id = %s",
            (day, str(employee_id)),
        ) == [{"units": 12.0}]

        monkeypatch.setattr(
            exception_inbox,
            "build_snapshot",
            lambda: {
                "attendance_location_mode": "shadow",
                "queue": [],
                "source_errors": [],
                "sections": [
                    {
                        "id": "attendance_unmapped_location",
                        "count": 0,
                        "rows": [],
                        "complete": True,
                    }
                ],
            },
        )
        monkeypatch.setattr(
            plant_day,
            "now",
            lambda: lifecycle_start + timedelta(days=2),
        )
        inbox_reconcile.run_once()
        assert db.query(
            "SELECT item_key FROM inbox_open_items WHERE item_key = %s",
            (item_key,),
        ) == []

        # After the clean Live boundary, a later historical source edit enqueues
        # both touched local days. Only the completed prior day becomes strict;
        # the still-running cutover day keeps its established matcher.
        app_settings.set_setting(
            "odoo_attendance_location",
            {
                "mode": "live",
                "cutover_at": lifecycle_start.isoformat(),
                "live_gate": {
                    "checked_at": lifecycle_start.isoformat(),
                    "report_digest": "durable-e2e-proof",
                    "activated_at": lifecycle_start.isoformat(),
                },
            },
        )
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO attendance_strict_days (day, reason, source_changed_at) "
                "VALUES (%s, %s, %s) ON CONFLICT (day) DO NOTHING",
                (day, "live_cutover", lifecycle_start),
            )
        assert db.query(
            "SELECT day FROM attendance_strict_days WHERE day = ANY(%s) ORDER BY day",
            ([prior_day, day],),
        ) == [{"day": day}]
        strict_mode = {"enabled": False}

        def prepared_for(claim_day, *_args):
            if not strict_mode["enabled"]:
                return prepared
            rows = prepared.rows if claim_day == day else ()
            return precompute.PreparedProductionDay(
                claim_day,
                rows,
                claim_day,
                "strict",
                production_history.strict_local_source_fingerprint(claim_day),
                f"durable-e2e-request:{claim_day.isoformat()}",
            )

        monkeypatch.setattr(
            attendance_recalc,
            "_precompute_module",
            lambda: type(
                "Precompute",
                (),
                {"prepare_day": staticmethod(prepared_for)},
            ),
        )
        strict_mode["enabled"] = True
        source.rows[attendance_id]["check_in_utc"] = lifecycle_start - timedelta(hours=14)
        source.rows[attendance_id]["odoo_write_date"] = source._tick()
        affected = attendance_mirror.upsert_rows(
            [dict(source.rows[attendance_id])],
            sync_completed_at=lifecycle_start + timedelta(hours=6),
        )
        assert affected == {prior_day, day}
        assert db.query(
            "SELECT day FROM attendance_strict_days WHERE day = ANY(%s) ORDER BY day",
            ([prior_day, day],),
        ) == [{"day": prior_day}, {"day": day}]
        results = [
            attendance_recalc.process_next(
                production_client=object(),
                now_utc=lifecycle_start + timedelta(hours=6),
                clock=lambda: lifecycle_start + timedelta(hours=6, minutes=1),
            )
            for _index in range(2)
        ]
        assert {result.day for result in results if result is not None} == {
            prior_day,
            day,
        }
        assert all(result is not None and result.status == "completed" for result in results)
        assert db.query(
            "SELECT day, completed_at IS NOT NULL AS completed, "
            "cache_ready_at IS NOT NULL AS cache_ready "
            "FROM attendance_recalc_queue WHERE day = ANY(%s) ORDER BY day",
            ([prior_day, day],),
        ) == [
            {"day": prior_day, "completed": True, "cache_ready": True},
            {"day": day, "completed": True, "cache_ready": True},
        ]
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
            cur.execute(
                "DELETE FROM attendance_recalc_queue WHERE day = ANY(%s)",
                ([prior_day, day],),
            )
            cur.execute(
                "DELETE FROM attendance_strict_days WHERE day = ANY(%s)",
                ([prior_day, day],),
            )
            cur.execute("DELETE FROM production_daily WHERE day = %s AND emp_id = %s", (day, str(employee_id)))
            cur.execute("DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s", (attendance_id,))
            cur.execute(
                "UPDATE odoo_attendance_sync_state SET baseline_completed_at = %s, "
                "last_incremental_completed_at = %s, "
                "last_incremental_observed_at = %s WHERE singleton = TRUE",
                (
                    original_sync["baseline_completed_at"],
                    original_sync["last_incremental_completed_at"],
                    original_sync["last_incremental_observed_at"],
                ),
            )
            if original_rollout is None:
                cur.execute(
                    "DELETE FROM app_settings WHERE key = %s",
                    ("odoo_attendance_location",),
                )
            else:
                app_settings.set_setting(
                    "odoo_attendance_location",
                    original_rollout,
                    cur=cur,
                )
