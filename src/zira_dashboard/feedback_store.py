"""Persistence for user-submitted feedback."""

from __future__ import annotations

from . import db


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), 500))


def insert(
    message: str,
    submitter: str | None = None,
    page_url: str | None = None,
    category: str | None = None,
) -> int:
    """Insert one feedback row; return its new id."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO feedback (submitter, page_url, category, message) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (submitter, page_url, category, message),
        )
        return cur.fetchone()["id"]


def recent(limit: int = 200) -> list[dict]:
    """Return the most recent feedback rows, newest first."""
    return db.query(
        "SELECT id, created_at, submitter, page_url, category, message "
        "FROM feedback ORDER BY id DESC LIMIT %s",
        (_clamp_limit(limit),),
    )
