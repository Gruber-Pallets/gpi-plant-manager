"""Odoo XML-RPC client.

Reads: hr.employee, hr.skill*, hr.department, hr.leave.type (time-off types),
hr.leave (time-off requests), resource.calendar + resource.calendar.attendance
(employee working hours).
Writes: hr.attendance (kiosk clock-in/out/transfer). The Odoo API user backing
ODOO_API_KEY needs write permission on hr.attendance.

Configuration comes from environment variables:
- ODOO_URL  — base URL, e.g. https://gruber-pallets.odoo.com (no trailing /odoo)
- ODOO_DB   — database name
- ODOO_LOGIN — username (email)
- ODOO_API_KEY — Odoo API key (Settings → Users → Account Security)

Never log or echo these values.
"""

from __future__ import annotations

import os
import xmlrpc.client
from datetime import datetime, timezone
from typing import Any


class OdooConfigError(RuntimeError):
    """Required env var is missing or malformed."""


class OdooAuthError(RuntimeError):
    """Odoo accepted the request but rejected our credentials."""


_uid_cache: int | None = None
_object_proxy: xmlrpc.client.ServerProxy | None = None


def _reset_cache_for_tests() -> None:
    """Clear cached uid + object proxy; tests call this between cases."""
    global _uid_cache, _object_proxy
    _uid_cache = None
    _object_proxy = None


def _config() -> tuple[str, str, str, str]:
    url = os.environ.get("ODOO_URL", "").rstrip("/")
    db = os.environ.get("ODOO_DB", "")
    login = os.environ.get("ODOO_LOGIN", "")
    key = os.environ.get("ODOO_API_KEY", "")
    missing = [k for k, v in (
        ("ODOO_URL", url), ("ODOO_DB", db),
        ("ODOO_LOGIN", login), ("ODOO_API_KEY", key),
    ) if not v]
    if missing:
        raise OdooConfigError(f"Missing env vars: {', '.join(missing)}")
    return url, db, login, key


def authenticate() -> int:
    global _uid_cache
    if _uid_cache is not None:
        return _uid_cache
    url, db, login, key = _config()
    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, login, key, {})
    if not uid:
        raise OdooAuthError("Odoo rejected credentials")
    _uid_cache = uid
    return uid


def execute(model: str, method: str, *args: Any, **kwargs: Any) -> Any:
    """Run an XML-RPC call against `model.method(*args, **kwargs)`. Caches
    the object proxy across calls."""
    global _object_proxy
    url, db, _, key = _config()
    uid = authenticate()
    if _object_proxy is None:
        _object_proxy = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
    return _object_proxy.execute_kw(
        db, uid, key, model, method, list(args), kwargs
    )


SKILL_TYPE_NAMES = ("Production Skills", "Supervisor Skills", "Certifications")


def fetch_skill_columns_with_types() -> list[dict]:
    """Return ordered list of {name, type} dicts: all skills from the
    Production type (alphabetical), then all from Supervisor (alphabetical)."""
    types = execute(
        "hr.skill.type", "search_read",
        [("name", "in", list(SKILL_TYPE_NAMES))],
        fields=["id", "name"],
    )
    type_order = {name: i for i, name in enumerate(SKILL_TYPE_NAMES)}
    types.sort(key=lambda t: type_order.get(t["name"], 999))
    type_ids = [t["id"] for t in types]
    type_name_by_id = {t["id"]: t["name"] for t in types}
    if not type_ids:
        return []
    skills = execute(
        "hr.skill", "search_read",
        [("skill_type_id", "in", type_ids)],
        fields=["id", "name", "skill_type_id"],
    )
    by_type: dict[int, list[str]] = {tid: [] for tid in type_ids}
    for s in skills:
        tid = s["skill_type_id"][0] if isinstance(s["skill_type_id"], list) else s["skill_type_id"]
        by_type.setdefault(tid, []).append(s["name"])
    out: list[dict] = []
    for tid in type_ids:
        for name in sorted(by_type.get(tid, []), key=str.lower):
            out.append({"name": name, "type": type_name_by_id[tid]})
    return out


def fetch_skill_columns() -> list[str]:
    """Backwards-compatible name-only view."""
    return [c["name"] for c in fetch_skill_columns_with_types()]


def fetch_skill_level_buckets() -> dict[int, int]:
    """Map hr.skill.level.id → bucket (0–3) using rank-within-type.

    For each skill type, sort levels ascending by level_progress, assign
    rank index, then bucket = round(rank * 3 / max(N-1, 1)) clamped 0..3.
    """
    levels = execute(
        "hr.skill.level", "search_read",
        [],
        fields=["id", "level_progress", "skill_type_id"],
    )
    by_type: dict[int, list[dict]] = {}
    for lvl in levels:
        tid = lvl["skill_type_id"][0] if isinstance(lvl["skill_type_id"], list) else lvl["skill_type_id"]
        by_type.setdefault(tid, []).append(lvl)
    out: dict[int, int] = {}
    for tid, lvls in by_type.items():
        lvls.sort(key=lambda l: l.get("level_progress", 0))
        n = len(lvls)
        for rank, lvl in enumerate(lvls):
            if n <= 1:
                bucket = 0
            else:
                bucket = round(rank * 3 / (n - 1))
            out[lvl["id"]] = max(0, min(3, bucket))
    return out


def fetch_departments() -> list[str]:
    """Return sorted hr.department names with leading numeric/code prefix
    stripped. Odoo conventionally numbers departments for sort order
    ("00 Supervisor", "01 Recycled", "02 New", "06 Transportation", ...)
    and the digits are noise in the app's dropdowns and dashboards.

    The cleaned names round-trip cleanly with `_department_id_for_wc()`
    above (its ILIKE search matches "Recycled" against "01 Recycled" in
    Odoo). Sorting is alphabetical on the cleaned name.

    Inactive (archived) departments are skipped."""
    import re
    rows = execute(
        "hr.department", "search_read",
        [("active", "=", True)],
        fields=["id", "name"],
    )
    cleaned: list[str] = []
    for r in rows:
        raw = (r.get("name") or "").strip()
        if not raw:
            continue
        # Strip leading digits + whitespace ("01 Recycled" → "Recycled",
        # "00 Supervisor" → "Supervisor"). Numeric-only prefix only —
        # leave names like "R&D Recycled" alone.
        cleaned.append(re.sub(r"^\d+\s+", "", raw))
    # De-dup while preserving order, then sort case-insensitive.
    seen: set[str] = set()
    out: list[str] = []
    for n in cleaned:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    out.sort(key=str.lower)
    return out


def fetch_employees() -> list[dict]:
    """All active hr.employee records with the fields we need.

    `wage_type` is an Odoo selection: 'hourly' or 'monthly'. Used by the
    late/absence report to filter out salaried managers who have
    flexible start times.
    """
    return execute(
        "hr.employee", "search_read",
        [("active", "=", True)],
        fields=["id", "name", "active", "work_email", "wage_type"],
    )


def fetch_skills_for(employee_ids: list[int]) -> dict[int, list[dict]]:
    """Return {employee_id: [{skill_id, skill_name, level_id}, ...]}."""
    if not employee_ids:
        return {}
    rows = execute(
        "hr.employee.skill", "search_read",
        [("employee_id", "in", employee_ids)],
        fields=["id", "employee_id", "skill_id", "skill_level_id"],
    )
    out: dict[int, list[dict]] = {eid: [] for eid in employee_ids}
    for r in rows:
        eid = r["employee_id"][0] if isinstance(r["employee_id"], list) else r["employee_id"]
        sid = r["skill_id"][0]    if isinstance(r["skill_id"], list)    else r["skill_id"]
        lid = r["skill_level_id"][0] if isinstance(r["skill_level_id"], list) else r["skill_level_id"]
        sname = r["skill_id"][1] if isinstance(r["skill_id"], list) else ""
        out.setdefault(eid, []).append({"skill_id": sid, "skill_name": sname, "level_id": lid})
    return out


# ---------- Kiosk attendance writes (Phase 0 pilot) ----------
#
# These are the first WRITE methods on the Odoo client — everything above
# is read-only sync. The Odoo API user backing ODOO_API_KEY must have
# write permission on `hr.attendance` for these to succeed.


def _kiosk_wc_field() -> str | None:
    """Custom field on hr.attendance where the kiosk records the work
    center the employee is punched into. The field has to exist in Odoo
    (added via Studio or a custom module — recommended:
    `x_kiosk_workcenter_name` as a Char). Set the env var when the field
    is in place; leave unset to skip it entirely (early dev / pre-Odoo-setup
    testing). Without the field, attendance rows are still written, just
    without the WC attribution."""
    return os.environ.get("ODOO_KIOSK_WC_FIELD") or None


def _kiosk_department_field() -> str | None:
    """Field on hr.attendance to write the kiosk's resolved department_id
    into. Odoo 17+ has a native writable `department_id`; older versions
    or installs with `department_id` as a related/computed read-only
    field need a custom Many2one field (recommended:
    `x_kiosk_department_id` → hr.department, added via Studio).

    Default unset (None) → no department write; attendance rows still
    create successfully, they just won't be tagged by department for
    reports. Set the env var to enable.

    Required for reports that group hours by department to pick up
    kiosk-created attendance, since the kiosk lets people transfer
    mid-shift across departments and the employee's home department
    on hr.employee won't reflect that."""
    return os.environ.get("ODOO_KIOSK_DEPARTMENT_FIELD") or None


# WC name → Odoo hr.department.id. Populated lazily by _department_id_for_wc.
# None is cached too (means "looked up, not found in Odoo") so we don't keep
# searching for a misconfigured department on every punch. Cleared on process
# restart, which is fine — department list rarely changes and a restart is
# cheap (Railway redeploy).
_wc_dept_id_cache: dict[str, int | None] = {}


def _department_id_for_wc(wc_name: str | None) -> int | None:
    """Resolve a kiosk WC name (e.g. "Repair 1") to an Odoo
    hr.department.id, going via the WC's `department` attribute in
    staffing.LOCATIONS (e.g. "Recycled") and a case-insensitive
    substring match against hr.department.name in Odoo (so "Recycled"
    matches "01 Recycled").

    Returns None if the WC is unknown, the WC has no department, or no
    matching Odoo department exists."""
    if not wc_name:
        return None
    if wc_name in _wc_dept_id_cache:
        return _wc_dept_id_cache[wc_name]
    from . import staffing
    dept_name: str | None = None
    for loc in staffing.LOCATIONS:
        if loc.name == wc_name:
            dept_name = loc.department
            break
    if not dept_name:
        _wc_dept_id_cache[wc_name] = None
        return None
    rows = execute(
        "hr.department", "search_read",
        [("name", "ilike", dept_name)],
        fields=["id"],
        limit=1,
    )
    dept_id = rows[0]["id"] if rows else None
    _wc_dept_id_cache[wc_name] = dept_id
    return dept_id


def _to_odoo_dt(ts: datetime) -> str:
    """Odoo expects naive UTC strings in 'YYYY-MM-DD HH:MM:SS' format.
    Accepts aware or naive datetimes; aware ones are converted to UTC."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def get_current_attendance(employee_odoo_id: int) -> dict | None:
    """Return the open hr.attendance row for this employee (check_out IS
    NULL), or None if they're already clocked out. Most recent open
    attendance wins if there's somehow more than one."""
    rows = execute(
        "hr.attendance", "search_read",
        [("employee_id", "=", employee_odoo_id), ("check_out", "=", False)],
        fields=["id", "employee_id", "check_in"],
        limit=1,
    )
    return rows[0] if rows else None


def clock_in(employee_odoo_id: int, wc_name: str | None, ts: datetime) -> int:
    """Create a new hr.attendance with check_in=ts. Returns the new id.

    Writes the WC name into ODOO_KIOSK_WC_FIELD when configured (Char).
    Writes the WC's resolved Odoo department into ODOO_KIOSK_DEPARTMENT_FIELD
    when configured (Many2one to hr.department), so reports that group
    hours by department attribute kiosk-created attendance correctly
    even when an employee transfers between departments mid-shift."""
    payload: dict[str, Any] = {
        "employee_id": employee_odoo_id,
        "check_in": _to_odoo_dt(ts),
    }
    wc_field = _kiosk_wc_field()
    if wc_field and wc_name:
        payload[wc_field] = wc_name
    dept_field = _kiosk_department_field()
    if dept_field:
        dept_id = _department_id_for_wc(wc_name)
        if dept_id:
            payload[dept_field] = dept_id
    return execute("hr.attendance", "create", payload)


def clock_out(attendance_id: int, ts: datetime) -> None:
    """Set check_out on an existing hr.attendance. Safe to call on an
    already-closed record — Odoo just overwrites the timestamp."""
    execute(
        "hr.attendance", "write",
        [attendance_id],
        {"check_out": _to_odoo_dt(ts)},
    )


def transfer(
    employee_odoo_id: int, new_wc_name: str | None, ts: datetime
) -> tuple[int | None, int]:
    """Close the employee's current open hr.attendance and open a new one
    at the new WC. Returns (closed_id, new_id). If the employee has no
    open attendance, closed_id is None — the new one is still opened so
    the kiosk fails gracefully when local state and Odoo state disagree."""
    current = get_current_attendance(employee_odoo_id)
    closed_id: int | None = None
    if current:
        clock_out(current["id"], ts)
        closed_id = current["id"]
    new_id = clock_in(employee_odoo_id, new_wc_name, ts)
    return closed_id, new_id


# ---------- Time-off reads (2026-05-27) ----------

import time as _time

_LEAVE_TYPES_TTL_SECONDS = 10 * 60
# (types_list, expires_at_epoch). Module-level so a process restart clears it.
_leave_types_cache: tuple[list[dict], float] | None = None


def fetch_leave_types() -> list[dict]:
    """All active hr.leave.type, cached in-process for 10 minutes.

    Returns [{id, name, request_unit, requires_allocation, color, active}, ...].
    """
    global _leave_types_cache
    now = _time.time()
    if _leave_types_cache and _leave_types_cache[1] > now:
        return _leave_types_cache[0]
    rows = execute(
        "hr.leave.type", "search_read",
        [("active", "=", True)],
        fields=["id", "name", "request_unit",
                "requires_allocation", "color", "active"],
    )
    _leave_types_cache = (rows, now + _LEAVE_TYPES_TTL_SECONDS)
    return rows


def fetch_leaves_for_range(start_d, end_d) -> list[dict]:
    """All hr.leave records overlapping [start_d, end_d] for active employees.

    Overlap rule: request_date_to >= start_d AND request_date_from <= end_d.
    Returns raw search_read dicts; caller normalizes Many2one fields.
    """
    domain = [
        ("request_date_to", ">=", start_d.isoformat()),
        ("request_date_from", "<=", end_d.isoformat()),
        ("employee_id.active", "=", True),
    ]
    return execute(
        "hr.leave", "search_read",
        domain,
        fields=[
            "id", "employee_id", "holiday_status_id", "state",
            "date_from", "date_to",
            "request_date_from", "request_date_to",
            "request_hour_from", "request_hour_to", "request_unit_hours",
            "number_of_days", "number_of_hours_display", "name",
        ],
    )


def fetch_resource_calendar(employee_odoo_id: int) -> dict | None:
    """Returns {hour_from, hour_to, lunch_from, lunch_to, tz} or None.

    Derives hour_from/hour_to from min/max of resource.calendar.attendance
    rows (excluding lunch periods). If lunch periods are configured on the
    calendar, returns them as well. Tz comes from resource.calendar.
    """
    emp_rows = execute(
        "hr.employee", "search_read",
        [("id", "=", employee_odoo_id)],
        fields=["id", "resource_calendar_id"],
    )
    if not emp_rows or not emp_rows[0].get("resource_calendar_id"):
        return None
    cal_field = emp_rows[0]["resource_calendar_id"]
    cal_id = cal_field[0] if isinstance(cal_field, list) else cal_field
    cal_rows = execute(
        "resource.calendar", "read",
        [cal_id], ["id", "tz"],
    )
    tz = cal_rows[0]["tz"] if cal_rows else None

    att_rows = execute(
        "resource.calendar.attendance", "search_read",
        [("calendar_id", "=", cal_id)],
        fields=["hour_from", "hour_to", "dayofweek", "day_period"],
    )
    # Filter to non-lunch periods for the work-window bounds.
    work = [a for a in att_rows if a.get("day_period") != "lunch"]
    lunches = [a for a in att_rows if a.get("day_period") == "lunch"]
    if not work:
        return None
    hour_from = min(float(a["hour_from"]) for a in work)
    hour_to = max(float(a["hour_to"]) for a in work)
    lunch_from = min((float(a["hour_from"]) for a in lunches), default=None)
    lunch_to = max((float(a["hour_to"]) for a in lunches), default=None)
    return {
        "hour_from": hour_from,
        "hour_to": hour_to,
        "lunch_from": lunch_from,
        "lunch_to": lunch_to,
        "tz": tz,
    }
