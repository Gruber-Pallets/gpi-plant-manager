from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from zira_dashboard import attendance_sync


NOW = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)


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
    def __init__(self, *, changes=(), open_rows=(), ids=(901,)):
        self.changes = changes
        self.open_rows = open_rows
        self.ids = ids
        self.change_calls: list[dict] = []
        self.open_calls = 0
        self.sweep_calls = 0

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


class TransactionalMirrorBackend:
    def __init__(self, state=None, *, rows_deleted=0):
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
        self.fail_baseline_completion = False
        self.rows_deleted = rows_deleted

    def sync_state(self):
        return self.state

    def record_incremental_started(self, started_at):
        self.started.append(started_at)

    def store_incremental_cycle(
        self, rows, *, cursor_write_date, cursor_id, completed_at
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

    def store_full_sweep(self, ids, *, generation, completed_at):
        self.sweep_transactions.append(
            {
                "ids": set(ids),
                "generation": generation,
                "completed_at": completed_at,
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
        return affected, self.rows_deleted

    def record_failure(self, error):
        self.failures.append(error)

    def complete_baseline_if_ready(self, completed_at):
        self.baseline_attempts.append(completed_at)
        if self.fail_baseline_completion:
            raise RuntimeError("baseline completion failed")
        if (
            self.state.last_incremental_completed_at is not None
            and self.state.last_full_sweep_completed_at is not None
        ):
            self.state = replace(self.state, baseline_completed_at=completed_at)
            return True
        return False


@pytest.fixture
def install_dependencies(monkeypatch):
    def install(source, backend):
        monkeypatch.setattr(attendance_sync, "_source", source)
        monkeypatch.setattr(attendance_sync, "_backend", backend)
        return source, backend

    return install


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


@pytest.mark.parametrize(
    "ids",
    [
        RuntimeError("page two failed"),
        [901, "bad"],
        [901, 901],
        [],
    ],
)
def test_failed_malformed_duplicate_or_empty_sweep_marks_nothing_deleted(
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
        }
    ]
    assert source.sweep_calls == 1


def test_full_sweep_reports_exact_committed_deletion_count(install_dependencies):
    backend = TransactionalMirrorBackend(rows_deleted=3)
    install_dependencies(CompleteOdooSource(ids=[901]), backend)

    result = attendance_sync.run_full_sweep(now_utc=NOW)

    assert result.success is True
    assert result.rows_deleted == 3


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


def test_tick_reports_exact_committed_sweep_deletion_count(install_dependencies):
    backend = TransactionalMirrorBackend(rows_deleted=2)
    install_dependencies(CompleteOdooSource(ids=[901]), backend)

    result = attendance_sync.tick(now_utc=NOW)

    assert result.success is True
    assert result.rows_deleted == 2


def test_tick_keeps_committed_deletion_count_when_baseline_completion_fails(
    install_dependencies,
):
    backend = TransactionalMirrorBackend(rows_deleted=2)
    backend.fail_baseline_completion = True
    install_dependencies(CompleteOdooSource(ids=[901]), backend)

    result = attendance_sync.tick(now_utc=NOW)

    assert result.success is False
    assert result.full_sweep_completed is True
    assert result.rows_deleted == 2
    assert result.error == "baseline completion failed"


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
