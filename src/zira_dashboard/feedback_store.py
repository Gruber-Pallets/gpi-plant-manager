"""Persistence for user-submitted feedback (index linking submitter → Odoo task)."""

from __future__ import annotations

from datetime import datetime

from . import db
from .feedback_image import NormalizedImage


class InvalidTransition(ValueError):
    pass


_TRANSITIONS = {
    "requested": {"in_progress", "completed", "declined"},
    "in_progress": {"completed", "declined"},
    "completed": set(),
    "declined": set(),
}


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


def for_admin(limit: int = 200) -> list[dict]:
    """Return local feedback with its current durable sync state."""
    return db.query(
        "SELECT f.id, f.created_at, f.submitter, f.page_url, f.task_type, "
        "f.message, f.status, f.finished_at, f.finished_by, f.resolution_note, "
        "f.projection_version, s.state AS sync_state, s.desired_version, "
        "s.last_synced_version, "
        "EXISTS (SELECT 1 FROM feedback_images bi "
        "WHERE bi.feedback_id = f.id AND bi.role = 'before') AS has_before_image, "
        "EXISTS (SELECT 1 FROM feedback_images ai "
        "WHERE ai.feedback_id = f.id AND ai.role = 'after') AS has_after_image "
        "FROM feedback f LEFT JOIN feedback_odoo_sync s ON s.feedback_id = f.id "
        "WHERE f.lifecycle_origin = 'local' ORDER BY f.id DESC LIMIT %s",
        (_clamp_limit(limit, default=200),),
    )


def transition(
    *,
    feedback_id: int,
    status: str,
    actor: str,
    resolution_note: str | None,
    after_image: NormalizedImage | None,
    now: datetime,
) -> int:
    """Apply one allowed local lifecycle change and return its new version."""
    clean_actor = actor.strip().lower()
    clean_note = (resolution_note or "").strip()
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, lifecycle_origin, projection_version "
            "FROM feedback WHERE id = %s FOR UPDATE",
            (feedback_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(feedback_id)
        if row["lifecycle_origin"] != "local":
            raise InvalidTransition("feedback is not locally managed")

        current = row["status"]
        if status not in _TRANSITIONS.get(current, set()):
            raise InvalidTransition("feedback is terminal or transition is invalid")

        terminal = status in {"completed", "declined"}
        if terminal and (not clean_actor or not clean_note):
            raise InvalidTransition(
                "terminal feedback requires an actor and resolution note"
            )
        if after_image is not None and not terminal:
            raise InvalidTransition("after image is allowed only for terminal feedback")

        version = int(row["projection_version"]) + 1
        cur.execute(
            "UPDATE feedback SET status = %s, lifecycle_origin = 'local', "
            "finished_at = %s, finished_by = %s, resolution_note = %s, "
            "projection_version = %s, updated_at = %s WHERE id = %s",
            (
                status,
                now if terminal else None,
                clean_actor if terminal else None,
                clean_note if terminal else None,
                version,
                now,
                feedback_id,
            ),
        )
        if after_image is not None:
            cur.execute(
                "INSERT INTO feedback_images "
                "(feedback_id, role, jpeg_bytes, sha256, byte_length, width, height) "
                "VALUES (%s, 'after', %s, %s, %s, %s, %s) "
                "ON CONFLICT (feedback_id, role) DO UPDATE SET "
                "jpeg_bytes = EXCLUDED.jpeg_bytes, sha256 = EXCLUDED.sha256, "
                "byte_length = EXCLUDED.byte_length, width = EXCLUDED.width, "
                "height = EXCLUDED.height, created_at = now()",
                (
                    feedback_id,
                    after_image.jpeg_bytes,
                    after_image.sha256,
                    after_image.byte_length,
                    after_image.width,
                    after_image.height,
                ),
            )
        cur.execute(
            "UPDATE feedback_odoo_sync SET desired_version = %s, due_at = %s, "
            "state = CASE WHEN state IN ('in_flight', 'quarantined') "
            "THEN state ELSE 'idle' END, "
            "updated_at = %s WHERE feedback_id = %s RETURNING feedback_id",
            (version, now, now, feedback_id),
        )
        if cur.fetchone() is None:
            raise InvalidTransition("feedback sync state is missing")
        return version
