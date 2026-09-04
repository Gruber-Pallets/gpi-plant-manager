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
from collections.abc import Callable, Sequence
from datetime import datetime, UTC

from . import attendance_location_policy
from .shift_config import SITE_TZ

_log = logging.getLogger(__name__)

# monotonic() of the last retention DELETE in write_cache; 0.0 means run
# on the first tick after boot.
_last_retention_at: float = 0.0
_MONITORING_STARTED_AT_SETTING = "missing_wc.monitoring_started_at"


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
        db.execute("DELETE FROM missing_wc_resolved WHERE resolved_at < now() - interval '15 days'")


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


def resolve(
    attendance_id, action: str, name: str | None = None, wc_name: str | None = None
) -> None:
    """Suppress an attendance row from the alert (action 'assigned'|'dismissed')."""
    from . import db

    db.execute(
        "INSERT INTO missing_wc_resolved (attendance_id, action, name, wc_name) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (attendance_id) DO UPDATE SET action = EXCLUDED.action, "
        "name = EXCLUDED.name, wc_name = EXCLUDED.wc_name, resolved_at = now()",
        (int(attendance_id), action, name, wc_name),
    )


def resolve_many(
    attendance_ids: Sequence[int],
    action: str,
    name: str | None = None,
    wc_name: str | None = None,
) -> None:
    ids = tuple(dict.fromkeys(int(value) for value in attendance_ids))
    if not ids:
        raise ValueError("at least one attendance id is required")
    from . import db

    with db.cursor() as cur:
        cur.executemany(
            "INSERT INTO missing_wc_resolved (attendance_id, action, name, wc_name) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (attendance_id) DO UPDATE SET action = EXCLUDED.action, "
            "name = EXCLUDED.name, wc_name = EXCLUDED.wc_name, resolved_at = now()",
            [(attendance_id, action, name, wc_name) for attendance_id in ids],
        )


def claim_many(
    item_key: str,
    attendance_ids: Sequence[int],
    action: str,
    name: str | None = None,
    wc_name: str | None = None,
) -> bool:
    """Atomically suppress all IDs once for one stable inbox item.

    The transaction-scoped advisory lock serializes clicks for the same stable
    item key. The suppression check happens after taking that lock. A partially
    suppressed item is completed without changing existing row metadata; only
    the claimant that finishes the set receives ``True``.
    """
    stable_key = str(item_key).strip()
    ids = tuple(dict.fromkeys(int(value) for value in attendance_ids))
    if not stable_key:
        raise ValueError("item key is required")
    if not ids:
        raise ValueError("at least one attendance id is required")
    from . import db

    with db.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (stable_key,),
        )
        cur.execute(
            "SELECT attendance_id FROM missing_wc_resolved "
            "WHERE attendance_id = ANY(%s)",
            (list(ids),),
        )
        existing_ids = {int(row["attendance_id"]) for row in cur.fetchall()}
        missing_ids = tuple(
            attendance_id for attendance_id in ids if attendance_id not in existing_ids
        )
        if not missing_ids:
            return False
        cur.executemany(
            "INSERT INTO missing_wc_resolved (attendance_id, action, name, wc_name) "
            "VALUES (%s, %s, %s, %s)",
            [(attendance_id, action, name, wc_name) for attendance_id in missing_ids],
        )
    return True


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

    return {
        int(r["attendance_id"])
        for r in db.query(
            "SELECT attendance_id FROM missing_wc_resolved "
            "WHERE resolved_at > now() - interval '15 days'"
        )
    }


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_utc_datetime(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def monitoring_started_at(*, now: datetime | None = None) -> datetime:
    """Return the first moment this deployment began enforcing missing-WC
    alerts, creating that one-time rollout boundary when absent.

    Attendance from before the optional Odoo field was activated cannot be
    repaired by the new workflow, so it must not be retroactively treated as
    urgent inbox work.
    """
    from . import app_settings

    saved = app_settings.get_setting(_MONITORING_STARTED_AT_SETTING)
    if isinstance(saved, dict):
        started = _as_utc_datetime(saved.get("at"))
        if started is not None:
            return started

    started = now or datetime.now(UTC)
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    else:
        started = started.astimezone(UTC)
    app_settings.set_setting(_MONITORING_STARTED_AT_SETTING, {"at": started.isoformat()})
    return started


def locally_unmapped_attendance_ids(attendance_ids: set[int]) -> set[int]:
    """Kiosk-created records whose selected app WC has no Odoo mapping.

    These records are already visible as Settings readiness gaps. They cannot
    be corrected from the inbox's per-attendance picker, so do not turn every
    lunch return at one into an urgent row.
    """
    ids = sorted({int(att_id) for att_id in attendance_ids})
    if not ids:
        return set()
    from . import db

    rows = db.query(
        "SELECT DISTINCT l.odoo_attendance_id "
        "FROM timeclock_punches_log l "
        "LEFT JOIN work_centers wc ON wc.name = l.wc_name "
        "WHERE l.odoo_attendance_id = ANY(%s) "
        "AND l.action IN ('clock_in', 'transfer_in') "
        "AND l.wc_name IS NOT NULL "
        "AND wc.odoo_work_center_id IS NULL",
        (ids,),
    )
    return {
        att_id for row in rows if (att_id := _as_int(row.get("odoo_attendance_id"))) is not None
    }


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


def shape_rows(
    cached: list[dict],
    people_by_odoo_id: dict,
    resolved: set,
    *,
    monitoring_started_at: datetime | None = None,
    locally_unmapped_attendance_ids: set[int] | None = None,
    requires_work_center: Callable[[str | None], bool] = lambda _department: True,
) -> list[dict]:
    """Pure: cached rows + {odoo_id: {name, wage_type, active, excluded}} +
    resolved att_id set -> modal rows for ACTIVE HOURLY people, newest first.
    One row per attendance record (each needs its own work center)."""
    out: list[dict] = []
    monitoring_start = _as_utc_datetime(monitoring_started_at)
    locally_unmapped_ids = {
        att_id
        for value in (locally_unmapped_attendance_ids or set())
        if (att_id := _as_int(value)) is not None
    }
    for r in cached:
        att_id = _as_int(r.get("att_id"))
        if att_id is None:
            continue
        if att_id in resolved:
            continue
        if att_id in locally_unmapped_ids:
            continue
        check_in_at = _as_utc_datetime(r.get("check_in"))
        if monitoring_start is not None and check_in_at is not None:
            if check_in_at < monitoring_start:
                continue
        employee_odoo_id = _as_int(r.get("employee_odoo_id"))
        p = people_by_odoo_id.get(employee_odoo_id)
        if not p or p.get("wage_type") != "hourly":
            continue
        if not p.get("active") or p.get("excluded"):
            continue
        effective_department = attendance_location_policy.effective_department_name(
            r.get("department_name"),
            p.get("department_name"),
        )
        if not requires_work_center(effective_department):
            continue
        out.append(
            {
                "attendance_id": att_id,
                "name": p.get("name") or r.get("employee_name") or "Unknown",
                "employee_odoo_id": employee_odoo_id,
                "check_in": r.get("check_in"),
                "check_in_label": _check_in_label(r.get("check_in")),
            }
        )
    out.sort(key=lambda x: x.get("check_in") or "", reverse=True)
    return out


def current_rows() -> list[dict]:
    """Badge/modal payload: cached snapshot filtered to active hourly people,
    minus suppressed records. All local reads — no Odoo I/O."""
    from . import db

    cached = _read_cache()
    prows = db.query(
        "SELECT odoo_id, name, wage_type, active, excluded, department_name FROM people "
        "WHERE odoo_id IS NOT NULL"
    )
    people_by_odoo_id = {int(r["odoo_id"]): r for r in prows}
    attendance_ids = {
        att_id for row in cached if (att_id := _as_int(row.get("att_id"))) is not None
    }
    return shape_rows(
        cached,
        people_by_odoo_id,
        resolved_ids(),
        monitoring_started_at=monitoring_started_at(),
        locally_unmapped_attendance_ids=locally_unmapped_attendance_ids(attendance_ids),
        requires_work_center=attendance_location_policy.department_requires_work_center,
    )
