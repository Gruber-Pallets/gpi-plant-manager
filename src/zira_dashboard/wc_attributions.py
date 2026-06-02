"""Retro time-windowed WC attribution for production that happened at
unscheduled work centers.

When a metered work center produced units on a given day but had no one
scheduled there, we let the user retroactively attribute the production to
the person who actually worked it. The attribution flows through to
leaderboards and dashboards via ``production_history.attribute_for_day``'s
``extra_assignments`` parameter.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

TESTING_PERSON = "Testing"
"""Sentinel person_name for ``source='testing'`` rows. These rows mark a
window whose production is credited to no one; they are never fed into
crediting as operators."""

TESTING_SOURCE = "testing"
"""``wc_time_attributions.source`` value marking a no-credit testing segment.
Referenced by the write side (routes) and both read-side filters so the string
can't drift out of sync."""


def add(day: date, wc_name: str, person_name: str,
        start_utc: datetime, end_utc: datetime, source: str = "manual") -> int:
    """Insert one attribution row. Returns the new row id."""
    from . import db
    rows = db.query(
        "INSERT INTO wc_time_attributions "
        "(day, wc_name, person_name, start_utc, end_utc, source) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (day, wc_name, person_name, start_utc, end_utc, source),
    )
    return rows[0]["id"] if rows else 0


def for_day(day: date) -> list[dict]:
    """All attributions for a day. Returns list of dicts with keys
    id, wc_name, person_name, start_utc, end_utc, source."""
    from . import db
    return db.query(
        "SELECT id, wc_name, person_name, start_utc, end_utc, source "
        "FROM wc_time_attributions WHERE day = %s ORDER BY wc_name, start_utc",
        (day,),
    )


def people_by_wc(day: date) -> dict[str, list[str]]:
    """Aggregated view: ``{wc_name: [person, ...]}`` -- convenience for joining
    into ``attribute_for_day``'s assignments dict. Excludes ``source='testing'``
    rows so a testing window never becomes a credited operator.

    Swallows DB errors (e.g. Postgres unreachable) so callers in hot paths
    like leaderboards keep working.
    """
    try:
        rows = for_day(day)
    except Exception:
        return {}
    out: dict[str, list[str]] = {}
    for r in rows:
        if r.get("source") == TESTING_SOURCE:
            continue
        out.setdefault(r["wc_name"], []).append(r["person_name"])
    return out


def testing_windows_for_day(day: date) -> dict[str, list[tuple]]:
    """``{wc_name: [(start_utc, end_utc), ...]}`` for ``source='testing'``
    rows. Swallows DB errors like ``people_by_wc``."""
    try:
        rows = for_day(day)
    except Exception:
        return {}
    out: dict[str, list[tuple]] = {}
    for r in rows:
        if r.get("source") != TESTING_SOURCE:
            continue
        out.setdefault(r["wc_name"], []).append((r["start_utc"], r["end_utc"]))
    return out


def delete(attribution_id: int) -> None:
    from . import db
    db.execute("DELETE FROM wc_time_attributions WHERE id = %s", (attribution_id,))


UNATTRIBUTED_MIN_UNITS = 5
"""WCs with units at or below this threshold are skipped — could be a stray
sample / fluke and shouldn't surface as work to attribute. Matches the
dashboards' existing ACTIVE_UNITS_THRESHOLD."""


def unattributed_for_day(day: date, client) -> list[dict]:
    """Walk metered WCs for ``day``. Return rows for WCs that:
      1. Produced more than UNATTRIBUTED_MIN_UNITS (filters flukes)
      2. Are NOT in the schedule's assignments
      3. Are NOT in the attributions table

    Each result dict: ``{wc_name, units, first_sample_utc, last_sample_utc}``.
    """
    from . import staffing
    from . import leaderboard as _lb
    from .stations import STATIONS

    sched = staffing.load_schedule(day)
    scheduled_wcs = {
        wc for wc, ops in sched.assignments.items()
        if ops and wc != staffing.TIME_OFF_KEY
    }
    attributed_wcs = (
        set(people_by_wc(day).keys())
        | set(testing_windows_for_day(day).keys())
    )

    # STATIONS uses short names (e.g., "Trim Saw", "Junior 2") while
    # LOCATIONS / schedules use the WC display name (e.g., "Trim Saw 1",
    # "Junior #2"). Map by meter_id so a schedule on "Trim Saw 1" is
    # recognized for the station with the matching meter.
    meter_to_loc_name = {loc.meter_id: loc.name for loc in staffing.LOCATIONS if loc.meter_id}

    # All metered work centers, regardless of cell. Production at any metered
    # WC without a schedule entry deserves to surface as a todo (Junior 2,
    # Trim Saw, etc., not just Recycling-cell stations).
    stations = [s for s in STATIONS if s.meter_id]
    # Don't pass now_utc for past days; for today use now.
    today = datetime.now(timezone.utc).date()
    now_arg = datetime.now(timezone.utc) if day == today else None
    results = _lb.cached_leaderboard(client, stations, day, now_utc=now_arg)

    out: list[dict] = []
    for r in results:
        if r.units <= UNATTRIBUTED_MIN_UNITS:
            continue
        # Use the LOCATION display name when available (matches the schedule).
        wc = meter_to_loc_name.get(r.station.meter_id, r.station.name)
        if wc in scheduled_wcs or wc in attributed_wcs:
            continue
        # Pull first/last sample times from active_intervals for time bounds.
        ais = r.active_intervals
        if not ais:
            continue
        first_utc = min(s for s, _ in ais)
        last_utc = max(e for _, e in ais)
        out.append({
            "wc_name": wc,
            "units": int(r.units),
            "first_sample_utc": first_utc,
            "last_sample_utc": last_utc,
        })
    return out
