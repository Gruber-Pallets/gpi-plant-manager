"""Private Odoo attendance reads and normalization used by the client facade."""

from __future__ import annotations

import logging
from datetime import date, datetime, time as _time, timedelta, UTC
from typing import Any, Callable

from . import shift_config


def _unwrap_m2o(value: Any) -> Any:
    return value[0] if isinstance(value, (list, tuple)) and value else value


def to_odoo_dt(ts: datetime) -> str:
    """Odoo expects naive UTC strings in 'YYYY-MM-DD HH:MM:SS' format.
    Accepts aware or naive datetimes; aware ones are converted to UTC."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC).replace(tzinfo=None)
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def odoo_dt_to_iso(value: Any) -> str | None:
    """Odoo returns datetimes as naive-UTC 'YYYY-MM-DD HH:MM:SS' strings
    (and False for empty). Return an ISO-8601 string with an explicit UTC
    offset, or None."""
    if not value:
        return None
    if isinstance(value, str):
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC
        )
        return dt.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return None


def is_zero_duration_attendance(row: dict) -> bool:
    """True for closed Odoo rows with no meaningful worked interval.

    Odoo can surface cleanup/no-op rows around midnight as 12:00:00 to
    12:00:01, which displays as 00:00 worked time. Those should not make the
    dashboard treat someone as present for the day.
    """
    check_in = odoo_dt_to_iso(row.get("check_in"))
    check_out = odoo_dt_to_iso(row.get("check_out"))
    if not check_in or not check_out:
        return False
    try:
        start = datetime.fromisoformat(check_in)
        end = datetime.fromisoformat(check_out)
    except (TypeError, ValueError):
        return False
    return 0 <= (end - start).total_seconds() < 60


def get_current_attendance(
    execute_fn: Callable[..., Any],
    employee_odoo_id: int,
    wc_field: str | None,
    department_field: str | None,
) -> dict | None:
    """Return the most recent open attendance row for an employee."""
    del wc_field
    fields = ["id", "employee_id", "check_in"]
    if department_field:
        fields.append(department_field)
    rows = execute_fn(
        "hr.attendance",
        "search_read",
        [("employee_id", "=", employee_odoo_id), ("check_out", "=", False)],
        fields=fields,
        limit=1,
    )
    if not rows:
        return None
    row = rows[0]
    department_value = (
        row.get(department_field) if department_field else None
    )
    if isinstance(department_value, list) and department_value:
        row["department_id"] = department_value[0]
        row["department_name"] = (
            department_value[1] if len(department_value) > 1 else None
        )
    else:
        row["department_id"] = None
        row["department_name"] = None
    return row


def fetch_attendances_missing_wc(
    execute_fn: Callable[..., Any], since, wc_field: str | None
) -> list[dict]:
    """Return attendance since ``since`` without a kiosk work-center tag."""
    if not wc_field:
        logging.getLogger("zira_dashboard.odoo_client").warning(
            "ODOO_KIOSK_WC_FIELD not configured; missing-work-center alert disabled"
        )
        return []
    rows = execute_fn(
        "hr.attendance",
        "search_read",
        [("check_in", ">=", to_odoo_dt(since)), (wc_field, "=", False)],
        fields=["id", "employee_id", "check_in", "check_out"],
        order="check_in desc",
        limit=500,
    )
    out: list[dict] = []
    for row in rows:
        employee = row.get("employee_id")
        out.append(
            {
                "att_id": row["id"],
                "employee_odoo_id": _unwrap_m2o(employee),
                "employee_name": (
                    employee[1]
                    if isinstance(employee, list) and len(employee) > 1
                    else None
                ),
                "check_in": odoo_dt_to_iso(row.get("check_in")),
                "check_out": odoo_dt_to_iso(row.get("check_out")),
            }
        )
    return out


def fetch_open_attendances(
    execute_fn: Callable[..., Any],
    wc_field: str | None,
    department_field: str | None,
) -> list[dict]:
    """Return normalized currently-open attendance rows."""
    del department_field
    fields = ["id", "employee_id", "check_in"]
    if wc_field:
        fields.append(wc_field)
    rows = execute_fn(
        "hr.attendance",
        "search_read",
        [("check_out", "=", False)],
        fields=fields,
    )
    out: list[dict] = []
    for row in rows:
        employee_id = _unwrap_m2o(row.get("employee_id"))
        if not employee_id:
            continue
        out.append(
            {
                "att_id": row["id"],
                "employee_odoo_id": employee_id,
                "check_in": odoo_dt_to_iso(row.get("check_in")),
                "wc_name": (
                    (row.get(wc_field) or None) if wc_field else None
                ),
            }
        )
    return out


def fetch_attendances_for_day(
    execute_fn: Callable[..., Any], day: date
) -> list[dict]:
    """Reduce a local day's attendance to the earliest row per employee."""
    start_local = datetime.combine(day, _time.min, tzinfo=shift_config.SITE_TZ)
    end_local = start_local + timedelta(days=1)
    rows = execute_fn(
        "hr.attendance",
        "search_read",
        [
            ("check_in", ">=", to_odoo_dt(start_local)),
            ("check_in", "<", to_odoo_dt(end_local)),
        ],
        fields=["id", "employee_id", "check_in", "check_out"],
    )
    aggregate: dict[int, dict] = {}
    for row in rows:
        if is_zero_duration_attendance(row):
            continue
        employee_id = _unwrap_m2o(row.get("employee_id"))
        if not employee_id:
            continue
        check_in = odoo_dt_to_iso(row.get("check_in"))
        if check_in is None:
            continue
        is_open = not row.get("check_out")
        current = aggregate.get(employee_id)
        if current is None:
            aggregate[employee_id] = {
                "employee_odoo_id": employee_id,
                "first_check_in": check_in,
                "currently_open": is_open,
            }
        else:
            if check_in < current["first_check_in"]:
                current["first_check_in"] = check_in
            if is_open:
                current["currently_open"] = True
    return list(aggregate.values())


def fetch_employee_attendances_for_day(
    execute_fn: Callable[..., Any], employee_odoo_id: int, day: date
) -> list[dict]:
    start_local = datetime.combine(day, _time.min, tzinfo=shift_config.SITE_TZ)
    end_local = start_local + timedelta(days=1)
    rows = execute_fn(
        "hr.attendance", "search_read",
        [
            ("employee_id", "=", int(employee_odoo_id)),
            ("check_in", ">=", to_odoo_dt(start_local)),
            ("check_in", "<", to_odoo_dt(end_local)),
        ],
        fields=["id", "check_in", "check_out"],
        order="check_in asc, id asc",
    )
    return [
        {
            "id": int(row["id"]),
            "check_in": odoo_dt_to_iso(row.get("check_in")),
            "check_out": odoo_dt_to_iso(row.get("check_out")),
        }
        for row in rows
        if row.get("id") and odoo_dt_to_iso(row.get("check_in"))
    ]


def fetch_attendance_intervals_for_day(
    execute_fn: Callable[..., Any], day: date, wc_field: str | None
) -> list[dict]:
    """Return every meaningful attendance interval for a local day."""
    start_local = datetime.combine(day, _time.min, tzinfo=shift_config.SITE_TZ)
    end_local = start_local + timedelta(days=1)
    fields = ["id", "employee_id", "check_in", "check_out"]
    if wc_field:
        fields.append(wc_field)
    rows = execute_fn(
        "hr.attendance",
        "search_read",
        [
            ("check_in", ">=", to_odoo_dt(start_local)),
            ("check_in", "<", to_odoo_dt(end_local)),
        ],
        fields=fields,
    )
    out: list[dict] = []
    for row in rows:
        if is_zero_duration_attendance(row):
            continue
        employee_id = _unwrap_m2o(row.get("employee_id"))
        if not employee_id:
            continue
        check_in = odoo_dt_to_iso(row.get("check_in"))
        if check_in is None:
            continue
        out.append(
            {
                "employee_odoo_id": employee_id,
                "check_in": check_in,
                "check_out": odoo_dt_to_iso(row.get("check_out")),
                "wc_name": (
                    (row.get(wc_field) or None) if wc_field else None
                ),
            }
        )
    return out
