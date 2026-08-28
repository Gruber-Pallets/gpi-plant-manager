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


def test_find_active_feedback_project_ids_is_exact_bounded_and_noncreating(monkeypatch):
    execute = MagicMock(return_value=[{"id": 7}, {"id": 8}])
    monkeypatch.setattr(oc, "execute", execute)

    assert oc.find_active_feedback_project_ids("Plant Manager") == [7, 8]
    execute.assert_called_once_with(
        "project.project",
        "search_read",
        [("name", "=", "Plant Manager"), ("active", "=", True)],
        fields=["id"],
        order="id asc",
        limit=2,
    )


def test_fetch_feedback_task_identity_includes_archived_exact_fields(monkeypatch):
    execute = MagicMock(
        return_value=[{
            "id": 501,
            "name": "[GPI-PM-PTO-41] Review Ana — 2026-08-20",
            "project_id": [7, "Plant Manager"],
            "active": False,
        }]
    )
    monkeypatch.setattr(oc, "execute", execute)

    assert oc.fetch_feedback_task_identity(501) == {
        "id": 501,
        "name": "[GPI-PM-PTO-41] Review Ana — 2026-08-20",
        "project_id": 7,
        "active": False,
    }
    execute.assert_called_once_with(
        "project.task",
        "search_read",
        [("id", "=", 501)],
        fields=["id", "name", "project_id", "active"],
        limit=2,
        context={"active_test": False},
    )


def test_review_create_primitives_each_dispatch_exactly_one_mutation(monkeypatch):
    execute = MagicMock(side_effect=[501, 502])
    monkeypatch.setattr(oc, "execute", execute)
    values = dict(
        project_id=7,
        name="case",
        description_html="<p>case</p>",
        assignee_uid=17,
        tag_id=None,
        deadline="2026-08-31",
    )

    assert oc.create_review_task_user_ids(**values) == 501
    assert oc.create_review_task_user_id(**values) == 502
    assert execute.call_count == 2
    assert "user_ids" in execute.call_args_list[0].args[2]
    assert "user_id" in execute.call_args_list[1].args[2]


def test_review_update_primitives_restore_exact_identity_with_one_write_each(monkeypatch):
    execute = MagicMock(side_effect=[True, True])
    monkeypatch.setattr(oc, "execute", execute)
    values = dict(
        task_id=501,
        project_id=7,
        name="case",
        description_html="<p>case</p>",
        assignee_uid=17,
        deadline="2026-08-31",
    )

    oc.update_review_task_user_ids(**values)
    oc.update_review_task_user_id(**values)
    assert execute.call_count == 2
    assert execute.call_args_list[0].args[3]["name"] == "case"
    assert execute.call_args_list[0].args[3]["project_id"] == 7
    assert execute.call_args_list[0].args[3]["user_ids"] == [(6, 0, [17])]
    assert execute.call_args_list[1].args[3]["user_id"] == 17


def test_find_task_resolution_messages_uses_bounded_marker_lookup(monkeypatch):
    execute = MagicMock(
        return_value=[{"id": 901, "body": "resolved gpi-pm-absence-pto-41"}]
    )
    monkeypatch.setattr(oc, "execute", execute)

    assert oc.find_task_message_ids(501, "gpi-pm-absence-pto-41") == [901]
    execute.assert_called_once_with(
        "mail.message",
        "search_read",
        [
            ("model", "=", "project.task"),
            ("res_id", "=", 501),
            ("body", "ilike", "gpi-pm-absence-pto-41"),
        ],
        fields=["id", "body"],
        order="id asc",
        limit=2,
    )
