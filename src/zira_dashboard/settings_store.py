"""User-editable settings in Postgres.

Two concerns stored here:
  - Legacy: per-station + per-group production target overrides
    (dict[str, int]-shaped via the ``_read`` / ``_write`` + ``station_target*`` /
    ``group_target*`` helpers). Storage is pallets-per-day, kept in
    app_settings under two keys:
      - 'station_targets' → {meter_id: pallets_per_day, ...}
      - 'group_targets'   → {category: pallets_per_day, ...}
  - Time-off feature toggles and defaults (arbitrary JSON shapes via the
    newer ``_read_raw`` / ``_write_raw`` + typed ``get_*`` / ``set_*``
    getters at the bottom of this file).

Per-day station targets come primarily from work_centers_store; this
module exists for legacy callers that still reach for category-level
group targets via STATIONS.category buckets.
"""

from __future__ import annotations

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
    """Read a target dict (``{str: int}``) from app_settings, coercing values
    to int and dropping any that won't convert. Missing/non-dict → ``{}``."""
    from . import app_settings
    raw = app_settings.get_setting(key)
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
    from . import app_settings
    app_settings.set_setting(key, {str(k): int(v) for k, v in data.items()})


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

_DEFAULT_SHIFT_HOURS: tuple[float, float] = (6.0, 14.5)


def _read_raw(key: str):
    """Return the stored JSON value for ``key`` (any shape), or None if missing.

    Thin wrapper over ``app_settings.get_setting`` — kept so the typed getters
    below read clearly. Unlike the legacy ``_read`` (which coerces to
    ``dict[str, int]``), this returns whatever shape was stored."""
    from . import app_settings
    return app_settings.get_setting(key)


def _write_raw(key: str, value) -> None:
    """Upsert ``key`` → ``value`` (JSON-encoded). Thin wrapper over
    ``app_settings.set_setting``."""
    from . import app_settings
    app_settings.set_setting(key, value)


# hidden_leave_type_ids -> list[int]
def get_hidden_leave_type_ids() -> list[int]:
    v = _read_raw("time_off.hidden_leave_type_ids")
    if not isinstance(v, list):
        return []
    return [int(x) for x in v if isinstance(x, (int, str)) and str(x).lstrip("-").isdigit()]


def set_hidden_leave_type_ids(ids: list[int]) -> None:
    _write_raw("time_off.hidden_leave_type_ids", [int(x) for x in ids])


# default_shift_hours -> (start, end) tuple of floats
def get_default_shift_hours() -> tuple[float, float]:
    v = _read_raw("time_off.default_shift_hours")
    if not isinstance(v, dict):
        return _DEFAULT_SHIFT_HOURS
    try:
        return (
            float(v.get("start", _DEFAULT_SHIFT_HOURS[0])),
            float(v.get("end", _DEFAULT_SHIFT_HOURS[1])),
        )
    except (TypeError, ValueError):
        return _DEFAULT_SHIFT_HOURS


def set_default_shift_hours(start: float, end: float) -> None:
    _write_raw("time_off.default_shift_hours",
               {"start": float(start), "end": float(end)})
