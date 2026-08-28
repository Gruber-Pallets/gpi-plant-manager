from unittest.mock import MagicMock

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
