"""Private Odoo feedback-task operations used by the client facade."""

from __future__ import annotations

import base64
import xmlrpc.client
from typing import Any, Callable


FEEDBACK_PROJECT_NAME = "Plant Manager"
FEEDBACK_STAGES = ("New", "In Progress", "Done", "Rejected")
FEEDBACK_DONE_STAGE = "Done"
FEEDBACK_REJECTED_STAGE = "Rejected"


class OdooUserPayloadError(RuntimeError):
    """Odoo returned a user lookup row that cannot be trusted."""


def find_active_users_by_login(
    execute_fn: Callable[..., Any], login: str, limit: int = 2
) -> list[dict]:
    """Return at most two active users echoing one normalized email."""
    if not isinstance(login, str):
        raise ValueError("login must be a normalized email and limit must be 2")
    normalized = login.strip().casefold()
    if login != normalized or "@" not in normalized or limit != 2:
        raise ValueError("login must be a normalized email and limit must be 2")
    rows = execute_fn(
        "res.users",
        "search_read",
        [("active", "=", True), ("login", "=ilike", normalized)],
        fields=["id", "login"],
        limit=limit,
    )
    if rows is None:
        rows = []
    if not isinstance(rows, list) or len(rows) > limit:
        raise OdooUserPayloadError("Odoo user payload was malformed")
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            raise OdooUserPayloadError("Odoo user payload row was malformed")
        user_id = row.get("id")
        echoed_login = row.get("login")
        if (
            isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or user_id <= 0
            or not isinstance(echoed_login, str)
        ):
            raise OdooUserPayloadError("Odoo user payload row was malformed")
        if echoed_login.casefold() == normalized:
            out.append({"id": user_id, "login": echoed_login})
    return out


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


def find_feedback_task(
    execute_fn: Callable[..., Any], project_id: int, name: str
) -> int | None:
    """Return the newest active exact-name task in a feedback project."""
    rows = execute_fn(
        "project.task",
        "search_read",
        [
            ("project_id", "=", project_id),
            ("name", "=", name),
            ("active", "=", True),
        ],
        fields=["id"],
        order="id desc",
        limit=1,
    ) or []
    return int(rows[0]["id"]) if rows else None


def find_feedback_task_ids(
    execute_fn: Callable[..., Any], project_id: int, name: str
) -> list[int]:
    rows = execute_fn(
        "project.task",
        "search_read",
        [("project_id", "=", project_id), ("name", "=", name)],
        fields=["id"],
        order="id asc",
        limit=2,
        context={"active_test": False},
    ) or []
    return [int(row["id"]) for row in rows]


def find_feedback_attachment_ids(
    execute_fn: Callable[..., Any], task_id: int, name: str
) -> list[int]:
    rows = execute_fn(
        "ir.attachment",
        "search_read",
        [
            ("res_model", "=", "project.task"),
            ("res_id", "=", task_id),
            ("name", "=", name),
        ],
        fields=["id"],
        order="id asc",
        limit=2,
        context={"active_test": False},
    ) or []
    return [int(row["id"]) for row in rows]


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


def close_task(execute_fn: Callable[..., Any], task_id: int) -> None:
    """Archive one project.task without implying any payroll state."""
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
        raise ValueError("task_id must be a positive integer")
    execute_fn("project.task", "write", [task_id], {"active": False})


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
