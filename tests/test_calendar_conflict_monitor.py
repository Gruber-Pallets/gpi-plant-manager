import zira_dashboard.calendar_conflict_monitor as mon


def test_decide_unchanged_set_is_not_changed():
    d = mon.decide({1, 2}, {1, 2})
    assert d == {"changed": False, "added": [], "removed": [], "now_empty": False}


def test_decide_new_employee_is_added():
    d = mon.decide({1, 2, 3}, {1, 2})
    assert d["changed"] is True
    assert d["added"] == [3]
    assert d["removed"] == []
    assert d["now_empty"] is False


def test_decide_resolved_employee_is_removed():
    d = mon.decide({1}, {1, 2})
    assert d["changed"] is True
    assert d["added"] == []
    assert d["removed"] == [2]
    assert d["now_empty"] is False


def test_decide_all_resolved_is_now_empty():
    d = mon.decide(set(), {1, 2})
    assert d["changed"] is True
    assert d["removed"] == [1, 2]
    assert d["now_empty"] is True


def test_decide_empty_to_empty_is_not_changed():
    d = mon.decide(set(), set())
    assert d["changed"] is False
    assert d["now_empty"] is False


from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_state(monkeypatch):
    state = {"odoo_task_id": None, "reported_emp_ids": [], "last_run_at": None}
    monkeypatch.setattr(mon, "_load_state", lambda: dict(state))
    saved = {}

    def _save(odoo_task_id, reported_emp_ids, last_run_at):
        saved.update(
            odoo_task_id=odoo_task_id,
            reported_emp_ids=sorted(reported_emp_ids),
            last_run_at=last_run_at,
        )

    monkeypatch.setattr(mon, "_save_state", _save)
    return state, saved


def _patch_conflicts(monkeypatch, rows):
    monkeypatch.setattr(mon.calendar_conflicts, "current_conflicts", lambda: rows)


def _conflict(odoo_id, name, missing):
    return {
        "name": name, "odoo_id": odoo_id, "cal_name": "M-Th",
        "covered": {0, 1, 2, 3}, "missing": missing, "verdict": "missing_days",
    }


def test_run_once_throttled_when_recent(fake_state, monkeypatch):
    state, saved = fake_state
    state["last_run_at"] = datetime.now(timezone.utc)
    current = MagicMock()
    monkeypatch.setattr(mon.calendar_conflicts, "current_conflicts", current)

    result = mon.run_once()

    assert result == {"skipped": "throttled"}
    current.assert_not_called()
    assert saved == {}


def test_run_once_first_run_creates_task(fake_state, monkeypatch):
    state, saved = fake_state  # last_run_at None -> due
    _patch_conflicts(monkeypatch, [_conflict(7, "Gerardo", {4})])
    monkeypatch.setattr(mon.odoo_client, "ensure_feedback_project", lambda: 3)
    monkeypatch.setattr(mon.odoo_client, "authenticate", lambda: 9)
    create = MagicMock(return_value=111)
    monkeypatch.setattr(mon.odoo_client, "create_feedback_task", create)
    comment = MagicMock()
    monkeypatch.setattr(mon.odoo_client, "post_task_message", comment)
    monkeypatch.setattr(mon.odoo_client, "update_task", MagicMock())

    result = mon.run_once()

    assert result["changed"] is True
    create.assert_called_once()
    comment.assert_called_once()
    assert saved["odoo_task_id"] == 111
    assert saved["reported_emp_ids"] == [7]
    assert saved["last_run_at"] is not None


def test_run_once_unchanged_is_silent(fake_state, monkeypatch):
    state, saved = fake_state
    state["last_run_at"] = datetime.now(timezone.utc) - timedelta(days=8)  # due
    state["reported_emp_ids"] = [7]
    state["odoo_task_id"] = 111
    _patch_conflicts(monkeypatch, [_conflict(7, "Gerardo", {4})])
    create = MagicMock()
    comment = MagicMock()
    monkeypatch.setattr(mon.odoo_client, "create_feedback_task", create)
    monkeypatch.setattr(mon.odoo_client, "post_task_message", comment)
    monkeypatch.setattr(mon.odoo_client, "update_task", MagicMock())

    result = mon.run_once()

    assert result["changed"] is False
    create.assert_not_called()
    comment.assert_not_called()
    assert saved["reported_emp_ids"] == [7]
    assert saved["odoo_task_id"] == 111
    assert saved["last_run_at"] is not None  # gate advanced


def test_run_once_all_resolved_archives_task(fake_state, monkeypatch):
    state, saved = fake_state
    state["last_run_at"] = datetime.now(timezone.utc) - timedelta(days=8)
    state["reported_emp_ids"] = [7]
    state["odoo_task_id"] = 111
    _patch_conflicts(monkeypatch, [])
    comment = MagicMock()
    update = MagicMock()
    monkeypatch.setattr(mon.odoo_client, "post_task_message", comment)
    monkeypatch.setattr(mon.odoo_client, "update_task", update)
    monkeypatch.setattr(mon.odoo_client, "create_feedback_task", MagicMock())

    result = mon.run_once()

    assert result["now_empty"] is True
    comment.assert_called_once()
    update.assert_called_once_with(111, active=False)
    assert saved["odoo_task_id"] is None
    assert saved["reported_emp_ids"] == []


def test_run_once_updates_existing_task_when_set_changes(fake_state, monkeypatch):
    # A task already exists and the conflict set grows (7 -> 7,8): update the
    # existing task body + comment; do NOT create a new task.
    state, saved = fake_state
    state["last_run_at"] = datetime.now(timezone.utc) - timedelta(days=8)  # due
    state["reported_emp_ids"] = [7]
    state["odoo_task_id"] = 111
    _patch_conflicts(monkeypatch, [_conflict(7, "Gerardo", {4}), _conflict(8, "Maria", {4})])
    create = MagicMock()
    update = MagicMock()
    comment = MagicMock()
    monkeypatch.setattr(mon.odoo_client, "create_feedback_task", create)
    monkeypatch.setattr(mon.odoo_client, "update_task", update)
    monkeypatch.setattr(mon.odoo_client, "post_task_message", comment)

    result = mon.run_once()

    assert result["changed"] is True
    assert result["now_empty"] is False
    create.assert_not_called()
    update.assert_called_once()           # description update on the existing task
    assert update.call_args.args[0] == 111
    comment.assert_called_once()
    assert saved["odoo_task_id"] == 111
    assert saved["reported_emp_ids"] == [7, 8]


def test_run_once_recreates_task_when_stored_task_update_fails(fake_state, monkeypatch):
    # The stored task was deleted in Odoo, so updating it raises. The monitor
    # must fall back to creating a fresh task rather than wedging forever.
    state, saved = fake_state
    state["last_run_at"] = datetime.now(timezone.utc) - timedelta(days=8)  # due
    state["reported_emp_ids"] = [7]
    state["odoo_task_id"] = 111
    _patch_conflicts(monkeypatch, [_conflict(7, "Gerardo", {4}), _conflict(8, "Maria", {4})])
    update = MagicMock(side_effect=RuntimeError("task 111 no longer exists"))
    monkeypatch.setattr(mon.odoo_client, "update_task", update)
    monkeypatch.setattr(mon.odoo_client, "ensure_feedback_project", lambda: 3)
    monkeypatch.setattr(mon.odoo_client, "authenticate", lambda: 9)
    create = MagicMock(return_value=222)
    monkeypatch.setattr(mon.odoo_client, "create_feedback_task", create)
    comment = MagicMock()
    monkeypatch.setattr(mon.odoo_client, "post_task_message", comment)

    result = mon.run_once()

    update.assert_called_once()              # attempted the stored task first
    create.assert_called_once()              # then fell back to create
    comment.assert_called_once()
    assert saved["odoo_task_id"] == 222      # new task id persisted
    assert saved["reported_emp_ids"] == [7, 8]
    assert result["changed"] is True
