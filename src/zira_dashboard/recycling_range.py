from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class RangeAggregate:
    total_units: int
    total_downtime: int
    total_elapsed: float
    total_available: float
    total_uptime_minutes: float
    total_man_hours: float
    agg_units: dict[str, int]
    agg_downtime: dict[str, int]
    agg_expected: dict[str, float]
    agg_who_today: dict[str, str | None]
    agg_category: dict[str, str]
    agg_station_obj: dict[str, Any]
    agg_active_names: set[str]
    schedule_today_assignments: dict[str, list[str]]


def aggregate_range(
    per_day: list[dict], days: list[date], *, is_range: bool
) -> RangeAggregate:
    total_units = sum(item["total_units"] for item in per_day)
    total_downtime = sum(item["total_downtime"] for item in per_day)
    total_elapsed = sum(item["elapsed"] for item in per_day)
    total_available = sum(item["available"] for item in per_day)
    total_uptime_minutes = sum(item["uptime_minutes"] for item in per_day)
    total_man_hours = sum(item["total_man_hours"] for item in per_day)

    agg_units: dict[str, int] = {}
    agg_downtime: dict[str, int] = {}
    agg_expected: dict[str, float] = {}
    agg_who_today: dict[str, str | None] = {}
    agg_category: dict[str, str] = {}
    agg_station_obj: dict[str, Any] = {}
    agg_active_names: set[str] = set()
    schedule_today_assignments: dict[str, list[str]] = {}

    for item, day in zip(per_day, days, strict=True):
        del day
        agg_active_names |= item["active_wc_names"]
        for name, units in item["per_wc_units"].items():
            agg_units[name] = agg_units.get(name, 0) + units
        for name, downtime in item["per_wc_downtime"].items():
            agg_downtime[name] = agg_downtime.get(name, 0) + downtime
        for name, expected in item["per_wc_expected"].items():
            agg_expected[name] = agg_expected.get(name, 0.0) + expected
        agg_category.update(item["per_wc_category"])
        agg_station_obj.update(item["per_wc_station_obj"])
        if not is_range:
            agg_who_today = item["per_wc_who"]
            schedule_today_assignments = item["schedule_assignments"]

    return RangeAggregate(
        total_units=total_units,
        total_downtime=total_downtime,
        total_elapsed=total_elapsed,
        total_available=total_available,
        total_uptime_minutes=total_uptime_minutes,
        total_man_hours=total_man_hours,
        agg_units=agg_units,
        agg_downtime=agg_downtime,
        agg_expected=agg_expected,
        agg_who_today=agg_who_today,
        agg_category=agg_category,
        agg_station_obj=agg_station_obj,
        agg_active_names=agg_active_names,
        schedule_today_assignments=schedule_today_assignments,
    )
