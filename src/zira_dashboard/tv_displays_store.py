"""Persistence layer for TV display registry.

Each row is a physical TV in the plant: a friendly name, which dashboard
it shows (kind = vs_recycling / vs_new / vs_recycling_leaderboard / wc,
plus wc_name when kind=wc),
and a light/dark theme. The /tv/{slug} route looks up the row and
dispatches to the appropriate render helper with the row's theme.

Seed list inserts on first boot. New seeded display types can also be
backfilled once on existing installs with an app_settings marker, so deleting
one after the backfill stays respected across redeploys.
"""
from __future__ import annotations

import logging

from .wc_dashboard_data import slug_for_wc

_log = logging.getLogger(__name__)


_VALID_KINDS = ("vs_recycling", "vs_new", "vs_recycling_leaderboard", "wc")
_RECYCLING_LEADERBOARD_SEED_MARKER = "tv_displays:seed_recycling_leaderboard_v1"
_RECYCLING_LEADERBOARD_SEED = (
    "Recycling-leaderboard",
    "vs_recycling_leaderboard",
    None,
)


# (name, kind, wc_name) — order matters for sort_order assignment at seed.
_SEED_LIST = [
    ("Recycling", "vs_recycling", None),
    ("New",       "vs_new",        None),
    _RECYCLING_LEADERBOARD_SEED,
    ("Junior 2",     "wc",            "Junior 2"),
    ("Repair 1",     "wc",            "Repair 1"),
    ("Repair 2",     "wc",            "Repair 2"),
    ("Repair 3",     "wc",            "Repair 3"),
    ("Dismantler 1", "wc",            "Dismantler 1"),
    ("Dismantler 2", "wc",            "Dismantler 2"),
    ("Dismantler 3", "wc",            "Dismantler 3"),
    ("Dismantler 4", "wc",            "Dismantler 4"),
]


def _unique_slug(base: str, *, exclude_id: int | None = None) -> str:
    """Return `base` if no other row owns it; else suffix -2, -3, ...

    `exclude_id` lets a row keep its own slug when saving with no name change.
    """
    from . import db
    candidate = base
    n = 2
    while True:
        rows = db.query(
            "SELECT id FROM tv_displays WHERE slug = %s",
            (candidate,),
        )
        if not rows or (exclude_id is not None and all(r["id"] == exclude_id for r in rows)):
            return candidate
        candidate = f"{base}-{n}"
        n += 1


def save(
    *,
    name: str,
    kind: str,
    wc_name: str | None,
    theme: str,
    id: int | None = None,
) -> dict:
    """Insert a new row or update an existing one (when `id` given).

    Slug is derived from `name` via `slug_for_wc`; on collision the
    store appends `-2`, `-3`, etc. (skipping the row's own slug when
    updating). Returns the saved row as a dict.
    """
    from . import db
    slug_base = slug_for_wc(name)
    if not slug_base:
        raise ValueError("name must produce a non-empty slug")
    if theme not in ("light", "dark"):
        theme = "dark"
    if kind not in _VALID_KINDS:
        raise ValueError(f"invalid kind: {kind}")
    slug = _unique_slug(slug_base, exclude_id=id)
    if id is None:
        rows = db.query(
            "INSERT INTO tv_displays (name, slug, kind, wc_name, theme) "
            "VALUES (%s, %s, %s, %s, %s) "
            "RETURNING id, name, slug, kind, wc_name, theme, sort_order",
            (name, slug, kind, wc_name, theme),
        )
    else:
        rows = db.query(
            "UPDATE tv_displays SET "
            "  name = %s, slug = %s, kind = %s, wc_name = %s, "
            "  theme = %s, updated_at = now() "
            "WHERE id = %s "
            "RETURNING id, name, slug, kind, wc_name, theme, sort_order",
            (name, slug, kind, wc_name, theme, id),
        )
    if not rows:
        raise LookupError(f"no tv_displays row with id={id}")
    return _hydrate(rows[0])


def set_theme(id: int, theme: str) -> None:
    """Update only the theme column. No slug re-derivation."""
    from . import db
    if theme not in ("light", "dark"):
        raise ValueError(f"invalid theme: {theme}")
    db.execute(
        "UPDATE tv_displays SET theme = %s, updated_at = now() WHERE id = %s",
        (theme, id),
    )


def delete(id: int) -> None:
    from . import db
    db.execute("DELETE FROM tv_displays WHERE id = %s", (id,))


def by_slug(slug: str) -> dict | None:
    from . import db
    rows = db.query(
        "SELECT id, name, slug, kind, wc_name, theme, sort_order "
        "FROM tv_displays WHERE slug = %s",
        (slug,),
    )
    return _hydrate(rows[0]) if rows else None


def list_displays() -> list[dict]:
    """All rows ordered by (sort_order ASC, name ASC). Stable for UI."""
    from . import db
    rows = db.query(
        "SELECT id, name, slug, kind, wc_name, theme, sort_order "
        "FROM tv_displays ORDER BY sort_order ASC, lower(name) ASC"
    )
    return [_hydrate(r) for r in rows]


def seed_defaults_if_empty() -> None:
    """Insert the default seed list and one-time backfills.

    Rows whose `wc_name` is not present in `staffing.LOCATIONS` are
    skipped with a warning log so a partial WC roster doesn't fail boot.
    """
    from . import app_settings, db, staffing
    existing = db.query("SELECT 1 FROM tv_displays LIMIT 1")
    if existing:
        _backfill_recycling_leaderboard_seed()
        return
    valid_wc_names = {loc.name for loc in staffing.LOCATIONS}
    inserted = 0
    for idx, (name, kind, wc_name) in enumerate(_SEED_LIST):
        if kind == "wc" and wc_name not in valid_wc_names:
            _log.warning(
                "tv_displays seed skipping %s — not in staffing.LOCATIONS", name
            )
            continue
        slug = _unique_slug(slug_for_wc(name))
        db.execute(
            "INSERT INTO tv_displays (name, slug, kind, wc_name, theme, sort_order) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (name, slug, kind, wc_name, "dark", idx),
        )
        inserted += 1
    app_settings.set_setting(_RECYCLING_LEADERBOARD_SEED_MARKER, {"done": True})
    _log.info("tv_displays seeded %d default rows", inserted)


def _backfill_recycling_leaderboard_seed() -> None:
    from . import app_settings, db

    if app_settings.get_setting(_RECYCLING_LEADERBOARD_SEED_MARKER):
        return
    existing = db.query(
        "SELECT 1 FROM tv_displays WHERE kind = %s LIMIT 1",
        ("vs_recycling_leaderboard",),
    )
    if not existing:
        sort_rows = db.query("SELECT COALESCE(MAX(sort_order), -1) AS sort_order FROM tv_displays")
        sort_order = int(sort_rows[0]["sort_order"]) + 1 if sort_rows else 0
        name, kind, wc_name = _RECYCLING_LEADERBOARD_SEED
        slug = _unique_slug(slug_for_wc(name))
        db.execute(
            "INSERT INTO tv_displays (name, slug, kind, wc_name, theme, sort_order) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (name, slug, kind, wc_name, "dark", sort_order),
        )
    app_settings.set_setting(_RECYCLING_LEADERBOARD_SEED_MARKER, {"done": True})


def _hydrate(row) -> dict:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "slug": row["slug"],
        "kind": row["kind"],
        "wc_name": row["wc_name"],
        "theme": row["theme"],
        "sort_order": int(row["sort_order"]),
    }
