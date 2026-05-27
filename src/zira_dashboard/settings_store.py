"""User-editable per-station + per-group target overrides, in Postgres.

Storage is pallets-per-day, kept in app_settings under two keys:
  - 'station_targets' → {meter_id: pallets_per_day, ...}
  - 'group_targets'   → {category: pallets_per_day, ...}

Per-day station targets come primarily from work_centers_store; this
module exists for legacy callers that still reach for category-level
group targets via STATIONS.category buckets.
"""

from __future__ import annotations

import json

from .shift_config import TARGET_PER_DAY, productive_minutes_per_day
from .stations import STATIONS, Station


def _wc_store():
    from . import work_centers_store
    return work_centers_store


def _loc_for_station(station: Station):
    from .staffing import LOCATIONS
    for loc in LOCATIONS:
        if loc.meter_id == station.meter_id:
            return loc
    return None


def _productive_hours() -> float:
    m = productive_minutes_per_day()
    return (m / 60.0) if m else 0.0


def _read(key: str) -> dict[str, int]:
    from . import db
    rows = db.query("SELECT value FROM app_settings WHERE key = %s", (key,))
    if not rows:
        return {}
    raw = rows[0]["value"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _write(key: str, data: dict[str, int]) -> None:
    from . import db
    db.execute(
        "INSERT INTO app_settings (key, value, updated_at) "
        "VALUES (%s, %s::jsonb, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, json.dumps({str(k): int(v) for k, v in data.items()})),
    )


def save(station_targets: dict[str, int], group_targets: dict[str, int]) -> None:
    _write("station_targets", station_targets)
    _write("group_targets", group_targets)


def station_target_per_day(station: Station) -> int:
    loc = _loc_for_station(station)
    if loc is not None:
        return _wc_store().goal_per_day(loc)
    return int(TARGET_PER_DAY.get(station.category, 0))


def group_target_per_day(category: str) -> int:
    overrides = _read("group_targets")
    override = overrides.get(category)
    if override is not None:
        return int(override)
    members = [s for s in STATIONS if s.category == category]
    if not members:
        return 0
    return sum(station_target_per_day(s) for s in members)


def station_target(station: Station) -> float:
    hrs = _productive_hours()
    return (station_target_per_day(station) / hrs) if hrs else 0.0


def group_target(category: str) -> float:
    hrs = _productive_hours()
    return (group_target_per_day(category) / hrs) if hrs else 0.0


def snapshot() -> dict:
    return {
        "station_targets": _read("station_targets"),
        "group_targets": _read("group_targets"),
    }


# ---- Time-off settings (2026-05-27) ----

import json as _json


def _read_raw(key: str):
    """Return the raw value from app_settings, or None if missing.

    Unlike the legacy ``_read`` above (which coerces values to ``dict[str, int]``),
    this returns whatever JSON shape was stored — scalar, list, or dict — for
    callers that need arbitrary payloads (e.g. time-off settings).
    """
    from . import db
    rows = db.query("SELECT value FROM app_settings WHERE key = %s", (key,))
    if not rows:
        return None
    raw = rows[0]["value"]
    if isinstance(raw, str):
        try:
            return _json.loads(raw)
        except _json.JSONDecodeError:
            return None
    return raw


def _write_raw(key: str, value) -> None:
    """Upsert key -> value (JSON-encoded) into app_settings.

    Matches the ``::jsonb`` + ``updated_at = now()`` convention used by the
    legacy ``_write`` above and by ``odoo_sync`` / migration scripts.
    """
    from . import db
    db.execute(
        "INSERT INTO app_settings (key, value, updated_at) "
        "VALUES (%s, %s::jsonb, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, _json.dumps(value)),
    )


# hidden_leave_type_ids -> list[int]
def get_hidden_leave_type_ids() -> list[int]:
    v = _read_raw("time_off.hidden_leave_type_ids")
    if not isinstance(v, list):
        return []
    return [int(x) for x in v if isinstance(x, (int, str)) and str(x).lstrip("-").isdigit()]


def set_hidden_leave_type_ids(ids: list[int]) -> None:
    _write_raw("time_off.hidden_leave_type_ids", [int(x) for x in ids])


# show_stratustime_overlay -> bool (default True)
def get_show_stratustime_overlay() -> bool:
    v = _read_raw("time_off.show_stratustime_overlay")
    if v is None:
        return True
    return bool(v)


def set_show_stratustime_overlay(on: bool) -> None:
    _write_raw("time_off.show_stratustime_overlay", bool(on))


# default_shift_hours -> (start, end) tuple of floats
def get_default_shift_hours() -> tuple[float, float]:
    v = _read_raw("time_off.default_shift_hours")
    if not isinstance(v, dict):
        return (6.0, 14.5)
    try:
        return (float(v.get("start", 6.0)), float(v.get("end", 14.5)))
    except (TypeError, ValueError):
        return (6.0, 14.5)


def set_default_shift_hours(start: float, end: float) -> None:
    _write_raw("time_off.default_shift_hours",
               {"start": float(start), "end": float(end)})
