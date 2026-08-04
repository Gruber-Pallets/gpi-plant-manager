from contextlib import contextmanager
from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest

import zira_dashboard.payroll_work_entry_alert as alert
from zira_dashboard.payroll_work_entry_rules import Decision


NOW = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)


def issue(employee_name="Darren Donahue", reason="payroll_overtime_mismatch"):
    return Decision(
        kind="review",
        employee_id=9,
        employee_name=employee_name,
        work_date=date(2026, 7, 24),
        reason_codes=(reason,),
        action=None,
        work_entry_id=8512,
        attendance_id=3803,
        before_duration=0.5,
        after_duration=None,
        attendance_regular=0.0,
        attendance_overtime=8.5228,
        work_regular=0.5,
        work_overtime=8.0,
    )


def patch_lock(monkeypatch, events=None):
    calls = MagicMock()

    @contextmanager
    def recording_lock():
        calls()
        if events is not None:
            events.append("lock-enter")
        try:
            yield
        finally:
            if events is not None:
                events.append("lock-exit")

    monkeypatch.setattr(alert.store, "monitor_lock", recording_lock)
    return calls


def patch_state(monkeypatch, task_id=None, keys=(), events=None):
    def load():
        if events is not None:
            events.append("state-load")
        return {"odoo_task_id": task_id, "reported_issue_keys": list(keys)}

    save = MagicMock(
        side_effect=(lambda *_args: events.append("state-save")) if events is not None else None
    )
    monkeypatch.setattr(alert.store, "load_monitor_state", load)
    monkeypatch.setattr(alert.store, "save_monitor_state", save)
    return save


def patch_odoo(
    monkeypatch,
    *,
    created_id=222,
    update_error=None,
    task_stages=None,
    events=None,
):
    def record(name, result=None, error=None):
        def operation(*_args, **_kwargs):
            if events is not None:
                events.append(name)
            if error is not None:
                raise error
            return result

        return MagicMock(side_effect=operation)

    monkeypatch.setattr(
        alert.odoo_client, "ensure_feedback_project", record("project", 3)
    )
    monkeypatch.setattr(alert.odoo_client, "authenticate", record("authenticate", 9))
    create = record("create", created_id)
    update = record("update", error=update_error)
    comment = record("comment")
    monkeypatch.setattr(alert.odoo_client, "create_feedback_task", create)
    monkeypatch.setattr(alert.odoo_client, "update_task", update)
    monkeypatch.setattr(alert.odoo_client, "post_task_message", comment)
    stages = {111: "New"} if task_stages is None else task_stages
    monkeypatch.setattr(
        alert.odoo_client,
        "fetch_task_stage_names",
        record("fetch", stages),
    )
    return create, update, comment


def test_first_issue_creates_one_task_and_saves_keys(monkeypatch):
    current = issue()
    patch_lock(monkeypatch)
    save = patch_state(monkeypatch)
    create, update, comment = patch_odoo(monkeypatch)

    result = alert.sync_review_task([current], NOW)

    create.assert_called_once_with(
        project_id=3,
        name="Payroll work entries need review",
        description_html=alert._build_task_body([current]),
        assignee_uid=9,
        tag_id=None,
        deadline="2026-08-10",
    )
    update.assert_not_called()
    comment.assert_called_once()
    save.assert_called_once_with(222, [current.issue_key], NOW)
    assert result == {"changed": True, "task_id": 222, "count": 1}


def test_same_issue_set_is_silent(monkeypatch):
    current = issue()
    patch_lock(monkeypatch)
    save = patch_state(monkeypatch, task_id=111, keys=[current.issue_key])
    create, update, comment = patch_odoo(monkeypatch)

    result = alert.sync_review_task([current], NOW)

    create.assert_not_called()
    update.assert_not_called()
    comment.assert_not_called()
    save.assert_not_called()
    assert result == {"changed": False, "task_id": 111, "count": 1}


def test_changed_issue_set_updates_existing_task(monkeypatch):
    current = issue(reason="non_draft_work_entry")
    patch_lock(monkeypatch)
    save = patch_state(monkeypatch, task_id=111, keys=[issue().issue_key])
    create, update, comment = patch_odoo(monkeypatch)

    alert.sync_review_task([current], NOW)

    create.assert_not_called()
    update.assert_called_once()
    assert update.call_args.args[0] == 111
    assert "description" in update.call_args.kwargs
    comment.assert_called_once()
    save.assert_called_once_with(111, [current.issue_key], NOW)


def test_deleted_stored_task_is_recreated(monkeypatch):
    current = issue(reason="non_draft_work_entry")
    patch_lock(monkeypatch)
    save = patch_state(monkeypatch, task_id=111, keys=[issue().issue_key])
    create, update, comment = patch_odoo(
        monkeypatch, created_id=333, update_error=RuntimeError("task deleted")
    )

    alert.sync_review_task([current], NOW)

    update.assert_called_once()
    create.assert_called_once()
    comment.assert_called_once()
    save.assert_called_once_with(333, [current.issue_key], NOW)


def test_unchanged_issue_recreates_missing_stored_task(monkeypatch):
    current = issue()
    patch_lock(monkeypatch)
    save = patch_state(monkeypatch, task_id=111, keys=[current.issue_key])
    create, update, comment = patch_odoo(
        monkeypatch, created_id=444, task_stages={}
    )

    alert.sync_review_task([current], NOW)

    update.assert_not_called()
    create.assert_called_once()
    comment.assert_called_once()
    save.assert_called_once_with(444, [current.issue_key], NOW)


def test_empty_issue_set_archives_existing_task(monkeypatch):
    previous = issue()
    patch_lock(monkeypatch)
    save = patch_state(monkeypatch, task_id=111, keys=[previous.issue_key])
    create, update, comment = patch_odoo(monkeypatch)

    result = alert.sync_review_task([], NOW)

    create.assert_not_called()
    comment.assert_called_once_with(
        111, "✅ All payroll Work Entry review items resolved."
    )
    update.assert_called_once_with(111, active=False)
    save.assert_called_once_with(None, [], NOW)
    assert result == {"changed": True, "task_id": None, "count": 0}


def test_empty_issue_set_without_task_clears_stale_keys(monkeypatch):
    patch_lock(monkeypatch)
    save = patch_state(monkeypatch, keys=[issue().issue_key])
    create, update, comment = patch_odoo(monkeypatch)

    result = alert.sync_review_task([], NOW)

    create.assert_not_called()
    update.assert_not_called()
    comment.assert_not_called()
    save.assert_called_once_with(None, [], NOW)
    assert result == {"changed": True, "task_id": None, "count": 0}


def test_task_body_escapes_dynamic_values_and_lists_totals():
    body = alert._build_task_body(
        [issue(employee_name="<Dale & Co>", reason="<unknown & reason>")]
    )

    assert "&lt;Dale &amp; Co&gt;" in body
    assert "<Dale & Co>" not in body
    assert "&lt;unknown &amp; reason&gt;" in body
    assert "<unknown & reason>" not in body
    assert "2026-07-24" in body
    assert "Attendance regular: 0.0000" in body
    assert "Payroll regular: 0.5000" in body
    assert "Attendance overtime: 8.5228" in body
    assert "Payroll overtime: 8.0000" in body


def test_task_body_explains_known_and_invalid_numeric_reasons():
    mismatch = alert._build_task_body([issue()])
    invalid = alert._build_task_body([issue(reason="invalid_numeric_data")])

    assert "Payroll and Attendance overtime disagree" in mismatch
    assert "some hour details are missing or are not real numbers" in invalid


def test_task_body_sorts_rows_without_changing_input():
    later_name = issue(employee_name="Zoe")
    earlier_name = Decision(
        **{**later_name.__dict__, "employee_id": 2, "employee_name": "amy"}
    )
    issues = [later_name, earlier_name]

    body = alert._build_task_body(issues)

    assert body.index("amy") < body.index("Zoe")
    assert issues == [later_name, earlier_name]


def test_sync_deduplicates_and_sorts_issue_keys(monkeypatch):
    first = issue(employee_name="Zoe", reason="write_failed")
    second = Decision(
        **{
            **first.__dict__,
            "employee_id": 2,
            "employee_name": "Amy",
            "reason_codes": ("fresh_read_failed",),
        }
    )
    patch_lock(monkeypatch)
    save = patch_state(monkeypatch)
    patch_odoo(monkeypatch)

    result = alert.sync_review_task([first, second, first], NOW)

    save.assert_called_once_with(
        222, sorted([first.issue_key, second.issue_key]), NOW
    )
    assert result["count"] == 2


@pytest.mark.parametrize(
    "bad_issue",
    [
        object(),
        Decision(**{**issue().__dict__, "kind": "noop"}),
        Decision(**{**issue().__dict__, "kind": "correct", "action": "duration_update"}),
    ],
)
def test_sync_rejects_values_that_are_not_review_decisions(monkeypatch, bad_issue):
    lock = patch_lock(monkeypatch)
    load = MagicMock()
    monkeypatch.setattr(alert.store, "load_monitor_state", load)

    with pytest.raises(ValueError, match="review Decision"):
        alert.sync_review_task([bad_issue], NOW)

    lock.assert_not_called()
    load.assert_not_called()


def test_one_lock_contains_state_load_all_odoo_work_and_state_save(monkeypatch):
    events = []
    current = issue()
    lock = patch_lock(monkeypatch, events)
    patch_state(monkeypatch, events=events)
    patch_odoo(monkeypatch, events=events)

    alert.sync_review_task([current], NOW)

    lock.assert_called_once_with()
    assert events == [
        "lock-enter",
        "state-load",
        "project",
        "authenticate",
        "create",
        "comment",
        "state-save",
        "lock-exit",
    ]


def test_lock_releases_and_state_is_not_saved_when_odoo_action_raises(monkeypatch):
    events = []
    current = issue()
    patch_lock(monkeypatch, events)
    save = patch_state(monkeypatch, events=events)
    _create, _update, comment = patch_odoo(monkeypatch, events=events)
    comment.side_effect = RuntimeError("Odoo unavailable")

    with pytest.raises(RuntimeError, match="Odoo unavailable"):
        alert.sync_review_task([current], NOW)

    save.assert_not_called()
    assert events[0] == "lock-enter"
    assert events[-1] == "lock-exit"


def test_state_is_not_saved_when_archive_fails(monkeypatch):
    patch_lock(monkeypatch)
    save = patch_state(monkeypatch, task_id=111, keys=[issue().issue_key])
    _create, update, _comment = patch_odoo(monkeypatch)
    update.side_effect = RuntimeError("archive failed")

    with pytest.raises(RuntimeError, match="archive failed"):
        alert.sync_review_task([], NOW)

    save.assert_not_called()
