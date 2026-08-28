from unittest.mock import MagicMock
import xmlrpc.client

import zira_dashboard.odoo_client as oc


def test_update_task_writes_fields(monkeypatch):
    execute = MagicMock(return_value=True)
    monkeypatch.setattr(oc, "execute", execute)
    oc.update_task(55, active=False, description="<p>x</p>")
    execute.assert_called_once_with("project.task", "write", [55], {"active": False, "description": "<p>x</p>"})


def test_close_task_only_archives_the_task(monkeypatch):
    execute = MagicMock(return_value=True)
    monkeypatch.setattr(oc, "execute", execute)

    oc.close_task(55)

    execute.assert_called_once_with(
        "project.task", "write", [55], {"active": False}
    )


def test_post_task_message_posts_to_chatter(monkeypatch):
    execute = MagicMock(return_value=1)
    monkeypatch.setattr(oc, "execute", execute)
    oc.post_task_message(55, "hello")
    execute.assert_called_once_with("project.task", "message_post", [55], body="hello")


def test_find_active_feedback_task_ids_preserves_zero_one_or_two_exact_matches(
    monkeypatch,
):
    execute = MagicMock(return_value=[{"id": 55}, {"id": 56}])
    monkeypatch.setattr(oc, "execute", execute)
    name = "[GPI-PM-PTO-41] Review Ana — 2026-08-20"
    assert oc.find_active_feedback_task_ids(7, name) == [55, 56]
    execute.assert_called_once_with(
        "project.task",
        "search_read",
        [
            ("project_id", "=", 7),
            ("name", "=", name),
            ("active", "=", True),
        ],
        fields=["id"],
        order="id asc",
        limit=2,
    )


def test_update_feedback_task_assigns_only_wendy_and_refreshes_case(monkeypatch):
    execute = MagicMock(return_value=True)
    monkeypatch.setattr(oc, "execute", execute)
    oc.update_feedback_task(
        55,
        description_html="<p>case</p>",
        assignee_uid=17,
        deadline="2026-08-31",
    )
    execute.assert_called_once_with(
        "project.task",
        "write",
        [55],
        {
            "description": "<p>case</p>",
            "date_deadline": "2026-08-31",
            "active": True,
            "user_ids": [(6, 0, [17])],
        },
    )


def test_update_feedback_task_falls_back_to_single_user_id(monkeypatch):
    execute = MagicMock(
        side_effect=[xmlrpc.client.Fault(1, "unknown field user_ids"), True]
    )
    monkeypatch.setattr(oc, "execute", execute)
    oc.update_feedback_task(
        55,
        description_html="<p>case</p>",
        assignee_uid=17,
        deadline="2026-08-31",
    )
    assert execute.call_args_list[1].args == (
        "project.task",
        "write",
        [55],
        {
            "description": "<p>case</p>",
            "date_deadline": "2026-08-31",
            "active": True,
            "user_id": 17,
        },
    )
