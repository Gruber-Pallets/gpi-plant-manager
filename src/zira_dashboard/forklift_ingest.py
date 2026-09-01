"""Pure transforms: gpiforklift.com API payloads -> snapshot row dicts.

No I/O. Keys match the forklift_calls_daily / forklift_driver_daily columns.
JSONB hour keys are stored as strings (slot number) for stable round-tripping.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, UTC


@dataclass(frozen=True)
class ForkliftCompletionEvent:
    event_id: str
    driver_id: str
    driver_name: str
    created_at_utc: datetime
    workstation_name: str | None
    on_time: bool | None
    late: bool | None
    response_ms: int | None
    handling_ms: int | None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def completion_events(
    items: list[dict], id_to_name: dict[str, str]
) -> tuple[ForkliftCompletionEvent, ...]:
    events_by_id: dict[str, ForkliftCompletionEvent] = {}
    for item in items or []:
        event_id = item.get("id")
        driver_id = item.get("completedBy")
        created_at = _optional_int(item.get("createdAt"))
        if not event_id or not driver_id or created_at is None:
            continue
        driver_key = str(driver_id)
        event = ForkliftCompletionEvent(
            event_id=str(event_id),
            driver_id=driver_key,
            driver_name=str(id_to_name.get(driver_key) or driver_key),
            created_at_utc=datetime.fromtimestamp(created_at / 1000.0, tz=UTC),
            workstation_name=(
                str(item["workstationName"])
                if item.get("workstationName")
                else None
            ),
            on_time=_optional_bool(item.get("onTime")),
            late=_optional_bool(item.get("late")),
            response_ms=_optional_int(item.get("responseMs")),
            handling_ms=_optional_int(item.get("handlingMs")),
        )
        events_by_id[event.event_id] = event
    return tuple(
        sorted(
            events_by_id.values(),
            key=lambda event: (event.created_at_utc, event.event_id),
        )
    )


def build_calls_daily(day: date, dashboard: dict, history: list[dict]) -> dict:
    completed = [c for c in history if c.get("status") == "completed"]
    by_station = Counter(c.get("workstationName") for c in completed if c.get("workstationName"))
    by_skill = Counter(c.get("requiredSkillId") for c in completed if c.get("requiredSkillId"))
    urgent = sum(1 for c in completed if c.get("priority") == "urgent")

    by_hour: dict[str, dict] = {}
    overload = neglected = 0
    for slot in (dashboard or {}).get("hourlyClaimAvgs", []) or []:
        key = str(slot.get("slot"))
        by_hour[key] = {
            "calls": int(slot.get("calls") or 0),
            "overload": int(slot.get("overloadCount") or 0),
            "neglected": int(slot.get("neglectedCount") or 0),
            "avg_minutes": float(slot.get("avgMinutes") or 0),
        }
        overload += int(slot.get("overloadCount") or 0)
        neglected += int(slot.get("neglectedCount") or 0)

    return {
        "day": day,
        "total_calls": len(completed),
        "urgent_calls": urgent,
        "overload_count": overload,
        "neglected_count": neglected,
        "by_hour": by_hour,
        "by_station": dict(by_station),
        "by_skill": dict(by_skill),
    }


def _local_dt(ms: int, tz) -> datetime:
    """Epoch milliseconds -> aware datetime in the plant timezone."""
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).astimezone(tz)


def aggregate_completions(items: list[dict], id_to_name: dict, tz) -> tuple[list[dict], list[dict]]:
    """Aggregate external-API completion items into the existing snapshot row
    shapes, bucketed by plant-local day (and hour) from each item's createdAt.

    Returns (calls_rows, driver_rows):
      * calls_rows  - one row per plant-local day, matching forklift_calls_daily.
      * driver_rows - one row per (day, completedBy), matching forklift_driver_daily.

    Pure (no I/O). Items missing createdAt or completedBy are skipped. Priority,
    skill, overload, and utilization still aren't on the feed (those stay 0/{}).
    Per-call ``onTime`` / ``late`` booleans are counted when present.
    """
    # Per-day aggregates.
    day_total: dict[date, int] = defaultdict(int)
    day_hour: dict[date, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    day_station: dict[date, Counter] = defaultdict(Counter)
    # Per-(day, driver) aggregates.
    drv_calls: dict[tuple, int] = defaultdict(int)
    drv_ontime: dict[tuple, int] = defaultdict(int)
    drv_late: dict[tuple, int] = defaultdict(int)
    drv_response: dict[tuple, list[int]] = defaultdict(list)
    drv_handling: dict[tuple, int] = defaultdict(int)

    for it in items or []:
        created = it.get("createdAt")
        driver = it.get("completedBy")
        if created is None or driver is None:
            continue
        local = _local_dt(int(created), tz)
        day = local.date()
        hour = local.hour

        day_total[day] += 1
        day_hour[day][hour] += 1
        station = it.get("workstationName")
        if station:
            day_station[day][station] += 1

        key = (day, driver)
        drv_calls[key] += 1
        if it.get("late") is True:
            drv_late[key] += 1
        elif it.get("onTime") is True:
            drv_ontime[key] += 1
        resp = it.get("responseMs")
        if resp is not None:
            drv_response[key].append(int(resp))
        handling = it.get("handlingMs")
        if handling is not None:
            drv_handling[key] += int(handling)

    calls_rows: list[dict] = []
    for day in sorted(day_total):
        by_hour = {
            str(h): {"calls": n, "overload": 0, "neglected": 0, "avg_minutes": 0}
            for h, n in sorted(day_hour[day].items())
        }
        calls_rows.append({
            "day": day,
            "total_calls": day_total[day],
            "urgent_calls": 0,
            "overload_count": 0,
            "neglected_count": 0,
            "by_hour": by_hour,
            "by_station": dict(day_station[day]),
            "by_skill": {},
        })

    driver_rows: list[dict] = []
    for (day, driver) in sorted(drv_calls, key=lambda k: (k[0], str(k[1]))):
        responses = drv_response[(day, driver)]
        avg_ms = round(sum(responses) / len(responses)) if responses else 0
        max_ms = max(responses) if responses else 0
        driver_rows.append({
            "day": day,
            "driver_id": str(driver),
            "name": id_to_name.get(str(driver)) or str(driver),
            "calls": drv_calls[(day, driver)],
            "on_time": drv_ontime[(day, driver)],
            "late": drv_late[(day, driver)],
            "avg_ms": avg_ms,
            "max_ms": max_ms,
            "utilization_pct": 0,
            "on_call_ms": drv_handling[(day, driver)],
            "available_ms": 0,
        })

    return calls_rows, driver_rows


def driver_metrics_from_dashboard(dashboard: dict, id_to_name: dict) -> list[dict]:
    """Extract per-driver on-time/late/utilization rows from a /api/dashboard
    payload. Resolves driver_id by reversing id_to_name on the display name;
    falls back to the name itself when unmapped.

    Only the on-time/utilization fields are returned (the columns
    upsert_driver_metrics fills); calls/avg_ms/max_ms stay owned by the
    completions snapshot."""
    name_to_id = {v: k for k, v in (id_to_name or {}).items()}
    out = []
    for d in (dashboard or {}).get("driverLeaderboard", []) or []:
        name = str(d.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "driver_id": name_to_id.get(name, name),
            "name": name,
            "on_time": int(d.get("onTime") or 0),
            "late": int(d.get("late") or 0),
            "on_call_ms": int(d.get("totalOnCallMs") or 0),
            "available_ms": int(d.get("availableMs") or 0),
            "utilization_pct": float(d.get("utilizationPct") or 0),
        })
    return out


def build_driver_daily(day: date, dashboard: dict) -> list[dict]:
    rows = []
    for d in (dashboard or {}).get("driverLeaderboard", []) or []:
        rows.append({
            "day": day,
            "driver_id": str(d.get("driverId") or d.get("name")),
            "name": d.get("name") or "",
            "calls": int(d.get("total") or 0),
            "on_time": int(d.get("onTime") or 0),
            "late": int(d.get("late") or 0),
            "avg_ms": int(d.get("avgMs") or 0),
            "max_ms": int(d.get("maxMs") or 0),
            "utilization_pct": float(d.get("utilizationPct") or 0),
            "on_call_ms": int(d.get("totalOnCallMs") or 0),
            "available_ms": int(d.get("availableMs") or 0),
        })
    return rows
