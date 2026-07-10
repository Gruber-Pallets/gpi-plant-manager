"""Private Odoo calendar operations used by the stable client facade."""

from __future__ import annotations

from typing import Any, Callable


# Odoo "Schedule Type" on resource.calendar. Confirmed against live Odoo.
SCHEDULE_TYPE_FIELD = "flexible_hours"


def _unwrap_m2o(value: Any) -> Any:
    return value[0] if isinstance(value, (list, tuple)) and value else value


def float_to_hhmm(value) -> str:
    """Convert Odoo's float schedule hours to a clamped HH:MM value."""
    total = int(round(float(value) * 60))
    total = max(0, min(total, 23 * 60 + 59))
    return f"{total // 60:02d}:{total % 60:02d}"


def calendar_hours_from_lines(rows) -> dict:
    """Reduce attendance rows to each calendar's outer weekday bounds."""
    acc: dict = {}
    for row in rows:
        if row.get("day_period") == "lunch":
            continue
        calendar_id = _unwrap_m2o(row.get("calendar_id"))
        if not isinstance(calendar_id, int) or isinstance(calendar_id, bool):
            continue
        try:
            weekday = int(row.get("dayofweek"))
        except (TypeError, ValueError):
            continue
        if not (0 <= weekday <= 6):
            continue
        hour_from = float(row.get("hour_from") or 0.0)
        hour_to = float(row.get("hour_to") or 0.0)
        day = acc.setdefault(calendar_id, {}).get(weekday)
        if day is None:
            acc[calendar_id][weekday] = [hour_from, hour_to]
        else:
            day[0] = min(day[0], hour_from)
            day[1] = max(day[1], hour_to)
    out: dict = {}
    for calendar_id, days in acc.items():
        out[calendar_id] = {
            str(weekday): [float_to_hhmm(low), float_to_hhmm(high)]
            for weekday, (low, high) in days.items()
        }
    return out


def calendar_lunch_windows_from_lines(rows) -> dict:
    """Reduce lunch attendance rows to each calendar's weekday windows."""
    out: dict = {}
    for row in rows:
        if row.get("day_period") != "lunch":
            continue
        calendar_id = _unwrap_m2o(row.get("calendar_id"))
        if not isinstance(calendar_id, int) or isinstance(calendar_id, bool):
            continue
        try:
            weekday = int(row.get("dayofweek"))
        except (TypeError, ValueError):
            continue
        if not (0 <= weekday <= 6):
            continue
        hour_from = float(row.get("hour_from") or 0.0)
        hour_to = float(row.get("hour_to") or 0.0)
        if hour_to <= hour_from:
            continue
        out.setdefault(calendar_id, {})[str(weekday)] = [
            float_to_hhmm(hour_from),
            float_to_hhmm(hour_to),
        ]
    return out


def is_flexible(value) -> bool:
    """Interpret a resource.calendar Schedule Type value as a flex flag."""
    if isinstance(value, str):
        return value.strip().lower() == "flexible"
    return bool(value)


def fetch_work_schedules(
    execute_fn: Callable[..., Any], schedule_type_field: str
) -> list[dict]:
    """Return active resource calendars normalized for schedule settings."""
    rows = execute_fn(
        "resource.calendar",
        "search_read",
        [("active", "=", True)],
        fields=["id", "name", schedule_type_field],
    )
    return [
        {
            "id": row["id"],
            "name": row.get("name") or "",
            "is_flexible": is_flexible(row.get(schedule_type_field)),
        }
        for row in rows
    ]


def fetch_calendar_hours(
    execute_fn: Callable[..., Any], calendar_ids
) -> dict:
    """Return per-weekday shift boundaries for resource calendars."""
    if not calendar_ids:
        return {}
    rows = execute_fn(
        "resource.calendar.attendance",
        "search_read",
        [("calendar_id", "in", list(calendar_ids))],
        fields=["calendar_id", "dayofweek", "hour_from", "hour_to", "day_period"],
    )
    return calendar_hours_from_lines(rows)


def fetch_calendar_lunch_windows(
    execute_fn: Callable[..., Any], calendar_ids
) -> dict:
    """Query and reduce per-weekday lunch windows for calendars."""
    rows = execute_fn(
        "resource.calendar.attendance",
        "search_read",
        [("calendar_id", "in", list(calendar_ids))],
        fields=["calendar_id", "dayofweek", "hour_from", "hour_to", "day_period"],
    )
    return calendar_lunch_windows_from_lines(rows)


def fetch_resource_calendar(
    execute_fn: Callable[..., Any],
    unwrap_m2o_fn: Callable[[Any], Any],
    employee_odoo_id: int,
) -> dict | None:
    """Fetch an employee's resource calendar without facade caching."""
    employee_rows = execute_fn(
        "hr.employee",
        "search_read",
        [("id", "=", employee_odoo_id)],
        fields=["id", "resource_calendar_id"],
    )
    if not employee_rows or not employee_rows[0].get("resource_calendar_id"):
        return None
    calendar_field = employee_rows[0]["resource_calendar_id"]
    calendar_id = unwrap_m2o_fn(calendar_field)
    calendar_rows = execute_fn(
        "resource.calendar",
        "read",
        [calendar_id],
        ["id", "tz"],
    )
    timezone = calendar_rows[0]["tz"] if calendar_rows else None

    attendance_rows = execute_fn(
        "resource.calendar.attendance",
        "search_read",
        [("calendar_id", "=", calendar_id)],
        fields=["hour_from", "hour_to", "dayofweek", "day_period"],
    )
    work = [row for row in attendance_rows if row.get("day_period") != "lunch"]
    lunches = [row for row in attendance_rows if row.get("day_period") == "lunch"]
    if not work:
        return None
    hour_from = min(float(row["hour_from"]) for row in work)
    hour_to = max(float(row["hour_to"]) for row in work)
    lunch_from = min(
        (float(row["hour_from"]) for row in lunches), default=None
    )
    lunch_to = max((float(row["hour_to"]) for row in lunches), default=None)
    return {
        "hour_from": hour_from,
        "hour_to": hour_to,
        "lunch_from": lunch_from,
        "lunch_to": lunch_to,
        "tz": timezone,
    }
