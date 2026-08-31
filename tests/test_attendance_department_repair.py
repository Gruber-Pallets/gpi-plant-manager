from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
import os
from threading import Barrier

import pytest

from zira_dashboard import (
    app as app_module,
    attendance_department_repair as repair,
    attendance_sync,
)
from zira_dashboard.attendance_timeline import LocationSpan


NOW = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)
VERSION = NOW - timedelta(minutes=2)
NEW_VERSION = NOW - timedelta(minutes=1)
DAY = date(2026, 8, 31)


def _span(
    *,
    attendance_id: int = 901,
    status: str = "valid",
    work_center_id: int | None = 72,
    repair_value: tuple[int, int, datetime] | None = (901, 8, VERSION),
) -> LocationSpan:
    return LocationSpan(
        employee_odoo_id=44,
        employee_name="Adrian A.",
        start_utc=NOW - timedelta(hours=2),
        end_utc=NOW,
        status=status,
        app_work_center_name="Repair 1" if status == "valid" else None,
        odoo_work_center_id=work_center_id,
        odoo_work_center_name="Odoo Repair One" if work_center_id else None,
        attendance_ids=(attendance_id,),
        department_repair=repair_value,
    )


def _row(
    *,
    attendance_id: int = 901,
    work_center_id: int | None = 72,
    department_id: int | None = 7,
    write_date: datetime = VERSION,
) -> dict:
    return {
        "odoo_attendance_id": attendance_id,
        "employee_odoo_id": 44,
        "employee_name": "Adrian A.",
        "check_in_utc": NOW - timedelta(hours=2),
        "check_out_utc": None,
        "odoo_work_center_id": work_center_id,
        "odoo_work_center_name": "Odoo Repair One" if work_center_id else None,
        "odoo_department_id": department_id,
        "odoo_department_name": "Old Team" if department_id == 7 else "Recycled",
        "odoo_write_date": write_date,
    }


class FakeBackend:
    def __init__(self, claim=None):
        self.claim = claim
        self.candidates = ()
        self.refreshes = []
        self.discards = []
        self.finishes = []
        self.failures = []
        self.claim_calls = []
        self.renewals = []
        self.renewed = True
        self.observations = ()

    def enqueue(self, candidates, observations=(), *, now_utc):
        self.candidates = tuple(candidates)
        self.observations = tuple(observations)
        return len(self.candidates)

    def claim_next(self, *, now_utc):
        self.claim_calls.append(now_utc)
        value, self.claim = self.claim, None
        return value

    def refresh_expected(self, claim, row, *, now_utc):
        self.refreshes.append((claim, row, now_utc))
        return True

    def renew_claim(self, claim, *, now_utc):
        self.renewals.append((claim, now_utc))
        return self.renewed

    def discard(self, claim, *, now_utc, reason):
        self.discards.append((claim, now_utc, reason))
        return True

    def finish_verified(self, claim, row, *, now_utc):
        self.finishes.append((claim, row, now_utc))
        return True

    def retry_or_fail(self, claim, error, *, now_utc, current_row=None):
        self.failures.append((claim, error, now_utc, current_row))
        return claim.attempt_count >= repair.MAX_ATTEMPTS


class FakeFacade:
    def __init__(
        self,
        reads,
        *,
        write_error: Exception | None = None,
        target_department_id: int | None = 8,
    ):
        self.reads = list(reads)
        self.write_error = write_error
        self.target_department_id = target_department_id
        self.events = []

    def fetch_attendance_rows_by_ids(self, ids):
        self.events.append(("read", tuple(ids)))
        value = self.reads.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def set_attendance_department_id(self, attendance_id, department_id):
        self.events.append(("write", attendance_id, department_id))
        if self.write_error is not None:
            raise self.write_error

    def target_department_id_for_app_work_center(self, _app_work_center_name):
        return self.target_department_id


@pytest.fixture
def installed(monkeypatch):
    def install(*, claim=None, reads=(), write_error=None, live=True):
        backend = FakeBackend(claim)
        facade = FakeFacade(reads, write_error=write_error)
        monkeypatch.setattr(repair, "_backend", backend)
        monkeypatch.setattr(repair, "_facade", facade)
        monkeypatch.setattr(repair, "_live_enabled", lambda *, now_utc: live)
        return backend, facade

    return install


def _claim(*, attempt_count: int = 1):
    return repair.RepairClaim(
        attendance_id=901,
        expected_write_date=VERSION,
        target_department_id=8,
        expected_work_center_id=72,
        mirror_write_date=VERSION,
        attempt_count=attempt_count,
    )


def test_enqueue_keeps_one_valid_candidate_per_attendance_and_expected_version(
    monkeypatch,
):
    backend = FakeBackend()
    monkeypatch.setattr(repair, "_backend", backend)
    monkeypatch.setattr(repair, "_facade", FakeFacade(()))
    real_now_utc = repair._now_utc
    monkeypatch.setattr(
        repair,
        "_now_utc",
        lambda value: NOW if value is None else real_now_utc(value),
    )
    candidate = _span()

    count = repair.enqueue_from_spans(
        (
            candidate,
            candidate,
            _span(status="unmapped_location"),
            _span(repair_value=None),
        ),
    )

    assert count == 1
    assert backend.candidates == (
        repair.RepairCandidate(
            attendance_id=901,
            expected_write_date=VERSION,
            target_department_id=8,
            expected_work_center_id=72,
            target_projected_at=NOW,
        ),
    )


def test_enqueue_drops_ambiguous_candidates_instead_of_guessing(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(repair, "_backend", backend)
    monkeypatch.setattr(repair, "_facade", FakeFacade(()))

    count = repair.enqueue_from_spans(
        (
            _span(),
            _span(
                work_center_id=73,
                repair_value=(901, 9, NEW_VERSION),
            ),
        ),
    )

    assert count == 0
    assert backend.candidates == ()


def test_shadow_mode_never_claims_or_writes(installed):
    backend, facade = installed(claim=_claim(), reads=([_row()],), live=False)

    assert repair.process_next(now_utc=NOW) is None

    assert backend.claim_calls == []
    assert facade.events == []


def test_changed_work_center_is_discarded_before_any_write(installed):
    backend, facade = installed(
        claim=_claim(),
        reads=([_row(work_center_id=73, write_date=NEW_VERSION)],),
    )

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(901, "discarded", 1, None)
    assert facade.events == [("read", (901,))]
    assert backend.discards[0][2] == "work_center_changed"


def test_changed_mirror_projection_discards_without_reading_or_writing(installed):
    claim = repair.RepairClaim(
        attendance_id=901,
        expected_write_date=VERSION,
        target_department_id=8,
        expected_work_center_id=72,
        mirror_write_date=NEW_VERSION,
        attempt_count=1,
    )
    backend, facade = installed(claim=claim, reads=([_row()],))

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(901, "discarded", 1, None)
    assert facade.events == []
    assert backend.discards[0][2] == "projection_changed"


def test_write_date_only_change_refreshes_mirror_and_retries(installed):
    current = _row(write_date=NEW_VERSION)
    backend, facade = installed(claim=_claim(), reads=([current],))

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(901, "version_refreshed", 1, None)
    assert facade.events == [("read", (901,))]
    assert backend.refreshes == [(_claim(), current, NOW)]


def test_already_correct_row_is_verified_and_mirrored_without_write(installed):
    current = _row(department_id=8, write_date=NEW_VERSION)
    backend, facade = installed(claim=_claim(), reads=([current],))

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(901, "already_correct", 1, None)
    assert facade.events == [("read", (901,))]
    assert backend.finishes == [(_claim(), current, NOW)]


def test_success_writes_only_department_then_rereads_and_mirrors(installed, monkeypatch):
    before = _row()
    verified = _row(department_id=8, write_date=NEW_VERSION)
    backend, facade = installed(claim=_claim(), reads=([before], [verified]))
    write_time = NOW + timedelta(seconds=30)
    real_now_utc = repair._now_utc
    monkeypatch.setattr(
        repair,
        "_now_utc",
        lambda value: write_time if value is None else real_now_utc(value),
    )

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(901, "repaired", 1, None)
    assert facade.events == [
        ("read", (901,)),
        ("write", 901, 8),
        ("read", (901,)),
    ]
    assert backend.renewals == [(_claim(), write_time)]
    assert backend.finishes == [(_claim(), verified, NOW)]


def test_lost_claim_is_renewed_and_fenced_before_the_odoo_write(installed):
    backend, facade = installed(claim=_claim(), reads=([_row()],))
    backend.renewed = False

    result = repair.process_next(now_utc=NOW)

    assert result is None
    assert [renewal[0] for renewal in backend.renewals] == [_claim()]
    assert facade.events == [("read", (901,))]


def test_over_budget_recovered_claim_can_fail_but_cannot_write(installed):
    claim = _claim(attempt_count=repair.MAX_ATTEMPTS + 1)
    backend, facade = installed(claim=claim, reads=([_row()],))

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(
        901,
        "failed",
        repair.MAX_ATTEMPTS + 1,
        "Odoo department repair exceeded its maximum attempts",
    )
    assert facade.events == [("read", (901,))]
    assert backend.renewals == []


def test_timeout_is_adopted_only_after_exact_reread(installed):
    before = _row()
    verified = _row(department_id=8, write_date=NEW_VERSION)
    backend, facade = installed(
        claim=_claim(),
        reads=([before], [verified]),
        write_error=TimeoutError("request timed out"),
    )

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(901, "adopted_timeout", 1, None)
    assert backend.finishes == [(_claim(), verified, NOW)]


@pytest.mark.parametrize(
    ("attempt_count", "outcome"),
    ((1, "retrying"), (repair.MAX_ATTEMPTS, "failed")),
)
def test_failed_post_write_verification_retries_then_becomes_visible_failure(
    installed,
    attempt_count,
    outcome,
):
    before = _row()
    still_wrong = _row(write_date=NEW_VERSION)
    claim = _claim(attempt_count=attempt_count)
    backend, facade = installed(claim=claim, reads=([before], [still_wrong]))

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(
        901,
        outcome,
        attempt_count,
        "Odoo department repair verification failed",
    )
    assert backend.failures == [
        (
            claim,
            "Odoo department repair verification failed",
            NOW,
            still_wrong,
        )
    ]


def test_sync_hook_projects_only_after_a_successful_sync(monkeypatch):
    calls = []
    monkeypatch.setattr(
        repair,
        "enqueue_after_successful_sync",
        lambda **kwargs: calls.append(kwargs) or 2,
    )
    success = attendance_sync.SyncResult(
        success=True,
        affected_days=frozenset((DAY,)),
    )
    failure = attendance_sync.SyncResult(success=False, error="source unavailable")

    assert (
        attendance_sync._enqueue_department_repairs_after_sync(
            success,
            now_utc=NOW,
            include_current_day=False,
        )
        == 2
    )
    assert (
        attendance_sync._enqueue_department_repairs_after_sync(
            failure,
            now_utc=NOW,
            include_current_day=True,
        )
        == 0
    )
    assert calls == [
        {
            "affected_days": frozenset((DAY,)),
            "now_utc": NOW,
            "include_current_day": False,
        }
    ]


def test_successful_tick_calls_department_projection_after_mirror_work(monkeypatch):
    calls = []
    state = attendance_sync.SyncState(
        cursor_write_date=VERSION,
        cursor_id=901,
        last_incremental_completed_at=VERSION,
        last_full_sweep_completed_at=VERSION,
        full_sweep_generation=1,
        baseline_completed_at=VERSION,
    )

    class Backend:
        def sync_state(self):
            return state

        def complete_baseline_if_ready(self, _completed_at):
            return True

    monkeypatch.setattr(attendance_sync, "_backend", Backend())
    monkeypatch.setattr(
        attendance_sync,
        "run_incremental_sync",
        lambda **_kwargs: attendance_sync.SyncResult(
            success=True,
            incremental_completed=True,
            affected_days=frozenset((DAY,)),
        ),
    )
    monkeypatch.setattr(
        attendance_sync,
        "_enqueue_department_repairs_after_sync",
        lambda result, **kwargs: calls.append((result, kwargs)) or 1,
    )

    result = attendance_sync.tick(now_utc=NOW)

    assert result.success is True
    assert calls == [
        (
            result,
            {"now_utc": NOW, "include_current_day": False},
        )
    ]


def test_exactly_one_department_repair_warmer_runs_every_15_seconds():
    matches = [
        item
        for item in app_module._WARMERS
        if item[1] is app_module._tick_attendance_department_repairs
    ]

    assert matches == [
        (
            "attendance department repairs",
            app_module._tick_attendance_department_repairs,
            15,
        )
    ]


def test_department_repair_tick_runs_one_blocking_worker_off_event_loop(monkeypatch):
    calls = []
    monkeypatch.setattr(repair, "process_next", lambda: calls.append("next"))

    asyncio.run(app_module._tick_attendance_department_repairs())

    assert calls == ["next"]


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_queue_deduplicates_expected_version_and_reopens_for_new_version(
    monkeypatch,
):
    from zira_dashboard import db

    monkeypatch.setattr(repair, "_backend", repair._PostgresBackend())
    db.bootstrap_schema()
    db.execute("DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s", (901,))
    try:
        assert repair.enqueue_from_spans((_span(),)) == 1
        assert repair.enqueue_from_spans((_span(),)) == 0
        assert (
            repair.enqueue_from_spans(
                (_span(repair_value=(901, 8, NEW_VERSION)),),
            )
            == 1
        )
        assert repair.enqueue_from_spans((_span(),)) == 0
        rows = db.query(
            "SELECT expected_write_date, target_odoo_department_id, status, attempt_count "
            "FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (901,),
        )
        assert rows == [
            {
                "expected_write_date": NEW_VERSION,
                "target_odoo_department_id": 8,
                "status": "pending",
                "attempt_count": 0,
            }
        ]
    finally:
        db.execute(
            "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (901,),
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_worker_verifies_and_upserts_the_repaired_row(monkeypatch):
    from zira_dashboard import attendance_mirror, db

    attendance_id = 902
    before = _row(attendance_id=attendance_id)
    verified = _row(
        attendance_id=attendance_id,
        department_id=8,
        write_date=NEW_VERSION,
    )
    span = _span(
        attendance_id=attendance_id,
        repair_value=(attendance_id, 8, VERSION),
    )
    db.bootstrap_schema()
    db.execute(
        "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    db.execute(
        "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    try:
        attendance_mirror.upsert_rows((before,), sync_completed_at=NOW)
        monkeypatch.setattr(repair, "_backend", repair._PostgresBackend())
        monkeypatch.setattr(repair, "_facade", FakeFacade(([before], [verified])))
        monkeypatch.setattr(repair, "_live_enabled", lambda *, now_utc: True)
        assert (
            repair._enqueue_projected_spans(
                (span,),
                projected_at_utc=NOW + timedelta(seconds=1),
            )
            == 1
        )

        result = repair.process_next(now_utc=NOW)

        assert result == repair.RepairResult(attendance_id, "repaired", 1, None)
        assert db.query(
            "SELECT status, attempt_count, expected_write_date, last_error "
            "FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        ) == [
            {
                "status": "complete",
                "attempt_count": 1,
                "expected_write_date": NEW_VERSION,
                "last_error": None,
            }
        ]
        mirrored = db.query(
            "SELECT odoo_work_center_id, odoo_department_id, odoo_write_date "
            "FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
        assert mirrored == [
            {
                "odoo_work_center_id": 72,
                "odoo_department_id": 8,
                "odoo_write_date": NEW_VERSION,
            }
        ]
    finally:
        db.execute(
            "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
        db.execute(
            "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_worker_stops_after_three_failed_verifications_and_is_visible(
    monkeypatch,
):
    from zira_dashboard import attendance_exceptions, attendance_mirror, db

    attendance_id = 903
    versions = tuple(VERSION + timedelta(seconds=index) for index in range(4))
    rows = tuple(_row(attendance_id=attendance_id, write_date=version) for version in versions)
    span = _span(
        attendance_id=attendance_id,
        repair_value=(attendance_id, 8, versions[0]),
    )
    db.bootstrap_schema()
    db.execute(
        "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    db.execute(
        "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    try:
        attendance_mirror.upsert_rows((rows[0],), sync_completed_at=NOW)
        facade = FakeFacade(
            (
                [rows[0]],
                [rows[1]],
                [rows[1]],
                [rows[2]],
                [rows[2]],
                [rows[3]],
            )
        )
        monkeypatch.setattr(repair, "_backend", repair._PostgresBackend())
        monkeypatch.setattr(repair, "_facade", facade)
        monkeypatch.setattr(repair, "_live_enabled", lambda *, now_utc: True)
        assert (
            repair._enqueue_projected_spans(
                (span,),
                projected_at_utc=NOW + timedelta(seconds=1),
            )
            == 1
        )

        results = tuple(
            repair.process_next(now_utc=NOW + timedelta(seconds=index)) for index in range(3)
        )

        assert [result.outcome for result in results] == [
            "retrying",
            "retrying",
            "failed",
        ]
        assert db.query(
            "SELECT status, attempt_count, last_error "
            "FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        ) == [
            {
                "status": "failed",
                "attempt_count": 3,
                "last_error": "Odoo department repair verification failed",
            }
        ]
        visible = attendance_exceptions._failed_department_repairs(
            NOW - timedelta(hours=3),
            NOW + timedelta(hours=1),
        )
        assert [row["odoo_attendance_id"] for row in visible] == [attendance_id]
    finally:
        db.execute(
            "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
        db.execute(
            "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_concurrent_first_enqueue_is_conflict_safe(monkeypatch):
    from zira_dashboard import attendance_mirror, db

    attendance_id = 979
    before = _row(attendance_id=attendance_id)
    candidate = repair.RepairCandidate(
        attendance_id=attendance_id,
        expected_write_date=VERSION,
        target_department_id=8,
        expected_work_center_id=72,
        target_projected_at=NOW,
    )
    real_cursor = db.cursor
    both_ready_to_insert = Barrier(2)

    @contextmanager
    def synchronized_cursor():
        with real_cursor() as cursor:

            class SyncCursor:
                def __init__(self):
                    self.first_insert = True

                def execute(self, sql, params=None):
                    if self.first_insert and sql.startswith(
                        "INSERT INTO attendance_department_repairs"
                    ):
                        self.first_insert = False
                        both_ready_to_insert.wait(timeout=2)
                    return cursor.execute(sql, params)

                def __getattr__(self, name):
                    return getattr(cursor, name)

            yield SyncCursor()

    db.bootstrap_schema()
    db.execute(
        "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    db.execute(
        "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    try:
        attendance_mirror.upsert_rows((before,), sync_completed_at=NOW)
        monkeypatch.setattr(repair.db, "cursor", synchronized_cursor)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    repair._PostgresBackend().enqueue,
                    (candidate,),
                    now_utc=NOW,
                )
                for _ in range(2)
            ]
            results = [future.result(timeout=3) for future in futures]

        assert sum(results) == 1
        assert db.query(
            "SELECT COUNT(*) AS count FROM attendance_department_repairs "
            "WHERE odoo_attendance_id = %s",
            (attendance_id,),
        ) == [{"count": 1}]
    finally:
        monkeypatch.setattr(repair.db, "cursor", real_cursor)
        db.execute(
            "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
        db.execute(
            "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_reclaimed_old_worker_cannot_renew_or_write(monkeypatch):
    from zira_dashboard import attendance_mirror, db

    attendance_id = 904
    before = _row(attendance_id=attendance_id)
    verified = _row(
        attendance_id=attendance_id,
        department_id=8,
        write_date=NEW_VERSION,
    )
    span = _span(
        attendance_id=attendance_id,
        repair_value=(attendance_id, 8, VERSION),
    )
    backend = repair._PostgresBackend()

    class ReclaimingFacade(FakeFacade):
        def __init__(self):
            super().__init__(([before], [verified]))
            self.reclaimed = None

        def fetch_attendance_rows_by_ids(self, ids):
            rows = super().fetch_attendance_rows_by_ids(ids)
            if self.reclaimed is None:
                self.reclaimed = backend.claim_next(
                    now_utc=NOW + repair._CLAIM_TIMEOUT,
                )
            return rows

    facade = ReclaimingFacade()
    db.bootstrap_schema()
    db.execute(
        "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    db.execute(
        "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    try:
        attendance_mirror.upsert_rows((before,), sync_completed_at=NOW)
        monkeypatch.setattr(repair, "_backend", backend)
        monkeypatch.setattr(repair, "_facade", facade)
        monkeypatch.setattr(repair, "_live_enabled", lambda *, now_utc: True)
        assert repair.enqueue_from_spans((span,)) == 1

        assert repair.process_next(now_utc=NOW) is None

        assert facade.reclaimed is not None
        assert facade.reclaimed.attempt_count == 2
        assert not [event for event in facade.events if event[0] == "write"]
        assert db.query(
            "SELECT status, attempt_count FROM attendance_department_repairs "
            "WHERE odoo_attendance_id = %s",
            (attendance_id,),
        ) == [{"status": "applying", "attempt_count": 2}]
    finally:
        db.execute(
            "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
        db.execute(
            "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_over_budget_stale_claim_fails_without_a_fourth_write(monkeypatch):
    from zira_dashboard import attendance_mirror, db

    attendance_id = 980
    before = _row(attendance_id=attendance_id)
    span = _span(
        attendance_id=attendance_id,
        repair_value=(attendance_id, 8, VERSION),
    )
    facade = FakeFacade(([before],))
    db.bootstrap_schema()
    db.execute(
        "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    db.execute(
        "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    try:
        attendance_mirror.upsert_rows((before,), sync_completed_at=NOW)
        monkeypatch.setattr(repair, "_backend", repair._PostgresBackend())
        monkeypatch.setattr(repair, "_facade", facade)
        monkeypatch.setattr(repair, "_live_enabled", lambda *, now_utc: True)
        assert repair.enqueue_from_spans((span,)) == 1
        db.execute(
            "UPDATE attendance_department_repairs SET status = 'applying', "
            "attempt_count = %s, updated_at = %s WHERE odoo_attendance_id = %s",
            (
                repair.MAX_ATTEMPTS,
                NOW - repair._CLAIM_TIMEOUT,
                attendance_id,
            ),
        )

        result = repair.process_next(now_utc=NOW)

        assert result == repair.RepairResult(
            attendance_id,
            "failed",
            repair.MAX_ATTEMPTS + 1,
            "Odoo department repair exceeded its maximum attempts",
        )
        assert not [event for event in facade.events if event[0] == "write"]
        assert db.query(
            "SELECT status, attempt_count FROM attendance_department_repairs "
            "WHERE odoo_attendance_id = %s",
            (attendance_id,),
        ) == [
            {
                "status": "failed",
                "attempt_count": repair.MAX_ATTEMPTS + 1,
            }
        ]
    finally:
        db.execute(
            "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
        db.execute(
            "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_mirror_advance_after_read_blocks_write_before_enqueue(monkeypatch):
    from zira_dashboard import attendance_mirror, db

    attendance_id = 982
    v2 = _row(attendance_id=attendance_id)
    v3_version = VERSION + timedelta(seconds=1)
    v3 = _row(
        attendance_id=attendance_id,
        department_id=8,
        write_date=v3_version,
    )
    v4 = _row(
        attendance_id=attendance_id,
        department_id=9,
        write_date=VERSION + timedelta(seconds=2),
    )
    v2_span = _span(
        attendance_id=attendance_id,
        repair_value=(attendance_id, 8, VERSION),
    )
    v3_span = _span(
        attendance_id=attendance_id,
        repair_value=(attendance_id, 9, v3_version),
    )
    backend = repair._PostgresBackend()

    class MirrorAdvanceAfterReadFacade(FakeFacade):
        def __init__(self):
            super().__init__(([v2],))
            self.advanced = False

        def fetch_attendance_rows_by_ids(self, ids):
            rows = super().fetch_attendance_rows_by_ids(ids)
            if not self.advanced:
                self.advanced = True
                attendance_mirror.upsert_rows(
                    (v3,),
                    sync_completed_at=NOW + timedelta(seconds=1),
                )
            return rows

    facade = MirrorAdvanceAfterReadFacade()
    db.bootstrap_schema()
    db.execute(
        "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    db.execute(
        "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    try:
        attendance_mirror.upsert_rows((v2,), sync_completed_at=NOW)
        monkeypatch.setattr(repair, "_backend", backend)
        monkeypatch.setattr(repair, "_facade", facade)
        monkeypatch.setattr(repair, "_live_enabled", lambda *, now_utc: True)
        assert (
            repair._enqueue_projected_spans(
                (v2_span,),
                projected_at_utc=NOW,
            )
            == 1
        )

        assert repair.process_next(now_utc=NOW) is None

        assert not [event for event in facade.events if event[0] == "write"]
        assert db.query(
            "SELECT status FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        ) == [{"status": "complete"}]

        assert (
            repair._enqueue_projected_spans(
                (v3_span,),
                projected_at_utc=NOW + timedelta(seconds=1),
            )
            == 1
        )
        next_facade = FakeFacade(([v3], [v4]))
        monkeypatch.setattr(repair, "_facade", next_facade)
        assert repair.process_next(now_utc=NOW + timedelta(seconds=2)) == (
            repair.RepairResult(attendance_id, "repaired", 1, None)
        )
        assert ("write", attendance_id, 9) in next_facade.events
    finally:
        db.execute(
            "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
        db.execute(
            "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_successor_after_read_is_promoted_before_any_stale_write(monkeypatch):
    from zira_dashboard import attendance_mirror, db

    attendance_id = 981
    v2 = _row(attendance_id=attendance_id)
    v3_version = VERSION + timedelta(seconds=1)
    v3 = _row(
        attendance_id=attendance_id,
        department_id=8,
        write_date=v3_version,
    )
    v4 = _row(
        attendance_id=attendance_id,
        department_id=9,
        write_date=VERSION + timedelta(seconds=2),
    )
    v2_span = _span(
        attendance_id=attendance_id,
        repair_value=(attendance_id, 8, VERSION),
    )
    v3_span = _span(
        attendance_id=attendance_id,
        repair_value=(attendance_id, 9, v3_version),
    )
    backend = repair._PostgresBackend()

    class SuccessorAfterReadFacade(FakeFacade):
        def __init__(self):
            super().__init__(([v2],))
            self.enqueued = False

        def fetch_attendance_rows_by_ids(self, ids):
            rows = super().fetch_attendance_rows_by_ids(ids)
            if not self.enqueued:
                self.enqueued = True
                attendance_mirror.upsert_rows(
                    (v3,),
                    sync_completed_at=NOW + timedelta(seconds=1),
                )
                assert repair.enqueue_from_spans((v3_span,)) == 1
            return rows

    facade = SuccessorAfterReadFacade()
    db.bootstrap_schema()
    db.execute(
        "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    db.execute(
        "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    try:
        attendance_mirror.upsert_rows((v2,), sync_completed_at=NOW)
        monkeypatch.setattr(repair, "_backend", backend)
        monkeypatch.setattr(repair, "_facade", facade)
        monkeypatch.setattr(repair, "_live_enabled", lambda *, now_utc: True)
        assert repair.enqueue_from_spans((v2_span,)) == 1

        assert repair.process_next(now_utc=NOW) is None

        assert not [event for event in facade.events if event[0] == "write"]
        assert db.query(
            "SELECT expected_write_date, target_odoo_department_id, status, "
            "attempt_count FROM attendance_department_repairs "
            "WHERE odoo_attendance_id = %s",
            (attendance_id,),
        ) == [
            {
                "expected_write_date": v3_version,
                "target_odoo_department_id": 9,
                "status": "pending",
                "attempt_count": 0,
            }
        ]

        next_facade = FakeFacade(([v3], [v4]))
        monkeypatch.setattr(repair, "_facade", next_facade)
        assert repair.process_next(now_utc=NOW + timedelta(seconds=2)) == (
            repair.RepairResult(attendance_id, "repaired", 1, None)
        )
        assert ("write", attendance_id, 9) in next_facade.events
    finally:
        db.execute(
            "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
        db.execute(
            "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_successor_arriving_before_settlement_is_processed_without_new_sync(
    monkeypatch,
):
    from zira_dashboard import attendance_mirror, db

    attendance_id = 905
    v2 = _row(attendance_id=attendance_id)
    v3_version = VERSION + timedelta(seconds=2)
    v3 = _row(
        attendance_id=attendance_id,
        department_id=8,
        write_date=v3_version,
    )
    v4_after_v2_write = _row(
        attendance_id=attendance_id,
        department_id=8,
        write_date=VERSION + timedelta(seconds=3),
    )
    v5 = _row(
        attendance_id=attendance_id,
        department_id=9,
        write_date=VERSION + timedelta(seconds=4),
    )
    v2_span = _span(
        attendance_id=attendance_id,
        repair_value=(attendance_id, 8, VERSION),
    )
    v3_span = _span(
        attendance_id=attendance_id,
        repair_value=(attendance_id, 9, v3_version),
    )
    backend = repair._PostgresBackend()
    db.bootstrap_schema()
    db.execute(
        "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    db.execute(
        "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    try:
        attendance_mirror.upsert_rows((v2,), sync_completed_at=NOW)
        monkeypatch.setattr(repair, "_backend", backend)
        assert (
            repair._enqueue_projected_spans(
                (v2_span,),
                projected_at_utc=NOW,
            )
            == 1
        )
        claim = backend.claim_next(now_utc=NOW)
        assert claim is not None

        attendance_mirror.upsert_rows((v3,), sync_completed_at=NOW + timedelta(seconds=2))
        assert (
            repair._enqueue_projected_spans(
                (v3_span,),
                projected_at_utc=NOW + timedelta(seconds=2),
            )
            == 1
        )
        assert backend.finish_verified(
            claim,
            v4_after_v2_write,
            now_utc=NOW + timedelta(seconds=3),
        )
        assert db.query(
            "SELECT expected_write_date, target_odoo_department_id, "
            "expected_odoo_work_center_id, status, attempt_count, "
            "successor_expected_write_date "
            "FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        ) == [
            {
                "expected_write_date": v4_after_v2_write["odoo_write_date"],
                "target_odoo_department_id": 9,
                "expected_odoo_work_center_id": 72,
                "status": "pending",
                "attempt_count": 0,
                "successor_expected_write_date": None,
            }
        ]

        facade = FakeFacade(([v4_after_v2_write], [v5]))
        monkeypatch.setattr(repair, "_facade", facade)
        monkeypatch.setattr(repair, "_live_enabled", lambda *, now_utc: True)
        assert repair.process_next(now_utc=NOW + timedelta(seconds=4)) == (
            repair.RepairResult(attendance_id, "repaired", 1, None)
        )
        assert ("write", attendance_id, 9) in facade.events
    finally:
        db.execute(
            "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
        db.execute(
            "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_newer_target_token_rebases_older_applying_successor(monkeypatch):
    from zira_dashboard import attendance_mirror, db

    attendance_id = 985
    v3_version = VERSION + timedelta(seconds=1)
    v4_version = VERSION + timedelta(seconds=2)
    v4 = _row(
        attendance_id=attendance_id,
        department_id=7,
        write_date=v4_version,
    )
    v5 = _row(
        attendance_id=attendance_id,
        department_id=9,
        write_date=VERSION + timedelta(seconds=3),
    )
    active_v4_span = _span(
        attendance_id=attendance_id,
        repair_value=(attendance_id, 8, v4_version),
    )
    delayed_v3_span = _span(
        attendance_id=attendance_id,
        repair_value=(attendance_id, 9, v3_version),
    )
    backend = repair._PostgresBackend()
    db.bootstrap_schema()
    db.execute(
        "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    db.execute(
        "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    try:
        attendance_mirror.upsert_rows((v4,), sync_completed_at=NOW)
        monkeypatch.setattr(repair, "_backend", backend)
        assert (
            repair._enqueue_projected_spans(
                (active_v4_span,),
                projected_at_utc=NOW + timedelta(seconds=1),
            )
            == 1
        )
        claim = backend.claim_next(now_utc=NOW + timedelta(seconds=1))
        assert claim is not None

        assert (
            repair._enqueue_projected_spans(
                (delayed_v3_span,),
                projected_at_utc=NOW + timedelta(seconds=2),
            )
            == 1
        )
        assert (
            backend.renew_claim(
                claim,
                now_utc=NOW + timedelta(seconds=3),
            )
            is False
        )
        assert db.query(
            "SELECT expected_write_date, target_odoo_department_id, status, "
            "attempt_count FROM attendance_department_repairs "
            "WHERE odoo_attendance_id = %s",
            (attendance_id,),
        ) == [
            {
                "expected_write_date": v4_version,
                "target_odoo_department_id": 9,
                "status": "pending",
                "attempt_count": 0,
            }
        ]

        facade = FakeFacade(([v4], [v5]))
        monkeypatch.setattr(repair, "_facade", facade)
        monkeypatch.setattr(repair, "_live_enabled", lambda *, now_utc: True)
        assert repair.process_next(now_utc=NOW + timedelta(seconds=4)) == (
            repair.RepairResult(attendance_id, "repaired", 1, None)
        )
        assert ("write", attendance_id, 9) in facade.events
    finally:
        db.execute(
            "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
        db.execute(
            "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_projected_successor_enqueued_after_settlement_keeps_new_target(
    monkeypatch,
):
    from zira_dashboard import attendance_mirror, db

    attendance_id = 983
    v2 = _row(attendance_id=attendance_id)
    v3_version = VERSION + timedelta(seconds=1)
    v3 = _row(
        attendance_id=attendance_id,
        department_id=8,
        write_date=v3_version,
    )
    v4_after_v2_write = _row(
        attendance_id=attendance_id,
        department_id=8,
        write_date=VERSION + timedelta(seconds=2),
    )
    v5 = _row(
        attendance_id=attendance_id,
        department_id=9,
        write_date=VERSION + timedelta(seconds=3),
    )
    v2_span = _span(
        attendance_id=attendance_id,
        repair_value=(attendance_id, 8, VERSION),
    )
    projected_v3_span = _span(
        attendance_id=attendance_id,
        repair_value=(attendance_id, 9, v3_version),
    )
    backend = repair._PostgresBackend()
    db.bootstrap_schema()
    db.execute(
        "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    db.execute(
        "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    try:
        attendance_mirror.upsert_rows((v2,), sync_completed_at=NOW)
        monkeypatch.setattr(repair, "_backend", backend)
        assert (
            repair._enqueue_projected_spans(
                (v2_span,),
                projected_at_utc=NOW,
            )
            == 1
        )
        claim = backend.claim_next(now_utc=NOW)
        assert claim is not None

        attendance_mirror.upsert_rows(
            (v3,),
            sync_completed_at=NOW + timedelta(seconds=1),
        )
        assert backend.finish_verified(
            claim,
            v4_after_v2_write,
            now_utc=NOW + timedelta(seconds=2),
        )

        assert (
            repair._enqueue_projected_spans(
                (projected_v3_span,),
                projected_at_utc=NOW + timedelta(seconds=1),
            )
            == 1
        )
        assert db.query(
            "SELECT expected_write_date, target_odoo_department_id, "
            "expected_odoo_work_center_id, status, attempt_count "
            "FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        ) == [
            {
                "expected_write_date": v4_after_v2_write["odoo_write_date"],
                "target_odoo_department_id": 9,
                "expected_odoo_work_center_id": 72,
                "status": "pending",
                "attempt_count": 0,
            }
        ]

        facade = FakeFacade(([v4_after_v2_write], [v5]))
        monkeypatch.setattr(repair, "_facade", facade)
        monkeypatch.setattr(repair, "_live_enabled", lambda *, now_utc: True)
        assert repair.process_next(now_utc=NOW + timedelta(seconds=3)) == (
            repair.RepairResult(attendance_id, "repaired", 1, None)
        )
        assert ("write", attendance_id, 9) in facade.events
    finally:
        db.execute(
            "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
        db.execute(
            "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_older_target_observation_cannot_reverse_completed_repair(monkeypatch):
    from zira_dashboard import attendance_mirror, db

    attendance_id = 984
    v3_version = VERSION + timedelta(seconds=1)
    v3 = _row(
        attendance_id=attendance_id,
        department_id=8,
        write_date=v3_version,
    )
    v4 = _row(
        attendance_id=attendance_id,
        department_id=9,
        write_date=VERSION + timedelta(seconds=2),
    )
    v3_span = _span(
        attendance_id=attendance_id,
        repair_value=(attendance_id, 9, v3_version),
    )
    stale_v2_span = _span(
        attendance_id=attendance_id,
        repair_value=(attendance_id, 8, VERSION),
    )
    backend = repair._PostgresBackend()
    db.bootstrap_schema()
    db.execute(
        "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    db.execute(
        "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    try:
        attendance_mirror.upsert_rows((v3,), sync_completed_at=NOW)
        monkeypatch.setattr(repair, "_backend", backend)
        assert (
            repair._enqueue_projected_spans(
                (v3_span,),
                projected_at_utc=NOW + timedelta(seconds=2),
            )
            == 1
        )
        claim = backend.claim_next(now_utc=NOW + timedelta(seconds=2))
        assert claim is not None
        assert backend.finish_verified(
            claim,
            v4,
            now_utc=NOW + timedelta(seconds=3),
        )

        assert (
            repair._enqueue_projected_spans(
                (stale_v2_span,),
                projected_at_utc=NOW + timedelta(seconds=1),
            )
            == 0
        )
        assert db.query(
            "SELECT expected_write_date, target_odoo_department_id, status "
            "FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        ) == [
            {
                "expected_write_date": v4["odoo_write_date"],
                "target_odoo_department_id": 9,
                "status": "complete",
            }
        ]
    finally:
        db.execute(
            "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
        db.execute(
            "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_older_correct_observation_cannot_clear_newer_failed_target(monkeypatch):
    from zira_dashboard import attendance_mirror, db

    attendance_id = 986
    current = _row(
        attendance_id=attendance_id,
        department_id=8,
        write_date=VERSION + timedelta(seconds=2),
    )
    stale_correct_span = _span(
        attendance_id=attendance_id,
        repair_value=None,
    )
    db.bootstrap_schema()
    db.execute(
        "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    db.execute(
        "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    try:
        attendance_mirror.upsert_rows((current,), sync_completed_at=NOW)
        db.execute(
            "INSERT INTO attendance_department_repairs "
            "(odoo_attendance_id, expected_write_date, target_odoo_department_id, "
            "expected_odoo_work_center_id, target_projected_at, status, "
            "attempt_count, updated_at, last_error) "
            "VALUES (%s, %s, 9, 72, %s, 'failed', 3, %s, 'newer failure')",
            (
                attendance_id,
                VERSION,
                NOW + timedelta(seconds=2),
                NOW + timedelta(seconds=2),
            ),
        )
        monkeypatch.setattr(repair, "_backend", repair._PostgresBackend())
        monkeypatch.setattr(repair, "_facade", FakeFacade((), target_department_id=8))

        assert (
            repair._enqueue_projected_spans(
                (stale_correct_span,),
                projected_at_utc=NOW + timedelta(seconds=1),
            )
            == 0
        )
        assert db.query(
            "SELECT status, target_odoo_department_id, last_error "
            "FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        ) == [
            {
                "status": "failed",
                "target_odoo_department_id": 9,
                "last_error": "newer failure",
            }
        ]
    finally:
        db.execute(
            "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
        db.execute(
            "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
@pytest.mark.parametrize(
    ("current_work_center_id", "current_department_id"),
    ((72, 8), (73, 11)),
)
def test_successful_projection_clears_obsolete_failed_exception_without_write(
    monkeypatch,
    current_work_center_id,
    current_department_id,
):
    from zira_dashboard import attendance_exceptions, attendance_mirror, db

    attendance_id = 906 + current_work_center_id
    current_version = VERSION + timedelta(seconds=1)
    current = _row(
        attendance_id=attendance_id,
        work_center_id=current_work_center_id,
        department_id=current_department_id,
        write_date=current_version,
    )
    span = _span(
        attendance_id=attendance_id,
        work_center_id=current_work_center_id,
        repair_value=None,
    )
    facade = FakeFacade((), target_department_id=current_department_id)
    db.bootstrap_schema()
    db.execute(
        "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    db.execute(
        "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
        (attendance_id,),
    )
    try:
        attendance_mirror.upsert_rows((current,), sync_completed_at=NOW)
        db.execute(
            "INSERT INTO attendance_department_repairs "
            "(odoo_attendance_id, expected_write_date, target_odoo_department_id, "
            "expected_odoo_work_center_id, target_projected_at, status, attempt_count, "
            "updated_at, last_error) "
            "VALUES (%s, %s, 8, 72, %s, 'failed', 3, %s, 'old failure')",
            (attendance_id, VERSION, NOW, NOW),
        )
        monkeypatch.setattr(repair, "_backend", repair._PostgresBackend())
        monkeypatch.setattr(repair, "_facade", facade)
        assert [
            row["odoo_attendance_id"]
            for row in attendance_exceptions._failed_department_repairs(
                NOW - timedelta(hours=3),
                NOW + timedelta(hours=1),
            )
        ] == [attendance_id]

        assert (
            repair._enqueue_projected_spans(
                (span,),
                projected_at_utc=NOW + timedelta(seconds=1),
            )
            == 1
        )

        assert db.query(
            "SELECT status, last_error FROM attendance_department_repairs "
            "WHERE odoo_attendance_id = %s",
            (attendance_id,),
        ) == [{"status": "complete", "last_error": None}]
        assert (
            attendance_exceptions._failed_department_repairs(
                NOW - timedelta(hours=3),
                NOW + timedelta(hours=1),
            )
            == ()
        )
        assert facade.events == []
    finally:
        db.execute(
            "DELETE FROM attendance_department_repairs WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
        db.execute(
            "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
