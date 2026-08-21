"""Persistence for user-submitted feedback (index linking submitter → Odoo task)."""

from __future__ import annotations

from . import db
from .feedback_image import NormalizedImage


def _clamp_limit(limit, default: int = 100) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, 500))


def insert(
    message: str,
    submitter: str | None = None,
    page_url: str | None = None,
    task_type: str | None = None,
    odoo_task_id: int | None = None,
) -> int:
    """Insert one feedback row; return its new id."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO feedback (submitter, page_url, task_type, odoo_task_id, message) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (submitter, page_url, task_type, odoo_task_id, message),
        )
        return cur.fetchone()["id"]


def create_submission(
    *,
    message: str,
    submitter: str | None,
    page_url: str | None,
    task_type: str,
    status: str = "requested",
    before_image: NormalizedImage | None = None,
) -> int:
    """Atomically save new feedback, its optional image, and Odoo sync intent."""
    if task_type not in {"bug", "feature"}:
        raise ValueError("unsupported feedback type")
    if status != "requested":
        raise ValueError("new feedback must start requested")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO feedback "
            "(submitter, page_url, task_type, message, status, lifecycle_origin, "
            "projection_version, updated_at) "
            "VALUES (%s, %s, %s, %s, 'requested', 'local', 1, now()) RETURNING id",
            (submitter, page_url, task_type, message),
        )
        feedback_id = int(cur.fetchone()["id"])
        if before_image is not None:
            cur.execute(
                "INSERT INTO feedback_images "
                "(feedback_id, role, jpeg_bytes, sha256, byte_length, width, height) "
                "VALUES (%s, 'before', %s, %s, %s, %s, %s)",
                (
                    feedback_id,
                    before_image.jpeg_bytes,
                    before_image.sha256,
                    before_image.byte_length,
                    before_image.width,
                    before_image.height,
                ),
            )
        cur.execute(
            "INSERT INTO feedback_odoo_sync "
            "(feedback_id, desired_version, last_synced_version, due_at, state) "
            "VALUES (%s, 1, 0, now(), 'idle')",
            (feedback_id,),
        )
        return feedback_id


def for_submitter(submitter: str | None, limit: int = 100) -> list[dict]:
    """Return one submitter's feedback rows, newest first."""
    return db.query(
        "SELECT id, created_at, submitter, page_url, task_type, odoo_task_id, message, status "
        "FROM feedback WHERE submitter = %s ORDER BY id DESC LIMIT %s",
        (submitter, _clamp_limit(limit)),
    )
