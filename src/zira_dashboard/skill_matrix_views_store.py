"""CRUD for People Matrix Views — server-side state shared across all
devices and users. See docs/superpowers/specs/2026-04-29-skill-matrix-views-design.md."""

from __future__ import annotations

ACTIVE_FILTERS = ("active", "inactive", "all")
RESERVE_FILTERS = ("include", "exclude", "only", "all")


def _row_to_dict(row: dict) -> dict:
    return {
        "name": row["name"],
        "is_default": row["is_default"],
        "hidden_skills": list(row["hidden_skills"] or []),
        "visible_people": list(row["visible_people"]) if row["visible_people"] is not None else None,
        "active_filter": row["active_filter"],
        "reserve_filter": row["reserve_filter"],
    }


def _coerce(payload: dict) -> dict:
    out: dict = {}
    hs = payload.get("hidden_skills") or []
    out["hidden_skills"] = [str(s) for s in hs if isinstance(s, str)]
    vp = payload.get("visible_people")
    if vp is None:
        out["visible_people"] = None
    else:
        cleaned = [str(s).strip() for s in vp if isinstance(s, str) and str(s).strip()]
        out["visible_people"] = cleaned if cleaned else None
    af = payload.get("active_filter")
    out["active_filter"] = af if af in ACTIVE_FILTERS else "active"
    rf = payload.get("reserve_filter")
    out["reserve_filter"] = rf if rf in RESERVE_FILTERS else "all"
    return out


def list_views() -> list[dict]:
    from . import db
    rows = db.query(
        "SELECT name, is_default, hidden_skills, visible_people, "
        "       active_filter, reserve_filter FROM skill_matrix_views "
        "ORDER BY is_default DESC, lower(name)"
    )
    return [_row_to_dict(r) for r in rows]


def get_view(name: str) -> dict | None:
    from . import db
    rows = db.query(
        "SELECT name, is_default, hidden_skills, visible_people, "
        "       active_filter, reserve_filter FROM skill_matrix_views "
        "WHERE name = %s",
        (name,),
    )
    return _row_to_dict(rows[0]) if rows else None


def get_default_view() -> dict | None:
    from . import db
    rows = db.query(
        "SELECT name, is_default, hidden_skills, visible_people, "
        "       active_filter, reserve_filter FROM skill_matrix_views "
        "WHERE is_default = TRUE LIMIT 1"
    )
    return _row_to_dict(rows[0]) if rows else None


def create_view(name: str, payload: dict) -> dict:
    from . import db
    name = (name or "").strip()[:80]
    if not name:
        raise ValueError("name required")
    fields = _coerce(payload)
    db.execute(
        "INSERT INTO skill_matrix_views (name, hidden_skills, visible_people, "
        "active_filter, reserve_filter) VALUES (%s, %s, %s, %s, %s)",
        (
            name, fields["hidden_skills"], fields["visible_people"],
            fields["active_filter"], fields["reserve_filter"],
        ),
    )
    out = get_view(name)
    assert out is not None
    return out


def update_view(name: str, payload: dict) -> dict:
    from . import db
    fields = _coerce(payload)
    db.execute(
        "UPDATE skill_matrix_views SET hidden_skills = %s, visible_people = %s, "
        "active_filter = %s, reserve_filter = %s, updated_at = now() "
        "WHERE name = %s",
        (
            fields["hidden_skills"], fields["visible_people"],
            fields["active_filter"], fields["reserve_filter"], name,
        ),
    )
    out = get_view(name)
    if out is None:
        raise ValueError(f"view {name!r} not found")
    return out


def delete_view(name: str) -> None:
    from . import db
    db.execute("DELETE FROM skill_matrix_views WHERE name = %s", (name,))


def set_default(name: str | None) -> None:
    """Atomically clear all defaults, then set one (or none if name is None)."""
    from . import db
    with db.cursor() as cur:
        cur.execute("UPDATE skill_matrix_views SET is_default = FALSE WHERE is_default = TRUE")
        if name is not None:
            cur.execute("UPDATE skill_matrix_views SET is_default = TRUE WHERE name = %s", (name,))
