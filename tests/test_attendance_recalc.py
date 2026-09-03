from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
import importlib
import os

import pytest


DAY_1 = date(2026, 8, 18)
DAY_2 = date(2026, 8, 19)
NOW = datetime(2026, 8, 30, 15, tzinfo=UTC)
FINISHED = NOW + timedelta(minutes=20)


_needs_postgres = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs local Postgres"
)


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
        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            self.result = None
            return
        if normalized.startswith("LOCK TABLE app_settings, attendance_strict_days"):
            self.result = None
            return
        if normalized.startswith("SELECT day, attempt_count, started_at, completed_at"):
            self.result = self.store.rows.get(params[0])
            if self.result is not None:
                self.result = dict(self.result)
            return
        if normalized.startswith("SELECT day, attempt_count, completed_at"):
            now = params[0]
            eligible = sorted(
                (
                    row
                    for row in self.store.rows.values()
                    if row["completed_at"] is not None
                    and row["cache_ready_at"] is None
                    and (row["cache_started_at"] is None or row["cache_started_at"] <= now)
                ),
                key=lambda row: (row["completed_at"], row["requested_at"], row["day"]),
            )
            self.result = dict(eligible[0]) if eligible else None
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
        if normalized.startswith("UPDATE attendance_recalc_queue SET cache_started_at = %s"):
            lease_until, day, completed_at = params
            row = self.store.rows[day]
            if row["completed_at"] != completed_at or row["cache_ready_at"] is not None:
                self.result = None
            else:
                row["cache_started_at"] = lease_until
                self.result = {"day": day}
            return
        if "SET completed_at = %s" in normalized:
            completed_at, cache_lease, day, attempt_count, lease_until = params
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
                row["cache_started_at"] = cache_lease
                row["cache_ready_at"] = None
                row["last_error"] = None
                self.result = {"day": day}
            return
        if normalized.startswith("UPDATE attendance_recalc_queue SET cache_started_at = NULL"):
            ready_at, day, completed_at, attempt_count, cache_lease = params
            row = self.store.rows[day]
            if (
                row["completed_at"] != completed_at
                or row["attempt_count"] != attempt_count
                or row["cache_ready_at"] is not None
                or row["cache_started_at"] != cache_lease
            ):
                self.result = None
            else:
                row["cache_started_at"] = None
                row["cache_ready_at"] = ready_at
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
    cache_started_at=None,
    cache_ready_at=None,
    attempt_count=0,
    last_error=None,
):
    return {
        "day": day,
        "reason": "mirror_changed",
        "requested_at": requested_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "cache_started_at": cache_started_at,
        "cache_ready_at": cache_ready_at,
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

    result = module.process_next(production_client="zira", now_utc=NOW, clock=lambda: NOW)

    assert result.status == "completed"
    assert result.rows_written == 2
    assert store.rows[DAY_1]["completed_at"] == NOW
    assert store.strict_days == {DAY_1}
    precompute_event = events.index(("precompute", DAY_1, "zira"))
    assert events[precompute_event - 1] == "commit"
    assert events[precompute_event + 1] == "begin"
    invalidate = events.index(("invalidate", DAY_1))
    assert "commit" in events[:invalidate]
    assert "commit" in events[invalidate + 1 :]


def test_correction_waits_in_recalc_commit_to_cache_ready_gap(monkeypatch):
    module = recalc()
    from zira_dashboard import attendance_corrections, db

    store = QueueStore(
        [queue_row(DAY_1, requested_at=NOW - timedelta(hours=1))],
        strict_days={DAY_1},
    )
    install_queue(monkeypatch, store)
    monkeypatch.setattr(
        "zira_dashboard.precompute.prepare_day",
        lambda day, _client: prepared_snapshot(day, 10, strict=True),
    )

    def query_queue(_sql, _params):
        return [dict(store.rows[DAY_1])]

    monkeypatch.setattr(db, "query", query_queue)
    refreshes = []

    def paused_refresh(day):
        # This callback is the deterministic pause after the production write
        # commits and before cache invalidation returns.
        assert store.events[-1] == "commit"
        assert store.rows[day]["completed_at"] == NOW
        assert store.rows[day]["cache_ready_at"] is None
        assert attendance_corrections._recalc_complete([day]) is False
        refreshes.append(day)

    monkeypatch.setattr(module, "_refresh_caches", paused_refresh)

    result = module.process_next(
        production_client="zira",
        now_utc=NOW,
        clock=lambda: NOW,
    )

    assert result.status == "completed"
    assert refreshes == [DAY_1]
    assert store.rows[DAY_1]["cache_started_at"] is None
    assert store.rows[DAY_1]["cache_ready_at"] == NOW
    assert attendance_corrections._recalc_complete([DAY_1]) is True


def test_crash_after_recalc_commit_recovers_cache_once_at_lease_boundary(monkeypatch):
    module = recalc()
    cache_lease = NOW + module.CLAIM_LEASE
    store = QueueStore(
        [
            queue_row(
                DAY_1,
                requested_at=NOW - timedelta(hours=1),
                completed_at=NOW,
                cache_started_at=cache_lease,
                attempt_count=1,
            )
        ]
    )
    install_queue(monkeypatch, store)
    refreshes = []
    monkeypatch.setattr(module, "_refresh_caches", lambda day: refreshes.append(day))
    monkeypatch.setattr(
        module,
        "_precompute_module",
        lambda: pytest.fail("cache recovery recomputed production"),
    )

    assert module.process_next(now_utc=cache_lease - timedelta(microseconds=1)) is None
    result = module.process_next(now_utc=cache_lease, clock=lambda: FINISHED)

    assert result.status == "completed"
    assert result.rows_written == 0
    assert refreshes == [DAY_1]
    assert store.rows[DAY_1]["cache_started_at"] is None
    assert store.rows[DAY_1]["cache_ready_at"] == FINISHED
    assert module.process_next(now_utc=FINISHED) is None


def test_expired_cache_recovery_runs_before_new_production_claim(monkeypatch):
    module = recalc()
    store = QueueStore(
        [
            queue_row(
                DAY_1,
                requested_at=NOW - timedelta(hours=1),
                completed_at=NOW - timedelta(minutes=30),
                cache_started_at=NOW,
                attempt_count=1,
            ),
            queue_row(DAY_2, requested_at=NOW - timedelta(hours=2)),
        ]
    )
    install_queue(monkeypatch, store)
    refreshes = []
    monkeypatch.setattr(module, "_refresh_caches", lambda day: refreshes.append(day))
    monkeypatch.setattr(
        module,
        "_precompute_module",
        lambda: pytest.fail("new production delayed expired cache recovery"),
    )

    result = module.process_next(now_utc=NOW, clock=lambda: FINISHED)

    assert result.day == DAY_1
    assert result.status == "completed"
    assert result.rows_written == 0
    assert refreshes == [DAY_1]
    assert store.rows[DAY_2]["attempt_count"] == 0


def test_cache_refresh_failure_stays_pending_until_lease_recovery(monkeypatch):
    module = recalc()
    store = QueueStore(
        [
            queue_row(
                DAY_1,
                requested_at=NOW - timedelta(hours=1),
                completed_at=NOW - timedelta(minutes=30),
                cache_started_at=NOW,
                attempt_count=1,
            )
        ]
    )
    install_queue(monkeypatch, store)
    monkeypatch.setattr(
        module,
        "_refresh_caches",
        lambda _day: (_ for _ in ()).throw(RuntimeError("cache unavailable")),
    )

    failed = module.process_next(now_utc=NOW, clock=lambda: NOW)

    assert failed.status == "failed"
    assert failed.retry_at == NOW + module.CLAIM_LEASE
    assert store.rows[DAY_1]["cache_ready_at"] is None
    assert store.rows[DAY_1]["cache_started_at"] == failed.retry_at
    assert module.process_next(now_utc=failed.retry_at - timedelta(microseconds=1)) is None

    monkeypatch.setattr(module, "_refresh_caches", lambda _day: None)
    recovered = module.process_next(now_utc=failed.retry_at, clock=lambda: FINISHED)

    assert recovered.status == "completed"
    assert store.rows[DAY_1]["cache_ready_at"] == FINISHED


def test_stale_cache_owner_cannot_publish_newer_workers_readiness(monkeypatch):
    module = recalc()
    newer_lease = FINISHED + module.CLAIM_LEASE
    store = QueueStore(
        [
            queue_row(
                DAY_1,
                requested_at=NOW - timedelta(hours=1),
                completed_at=NOW,
                cache_started_at=newer_lease,
                attempt_count=1,
            )
        ]
    )
    install_queue(monkeypatch, store)
    stale = module.CacheRefreshClaim(
        day=DAY_1,
        attempt_count=1,
        completed_at=NOW,
        lease_until=NOW + module.CLAIM_LEASE,
    )

    assert module._mark_cache_ready(stale, FINISHED) is False
    assert store.rows[DAY_1]["cache_ready_at"] is None
    assert store.rows[DAY_1]["cache_started_at"] == newer_lease


def test_two_cache_recovery_workers_cannot_claim_the_same_day(monkeypatch):
    module = recalc()
    store = QueueStore(
        [
            queue_row(
                DAY_1,
                requested_at=NOW - timedelta(hours=1),
                completed_at=NOW - timedelta(minutes=30),
                cache_started_at=NOW,
                attempt_count=1,
            )
        ]
    )
    install_queue(monkeypatch, store)

    first = module._claim_pending_cache(NOW)
    second = module._claim_pending_cache(NOW)

    assert first.day == DAY_1
    assert first.lease_until == NOW + module.CLAIM_LEASE
    assert second is None


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
                    clock=lambda: NOW + module.CLAIM_LEASE,
                )
            )
            return prepared_snapshot(day, 10, strict=True)
        return prepared_snapshot(day, 20, strict=True)

    monkeypatch.setattr(
        "zira_dashboard.precompute.prepare_day",
        interleaved_prepare,
    )
    monkeypatch.setattr(module, "_refresh_caches", lambda day: None)

    older_result = module.process_next(production_client="older", now_utc=NOW, clock=lambda: NOW)

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


def test_completion_serializes_before_queue_and_matcher_locks(monkeypatch):
    module = recalc()
    from zira_dashboard import attendance_readiness

    lease_until = NOW + module.CLAIM_LEASE
    store = QueueStore(
        [
            queue_row(
                DAY_1,
                requested_at=NOW - timedelta(hours=1),
                started_at=lease_until,
                attempt_count=1,
            )
        ]
    )
    install_queue(monkeypatch, store)
    monkeypatch.setattr(
        "zira_dashboard.precompute.store_prepared_day",
        lambda _prepared, *, cur: (
            cur.execute(
                "LOCK TABLE app_settings, attendance_strict_days IN SHARE ROW EXCLUSIVE MODE"
            )
            or 0
        ),
    )
    claim = module.RecalcClaim(DAY_1, 1, lease_until)

    assert module._complete_claim(claim, prepared_snapshot(DAY_1), NOW) == 0

    sql = [statement for statement, _params in store.sql]
    advisory_lock = next(
        index
        for index, operation in enumerate(store.sql)
        if operation[1] == (attendance_readiness._READINESS_LOCK_ID,)
    )
    queue_lock = next(
        index
        for index, statement in enumerate(sql)
        if "FROM attendance_recalc_queue" in statement and statement.endswith("FOR UPDATE")
    )
    matcher_lock = sql.index(
        "LOCK TABLE app_settings, attendance_strict_days IN SHARE ROW EXCLUSIVE MODE"
    )
    assert advisory_lock < queue_lock < matcher_lock


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

    result = module.process_next(production_client="zira", now_utc=NOW, clock=lambda: NOW)

    assert result.status == "failed"
    assert result.error == "zira unavailable"
    assert store.rows[DAY_1]["completed_at"] is None
    assert store.rows[DAY_1]["attempt_count"] == 1
    assert store.rows[DAY_1]["started_at"] == NOW + timedelta(seconds=15)


def test_failure_backoff_starts_at_actual_finish_time(monkeypatch):
    module = recalc()
    store = QueueStore([queue_row(DAY_1, requested_at=NOW - timedelta(hours=1))])
    install_queue(monkeypatch, store)
    monkeypatch.setattr(
        "zira_dashboard.precompute.prepare_day",
        lambda day, client: (_ for _ in ()).throw(RuntimeError("late failure")),
    )

    result = module.process_next(
        production_client="zira",
        now_utc=NOW,
        clock=lambda: FINISHED,
    )

    assert result.status == "failed"
    assert result.retry_at == FINISHED + timedelta(seconds=15)
    assert store.rows[DAY_1]["started_at"] == result.retry_at


def test_completion_uses_actual_finish_time(monkeypatch):
    module = recalc()
    store = QueueStore([queue_row(DAY_1, requested_at=NOW - timedelta(hours=1))])
    install_queue(monkeypatch, store)
    monkeypatch.setattr(
        "zira_dashboard.precompute.prepare_day",
        lambda day, client: prepared_snapshot(day),
    )
    monkeypatch.setattr(module, "_refresh_caches", lambda day: None)

    result = module.process_next(
        production_client="zira",
        now_utc=NOW,
        clock=lambda: FINISHED,
    )

    assert result.status == "completed"
    assert store.rows[DAY_1]["completed_at"] == FINISHED


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

    result = module.process_next(production_client="zira", now_utc=NOW, clock=lambda: NOW)

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

    assert module.process_next(now_utc=NOW, clock=lambda: NOW).status == "completed"
    assert seen == [(DAY_1, sentinel)]


@pytest.mark.parametrize("failure_boundary", ["client", "module"])
def test_lazy_dependency_resolution_failure_is_recorded_for_retry(monkeypatch, failure_boundary):
    module = recalc()
    store = QueueStore([queue_row(DAY_1, requested_at=NOW - timedelta(hours=1))])
    install_queue(monkeypatch, store)
    if failure_boundary == "client":
        monkeypatch.setattr(
            module,
            "_default_production_client",
            lambda: (_ for _ in ()).throw(RuntimeError("client resolution failed")),
        )
    else:
        monkeypatch.setattr(
            module,
            "_precompute_module",
            lambda: (_ for _ in ()).throw(RuntimeError("module resolution failed")),
            raising=False,
        )

    result = module.process_next(now_utc=NOW, clock=lambda: FINISHED)

    assert result.status == "failed"
    assert result.error == f"{failure_boundary} resolution failed"
    assert result.retry_at == FINISHED + timedelta(seconds=15)


@_needs_postgres
def test_stale_claim_cannot_complete_or_reopen_replacement_completion():
    from zira_dashboard import attendance_recalc, db

    test_day = date(2098, 8, 21)
    lease_until = NOW + attendance_recalc.CLAIM_LEASE
    replacement_completed_at = FINISHED + timedelta(minutes=5)
    stale_claim = attendance_recalc.RecalcClaim(
        day=test_day,
        attempt_count=1,
        lease_until=lease_until,
    )
    db.init_pool()
    db.bootstrap_schema()
    db.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (test_day,))
    db.execute(
        "INSERT INTO attendance_recalc_queue "
        "(day, reason, requested_at, started_at, completed_at, attempt_count) "
        "VALUES (%s, %s, %s, NULL, %s, 2)",
        (test_day, "replacement", NOW, replacement_completed_at),
    )
    try:
        assert (
            attendance_recalc._complete_claim(
                stale_claim,
                prepared_snapshot(test_day, 99),
                FINISHED,
            )
            is None
        )
        with pytest.raises(RuntimeError, match="superseded"):
            attendance_recalc._record_failure(
                stale_claim,
                RuntimeError("stale failure"),
                FINISHED,
            )

        stored = db.query(
            "SELECT started_at, completed_at, attempt_count, last_error "
            "FROM attendance_recalc_queue WHERE day = %s",
            (test_day,),
        )[0]
        assert stored == {
            "started_at": None,
            "completed_at": replacement_completed_at,
            "attempt_count": 2,
            "last_error": None,
        }
    finally:
        db.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (test_day,))


def test_refreshes_staffing_and_http_caches_after_success(monkeypatch):
    module = recalc()
    from zira_dashboard import _http_cache, staffing

    events = []
    monkeypatch.setattr(
        staffing,
        "invalidate_schedule_cache",
        lambda day: events.append(("staffing", day)),
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
