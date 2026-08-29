from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
import threading

import pytest

from zira_dashboard import attendance_mirror, attendance_sync


NOW = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)


@dataclass(frozen=True)
class CompleteSweepSnapshot:
    ids: tuple[object, ...]
    complete: bool = True


def _row(
    attendance_id: int,
    *,
    write_date: datetime,
    check_in: datetime = datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
    check_out: datetime | None = None,
) -> dict:
    return {
        "odoo_attendance_id": attendance_id,
        "employee_odoo_id": 44,
        "employee_name": "Adrian A.",
        "check_in_utc": check_in,
        "check_out_utc": check_out,
        "odoo_work_center_id": 72,
        "odoo_work_center_name": "Unknown Odoo / Dismantler 1",
        "odoo_department_id": 8,
        "odoo_department_name": "01 Recycled",
        "odoo_write_date": write_date,
    }


class CompleteOdooSource:
    def __init__(
        self,
        *,
        changes=(),
        open_rows=(),
        ids=(901,),
        sweep_complete=True,
        rows_by_id=(),
    ):
        self.changes = changes
        self.open_rows = open_rows
        self.ids = ids
        self.sweep_complete = sweep_complete
        self.rows_by_id = rows_by_id
        self.change_calls: list[dict] = []
        self.open_calls = 0
        self.sweep_calls = 0
        self.rows_by_id_calls: list[tuple[int, ...]] = []

    @staticmethod
    def _read(value):
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value()
        return list(value)

    def fetch_attendance_changes(self, **kwargs):
        self.change_calls.append(kwargs)
        return self._read(self.changes)

    def fetch_open_attendance_rows(self):
        self.open_calls += 1
        return self._read(self.open_rows)

    def fetch_all_attendance_ids(self):
        self.sweep_calls += 1
        return self._read(self.ids)

    def fetch_complete_attendance_id_sweep(self):
        self.sweep_calls += 1
        return CompleteSweepSnapshot(
            ids=tuple(self._read(self.ids)),
            complete=self.sweep_complete,
        )

    def fetch_attendance_rows_by_ids(self, ids):
        requested = tuple(ids)
        self.rows_by_id_calls.append(requested)
        value = self.rows_by_id
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value(requested)
        return list(value)


class TransactionalMirrorBackend:
    def __init__(
        self,
        state=None,
        *,
        deleted_count: int = 0,
        active_ids=(),
        tombstoned_ids=(),
    ):
        self.state = state or attendance_sync.SyncState(
            cursor_write_date=None,
            cursor_id=None,
            last_incremental_completed_at=None,
            last_full_sweep_completed_at=None,
            full_sweep_generation=0,
            baseline_completed_at=None,
        )
        self.started: list[datetime] = []
        self.incremental_transactions: list[dict] = []
        self.sweep_transactions: list[dict] = []
        self.failures: list[str] = []
        self.baseline_attempts: list[datetime] = []
        self.fail_incremental_store = False
        self.deleted_count = deleted_count
        self.active_ids = set(active_ids)
        self.tombstoned_ids = set(tombstoned_ids)
        self.lock_events: list[str] = []
        self.transaction_events: list[str] = []
        self.lock_depth = 0
        self._run_lock = threading.Lock()

    @contextmanager
    def logical_run(self):
        with self._run_lock:
            self.lock_events.append("enter")
            self.transaction_events.append("begin")
            self.lock_depth += 1
            state_before = self.state
            started_count = len(self.started)
            incremental_count = len(self.incremental_transactions)
            sweep_count = len(self.sweep_transactions)
            errors_before = getattr(self, "errors", None)
            if errors_before is not None:
                errors_before = dict(errors_before)
            try:
                yield self
            except Exception:
                self.state = state_before
                del self.started[started_count:]
                del self.incremental_transactions[incremental_count:]
                del self.sweep_transactions[sweep_count:]
                if errors_before is not None:
                    self.errors = errors_before
                self.transaction_events.append("rollback")
                raise
            else:
                self.transaction_events.append("commit")
            finally:
                self.lock_depth -= 1
                self.lock_events.append("exit")

    def sync_state(self):
        return self.state

    def record_incremental_started(self, started_at):
        self.started.append(started_at)

    def store_incremental_cycle(
        self,
        rows,
        *,
        cursor_write_date,
        cursor_id,
        completed_at,
        observed_at,
    ):
        if self.fail_incremental_store:
            raise RuntimeError("database transaction failed")
        self.incremental_transactions.append(
            {
                "rows": tuple(rows),
                "cursor_write_date": cursor_write_date,
                "cursor_id": cursor_id,
                "completed_at": completed_at,
            }
        )
        self.state = replace(
            self.state,
            cursor_write_date=cursor_write_date or self.state.cursor_write_date,
            cursor_id=(
                cursor_id if cursor_write_date is not None else self.state.cursor_id
            ),
            last_incremental_completed_at=completed_at,
        )
        return {date(2026, 8, 28)} if self.state.baseline_completed_at else set()

    def active_attendance_ids(self):
        return set(self.active_ids)

    def tombstoned_attendance_ids(self, ids):
        return set(ids) & self.tombstoned_ids

    def store_full_sweep(
        self,
        ids,
        *,
        recovery_rows=(),
        generation,
        completed_at,
        observed_at,
    ):
        self.sweep_transactions.append(
            {
                "ids": set(ids),
                "generation": generation,
                "completed_at": completed_at,
                "recovery_rows": tuple(recovery_rows),
            }
        )
        self.state = replace(
            self.state,
            last_full_sweep_completed_at=completed_at,
            full_sweep_generation=generation,
        )
        affected = (
            {date(2026, 8, 27)} if self.state.baseline_completed_at else set()
        )
        return attendance_sync.SweepStoreResult(
            affected_days=frozenset(affected),
            deleted_count=self.deleted_count,
        )

    def record_failure(self, owner, error):
        self.transaction_events.append("record_failure")
        self.failures.append(error)

    def complete_baseline_if_ready(self, completed_at):
        self.baseline_attempts.append(completed_at)
        if (
            self.state.last_incremental_completed_at is not None
            and self.state.last_full_sweep_completed_at is not None
        ):
            self.state = replace(self.state, baseline_completed_at=completed_at)
            return True
        return False


class CountingSweepBackend(TransactionalMirrorBackend):
    def __init__(self, *, deleted_count: int):
        super().__init__(deleted_count=deleted_count)


class OwnedErrorBackend(TransactionalMirrorBackend):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.errors: dict[str, str] = {}
        self.fail_baseline = False
        self.fail_recording = False

    def record_failure(self, owner, error):
        if self.fail_recording:
            raise RuntimeError("health write failed")
        self.failures.append(f"{owner}:{error}")
        self.errors[owner] = error

    def store_incremental_cycle(self, *args, **kwargs):
        result = super().store_incremental_cycle(*args, **kwargs)
        self.errors.pop("incremental", None)
        return result

    def store_full_sweep(self, *args, **kwargs):
        result = super().store_full_sweep(*args, **kwargs)
        self.errors.pop("sweep", None)
        return result

    def complete_baseline_if_ready(self, completed_at):
        if self.fail_baseline:
            raise RuntimeError("baseline state failed")
        result = super().complete_baseline_if_ready(completed_at)
        if result:
            self.errors.pop("baseline", None)
        return result


@pytest.fixture
def install_dependencies(monkeypatch):
    def install(source, backend):
        monkeypatch.setattr(attendance_sync, "_source", source)
        monkeypatch.setattr(attendance_sync, "_backend", backend)
        return source, backend

    return install


def _install_single_cursor_production_backend(monkeypatch):
    events = []

    class Cursor:
        def execute(self, sql, params=None):
            assert "pg_advisory_xact_lock" in sql
            events.append(("lock", self, params))

    cursor = Cursor()

    @contextmanager
    def one_connection_only():
        if any(event[0] == "checkout" for event in events):
            raise AssertionError("successful run attempted a second pool checkout")
        events.append(("checkout", cursor))
        try:
            yield cursor
        except Exception:
            events.append(("rollback", cursor))
            raise
        else:
            events.append(("commit", cursor))

    def state_for(used_cursor):
        events.append(("state", used_cursor))
        return attendance_mirror.SyncState(
            cursor_write_date=None,
            cursor_id=None,
            last_incremental_completed_at=None,
            last_full_sweep_completed_at=None,
            full_sweep_generation=0,
            baseline_completed_at=None,
        )

    monkeypatch.setattr(attendance_mirror.db, "cursor", one_connection_only)
    monkeypatch.setattr(
        attendance_mirror, "_sync_state_cur", state_for, raising=False
    )
    monkeypatch.setattr(
        attendance_mirror,
        "_record_incremental_started_cur",
        lambda used_cursor, _at: events.append(("started", used_cursor)),
        raising=False,
    )
    monkeypatch.setattr(
        attendance_mirror,
        "_store_incremental_cycle_cur",
        lambda used_cursor, *_args, **_kwargs: (
            events.append(("incremental_store", used_cursor)) or set()
        ),
        raising=False,
    )
    monkeypatch.setattr(
        attendance_mirror,
        "_active_attendance_ids_cur",
        lambda used_cursor: (
            events.append(("active_ids", used_cursor)) or set()
        ),
        raising=False,
    )
    monkeypatch.setattr(
        attendance_mirror,
        "_tombstoned_attendance_ids_cur",
        lambda used_cursor, _ids: (
            events.append(("tombstoned_ids", used_cursor)) or set()
        ),
        raising=False,
    )
    monkeypatch.setattr(
        attendance_mirror,
        "_store_full_sweep_cur",
        lambda used_cursor, *_args, **_kwargs: (
            events.append(("sweep_store", used_cursor))
            or attendance_mirror._FullSweepStoreResult(
                affected_days=frozenset(), deleted_count=0
            )
        ),
        raising=False,
    )
    backend = attendance_sync._MirrorBackend()
    monkeypatch.setattr(attendance_sync, "_backend", backend)
    return cursor, events


def test_production_incremental_lock_state_and_store_share_one_cursor(
    monkeypatch,
):
    cursor, events = _install_single_cursor_production_backend(monkeypatch)
    monkeypatch.setattr(attendance_sync, "_source", CompleteOdooSource())

    result = attendance_sync.run_incremental_sync(now_utc=NOW)

    assert result.success is True
    assert [event[0] for event in events] == [
        "checkout",
        "lock",
        "state",
        "started",
        "incremental_store",
        "commit",
    ]
    assert all(event[1] is cursor for event in events)


def test_production_forced_sweep_lock_state_and_store_share_one_cursor(
    monkeypatch,
):
    cursor, events = _install_single_cursor_production_backend(monkeypatch)
    monkeypatch.setattr(
        attendance_sync, "_source", CompleteOdooSource(ids=[901])
    )

    result = attendance_sync.run_full_sweep(now_utc=NOW)

    assert result.success is True
    assert [event[0] for event in events] == [
        "checkout",
        "lock",
        "state",
        "active_ids",
        "tombstoned_ids",
        "sweep_store",
        "commit",
    ]
    assert all(event[1] is cursor for event in events)


def test_source_failure_rolls_back_locked_transaction_before_failure_record(
    install_dependencies,
):
    backend = TransactionalMirrorBackend()
    install_dependencies(
        CompleteOdooSource(changes=RuntimeError("source failed")), backend
    )

    result = attendance_sync.run_incremental_sync(now_utc=NOW)

    assert result.success is False
    assert backend.started == []
    assert backend.transaction_events == [
        "begin",
        "rollback",
        "record_failure",
    ]


def test_lock_connection_loss_rolls_back_staged_rows_before_failure_record(
    install_dependencies,
):
    class ConnectionLossBackend(TransactionalMirrorBackend):
        @contextmanager
        def logical_run(self):
            with super().logical_run() as run:
                yield run
                raise ConnectionError("lock connection lost before commit")

    backend = ConnectionLossBackend()
    install_dependencies(
        CompleteOdooSource(changes=[_row(901, write_date=NOW)]), backend
    )

    result = attendance_sync.run_incremental_sync(now_utc=NOW)

    assert result.success is False
    assert result.error == "lock connection lost before commit"
    assert backend.incremental_transactions == []
    assert backend.state.last_incremental_completed_at is None
    assert backend.transaction_events == [
        "begin",
        "rollback",
        "record_failure",
    ]


def test_incremental_and_open_rows_merge_dedupe_and_store_as_one_cycle(
    install_dependencies,
):
    old = _row(901, write_date=NOW - timedelta(minutes=3))
    changed = _row(902, write_date=NOW - timedelta(minutes=1))
    refreshed = _row(901, write_date=NOW - timedelta(minutes=2))
    source, backend = install_dependencies(
        CompleteOdooSource(changes=[old, changed], open_rows=[refreshed]),
        TransactionalMirrorBackend(),
    )

    result = attendance_sync.run_incremental_sync(now_utc=NOW)

    assert result.success is True
    assert result.rows_stored == 2
    assert source.open_calls == 1
    assert len(backend.incremental_transactions) == 1
    transaction = backend.incremental_transactions[0]
    assert [row["odoo_attendance_id"] for row in transaction["rows"]] == [
        901,
        902,
    ]
    assert transaction["rows"][0] == refreshed
    assert transaction["cursor_write_date"] == changed["odoo_write_date"]
    assert transaction["cursor_id"] == 902


def test_failed_open_page_keeps_prior_cursor_and_last_good_mirror(
    install_dependencies,
):
    previous_cursor = NOW - timedelta(minutes=10)
    backend = TransactionalMirrorBackend(
        attendance_sync.SyncState(
            cursor_write_date=previous_cursor,
            cursor_id=700,
            last_incremental_completed_at=previous_cursor,
            last_full_sweep_completed_at=None,
            full_sweep_generation=0,
            baseline_completed_at=None,
        )
    )
    source, backend = install_dependencies(
        CompleteOdooSource(
            changes=[_row(901, write_date=NOW - timedelta(minutes=1))],
            open_rows=RuntimeError("open page interrupted"),
        ),
        backend,
    )

    result = attendance_sync.run_incremental_sync(now_utc=NOW)

    assert result.success is False
    assert backend.incremental_transactions == []
    assert backend.state.cursor_write_date == previous_cursor
    assert backend.state.cursor_id == 700
    assert backend.state.last_incremental_completed_at == previous_cursor
    assert backend.failures == ["open page interrupted"]
    assert source.change_calls == [
        {"after_write_date": previous_cursor, "after_id": 700}
    ]


def test_database_failure_does_not_report_cursor_or_freshness_success(
    install_dependencies,
):
    previous_cursor = NOW - timedelta(hours=1)
    backend = TransactionalMirrorBackend(
        attendance_sync.SyncState(
            cursor_write_date=previous_cursor,
            cursor_id=500,
            last_incremental_completed_at=previous_cursor,
            last_full_sweep_completed_at=None,
            full_sweep_generation=0,
            baseline_completed_at=None,
        )
    )
    backend.fail_incremental_store = True
    install_dependencies(
        CompleteOdooSource(
            changes=[_row(901, write_date=NOW - timedelta(minutes=1))]
        ),
        backend,
    )

    result = attendance_sync.run_incremental_sync(now_utc=NOW)

    assert result.success is False
    assert backend.state.cursor_write_date == previous_cursor
    assert backend.state.last_incremental_completed_at == previous_cursor
    assert backend.failures == ["database transaction failed"]
    assert backend.started == []
    assert backend.transaction_events == [
        "begin",
        "rollback",
        "record_failure",
    ]


def test_sweep_success_does_not_clear_incremental_failure(
    install_dependencies,
):
    backend = OwnedErrorBackend()
    source = CompleteOdooSource(
        changes=RuntimeError("incremental failed"), ids=[901]
    )
    install_dependencies(source, backend)

    incremental = attendance_sync.run_incremental_sync(now_utc=NOW)
    sweep = attendance_sync.run_full_sweep(now_utc=NOW)

    assert incremental.success is False
    assert sweep.success is True
    assert backend.errors == {"incremental": "incremental failed"}


def test_incremental_success_does_not_clear_sweep_failure(
    install_dependencies,
):
    backend = OwnedErrorBackend()
    source = CompleteOdooSource(ids=RuntimeError("sweep failed"))
    install_dependencies(source, backend)

    sweep = attendance_sync.run_full_sweep(now_utc=NOW)
    incremental = attendance_sync.run_incremental_sync(now_utc=NOW)

    assert sweep.success is False
    assert incremental.success is True
    assert backend.errors == {"sweep": "sweep failed"}


def test_same_owner_success_clears_only_its_prior_failure(install_dependencies):
    backend = OwnedErrorBackend()
    source = CompleteOdooSource(changes=RuntimeError("first failure"))
    install_dependencies(source, backend)

    failed = attendance_sync.run_incremental_sync(now_utc=NOW)
    assert backend.errors == {"incremental": "first failure"}
    source.changes = []
    succeeded = attendance_sync.run_incremental_sync(
        now_utc=NOW + timedelta(seconds=30)
    )

    assert failed.success is False
    assert succeeded.success is True
    assert backend.errors == {}


def test_baseline_failure_has_its_own_error_owner(install_dependencies):
    backend = OwnedErrorBackend()
    backend.fail_baseline = True
    install_dependencies(CompleteOdooSource(ids=[901]), backend)

    result = attendance_sync.tick(now_utc=NOW)

    assert result.success is False
    assert backend.errors == {"baseline": "baseline state failed"}


def test_baseline_failure_still_reports_committed_sweep_deletion_count(
    install_dependencies,
):
    backend = OwnedErrorBackend(deleted_count=2)
    backend.fail_baseline = True
    install_dependencies(CompleteOdooSource(ids=[901]), backend)

    result = attendance_sync.tick(now_utc=NOW)

    assert result.success is False
    assert result.rows_deleted == 2


def test_later_baseline_success_clears_only_baseline_error(install_dependencies):
    backend = OwnedErrorBackend()
    backend.fail_baseline = True
    install_dependencies(CompleteOdooSource(ids=[901]), backend)
    failed = attendance_sync.tick(now_utc=NOW)
    assert failed.success is False
    assert backend.errors == {"baseline": "baseline state failed"}

    backend.fail_baseline = False
    recovered = attendance_sync.tick(now_utc=NOW + timedelta(seconds=30))

    assert recovered.success is True
    assert recovered.baseline_completed is True
    assert backend.errors == {}


def test_failure_reporting_failure_preserves_original_result_and_releases_lock(
    install_dependencies, caplog
):
    backend = OwnedErrorBackend()
    backend.fail_recording = True
    install_dependencies(
        CompleteOdooSource(changes=RuntimeError("original source failure")),
        backend,
    )

    result = attendance_sync.run_incremental_sync(now_utc=NOW)

    assert result.success is False
    assert result.error == "original source failure"
    assert backend.lock_depth == 0
    assert "could not record Odoo attendance mirror failure" in caplog.text


def test_each_direct_sync_holds_same_logical_lock_through_source_and_store(
    install_dependencies,
):
    backend = TransactionalMirrorBackend()

    class LockCheckingSource(CompleteOdooSource):
        def fetch_attendance_changes(self, **kwargs):
            assert backend.lock_depth == 1
            return super().fetch_attendance_changes(**kwargs)

        def fetch_open_attendance_rows(self):
            assert backend.lock_depth == 1
            return super().fetch_open_attendance_rows()

        def fetch_complete_attendance_id_sweep(self):
            assert backend.lock_depth == 1
            return super().fetch_complete_attendance_id_sweep()

    install_dependencies(LockCheckingSource(ids=[901]), backend)

    incremental = attendance_sync.run_incremental_sync(now_utc=NOW)
    sweep = attendance_sync.run_full_sweep(now_utc=NOW)

    assert incremental.success is True
    assert sweep.success is True
    assert backend.lock_events == ["enter", "exit", "enter", "exit"]
    assert backend.lock_depth == 0


def test_logical_run_lock_releases_after_source_failure(install_dependencies):
    backend = TransactionalMirrorBackend()
    source = CompleteOdooSource(changes=RuntimeError("source failed"))
    install_dependencies(source, backend)

    failed = attendance_sync.run_incremental_sync(now_utc=NOW)
    source.changes = []
    recovered = attendance_sync.run_incremental_sync(
        now_utc=NOW + timedelta(seconds=30)
    )

    assert failed.success is False
    assert recovered.success is True
    assert backend.lock_events == ["enter", "exit", "enter", "exit"]
    assert backend.lock_depth == 0


def test_shared_lock_orders_incremental_before_concurrent_full_sweep(
    install_dependencies,
):
    backend = TransactionalMirrorBackend()
    first_source_entered = threading.Event()
    release_first_source = threading.Event()
    sweep_source_entered = threading.Event()

    class OrderedSource(CompleteOdooSource):
        def fetch_attendance_changes(self, **kwargs):
            first_source_entered.set()
            assert release_first_source.wait(timeout=2)
            return super().fetch_attendance_changes(**kwargs)

        def fetch_complete_attendance_id_sweep(self):
            sweep_source_entered.set()
            return super().fetch_complete_attendance_id_sweep()

    install_dependencies(OrderedSource(ids=[901]), backend)
    results = []
    incremental_thread = threading.Thread(
        target=lambda: results.append(
            attendance_sync.run_incremental_sync(now_utc=NOW)
        )
    )
    sweep_thread = threading.Thread(
        target=lambda: results.append(attendance_sync.run_full_sweep(now_utc=NOW))
    )

    incremental_thread.start()
    assert first_source_entered.wait(timeout=1)
    sweep_thread.start()
    overlapped = sweep_source_entered.wait(timeout=0.1)
    release_first_source.set()
    incremental_thread.join(timeout=2)
    sweep_thread.join(timeout=2)

    assert overlapped is False
    assert sweep_source_entered.is_set()
    assert len(results) == 2
    assert all(result.success for result in results)
    assert backend.lock_events == ["enter", "exit", "enter", "exit"]


def test_shared_lock_finishes_sweep_before_later_incremental_observation(
    install_dependencies,
):
    backend = TransactionalMirrorBackend()
    sweep_source_entered = threading.Event()
    release_sweep_source = threading.Event()
    incremental_source_entered = threading.Event()

    class OrderedSource(CompleteOdooSource):
        def fetch_complete_attendance_id_sweep(self):
            sweep_source_entered.set()
            assert release_sweep_source.wait(timeout=2)
            return super().fetch_complete_attendance_id_sweep()

        def fetch_attendance_changes(self, **kwargs):
            incremental_source_entered.set()
            return super().fetch_attendance_changes(**kwargs)

    install_dependencies(OrderedSource(ids=[901]), backend)
    results = []
    sweep_thread = threading.Thread(
        target=lambda: results.append(attendance_sync.run_full_sweep(now_utc=NOW))
    )
    incremental_thread = threading.Thread(
        target=lambda: results.append(
            attendance_sync.run_incremental_sync(now_utc=NOW)
        )
    )

    sweep_thread.start()
    assert sweep_source_entered.wait(timeout=1)
    incremental_thread.start()
    overlapped = incremental_source_entered.wait(timeout=0.1)
    release_sweep_source.set()
    sweep_thread.join(timeout=2)
    incremental_thread.join(timeout=2)

    assert overlapped is False
    assert incremental_source_entered.is_set()
    assert len(results) == 2
    assert all(result.success for result in results)
    assert backend.lock_events == ["enter", "exit", "enter", "exit"]


@pytest.mark.parametrize(
    "ids",
    [
        RuntimeError("page two failed"),
        [901, "bad"],
        [901, 901],
    ],
)
def test_failed_malformed_or_duplicate_sweep_marks_nothing_deleted(
    install_dependencies, ids
):
    source, backend = install_dependencies(
        CompleteOdooSource(ids=ids), TransactionalMirrorBackend()
    )

    result = attendance_sync.run_full_sweep(now_utc=NOW)

    assert result.success is False
    assert backend.sweep_transactions == []
    assert backend.state.last_full_sweep_completed_at is None
    assert len(backend.failures) == 1
    assert source.sweep_calls == 1
    assert backend.transaction_events == [
        "begin",
        "rollback",
        "record_failure",
    ]


def test_unconfirmed_empty_sweep_marks_nothing_deleted(install_dependencies):
    _source, backend = install_dependencies(
        CompleteOdooSource(ids=[], sweep_complete=False),
        TransactionalMirrorBackend(active_ids={901}),
    )

    result = attendance_sync.run_full_sweep(now_utc=NOW)

    assert result.success is False
    assert backend.sweep_transactions == []


def test_bare_id_list_without_complete_snapshot_boundary_is_rejected(
    install_dependencies,
):
    class BareListSource(CompleteOdooSource):
        def fetch_complete_attendance_id_sweep(self):
            self.sweep_calls += 1
            return [901]

    _source, backend = install_dependencies(
        BareListSource(), TransactionalMirrorBackend()
    )

    result = attendance_sync.run_full_sweep(now_utc=NOW)

    assert result.success is False
    assert "completion boundary" in result.error
    assert backend.sweep_transactions == []


def test_complete_sweep_commits_one_generation_after_complete_validated_read(
    install_dependencies,
):
    source, backend = install_dependencies(
        CompleteOdooSource(ids=[901, 902, 903]),
        TransactionalMirrorBackend(),
    )

    result = attendance_sync.run_full_sweep(now_utc=NOW)

    assert result.success is True
    assert result.full_sweep_completed is True
    assert backend.sweep_transactions == [
        {
            "ids": {901, 902, 903},
            "generation": 1,
            "completed_at": NOW,
            "recovery_rows": (),
        }
    ]
    assert source.sweep_calls == 1


def test_true_empty_complete_sweep_allows_first_baseline_when_mirror_is_empty(
    install_dependencies,
):
    source, backend = install_dependencies(
        CompleteOdooSource(ids=[]),
        TransactionalMirrorBackend(active_ids=[]),
    )

    result = attendance_sync.tick(now_utc=NOW)

    assert result.success is True
    assert result.full_sweep_completed is True
    assert result.baseline_completed is True
    assert backend.sweep_transactions == [
        {
            "ids": set(),
            "generation": 1,
            "completed_at": NOW,
            "recovery_rows": (),
        }
    ]
    assert source.rows_by_id_calls == []


def test_complete_empty_sweep_requires_direct_absence_confirmation_before_deletion(
    install_dependencies,
):
    source, backend = install_dependencies(
        CompleteOdooSource(ids=[], rows_by_id=[]),
        TransactionalMirrorBackend(
            active_ids={901, 902}, deleted_count=2
        ),
    )

    result = attendance_sync.run_full_sweep(now_utc=NOW)

    assert result.success is True
    assert result.rows_deleted == 2
    assert source.rows_by_id_calls == [(901, 902)]
    assert backend.sweep_transactions[0]["ids"] == set()


@pytest.mark.parametrize(
    "rows_by_id",
    [
        RuntimeError("confirmation page failed"),
        [_row(901, write_date=NOW)],
        [{"odoo_attendance_id": 901}],
    ],
)
def test_empty_sweep_without_complete_absence_proof_marks_nothing_deleted(
    install_dependencies, rows_by_id
):
    _source, backend = install_dependencies(
        CompleteOdooSource(ids=[], rows_by_id=rows_by_id),
        TransactionalMirrorBackend(active_ids={901}, deleted_count=1),
    )

    result = attendance_sync.run_full_sweep(now_utc=NOW)

    assert result.success is False
    assert backend.sweep_transactions == []


def test_sweep_recovers_source_present_tombstone_with_complete_row(
    install_dependencies,
):
    recovered = _row(
        901,
        write_date=NOW - timedelta(days=5),
        check_in=NOW - timedelta(days=5, hours=8),
        check_out=NOW - timedelta(days=5),
    )
    source, backend = install_dependencies(
        CompleteOdooSource(ids=[901, 902], rows_by_id=[recovered]),
        TransactionalMirrorBackend(tombstoned_ids={901}),
    )

    result = attendance_sync.run_full_sweep(now_utc=NOW)

    assert result.success is True
    assert source.rows_by_id_calls == [(901,)]
    assert backend.sweep_transactions[0]["recovery_rows"] == (recovered,)


@pytest.mark.parametrize(
    "recovery_rows",
    [
        [],
        [_row(999, write_date=NOW)],
        [_row(901, write_date=NOW), _row(901, write_date=NOW)],
        [{"odoo_attendance_id": 901}],
        RuntimeError("recovery page failed"),
    ],
)
def test_missing_malformed_duplicate_or_unrelated_recovery_aborts_whole_sweep(
    install_dependencies, recovery_rows
):
    _source, backend = install_dependencies(
        CompleteOdooSource(ids=[901], rows_by_id=recovery_rows),
        TransactionalMirrorBackend(tombstoned_ids={901}),
    )

    result = attendance_sync.run_full_sweep(now_utc=NOW)

    assert result.success is False
    assert backend.sweep_transactions == []


def test_tombstone_recovery_reads_safe_bounded_chunks(install_dependencies):
    ids = tuple(range(1, 252))

    def rows_for(requested):
        return [_row(item, write_date=NOW) for item in requested]

    source, backend = install_dependencies(
        CompleteOdooSource(ids=ids, rows_by_id=rows_for),
        TransactionalMirrorBackend(tombstoned_ids=ids),
    )

    result = attendance_sync.run_full_sweep(now_utc=NOW)

    assert result.success is True
    assert [len(chunk) for chunk in source.rows_by_id_calls] == [250, 1]
    assert len(backend.sweep_transactions[0]["recovery_rows"]) == 251


def test_full_sweep_propagates_exact_committed_deletion_count(
    install_dependencies,
):
    backend = CountingSweepBackend(deleted_count=3)
    install_dependencies(CompleteOdooSource(ids=[901]), backend)

    result = attendance_sync.run_full_sweep(now_utc=NOW)

    assert result.success is True
    assert result.rows_deleted == 3


def test_tick_propagates_exact_committed_deletion_count(install_dependencies):
    backend = CountingSweepBackend(deleted_count=2)
    install_dependencies(CompleteOdooSource(ids=[901]), backend)

    result = attendance_sync.tick(now_utc=NOW)

    assert result.success is True
    assert result.rows_deleted == 2


def test_initial_tick_requires_both_successful_halves_before_baseline(
    install_dependencies,
):
    source, backend = install_dependencies(
        CompleteOdooSource(
            changes=[_row(901, write_date=NOW - timedelta(minutes=1))],
            ids=[901],
        ),
        TransactionalMirrorBackend(),
    )

    result = attendance_sync.tick(now_utc=NOW)

    assert result.success is True
    assert result.incremental_completed is True
    assert result.full_sweep_completed is True
    assert result.baseline_completed is True
    assert backend.state.baseline_completed_at == NOW
    assert len(backend.incremental_transactions) == 1
    assert len(backend.sweep_transactions) == 1
    assert source.open_calls == 1


def test_tick_does_not_mark_baseline_when_full_sweep_fails(
    install_dependencies,
):
    _source, backend = install_dependencies(
        CompleteOdooSource(ids=RuntimeError("sweep failed")),
        TransactionalMirrorBackend(),
    )

    result = attendance_sync.tick(now_utc=NOW)

    assert result.success is False
    assert result.incremental_completed is True
    assert result.full_sweep_completed is False
    assert result.baseline_completed is False
    assert backend.baseline_attempts == []


def test_tick_refreshes_incremental_and_open_every_time_but_sweeps_hourly(
    install_dependencies,
):
    last_sweep = NOW - timedelta(minutes=30)
    backend = TransactionalMirrorBackend(
        attendance_sync.SyncState(
            cursor_write_date=None,
            cursor_id=None,
            last_incremental_completed_at=NOW - timedelta(minutes=1),
            last_full_sweep_completed_at=last_sweep,
            full_sweep_generation=4,
            baseline_completed_at=NOW - timedelta(days=1),
        )
    )
    source, backend = install_dependencies(
        CompleteOdooSource(ids=[901]), backend
    )

    first = attendance_sync.tick(now_utc=NOW)
    backend.state = replace(
        backend.state, last_full_sweep_completed_at=NOW - timedelta(hours=1)
    )
    second = attendance_sync.tick(now_utc=NOW + timedelta(seconds=30))

    assert first.success is True
    assert first.full_sweep_completed is False
    assert second.success is True
    assert second.full_sweep_completed is True
    assert source.open_calls == 2
    assert len(source.change_calls) == 2
    assert source.sweep_calls == 1


def test_concurrent_ticks_recheck_due_state_under_lock_and_sweep_once(
    install_dependencies,
):
    initial_reads = threading.Barrier(2)

    class ConcurrentTickBackend(TransactionalMirrorBackend):
        def sync_state(self):
            if self.lock_depth == 0:
                stale_state = self.state
                initial_reads.wait(timeout=2)
                return stale_state
            return super().sync_state()

    backend = ConcurrentTickBackend(
        attendance_sync.SyncState(
            cursor_write_date=None,
            cursor_id=None,
            last_incremental_completed_at=NOW - timedelta(minutes=1),
            last_full_sweep_completed_at=NOW - timedelta(hours=1),
            full_sweep_generation=4,
            baseline_completed_at=NOW - timedelta(days=1),
        )
    )
    source, _backend = install_dependencies(
        CompleteOdooSource(ids=[901]), backend
    )
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(attendance_sync.tick(now_utc=NOW)))
        for _ in range(2)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert all(result.success for result in results)
    assert source.sweep_calls == 1
    assert sum(result.full_sweep_completed for result in results) == 1
    assert len(backend.sweep_transactions) == 1


def test_post_baseline_result_reports_recalc_days_from_atomic_store(
    install_dependencies,
):
    backend = TransactionalMirrorBackend(
        attendance_sync.SyncState(
            cursor_write_date=None,
            cursor_id=None,
            last_incremental_completed_at=NOW - timedelta(minutes=1),
            last_full_sweep_completed_at=NOW - timedelta(minutes=1),
            full_sweep_generation=2,
            baseline_completed_at=NOW - timedelta(days=1),
        )
    )
    install_dependencies(
        CompleteOdooSource(
            changes=[
                _row(
                    901,
                    write_date=NOW,
                    check_in=datetime(2026, 8, 29, 4, 30, tzinfo=UTC),
                    check_out=datetime(2026, 8, 29, 6, 30, tzinfo=UTC),
                )
            ]
        ),
        backend,
    )

    result = attendance_sync.run_incremental_sync(now_utc=NOW)

    assert result.affected_days == frozenset({date(2026, 8, 28)})


@pytest.mark.parametrize("bad_now", [datetime(2026, 8, 28, 15, 0), "now"])
def test_public_sync_functions_require_aware_datetimes(
    install_dependencies, bad_now
):
    source, backend = install_dependencies(
        CompleteOdooSource(), TransactionalMirrorBackend()
    )

    with pytest.raises(TypeError, match="now_utc must be an aware datetime"):
        attendance_sync.tick(now_utc=bad_now)
    assert source.change_calls == []
    assert backend.started == []
