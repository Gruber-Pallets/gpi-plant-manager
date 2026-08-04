from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

import zira_dashboard.payroll_work_entry_guard as guard
import zira_dashboard.payroll_work_entry_store as store
from zira_dashboard.payroll_work_entry_rules import Decision


NOW = datetime(2026, 8, 4, 17, 0, tzinfo=UTC)
DAY = date(2026, 7, 24)


def correction(
    *,
    action="duration_update",
    before=3.6214,
    after=3.1214,
    employee_id=19,
    entry_id=8502,
    attendance_id=3811,
):
    return Decision(
        kind="correct",
        employee_id=employee_id,
        employee_name=f"Employee {employee_id}",
        work_date=DAY,
        reason_codes=(),
        action=action,
        work_entry_id=entry_id,
        attendance_id=attendance_id,
        before_duration=before,
        after_duration=after,
        attendance_regular=after,
        attendance_overtime=5.3092,
        work_regular=before,
        work_overtime=5.3092,
    )


def work_row(item, *, duration=None):
    return {
        "id": item.work_entry_id,
        "employee_id": item.employee_id,
        "employee_name": item.employee_name,
        "date": item.work_date,
        "duration": item.before_duration if duration is None else duration,
        "state": "draft",
        "active": True,
        "conflict": False,
        "type_code": "WORK100",
        "attendance_id": item.attendance_id,
    }


def saved_attempt(item, attempt_id=None, reason="pending_correction"):
    return store.CorrectionAttempt(
        attempt_id=attempt_id or uuid4(),
        decision=item,
        last_reason=reason,
        last_detail="correction intent saved",
        created_at=NOW,
        updated_at=NOW,
    )


class FakeAttemptStore:
    def __init__(self, attempts=(), events=None):
        self.pending = {item.attempt_id: item for item in attempts}
        self.audit = {}
        self.events = events if events is not None else []
        self.finalize_failures = {}
        self.create_error = None
        self.create_calls = []
        self.mark_calls = []

    @contextmanager
    def guard_lock(self):
        self.events.append("guard-enter")
        try:
            yield
        finally:
            self.events.append("guard-exit")

    def load_pending_attempts(self):
        self.events.append("load-pending")
        return sorted(self.pending.values(), key=lambda item: str(item.attempt_id))

    def create_attempt(self, attempt_id, item, now):
        self.events.append("create-intent")
        self.create_calls.append((attempt_id, item, now))
        if self.create_error is not None:
            raise self.create_error
        if any(
            current.decision.work_entry_id == item.work_entry_id
            for current in self.pending.values()
        ):
            raise RuntimeError("one pending correction already exists for entry")
        attempt = store.CorrectionAttempt(
            attempt_id=attempt_id,
            decision=item,
            last_reason="pending_correction",
            last_detail="correction intent saved",
            created_at=now,
            updated_at=now,
        )
        self.pending[attempt_id] = attempt
        return attempt

    def mark_attempt_issue(self, attempt_id, reason, detail, now):
        self.events.append(f"mark-{reason}")
        self.mark_calls.append((attempt_id, reason, detail, now))
        current = self.pending.get(attempt_id)
        if current is not None:
            self.pending[attempt_id] = replace(
                current,
                last_reason=reason,
                last_detail=detail,
                updated_at=now,
            )

    def finalize_attempt(self, attempt_id, detail, corrected_at):
        self.events.append("finalize")
        mode = self.finalize_failures.get(attempt_id)
        if mode == "before":
            self.finalize_failures[attempt_id] = None
            raise RuntimeError("audit transaction rolled back")
        current = self.pending.get(attempt_id)
        if current is None:
            return False
        self.audit.setdefault(attempt_id, (current, detail, corrected_at))
        del self.pending[attempt_id]
        if mode == "after":
            self.finalize_failures[attempt_id] = None
            raise RuntimeError("commit succeeded but response was lost")
        return True


class FakeOdoo:
    def __init__(self, rows=(), events=None):
        self.rows = {row["id"]: dict(row) for row in rows}
        self.events = events if events is not None else []
        self.write_calls = []
        self.delete_calls = []
        self.write_mode = None
        self.delete_mode = None
        self.fail_next_reads = 0
        self.candidates = []

    def read(self, entry_id):
        self.events.append(f"read-{entry_id}")
        if self.fail_next_reads:
            self.fail_next_reads -= 1
            raise RuntimeError("verification read failed")
        row = self.rows.get(entry_id)
        return dict(row) if row is not None else None

    def write(self, entry_id, duration):
        self.events.append(f"write-{entry_id}")
        self.write_calls.append((entry_id, duration))
        self.rows[entry_id]["duration"] = duration
        if self.write_mode == "commit_raise":
            self.write_mode = None
            raise RuntimeError("write committed but response was lost")
        if self.write_mode == "verify_read_fails":
            self.write_mode = None
            self.fail_next_reads = 1

    def delete(self, entry_id):
        self.events.append(f"delete-{entry_id}")
        self.delete_calls.append(entry_id)
        self.rows.pop(entry_id, None)
        if self.delete_mode == "commit_raise":
            self.delete_mode = None
            raise RuntimeError("delete committed but response was lost")

    def fetch_candidates(self, _since):
        self.events.append("fetch-candidates")
        return list(self.candidates)


def wire(monkeypatch, fake_store, fake_odoo):
    alerts = []
    monkeypatch.setenv("PAYROLL_WORK_ENTRY_GUARD_ENABLED", "1")
    monkeypatch.setattr(guard.store, "guard_lock", fake_store.guard_lock)
    monkeypatch.setattr(
        guard.store,
        "load_pending_attempts",
        fake_store.load_pending_attempts,
        raising=False,
    )
    monkeypatch.setattr(
        guard.store, "create_attempt", fake_store.create_attempt, raising=False
    )
    monkeypatch.setattr(
        guard.store,
        "mark_attempt_issue",
        fake_store.mark_attempt_issue,
        raising=False,
    )
    monkeypatch.setattr(
        guard.store,
        "finalize_attempt",
        fake_store.finalize_attempt,
        raising=False,
    )
    monkeypatch.setattr(
        guard.store,
        "append_correction",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("guard must finalize durable attempts")
        ),
    )
    monkeypatch.setattr(
        guard.odoo_client, "fetch_payroll_work_entry", fake_odoo.read
    )
    monkeypatch.setattr(
        guard.odoo_client, "set_payroll_work_entry_duration", fake_odoo.write
    )
    monkeypatch.setattr(
        guard.odoo_client, "delete_payroll_work_entry", fake_odoo.delete
    )
    monkeypatch.setattr(
        guard.odoo_client,
        "fetch_recent_payroll_candidates",
        fake_odoo.fetch_candidates,
    )
    monkeypatch.setattr(
        guard.alert,
        "sync_review_task",
        lambda issues, _now: alerts.append(list(issues)),
    )
    return alerts


def test_duration_commit_then_rpc_error_is_recovered_without_second_write(monkeypatch):
    item = correction()
    attempt = saved_attempt(item)
    events = []
    fake_store = FakeAttemptStore([attempt], events)
    fake_odoo = FakeOdoo([work_row(item)], events)
    fake_odoo.write_mode = "commit_raise"
    alerts = wire(monkeypatch, fake_store, fake_odoo)

    first_result = guard.run_once(NOW)
    second_result = guard.run_once(NOW)

    assert fake_odoo.write_calls == [(item.work_entry_id, item.after_duration)]
    assert len(fake_store.audit) == 1
    assert fake_store.audit[attempt.attempt_id][1] == "duration reread matched"
    assert fake_store.pending == {}
    assert alerts[-1] == []
    assert first_result == {"corrected": 1, "review": 0, "noop": 0}
    assert second_result == {"corrected": 0, "review": 0, "noop": 0}


def test_delete_commit_then_rpc_error_is_recovered_without_second_delete(monkeypatch):
    item = correction(action="delete_zero_regular", before=0.5, after=0.0)
    attempt = saved_attempt(item)
    fake_store = FakeAttemptStore([attempt])
    fake_odoo = FakeOdoo([work_row(item)])
    fake_odoo.delete_mode = "commit_raise"
    wire(monkeypatch, fake_store, fake_odoo)

    first_result = guard.run_once(NOW)
    second_result = guard.run_once(NOW)

    assert fake_odoo.delete_calls == [item.work_entry_id]
    assert len(fake_store.audit) == 1
    assert fake_store.audit[attempt.attempt_id][1] == (
        "zero-target draft regular row absent"
    )
    assert fake_store.pending == {}
    assert first_result == {"corrected": 1, "review": 0, "noop": 0}
    assert second_result == {"corrected": 0, "review": 0, "noop": 0}


def test_verification_read_outage_keeps_review_then_recovers_next_run(monkeypatch):
    item = correction()
    attempt = saved_attempt(item)
    fake_store = FakeAttemptStore([attempt])
    fake_odoo = FakeOdoo([work_row(item)])
    fake_odoo.write_mode = "verify_read_fails"
    alerts = wire(monkeypatch, fake_store, fake_odoo)

    first_result = guard.run_once(NOW)

    assert attempt.attempt_id in fake_store.pending
    assert fake_store.audit == {}
    assert fake_odoo.write_calls == [(item.work_entry_id, item.after_duration)]
    assert "pending_correction" in alerts[-1][0].reason_codes
    assert "verification_failed" in alerts[-1][0].reason_codes

    second_result = guard.run_once(NOW)

    assert fake_odoo.write_calls == [(item.work_entry_id, item.after_duration)]
    assert len(fake_store.audit) == 1
    assert fake_store.audit[attempt.attempt_id][1] == (
        "target observed during recovery; actor unknown"
    )
    assert fake_store.pending == {}
    assert first_result == {"corrected": 0, "review": 1, "noop": 0}
    assert second_result == {"corrected": 0, "review": 0, "noop": 0}


def test_audit_failure_before_commit_keeps_pending_then_finalizes_once(monkeypatch):
    item = correction()
    attempt = saved_attempt(item)
    fake_store = FakeAttemptStore([attempt])
    fake_store.finalize_failures[attempt.attempt_id] = "before"
    fake_odoo = FakeOdoo([work_row(item)])
    alerts = wire(monkeypatch, fake_store, fake_odoo)

    first_result = guard.run_once(NOW)

    assert attempt.attempt_id in fake_store.pending
    assert fake_store.audit == {}
    assert "audit_failed" in alerts[-1][0].reason_codes

    second_result = guard.run_once(NOW)

    assert fake_odoo.write_calls == [(item.work_entry_id, item.after_duration)]
    assert len(fake_store.audit) == 1
    assert fake_store.audit[attempt.attempt_id][1] == (
        "target observed during recovery; actor unknown"
    )
    assert fake_store.pending == {}
    assert first_result == {"corrected": 1, "review": 1, "noop": 0}
    assert second_result == {"corrected": 0, "review": 0, "noop": 0}


def test_audit_commit_response_loss_does_not_duplicate_audit(monkeypatch):
    item = correction()
    attempt = saved_attempt(item)
    fake_store = FakeAttemptStore([attempt])
    fake_store.finalize_failures[attempt.attempt_id] = "after"
    fake_odoo = FakeOdoo([work_row(item, duration=item.after_duration)])
    wire(monkeypatch, fake_store, fake_odoo)

    first_result = guard.run_once(NOW)
    second_result = guard.run_once(NOW)

    assert fake_odoo.write_calls == []
    assert len(fake_store.audit) == 1
    assert fake_store.audit[attempt.attempt_id][1] == (
        "target observed during recovery; actor unknown"
    )
    assert fake_store.pending == {}
    assert first_result == {"corrected": 0, "review": 1, "noop": 0}
    assert second_result == {"corrected": 0, "review": 0, "noop": 0}


def test_crash_after_intent_then_external_delete_finalizes_without_mutation(monkeypatch):
    item = correction(action="delete_zero_regular", before=0.5, after=0.0)
    attempt = saved_attempt(item)
    events = []
    fake_store = FakeAttemptStore([attempt], events)
    fake_odoo = FakeOdoo([], events)
    wire(monkeypatch, fake_store, fake_odoo)

    result = guard.run_once(NOW)

    assert events.index("finalize") < events.index("fetch-candidates")
    assert fake_store.pending == {}
    assert len(fake_store.audit) == 1
    assert fake_store.audit[attempt.attempt_id][1] == (
        "row absent during recovery; actor unknown"
    )
    assert fake_odoo.delete_calls == []
    assert result == {"corrected": 0, "review": 0, "noop": 0}


def test_crash_after_intent_then_external_target_finalizes_without_mutation(
    monkeypatch,
):
    item = correction()
    attempt = saved_attempt(item)
    fake_store = FakeAttemptStore([attempt])
    fake_odoo = FakeOdoo([work_row(item, duration=item.after_duration)])
    wire(monkeypatch, fake_store, fake_odoo)

    result = guard.run_once(NOW)

    assert fake_odoo.write_calls == []
    assert fake_store.pending == {}
    assert len(fake_store.audit) == 1
    assert fake_store.audit[attempt.attempt_id][1] == (
        "target observed during recovery; actor unknown"
    )
    assert result == {"corrected": 0, "review": 0, "noop": 0}


def test_one_pending_failure_does_not_block_other_attempt_recovery(monkeypatch):
    first = correction()
    second = correction(
        employee_id=22,
        entry_id=8483,
        attendance_id=3805,
    )
    first_attempt = saved_attempt(first)
    second_attempt = saved_attempt(second)
    fake_store = FakeAttemptStore([first_attempt, second_attempt])
    fake_odoo = FakeOdoo(
        [
            work_row(first),
            work_row(second, duration=second.after_duration),
        ]
    )
    fake_odoo.fail_next_reads = 1
    alerts = wire(monkeypatch, fake_store, fake_odoo)

    guard.run_once(NOW)

    assert len(fake_store.pending) == 1
    assert len(fake_store.audit) == 1
    assert set(fake_store.pending).isdisjoint(fake_store.audit)
    assert len(alerts[-1]) == 1
    pending_entry_id = next(iter(fake_store.pending.values())).decision.work_entry_id
    assert alerts[-1][0].work_entry_id == pending_entry_id


def test_new_correction_persists_intent_before_strict_mutation(monkeypatch):
    item = correction()
    attempt_id = UUID("6c06ad5f-6148-4458-a3e5-20844323c91d")
    events = []
    fake_store = FakeAttemptStore(events=events)
    fake_odoo = FakeOdoo([work_row(item)], events)
    fake_odoo.candidates = [
        {
            "id": item.work_entry_id,
            "employee_id": item.employee_id,
            "employee_name": item.employee_name,
            "date": item.work_date,
        }
    ]
    wire(monkeypatch, fake_store, fake_odoo)
    monkeypatch.setattr(guard, "uuid4", lambda: attempt_id, raising=False)
    monkeypatch.setattr(
        guard.odoo_client,
        "fetch_payroll_inputs",
        lambda *_args: (
            [
                {
                    "id": item.work_entry_id,
                    "employee_id": item.employee_id,
                    "employee_name": item.employee_name,
                    "date": item.work_date,
                }
            ],
            [],
        ),
    )
    monkeypatch.setattr(guard, "classify_day", lambda *_args: item)

    guard.run_once(NOW)

    assert events.index("create-intent") < events.index(f"write-{item.work_entry_id}")
    assert fake_store.create_calls == [(attempt_id, item, NOW)]
    assert fake_odoo.write_calls == [(item.work_entry_id, item.after_duration)]
    assert len(fake_store.audit) == 1


def test_intent_persistence_failure_never_calls_odoo_mutation(monkeypatch):
    item = correction()
    fake_store = FakeAttemptStore()
    fake_store.create_error = RuntimeError("database unavailable")
    fake_odoo = FakeOdoo([work_row(item)])
    fake_odoo.candidates = [
        {
            "id": item.work_entry_id,
            "employee_id": item.employee_id,
            "employee_name": item.employee_name,
            "date": item.work_date,
        }
    ]
    alerts = wire(monkeypatch, fake_store, fake_odoo)
    monkeypatch.setattr(
        guard.odoo_client,
        "fetch_payroll_inputs",
        lambda *_args: (
            [
                {
                    "id": item.work_entry_id,
                    "employee_id": item.employee_id,
                    "employee_name": item.employee_name,
                    "date": item.work_date,
                }
            ],
            [],
        ),
    )
    monkeypatch.setattr(guard, "classify_day", lambda *_args: item)

    guard.run_once(NOW)

    assert fake_odoo.write_calls == []
    assert fake_odoo.delete_calls == []
    assert alerts[-1][0].reason_codes == ("intent_failed",)


def test_candidate_with_existing_pending_attempt_never_creates_second_intent(
    monkeypatch,
):
    item = correction()
    attempt = saved_attempt(item)
    fake_store = FakeAttemptStore([attempt])
    fake_odoo = FakeOdoo([work_row(item)])
    fake_odoo.fail_next_reads = 1
    fake_odoo.candidates = [
        {
            "id": item.work_entry_id,
            "employee_id": item.employee_id,
            "employee_name": item.employee_name,
            "date": item.work_date,
        }
    ]
    wire(monkeypatch, fake_store, fake_odoo)
    monkeypatch.setattr(
        guard.odoo_client,
        "fetch_payroll_inputs",
        lambda *_args: (
            [
                {
                    "id": item.work_entry_id,
                    "employee_id": item.employee_id,
                    "employee_name": item.employee_name,
                    "date": item.work_date,
                }
            ],
            [],
        ),
    )
    monkeypatch.setattr(guard, "classify_day", lambda *_args: item)

    guard.run_once(NOW)

    assert fake_store.create_calls == []
    assert attempt.attempt_id in fake_store.pending


def test_malformed_pending_snapshot_aborts_before_candidate_or_odoo_work(monkeypatch):
    fake_store = FakeAttemptStore()
    fake_odoo = FakeOdoo()
    wire(monkeypatch, fake_store, fake_odoo)
    monkeypatch.setattr(
        guard.store,
        "load_pending_attempts",
        lambda: (_ for _ in ()).throw(ValueError("malformed snapshot")),
        raising=False,
    )

    with pytest.raises(ValueError, match="malformed snapshot"):
        guard.run_once(NOW)

    assert fake_odoo.events == []
    assert fake_odoo.write_calls == []
    assert fake_odoo.delete_calls == []
