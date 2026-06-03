"""Odoo XML-RPC client.

Reads: hr.employee, hr.skill*, hr.department, hr.leave.type (time-off types),
hr.leave (time-off requests), hr.leave.allocation (time-off balances),
resource.calendar + resource.calendar.attendance (employee working hours).
Writes: hr.attendance (kiosk clock-in/out/transfer); hr.leave (kiosk time-off
create / edit / refuse). The Odoo API user backing ODOO_API_KEY needs write
permission on hr.attendance and hr.leave.

Configuration comes from environment variables:
- ODOO_URL  — base URL, e.g. https://gruber-pallets.odoo.com (no trailing /odoo)
- ODOO_DB   — database name
- ODOO_LOGIN — username (email)
- ODOO_API_KEY — Odoo API key (Settings → Users → Account Security)

Never log or echo these values.
"""

from __future__ import annotations

import os
import threading
import time
import xmlrpc.client
from datetime import datetime, timezone
from typing import Any


def unwrap_m2o(val):
    """Odoo many2one → its id. XML-RPC returns these as ``[id, name]`` (or
    ``False`` when unset; sometimes a bare id in a write-payload echo). Returns
    the id for a non-empty list/tuple, otherwise the value unchanged."""
    return val[0] if isinstance(val, (list, tuple)) and val else val


class OdooConfigError(RuntimeError):
    """Required env var is missing or malformed."""


class OdooAuthError(RuntimeError):
    """Odoo accepted the request but rejected our credentials."""


_uid_cache: int | None = None
# xmlrpc.client.ServerProxy holds ONE persistent http.client connection and is
# NOT thread-safe: two threads sharing a proxy interleave on the same connection
# and corrupt its state machine (CannotSendRequest 'Request-sent' /
# ResponseNotReady 'Idle'). The warmers (asyncio.to_thread) and request handlers
# call execute() concurrently, so the object proxy is kept per-thread. _uid_cache
# stays a plain int — it's set-once and a benign re-auth race is harmless.
_thread_local = threading.local()


def _reset_cache_for_tests() -> None:
    """Clear cached uid + per-thread object proxy; tests call this between cases."""
    global _uid_cache
    _uid_cache = None
    if hasattr(_thread_local, "object_proxy"):
        del _thread_local.object_proxy


def _object_proxy_for_thread() -> xmlrpc.client.ServerProxy:
    """Return this thread's object-endpoint ServerProxy, creating it once.

    Thread-local so concurrent callers never share one ServerProxy (and its
    single underlying connection) — see the note on _thread_local above.
    """
    proxy = getattr(_thread_local, "object_proxy", None)
    if proxy is None:
        url, _db, _login, _key = _config()
        proxy = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        _thread_local.object_proxy = proxy
    return proxy


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
    """Run an XML-RPC call against `model.method(*args, **kwargs)`. Reuses a
    per-thread object proxy (and its connection) across calls."""
    _url, db, _login, key = _config()
    uid = authenticate()
    return _object_proxy_for_thread().execute_kw(
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
        tid = unwrap_m2o(s["skill_type_id"])
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
        tid = unwrap_m2o(lvl["skill_type_id"])
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


def _float_to_hhmm(f) -> str:
    """Odoo stores working-schedule hours as floats (5.75 == 05:45). Round
    to the nearest minute, carrying into the hour, clamped to [00:00, 23:59]."""
    total = int(round(float(f) * 60))          # minutes since midnight
    total = max(0, min(total, 23 * 60 + 59))
    return f"{total // 60:02d}:{total % 60:02d}"


def _calendar_hours_from_lines(rows) -> dict:
    """Reduce resource.calendar.attendance rows to per-weekday OUTER shift
    boundaries: {cal_id: {"0": ["05:45","14:30"], ...}} with weekday keys
    0=Mon..6=Sun (Odoo's dayofweek convention, same as Python weekday()).
    A lunch split (two lines on one day) collapses to min(hour_from) ..
    max(hour_to). Malformed rows are skipped."""
    acc: dict = {}   # {cal_id: {weekday:int -> [min_from:float, max_to:float]}}
    for r in rows:
        if r.get("day_period") == "lunch":
            continue
        cal = r.get("calendar_id")
        cal_id = unwrap_m2o(cal)
        if not isinstance(cal_id, int) or isinstance(cal_id, bool):
            continue
        try:
            wd = int(r.get("dayofweek"))
        except (TypeError, ValueError):
            continue
        if not (0 <= wd <= 6):
            continue
        hf = float(r.get("hour_from") or 0.0)
        ht = float(r.get("hour_to") or 0.0)
        day = acc.setdefault(cal_id, {}).get(wd)
        if day is None:
            acc[cal_id][wd] = [hf, ht]
        else:
            day[0] = min(day[0], hf)
            day[1] = max(day[1], ht)
    out: dict = {}
    for cal_id, days in acc.items():
        out[cal_id] = {
            str(wd): [_float_to_hhmm(lo), _float_to_hhmm(hi)]
            for wd, (lo, hi) in days.items()
        }
    return out


# Odoo "Schedule Type" on resource.calendar. Confirmed against live Odoo
# (Task 6 Step 1). Odoo 18 exposes flexible scheduling as the boolean
# `flexible_hours`; if your instance uses a selection, change this name —
# _is_flexible() already accepts both a bool and the string 'flexible'.
SCHEDULE_TYPE_FIELD = "flexible_hours"


def _is_flexible(value) -> bool:
    """Interpret the resource.calendar Schedule Type value as a flex flag.
    Accepts a boolean (Odoo 18 `flexible_hours`) or a selection string."""
    if isinstance(value, str):
        return value.strip().lower() == "flexible"
    return bool(value)


def fetch_work_schedules() -> list[dict]:
    """Active working schedules (resource.calendar):
    [{id, name, is_flexible}, ...]. is_flexible drives the auto-lunch
    elapsed-time trigger for flexible-schedule employees."""
    rows = execute(
        "resource.calendar", "search_read",
        [("active", "=", True)],
        fields=["id", "name", SCHEDULE_TYPE_FIELD],
    )
    return [
        {"id": r["id"], "name": r.get("name") or "",
         "is_flexible": _is_flexible(r.get(SCHEDULE_TYPE_FIELD))}
        for r in rows
    ]


def fetch_calendar_hours(calendar_ids) -> dict:
    """Per-weekday shift boundaries for the given resource.calendar ids,
    derived from their attendance lines. Returns
    {cal_id: {"0": ["05:45","14:30"], ...}}; empty dict for no ids."""
    if not calendar_ids:
        return {}
    rows = execute(
        "resource.calendar.attendance", "search_read",
        [("calendar_id", "in", list(calendar_ids))],
        fields=["calendar_id", "dayofweek", "hour_from", "hour_to", "day_period"],
    )
    return _calendar_hours_from_lines(rows)


def fetch_employees() -> list[dict]:
    """All active hr.employee records with the fields we need.

    `wage_type` is an Odoo selection: 'hourly' or 'monthly'. Used by the
    late/absence report to filter out salaried managers who have
    flexible start times.
    """
    return execute(
        "hr.employee", "search_read",
        [("active", "=", True)],
        fields=["id", "name", "active", "work_email", "wage_type", "resource_calendar_id"],
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
        eid = unwrap_m2o(r["employee_id"])
        sid = unwrap_m2o(r["skill_id"])
        lid = unwrap_m2o(r["skill_level_id"])
        sname = r["skill_id"][1] if isinstance(r["skill_id"], list) else ""
        out.setdefault(eid, []).append({"skill_id": sid, "skill_name": sname, "level_id": lid})
    return out


def fetch_spanish_speaker_ids() -> set[int]:
    """Odoo employee ids who have a 'Spanish' skill (Languages type) at a
    non-zero level — i.e. level 1-3 in Odoo's Languages rating.

    Matches the skill by name (ilike 'Spanish') so it works regardless of
    skill-type wiring, and filters on hr.employee.skill.level_progress > 0
    so a level-0 / unrated entry doesn't count. Used to flag bilingual
    kiosk users; deliberately separate from fetch_skills_for so it never
    adds Languages columns to the production skills matrix.
    """
    skills = execute(
        "hr.skill", "search_read",
        [("name", "ilike", "Spanish")],
        fields=["id", "name"],
    )
    skill_ids = [s["id"] for s in skills]
    if not skill_ids:
        return set()
    rows = execute(
        "hr.employee.skill", "search_read",
        [("skill_id", "in", skill_ids), ("level_progress", ">", 0)],
        fields=["employee_id"],
    )
    out: set[int] = set()
    for r in rows:
        eid = r["employee_id"]
        out.add(unwrap_m2o(eid))
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
    attendance wins if there's somehow more than one.

    When ODOO_KIOSK_DEPARTMENT_FIELD is configured, the returned dict also
    carries ``department_id`` (int|None) and ``department_name`` (str|None)
    parsed from that Many2one, so callers can tell which department the
    person is currently punched into."""
    dept_field = _kiosk_department_field()
    fields = ["id", "employee_id", "check_in"]
    if dept_field:
        fields.append(dept_field)
    rows = execute(
        "hr.attendance", "search_read",
        [("employee_id", "=", employee_odoo_id), ("check_out", "=", False)],
        fields=fields,
        limit=1,
    )
    if not rows:
        return None
    row = rows[0]
    dept_val = row.get(dept_field) if dept_field else None
    if isinstance(dept_val, list) and dept_val:
        row["department_id"] = dept_val[0]
        row["department_name"] = dept_val[1] if len(dept_val) > 1 else None
    else:
        row["department_id"] = None
        row["department_name"] = None
    return row


def _odoo_dt_to_iso(value: Any) -> str | None:
    """Odoo returns datetimes as naive-UTC 'YYYY-MM-DD HH:MM:SS' strings
    (and False for empty). Return an ISO-8601 string with an explicit UTC
    offset, or None."""
    if not value:
        return None
    if isinstance(value, str):
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc)
        return dt.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return None


def fetch_open_attendances() -> list[dict]:
    """Every currently-open hr.attendance (check_out IS NULL), one entry
    per clocked-in employee. Returns
    [{att_id, employee_odoo_id, check_in, wc_name}, ...] where check_in is
    an ISO-8601 UTC string and wc_name is None when the kiosk WC field is
    unset or empty (e.g. a punch added by hand directly in Odoo)."""
    wc_field = _kiosk_wc_field()
    fields = ["id", "employee_id", "check_in"]
    if wc_field:
        fields.append(wc_field)
    rows = execute(
        "hr.attendance", "search_read",
        [("check_out", "=", False)],
        fields=fields,
    )
    out: list[dict] = []
    for r in rows:
        emp = r.get("employee_id")
        emp_id = unwrap_m2o(emp)
        if not emp_id:
            continue
        out.append({
            "att_id": r["id"],
            "employee_odoo_id": emp_id,
            "check_in": _odoo_dt_to_iso(r.get("check_in")),
            "wc_name": (r.get(wc_field) or None) if wc_field else None,
        })
    return out


def fetch_attendances_for_day(day) -> list[dict]:
    """Every hr.attendance whose check_in falls on `day` (site-local day,
    open AND closed), reduced to one entry per employee — their EARLIEST
    check_in plus whether any of their punches is still open.

    Returns [{employee_odoo_id, first_check_in, currently_open}, ...] where
    first_check_in is an ISO-8601 UTC string. `day` bounds are the local
    day converted to UTC, since Odoo stores naive-UTC datetimes."""
    from datetime import datetime, time as _time, timedelta
    from . import shift_config
    start_local = datetime.combine(day, _time.min, tzinfo=shift_config.SITE_TZ)
    end_local = start_local + timedelta(days=1)
    rows = execute(
        "hr.attendance", "search_read",
        [
            ("check_in", ">=", _to_odoo_dt(start_local)),
            ("check_in", "<", _to_odoo_dt(end_local)),
        ],
        fields=["id", "employee_id", "check_in", "check_out"],
    )
    agg: dict[int, dict] = {}
    for r in rows:
        emp = r.get("employee_id")
        emp_id = unwrap_m2o(emp)
        if not emp_id:
            continue
        ci = _odoo_dt_to_iso(r.get("check_in"))
        if ci is None:
            continue
        is_open = not r.get("check_out")
        cur = agg.get(emp_id)
        if cur is None:
            agg[emp_id] = {"employee_odoo_id": emp_id, "first_check_in": ci, "currently_open": is_open}
        else:
            if ci < cur["first_check_in"]:
                cur["first_check_in"] = ci
            if is_open:
                cur["currently_open"] = True
    return list(agg.values())


def fetch_attendance_intervals_for_day(day) -> list[dict]:
    """EVERY hr.attendance whose check_in falls on `day` (site-local), as full
    intervals -- NOT collapsed per employee like fetch_attendances_for_day.

    Returns [{employee_odoo_id, check_in, check_out, wc_name}, ...] where
    check_in/check_out are ISO-8601 UTC strings (check_out is None for a record
    still open) and wc_name is the kiosk WC field (None if unset/untagged).

    This is the goal's source of truth for where each operator was clocked in:
    auto-lunch splits the day into a morning + afternoon record, and mid-shift
    transfers each create their own record, so a person can legitimately have
    several intervals in a day. Day bounds are the local day converted to UTC
    (Odoo stores naive-UTC datetimes)."""
    from datetime import datetime, time as _time, timedelta
    from . import shift_config
    start_local = datetime.combine(day, _time.min, tzinfo=shift_config.SITE_TZ)
    end_local = start_local + timedelta(days=1)
    wc_field = _kiosk_wc_field()
    fields = ["id", "employee_id", "check_in", "check_out"]
    if wc_field:
        fields.append(wc_field)
    rows = execute(
        "hr.attendance", "search_read",
        [
            ("check_in", ">=", _to_odoo_dt(start_local)),
            ("check_in", "<", _to_odoo_dt(end_local)),
        ],
        fields=fields,
    )
    out: list[dict] = []
    for r in rows:
        emp = r.get("employee_id")
        emp_id = unwrap_m2o(emp)
        if not emp_id:
            continue
        ci = _odoo_dt_to_iso(r.get("check_in"))
        if ci is None:
            continue
        out.append({
            "employee_odoo_id": emp_id,
            "check_in": ci,
            "check_out": _odoo_dt_to_iso(r.get("check_out")),
            "wc_name": (r.get(wc_field) or None) if wc_field else None,
        })
    return out


def set_attendance_wc(attendance_id: int, wc_name: str | None) -> None:
    """Write the kiosk WC (and resolved department) onto an existing
    hr.attendance. No-op when the WC field isn't configured or wc_name is
    empty. Used when the sync adopts a manually-created open attendance, so
    kiosk WC/department reports still attribute it."""
    wc_field = _kiosk_wc_field()
    if not wc_field or not wc_name:
        return
    payload: dict[str, Any] = {wc_field: wc_name}
    dept_field = _kiosk_department_field()
    if dept_field:
        dept_id = _department_id_for_wc(wc_name)
        if dept_id:
            payload[dept_field] = dept_id
    execute("hr.attendance", "write", [attendance_id], payload)


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


def undo_transfer(closed_id: int | None, new_id: int) -> None:
    """Reverse a transfer: delete the newly opened attendance and reopen the
    previously closed one (clear its check_out). ``closed_id`` is None when the
    transfer actually opened a fresh punch (person had none) — then we only
    delete the new row."""
    execute("hr.attendance", "unlink", [new_id])
    if closed_id:
        execute("hr.attendance", "write", [closed_id], {"check_out": False})


# ---------- Time-off reads (2026-05-27) ----------

_LEAVE_TYPES_TTL_SECONDS = 10 * 60
# (types_list, expires_at_epoch). Module-level so a process restart clears it.
_leave_types_cache: tuple[list[dict], float] | None = None


def _norm_requires_allocation(value) -> str:
    """Canonicalize hr.leave.type.requires_allocation to 'yes' / 'no'.

    Odoo <=18 exposes this as a Selection ('yes'/'no'); Odoo 19+ changed it
    to a Boolean, so XML-RPC returns a Python bool. The rest of the app —
    the leave_types_cache TEXT/CHECK('yes','no') column and the kiosk's
    `data-requires-alloc` attribute compared against the literal "yes" —
    assumes the string form. Normalizing here, at the Odoo boundary, keeps
    every downstream consumer working regardless of Odoo version.

    A raw boolean True both fails the cache CHECK *and* renders into the
    kiosk option as "True" (!= "yes"), which is what made a fully-configured
    Paid Time Off type show "No allocation tracked".
    """
    if isinstance(value, str):
        return "yes" if value.strip().lower() in ("yes", "true", "1") else "no"
    return "yes" if value else "no"


def fetch_leave_types() -> list[dict]:
    """All active hr.leave.type, cached in-process for 10 minutes.

    Returns [{id, name, request_unit, requires_allocation, color, active}, ...]
    with ``requires_allocation`` normalized to the 'yes'/'no' strings the rest
    of the app expects (Odoo 19+ returns it as a boolean — see
    ``_norm_requires_allocation``).
    """
    global _leave_types_cache
    now = time.time()
    if _leave_types_cache and _leave_types_cache[1] > now:
        return _leave_types_cache[0]
    rows = execute(
        "hr.leave.type", "search_read",
        [("active", "=", True)],
        fields=["id", "name", "request_unit",
                "requires_allocation", "color", "active"],
    )
    for r in rows:
        r["requires_allocation"] = _norm_requires_allocation(
            r.get("requires_allocation"))
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
            # Odoo 19 renamed hr.leave.number_of_hours_display -> number_of_hours
            # (the _display variant was dropped from hr.leave; it survives on
            # hr.leave.allocation — see fetch_balances_for).
            "number_of_days", "number_of_hours", "name",
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
    cal_id = unwrap_m2o(cal_field)
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


# hr.leave.allocation states that contribute to allocated_total.
_ALLOCATION_STATE_VALIDATED = "validate"
# hr.leave states pulled together; "validate" is taken, others are pending.
_LEAVE_STATES_OPEN = ("confirm", "validate1", "validate")
_LEAVE_STATE_TAKEN = "validate"


def fetch_balances_for(employee_odoo_id: int) -> list[dict]:
    """Per-leave-type balance for one employee, via direct aggregation.

    Algorithm: for each leave type, sum allocations in state='validate' minus
    leaves in state='validate' (taken) and state IN ('confirm','validate1')
    (pending). Returns one row per type, including types with zero allocation.

    The `unit` field is 'days' when type.request_unit == 'day' or 'half_day',
    and 'hours' when type.request_unit == 'hour'. Numeric fields use the
    matching unit (days_display vs hours_display from Odoo).
    """
    types = fetch_leave_types()
    # NOTE on field-name asymmetry (Odoo 19): hr.leave.allocation still
    # exposes the *_display duration fields, but hr.leave dropped
    # number_of_hours_display in favor of number_of_hours. So the allocation
    # query keeps _display while the leave query uses number_of_hours.
    allocations = execute(
        "hr.leave.allocation", "search_read",
        [("employee_id", "=", employee_odoo_id),
         ("state", "=", _ALLOCATION_STATE_VALIDATED)],
        fields=["holiday_status_id", "number_of_days_display",
                "number_of_hours_display"],
    )
    leaves = execute(
        "hr.leave", "search_read",
        [("employee_id", "=", employee_odoo_id),
         ("state", "in", list(_LEAVE_STATES_OPEN))],
        fields=["holiday_status_id", "state",
                "number_of_days", "number_of_hours"],
    )

    def _hsid(row: dict) -> int:
        """Many2one fields come as [id, name] from Odoo — unwrap to id."""
        v = row["holiday_status_id"]
        return unwrap_m2o(v)

    out: list[dict] = []
    for t in types:
        tid = t["id"]
        unit = "hours" if t["request_unit"] == "hour" else "days"
        # Allocations keep the _display field name; leaves use number_of_hours
        # (Odoo 19 — see the search_read field lists above).
        alloc_field = ("number_of_hours_display" if unit == "hours"
                       else "number_of_days_display")
        leave_field = ("number_of_hours" if unit == "hours"
                       else "number_of_days")
        alloc = 0.0
        for a in allocations:
            if _hsid(a) == tid:
                alloc += float(a.get(alloc_field) or 0)
        taken = 0.0
        pending = 0.0
        for lv in leaves:
            if _hsid(lv) != tid:
                continue
            val = float(lv.get(leave_field) or 0)
            if lv["state"] == _LEAVE_STATE_TAKEN:
                taken += val
            else:
                pending += val
        available = alloc - taken
        practical = alloc - taken - pending
        out.append({
            "holiday_status_id": tid,
            "unit": unit,
            "allocated_total": alloc,
            "taken": taken,
            "pending": pending,
            "available": available,
            "available_practical": practical,
        })
    return out


# ---------- Time-off writes (2026-05-27) ----------


def create_leave(
    employee_odoo_id: int,
    holiday_status_id: int,
    date_from,
    date_to,
    hour_from: float | None = None,
    hour_to: float | None = None,
    note: str | None = None,
) -> int:
    """Create an hr.leave and return the new leave id.

    A bare ``create`` lands in Odoo's ``'draft'`` ("To Submit") state — it
    does NOT enter the approval workflow on its own (it won't show in the
    manager's "Waiting for Approval" queue and won't be deducted from the
    balance). Callers that want the request to become a real pending approval
    must follow with ``confirm_leave`` (see ``time_off_sync._push_create``).

    Sets request_unit_hours=True with float hour_from/hour_to when given;
    otherwise creates a day-unit leave for the date range.
    """
    payload: dict[str, Any] = {
        "employee_id": employee_odoo_id,
        "holiday_status_id": holiday_status_id,
        "request_date_from": date_from.isoformat(),
        "request_date_to": date_to.isoformat(),
    }
    if hour_from is not None and hour_to is not None:
        payload["request_unit_hours"] = True
        payload["request_hour_from"] = float(hour_from)
        payload["request_hour_to"] = float(hour_to)
    if note:
        payload["name"] = note
    return execute("hr.leave", "create", payload)


def confirm_leave(leave_id: int) -> None:
    """Submit a draft hr.leave into the approval workflow.

    Calls ``action_confirm`` (the "Submit" / "Confirm Request" button),
    moving a ``'draft'`` ("To Submit") leave to ``'confirm'`` ("To Approve")
    — or straight to validated for no-validation types. Reads the current
    state first and only confirms drafts, because Odoo's ``action_confirm``
    raises on records already past draft; this keeps the call idempotent
    across sync retries and the duplicate-leave path.
    """
    rows = execute("hr.leave", "read", [leave_id], ["state"])
    if rows and rows[0].get("state") == "draft":
        execute("hr.leave", "action_confirm", [leave_id])


def write_leave(leave_id: int, **fields: Any) -> None:
    """Update fields on an existing hr.leave."""
    execute("hr.leave", "write", [leave_id], fields)


def refuse_leave(leave_id: int) -> None:
    """Call hr.leave.action_refuse — handles pending-cancel and
    approved-cancel via the same workflow."""
    execute("hr.leave", "action_refuse", [leave_id])


def fetch_public_holidays(start_d, end_d) -> list[dict]:
    """Company-wide public holidays from Odoo's resource.calendar.leaves
    (rows with resource_id=False). Returns [{id, name, date_from, date_to}, ...]
    for any holiday whose [date_from, date_to] overlaps the requested range.

    ``resource.calendar.leaves.date_from`` is a datetime field (not date), so
    the domain needs the time component. ``resource_id=False`` means the row
    applies to anyone on the calendar — typically "4th of July", "Christmas
    Day", etc., as opposed to per-employee leaves which would set
    ``resource_id`` to a specific employee.
    """
    domain = [
        ("resource_id", "=", False),
        ("date_to", ">=", start_d.isoformat() + " 00:00:00"),
        ("date_from", "<=", end_d.isoformat() + " 23:59:59"),
    ]
    return execute(
        "resource.calendar.leaves", "search_read",
        domain,
        fields=["id", "name", "date_from", "date_to"],
    )


def find_duplicate_leave(
    employee_odoo_id: int,
    holiday_status_id: int,
    date_from,
    date_to,
) -> int | None:
    """Return id of an existing hr.leave matching this employee+type+range
    in non-rejected state, else None. Retry-dedupe guard."""
    rows = execute(
        "hr.leave", "search_read",
        [("employee_id", "=", employee_odoo_id),
         ("holiday_status_id", "=", holiday_status_id),
         ("request_date_from", "=", date_from.isoformat()),
         ("request_date_to", "=", date_to.isoformat()),
         ("state", "in", list(_LEAVE_STATES_OPEN))],
        fields=["id"], limit=1,
    )
    return rows[0]["id"] if rows else None
