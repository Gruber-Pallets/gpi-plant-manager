"""Persistence layer for pinned_dashboards.

Tracks which dashboards (built-in VS, built-in per-WC, custom) the user
has favorited for the Dashboards sub-nav. `kind` + `ref` together
identify a dashboard:
  - kind='vs_recycling',     ref=''   (Recycling VS)
  - kind='vs_new',           ref=''   (New VS)
  - kind='vs_work_centers',  ref=''   (Work Centers status board)
  - kind='wc',               ref=<WC name>
  - kind='custom',           ref=<custom_dashboards.slug>

Seed on first boot pins the three built-in VS-family dashboards.
Deleted seeds stay deleted across redeploys (same pattern as
tv_displays_store).
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


def pin(kind: str, ref: str) -> None:
    """Insert a pin. Idempotent — duplicate inserts no-op via ON CONFLICT."""
    from . import db
    if kind not in ("vs_recycling", "vs_new", "vs_work_centers", "wc", "custom"):
        raise ValueError(f"invalid kind: {kind}")
    if not isinstance(ref, str):
        raise ValueError("ref must be a string")
    db.execute(
        "INSERT INTO pinned_dashboards (kind, ref, sort_order) "
        "VALUES (%s, %s, "
        "  COALESCE((SELECT MAX(sort_order) + 1 FROM pinned_dashboards), 0)"
        ") "
        "ON CONFLICT (kind, ref) DO NOTHING",
        (kind, ref),
    )


def unpin(kind: str, ref: str) -> None:
    from . import db
    db.execute(
        "DELETE FROM pinned_dashboards WHERE kind = %s AND ref = %s",
        (kind, ref),
    )


def is_pinned(kind: str, ref: str) -> bool:
    from . import db
    rows = db.query(
        "SELECT 1 FROM pinned_dashboards WHERE kind = %s AND ref = %s",
        (kind, ref),
    )
    return bool(rows)


def list_pins() -> list[dict]:
    """All pins ordered by (sort_order ASC, created_at ASC)."""
    from . import db
    rows = db.query(
        "SELECT kind, ref, sort_order "
        "FROM pinned_dashboards "
        "ORDER BY sort_order ASC, created_at ASC"
    )
    return [
        {"kind": r["kind"], "ref": r["ref"], "sort_order": int(r["sort_order"])}
        for r in rows
    ]


def seed_defaults_if_empty() -> None:
    """Pin Recycling VS + New VS + Work Centers on first boot. No-op on
    a non-empty table.

    Deleted seeds stay deleted across redeploys.
    """
    from . import db
    existing = db.query("SELECT 1 FROM pinned_dashboards LIMIT 1")
    if existing:
        return
    db.execute(
        "INSERT INTO pinned_dashboards (kind, ref, sort_order) VALUES "
        "  ('vs_recycling', '', 0), "
        "  ('vs_new', '', 1), "
        "  ('vs_work_centers', '', 2)"
    )
    _log.info("pinned_dashboards seeded 3 default pins (Recycling VS + New VS + Work Centers)")
