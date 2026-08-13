"""First-workday celebration of the previous month's ribbon podiums."""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from . import awards, production_history, shift_config, work_centers_store


def previous_month(today: date) -> tuple[int, int]:
    if today.month == 1:
        return (today.year - 1, 12)
    return (today.year, today.month - 1)


def is_ribbon_announce_day(today: date) -> bool:
    """True iff `today` is the first plant workday of its calendar month."""
    cursor = date(today.year, today.month, 1)
    while cursor <= today:
        if shift_config.is_workday(cursor):
            return cursor == today
        cursor = cursor + timedelta(days=1)
    return False


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return (date(year, month, 1), date(year, month, last_day))


def ribbon_announce_payload(today: date) -> dict | None:
    if not is_ribbon_announce_day(today):
        return None
    year, month = previous_month(today)
    start, end = _month_bounds(year, month)
    records = production_history.daily_records(start, end)
    groups_out: list[dict] = []
    for group in work_centers_store.registered_groups():
        rows = awards.person_days_in_group(group, start, end, records=records)
        total_units = sum(float(r.get("units") or 0) for r in rows)
        if total_units <= 0:
            continue
        entries = awards.apply_overrides(
            awards.monthly_badges(group, year, month, records=records),
            scope="badge",
            group_name=group,
            year=year,
            month=month,
        )
        if not entries:
            continue
        groups_out.append({"group": group, "entries": entries})
    if not groups_out:
        return None
    return {
        "year": year,
        "month": month,
        "label": f"{calendar.month_name[month]} {year}",
        "groups": groups_out,
    }
