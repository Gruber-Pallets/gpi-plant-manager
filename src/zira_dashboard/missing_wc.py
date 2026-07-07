"""Missing-work-center alert: cached Odoo hr.attendance rows lacking a
work-center tag, plus suppression + row shaping for the badge/modal.

Mirrors late_report.py: the warmer owns the Odoo fetch (see
app._tick_missing_wc); this module does local reads + pure shaping, so the
badge endpoint never touches Odoo on the hot path.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, UTC

from .shift_config import SITE_TZ

_log = logging.getLogger(__name__)

# monotonic() of the last retention DELETE in write_cache; 0.0 means run
# on the first tick after boot.
_last_retention_at: float = 0.0


def write_cache(rows: list[dict]) -> None:
    """Overwrite the single-row snapshot with the latest fetch (warmer-owned).

    Also prunes missing_wc_resolved rows older than the snapshot window
    (~once/hour) so the table doesn't grow forever."""
    global _last_retention_at
    from . import db
    db.execute(
        "INSERT INTO missing_wc_cache (id, snapshot, refreshed_at) "
        "VALUES (1, %s::jsonb, now()) "
        "ON CONFLICT (id) DO UPDATE SET snapshot = EXCLUDED.snapshot, refreshed_at = now()",
        (json.dumps(rows or []),),
    )
    now = time.monotonic()
    if now - _last_retention_at >= 3600:
        _last_retention_at = now
        db.execute(
            "DELETE FROM missing_wc_resolved "
            "WHERE resolved_at < now() - interval '15 days'"
        )


def _read_cache() -> list[dict]:
    from . import db
    rows = db.query("SELECT snapshot FROM missing_wc_cache WHERE id = 1")
    if not rows:
        return []
    snap = rows[0]["snapshot"]
    if isinstance(snap, list):
        return snap
    try:
        return json.loads(snap) if snap else []
    except (TypeError, ValueError):
        return []


def resolve(attendance_id, action: str, name: str | None = None,
            wc_name: str | None = None) -> None:
    """Suppress an attendance row from the alert (action 'assigned'|'dismissed')."""
    from . import db
    db.execute(
        "INSERT INTO missing_wc_resolved (attendance_id, action, name, wc_name) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (attendance_id) DO UPDATE SET action = EXCLUDED.action, "
        "name = EXCLUDED.name, wc_name = EXCLUDED.wc_name, resolved_at = now()",
        (int(attendance_id), action, name, wc_name),
    )


def unresolve(attendance_id) -> None:
    """Drop a suppression row so the attendance re-appears in the alert (undo)."""
    from . import db
    db.execute(
        "DELETE FROM missing_wc_resolved WHERE attendance_id = %s",
        (int(attendance_id),),
    )


def resolved_ids() -> set[int]:
    """Suppressed attendance ids. The snapshot only covers the last 14 days,
    so older resolutions are irrelevant — the filter keeps this 60s badge-poll
    read small as the table grows."""
    from . import db
    return {int(r["attendance_id"])
            for r in db.query(
                "SELECT attendance_id FROM missing_wc_resolved "
                "WHERE resolved_at > now() - interval '15 days'")}


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _check_in_label(check_in_iso) -> str:
    """ISO UTC string -> 'H:MM AM/PM Ddd' in site-local time, '' on bad input."""
    if not check_in_iso:
        return ""
    try:
        dt = datetime.fromisoformat(check_in_iso)
    except (TypeError, ValueError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(SITE_TZ)
    fmt = "%#I:%M %p %a" if os.name == "nt" else "%-I:%M %p %a"
    return local.strftime(fmt)


def shape_rows(cached: list[dict], people_by_odoo_id: dict, resolved: set) -> list[dict]:
    """Pure: cached rows + {odoo_id: {name, wage_type, active, excluded}} +
    resolved att_id set -> modal rows for ACTIVE HOURLY people, newest first.
    One row per attendance record (each needs its own work center)."""
    out: list[dict] = []
    for r in cached:
        att_id = _as_int(r.get("att_id"))
        if att_id is None:
            continue
        if att_id in resolved:
            continue
        employee_odoo_id = _as_int(r.get("employee_odoo_id"))
        p = people_by_odoo_id.get(employee_odoo_id)
        if not p or p.get("wage_type") != "hourly":
            continue
        if not p.get("active") or p.get("excluded"):
            continue
        out.append({
            "attendance_id": att_id,
            "name": p.get("name") or r.get("employee_name") or "Unknown",
            "employee_odoo_id": employee_odoo_id,
            "check_in": r.get("check_in"),
            "check_in_label": _check_in_label(r.get("check_in")),
        })
    out.sort(key=lambda x: x.get("check_in") or "", reverse=True)
    return out


def current_rows() -> list[dict]:
    """Badge/modal payload: cached snapshot filtered to active hourly people,
    minus suppressed records. All local reads — no Odoo I/O."""
    from . import db
    cached = _read_cache()
    prows = db.query(
        "SELECT odoo_id, name, wage_type, active, excluded FROM people "
        "WHERE odoo_id IS NOT NULL"
    )
    people_by_odoo_id = {int(r["odoo_id"]): r for r in prows}
    return shape_rows(cached, people_by_odoo_id, resolved_ids())
