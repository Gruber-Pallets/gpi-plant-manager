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

import base64
import os
import threading
import time
import xmlrpc.client
from datetime import datetime, UTC
from typing import Any

from . import _odoo_calendars, _odoo_skills


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

# Socket timeout for every Odoo XML-RPC connection, in seconds. Without one,
# a hung TCP connection blocks its calling thread forever — for a background
# warmer that means the loop silently stops ticking until the next deploy.
_XMLRPC_TIMEOUT_SECONDS = 15


class _TimeoutTransport(xmlrpc.client.Transport):
    """http transport whose persistent connection gets a socket timeout."""

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = _XMLRPC_TIMEOUT_SECONDS
        return conn


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    """https transport whose persistent connection gets a socket timeout."""

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = _XMLRPC_TIMEOUT_SECONDS
        return conn


def _server_proxy(url: str) -> xmlrpc.client.ServerProxy:
    """ServerProxy with a socket timeout, picking Transport vs SafeTransport
    to match the URL scheme (ODOO_URL is https in prod; http covers local
    dev against a bare Odoo container)."""
    transport = (_TimeoutSafeTransport() if url.startswith("https")
                 else _TimeoutTransport())
    return xmlrpc.client.ServerProxy(url, transport=transport)


def _reset_cache_for_tests() -> None:
    """Clear cached uid + per-thread object proxy; tests call this between cases."""
    global _uid_cache, _feedback_project_id
    _uid_cache = None
    _feedback_project_id = None
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
        proxy = _server_proxy(f"{url}/xmlrpc/2/object")
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
    common = _server_proxy(f"{url}/xmlrpc/2/common")
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


SKILL_TYPE_NAMES = _odoo_skills.SKILL_TYPE_NAMES

FEEDBACK_PROJECT_NAME = "Plant Manager"
FEEDBACK_STAGES = ("New", "In Progress", "Done", "Rejected")
FEEDBACK_DONE_STAGE = "Done"
FEEDBACK_REJECTED_STAGE = "Rejected"

_feedback_project_id: int | None = None


def fetch_skill_columns_with_types() -> list[dict]:
    return _odoo_skills.fetch_skill_columns_with_types(execute, unwrap_m2o)


def fetch_skill_columns() -> list[str]:
    return _odoo_skills.fetch_skill_columns(execute, unwrap_m2o)


def fetch_skill_level_buckets() -> dict[int, int]:
    return _odoo_skills.fetch_skill_level_buckets(execute, unwrap_m2o)


def set_employee_skill_level(employee_odoo_id: int, skill_odoo_id: int, bucket: int) -> None:
    _odoo_skills.set_employee_skill_level(
        execute,
        unwrap_m2o,
        employee_odoo_id,
        skill_odoo_id,
        bucket,
    )


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


_float_to_hhmm = _odoo_calendars.float_to_hhmm
_calendar_hours_from_lines = _odoo_calendars.calendar_hours_from_lines
_calendar_lunch_windows_from_lines = (
    _odoo_calendars.calendar_lunch_windows_from_lines
)


# Odoo "Schedule Type" on resource.calendar. Confirmed against live Odoo
# (Task 6 Step 1). Odoo 18 exposes flexible scheduling as the boolean
# `flexible_hours`; if your instance uses a selection, change this name —
# _is_flexible() already accepts both a bool and the string 'flexible'.
SCHEDULE_TYPE_FIELD = _odoo_calendars.SCHEDULE_TYPE_FIELD


_is_flexible = _odoo_calendars.is_flexible


def fetch_work_schedules() -> list[dict]:
    return _odoo_calendars.fetch_work_schedules(execute)


def fetch_calendar_hours(calendar_ids) -> dict:
    return _odoo_calendars.fetch_calendar_hours(execute, calendar_ids)


_calendar_lunch_windows_cache: dict[tuple[int, ...], tuple[dict, float]] = {}
_CALENDAR_LUNCH_TTL_SECONDS = 10 * 60


def fetch_calendar_lunch_windows(calendar_ids) -> dict:
    ids = tuple(sorted({int(i) for i in (calendar_ids or []) if i is not None}))
    if not ids:
        return {}
    now = time.monotonic()
    cached = _calendar_lunch_windows_cache.get(ids)
    if cached is not None and cached[1] > now:
        return cached[0]
    out = _odoo_calendars.fetch_calendar_lunch_windows(execute, ids)
    _calendar_lunch_windows_cache[ids] = (
        out,
        now + _CALENDAR_LUNCH_TTL_SECONDS,
    )
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
        fields=["id", "name", "active", "work_email", "wage_type", "resource_calendar_id"],
    )


def fetch_skills_for(employee_ids: list[int]) -> dict[int, list[dict]]:
    return _odoo_skills.fetch_skills_for(execute, employee_ids, unwrap_m2o)


def fetch_spanish_speaker_ids() -> set[int]:
    return _odoo_skills.fetch_spanish_speaker_ids(execute, unwrap_m2o)


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
        ts = ts.astimezone(UTC).replace(tzinfo=None)
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


def fetch_attendances_missing_wc(since) -> list[dict]:
    """hr.attendance from `since` (a tz-aware datetime) with NO kiosk
    work-center tag. Returns
    [{att_id, employee_odoo_id, employee_name, check_in (ISO), check_out (ISO|None)}].

    Returns [] (and logs once) when the kiosk WC field isn't configured — with
    no WC field we can't tell tagged from untagged, so the alert stays dark
    rather than flagging every record."""
    import logging
    wc_field = _kiosk_wc_field()
    if not wc_field:
        logging.getLogger(__name__).warning(
            "ODOO_KIOSK_WC_FIELD not configured; missing-work-center alert disabled"
        )
        return []
    rows = execute(
        "hr.attendance", "search_read",
        [("check_in", ">=", _to_odoo_dt(since)), (wc_field, "=", False)],
        fields=["id", "employee_id", "check_in", "check_out"],
        order="check_in desc",
        limit=500,
    )
    out: list[dict] = []
    for r in rows:
        emp = r.get("employee_id")
        out.append({
            "att_id": r["id"],
            "employee_odoo_id": unwrap_m2o(emp),
            "employee_name": emp[1] if isinstance(emp, list) and len(emp) > 1 else None,
            "check_in": _odoo_dt_to_iso(r.get("check_in")),
            "check_out": _odoo_dt_to_iso(r.get("check_out")),
        })
    return out


def _odoo_dt_to_iso(value: Any) -> str | None:
    """Odoo returns datetimes as naive-UTC 'YYYY-MM-DD HH:MM:SS' strings
    (and False for empty). Return an ISO-8601 string with an explicit UTC
    offset, or None."""
    if not value:
        return None
    if isinstance(value, str):
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC)
        return dt.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return None


def _is_zero_duration_attendance(row: dict) -> bool:
    """True for closed Odoo rows with no meaningful worked interval.

    Odoo can surface cleanup/no-op rows around midnight as 12:00:00 to
    12:00:01, which displays as 00:00 worked time. Those should not make the
    dashboard treat someone as present for the day.
    """
    check_in = _odoo_dt_to_iso(row.get("check_in"))
    check_out = _odoo_dt_to_iso(row.get("check_out"))
    if not check_in or not check_out:
        return False
    try:
        start = datetime.fromisoformat(check_in)
        end = datetime.fromisoformat(check_out)
    except (TypeError, ValueError):
        return False
    return 0 <= (end - start).total_seconds() < 60


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
        if _is_zero_duration_attendance(r):
            continue
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
        if _is_zero_duration_attendance(r):
            continue
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


def clear_attendance_wc(attendance_id: int) -> None:
    """Clear the kiosk WC (and resolved department) on an hr.attendance — the
    reverse of set_attendance_wc, used by inbox undo of a missing-WC assign.
    Writes the field(s) to False directly (set_attendance_wc guards against a
    falsy value and would no-op) so the missing-WC warmer re-flags the row.
    No-op when the WC field isn't configured."""
    wc_field = _kiosk_wc_field()
    if not wc_field:
        return
    payload: dict[str, Any] = {wc_field: False}
    dept_field = _kiosk_department_field()
    if dept_field:
        payload[dept_field] = False
    execute("hr.attendance", "write", [attendance_id], payload)


def _overtime_status_for_attendance(attendance_id: int) -> str:
    rows = execute(
        "hr.attendance", "search_read",
        [("id", "=", attendance_id)],
        fields=["overtime_hours"],
        limit=1,
    )
    try:
        overtime_hours = float(rows[0].get("overtime_hours") or 0)
    except (IndexError, TypeError, ValueError):
        overtime_hours = 0
    return "to_approve" if overtime_hours > 0 else "approved"


def clock_in(employee_odoo_id: int, wc_name: str | None, ts: datetime) -> int:
    """Create a new hr.attendance with check_in=ts. Returns the new id.

    Kiosk-created records are marked as ``approved`` so Odoo's manager-review
    overtime setting does not put every normal plant punch into Management >
    To Approve. Actual missed-punch corrections still have their own local
    alert flow.

    Writes the WC name into ODOO_KIOSK_WC_FIELD when configured (Char).
    Writes the WC's resolved Odoo department into ODOO_KIOSK_DEPARTMENT_FIELD
    when configured (Many2one to hr.department), so reports that group
    hours by department attribute kiosk-created attendance correctly
    even when an employee transfers between departments mid-shift."""
    payload: dict[str, Any] = {
        "employee_id": employee_odoo_id,
        "check_in": _to_odoo_dt(ts),
        "in_mode": "kiosk",
        "overtime_status": "approved",
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


def clock_out(attendance_id: int, ts: datetime, *, mode: str = "kiosk") -> None:
    """Set check_out on an existing hr.attendance. Safe to call on an
    already-closed record — Odoo just overwrites the timestamp."""
    execute(
        "hr.attendance", "write",
        [attendance_id],
        {
            "check_out": _to_odoo_dt(ts),
            "out_mode": mode,
        },
    )
    execute(
        "hr.attendance", "write",
        [attendance_id],
        {"overtime_status": _overtime_status_for_attendance(attendance_id)},
    )


def transfer(
    employee_odoo_id: int,
    new_wc_name: str | None,
    ts: datetime,
    current: dict | None = None,
) -> tuple[int | None, int]:
    """Close the employee's current open hr.attendance and open a new one
    at the new WC. Returns (closed_id, new_id). If the employee has no
    open attendance, closed_id is None — the new one is still opened so
    the kiosk fails gracefully when local state and Odoo state disagree.

    Callers that just fetched the live current row may pass it as ``current``
    to avoid a second XML-RPC lookup and guarantee the same row is closed."""
    if current is None:
        current = (
            _cached_current_attendance_for_transfer(employee_odoo_id)
            or get_current_attendance(employee_odoo_id)
        )
    closed_id: int | None = None
    if current:
        clock_out(current["id"], ts)
        closed_id = current["id"]
    new_id = clock_in(employee_odoo_id, new_wc_name, ts)
    return closed_id, new_id


def _cached_current_attendance_for_transfer(employee_odoo_id: int) -> dict | None:
    """Return a fresh positive open-attendance cache hit for transfer().

    We only trust positive hits from the warmer snapshot. A missing employee
    can be a normal warmer lag immediately after a kiosk clock-in, so transfer
    falls back to the live Odoo lookup for misses, stale rows, and cache errors.
    """
    try:
        from . import live_cache
        snapshot, refreshed_at = live_cache.read_open_attendance()
        if snapshot is None or live_cache.is_stale(refreshed_at):
            return None
        row = snapshot.get(str(employee_odoo_id))
        if not row:
            return None
        att_id = row.get("att_id")
        if not att_id:
            return None
        return {
            "id": int(att_id),
            "employee_id": employee_odoo_id,
            "check_in": row.get("check_in"),
            "department_id": None,
            "department_name": None,
        }
    except Exception:
        return None


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


def fetch_leaves_for_range(start_d, end_d, modified_since=None) -> list[dict]:
    """All hr.leave records overlapping [start_d, end_d] for active employees.

    Overlap rule: request_date_to >= start_d AND request_date_from <= end_d.
    Returns raw search_read dicts; caller normalizes Many2one fields.

    ``modified_since`` (optional tz-aware datetime) additionally requires
    ``write_date >`` it — the incremental poller's filter, so a normal tick
    only pulls leaves that changed since the last poll.
    """
    domain = [
        ("request_date_to", ">=", start_d.isoformat()),
        ("request_date_from", "<=", end_d.isoformat()),
        ("employee_id.active", "=", True),
    ]
    if modified_since is not None:
        domain.append(("write_date", ">", _to_odoo_dt(modified_since)))
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


_RESOURCE_CALENDAR_TTL_SECONDS = 10 * 60
# {employee_odoo_id: (result_or_None, expires_at_epoch)} — same pattern as
# _leave_types_cache. The kiosk details form calls fetch_resource_calendar on
# every render (3 serial XML-RPC calls) and working schedules basically never
# change intraday. None ("no calendar") is cached too.
_resource_calendar_cache: dict[int, tuple[dict | None, float]] = {}


def fetch_resource_calendar(employee_odoo_id: int) -> dict | None:
    """Returns {hour_from, hour_to, lunch_from, lunch_to, tz} or None,
    cached in-process for 10 minutes per employee.

    Derives hour_from/hour_to from min/max of resource.calendar.attendance
    rows (excluding lunch periods). If lunch periods are configured on the
    calendar, returns them as well. Tz comes from resource.calendar.
    """
    now = time.time()
    cached = _resource_calendar_cache.get(employee_odoo_id)
    if cached and cached[1] > now:
        return cached[0]
    result = _fetch_resource_calendar_uncached(employee_odoo_id)
    _resource_calendar_cache[employee_odoo_id] = (
        result, now + _RESOURCE_CALENDAR_TTL_SECONDS)
    return result


def _fetch_resource_calendar_uncached(employee_odoo_id: int) -> dict | None:
    return _odoo_calendars.fetch_resource_calendar(
        execute, unwrap_m2o, employee_odoo_id
    )


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

    Thin wrapper over ``fetch_balances_for_many`` for the single-employee
    interactive path (kiosk wizard open / manual refresh).
    """
    return fetch_balances_for_many([employee_odoo_id])[employee_odoo_id]


def fetch_balances_for_many(employee_odoo_ids: list[int]) -> dict[int, list[dict]]:
    """``fetch_balances_for`` for MANY employees in the same 2 XML-RPC calls.

    The allocation and leave queries use ``("employee_id", "in", ids)`` and
    rows are grouped by employee in Python, so the 10-min balance sweep costs
    2 Odoo round-trips total instead of 2 per stale employee. Returns
    {employee_odoo_id: [balance rows]} with an entry (possibly all-zero) for
    every requested id.
    """
    ids = list(dict.fromkeys(employee_odoo_ids))   # de-dup, keep order
    if not ids:
        return {}
    types = fetch_leave_types()
    # NOTE on field-name asymmetry (Odoo 19): hr.leave.allocation still
    # exposes the *_display duration fields, but hr.leave dropped
    # number_of_hours_display in favor of number_of_hours. So the allocation
    # query keeps _display while the leave query uses number_of_hours.
    allocations = execute(
        "hr.leave.allocation", "search_read",
        [("employee_id", "in", ids),
         ("state", "=", _ALLOCATION_STATE_VALIDATED)],
        fields=["employee_id", "holiday_status_id", "number_of_days_display",
                "number_of_hours_display"],
    )
    leaves = execute(
        "hr.leave", "search_read",
        [("employee_id", "in", ids),
         ("state", "in", list(_LEAVE_STATES_OPEN))],
        fields=["employee_id", "holiday_status_id", "state",
                "number_of_days", "number_of_hours"],
    )
    alloc_by_emp: dict[int, list[dict]] = {eid: [] for eid in ids}
    leave_by_emp: dict[int, list[dict]] = {eid: [] for eid in ids}
    for a in allocations:
        eid = unwrap_m2o(a.get("employee_id"))
        if eid in alloc_by_emp:
            alloc_by_emp[eid].append(a)
    for lv in leaves:
        eid = unwrap_m2o(lv.get("employee_id"))
        if eid in leave_by_emp:
            leave_by_emp[eid].append(lv)
    return {
        eid: _aggregate_balances(types, alloc_by_emp[eid], leave_by_emp[eid])
        for eid in ids
    }


def _aggregate_balances(
    types: list[dict], allocations: list[dict], leaves: list[dict],
) -> list[dict]:
    """Reduce one employee's allocation + leave rows to per-type balances."""

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


def approve_leave(leave_id: int) -> str | None:
    """Approve a pending hr.leave and return its final Odoo state.

    Odoo 19 generally creates Time Off in ``confirm``. Older flows may still
    expose ``draft`` first, and two-step approval can pass through
    ``validate1``. Read between each workflow action so this is safe to call
    on duplicates and retries.
    """
    state: str | None = None
    for _ in range(3):
        rows = execute("hr.leave", "read", [leave_id], ["state"])
        state = rows[0].get("state") if rows else None
        if state == "draft":
            confirm_leave(leave_id)
            continue
        if state in ("confirm", "validate1"):
            execute("hr.leave", "action_approve", [leave_id])
            continue
        return state
    rows = execute("hr.leave", "read", [leave_id], ["state"])
    return rows[0].get("state") if rows else state


def write_leave(leave_id: int, **fields: Any) -> None:
    """Update fields on an existing hr.leave."""
    execute("hr.leave", "write", [leave_id], fields)


def refuse_leave(leave_id: int) -> None:
    """Call hr.leave.action_refuse — handles pending-cancel and
    approved-cancel via the same workflow."""
    execute("hr.leave", "action_refuse", [leave_id])


def reset_leave_to_confirm(leave_id: int) -> None:
    """Reset a refused/cancelled hr.leave back to 'confirm' (To Approve) so
    it can re-enter the approval workflow — used by the local-record
    backfill to replay a leave Odoo previously rejected.

    LIVE-VERIFIED direct state write: this Odoo version has no usable reset
    action ('hr.leave.action_draft' does not exist, and action_reset_confirm
    crashes upstream with a super() AttributeError). The state field is
    change-tracked, so the write still leaves a chatter breadcrumb."""
    execute("hr.leave", "write", [leave_id], {"state": "confirm"})


def fetch_leave_state(leave_id: int) -> str | None:
    """Current hr.leave state, or None when the record no longer exists.
    search_read (not read) so a deleted leave returns [] instead of
    raising."""
    rows = execute(
        "hr.leave", "search_read",
        [("id", "=", leave_id)],
        fields=["state"],
    )
    return rows[0]["state"] if rows else None


def post_leave_message(leave_id: int, body: str) -> None:
    """Post a message to an hr.leave's chatter so the employee is notified.

    ``body`` is passed as a keyword arg because ``execute`` forwards
    **kwargs as Odoo's keyword args (see ``execute``); the leave id is the
    positional recordset. Used to deliver a denial reason back to the
    requester. Callers treat this as best-effort — a failed post must not
    roll back a completed refusal.
    """
    execute("hr.leave", "message_post", [leave_id], body=body)


_PUBLIC_HOLIDAYS_TTL_SECONDS = 10 * 60
# {(start_d, end_d): (rows, expires_at_epoch)} — same pattern as
# _leave_types_cache, keyed by range because the Who's-Out calendar asks for
# varying windows. Module-level so a process restart clears it.
_public_holidays_cache: dict[tuple, tuple[list[dict], float]] = {}


def fetch_public_holidays(start_d, end_d) -> list[dict]:
    """Company-wide public holidays from Odoo's resource.calendar.leaves
    (rows with resource_id=False), cached in-process for 10 minutes per
    requested range (the Who's-Out calendar calls this on every render).
    Returns [{id, name, date_from, date_to, calendar_id}, ...]
    for any holiday whose [date_from, date_to] overlaps the requested range.
    ``calendar_id`` is False for company-wide records, or [id, name] when the
    holiday is scoped to one working schedule (the backfill reconciler uses
    this to decide whether a holiday blocks a given employee).

    ``resource.calendar.leaves.date_from`` is a datetime field (not date), so
    the domain needs the time component. ``resource_id=False`` means the row
    applies to anyone on the calendar — typically "4th of July", "Christmas
    Day", etc., as opposed to per-employee leaves which would set
    ``resource_id`` to a specific employee.
    """
    now = time.time()
    cached = _public_holidays_cache.get((start_d, end_d))
    if cached and cached[1] > now:
        return cached[0]
    domain = [
        ("resource_id", "=", False),
        ("date_to", ">=", start_d.isoformat() + " 00:00:00"),
        ("date_from", "<=", end_d.isoformat() + " 23:59:59"),
    ]
    rows = execute(
        "resource.calendar.leaves", "search_read",
        domain,
        fields=["id", "name", "date_from", "date_to", "calendar_id"],
    )
    # Drop expired entries so navigating many ranges can't grow the dict.
    for k in [k for k, (_, exp) in _public_holidays_cache.items() if exp <= now]:
        del _public_holidays_cache[k]
    _public_holidays_cache[(start_d, end_d)] = (
        rows, now + _PUBLIC_HOLIDAYS_TTL_SECONDS)
    return rows


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


def _ensure_feedback_stages(project_id: int) -> None:
    existing = execute(
        "project.task.type", "search_read",
        [("project_ids", "in", [project_id])], fields=["name"],
    ) or []
    have = {r["name"] for r in existing}
    for seq, name in enumerate(FEEDBACK_STAGES):
        if name in have:
            continue
        execute("project.task.type", "create", {
            "name": name,
            "sequence": seq,
            "fold": name in (FEEDBACK_DONE_STAGE, FEEDBACK_REJECTED_STAGE),
            "project_ids": [(4, project_id)],
        })


def ensure_feedback_project() -> int:
    """Find-or-create the 'Plant Manager' project (+ its stages); cache the id."""
    global _feedback_project_id
    if _feedback_project_id is not None:
        return _feedback_project_id
    found = execute(
        "project.project", "search_read",
        [("name", "=", FEEDBACK_PROJECT_NAME)], fields=["id"], limit=1,
    )
    if found:
        project_id = found[0]["id"]
    else:
        project_id = execute("project.project", "create", {"name": FEEDBACK_PROJECT_NAME})
    _ensure_feedback_stages(project_id)
    _feedback_project_id = project_id
    return project_id


def ensure_feedback_tag(name: str) -> int:
    """Find-or-create a project.tags row by name; return its id."""
    found = execute(
        "project.tags", "search_read",
        [("name", "=", name)], fields=["id"], limit=1,
    )
    if found:
        return found[0]["id"]
    return execute("project.tags", "create", {"name": name})


def create_feedback_task(
    project_id: int,
    name: str,
    description_html: str,
    assignee_uid: int,
    tag_id: int | None,
    deadline: str,
) -> int:
    """Create a project.task. Tries Odoo 16/17 `user_ids` (m2m), falls back to
    legacy `user_id` (m2o) if the field is rejected."""
    base = {
        "name": name,
        "project_id": project_id,
        "description": description_html,
        "date_deadline": deadline,
    }
    if tag_id:
        base["tag_ids"] = [(6, 0, [tag_id])]
    try:
        return execute("project.task", "create",
                       dict(base, user_ids=[(6, 0, [assignee_uid])]))
    except xmlrpc.client.Fault as fault:
        # Only retry when Odoo rejected the `user_ids` field itself (older
        # versions expose the m2o `user_id` instead). Any other Fault — access
        # rights, validation, a transient server error — is a real failure and
        # must propagate, not trigger a second create attempt.
        if "user_ids" not in (fault.faultString or ""):
            raise
        return execute("project.task", "create",
                       dict(base, user_id=assignee_uid))


def update_task(task_id: int, **fields: Any) -> None:
    """Write fields on a project.task (e.g. description=..., active=False)."""
    execute("project.task", "write", [task_id], fields)


def post_task_message(task_id: int, body: str) -> None:
    """Post a message to a project.task's chatter (mirrors post_leave_message:
    `body` is forwarded as Odoo's keyword arg by `execute`)."""
    execute("project.task", "message_post", [task_id], body=body)


def add_task_attachment(
    task_id: int, filename: str, mimetype: str | None, raw_bytes: bytes
) -> int:
    """Attach a file to a project.task as an ir.attachment."""
    return execute("ir.attachment", "create", {
        "name": filename,
        "datas": base64.b64encode(raw_bytes).decode("ascii"),
        "res_model": "project.task",
        "res_id": task_id,
        "mimetype": mimetype or "application/octet-stream",
    })


def fetch_task_stage_names(task_ids) -> dict[int, str | None]:
    """Return {task_id: stage name} for the given project.task ids."""
    ids = [int(t) for t in task_ids if t]
    if not ids:
        return {}
    rows = execute("project.task", "read", ids, fields=["id", "stage_id"]) or []
    out: dict[int, str | None] = {}
    for r in rows:
        stage = r.get("stage_id")
        out[r["id"]] = stage[1] if isinstance(stage, (list, tuple)) and len(stage) > 1 else None
    return out


def feedback_status_bucket(stage_name: str | None) -> str:
    """Collapse an Odoo stage name to open / done / rejected.

    Matching is by exact stage name, so renaming the "Done"/"Rejected" stages
    in the Odoo UI would make those tasks read as "open" here. The stages are
    seeded by `_ensure_feedback_stages`; leave their names as-is.
    """
    if stage_name == FEEDBACK_DONE_STAGE:
        return "done"
    if stage_name == FEEDBACK_REJECTED_STAGE:
        return "rejected"
    return "open"
