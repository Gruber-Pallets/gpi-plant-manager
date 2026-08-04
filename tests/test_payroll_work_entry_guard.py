import logging
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import zira_dashboard.payroll_work_entry_guard as guard
from zira_dashboard.payroll_work_entry_rules import Decision, TOLERANCE_HOURS


NOW = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)
DAY = date(2026, 7, 24)


def decision(
    *,
    kind="correct",
    employee_id=19,
    entry_id=8502,
    attendance_id=3811,
    work_date=DAY,
    action="duration_update",
    reasons=(),
    before=3.6214,
    after=3.1214,
):
    return Decision(
        kind=kind,
        employee_id=employee_id,
        employee_name=f"Employee {employee_id}",
        work_date=work_date,
        reason_codes=reasons,
        action=action if kind == "correct" else None,
        work_entry_id=entry_id,
        attendance_id=attendance_id,
        before_duration=before,
        after_duration=after if kind == "correct" else None,
        attendance_regular=after,
        attendance_overtime=5.3092,
        work_regular=before,
        work_overtime=5.3092,
    )


def candidate(item):
    return {
        "id": item.work_entry_id,
        "employee_id": item.employee_id,
        "employee_name": item.employee_name,
        "date": item.work_date,
    }


def fresh(item, **changes):
    row = {
        "id": item.work_entry_id,
        "employee_id": item.employee_id,
        "employee_name": item.employee_name,
        "date": item.work_date,
        "duration": item.before_duration,
        "state": "draft",
        "active": True,
        "conflict": False,
        "type_code": "WORK100",
        "attendance_id": item.attendance_id,
    }
    row.update(changes)
    return row


def recording_lock(events=None, calls=None):
    @contextmanager
    def lock():
        if calls is not None:
            calls()
        if events is not None:
            events.append("guard-enter")
        try:
            yield
        finally:
            if events is not None:
                events.append("guard-exit")

    return lock


def wire_batch(monkeypatch, decisions, *, events=None, candidates=None):
    events = events if events is not None else []
    monkeypatch.setenv("PAYROLL_WORK_ENTRY_GUARD_ENABLED", "1")
    monkeypatch.setattr(guard.store, "guard_lock", recording_lock(events))
    candidate_rows = (
        [candidate(item) for item in decisions] if candidates is None else candidates
    )

    def fetch_candidates(_since):
        events.append("candidates")
        return candidate_rows

    monkeypatch.setattr(
        guard.odoo_client, "fetch_recent_payroll_candidates", fetch_candidates
    )
    grouped_work = [
        {
            "employee_id": item.employee_id,
            "employee_name": item.employee_name,
            "date": item.work_date,
            "id": item.work_entry_id,
        }
        for item in decisions
    ]

    def fetch_inputs(_ids, _start, _end):
        events.append("inputs")
        return grouped_work, []

    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_inputs", fetch_inputs)
    by_key = {(item.employee_id, item.work_date): item for item in decisions}

    def classify(employee_id, _name, work_date, _work, _attendance):
        events.append(f"classify {employee_id} {work_date.isoformat()}")
        return by_key[(employee_id, work_date)]

    monkeypatch.setattr(guard, "classify_day", classify)
    monkeypatch.setattr(
        guard.alert,
        "sync_review_task",
        MagicMock(side_effect=lambda *_args: events.append("alert")),
    )
    monkeypatch.setattr(guard.store, "append_correction", MagicMock())
    monkeypatch.setattr(
        guard.odoo_client, "set_payroll_work_entry_duration", MagicMock()
    )
    monkeypatch.setattr(guard.odoo_client, "delete_payroll_work_entry", MagicMock())
    monkeypatch.setattr(
        guard.odoo_client,
        "payroll_work_entry_exists",
        MagicMock(return_value=False),
    )
    return events


@pytest.mark.parametrize("value", ["0", "FALSE", "no", " No "])
def test_kill_switch_makes_zero_lock_odoo_db_or_alert_calls(monkeypatch, value):
    monkeypatch.setenv("PAYROLL_WORK_ENTRY_GUARD_ENABLED", value)
    lock = MagicMock()
    fetch = MagicMock()
    sync = MagicMock()
    audit = MagicMock()
    monkeypatch.setattr(guard.store, "guard_lock", lock)
    monkeypatch.setattr(guard.odoo_client, "fetch_recent_payroll_candidates", fetch)
    monkeypatch.setattr(guard.alert, "sync_review_task", sync)
    monkeypatch.setattr(guard.store, "append_correction", audit)

    assert guard.run_once(datetime(2026, 8, 3, 20, 0)) == {"skipped": "disabled"}

    lock.assert_not_called()
    fetch.assert_not_called()
    sync.assert_not_called()
    audit.assert_not_called()


def test_default_is_enabled_and_empty_run_is_locked(monkeypatch):
    events = []
    fetch_inputs = MagicMock()
    monkeypatch.delenv("PAYROLL_WORK_ENTRY_GUARD_ENABLED", raising=False)
    monkeypatch.setattr(guard.store, "guard_lock", recording_lock(events))
    monkeypatch.setattr(
        guard.odoo_client,
        "fetch_recent_payroll_candidates",
        lambda _since: events.append("candidates") or [],
    )
    monkeypatch.setattr(
        guard.alert,
        "sync_review_task",
        lambda _issues, _now: events.append("alert"),
    )
    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_inputs", fetch_inputs)

    assert guard.run_once(NOW) == {"corrected": 0, "review": 0, "noop": 0}
    assert events == ["guard-enter", "candidates", "alert", "guard-exit"]
    fetch_inputs.assert_not_called()


def test_enabled_run_requires_timezone_aware_now_before_lock(monkeypatch):
    monkeypatch.setenv("PAYROLL_WORK_ENTRY_GUARD_ENABLED", "1")
    lock = MagicMock()
    monkeypatch.setattr(guard.store, "guard_lock", lock)

    with pytest.raises(ValueError, match="timezone-aware"):
        guard.run_once(datetime(2026, 8, 3, 20, 0))

    lock.assert_not_called()


def test_aware_now_drives_exact_90_day_write_date_lookback(monkeypatch):
    local_now = datetime(2026, 8, 3, 15, 0, tzinfo=timezone(timedelta(hours=-5)))
    seen = []
    monkeypatch.setenv("PAYROLL_WORK_ENTRY_GUARD_ENABLED", "1")
    monkeypatch.setattr(guard.store, "guard_lock", recording_lock())
    monkeypatch.setattr(
        guard.odoo_client,
        "fetch_recent_payroll_candidates",
        lambda since: seen.append(since) or [],
    )
    monkeypatch.setattr(guard.alert, "sync_review_task", MagicMock())

    guard.run_once(local_now)

    assert seen == [local_now - timedelta(days=90)]
    guard.alert.sync_review_task.assert_called_once_with([], local_now)


def test_all_enabled_external_work_occurs_inside_guard_lock(monkeypatch):
    item = decision()
    events = wire_batch(monkeypatch, [item])
    read_count = 0

    def read(_entry_id):
        nonlocal read_count
        read_count += 1
        events.append("fresh-read" if read_count == 1 else "verify-read")
        duration = item.before_duration if read_count == 1 else item.after_duration
        return fresh(item, duration=duration)

    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_work_entry", read)
    monkeypatch.setattr(
        guard.odoo_client,
        "set_payroll_work_entry_duration",
        lambda *_args: events.append("write"),
    )
    monkeypatch.setattr(
        guard.store,
        "append_correction",
        lambda *_args: events.append("audit"),
    )

    guard.run_once(NOW)

    assert events == [
        "guard-enter",
        "candidates",
        "inputs",
        "classify 19 2026-07-24",
        "fresh-read",
        "write",
        "verify-read",
        "audit",
        "alert",
        "guard-exit",
    ]


def test_guard_lock_releases_when_batch_fetch_raises(monkeypatch):
    events = []
    monkeypatch.setenv("PAYROLL_WORK_ENTRY_GUARD_ENABLED", "1")
    monkeypatch.setattr(guard.store, "guard_lock", recording_lock(events))

    def fail(_since):
        events.append("candidates")
        raise RuntimeError("Odoo unavailable")

    monkeypatch.setattr(guard.odoo_client, "fetch_recent_payroll_candidates", fail)

    with pytest.raises(RuntimeError, match="Odoo unavailable"):
        guard.run_once(NOW)

    assert events == ["guard-enter", "candidates", "guard-exit"]


def test_positive_target_writes_rereads_then_audits(monkeypatch):
    item = decision()
    events = wire_batch(monkeypatch, [item])
    reads = iter([fresh(item), fresh(item, duration=item.after_duration)])
    monkeypatch.setattr(
        guard.odoo_client,
        "fetch_payroll_work_entry",
        lambda _entry_id: events.append("read") or next(reads),
    )
    write = MagicMock(side_effect=lambda *_args: events.append("write"))
    monkeypatch.setattr(guard.odoo_client, "set_payroll_work_entry_duration", write)
    monkeypatch.setattr(
        guard.store,
        "append_correction",
        lambda _decision, _detail, _now: events.append("audit"),
    )

    result = guard.run_once(NOW)

    lifecycle = [event for event in events if event in {"read", "write", "audit"}]
    assert lifecycle == ["read", "write", "read", "audit"]
    write.assert_called_once_with(item.work_entry_id, item.after_duration)
    assert result == {"corrected": 1, "review": 0, "noop": 0}
    guard.alert.sync_review_task.assert_called_once_with([], NOW)


def test_zero_target_deletes_only_regular_row_then_audits(monkeypatch):
    item = decision(action="delete_zero_regular", before=0.5, after=0.0)
    wire_batch(monkeypatch, [item])
    monkeypatch.setattr(
        guard.odoo_client, "fetch_payroll_work_entry", lambda _id: fresh(item)
    )
    delete = MagicMock()
    write = MagicMock()
    monkeypatch.setattr(guard.odoo_client, "delete_payroll_work_entry", delete)
    monkeypatch.setattr(guard.odoo_client, "set_payroll_work_entry_duration", write)

    result = guard.run_once(NOW)

    delete.assert_called_once_with(item.work_entry_id)
    write.assert_not_called()
    guard.odoo_client.payroll_work_entry_exists.assert_called_once_with(
        item.work_entry_id
    )
    guard.store.append_correction.assert_called_once_with(
        item, "zero-target draft regular row absent", NOW
    )
    assert result == {"corrected": 1, "review": 0, "noop": 0}


@pytest.mark.parametrize(
    "changed_row",
    [
        None,
        {"id": 9999},
        {"active": False},
        {"state": "validated"},
        {"conflict": True},
        {"type_code": "OVERTIME"},
        {"attendance_id": 9999},
        {"employee_id": 9999},
        {"date": date(2026, 7, 25)},
        {"duration": 3.6214 + TOLERANCE_HOURS + 0.001},
    ],
    ids=[
        "none",
        "wrong-id",
        "inactive",
        "wrong-state",
        "conflict",
        "wrong-type",
        "wrong-attendance",
        "wrong-employee",
        "wrong-date",
        "wrong-duration",
    ],
)
def test_changed_fresh_snapshot_refuses_write_and_creates_review(
    monkeypatch, changed_row
):
    item = decision()
    wire_batch(monkeypatch, [item])
    row = None if changed_row is None else fresh(item, **changed_row)
    monkeypatch.setattr(
        guard.odoo_client, "fetch_payroll_work_entry", lambda _id: row
    )

    result = guard.run_once(NOW)

    guard.odoo_client.set_payroll_work_entry_duration.assert_not_called()
    guard.store.append_correction.assert_not_called()
    issues = guard.alert.sync_review_task.call_args.args[0]
    assert issues[0].reason_codes == ("fresh_state_changed",)
    assert result == {"corrected": 0, "review": 1, "noop": 0}


def test_fresh_read_exception_is_review_and_other_group_still_corrects(monkeypatch):
    first = decision(employee_id=19, entry_id=8502)
    second = decision(employee_id=22, entry_id=8483, attendance_id=3805)
    wire_batch(monkeypatch, [first, second])
    counts = {8502: 0, 8483: 0}

    def read(entry_id):
        counts[entry_id] += 1
        if entry_id == first.work_entry_id:
            raise RuntimeError("read failed")
        duration = second.before_duration if counts[entry_id] == 1 else second.after_duration
        return fresh(second, duration=duration)

    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_work_entry", read)

    result = guard.run_once(NOW)

    assert result == {"corrected": 1, "review": 1, "noop": 0}
    assert guard.alert.sync_review_task.call_args.args[0][0].reason_codes == (
        "fresh_read_failed",
    )


@pytest.mark.parametrize("action", ["duration_update", "delete_zero_regular"])
def test_mutation_failure_does_not_audit(monkeypatch, action):
    item = decision(
        action=action,
        before=0.5 if action == "delete_zero_regular" else 3.6214,
        after=0.0 if action == "delete_zero_regular" else 3.1214,
    )
    wire_batch(monkeypatch, [item])
    monkeypatch.setattr(
        guard.odoo_client, "fetch_payroll_work_entry", lambda _id: fresh(item)
    )
    mutation = MagicMock(side_effect=RuntimeError("Odoo refused"))
    if action == "duration_update":
        monkeypatch.setattr(
            guard.odoo_client, "set_payroll_work_entry_duration", mutation
        )
    else:
        monkeypatch.setattr(guard.odoo_client, "delete_payroll_work_entry", mutation)

    result = guard.run_once(NOW)

    mutation.assert_called_once()
    guard.store.append_correction.assert_not_called()
    assert guard.alert.sync_review_task.call_args.args[0][0].reason_codes == (
        "write_failed",
    )
    assert result == {"corrected": 0, "review": 1, "noop": 0}


def test_write_failure_isolated_from_other_candidate_group(monkeypatch):
    first = decision(employee_id=19, entry_id=8502)
    second = decision(employee_id=22, entry_id=8483, attendance_id=3805)
    wire_batch(monkeypatch, [first, second])
    read_counts = {8502: 0, 8483: 0}

    def read(entry_id):
        read_counts[entry_id] += 1
        item = first if entry_id == first.work_entry_id else second
        duration = item.before_duration if read_counts[entry_id] == 1 else item.after_duration
        return fresh(item, duration=duration)

    def write(entry_id, _duration):
        if entry_id == first.work_entry_id:
            raise RuntimeError("Odoo refused")

    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_work_entry", read)
    monkeypatch.setattr(guard.odoo_client, "set_payroll_work_entry_duration", write)

    result = guard.run_once(NOW)

    assert result == {"corrected": 1, "review": 1, "noop": 0}
    audited = guard.store.append_correction.call_args.args[0]
    assert audited.work_entry_id == second.work_entry_id
    assert guard.alert.sync_review_task.call_args.args[0][0].reason_codes == (
        "write_failed",
    )


@pytest.mark.parametrize(
    "verified",
    [
        None,
        {"duration": 4.1214},
        {"id": 9999, "duration": 3.1214},
        {"active": False, "duration": 3.1214},
        {"type_code": "OVERTIME", "duration": 3.1214},
        {"attendance_id": 9999, "duration": 3.1214},
    ],
    ids=["none", "wrong-duration", "wrong-id", "inactive", "wrong-type", "wrong-link"],
)
def test_failed_duration_verification_does_not_audit(monkeypatch, verified):
    item = decision()
    wire_batch(monkeypatch, [item])
    verified_row = None if verified is None else fresh(item, **verified)
    reads = iter([fresh(item), verified_row])
    monkeypatch.setattr(
        guard.odoo_client, "fetch_payroll_work_entry", lambda _id: next(reads)
    )

    result = guard.run_once(NOW)

    guard.odoo_client.set_payroll_work_entry_duration.assert_called_once()
    guard.store.append_correction.assert_not_called()
    assert guard.alert.sync_review_task.call_args.args[0][0].reason_codes == (
        "verification_failed",
    )
    assert result == {"corrected": 0, "review": 1, "noop": 0}


def test_duration_verification_exception_does_not_audit(monkeypatch):
    item = decision()
    wire_batch(monkeypatch, [item])
    reads = iter([fresh(item), RuntimeError("read failed")])

    def read(_id):
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_work_entry", read)

    result = guard.run_once(NOW)

    guard.store.append_correction.assert_not_called()
    assert guard.alert.sync_review_task.call_args.args[0][0].reason_codes == (
        "verification_failed",
    )
    assert result["review"] == 1


def test_ambiguous_write_response_never_skips_fresh_verification(monkeypatch):
    item = decision()
    wire_batch(monkeypatch, [item])
    reads = MagicMock(side_effect=[fresh(item), fresh(item)])
    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_work_entry", reads)
    monkeypatch.setattr(
        guard.odoo_client,
        "set_payroll_work_entry_duration",
        MagicMock(return_value=False),
    )

    result = guard.run_once(NOW)

    assert reads.call_count == 2
    guard.store.append_correction.assert_not_called()
    assert result == {"corrected": 0, "review": 1, "noop": 0}


@pytest.mark.parametrize("exists", [True, RuntimeError("existence read failed")])
def test_delete_verification_failure_does_not_audit(monkeypatch, exists):
    item = decision(action="delete_zero_regular", before=0.5, after=0.0)
    wire_batch(monkeypatch, [item])
    monkeypatch.setattr(
        guard.odoo_client, "fetch_payroll_work_entry", lambda _id: fresh(item)
    )
    check = MagicMock(
        side_effect=exists if isinstance(exists, Exception) else None,
        return_value=exists if not isinstance(exists, Exception) else None,
    )
    monkeypatch.setattr(guard.odoo_client, "payroll_work_entry_exists", check)

    result = guard.run_once(NOW)

    guard.store.append_correction.assert_not_called()
    assert guard.alert.sync_review_task.call_args.args[0][0].reason_codes == (
        "verification_failed",
    )
    assert result == {"corrected": 0, "review": 1, "noop": 0}


def test_audit_failure_becomes_review_without_second_odoo_write(monkeypatch):
    item = decision()
    wire_batch(monkeypatch, [item])
    reads = iter([fresh(item), fresh(item, duration=item.after_duration)])
    monkeypatch.setattr(
        guard.odoo_client, "fetch_payroll_work_entry", lambda _id: next(reads)
    )
    monkeypatch.setattr(
        guard.store,
        "append_correction",
        MagicMock(side_effect=RuntimeError("db down")),
    )

    result = guard.run_once(NOW)

    guard.odoo_client.set_payroll_work_entry_duration.assert_called_once()
    assert guard.alert.sync_review_task.call_args.args[0][0].reason_codes == (
        "audit_failed",
    )
    assert result == {"corrected": 1, "review": 1, "noop": 0}


def test_every_candidate_group_is_classified_before_first_write(monkeypatch):
    first = decision(employee_id=19, entry_id=8502)
    second = decision(employee_id=22, entry_id=8483, attendance_id=3805)
    events = wire_batch(monkeypatch, [first, second])
    read_counts = {8502: 0, 8483: 0}

    def read(entry_id):
        read_counts[entry_id] += 1
        item = first if entry_id == 8502 else second
        duration = item.before_duration if read_counts[entry_id] == 1 else item.after_duration
        return fresh(item, duration=duration)

    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_work_entry", read)
    monkeypatch.setattr(
        guard.odoo_client,
        "set_payroll_work_entry_duration",
        lambda entry_id, _duration: events.append(f"write {entry_id}"),
    )

    guard.run_once(NOW)

    classify_positions = [
        index for index, event in enumerate(events) if event.startswith("classify ")
    ]
    first_write = events.index("write 8502")
    assert len(classify_positions) == 2
    assert max(classify_positions) < first_write


def test_duplicate_candidates_group_once_across_multiple_days(monkeypatch):
    first = decision(kind="noop", action=None)
    second = decision(
        kind="noop",
        action=None,
        entry_id=8503,
        work_date=date(2026, 7, 25),
    )
    candidates = [candidate(first), candidate(first), candidate(second)]
    events = wire_batch(monkeypatch, [first, second], candidates=candidates)
    calls = []

    def fetch_inputs(employee_ids, start_day, end_day):
        calls.append((employee_ids, start_day, end_day))
        return [
            {
                "employee_id": item.employee_id,
                "employee_name": item.employee_name,
                "date": item.work_date,
                "id": item.work_entry_id,
            }
            for item in (first, second)
        ], []

    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_inputs", fetch_inputs)

    result = guard.run_once(NOW)

    classifications = [event for event in events if event.startswith("classify ")]
    assert len(classifications) == 2
    assert calls == [([19], DAY, date(2026, 7, 25))]
    assert result == {"corrected": 0, "review": 0, "noop": 2}


def test_all_review_and_noop_decisions_never_reread_or_mutate(monkeypatch):
    review = decision(kind="review", reasons=("unapproved_overtime",))
    noop = decision(kind="noop", employee_id=22, entry_id=8483, attendance_id=3805)
    wire_batch(monkeypatch, [review, noop])
    read = MagicMock()
    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_work_entry", read)

    result = guard.run_once(NOW)

    read.assert_not_called()
    guard.odoo_client.set_payroll_work_entry_duration.assert_not_called()
    guard.odoo_client.delete_payroll_work_entry.assert_not_called()
    guard.store.append_correction.assert_not_called()
    assert guard.alert.sync_review_task.call_args.args[0] == [review]
    assert result == {"corrected": 0, "review": 1, "noop": 1}


def test_all_review_batch_counts_and_syncs_every_group(monkeypatch):
    first = decision(kind="review", reasons=("unapproved_overtime",))
    second = decision(
        kind="review",
        employee_id=22,
        entry_id=8483,
        attendance_id=3805,
        reasons=("non_draft_work_entry",),
    )
    wire_batch(monkeypatch, [first, second])
    read = MagicMock()
    monkeypatch.setattr(guard.odoo_client, "fetch_payroll_work_entry", read)

    result = guard.run_once(NOW)

    read.assert_not_called()
    assert guard.alert.sync_review_task.call_args.args[0] == [first, second]
    assert result == {"corrected": 0, "review": 2, "noop": 0}


def test_missing_candidate_group_is_review(monkeypatch):
    item = decision()
    wire_batch(monkeypatch, [item])
    monkeypatch.setattr(
        guard.odoo_client, "fetch_payroll_inputs", lambda *_args: ([], [])
    )

    result = guard.run_once(NOW)

    issues = guard.alert.sync_review_task.call_args.args[0]
    assert issues[0].reason_codes == ("missing_candidate_group",)
    assert result == {"corrected": 0, "review": 1, "noop": 0}


def test_alert_failure_is_logged_without_changing_counts(monkeypatch, caplog):
    item = decision()
    wire_batch(monkeypatch, [item])
    reads = iter([fresh(item), fresh(item, duration=item.after_duration)])
    monkeypatch.setattr(
        guard.odoo_client, "fetch_payroll_work_entry", lambda _id: next(reads)
    )
    monkeypatch.setattr(
        guard.alert,
        "sync_review_task",
        MagicMock(side_effect=RuntimeError("task API down")),
    )

    with caplog.at_level(logging.WARNING, logger=guard.__name__):
        result = guard.run_once(NOW)

    assert result == {"corrected": 1, "review": 0, "noop": 0}
    assert "could not sync review task" in caplog.text
    assert "corrected=1 review=0 noop=0 candidates=1" in caplog.text


def test_unsupported_correction_action_fails_closed(monkeypatch):
    item = replace(decision(), action="replace_all")
    wire_batch(monkeypatch, [item])
    monkeypatch.setattr(
        guard.odoo_client, "fetch_payroll_work_entry", lambda _id: fresh(item)
    )

    result = guard.run_once(NOW)

    guard.odoo_client.set_payroll_work_entry_duration.assert_not_called()
    guard.odoo_client.delete_payroll_work_entry.assert_not_called()
    guard.store.append_correction.assert_not_called()
    assert guard.alert.sync_review_task.call_args.args[0][0].reason_codes == (
        "write_failed",
    )
    assert result == {"corrected": 0, "review": 1, "noop": 0}
