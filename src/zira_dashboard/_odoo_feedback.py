"""Private Odoo feedback-task operations used by the client facade."""

from __future__ import annotations

import base64
import xmlrpc.client
from typing import Any, Callable


FEEDBACK_PROJECT_NAME = "Plant Manager"
FEEDBACK_STAGES = ("New", "In Progress", "Done", "Rejected")
FEEDBACK_DONE_STAGE = "Done"
FEEDBACK_REJECTED_STAGE = "Rejected"


def find_or_create_feedback_project(execute_fn: Callable[..., Any]) -> int:
    found = execute_fn(
        "project.project",
        "search_read",
        [("name", "=", FEEDBACK_PROJECT_NAME)],
        fields=["id"],
        limit=1,
    )
    if found:
        return found[0]["id"]
    return execute_fn(
        "project.project", "create", {"name": FEEDBACK_PROJECT_NAME}
    )


def ensure_feedback_stages(
    execute_fn: Callable[..., Any], project_id: int
) -> None:
    existing = execute_fn(
        "project.task.type",
        "search_read",
        [("project_ids", "in", [project_id])],
        fields=["name"],
    ) or []
    have = {row["name"] for row in existing}
    for sequence, name in enumerate(FEEDBACK_STAGES):
        if name in have:
            continue
        execute_fn(
            "project.task.type",
            "create",
            {
                "name": name,
                "sequence": sequence,
                "fold": name in (FEEDBACK_DONE_STAGE, FEEDBACK_REJECTED_STAGE),
                "project_ids": [(4, project_id)],
            },
        )


def ensure_feedback_tag(execute_fn: Callable[..., Any], name: str) -> int:
    """Find-or-create a project.tags row by name; return its id."""
    found = execute_fn(
        "project.tags",
        "search_read",
        [("name", "=", name)],
        fields=["id"],
        limit=1,
    )
    if found:
        return found[0]["id"]
    return execute_fn("project.tags", "create", {"name": name})


def create_feedback_task(
    execute_fn: Callable[..., Any],
    project_id: int,
    name: str,
    description_html: str,
    assignee_uid: int,
    tag_id: int | None,
    deadline: str,
) -> int:
    """Create a project.task, with an older-Odoo assignee fallback."""
    base = {
        "name": name,
        "project_id": project_id,
        "description": description_html,
        "date_deadline": deadline,
    }
    if tag_id:
        base["tag_ids"] = [(6, 0, [tag_id])]
    try:
        return execute_fn(
            "project.task",
            "create",
            dict(base, user_ids=[(6, 0, [assignee_uid])]),
        )
    except xmlrpc.client.Fault as fault:
        if "user_ids" not in (fault.faultString or ""):
            raise
        return execute_fn(
            "project.task", "create", dict(base, user_id=assignee_uid)
        )


def update_task(
    execute_fn: Callable[..., Any], task_id: int, **fields: Any
) -> None:
    """Write fields on a project.task (e.g. description=..., active=False)."""
    execute_fn("project.task", "write", [task_id], fields)


def post_task_message(
    execute_fn: Callable[..., Any], task_id: int, body: str
) -> None:
    """Post a message to a project.task's chatter."""
    execute_fn("project.task", "message_post", [task_id], body=body)


def add_task_attachment(
    execute_fn: Callable[..., Any],
    task_id: int,
    filename: str,
    mimetype: str | None,
    raw_bytes: bytes,
) -> int:
    """Attach a file to a project.task as an ir.attachment."""
    return execute_fn(
        "ir.attachment",
        "create",
        {
            "name": filename,
            "datas": base64.b64encode(raw_bytes).decode("ascii"),
            "res_model": "project.task",
            "res_id": task_id,
            "mimetype": mimetype or "application/octet-stream",
        },
    )


def fetch_task_stage_names(
    execute_fn: Callable[..., Any], task_ids
) -> dict[int, str | None]:
    """Return {task_id: stage name} for the given project.task ids."""
    ids = [int(task_id) for task_id in task_ids if task_id]
    if not ids:
        return {}
    rows = execute_fn(
        "project.task", "read", ids, fields=["id", "stage_id"]
    ) or []
    out: dict[int, str | None] = {}
    for row in rows:
        stage = row.get("stage_id")
        out[row["id"]] = (
            stage[1]
            if isinstance(stage, (list, tuple)) and len(stage) > 1
            else None
        )
    return out


def feedback_status_bucket(stage_name: str | None) -> str:
    """Collapse an Odoo stage name to open / done / rejected."""
    if stage_name == FEEDBACK_DONE_STAGE:
        return "done"
    if stage_name == FEEDBACK_REJECTED_STAGE:
        return "rejected"
    return "open"
