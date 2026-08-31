from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
import os
from threading import Barrier
from types import SimpleNamespace

import pytest

from zira_dashboard import (
    app as app_module,
    attendance_department_repair as repair,
    attendance_exceptions,
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
    def __init__(self, claim=None, *, owns=True):
        self.claim = claim
        self.owns_value = owns
        self.candidates = ()
        self.enqueue_calls = 0
        self.refreshes = []
        self.target_refreshes = []
        self.discards = []
        self.finishes = []
        self.failures = []
        self.claim_calls = []
        self.ownership_checks = []
        self.repair_statuses = {}
        self.lease_updated_at = NOW
        self.reservations = []

    def enqueue(self, candidates, *, now_utc):
        self.enqueue_calls += 1
        self.candidates = tuple(candidates)
        return len(self.candidates)

    def reconcile(self, candidates, projection_proofs, *, now_utc):
        self.enqueue_calls += 1
        self.candidates = tuple(candidates)
        candidate_ids = {candidate.attendance_id for candidate in self.candidates}
        proof_ids = {proof.attendance_id for proof in projection_proofs}
        for attendance_id in proof_ids - candidate_ids:
            if attendance_id in self.repair_statuses:
                self.repair_statuses[attendance_id] = "complete"
        return len(self.candidates)

    def claim_next(self, *, now_utc):
        self.claim_calls.append(now_utc)
        value, self.claim = self.claim, None
        return value

    def owns(self, claim):
        self.ownership_checks.append(claim)
        return self.owns_value

    def reserve_for_write(self, claim, *, now_utc):
        self.reservations.append((claim, now_utc))
        if not self.owns_value:
            return False
        self.lease_updated_at = now_utc
        return True

    def refresh_expected(self, claim, row, *, now_utc):
        self.refreshes.append((claim, row, now_utc))
        return self.owns_value

    def refresh_target(self, claim, target_department_id, *, now_utc):
        self.target_refreshes.append((claim, target_department_id, now_utc))
        return self.owns_value

    def discard(self, claim, *, now_utc, reason):
        self.discards.append((claim, now_utc, reason))
        return self.owns_value

    def finish_verified(self, claim, row, *, now_utc):
        self.finishes.append((claim, row, now_utc))
        return self.owns_value

    def retry_or_fail(self, claim, error, *, now_utc, current_row=None):
        self.failures.append((claim, error, now_utc, current_row))
        if not self.owns_value:
            return None
        return claim.attempt_count >= repair.MAX_ATTEMPTS


class FakeFacade:
    def __init__(
        self,
        reads,
        *,
        write_error: Exception | None = None,
        resolved_target: object = 8,
        before_write=None,
    ):
        self.reads = list(reads)
        self.write_error = write_error
        self.resolved_target = resolved_target
        self.before_write = before_write
        self.events = []
        self.resolver_calls = []

    def fetch_attendance_rows_by_ids(self, ids):
        self.events.append(("read", tuple(ids)))
        value = self.reads.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def set_attendance_department_id(self, attendance_id, department_id):
        if self.before_write is not None:
            self.before_write()
        self.events.append(("write_department_only", attendance_id, department_id))
        if self.write_error is not None:
            raise self.write_error

    def target_department_id_for_work_center(self, work_center_id, *, force=False):
        self.resolver_calls.append((work_center_id, force))
        if isinstance(self.resolved_target, BaseException):
            raise self.resolved_target
        return self.resolved_target


@pytest.fixture
def installed(monkeypatch):
    def install(
        *,
        claim=None,
        reads=(),
        write_error=None,
        live=True,
        owns=True,
        resolved_target=8,
        before_write=None,
    ):
        backend = FakeBackend(claim, owns=owns)
        facade = FakeFacade(
            reads,
            write_error=write_error,
            resolved_target=resolved_target,
            before_write=before_write,
        )
        monkeypatch.setattr(repair, "_backend", backend)
        monkeypatch.setattr(repair, "_facade", facade)
        monkeypatch.setattr(repair, "_live_enabled", lambda *, now_utc: live)
        monkeypatch.setattr(repair, "_wall_clock_utc", lambda: NOW, raising=False)
        return backend, facade

    return install


def _claim(*, attempt_count: int = 1, mirror_write_date: datetime = VERSION):
    return repair.RepairClaim(
        attendance_id=901,
        expected_write_date=VERSION,
        target_department_id=8,
        expected_work_center_id=72,
        mirror_write_date=mirror_write_date,
        attempt_count=attempt_count,
    )


def test_enqueue_collapses_adjacent_spans_to_one_candidate(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(repair, "_backend", backend)
    monkeypatch.setattr(repair, "_now_utc", lambda _value: NOW)

    count = repair.enqueue_from_spans(
        (
            _span(),
            _span(),
            _span(status="unmapped_location", repair_value=None),
        ),
    )

    assert count == 1
    assert backend.candidates == (repair.RepairCandidate(901, VERSION, 8, 72, NOW),)


def test_enqueue_conflicting_same_id_candidates_fails_before_database_write(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(repair, "_backend", backend)

    count = repair.enqueue_from_spans(
        (
            _span(),
            _span(work_center_id=73, repair_value=(901, 9, NEW_VERSION)),
        )
    )

    assert count == 0
    assert backend.enqueue_calls == 0


def test_successful_no_candidate_projection_clears_only_covered_obsolete_failure(
    monkeypatch,
):
    backend = FakeBackend()
    backend.repair_statuses = {901: "failed", 902: "failed"}
    monkeypatch.setattr(repair, "_backend", backend)
    monkeypatch.setattr(
        repair,
        "_projection_rows",
        lambda **_kwargs: (_row(work_center_id=73, department_id=8),),
    )
    monkeypatch.setattr(
        repair.work_centers_store,
        "app_work_center_name_for_odoo_id",
        lambda value: "Repair 1" if value == 73 else None,
    )
    monkeypatch.setattr(
        repair.work_centers_store,
        "odoo_work_center_id_for",
        lambda value: 73 if value == "Repair 1" else None,
    )
    monkeypatch.setattr(
        repair.attendance_mirror,
        "health_snapshot",
        lambda: SimpleNamespace(
            last_incremental_completed_at=NOW,
            baseline_completed_at=NOW,
        ),
    )
    monkeypatch.setattr(
        repair._facade,
        "target_department_id_for_work_center",
        lambda _wc_id, *, force=False: 8,
    )
    monkeypatch.setattr(
        attendance_exceptions.db,
        "query",
        lambda *_args, **_kwargs: (
            [{"odoo_attendance_id": attendance_id}]
            if (attendance_id := 901) and backend.repair_statuses[attendance_id] == "failed"
            else []
        ),
    )

    assert (
        repair.enqueue_after_successful_sync(
            affected_days=(DAY,),
            now_utc=NOW,
            include_current_day=False,
        )
        == 0
    )

    assert backend.repair_statuses == {901: "complete", 902: "failed"}
    assert (
        attendance_exceptions._failed_department_repairs(
            NOW - timedelta(hours=3), NOW + timedelta(hours=1)
        )
        == ()
    )


def test_projection_failure_does_not_clear_any_failed_intent(monkeypatch):
    backend = FakeBackend()
    backend.repair_statuses = {901: "failed"}
    monkeypatch.setattr(repair, "_backend", backend)
    monkeypatch.setattr(repair, "_projection_rows", lambda **_kwargs: (_row(),))
    monkeypatch.setattr(
        repair.attendance_timeline,
        "project_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("resolver unavailable")),
    )
    monkeypatch.setattr(
        repair.attendance_mirror,
        "health_snapshot",
        lambda: SimpleNamespace(
            last_incremental_completed_at=NOW,
            baseline_completed_at=NOW,
        ),
    )

    with pytest.raises(RuntimeError, match="resolver unavailable"):
        repair.enqueue_after_successful_sync(
            affected_days=(DAY,),
            now_utc=NOW,
            include_current_day=False,
        )

    assert backend.repair_statuses == {901: "failed"}
    assert backend.enqueue_calls == 0


class _OutOfOrderDiscoveryBackend:
    def __init__(self, *, current_row, queued_version, queued_target):
        self.current_row = current_row
        self.queue = {
            "status": "pending",
            "expected_write_date": queued_version,
            "target_department_id": queued_target,
        }

    def reconcile(self, candidates, projection_proofs, *, now_utc):
        del now_utc
        candidates = tuple(candidates)
        proofs = tuple(projection_proofs)
        # The old contract supplied only bare IDs, which cannot fence an
        # out-of-order projection against the now-current mirror version.
        if proofs and isinstance(proofs[0], int):
            matching_ids = set(proofs)
        else:
            matching_ids = {
                proof.attendance_id
                for proof in proofs
                if proof.write_date == self.current_row["odoo_write_date"]
                and proof.work_center_id == self.current_row["odoo_work_center_id"]
                and proof.department_id == self.current_row["odoo_department_id"]
            }
        candidates_by_id = {
            candidate.attendance_id: candidate
            for candidate in candidates
            if candidate.attendance_id in matching_ids
        }
        for attendance_id in matching_ids:
            candidate = candidates_by_id.get(attendance_id)
            if candidate is None:
                self.queue["status"] = "complete"
            else:
                self.queue = {
                    "status": "pending",
                    "expected_write_date": candidate.expected_write_date,
                    "target_department_id": candidate.target_department_id,
                }
        return len(candidates_by_id)


def _install_out_of_order_projection(monkeypatch, *, backend, snapshot, target):
    monkeypatch.setattr(repair, "_backend", backend)
    monkeypatch.setattr(repair, "_projection_rows", lambda **_kwargs: (snapshot,))
    monkeypatch.setattr(
        repair.work_centers_store,
        "app_work_center_name_for_odoo_id",
        lambda value: "Repair 1" if value == 72 else None,
    )
    monkeypatch.setattr(
        repair.work_centers_store,
        "odoo_work_center_id_for",
        lambda value: 72 if value == "Repair 1" else None,
    )
    monkeypatch.setattr(
        repair.attendance_mirror,
        "health_snapshot",
        lambda: SimpleNamespace(
            last_incremental_completed_at=NOW,
            baseline_completed_at=NOW,
        ),
    )
    monkeypatch.setattr(
        repair._facade,
        "target_department_id_for_work_center",
        lambda _wc_id, *, force=False: target,
    )


def test_older_candidate_discovery_cannot_replace_newer_queued_version(monkeypatch):
    current = _row(write_date=NEW_VERSION, department_id=7)
    backend = _OutOfOrderDiscoveryBackend(
        current_row=current,
        queued_version=NEW_VERSION,
        queued_target=9,
    )
    _install_out_of_order_projection(
        monkeypatch,
        backend=backend,
        snapshot=_row(write_date=VERSION, department_id=7),
        target=8,
    )

    repair.enqueue_after_successful_sync(
        affected_days=(DAY,),
        now_utc=NOW,
        include_current_day=False,
    )

    assert backend.queue == {
        "status": "pending",
        "expected_write_date": NEW_VERSION,
        "target_department_id": 9,
    }


def test_older_no_candidate_discovery_cannot_complete_newer_intent(monkeypatch):
    current = _row(write_date=NEW_VERSION, department_id=7)
    backend = _OutOfOrderDiscoveryBackend(
        current_row=current,
        queued_version=NEW_VERSION,
        queued_target=9,
    )
    _install_out_of_order_projection(
        monkeypatch,
        backend=backend,
        snapshot=_row(write_date=VERSION, department_id=8),
        target=8,
    )

    repair.enqueue_after_successful_sync(
        affected_days=(DAY,),
        now_utc=NOW,
        include_current_day=False,
    )

    assert backend.queue == {
        "status": "pending",
        "expected_write_date": NEW_VERSION,
        "target_department_id": 9,
    }


@pytest.mark.parametrize("unavailable_target", [None, 0])
def test_unavailable_force_fresh_projection_target_never_clears_failed_intent(
    monkeypatch,
    unavailable_target,
):
    backend = FakeBackend()
    backend.repair_statuses = {901: "failed"}
    monkeypatch.setattr(repair, "_backend", backend)
    monkeypatch.setattr(repair, "_projection_rows", lambda **_kwargs: (_row(),))
    monkeypatch.setattr(
        repair.work_centers_store,
        "app_work_center_name_for_odoo_id",
        lambda value: "Repair 1" if value == 72 else None,
    )
    monkeypatch.setattr(
        repair.work_centers_store,
        "odoo_work_center_id_for",
        lambda value: 72 if value == "Repair 1" else None,
    )
    monkeypatch.setattr(
        repair.attendance_mirror,
        "health_snapshot",
        lambda: SimpleNamespace(
            last_incremental_completed_at=NOW,
            baseline_completed_at=NOW,
        ),
    )
    monkeypatch.setattr(
        repair._facade,
        "target_department_id_for_work_center",
        lambda _wc_id, *, force=False: unavailable_target,
    )

    with pytest.raises((RuntimeError, ValueError, TypeError)):
        repair.enqueue_after_successful_sync(
            affected_days=(DAY,),
            now_utc=NOW,
            include_current_day=False,
        )

    assert backend.repair_statuses == {901: "failed"}
    assert backend.enqueue_calls == 0


@pytest.mark.parametrize(
    "repair_value",
    [
        (0, 8, VERSION),
        (901, 0, VERSION),
        (901, 8, VERSION.replace(tzinfo=None)),
    ],
)
def test_enqueue_rejects_invalid_ids_and_naive_versions(monkeypatch, repair_value):
    backend = FakeBackend()
    monkeypatch.setattr(repair, "_backend", backend)

    with pytest.raises((TypeError, ValueError)):
        repair.enqueue_from_spans((_span(repair_value=repair_value),))

    assert backend.enqueue_calls == 0


def test_shadow_mode_never_claims_or_writes(installed):
    backend, facade = installed(claim=_claim(), reads=([_row()],), live=False)

    assert repair.process_next(now_utc=NOW) is None
    assert backend.claim_calls == []
    assert facade.events == []


def test_changed_work_center_discards_without_write_or_supersede(installed):
    backend, facade = installed(
        claim=_claim(),
        reads=([_row(work_center_id=73, write_date=NEW_VERSION)],),
    )

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(901, "discarded", 1, None)
    assert facade.events == [("read", (901,))]
    assert backend.discards[0][2] == "work_center_changed"
    assert backend.refreshes == []


def test_changed_local_projection_discards_before_remote_read(installed):
    backend, facade = installed(
        claim=_claim(mirror_write_date=NEW_VERSION),
        reads=([_row()],),
    )

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(901, "discarded", 1, None)
    assert facade.events == []
    assert backend.discards[0][2] == "projection_changed"


def test_same_work_center_version_drift_refreshes_and_requeues(installed):
    current = _row(write_date=NEW_VERSION)
    backend, facade = installed(claim=_claim(), reads=([current],))

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(901, "version_refreshed", 1, None)
    assert facade.events == [("read", (901,))]
    assert backend.refreshes == [(_claim(), current, NOW)]


def test_same_work_center_version_drift_stops_after_bounded_attempts(installed):
    claim = _claim(attempt_count=repair.MAX_ATTEMPTS)
    backend, facade = installed(claim=claim, reads=([_row(write_date=NEW_VERSION)],))

    result = repair.process_next(now_utc=NOW)

    assert result.outcome == "failed"
    assert "kept changing" in result.error
    assert facade.events == [("read", (901,))]
    assert backend.refreshes == []


def test_force_fresh_changed_target_requeues_before_any_write(installed):
    backend, facade = installed(
        claim=_claim(),
        reads=([_row()], [_row()]),
        resolved_target=9,
    )

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(901, "target_refreshed", 1, None)
    assert facade.events == [("read", (901,))]
    assert facade.resolver_calls == [(72, True)]
    assert backend.target_refreshes == [(_claim(), 9, NOW)]


@pytest.mark.parametrize(
    "unavailable",
    [None, RuntimeError("department resolver unavailable")],
)
def test_force_fresh_unavailable_target_retries_without_write(installed, unavailable):
    backend, facade = installed(
        claim=_claim(),
        reads=([_row()], [_row()]),
        resolved_target=unavailable,
    )

    result = repair.process_next(now_utc=NOW)

    assert result.outcome == "retrying"
    assert facade.events == [("read", (901,))]
    assert facade.resolver_calls == [(72, True)]
    assert backend.target_refreshes == []


def test_correct_department_is_adopted_without_write(installed):
    current = _row(department_id=8)
    backend, facade = installed(claim=_claim(), reads=([current],))

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(901, "already_correct", 1, None)
    assert facade.events == [("read", (901,))]
    assert backend.finishes == [(_claim(), current, NOW)]


def test_write_is_department_only_and_second_read_verifies_exact_row(installed):
    verified = _row(department_id=8, write_date=NEW_VERSION)
    backend, facade = installed(claim=_claim(), reads=([_row()], [verified]))

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(901, "repaired", 1, None)
    assert facade.events == [
        ("read", (901,)),
        ("write_department_only", 901, 8),
        ("read", (901,)),
    ]
    assert backend.reservations == [(_claim(), NOW)]
    assert backend.finishes == [(_claim(), verified, NOW)]


def test_prewrite_reservation_uses_side_effect_wall_clock(installed, monkeypatch):
    reserved_at = NOW + timedelta(seconds=30)
    verified = _row(department_id=8, write_date=NEW_VERSION)
    backend, _facade = installed(claim=_claim(), reads=([_row()], [verified]))
    monkeypatch.setattr(repair, "_wall_clock_utc", lambda: reserved_at)

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(901, "repaired", 1, None)
    assert backend.reservations == [(_claim(), reserved_at)]


def test_stale_worker_is_fenced_immediately_before_write(installed):
    backend, facade = installed(claim=_claim(), reads=([_row()],), owns=False)

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(901, "superseded", 1, None)
    assert facade.events == [("read", (901,))]
    assert backend.finishes == []
    assert backend.failures == []


def test_near_expiry_worker_renews_lease_before_reclaimer_can_claim(installed):
    reclaimed = []
    backend_holder = {}

    def reclaim_after_old_lease_would_expire():
        backend = backend_holder["backend"]
        reclaim_at = NOW + timedelta(seconds=2)
        if reclaim_at - backend.lease_updated_at >= timedelta(minutes=15):
            reclaimed.append("reclaimed")

    backend, facade = installed(
        claim=_claim(),
        reads=([_row()], [_row(department_id=8, write_date=NEW_VERSION)]),
        before_write=reclaim_after_old_lease_would_expire,
    )
    backend_holder["backend"] = backend
    backend.lease_updated_at = NOW - timedelta(minutes=14, seconds=59)

    result = repair.process_next(now_utc=NOW)

    assert result.outcome == "repaired"
    assert backend.reservations == [(_claim(), NOW)]
    assert reclaimed == []
    assert facade.events.count(("write_department_only", 901, 8)) == 1


def test_timeout_is_adopted_after_exact_reread_without_second_write(installed):
    verified = _row(department_id=8, write_date=NEW_VERSION)
    backend, facade = installed(
        claim=_claim(),
        reads=([_row()], [verified]),
        write_error=TimeoutError("request timed out"),
    )

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(901, "adopted_timeout", 1, None)
    assert facade.events.count(("write_department_only", 901, 8)) == 1
    assert backend.finishes == [(_claim(), verified, NOW)]


@pytest.mark.parametrize(
    "bad_read",
    [
        [],
        [_row(), _row()],
        [_row(attendance_id=999)],
        [{"odoo_attendance_id": 901}],
    ],
)
def test_missing_duplicate_unrelated_or_malformed_read_retries(installed, bad_read):
    backend, facade = installed(claim=_claim(), reads=(bad_read,))

    result = repair.process_next(now_utc=NOW)

    assert result.outcome == "retrying"
    assert facade.events == [("read", (901,))]
    assert len(backend.failures) == 1
    assert len(backend.failures[0][1]) <= 500


@pytest.mark.parametrize(
    ("verified", "expected_outcome"),
    [
        (_row(attendance_id=999, department_id=8, write_date=NEW_VERSION), "retrying"),
        (_row(work_center_id=73, department_id=8, write_date=NEW_VERSION), "discarded"),
        (_row(department_id=7, write_date=NEW_VERSION), "retrying"),
    ],
)
def test_failed_exact_second_read_never_reports_success(
    installed,
    verified,
    expected_outcome,
):
    backend, _facade = installed(claim=_claim(), reads=([_row()], [verified]))

    result = repair.process_next(now_utc=NOW)

    assert result.outcome == expected_outcome
    assert backend.finishes == []


def test_terminal_failure_after_bounded_attempts(installed):
    claim = _claim(attempt_count=repair.MAX_ATTEMPTS)
    backend, _facade = installed(
        claim=claim,
        reads=([_row()], [_row(write_date=NEW_VERSION)]),
    )

    result = repair.process_next(now_utc=NOW)

    assert result == repair.RepairResult(
        901,
        "failed",
        repair.MAX_ATTEMPTS,
        "Odoo department repair verification failed",
    )
    assert backend.failures[0][0] == claim


def test_discovery_uses_explicit_mapping_policy_and_force_fresh_department(monkeypatch):
    events = []
    raw_rows = (_row(),)
    projected = (_span(),)
    monkeypatch.setattr(repair, "_projection_rows", lambda **kwargs: raw_rows)
    monkeypatch.setattr(
        repair.attendance_timeline,
        "project_rows",
        lambda rows, **kwargs: events.append((rows, kwargs)) or projected,
    )
    monkeypatch.setattr(
        repair.work_centers_store,
        "app_work_center_name_for_odoo_id",
        lambda value: "Repair 1" if value == 72 else None,
    )
    monkeypatch.setattr(
        repair.work_centers_store,
        "odoo_work_center_id_for",
        lambda value: 72 if value == "Repair 1" else None,
    )
    monkeypatch.setattr(
        repair.attendance_location_policy,
        "department_requires_work_center",
        lambda name: name != "Maintenance",
    )
    resolver_calls = []
    monkeypatch.setattr(
        repair._facade,
        "target_department_id_for_work_center",
        lambda wc_id, *, force=False: resolver_calls.append((wc_id, force)) or 8,
    )
    monkeypatch.setattr(
        repair.attendance_mirror,
        "health_snapshot",
        lambda: SimpleNamespace(
            last_incremental_completed_at=NOW,
            baseline_completed_at=NOW,
        ),
    )
    backend = FakeBackend()
    monkeypatch.setattr(repair, "_backend", backend)

    count = repair.enqueue_after_successful_sync(
        affected_days=frozenset((DAY,)),
        now_utc=NOW,
        include_current_day=False,
        include_baseline=False,
    )

    assert count == 1
    assert events[0][0] == raw_rows
    expected_department = events[0][1]["expected_department_id"]
    assert expected_department("Repair 1") == 8
    assert resolver_calls == [(72, True)]


def test_sync_discovery_only_runs_after_committed_success_and_cannot_relabel_it(monkeypatch):
    events = []

    class RunBackend:
        def sync_state(self):
            return attendance_sync.SyncState(None, None, None, None, 0, None)

        def record_incremental_started(self, _started):
            events.append("started")

        def store_incremental_cycle(self, *_args, **_kwargs):
            events.append("stored")
            return {DAY}

    class Backend:
        def logical_run(self):
            class LogicalRun:
                def __enter__(self):
                    events.append("begin")
                    return RunBackend()

                def __exit__(self, *_args):
                    events.append("commit")

            return LogicalRun()

        def record_failure(self, *_args):
            raise AssertionError("discovery must not relabel the successful mirror sync")

    monkeypatch.setattr(attendance_sync, "_backend", Backend())
    monkeypatch.setattr(
        attendance_sync,
        "_source",
        SimpleNamespace(
            fetch_attendance_changes=lambda **_kwargs: [],
            fetch_open_attendance_rows=lambda: [],
        ),
    )
    monkeypatch.setattr(
        attendance_sync,
        "_enqueue_department_repairs_after_sync",
        lambda *_args, **_kwargs: (
            events.append("discover") or (_ for _ in ()).throw(RuntimeError("resolver down"))
        ),
    )

    result = attendance_sync.run_incremental_sync(now_utc=NOW)

    assert result.success is True
    assert events == ["begin", "started", "stored", "commit", "discover"]


def test_historical_write_date_only_observation_reaches_committed_discovery(monkeypatch):
    discoveries = []
    historical = _row(write_date=NEW_VERSION)
    historical["check_out_utc"] = NOW - timedelta(hours=1)

    class RunBackend:
        def sync_state(self):
            return attendance_sync.SyncState(VERSION, 901, VERSION, VERSION, 1, VERSION)

        def record_incremental_started(self, _started):
            return None

        def store_incremental_cycle(self, *_args, **_kwargs):
            # Source version alone is intentionally not a recalculation day.
            return set()

    class Backend:
        @contextmanager
        def logical_run(self):
            yield RunBackend()

        def record_failure(self, *_args):
            raise AssertionError("successful observation must not record failure")

    monkeypatch.setattr(attendance_sync, "_backend", Backend())
    monkeypatch.setattr(
        attendance_sync,
        "_source",
        SimpleNamespace(
            fetch_attendance_changes=lambda **_kwargs: [historical],
            fetch_open_attendance_rows=lambda: [],
        ),
    )
    monkeypatch.setattr(
        attendance_sync,
        "_enqueue_department_repairs_after_sync",
        lambda result, **_kwargs: discoveries.append(result),
    )

    result = attendance_sync.run_incremental_sync(now_utc=NOW)

    assert result.affected_days == frozenset()
    assert result.repair_attendance_ids == frozenset({901})
    assert discoveries[0].repair_attendance_ids == frozenset({901})


def test_baseline_completion_requests_all_committed_rows_for_discovery(monkeypatch):
    calls = []
    monkeypatch.setattr(
        repair,
        "enqueue_after_successful_sync",
        lambda **kwargs: calls.append(kwargs) or 0,
    )

    result = attendance_sync.SyncResult(success=True, baseline_completed=True)
    attendance_sync._enqueue_department_repairs_after_sync(
        result,
        now_utc=NOW,
        include_current_day=False,
    )

    assert calls == [
        {
            "affected_days": frozenset(),
            "attendance_ids": frozenset(),
            "now_utc": NOW,
            "include_current_day": False,
            "include_baseline": True,
        }
    ]


def test_baseline_scan_filters_zero_duration_rows_and_keeps_valid_mismatch(monkeypatch):
    zero = _row(attendance_id=900)
    zero["check_out_utc"] = zero["check_in_utc"]
    valid = _row()
    sql_calls = []

    def mirror_query(sql, *_args):
        sql_calls.append(" ".join(sql.split()))
        rows = (zero, valid)
        if "check_out_utc IS NULL OR check_out_utc > check_in_utc" in sql:
            rows = tuple(
                row
                for row in rows
                if row["check_out_utc"] is None or row["check_out_utc"] > row["check_in_utc"]
            )
        return list(rows)

    backend = FakeBackend()
    monkeypatch.setattr(repair, "_backend", backend)
    monkeypatch.setattr(repair.db, "query", mirror_query)
    monkeypatch.setattr(
        repair.work_centers_store,
        "app_work_center_name_for_odoo_id",
        lambda value: "Repair 1" if value == 72 else None,
    )
    monkeypatch.setattr(
        repair.work_centers_store,
        "odoo_work_center_id_for",
        lambda value: 72 if value == "Repair 1" else None,
    )
    monkeypatch.setattr(
        repair.attendance_mirror,
        "health_snapshot",
        lambda: SimpleNamespace(
            last_incremental_completed_at=NOW,
            baseline_completed_at=NOW,
        ),
    )
    monkeypatch.setattr(
        repair._facade,
        "target_department_id_for_work_center",
        lambda _wc_id, *, force=False: 8,
    )

    assert (
        repair.enqueue_after_successful_sync(
            affected_days=(),
            now_utc=NOW,
            include_current_day=False,
            include_baseline=True,
        )
        == 1
    )
    assert backend.candidates == (repair.RepairCandidate(901, VERSION, 8, 72, NOW),)
    assert "check_out_utc IS NULL OR check_out_utc > check_in_utc" in sql_calls[0]


def test_exact_observation_scan_uses_the_same_active_row_predicate(monkeypatch):
    zero = _row(attendance_id=900)
    zero["check_out_utc"] = zero["check_in_utc"]
    valid = _row()
    sql_calls = []

    def mirror_query(sql, *_args):
        sql_calls.append(" ".join(sql.split()))
        rows = (zero, valid)
        if "check_out_utc IS NULL OR check_out_utc > check_in_utc" in sql:
            rows = tuple(
                row
                for row in rows
                if row["check_out_utc"] is None or row["check_out_utc"] > row["check_in_utc"]
            )
        return list(rows)

    monkeypatch.setattr(repair.db, "query", mirror_query)

    rows = repair._projection_rows(
        affected_days=(),
        attendance_ids=(900, 901),
        now_utc=NOW,
        include_current_day=False,
        include_baseline=False,
    )

    assert [row["odoo_attendance_id"] for row in rows] == [901]
    assert "check_out_utc IS NULL OR check_out_utc > check_in_utc" in sql_calls[0]


def test_successful_hourly_sweep_retries_full_baseline_discovery(monkeypatch):
    discoveries = []
    state = attendance_sync.SyncState(VERSION, 901, VERSION, VERSION, 1, VERSION)

    class RunBackend:
        def sync_state(self):
            return state

        def active_attendance_ids(self):
            return {901}

        def tombstoned_attendance_ids(self, _ids):
            return set()

        def store_full_sweep(self, *_args, **_kwargs):
            return attendance_sync.SweepStoreResult(frozenset(), 0)

    class Backend:
        @contextmanager
        def logical_run(self):
            yield RunBackend()

        def record_failure(self, *_args):
            raise AssertionError("successful sweep must not record failure")

    monkeypatch.setattr(attendance_sync, "_backend", Backend())
    monkeypatch.setattr(
        attendance_sync,
        "_source",
        SimpleNamespace(
            fetch_complete_attendance_id_sweep=lambda: attendance_sync.AttendanceIdSweepSnapshot(
                (901,), True
            )
        ),
    )
    monkeypatch.setattr(
        attendance_sync,
        "_enqueue_department_repairs_after_sync",
        lambda result, **_kwargs: discoveries.append(result),
    )

    result = attendance_sync.run_full_sweep(now_utc=NOW)

    assert result.baseline_completed is True
    assert discoveries == [result]


def test_exactly_one_department_repair_warmer_runs_every_15_seconds():
    matches = [
        item
        for item in app_module._WARMERS
        if item[1] is app_module._tick_attendance_department_repair
    ]

    assert matches == [
        (
            "attendance department repair",
            app_module._tick_attendance_department_repair,
            15,
        )
    ]


def test_department_repair_tick_processes_one_job_off_event_loop(monkeypatch):
    calls = []
    monkeypatch.setattr(repair, "process_next", lambda: calls.append("next"))

    asyncio.run(app_module._tick_attendance_department_repair())

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
                    now_utc=NOW + repair._LEASE,
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

        assert repair.process_next(now_utc=NOW) == repair.RepairResult(
            attendance_id, "superseded", 1, None
        )

        assert facade.reclaimed is not None
        assert facade.reclaimed.attempt_count == 2
        assert not [event for event in facade.events if event[0] == "write_department_only"]
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
                NOW - repair._LEASE,
                attendance_id,
            ),
        )

        result = repair.process_next(now_utc=NOW)

        assert result is None
        assert not [event for event in facade.events if event[0] == "write_department_only"]
        assert db.query(
            "SELECT status, attempt_count FROM attendance_department_repairs "
            "WHERE odoo_attendance_id = %s",
            (attendance_id,),
        ) == [
            {
                "status": "failed",
                "attempt_count": repair.MAX_ATTEMPTS,
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
def test_postgres_over_budget_stale_claim_promotes_newer_successor(monkeypatch):
    from zira_dashboard import attendance_mirror, db

    attendance_id = 987
    v2 = _row(attendance_id=attendance_id)
    v3_version = VERSION + timedelta(seconds=1)
    v3 = _row(attendance_id=attendance_id, write_date=v3_version)
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
        assert repair._enqueue_projected_spans((v2_span,), projected_at_utc=NOW) == 1
        db.execute(
            "UPDATE attendance_department_repairs SET status = 'applying', "
            "attempt_count = %s, updated_at = %s WHERE odoo_attendance_id = %s",
            (repair.MAX_ATTEMPTS, NOW - repair._LEASE, attendance_id),
        )
        attendance_mirror.upsert_rows(
            (v3,),
            sync_completed_at=NOW + timedelta(seconds=1),
        )
        assert (
            repair._enqueue_projected_spans(
                (v3_span,),
                projected_at_utc=NOW + timedelta(seconds=1),
            )
            == 1
        )

        claim = backend.claim_next(now_utc=NOW)

        assert claim is not None
        assert claim.expected_write_date == v3_version
        assert claim.target_department_id == 9
        assert claim.expected_work_center_id == 72
        assert claim.attempt_count == 1
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

        assert repair.process_next(now_utc=NOW) == repair.RepairResult(
            attendance_id, "superseded", 1, None
        )

        assert not [event for event in facade.events if event[0] == "write_department_only"]
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
        next_facade = FakeFacade(([v3], [v4]), resolved_target=9)
        monkeypatch.setattr(repair, "_facade", next_facade)
        assert repair.process_next(now_utc=NOW + timedelta(seconds=2)) == (
            repair.RepairResult(attendance_id, "repaired", 1, None)
        )
        assert ("write_department_only", attendance_id, 9) in next_facade.events
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

        assert repair.process_next(now_utc=NOW) == repair.RepairResult(
            attendance_id, "superseded", 1, None
        )

        assert not [event for event in facade.events if event[0] == "write_department_only"]
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

        next_facade = FakeFacade(([v3], [v4]), resolved_target=9)
        monkeypatch.setattr(repair, "_facade", next_facade)
        assert repair.process_next(now_utc=NOW + timedelta(seconds=2)) == (
            repair.RepairResult(attendance_id, "repaired", 1, None)
        )
        assert ("write_department_only", attendance_id, 9) in next_facade.events
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

        facade = FakeFacade(([v4_after_v2_write], [v5]), resolved_target=9)
        monkeypatch.setattr(repair, "_facade", facade)
        monkeypatch.setattr(repair, "_live_enabled", lambda *, now_utc: True)
        assert repair.process_next(now_utc=NOW + timedelta(seconds=4)) == (
            repair.RepairResult(attendance_id, "repaired", 1, None)
        )
        assert ("write_department_only", attendance_id, 9) in facade.events
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

        facade = FakeFacade(([v4], [v5]), resolved_target=9)
        monkeypatch.setattr(repair, "_facade", facade)
        monkeypatch.setattr(repair, "_live_enabled", lambda *, now_utc: True)
        assert repair.process_next(now_utc=NOW + timedelta(seconds=4)) == (
            repair.RepairResult(attendance_id, "repaired", 1, None)
        )
        assert ("write_department_only", attendance_id, 9) in facade.events
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

        projected_at = NOW + timedelta(seconds=1)
        candidate = repair._candidate_for_span(
            projected_v3_span,
            projected_at_utc=projected_at,
        )
        assert candidate is not None
        assert (
            backend.reconcile(
                (candidate,),
                (
                    repair.RepairProjectionProof(
                        attendance_id,
                        v3_version,
                        72,
                        8,
                        9,
                        projected_at,
                    ),
                ),
                now_utc=NOW + timedelta(seconds=2),
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

        facade = FakeFacade(([v4_after_v2_write], [v5]), resolved_target=9)
        monkeypatch.setattr(repair, "_facade", facade)
        monkeypatch.setattr(repair, "_live_enabled", lambda *, now_utc: True)
        assert repair.process_next(now_utc=NOW + timedelta(seconds=3)) == (
            repair.RepairResult(attendance_id, "repaired", 1, None)
        )
        assert ("write_department_only", attendance_id, 9) in facade.events
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

        stale_projected_at = NOW + timedelta(seconds=1)
        stale_candidate = repair._candidate_for_span(
            stale_v2_span,
            projected_at_utc=stale_projected_at,
        )
        assert stale_candidate is not None
        assert (
            backend.reconcile(
                (stale_candidate,),
                (
                    repair.RepairProjectionProof(
                        attendance_id,
                        VERSION,
                        72,
                        7,
                        8,
                        stale_projected_at,
                    ),
                ),
                now_utc=NOW + timedelta(seconds=3),
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
        backend = repair._PostgresBackend()

        assert (
            backend.reconcile(
                (),
                (
                    repair.RepairProjectionProof(
                        attendance_id,
                        current["odoo_write_date"],
                        72,
                        8,
                        8,
                        NOW + timedelta(seconds=1),
                    ),
                ),
                now_utc=NOW + timedelta(seconds=1),
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
    facade = FakeFacade((), resolved_target=current_department_id)
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
        backend = repair._PostgresBackend()
        assert [
            row["odoo_attendance_id"]
            for row in attendance_exceptions._failed_department_repairs(
                NOW - timedelta(hours=3),
                NOW + timedelta(hours=1),
            )
        ] == [attendance_id]

        assert (
            backend.reconcile(
                (),
                (
                    repair.RepairProjectionProof(
                        attendance_id,
                        current_version,
                        current_work_center_id,
                        current_department_id,
                        current_department_id,
                        NOW + timedelta(seconds=1),
                    ),
                ),
                now_utc=NOW + timedelta(seconds=1),
            )
            == 0
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


class _RecordingCursor:
    def __init__(self, fetches=(), fetchalls=()):
        self.calls = []
        self.fetches = list(fetches)
        self.fetchalls = list(fetchalls)
        self.rowcount = 1

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.fetches.pop(0) if self.fetches else None

    def fetchall(self):
        return self.fetchalls.pop(0) if self.fetchalls else []


def _install_cursor(monkeypatch, cursor):
    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(repair.db, "cursor", fake_cursor)


def _owned_queue_row(*, status="applying", target=8, projected_at=None):
    return {
        "status": status,
        "attempt_count": 1,
        "expected_write_date": VERSION,
        "target_odoo_department_id": target,
        "expected_odoo_work_center_id": 72,
        "target_projected_at": projected_at or NOW - timedelta(seconds=1),
        "successor_expected_write_date": None,
        "successor_target_odoo_department_id": None,
        "successor_expected_odoo_work_center_id": None,
        "successor_target_projected_at": None,
    }


def test_postgres_claim_is_oldest_first_skip_locked_with_fifteen_minute_lease(monkeypatch):
    row = {
        "odoo_attendance_id": 901,
        "expected_write_date": VERSION,
        "target_odoo_department_id": 8,
        "attempt_count": 0,
        "expected_odoo_work_center_id": 72,
        "mirror_write_date": VERSION,
    }
    cursor = _RecordingCursor((row,))
    _install_cursor(monkeypatch, cursor)

    claim = repair._PostgresBackend().claim_next(now_utc=NOW)

    assert claim == _claim()
    promotion_sql, promotion_params = cursor.calls[0]
    assert "successor_expected_write_date" in promotion_sql
    assert "status = 'pending'" in promotion_sql
    assert promotion_params == (NOW, NOW - timedelta(minutes=15), repair.MAX_ATTEMPTS)
    select_sql, select_params = cursor.calls[2]
    assert "ORDER BY r.updated_at, r.odoo_attendance_id" in select_sql
    assert "FOR UPDATE OF r SKIP LOCKED LIMIT 1" in select_sql
    assert select_params == (repair.MAX_ATTEMPTS, NOW - timedelta(minutes=15))


def test_discovery_enqueue_preserves_applying_row_as_successor(monkeypatch):
    cursor = _RecordingCursor((None, _owned_queue_row()))
    _install_cursor(monkeypatch, cursor)
    candidate = repair.RepairCandidate(901, NEW_VERSION, 9, 72, NOW)

    assert repair._PostgresBackend().enqueue((candidate,), now_utc=NOW) == 1

    sql, params = cursor.calls[0]
    assert "ON CONFLICT (odoo_attendance_id) DO NOTHING" in sql
    assert params == (901, NEW_VERSION, 9, 72, NOW, NOW)
    successor_sql, successor_params = cursor.calls[2]
    assert "successor_expected_write_date" in successor_sql
    assert "status = 'pending'" not in successor_sql
    assert successor_params == (NEW_VERSION, 9, 72, NOW, 901)


def test_postgres_reconciliation_completes_only_covered_ids_without_candidates(monkeypatch):
    cursor = _RecordingCursor(
        (
            {
                "odoo_write_date": VERSION,
                "odoo_work_center_id": 72,
                "odoo_department_id": 8,
            },
        ),
        (({"odoo_attendance_id": 901},),),
    )
    _install_cursor(monkeypatch, cursor)
    proof = repair.RepairProjectionProof(901, VERSION, 72, 8, 8, NOW)

    assert repair._PostgresBackend().reconcile((), (proof,), now_utc=NOW) == 0

    lock_sql, lock_params = cursor.calls[0]
    assert "attendance_department_repairs" in lock_sql
    assert "FOR UPDATE" in lock_sql
    assert lock_params == ([901],)
    mirror_sql, mirror_params = cursor.calls[1]
    assert "odoo_attendance_mirror" in mirror_sql
    assert "FOR UPDATE" in mirror_sql
    assert mirror_params == (901,)
    sql, params = cursor.calls[2]
    assert "status = 'complete'" in sql
    assert "last_error = NULL" in sql
    assert "status <> 'applying'" in sql
    assert "odoo_attendance_id = %s" in sql
    assert "%s > target_projected_at" in sql
    assert params == (VERSION, 8, 72, NOW, NOW, 901, NOW)


def test_baseline_reconciliation_validates_only_actionable_repair_ids(monkeypatch):
    proofs = tuple(
        repair.RepairProjectionProof(attendance_id, VERSION, 72, 8, 8, NOW)
        for attendance_id in range(1, 10_001)
    )
    cursor = _RecordingCursor(
        (
            {
                "odoo_write_date": VERSION,
                "odoo_work_center_id": 72,
                "odoo_department_id": 8,
            },
        ),
        (({"odoo_attendance_id": 1},),),
    )
    _install_cursor(monkeypatch, cursor)

    assert repair._PostgresBackend().reconcile((), proofs, now_utc=NOW) == 0

    queue_locks = [sql for sql, _params in cursor.calls if "repairs" in sql and "FOR UPDATE" in sql]
    mirror_locks = [
        sql
        for sql, _params in cursor.calls
        if "odoo_attendance_mirror" in sql and "FOR UPDATE" in sql
    ]
    assert len(queue_locks) == 1
    assert len(mirror_locks) == 1


def test_prewrite_reservation_proves_exact_fence_and_renews_lease(monkeypatch):
    cursor = _RecordingCursor(
        (_owned_queue_row(), {"odoo_write_date": VERSION, "odoo_work_center_id": 72})
    )
    _install_cursor(monkeypatch, cursor)

    assert repair._PostgresBackend().reserve_for_write(_claim(), now_utc=NOW) is True

    sql, params = cursor.calls[0]
    assert "expected_odoo_work_center_id" in sql
    assert "target_projected_at" in sql
    assert "FOR UPDATE" in sql
    assert params == (901,)
    mirror_sql, mirror_params = cursor.calls[1]
    assert "deleted_at IS NULL" in mirror_sql
    assert "odoo_write_date" in mirror_sql
    assert "odoo_work_center_id" in mirror_sql
    assert mirror_params == (901,)
    renewal_sql, renewal_params = cursor.calls[2]
    assert "UPDATE attendance_department_repairs SET updated_at = %s" in renewal_sql
    assert renewal_params == (NOW, 901, 1, VERSION, 8, 72)


def test_stale_verified_row_cannot_update_mirror_or_complete_queue(monkeypatch):
    cursor = _RecordingCursor(
        (
            _owned_queue_row(),
            {"odoo_write_date": NEW_VERSION + timedelta(minutes=1), "odoo_work_center_id": 72},
        )
    )
    _install_cursor(monkeypatch, cursor)
    monkeypatch.setattr(
        repair.attendance_mirror,
        "_locked_sync_state",
        lambda _cur: {"baseline_completed_at": NOW},
    )
    upserts = []
    monkeypatch.setattr(
        repair.attendance_mirror,
        "_upsert_rows_cur",
        lambda *_args, **_kwargs: upserts.append((_args, _kwargs)),
    )

    completed = repair._PostgresBackend().finish_verified(
        _claim(),
        _row(department_id=8, write_date=NEW_VERSION),
        now_utc=NOW,
    )

    assert completed is False
    assert upserts == []
    assert not any("UPDATE attendance_department_repairs SET" in sql for sql, _ in cursor.calls)
