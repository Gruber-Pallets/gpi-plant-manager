from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from zira_dashboard import (
    assignment_windows,
    attendance,
    attendance_corrections,
    attendance_location_policy,
    attendance_mirror,
    attendance_readiness,
    attendance_sync,
    attendance_timeline,
    db,
    inbox_reconcile,
    precompute,
    production_history,
    shift_config,
    timeclock_sync,
    wc_attributions,
)


DAY = date(2026, 8, 31)
T0 = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
T2 = T0 + timedelta(minutes=30)
T3 = T0 + timedelta(minutes=60)


def _row(row_id, employee_id, name, start, end, wc_id, wc_name):
    return {
        "odoo_attendance_id": row_id,
        "employee_odoo_id": employee_id,
        "employee_name": name,
        "check_in_utc": start,
        "check_out_utc": end,
        "odoo_work_center_id": wc_id,
        "odoo_work_center_name": wc_name,
        "odoo_department_id": 7,
        "odoo_department_name": "Production",
        "odoo_write_date": T3,
    }


def _project(rows):
    return attendance_timeline.project_rows(
        rows,
        as_of_utc=T3,
        verified_through_utc=T3,
        map_work_center=lambda wc_id: {11: "WC A", 22: "WC B"}.get(wc_id),
        requires_work_center=lambda _department: True,
        expected_department_id=lambda _wc: 7,
    )


class _StatefulOdoo:
    """One mutable fake for Timeclock, Luke, sync, and correction I/O."""

    WC_NAMES = {11: "Luke A", 22: "Luke B", 999: "Luke's exact raw name"}

    def __init__(self):
        self.rows = {}
        self.next_id = 100
        self.write_clock = T0
        self.timeout = False
        self.fail_verification = False
        self.sweep_complete = True
        self.mutations = []

    def _written(self):
        self.write_clock += timedelta(microseconds=1)
        return self.write_clock

    def _new(self, employee_id, name, start, end, wc_id):
        self.next_id += 1
        row = _row(
            self.next_id,
            employee_id,
            name,
            start,
            end,
            wc_id,
            self.WC_NAMES.get(wc_id),
        )
        row["odoo_write_date"] = self._written()
        self.rows[self.next_id] = row
        self.mutations.append(("create", self.next_id))
        return self.next_id

    def get_current_attendance(self, employee_id):
        open_rows = [
            row
            for row in self.rows.values()
            if row["employee_odoo_id"] == employee_id and row["check_out_utc"] is None
        ]
        if not open_rows:
            return None
        return dict(max(open_rows, key=lambda row: row["check_in_utc"]))

    def clock_in(self, employee_id, wc_name, at, *, odoo_department_id=None):
        wc_id = next((key for key, value in self.WC_NAMES.items() if value == wc_name), None)
        name = {101: "Adrian", 202: "Blair"}.get(employee_id, f"Worker {employee_id}")
        attendance_id = self._new(employee_id, name, at, None, wc_id)
        if odoo_department_id is not None:
            self.rows[attendance_id]["odoo_department_id"] = odoo_department_id
        return attendance_id

    def close_all_open_attendance_rows(self, employee_id, at):
        closed = []
        for row in sorted(self.rows.values(), key=lambda value: value["odoo_attendance_id"]):
            if row["employee_odoo_id"] != employee_id or row["check_out_utc"] is not None:
                continue
            row["check_out_utc"] = at
            row["odoo_write_date"] = self._written()
            closed.append(row["odoo_attendance_id"])
            self.mutations.append(("close", row["odoo_attendance_id"]))
        return tuple(closed)

    def luke_transfer(self, employee_id, name, at, wc_id):
        self.close_all_open_attendance_rows(employee_id, at)
        return self._new(employee_id, name, at, None, wc_id)

    def fetch_attendance_changes(self, *, after_write_date, after_id):
        if self.timeout:
            raise TimeoutError("fake Odoo timed out")
        boundary = (
            after_write_date or datetime.min.replace(tzinfo=UTC),
            after_id or 0,
        )
        return [
            dict(row)
            for row in sorted(
                self.rows.values(),
                key=lambda value: (value["odoo_write_date"], value["odoo_attendance_id"]),
            )
            if (row["odoo_write_date"], row["odoo_attendance_id"]) > boundary
        ]

    def fetch_open_attendance_rows(self):
        if self.timeout:
            raise TimeoutError("fake Odoo timed out")
        return [dict(row) for row in self.rows.values() if row["check_out_utc"] is None]

    def fetch_complete_attendance_id_sweep(self):
        return attendance_sync.AttendanceIdSweepSnapshot(
            tuple(sorted(self.rows)), complete=self.sweep_complete
        )

    def fetch_attendance_rows_by_ids(self, ids):
        if self.timeout:
            raise TimeoutError("fake Odoo timed out")
        return [dict(self.rows[row_id]) for row_id in ids if row_id in self.rows]

    def fetch_employee_attendance_rows(self, employee_id, start, end):
        if self.timeout:
            raise TimeoutError("fake Odoo timed out")
        if self.fail_verification:
            raise TimeoutError("fake verification reread timed out")
        infinity = datetime.max.replace(tzinfo=UTC)
        return [
            dict(row)
            for row in self.rows.values()
            if row["employee_odoo_id"] == employee_id
            and row["check_in_utc"] < (end or infinity)
            and (row["check_out_utc"] or infinity) > start
        ]

    def update_attendance_interval(self, attendance_id, *, values):
        row = self.rows[attendance_id]
        row.update(values)
        if "odoo_work_center_id" in values:
            row["odoo_work_center_name"] = self.WC_NAMES.get(values["odoo_work_center_id"])
        row["odoo_write_date"] = self._written()
        self.mutations.append(("update", attendance_id, dict(values)))

    def create_attendance_interval(self, **values):
        return self._new(
            values["employee_odoo_id"],
            {101: "Adrian", 202: "Blair"}.get(values["employee_odoo_id"]),
            values["check_in_utc"],
            values["check_out_utc"],
            values["odoo_work_center_id"],
        )


class _StatefulMirror:
    """Transactional state boundary used by the public sync orchestration."""

    def __init__(self):
        self.rows = {}
        self.cursor_write_date = None
        self.cursor_id = None
        self.last_incremental = None
        self.last_sweep = T0
        self.baseline = T0
        self.generation = 1
        self.failures = []
        self.recalc_days = set()

    @contextmanager
    def logical_run(self):
        before = (
            {key: dict(value) for key, value in self.rows.items()},
            self.cursor_write_date,
            self.cursor_id,
            self.last_incremental,
            self.last_sweep,
            set(self.recalc_days),
        )
        try:
            yield self
        except Exception:
            (
                self.rows,
                self.cursor_write_date,
                self.cursor_id,
                self.last_incremental,
                self.last_sweep,
                self.recalc_days,
            ) = before
            raise

    def sync_state(self):
        return attendance_sync.SyncState(
            cursor_write_date=self.cursor_write_date,
            cursor_id=self.cursor_id,
            last_incremental_completed_at=self.last_incremental,
            last_full_sweep_completed_at=self.last_sweep,
            full_sweep_generation=self.generation,
            baseline_completed_at=self.baseline,
        )

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
        del observed_at
        affected = set()
        for row in rows:
            previous = self.rows.get(row["odoo_attendance_id"])
            if previous != row:
                if previous is not None:
                    affected.update(attendance_mirror._row_days(previous, completed_at))
                affected.update(attendance_mirror._row_days(row, completed_at))
            self.rows[row["odoo_attendance_id"]] = dict(row)
        if cursor_write_date is not None:
            self.cursor_write_date = cursor_write_date
            self.cursor_id = cursor_id
        self.last_incremental = completed_at
        self.recalc_days.update(affected)
        return affected

    def active_attendance_ids(self):
        return set(self.rows)

    def tombstoned_attendance_ids(self, _ids):
        return set()

    def store_full_sweep(
        self,
        ids,
        *,
        recovery_rows,
        generation,
        completed_at,
        observed_at,
    ):
        del recovery_rows, observed_at
        removed = set(self.rows) - set(ids)
        for row_id in removed:
            self.rows.pop(row_id)
        self.generation = generation
        self.last_sweep = completed_at
        return attendance_sync.SweepStoreResult(frozenset(), len(removed))

    def record_failure(self, owner, error):
        self.failures.append((owner, str(error)))

    def complete_baseline_if_ready(self, _completed_at):
        return True


class _StatefulMeter:
    def __init__(self):
        self.samples = {}
        self.source_totals = {}

    def set_samples(self, day, wc_name, samples):
        self.samples[(day, wc_name)] = tuple(samples)

    def set_source_total(self, day, wc_name, units):
        self.source_totals[(day, wc_name)] = units

    def leaderboard(self, day, _now_utc=None):
        results = []
        for (sample_day, wc_name), samples in sorted(self.samples.items()):
            if sample_day != day:
                continue
            active = ()
            if samples:
                active = ((samples[0][0], samples[-1][0] + timedelta(hours=1)),)
            results.append(
                SimpleNamespace(
                    station=SimpleNamespace(name=wc_name),
                    units=self.source_totals.get(
                        (sample_day, wc_name), sum(units for _at, units in samples)
                    ),
                    reading_count=len(samples),
                    truncated=False,
                    downtime_minutes=0,
                    active_minutes=60,
                    last_reading_at=samples[-1][0] if samples else None,
                    last_status="Working",
                    samples=samples,
                    active_intervals=active,
                )
            )
        return results


def test_clock_in_luke_locations_transfers_and_shared_production_form_one_truth():
    rows = [
        _row(1, 101, "Adrian", T0, T1, None, None),
        _row(2, 101, "Adrian", T1, T2, 11, "Luke A"),
        _row(3, 101, "Adrian", T2, T3, 22, "Luke B"),
        _row(4, 202, "Blair", T2, T3, 22, "Luke B"),
    ]

    spans = _project(rows)
    assert [(span.status, span.app_work_center_name) for span in spans] == [
        ("pending_first_location", None),
        ("valid", "WC A"),
        ("valid", "WC B"),
        ("valid", "WC B"),
    ]
    segments = assignment_windows.work_segments_from_timeline(
        spans, window_start_utc=T0, window_end_utc=T3
    )
    attribution = production_history.attribute_for_segments(
        segments,
        wc_totals={"WC A": (10, 0), "WC B": (20, 0)},
        samples_by_wc={
            "WC A": [(T1 + timedelta(minutes=1), 10)],
            "WC B": [(T2 + timedelta(minutes=1), 20)],
        },
        productive_minutes=lambda _person, _wc, start, end: (end - start).total_seconds() / 60,
        strict=True,
    )

    assert attribution[(101, "Adrian")]["WC A"]["units"] == 10
    assert attribution[(101, "Adrian")]["WC B"]["units"] == 10
    assert attribution[(202, "Blair")]["WC B"]["units"] == 10
    assert sum(wc["units"] for person in attribution.values() for wc in person.values()) == 30


def test_conflicting_and_unknown_odoo_locations_never_fabricate_credit():
    spans = _project(
        [
            _row(10, 101, "Adrian", T0, T2, 11, "Luke A"),
            _row(11, 101, "Adrian", T1, T3, 22, "Luke B"),
            _row(12, 202, "Blair", T0, T3, 999, "Luke's exact raw name"),
        ]
    )

    assert any(span.status == "conflicting_location" for span in spans)
    unknown = next(span for span in spans if span.status == "unmapped_location")
    assert unknown.odoo_work_center_name == "Luke's exact raw name"
    segments = assignment_windows.work_segments_from_timeline(
        spans, window_start_utc=T0, window_end_utc=T3
    )
    assert not [segment for segment in segments if segment.person_odoo_id == 202]
    assert not [
        segment
        for segment in segments
        if segment.person_odoo_id == 101 and segment.start_utc < T2 and segment.end_utc > T1
    ]


def test_closed_final_attendance_has_no_location_past_clock_out():
    clock_out = T2 + timedelta(minutes=10)
    spans = _project([_row(30, 101, "Adrian", T1, clock_out, 11, "Luke A")])

    assert spans[-1].end_utc == clock_out
    assert all(span.end_utc <= clock_out for span in spans)


class _CorrectionQueueCursor:
    def __init__(self):
        self.result = None
        self.operations = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.operations.append((normalized, params))
        if normalized.startswith("SELECT status, attempt_count"):
            self.result = {"status": "recalculating", "attempt_count": 1}
        elif normalized.startswith("UPDATE attendance_correction_jobs"):
            self.result = {"id": 9}
        elif normalized.startswith("SELECT status FROM attendance_correction_jobs"):
            self.result = {"status": "complete"}
        else:
            self.result = None

    def fetchone(self):
        return self.result


@contextmanager
def _cursor(value):
    yield value


def test_manager_split_verifies_enqueues_recalc_and_then_allows_only_its_issue_to_resolve(
    monkeypatch,
):
    source = _row(70, 101, "Adrian", T0, T3, 11, "Luke A")
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=101,
        start_utc=T1,
        end_utc=T2,
        odoo_work_center_id=22,
        odoo_department_id=7,
    )
    completed = []
    next_created_id = 700
    for operation in plan.operations:
        record = {
            "operation_key": operation.key,
            "kind": operation.kind,
            "attendance_id": operation.attendance_id,
        }
        if operation.kind == "create":
            next_created_id += 1
            record["attendance_id"] = next_created_id
        completed.append(record)
    verified_source = [
        {
            **row,
            "employee_name": "Adrian",
            "odoo_work_center_name": "Luke B" if row["odoo_work_center_id"] == 22 else "Luke A",
            "odoo_department_name": "Production",
            "odoo_write_date": T3,
        }
        for row in attendance_corrections._expected_with_created_ids(plan, completed)
    ]

    class Facade:
        def fetch_employee_attendance_rows(self, *_args):
            return verified_source

    verified = attendance_corrections._verification_rows(Facade(), {101: plan}, completed, T1, T2)
    assert [row["odoo_work_center_id"] for row in verified] == [11, 22, 11]

    cursor = _CorrectionQueueCursor()
    claim = SimpleNamespace(
        job_id=9,
        attempt_count=1,
        lease_until=T3 + timedelta(minutes=15),
        row={"completed_operations": []},
    )
    enqueued = []
    monkeypatch.setattr(db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_mirror,
        "_enqueue_recalc_cur",
        lambda cur, days, reason, *, requested_at: enqueued.append(
            (cur, tuple(days), reason, requested_at)
        ),
    )
    touched_days = attendance_corrections._touched_days({101: [source]}, {101: plan}, open_end=T3)

    assert attendance_corrections._enqueue_recalculation(claim, touched_days, (), requested_at=T3)
    assert enqueued == [(cursor, (DAY,), "attendance_correction_verified", T3)]
    monkeypatch.setattr(
        db,
        "query",
        lambda *_a, **_k: [{"day": DAY, "completed_at": T3, "cache_ready_at": T3}],
    )
    assert attendance_corrections._recalc_complete(touched_days)
    assert inbox_reconcile._correction_allows_resolution_cur(
        cursor, "production_unassigned_run:wc-b:1"
    )

    corrected_key = "production_unassigned_run:wc-b:1"
    unrelated_key = "attendance_unmapped_location:202"
    previous = {
        corrected_key: {"item_kind": "production_unassigned_run"},
        unrelated_key: {"item_kind": "attendance_unmapped_location"},
    }
    current = {unrelated_key: previous[unrelated_key]}
    actions = inbox_reconcile.plan_reconcile(
        current,
        previous,
        {"production_unassigned_run", "attendance_unmapped_location"},
    )
    assert actions["departed"] == [corrected_key]
    assert actions["still_open"] == [unrelated_key]


def test_post_baseline_historical_edit_queues_and_atomically_replaces_a_strict_day(
    monkeypatch,
):
    historical_start = datetime(2026, 8, 29, 13, 0, tzinfo=UTC)
    historical_end = datetime(2026, 8, 29, 14, 0, tzinfo=UTC)
    historical_day = date(2026, 8, 29)
    changed_row = _row(
        80,
        101,
        "Adrian",
        historical_start,
        historical_end,
        22,
        "Luke B",
    )
    days = attendance_mirror._row_days(changed_row, T3)
    queue_cursor = _CorrectionQueueCursor()

    attendance_mirror._enqueue_recalc_cur(
        queue_cursor,
        days,
        "attendance_source_changed",
        requested_at=T3,
    )
    assert days == {historical_day}
    assert any(
        "INSERT INTO attendance_recalc_queue" in operation
        for operation, _params in queue_cursor.operations
    )

    writes = []

    class ProductionCursor:
        def execute(self, sql, params=None):
            writes.append((" ".join(sql.split()), params))

    monkeypatch.setattr(
        db,
        "execute_values",
        lambda cur, sql, values, template=None: writes.append(
            ("execute_values", tuple(values), template)
        ),
    )
    row = {
        "day": historical_day,
        "emp_id": "101",
        "name": "Adrian",
        "wc_name": "WC B",
        "units": 10,
        "downtime": 0,
        "hours": 2,
        "days_worked": 0.25,
        "excluded_minutes": 0,
    }
    precompute._upsert_production_daily_cur(
        ProductionCursor(),
        [row],
        replace_days=(historical_day,),
        strict_day=historical_day,
    )

    assert "INSERT INTO attendance_strict_days" in writes[0][0]
    assert "DELETE FROM production_daily" in writes[1][0]
    assert writes[2][0] == "execute_values"


def test_public_orchestration_keeps_one_stateful_odoo_and_meter_truth(monkeypatch):
    """Exercise the real sync, strict matcher, precompute, and correction worker."""
    odoo = _StatefulOdoo()
    mirror = _StatefulMirror()
    meter = _StatefulMeter()
    saved = {DAY: ({"marker": "last-good"},)}
    queued = []
    synced_punches = []

    monkeypatch.setattr(attendance_sync, "_source", odoo)
    monkeypatch.setattr(attendance_sync, "_backend", mirror)
    monkeypatch.setattr(
        attendance_sync,
        "_enqueue_department_repairs_after_sync",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "match_state_for_day",
        lambda _day, *, now_utc=None: "strict",
    )
    monkeypatch.setattr(
        attendance_mirror,
        "health_snapshot",
        lambda: attendance_mirror.MirrorHealth(
            last_incremental_completed_at=mirror.last_incremental,
            last_full_sweep_completed_at=mirror.last_sweep,
            baseline_completed_at=mirror.baseline,
            oldest_recalc_requested_at=None,
            last_error=None,
        ),
    )

    def timeline(start, end, *, as_of_utc=None):
        as_of = as_of_utc or end
        rows = [
            dict(row)
            for row in mirror.rows.values()
            if row["check_in_utc"] < end
            and (row["check_out_utc"] is None or row["check_out_utc"] > start)
        ]
        return attendance_timeline.project_rows(
            rows,
            as_of_utc=as_of,
            verified_through_utc=mirror.last_incremental or as_of,
            map_work_center=lambda wc_id: {11: "WC A", 22: "WC B"}.get(wc_id),
            requires_work_center=lambda _department: True,
            expected_department_id=lambda _wc: 7,
        )

    monkeypatch.setattr(attendance_timeline, "timeline_for_range", timeline)
    monkeypatch.setattr(
        production_history,
        "_metered_leaderboard",
        lambda client, day, **kwargs: client.leaderboard(day, kwargs.get("now_utc")),
    )
    monkeypatch.setattr(shift_config, "shift_start_for", lambda _day: time(7))
    monkeypatch.setattr(shift_config, "shift_end_for", lambda _day: time(15))
    monkeypatch.setattr(shift_config, "breaks_for", lambda _day: ())
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(wc_attributions, "testing_windows_for_day", lambda _day: {})
    monkeypatch.setattr(wc_attributions, "breakdown_windows_for_day", lambda _day: {})
    monkeypatch.setattr(attendance, "name_to_person_id", lambda: {})
    monkeypatch.setattr(attendance_readiness, "ordinary_refresh_ready", lambda _day: True)
    monkeypatch.setattr(
        attendance_mirror,
        "enqueue_recalc",
        lambda days, reason: queued.append((tuple(days), reason)),
    )

    def store(prepared, *, cur=None):
        del cur
        saved[prepared.day] = tuple(dict(row) for row in prepared.rows)
        return len(prepared.rows)

    monkeypatch.setattr(precompute, "store_prepared_day", store)

    # Plant Manager opens the day only; the public punch worker sends no WC.
    punch = {
        "id": 1,
        "person_odoo_id": 101,
        "action": "clock_in",
        "wc_name": None,
        "close_all_open_rows": False,
        "occurred_at": T0,
    }
    with monkeypatch.context() as local:
        local.setattr(timeclock_sync.db, "query", lambda *_a, **_k: [punch])
        local.setattr(
            timeclock_sync.db,
            "execute",
            lambda _sql, params=None: synced_punches.append(params),
        )
        local.setattr(
            timeclock_sync.odoo_client, "get_current_attendance", odoo.get_current_attendance
        )
        local.setattr(timeclock_sync.odoo_client, "clock_in", odoo.clock_in)
        timeclock_sync.sync_one_by_id(1)

    assert odoo.get_current_attendance(101)["odoo_work_center_id"] is None
    assert synced_punches[-1][0] == 101
    assert attendance_sync.run_incremental_sync(now_utc=T0 + timedelta(minutes=4)).success
    first_spans = timeline(T0, T1, as_of_utc=T0 + timedelta(minutes=4))
    assert [(span.status, span.app_work_center_name) for span in first_spans] == [
        ("pending_first_location", None)
    ]

    meter.set_samples(DAY, "WC A", ((T0 + timedelta(minutes=3), 4),))
    pending_runs = production_history.unassigned_runs_for_day(
        DAY, meter, now_utc=T0 + timedelta(minutes=4)
    )
    assert [(run.wc_name, run.units) for run in pending_runs] == [("WC A", 4.0)]
    pending_precompute = precompute.precompute_day(DAY, meter)
    assert pending_precompute == {"day": DAY.isoformat(), "rows_written": 0}
    assert saved[DAY] == ()

    # Luke supplies A, later transfers Adrian to B, and puts Blair at B.
    first_row_id = odoo.get_current_attendance(101)["odoo_attendance_id"]
    odoo.luke_transfer(101, "Adrian", T1, 11)
    assert attendance_sync.run_incremental_sync(now_utc=T1 + timedelta(seconds=1)).success
    meter.set_samples(
        DAY,
        "WC A",
        ((T0 + timedelta(minutes=3), 4), (T1 + timedelta(minutes=1), 10)),
    )
    odoo.luke_transfer(101, "Adrian", T2, 22)
    odoo._new(202, "Blair", T2, None, 22)
    meter.set_samples(DAY, "WC B", ((T2 + timedelta(minutes=1), 20),))

    # Conflicts and unknown raw labels remain exact but never become segments.
    odoo._new(303, "Casey", T1, T2, 11)
    odoo._new(303, "Casey", T1 + timedelta(minutes=1), T2, 22)
    odoo._new(404, "Dana", T1, T2, 999)
    assert attendance_sync.run_incremental_sync(now_utc=T2 + timedelta(minutes=2)).success
    observed = timeline(T0, T3, as_of_utc=T2 + timedelta(minutes=2))
    assert any(span.status == "conflicting_location" for span in observed)
    raw = next(span for span in observed if span.employee_odoo_id == 404)
    assert raw.status == "unmapped_location"
    assert raw.odoo_work_center_name == "Luke's exact raw name"
    visible_segments = assignment_windows.work_segments_from_timeline(
        observed, window_start_utc=T0, window_end_utc=T3
    )
    assert not [segment for segment in visible_segments if segment.person_odoo_id == 404]
    assert not [
        segment
        for segment in visible_segments
        if segment.person_odoo_id == 303 and segment.end_utc > T1 + timedelta(minutes=1)
    ]

    # A manager fixes the first unassigned interval.  The public worker owns
    # Odoo mutation, exact reread, mirror sync, strict recompute/cache, and audit.
    source = dict(odoo.rows[first_row_id])
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=101,
        start_utc=T0,
        end_utc=T1,
        odoo_work_center_id=11,
        odoo_department_id=7,
    )
    preview = attendance_corrections.CorrectionPreview(
        item_key="production_unassigned_run:wc-a:early",
        employee_odoo_ids=(101,),
        target_work_center_name="WC A",
        target_odoo_work_center_id=11,
        target_odoo_department_id=7,
        start_utc=T0,
        end_utc=T1,
        plans=(plan,),
    )
    claim = attendance_corrections._JobClaim(
        job_id=9,
        attempt_count=1,
        lease_until=T3 + timedelta(minutes=15),
        row={
            "id": 9,
            "item_key": preview.item_key,
            "status": "applying",
            "target_work_center_name": "WC A",
            "target_odoo_work_center_id": 11,
            "employee_odoo_ids": [101],
            "source_snapshot": attendance_corrections._snapshot_payload(preview),
            "operations": attendance_corrections._plans_payload(preview),
            "completed_operations": [],
            "start_utc": T0,
            "end_utc": T1,
            "actor_email": "manager@example.com",
            "actor_name": "Manager",
        },
    )
    correction_state = {"audit": False, "cache": False, "claimed": False}

    def claim_job(**_kwargs):
        if correction_state["claimed"]:
            return None
        correction_state["claimed"] = True
        return claim

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

    def reserve(local_claim, operation):
        return attendance_corrections._OperationReservation(
            job_id=local_claim.job_id,
            attempt_count=local_claim.attempt_count,
            operation_key=operation.key,
            token="a" * 32,
            reserved_until=local_claim.lease_until,
        )

    def complete_reserved(local_claim, _reservation, record, **kwargs):
        return complete_record(local_claim, record, **kwargs)

    def mirror_verified(local_claim, *_args, **_kwargs):
        result = attendance_sync.run_incremental_sync(now_utc=T3)
        assert result.success
        local_claim.row["completed_operations"].append({"stage": "mirror_complete"})
        return True

    def enqueue_recalc(local_claim, days, *_args, **_kwargs):
        queued.extend((tuple(days), "attendance_correction_verified") for _ in range(1))
        local_claim.row["completed_operations"].append(
            {"stage": "recalc_enqueued", "recalc_ids": [day.isoformat() for day in days]}
        )
        return True

    def run_recalc(days):
        for day in days:
            assert precompute.precompute_day(day, meter)["rows_written"] > 0
        correction_state["cache"] = True
        return True

    monkeypatch.setattr(attendance_corrections, "_claim_job", claim_job)
    monkeypatch.setattr(attendance_corrections, "_default_facade", lambda: odoo)
    monkeypatch.setattr(
        attendance_corrections, "_validate_applying_targets", lambda *_a, **_k: None
    )
    monkeypatch.setattr(attendance_corrections, "_claim_is_current", lambda _claim: True)
    monkeypatch.setattr(attendance_corrections, "_heartbeat_claim", lambda _claim: None)
    monkeypatch.setattr(attendance_corrections, "_transition", transition)
    monkeypatch.setattr(attendance_corrections, "_complete_record", complete_record)
    monkeypatch.setattr(attendance_corrections, "_reserve_operation", reserve)
    monkeypatch.setattr(
        attendance_corrections,
        "_renew_operation_reservation",
        lambda _claim, reservation: reservation,
    )
    monkeypatch.setattr(attendance_corrections, "_complete_reserved_operation", complete_reserved)
    monkeypatch.setattr(attendance_corrections, "_mirror_verified_rows", mirror_verified)
    monkeypatch.setattr(attendance_corrections, "_enqueue_recalculation", enqueue_recalc)
    monkeypatch.setattr(attendance_corrections, "_run_recalculation", run_recalc)
    monkeypatch.setattr(
        attendance_corrections,
        "_complete_with_audit",
        lambda *_a, **_k: correction_state.__setitem__("audit", True) or True,
    )

    correction = attendance_corrections.process_job(9)

    assert correction.status == "complete"
    assert odoo.rows[first_row_id]["odoo_work_center_id"] == 11
    assert correction_state == {"audit": True, "cache": True, "claimed": True}
    by_person_wc = {
        (row["emp_id"], row["wc_name"]): row["units"] for row in saved[DAY] if row["units"] > 0
    }
    assert by_person_wc == {("101", "WC A"): 14.0, ("101", "WC B"): 10.0, ("202", "WC B"): 10.0}
    assert all(row["units"] == 0 for row in saved[DAY] if row["emp_id"] in {"303", "404"})
    departed = inbox_reconcile.plan_reconcile(
        {},
        {preview.item_key: {"item_kind": "production_unassigned_run"}},
        {"production_unassigned_run"},
    )
    assert departed["departed"] == [preview.item_key]

    # Plant Manager closes the workday; the final mirrored interval is bounded.
    for log_id, employee_id in ((2, 101), (3, 202)):
        clock_out = {
            "id": log_id,
            "person_odoo_id": employee_id,
            "action": "clock_out",
            "wc_name": None,
            "close_all_open_rows": True,
            "occurred_at": T3,
        }
        with monkeypatch.context() as local:
            local.setattr(timeclock_sync.db, "query", lambda *_a, row=clock_out, **_k: [row])
            local.setattr(timeclock_sync.db, "execute", lambda *_a, **_k: None)
            local.setattr(
                timeclock_sync.odoo_client,
                "close_all_open_attendance_rows",
                odoo.close_all_open_attendance_rows,
            )
            timeclock_sync.sync_one_by_id(log_id)
    assert attendance_sync.run_incremental_sync(now_utc=T3 + timedelta(seconds=1)).success
    assert all(
        span.end_utc <= T3
        for span in timeline(T0, T3 + timedelta(hours=1), as_of_utc=T3 + timedelta(hours=1))
        if span.employee_odoo_id in {101, 202}
    )

    # A late Odoo edit of an old strict day is discovered and replaces that
    # historical snapshot without reinterpreting the current day.
    historical_day = DAY - timedelta(days=1)
    historical_start = T0 - timedelta(days=1)
    historical_id = odoo._new(
        505,
        "Evan",
        historical_start,
        historical_start + timedelta(hours=1),
        11,
    )
    meter.set_samples(historical_day, "WC A", ((historical_start + timedelta(minutes=5), 5),))
    assert attendance_sync.run_incremental_sync(now_utc=T3 + timedelta(minutes=1)).success
    assert precompute.precompute_day(historical_day, meter)["rows_written"] == 1
    assert saved[historical_day][0]["wc_name"] == "WC A"

    odoo.update_attendance_interval(historical_id, values={"odoo_work_center_id": 22})
    meter.set_samples(historical_day, "WC A", ())
    meter.set_samples(historical_day, "WC B", ((historical_start + timedelta(minutes=5), 5),))
    assert attendance_sync.run_incremental_sync(now_utc=T3 + timedelta(minutes=2)).success
    assert historical_day in mirror.recalc_days
    assert precompute.precompute_day(historical_day, meter)["rows_written"] == 1
    assert saved[historical_day][0]["wc_name"] == "WC B"

    def correction_claim(job_id, source_row, target_wc):
        local_plan = attendance_corrections.plan_correction(
            rows=[source_row],
            employee_odoo_id=source_row["employee_odoo_id"],
            start_utc=source_row["check_in_utc"],
            end_utc=source_row["check_out_utc"],
            odoo_work_center_id=target_wc,
            odoo_department_id=7,
        )
        local_preview = attendance_corrections.CorrectionPreview(
            item_key=f"production_unassigned_run:historical:{job_id}",
            employee_odoo_ids=(source_row["employee_odoo_id"],),
            target_work_center_name={11: "WC A", 22: "WC B"}[target_wc],
            target_odoo_work_center_id=target_wc,
            target_odoo_department_id=7,
            start_utc=source_row["check_in_utc"],
            end_utc=source_row["check_out_utc"],
            plans=(local_plan,),
        )
        return attendance_corrections._JobClaim(
            job_id=job_id,
            attempt_count=1,
            lease_until=T3 + timedelta(minutes=15),
            row={
                "id": job_id,
                "item_key": local_preview.item_key,
                "status": "applying",
                "target_work_center_name": local_preview.target_work_center_name,
                "target_odoo_work_center_id": target_wc,
                "employee_odoo_ids": list(local_preview.employee_odoo_ids),
                "source_snapshot": attendance_corrections._snapshot_payload(local_preview),
                "operations": attendance_corrections._plans_payload(local_preview),
                "completed_operations": [],
                "start_utc": local_preview.start_utc,
                "end_utc": local_preview.end_utc,
                "actor_email": "manager@example.com",
                "actor_name": "Manager",
            },
        )

    # A stale manager preview fails through the public worker before any new
    # Odoo write, leaving both mirror and computed production unchanged.
    stale_claim = correction_claim(10, dict(odoo.rows[historical_id]), 11)
    odoo.update_attendance_interval(historical_id, values={"odoo_work_center_id": 999})
    mutation_count = len(odoo.mutations)
    local_before = {key: dict(value) for key, value in mirror.rows.items()}
    production_before = {key: tuple(dict(row) for row in value) for key, value in saved.items()}
    monkeypatch.setattr(attendance_corrections, "_claim_job", lambda **_kwargs: stale_claim)

    stale_result = attendance_corrections.process_job(10)

    assert stale_result.status == "failed"
    assert len(odoo.mutations) == mutation_count
    assert mirror.rows == local_before
    assert saved == production_before

    # A reread outage after an accepted Odoo update remains recoverable.  The
    # remote write is real, but no unverified row reaches mirror/recalc/cache.
    odoo.update_attendance_interval(historical_id, values={"odoo_work_center_id": 22})
    reread_claim = correction_claim(11, dict(odoo.rows[historical_id]), 11)
    local_before = {key: dict(value) for key, value in mirror.rows.items()}
    production_before = {key: tuple(dict(row) for row in value) for key, value in saved.items()}
    mutation_count = len(odoo.mutations)
    odoo.fail_verification = True
    monkeypatch.setattr(attendance_corrections, "_claim_job", lambda **_kwargs: reread_claim)

    reread_result = attendance_corrections.process_job(11)

    assert reread_result.status == "recoverable"
    assert len(odoo.mutations) == mutation_count + 1
    assert mirror.rows == local_before
    assert saved == production_before
    odoo.fail_verification = False

    # Public source failures keep the same mirror and computed snapshots.
    mirror_before = {key: dict(value) for key, value in mirror.rows.items()}
    saved_before = {key: tuple(dict(row) for row in value) for key, value in saved.items()}
    odoo.timeout = True
    assert attendance_sync.run_incremental_sync(now_utc=T3 + timedelta(minutes=3)).success is False
    assert mirror.rows == mirror_before
    odoo.timeout = False
    odoo.sweep_complete = False
    assert attendance_sync.run_full_sweep(now_utc=T3 + timedelta(minutes=4)).success is False
    assert mirror.rows == mirror_before
    assert saved == saved_before

    meter.set_samples(historical_day, "WC B", ())
    meter.set_source_total(historical_day, "WC B", 5)
    with pytest.raises(
        production_history.ProductionSourceUnavailable,
        match="samples.*do not match",
    ):
        precompute.precompute_day(historical_day, meter)
    assert saved == saved_before
    assert queued[-1] == ((historical_day,), "production_source_unavailable")
