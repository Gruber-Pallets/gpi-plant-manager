"""inbox_reconcile: pure diff + complete-kinds guard + run_once + degraded wiring."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from threading import Barrier, Event, Lock
import time

import psycopg2

import pytest

from zira_dashboard import inbox_reconcile


_needs_postgres = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs local Postgres"
)


def test_plan_reconcile_reports_departed_only_for_complete_kinds():
    prev = {
        "missing_wc:1": {"item_kind": "missing_wc"},
        "time_off:9": {"item_kind": "time_off"},
        "late:5:2026-06-26": {"item_kind": "late"},
    }
    open_now = {
        "missing_wc:1": {"item_kind": "missing_wc"},  # still open
        "missed_punch_out:7": {"item_kind": "missed_punch_out"},  # new
    }
    # time_off was NOT fully enumerated this tick (errored or truncated).
    complete = {"missing_wc", "late", "missed_punch_out", "assignment", "plant_schedule"}

    actions = inbox_reconcile.plan_reconcile(open_now, prev, complete)

    assert set(actions["arrivals"]) == {"missed_punch_out:7"}
    assert actions["still_open"] == ["missing_wc:1"]
    assert "late:5:2026-06-26" in actions["departed"]  # left, kind complete
    assert "time_off:9" not in actions["departed"]  # kind not complete -> kept


def test_complete_kinds_skips_errored_and_truncated():
    snapshot = {
        "source_errors": [{"source": "Pending Time Off"}],  # time_off errored
        "sections": [
            {"id": "missing_wc", "count": 1, "rows": [{"x": 1}]},  # complete
            {"id": "time_off", "count": 0, "rows": []},  # errored -> skip
            {"id": "late", "count": 9, "rows": [{"x": 1}, {"x": 2}]},  # truncated -> skip
            {"id": "missed_punch_out", "count": 0, "rows": []},  # complete (empty)
        ],
    }
    complete = inbox_reconcile._complete_kinds(snapshot)
    assert "missing_wc" in complete
    assert "missed_punch_out" in complete
    assert "time_off" not in complete  # source errored
    assert "late" not in complete  # rows(2) < count(9) -> truncated by a cap


def test_open_now_keeps_absence_pto_kind_inside_time_off_section():
    snapshot = {
        "queue": [{
            "section_id": "time_off",
            "item_key": "absence_pto:41",
            "name": "Maria",
            "action": {"type": "absence_pto", "request_id": 41},
        }],
    }

    assert inbox_reconcile._open_now_from_snapshot(snapshot)["absence_pto:41"][
        "item_kind"
    ] == "absence_pto"


def test_complete_kinds_protects_only_failed_source_in_shared_time_off_section():
    snapshot = {
        "source_errors": [{"source": "Past Absence PTO"}],
        "sections": [{"id": "time_off", "count": 1, "rows": [{}]}],
    }

    complete = inbox_reconcile._complete_kinds(snapshot)

    assert "time_off" in complete
    assert "absence_pto" not in complete


def test_healthy_shared_time_off_section_completes_both_kinds():
    snapshot = {
        "source_errors": [],
        "sections": [{"id": "time_off", "count": 2, "rows": [{}, {}]}],
    }

    complete = inbox_reconcile._complete_kinds(snapshot)

    assert "time_off" in complete
    assert "absence_pto" in complete


def test_truncated_shared_time_off_section_completes_neither_kind():
    snapshot = {
        "source_errors": [],
        "sections": [{"id": "time_off", "count": 2, "rows": [{}]}],
    }

    complete = inbox_reconcile._complete_kinds(snapshot)

    assert "time_off" not in complete
    assert "absence_pto" not in complete


def test_complete_kinds_includes_late_despite_snoozed_padding():
    """build_snapshot appends `snoozed` rows to the late queue, but the late
    `count` sums only the three actionable buckets -> len(rows) > count. A
    display cap can only ever HIDE rows (shown < count), so more-rows-than-count
    is never truncation. late must stay fully-enumerated so a legitimate late
    self-clear auto-resolves promptly instead of waiting for a snooze-free tick."""
    snapshot = {
        "source_errors": [],
        "sections": [
            {
                "id": "late",
                "count": 1,  # one actionable item (e.g. scheduled_late)
                "rows": [
                    {"item_key": "late:5:2026-06-26", "priority": "urgent"},  # counted
                    {"item_key": "late:8:2026-06-26", "priority": "muted"},  # snoozed, NOT counted
                ],
            },
        ],
    }
    assert "late" in inbox_reconcile._complete_kinds(snapshot)


def test_complete_kinds_includes_late_when_item_key_repeats_across_buckets():
    """All four late buckets share one item_key per employee, so an employee in
    two buckets yields two rows but a single open-set key. len(rows) still
    exceeds count; late must not be dropped from complete_kinds over it."""
    snapshot = {
        "source_errors": [],
        "sections": [
            {
                "id": "late",
                "count": 1,
                "rows": [
                    {"item_key": "late:5:2026-06-26", "priority": "urgent"},  # scheduled late
                    {"item_key": "late:5:2026-06-26", "priority": "muted"},  # same emp, snoozed
                ],
            },
        ],
    }
    assert "late" in inbox_reconcile._complete_kinds(snapshot)


def test_complete_kinds_includes_healthy_roster_sync_alert():
    snapshot = {
        "source_errors": [],
        "sections": [
            {
                "id": "odoo_roster_sync",
                "count": 0,
                "rows": [],
            },
        ],
    }

    assert "odoo_roster_sync" in inbox_reconcile._complete_kinds(snapshot)


def test_complete_kinds_includes_healthy_auto_lunch_but_skips_source_error():
    live_snapshot = {
        "source_errors": [],
        "sections": [
            {
                "id": "auto_lunch",
                "count": 0,
                "rows": [],
            },
        ],
    }

    assert "auto_lunch" in inbox_reconcile._complete_kinds(live_snapshot)

    live_snapshot["source_errors"] = [{"source": "Auto-Lunch"}]
    assert "auto_lunch" not in inbox_reconcile._complete_kinds(live_snapshot)


def test_cutover_blocker_is_complete_when_attendance_source_is_healthy():
    snapshot = {
        "sections": [
            {
                "id": "attendance_cutover_blocked",
                "count": 0,
                "rows": [],
                "complete": True,
            }
        ],
        "source_errors": [],
    }

    assert "attendance_cutover_blocked" in inbox_reconcile._complete_kinds(snapshot)

    snapshot["source_errors"] = [{"source": "Attendance Timeline"}]
    assert "attendance_cutover_blocked" not in inbox_reconcile._complete_kinds(snapshot)


def test_cutover_blocker_departs_after_explicit_off_snapshot():
    key = "attendance_cutover_blocked:2026-09-01T12:00:00+00:00"
    failed_snapshot = {
        "attendance_location_mode": "shadow",
        "queue": [
            {
                "item_key": key,
                "section_id": "attendance_cutover_blocked",
                "priority": "urgent",
            }
        ],
        "sections": [
            {
                "id": "attendance_cutover_blocked",
                "count": 1,
                "rows": [{"item_key": key}],
                "complete": True,
            }
        ],
        "source_errors": [],
    }
    open_after_failure = inbox_reconcile._open_now_from_snapshot(failed_snapshot)
    assert inbox_reconcile.plan_reconcile(
        open_after_failure,
        {},
        inbox_reconcile._complete_kinds(failed_snapshot),
    )["arrivals"] == [key]

    off_snapshot = {
        "attendance_location_mode": "off",
        "queue": [],
        "sections": [],
        "source_errors": [],
    }
    assert inbox_reconcile.plan_reconcile(
        {},
        open_after_failure,
        inbox_reconcile._complete_kinds(off_snapshot),
    )["departed"] == [key]


def _mirror_row(**over):
    base = {
        "item_key": "missing_wc:1",
        "item_kind": "missing_wc",
        "person_name": "Maria",
        "category_label": "Missing WC",
        "first_seen": datetime(2026, 6, 1, tzinfo=timezone.utc),
        "last_seen": datetime(2026, 6, 1, tzinfo=timezone.utc),  # long ago -> past grace
    }
    base.update(over)
    return base


def _snap_complete_missing_wc():
    # Nothing open now; the missing_wc section is fully enumerated (0 == 0).
    return {
        "queue": [],
        "source_errors": [],
        "sections": [{"id": "missing_wc", "count": 0, "rows": []}],
    }


def _auto_event(row):
    return {
        "item_kind": row["item_kind"],
        "item_key": row["item_key"],
        "person_name": row.get("person_name"),
        "category_label": row.get("category_label"),
        "action": "auto_resolved",
        "outcome": "Auto-resolved",
        "actor_upn": None,
        "actor_name": None,
        "source": "auto",
    }


class _AtomicDepartureCursor:
    def __init__(self, database):
        self.database = database
        self.operations = []
        self._result = None

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.operations.append((normalized, params))
        if normalized.startswith("DELETE FROM inbox_open_items"):
            key, expected_last_seen = params
            current = self.database.mirror.get(key)
            if current is None or current["last_seen"] != expected_last_seen:
                self._result = None
                return
            self.database.mirror.pop(key)
            self._result = {"item_key": key}
            return
        if normalized.startswith("INSERT INTO inbox_events"):
            if self.database.fail_audit:
                raise RuntimeError("audit insert failed")
            self.database.events.append(params)
            self._result = {"id": len(self.database.events)}
            return
        raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self):
        return self._result


class _AtomicDepartureDatabase:
    """Small transactional fake: state changes commit or roll back together."""

    def __init__(self, row, *, fail_audit=False):
        self.mirror = {row["item_key"]: dict(row)}
        self.events = []
        self.fail_audit = fail_audit
        self.cursors = []
        self.commits = 0
        self.rollbacks = 0
        self._lock = Lock()

    @contextmanager
    def cursor(self):
        with self._lock:
            mirror_before = {key: dict(row) for key, row in self.mirror.items()}
            events_before = list(self.events)
            cursor = _AtomicDepartureCursor(self)
            self.cursors.append(cursor)
            try:
                yield cursor
            except Exception:
                self.mirror = mirror_before
                self.events = events_before
                self.rollbacks += 1
                raise
            else:
                self.commits += 1


def test_run_once_logs_auto_resolved_for_silent_departure(monkeypatch):
    from zira_dashboard import exception_inbox, inbox_log

    monkeypatch.setattr(exception_inbox, "build_snapshot", _snap_complete_missing_wc)
    monkeypatch.setattr(inbox_reconcile, "_read_mirror", lambda: {"missing_wc:1": _mirror_row()})
    deleted, logged = [], []
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda k, i: None)

    def delete(key, last_seen, *, auto_event=None):
        deleted.append(key)
        if auto_event is not None:
            logged.append(auto_event)
        return _mirror_row()

    monkeypatch.setattr(inbox_reconcile, "_delete", delete)
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda k, s: False)
    monkeypatch.setattr(
        inbox_log,
        "log_event_safe",
        lambda **kw: pytest.fail("atomic departure path must not use safe logging"),
    )

    inbox_reconcile.run_once()

    assert deleted == ["missing_wc:1"]
    assert len(logged) == 1
    assert logged[0]["action"] == "auto_resolved"
    assert logged[0]["item_key"] == "missing_wc:1"
    assert logged[0]["actor_upn"] is None


def test_concurrent_departure_is_claimed_and_logged_exactly_once(monkeypatch):
    from zira_dashboard import exception_inbox, inbox_log

    row = _mirror_row()
    database = _AtomicDepartureDatabase(row)
    read_barrier = Barrier(2)

    monkeypatch.setattr(exception_inbox, "build_snapshot", _snap_complete_missing_wc)

    def read_mirror():
        read_barrier.wait(timeout=2)
        return {row["item_key"]: dict(row)}

    monkeypatch.setattr(inbox_reconcile, "_read_mirror", read_mirror)
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda key, info: None)
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda key, since: False)
    monkeypatch.setattr(inbox_reconcile.db, "cursor", database.cursor)

    with ThreadPoolExecutor(max_workers=2) as pool:
        passes = [pool.submit(inbox_reconcile.run_once) for _ in range(2)]
        for reconcile_pass in passes:
            reconcile_pass.result(timeout=2)

    assert database.mirror == {}
    assert [event[1] for event in database.events] == ["missing_wc:1"]
    assert [
        [operation[0].split()[0] for operation in cursor.operations] for cursor in database.cursors
    ] == [["DELETE", "INSERT"], ["DELETE"]]


def test_departure_does_not_log_or_delete_after_concurrent_last_seen_refresh(
    monkeypatch,
):
    from zira_dashboard import exception_inbox, inbox_log

    stale_row = _mirror_row()
    refreshed_last_seen = datetime(2026, 6, 2, tzinfo=timezone.utc)
    refreshed_row = {**stale_row, "last_seen": refreshed_last_seen}
    database = _AtomicDepartureDatabase(refreshed_row)

    monkeypatch.setattr(exception_inbox, "build_snapshot", _snap_complete_missing_wc)
    monkeypatch.setattr(
        inbox_reconcile,
        "_read_mirror",
        lambda: {stale_row["item_key"]: stale_row},
    )
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda key, info: None)
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda key, since: False)
    monkeypatch.setattr(inbox_reconcile.db, "cursor", database.cursor)

    inbox_reconcile.run_once()

    assert database.mirror["missing_wc:1"]["last_seen"] == refreshed_last_seen
    assert database.events == []
    assert len(database.cursors) == 1
    delete = database.cursors[0].operations[0]
    assert delete[1] == ("missing_wc:1", stale_row["last_seen"])
    assert len(database.cursors[0].operations) == 1


def test_delete_and_auto_event_use_one_transaction_cursor(monkeypatch):
    row = _mirror_row()
    database = _AtomicDepartureDatabase(row)
    monkeypatch.setattr(inbox_reconcile.db, "cursor", database.cursor)
    token = datetime(2026, 6, 1, tzinfo=timezone.utc)

    claimed = inbox_reconcile._delete(
        "missing_wc:1",
        token,
        auto_event=_auto_event(row),
    )

    assert claimed == {"item_key": "missing_wc:1"}
    assert len(database.cursors) == 1
    cursor = database.cursors[0]
    assert cursor.operations[0][0] == (
        "DELETE FROM inbox_open_items WHERE item_key = %s AND last_seen = %s RETURNING item_key"
    )
    assert cursor.operations[0][1] == ("missing_wc:1", token)
    assert cursor.operations[1][0].startswith("INSERT INTO inbox_events")
    assert cursor.operations[1][1][1] == "missing_wc:1"
    assert cursor.operations[1][1][4] == "auto_resolved"
    assert database.commits == 1


@_needs_postgres
def test_concurrent_unfinished_correction_insert_blocks_departure_deletion():
    from zira_dashboard import db

    item_key = "production_unassigned_run:Task 6 race:2098-08-31T13:00:00+00:00"
    last_seen = datetime(2098, 8, 31, 13, tzinfo=timezone.utc)
    insert_started = Event()
    release_insert = Event()
    db.init_pool()
    db.bootstrap_schema()
    db.execute("DELETE FROM attendance_correction_jobs WHERE item_key = %s", (item_key,))
    db.execute("DELETE FROM inbox_open_items WHERE item_key = %s", (item_key,))
    db.execute(
        "INSERT INTO inbox_open_items "
        "(item_key, item_kind, category_label, priority, first_seen, last_seen) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            item_key,
            "production_unassigned_run",
            "Production Without a Worker",
            "urgent",
            last_seen,
            last_seen,
        ),
    )

    def insert_unfinished_correction():
        connection = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            with connection.cursor() as cur:
                cur.execute(
                    "INSERT INTO attendance_correction_jobs "
                    "(item_key, status, target_work_center_name, "
                    "target_odoo_work_center_id, start_utc, employee_odoo_ids, "
                    "source_snapshot, operations) "
                    "VALUES (%s, 'planned', %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)",
                    (
                        item_key,
                        "Dismantler 1",
                        44,
                        last_seen,
                        "[42]",
                        "{}",
                        "[]",
                    ),
                )
                insert_started.set()
                assert release_insert.wait(timeout=5)
            connection.commit()
        finally:
            connection.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            insertion = pool.submit(insert_unfinished_correction)
            assert insert_started.wait(timeout=5)
            deletion = pool.submit(
                inbox_reconcile._delete,
                item_key,
                last_seen,
                correction_linked=True,
            )

            waiting_on_insert = False
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not deletion.done():
                waiting_on_insert = bool(
                    db.query(
                        "SELECT 1 FROM pg_locks l "
                        "JOIN pg_class c ON c.oid = l.relation "
                        "WHERE c.relname = 'attendance_correction_jobs' "
                        "AND l.mode = 'ShareLock' "
                        "AND NOT l.granted"
                    )
                )
                if waiting_on_insert:
                    break
                time.sleep(0.01)

            release_insert.set()
            insertion.result(timeout=5)
            claimed = deletion.result(timeout=5)

        assert waiting_on_insert is True
        assert claimed is None
        assert db.query(
            "SELECT item_key FROM inbox_open_items WHERE item_key = %s",
            (item_key,),
        ) == [{"item_key": item_key}]
    finally:
        release_insert.set()
        db.execute("DELETE FROM attendance_correction_jobs WHERE item_key = %s", (item_key,))
        db.execute("DELETE FROM inbox_open_items WHERE item_key = %s", (item_key,))


def test_failed_auto_event_rolls_back_claim_and_next_tick_retries(monkeypatch):
    from zira_dashboard import exception_inbox, inbox_log

    row = _mirror_row()
    database = _AtomicDepartureDatabase(row, fail_audit=True)
    monkeypatch.setattr(exception_inbox, "build_snapshot", _snap_complete_missing_wc)
    monkeypatch.setattr(inbox_reconcile, "_read_mirror", lambda: {row["item_key"]: row})
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda key, info: None)
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda key, since: False)
    monkeypatch.setattr(inbox_reconcile.db, "cursor", database.cursor)

    inbox_reconcile.run_once()

    assert database.mirror == {row["item_key"]: row}
    assert database.events == []
    assert database.rollbacks == 1
    assert database.commits == 0

    database.fail_audit = False
    inbox_reconcile.run_once()

    assert database.mirror == {}
    assert len(database.events) == 1
    assert database.rollbacks == 1
    assert database.commits == 1


def test_run_once_auto_resolves_live_auto_lunch_after_grace(monkeypatch):
    from zira_dashboard import exception_inbox, inbox_log

    snapshot = {
        "queue": [],
        "source_errors": [],
        "sections": [{"id": "auto_lunch", "count": 0, "rows": []}],
    }
    item_key = "auto_lunch:setting"
    mirror_row = _mirror_row(
        item_key=item_key,
        item_kind="auto_lunch",
        person_name="Auto-Lunch",
        category_label="Auto-Lunch",
        priority="urgent",
    )
    monkeypatch.setattr(exception_inbox, "build_snapshot", lambda: snapshot)
    monkeypatch.setattr(inbox_reconcile, "_read_mirror", lambda: {item_key: mirror_row})
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda key, info: None)
    deleted, logged = [], []

    def delete(key, last_seen, *, auto_event=None):
        deleted.append(key)
        if auto_event is not None:
            logged.append(auto_event)
        return mirror_row

    monkeypatch.setattr(inbox_reconcile, "_delete", delete)
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda key, since: False)

    inbox_reconcile.run_once()

    assert deleted == [item_key]
    assert len(logged) == 1
    assert logged[0] == {
        "item_kind": "auto_lunch",
        "item_key": item_key,
        "person_name": "Auto-Lunch",
        "category_label": "Auto-Lunch",
        "action": "auto_resolved",
        "outcome": "Auto-resolved",
        "actor_upn": None,
        "actor_name": None,
        "source": "auto",
    }


def test_run_once_keeps_live_auto_lunch_when_source_errored(monkeypatch):
    from zira_dashboard import exception_inbox, inbox_log

    snapshot = {
        "queue": [],
        "source_errors": [{"source": "Auto-Lunch"}],
        "sections": [{"id": "auto_lunch", "count": 0, "rows": []}],
    }
    item_key = "auto_lunch:setting"
    mirror_row = _mirror_row(item_key=item_key, item_kind="auto_lunch")
    monkeypatch.setattr(exception_inbox, "build_snapshot", lambda: snapshot)
    monkeypatch.setattr(inbox_reconcile, "_read_mirror", lambda: {item_key: mirror_row})
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda key, info: None)
    deleted, logged = [], []
    monkeypatch.setattr(
        inbox_reconcile,
        "_delete",
        lambda key, last_seen: deleted.append(key) or mirror_row,
    )
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda key, since: False)
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: logged.append(kw) or 1)

    inbox_reconcile.run_once()

    assert deleted == []
    assert logged == []


def test_run_once_skips_auto_when_human_resolved(monkeypatch):
    from zira_dashboard import exception_inbox, inbox_log

    row = _mirror_row()
    database = _AtomicDepartureDatabase(row)
    monkeypatch.setattr(exception_inbox, "build_snapshot", _snap_complete_missing_wc)
    monkeypatch.setattr(inbox_reconcile, "_read_mirror", lambda: {"missing_wc:1": row})
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda k, i: None)
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda k, s: True)
    monkeypatch.setattr(inbox_reconcile.db, "cursor", database.cursor)

    inbox_reconcile.run_once()

    assert database.mirror == {}
    assert database.events == []
    assert len(database.cursors) == 1
    assert [sql.split()[0] for sql, _params in database.cursors[0].operations] == ["DELETE"]


def test_run_once_respects_grace_period(monkeypatch):
    from zira_dashboard import exception_inbox, inbox_log, plant_day

    monkeypatch.setattr(exception_inbox, "build_snapshot", _snap_complete_missing_wc)
    # last_seen is "just now" -> within the grace window -> must be left for next tick.
    monkeypatch.setattr(
        inbox_reconcile,
        "_read_mirror",
        lambda: {"missing_wc:1": _mirror_row(last_seen=plant_day.now())},
    )
    deleted, logged = [], []
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda k, i: None)

    def delete(key, last_seen, *, auto_event=None):
        deleted.append(key)
        if auto_event is not None:
            logged.append(auto_event)
        return _mirror_row()

    monkeypatch.setattr(inbox_reconcile, "_delete", delete)
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda k, s: False)

    inbox_reconcile.run_once()

    assert deleted == []  # too recent -> not auto-resolved this tick
    assert logged == []


def test_build_snapshot_flags_degraded_source_into_source_errors(monkeypatch):
    """The Critical guard: a late/assignments payload that swallowed its error
    (degraded=True) must surface in source_errors so the reconciler skips it."""
    from zira_dashboard import exception_inbox, missing_wc, missed_punch_out
    from zira_dashboard.routes import staffing as staffing_routes

    monkeypatch.setattr(
        staffing_routes,
        "assignments_todo_payload",
        lambda: {"degraded": True, "count": 0, "items": [], "people": []},
    )
    monkeypatch.setattr(
        staffing_routes,
        "late_report_payload",
        lambda: {"count": 0, "scheduled_late": [], "unscheduled_late": [], "snoozed": []},
    )
    monkeypatch.setattr(missing_wc, "current_rows", lambda: [])
    monkeypatch.setattr(missed_punch_out, "current_rows", lambda: [])
    monkeypatch.setattr(exception_inbox, "_pending_time_off", lambda today: (0, []))
    monkeypatch.setattr(exception_inbox, "_pending_absence_pto", lambda: (0, []))
    monkeypatch.setattr(exception_inbox, "_plant_schedule_reminder", lambda: (0, []))
    monkeypatch.setattr(exception_inbox, "_work_center_names", lambda: [])

    snap = exception_inbox.build_snapshot()
    sources = {e["source"] for e in snap["source_errors"]}
    assert "Assignments To Do" in sources  # degraded -> flagged as a source error
    assert "Late / Absence" not in sources  # healthy -> not flagged


def test_reconcile_tick_is_registered():
    from zira_dashboard import app

    names = [w[0] for w in app._WARMERS]
    assert "Inbox reconcile" in names
    entry = next(w for w in app._WARMERS if w[0] == "Inbox reconcile")
    assert entry[2] == 60  # seconds


def test_run_once_auto_resolves_departed_breakdown_row(monkeypatch):
    from zira_dashboard import exception_inbox, inbox_log

    def _snap():
        return {
            "queue": [],
            "source_errors": [],
            "sections": [{"id": "breakdown", "count": 0, "rows": []}],
        }

    monkeypatch.setattr(exception_inbox, "build_snapshot", _snap)
    monkeypatch.setattr(
        inbox_reconcile,
        "_read_mirror",
        lambda: {
            "breakdown:Dismantler 2:x": _mirror_row(
                item_key="breakdown:Dismantler 2:x",
                item_kind="breakdown",
                category_label="Machine Breakdown",
            ),
        },
    )
    deleted, logged = [], []
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda k, i: None)

    def delete(key, last_seen, *, auto_event=None):
        deleted.append(key)
        if auto_event is not None:
            logged.append(auto_event)
        return _mirror_row()

    monkeypatch.setattr(inbox_reconcile, "_delete", delete)
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda k, s: False)

    inbox_reconcile.run_once()

    assert deleted == ["breakdown:Dismantler 2:x"]
    assert logged[0]["item_kind"] == "breakdown"
    assert logged[0]["action"] == "auto_resolved"
