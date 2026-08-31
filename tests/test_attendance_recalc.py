from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
import importlib

import pytest


DAY_1 = date(2026, 8, 18)
DAY_2 = date(2026, 8, 19)
NOW = datetime(2026, 8, 30, 15, tzinfo=UTC)


def recalc():
    try:
        return importlib.import_module("zira_dashboard.attendance_recalc")
    except ModuleNotFoundError:
        pytest.fail("attendance_recalc worker is not implemented")


class QueueCursor:
    def __init__(self, store):
        self.store = store
        self.result = None

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.store.sql.append((normalized, params))
        if normalized.startswith("SELECT day, attempt_count, started_at, completed_at"):
            self.result = self.store.rows.get(params[0])
            if self.result is not None:
                self.result = dict(self.result)
            return
        if normalized.startswith("SELECT day, attempt_count"):
            now = params[0]
            eligible = sorted(
                (
                    row
                    for row in self.store.rows.values()
                    if row["completed_at"] is None
                    and (row["started_at"] is None or row["started_at"] <= now)
                ),
                key=lambda row: (row["requested_at"], row["day"]),
            )
            self.result = dict(eligible[0]) if eligible else None
            return
        if (
            normalized.startswith("UPDATE attendance_recalc_queue SET started_at")
            and "last_error" not in normalized
        ):
            lease_until, attempt_count, day = params
            row = self.store.rows[day]
            row["started_at"] = lease_until
            row["attempt_count"] = attempt_count
            self.result = dict(row)
            return
        if "SET completed_at = %s" in normalized:
            completed_at, day, attempt_count, lease_until = params
            if self.store.fail_completion:
                self.result = None
                return
            row = self.store.rows[day]
            if (
                row["attempt_count"] != attempt_count
                or row["started_at"] != lease_until
                or row["completed_at"] is not None
            ):
                self.result = None
            else:
                row["completed_at"] = completed_at
                row["started_at"] = None
                row["last_error"] = None
                self.result = {"day": day}
            return
        if "SET started_at = %s, last_error = %s" in normalized:
            retry_at, error, day, attempt_count, lease_until = params
            if self.store.fail_failure_recording:
                raise RuntimeError("queue write failed")
            row = self.store.rows[day]
            if (
                row["attempt_count"] != attempt_count
                or row["started_at"] != lease_until
                or row["completed_at"] is not None
            ):
                self.result = None
            else:
                row["started_at"] = retry_at
                row["last_error"] = error
                self.result = {"day": day}
            return
        if normalized.startswith("INSERT INTO attendance_strict_days"):
            self.store.strict_days.add(params[0])
            self.result = None
            return
        if normalized.startswith("DELETE FROM production_daily"):
            self.store.production.pop(params[0], None)
            self.result = None
            return
        raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self):
        return self.result


class QueueStore:
    def __init__(
        self,
        rows,
        *,
        fail_completion=False,
        fail_failure_recording=False,
        strict_days=(),
    ):
        self.rows = {row["day"]: dict(row) for row in rows}
        self.production = {}
        self.strict_days = set(strict_days)
        self.sql = []
        self.events = []
        self.fail_completion = fail_completion
        self.fail_failure_recording = fail_failure_recording

    def execute_values(self, cur, sql, values, template=None):
        del cur, sql, template
        for value in values:
            self.production.setdefault(value[0], []).append(tuple(value))

    def cursor(self):
        store = self

        class Transaction:
            def __enter__(self):
                store.events.append("begin")
                self.before = (
                    deepcopy(store.rows),
                    deepcopy(store.production),
                    set(store.strict_days),
                )
                self.cursor = QueueCursor(store)
                return self.cursor

            def __exit__(self, exc_type, exc, tb):
                if exc_type:
                    store.rows, store.production, store.strict_days = self.before
                store.events.append("rollback" if exc_type else "commit")
                return False

        return Transaction()


def queue_row(
    day,
    *,
    requested_at,
    started_at=None,
    completed_at=None,
    attempt_count=0,
    last_error=None,
):
    return {
        "day": day,
        "reason": "mirror_changed",
        "requested_at": requested_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "attempt_count": attempt_count,
        "last_error": last_error,
    }


def install_queue(monkeypatch, store):
    from zira_dashboard import db

    monkeypatch.setattr(db, "cursor", store.cursor)
    monkeypatch.setattr(db, "execute_values", store.execute_values)


def prepared_snapshot(day, *units, strict=False):
    from zira_dashboard.precompute import PreparedProductionDay

    rows = tuple(
        {
            "day": day,
            "emp_id": str(index),
            "name": f"Worker {index}",
            "wc_name": "Repair 1",
            "units": value,
            "downtime": 0,
            "hours": 1,
            "days_worked": 1,
            "excluded_minutes": 0,
        }
        for index, value in enumerate(units, start=1)
    )
    return PreparedProductionDay(day=day, rows=rows, strict_day=day if strict else None)


def test_claims_oldest_eligible_day_with_skip_locked_and_durable_lease(monkeypatch):
    module = recalc()
    store = QueueStore(
        [
            queue_row(DAY_2, requested_at=NOW - timedelta(hours=1)),
            queue_row(DAY_1, requested_at=NOW - timedelta(hours=2)),
        ]
    )
    install_queue(monkeypatch, store)

    claim = module._claim_next(NOW)

    assert claim.day == DAY_1
    assert claim.attempt_count == 1
    assert store.rows[DAY_1]["started_at"] == NOW + module.CLAIM_LEASE
    select_sql = store.sql[0][0]
    assert "ORDER BY requested_at ASC, day ASC" in select_sql
    assert "FOR UPDATE SKIP LOCKED" in select_sql


def test_two_claimers_cannot_claim_the_same_active_day(monkeypatch):
    module = recalc()
    store = QueueStore(
        [
            queue_row(DAY_1, requested_at=NOW - timedelta(hours=2)),
            queue_row(DAY_2, requested_at=NOW - timedelta(hours=1)),
        ]
    )
    install_queue(monkeypatch, store)

    first = module._claim_next(NOW)
    second = module._claim_next(NOW)

    assert (first.day, second.day) == (DAY_1, DAY_2)
    assert module._claim_next(NOW) is None


def test_crashed_claim_recovers_at_lease_boundary_not_before(monkeypatch):
    module = recalc()
    store = QueueStore(
        [
            queue_row(
                DAY_1,
                requested_at=NOW - timedelta(hours=1),
                started_at=NOW,
                attempt_count=1,
            )
        ]
    )
    install_queue(monkeypatch, store)

    assert module._claim_next(NOW - timedelta(microseconds=1)) is None
    recovered = module._claim_next(NOW)
    assert recovered.day == DAY_1
    assert recovered.attempt_count == 2


def test_failure_records_bounded_error_and_retry_equality_is_eligible(monkeypatch):
    module = recalc()
    store = QueueStore([queue_row(DAY_1, requested_at=NOW - timedelta(hours=1))])
    install_queue(monkeypatch, store)
    claim = module._claim_next(NOW)

    retry_at = module._record_failure(claim, RuntimeError("x" * 1000), NOW)

    assert retry_at == NOW + module._retry_delay(claim.attempt_count)
    assert len(store.rows[DAY_1]["last_error"]) == module.ERROR_LIMIT
    assert module._claim_next(retry_at - timedelta(microseconds=1)) is None
    assert module._claim_next(retry_at).day == DAY_1


def test_retry_delay_is_bounded_exponential():
    module = recalc()
    assert module._retry_delay(1) == timedelta(seconds=15)
    assert module._retry_delay(2) == timedelta(seconds=30)
    assert module._retry_delay(99) == timedelta(minutes=15)


def test_success_marks_completed_keeps_historical_strict_marker_and_invalidates_after_commit(
    monkeypatch,
):
    module = recalc()
    store = QueueStore(
        [queue_row(DAY_1, requested_at=NOW - timedelta(hours=1))],
        strict_days={DAY_1},
    )
    install_queue(monkeypatch, store)
    events = store.events
    monkeypatch.setattr(
        "zira_dashboard.precompute.prepare_day",
        lambda day, client: (
            events.append(("precompute", day, client))
            or prepared_snapshot(day, 10, 20, strict=True)
        ),
    )
    monkeypatch.setattr(
        module,
        "_refresh_caches",
        lambda day: events.append(("invalidate", day)),
    )

    result = module.process_next(production_client="zira", now_utc=NOW)

    assert result.status == "completed"
    assert result.rows_written == 2
    assert store.rows[DAY_1]["completed_at"] == NOW
    assert store.strict_days == {DAY_1}
    precompute_event = events.index(("precompute", DAY_1, "zira"))
    assert events[precompute_event - 1] == "commit"
    assert events[precompute_event + 1] == "begin"
    complete_commit = max(i for i, event in enumerate(events) if event == "commit")
    invalidate = events.index(("invalidate", DAY_1))
    assert complete_commit < invalidate


def test_expired_older_worker_cannot_overwrite_newer_completed_snapshot(monkeypatch):
    module = recalc()
    store = QueueStore([queue_row(DAY_1, requested_at=NOW - timedelta(hours=1))])
    install_queue(monkeypatch, store)
    nested_results = []

    def interleaved_prepare(day, client):
        if client == "older":
            nested_results.append(
                module.process_next(
                    production_client="newer",
                    now_utc=NOW + module.CLAIM_LEASE,
                )
            )
            return prepared_snapshot(day, 10, strict=True)
        return prepared_snapshot(day, 20, strict=True)

    monkeypatch.setattr(
        "zira_dashboard.precompute.prepare_day",
        interleaved_prepare,
    )
    monkeypatch.setattr(module, "_refresh_caches", lambda day: None)

    older_result = module.process_next(production_client="older", now_utc=NOW)

    assert nested_results[0].status == "completed"
    assert older_result.status == "superseded"
    assert [row[4] for row in store.production[DAY_1]] == [20]
    assert store.strict_days == {DAY_1}
    assert store.rows[DAY_1]["attempt_count"] == 2
    assert store.rows[DAY_1]["completed_at"] == NOW + module.CLAIM_LEASE
    fenced_transactions = [sql for sql, _params in store.sql if "attendance_recalc_queue" in sql]
    assert any("FOR UPDATE" in sql for sql in fenced_transactions)


def test_completion_failure_rolls_back_marker_snapshot_and_queue_together(monkeypatch):
    module = recalc()
    store = QueueStore(
        [queue_row(DAY_1, requested_at=NOW - timedelta(hours=1))],
        fail_completion=True,
    )
    old_row = (DAY_1, "old", "Old Worker", "Repair 1", 5, 0, 1, 1, 0)
    store.production[DAY_1] = [old_row]
    install_queue(monkeypatch, store)
    claim = module._claim_next(NOW)

    with pytest.raises(RuntimeError, match="changed while completing"):
        module._complete_claim(
            claim,
            prepared_snapshot(DAY_1, 20, strict=True),
            NOW,
        )

    assert store.production[DAY_1] == [old_row]
    assert store.strict_days == set()
    assert store.rows[DAY_1]["completed_at"] is None
    assert store.rows[DAY_1]["started_at"] == claim.lease_until
    assert store.events[-1] == "rollback"


def test_failure_stays_retryable_and_never_invalidates_cache(monkeypatch):
    module = recalc()
    store = QueueStore([queue_row(DAY_1, requested_at=NOW - timedelta(hours=1))])
    install_queue(monkeypatch, store)
    monkeypatch.setattr(
        "zira_dashboard.precompute.prepare_day",
        lambda day, client: (_ for _ in ()).throw(RuntimeError("zira unavailable")),
    )
    monkeypatch.setattr(
        module,
        "_refresh_caches",
        lambda day: pytest.fail("failed recomputation invalidated caches"),
    )

    result = module.process_next(production_client="zira", now_utc=NOW)

    assert result.status == "failed"
    assert result.error == "zira unavailable"
    assert store.rows[DAY_1]["completed_at"] is None
    assert store.rows[DAY_1]["attempt_count"] == 1
    assert store.rows[DAY_1]["started_at"] == NOW + timedelta(seconds=15)


def test_failure_recording_failure_reports_original_and_releases_by_lease(monkeypatch):
    module = recalc()
    store = QueueStore(
        [queue_row(DAY_1, requested_at=NOW - timedelta(hours=1))],
        fail_failure_recording=True,
    )
    install_queue(monkeypatch, store)
    monkeypatch.setattr(
        "zira_dashboard.precompute.prepare_day",
        lambda day, client: (_ for _ in ()).throw(RuntimeError("original failure")),
    )

    result = module.process_next(production_client="zira", now_utc=NOW)

    assert result.status == "failed"
    assert result.error == "original failure"
    assert result.record_error == "queue write failed"
    assert module._claim_next(NOW + module.CLAIM_LEASE).day == DAY_1


def test_lazy_zira_client_is_loaded_only_after_a_job_is_claimed(monkeypatch):
    module = recalc()
    empty = QueueStore([])
    install_queue(monkeypatch, empty)
    monkeypatch.setattr(
        module,
        "_default_production_client",
        lambda: pytest.fail("empty queue loaded Zira client"),
    )
    assert module.process_next(now_utc=NOW) is None

    queued = QueueStore([queue_row(DAY_1, requested_at=NOW - timedelta(hours=1))])
    install_queue(monkeypatch, queued)
    sentinel = object()
    monkeypatch.setattr(module, "_default_production_client", lambda: sentinel)
    seen = []
    monkeypatch.setattr(
        "zira_dashboard.precompute.prepare_day",
        lambda day, client: seen.append((day, client)) or prepared_snapshot(day),
    )
    monkeypatch.setattr(module, "_refresh_caches", lambda day: None)

    assert module.process_next(now_utc=NOW).status == "completed"
    assert seen == [(DAY_1, sentinel)]


def test_refreshes_staffing_and_http_caches_after_success(monkeypatch):
    module = recalc()
    from zira_dashboard import _http_cache, staffing

    events = []
    monkeypatch.setattr(
        staffing, "invalidate_schedule_cache", lambda day: events.append(("staffing", day))
    )
    monkeypatch.setattr(_http_cache, "invalidate_all_cache", lambda: events.append(("http", None)))

    module._refresh_caches(DAY_1)

    assert events == [("staffing", DAY_1), ("http", None)]


def test_app_registers_exactly_one_nonblocking_fifteen_second_recalc_warmer():
    module = recalc()
    from zira_dashboard import app as app_module

    matches = [
        warmer for warmer in app_module._WARMERS if warmer[1] is app_module._tick_attendance_recalc
    ]
    assert matches == [("attendance recalculation", app_module._tick_attendance_recalc, 15)]
    assert module.process_next is not None
