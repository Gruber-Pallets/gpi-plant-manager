from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
import os

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

    def enqueue(self, candidates, *, now_utc):
        self.candidates = tuple(candidates)
        return len(self.candidates)

    def claim_next(self, *, now_utc):
        self.claim_calls.append(now_utc)
        value, self.claim = self.claim, None
        return value

    def refresh_expected(self, claim, row, *, now_utc):
        self.refreshes.append((claim, row, now_utc))
        return True

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
    def __init__(self, reads, *, write_error: Exception | None = None):
        self.reads = list(reads)
        self.write_error = write_error
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
        ),
    )


def test_enqueue_drops_ambiguous_candidates_instead_of_guessing(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(repair, "_backend", backend)

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


def test_success_writes_only_department_then_rereads_and_mirrors(installed):
    before = _row()
    verified = _row(department_id=8, write_date=NEW_VERSION)
    backend, facade = installed(claim=_claim(), reads=([before], [verified]))

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(901, "repaired", 1, None)
    assert facade.events == [
        ("read", (901,)),
        ("write", 901, 8),
        ("read", (901,)),
    ]
    assert backend.finishes == [(_claim(), verified, NOW)]


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
        assert repair.enqueue_from_spans((span,)) == 1

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
        assert repair.enqueue_from_spans((span,)) == 1

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
