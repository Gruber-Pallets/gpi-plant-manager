"""Per-day, per-person production attribution.

Joins published schedules (who worked where) with Zira leaderboard output
(what each WC produced) into a {person → {wc → totals}} structure used by
the VS dashboard, Player Cards, and Leaderboards features. Units and
downtime at multi-person WCs are split equally across all assigned
operators.

The pure core (`attribute_for_day`, `attribute_for_range`) takes pre-fetched
data and is fully testable. The wrappers (`attribution_for`,
`attribution_range`) call Zira and load schedules.
"""

from __future__ import annotations

from datetime import date, UTC


def attribute_for_day(
    assignments: dict[str, list[str]],
    wc_totals: dict[str, tuple[int, int]],
    elapsed_minutes: int,
    extra_assignments: dict[str, list[str]] | None = None,
    excluded_minutes: dict[str, dict[str, float]] | None = None,
) -> dict[str, dict[str, dict[str, float]]]:
    """Attribute one day's WC output to the operators on each WC.

    Args:
        assignments: {wc_name: [person_name, ...]} — from the schedule's
            assignments dict, with the time-off pseudo-key already stripped.
        wc_totals: {wc_name: (units, downtime_minutes)} — from a Zira
            leaderboard call. Missing entries (WC with no meter) are
            treated as zero output.
        elapsed_minutes: shift minutes available that day; same for everyone.
        extra_assignments: optional ``{wc_name: [person, ...]}`` for retro
            time-window attributions. Adds operators to UNSCHEDULED WCs only
            (a WC already present in ``assignments`` with people is left
            alone -- the published schedule wins). Used to flow retro
            attributions into leaderboards and dashboards.
        excluded_minutes: optional ``{person: {wc_name: minutes}}`` of
            machine-breakdown-excluded minutes (the mirror of testing's unit
            offset -- this zeroes EXPECTED minutes, not units). Missing
            entries default to 0.0.

    Returns:
        {person: {wc_name: {"units": float, "downtime": float, "hours": float,
                            "days_worked": int, "excluded_minutes": float}}}
    """
    from .staffing import TIME_OFF_KEY  # local import avoids circular at module load

    out: dict[str, dict[str, dict[str, float]]] = {}
    hours = elapsed_minutes / 60.0
    excluded_minutes = excluded_minutes or {}

    # Merge: scheduled wins; extras only fire when a WC has no scheduled people.
    merged: dict[str, list[str]] = {}
    for wc_name, operators in assignments.items():
        if wc_name == TIME_OFF_KEY or not operators:
            continue
        merged[wc_name] = list(operators)
    if extra_assignments:
        for wc_name, ppl in extra_assignments.items():
            if wc_name in merged:  # scheduled — skip
                continue
            if not ppl:
                continue
            merged[wc_name] = list(ppl)

    for wc_name, operators in merged.items():
        units, downtime = wc_totals.get(wc_name, (0, 0))
        n = len(operators)
        per_units = units / n
        per_downtime = downtime / n
        for person in operators:
            wc_map = out.setdefault(person, {})
            wc_map[wc_name] = {
                "units": per_units,
                "downtime": per_downtime,
                "hours": hours,
                "days_worked": 1,
                "excluded_minutes": excluded_minutes.get(person, {}).get(wc_name, 0.0),
            }
    return out


def attribute_for_range(
    daily_attributions: list[dict[str, dict[str, dict[str, float]]]],
) -> dict[str, dict[str, dict[str, float]]]:
    """Sum a list of per-day attribution dicts (output of attribute_for_day).

    Adds the four numeric fields per (person, wc); days_worked counts the
    number of input days that contained that (person, wc) pair.
    """
    out: dict[str, dict[str, dict[str, float]]] = {}
    for daily in daily_attributions:
        for person, wc_map in daily.items():
            person_out = out.setdefault(person, {})
            for wc_name, totals in wc_map.items():
                acc = person_out.setdefault(
                    wc_name,
                    {"units": 0.0, "downtime": 0.0, "hours": 0.0, "days_worked": 0},
                )
                acc["units"] += totals["units"]
                acc["downtime"] += totals["downtime"]
                acc["hours"] += totals["hours"]
                acc["days_worked"] += totals["days_worked"]
    return out


def _metered_leaderboard(client, day: date):
    """cached_leaderboard results for all metered WCs, or [] if none.
    Shared by _fetch_wc_totals and _fetch_wc_samples so the station-building
    block can't drift between them."""
    from . import staffing  # local import — staffing imports leaderboard.Station
    from .leaderboard import cached_leaderboard as leaderboard  # local — leaderboard pulls shift_config/tzdata
    from .stations import Station

    metered = [loc for loc in staffing.LOCATIONS if loc.meter_id]
    if not metered:
        return []
    stations = [
        Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)
        for loc in metered
    ]
    return leaderboard(client, stations, day)


def _fetch_wc_totals(client, day: date) -> dict[str, tuple[int, int]]:
    """Returns {wc_name: (units, downtime_minutes)} for every metered WC.

    Only consults staffing.LOCATIONS and pulls the WCs that have a meter_id.
    Unmetered WCs return no entry; callers should treat missing entries as
    zero output (which is what attribute_for_day does).
    """
    return {r.station.name: (r.units, r.downtime_minutes)
            for r in _metered_leaderboard(client, day)}


def _fetch_wc_samples(client, day: date) -> dict[str, list[tuple]]:
    """``{wc_name: [(event_dt_utc, units), ...]}`` for metered WCs on ``day``.
    Reuses the cached leaderboard (same call _fetch_wc_totals makes), so this
    is cheap when both run for the same day."""
    return {r.station.name: list(r.samples)
            for r in _metered_leaderboard(client, day)}


def _apply_testing_offsets(
    wc_totals: dict[str, tuple[int, int]],
    samples_by_wc: dict[str, list[tuple]],
    testing_windows: dict[str, list[tuple]],
) -> dict[str, tuple[int, int]]:
    """Subtract units produced inside testing windows from each WC's total so
    they're credited to no one. Downtime is left untouched. Floors at 0."""
    if not testing_windows:
        return wc_totals
    out = dict(wc_totals)
    for wc, windows in testing_windows.items():
        if wc not in out:
            continue
        samples = samples_by_wc.get(wc, [])
        testing_units = sum(
            u for (t, u) in samples
            if any(s <= t < e for (s, e) in windows)
        )
        units, downtime = out[wc]
        out[wc] = (max(0, units - testing_units), downtime)
    return out


def _elapsed_minutes_for(d: date) -> int:
    """Productive minutes available on day d, evaluated as of right now."""
    from datetime import datetime
    from .shift_config import shift_elapsed_minutes  # local — pulls tzdata
    return shift_elapsed_minutes(d, datetime.now(UTC))


def _effective_now(day: date, now: datetime) -> datetime:
    """`now`, clamped to `day`'s shift end. Used to cap an OPEN breakdown
    exclusion window for a past day (or a today read taken after hours) so
    excluded-minutes math never runs past the shift that actually happened."""
    from datetime import datetime, UTC
    from .shift_config import shift_end_for, SITE_TZ
    shift_end_utc = datetime.combine(day, shift_end_for(day), tzinfo=SITE_TZ).astimezone(UTC)
    return min(now, shift_end_utc)


def _excluded_minutes_by_person_wc(day: date, now: datetime) -> dict[str, dict[str, float]]:
    """{person: {wc_name: minutes}} of machine-breakdown-excluded minutes for
    `day`. Open breakdown windows are capped at `now` (already clamped to
    shift end by the caller) so a live in-progress breakdown is reflected
    immediately, matching the design's "today's live averages are correct
    during the outage" requirement."""
    from . import wc_attributions, machine_breakdown
    from .shift_config import productive_minutes_in_window
    windows_by_key = wc_attributions.breakdown_windows_for_day(day)
    out: dict[str, dict[str, float]] = {}
    for (person, wc), windows in windows_by_key.items():
        closed = [(s, e if e is not None else now) for (s, e) in windows]
        minutes = machine_breakdown.excluded_minutes_for_windows(
            closed, day, productive_minutes_in_window
        )
        if minutes > 0:
            out.setdefault(person, {})[wc] = minutes
    return out


def attribution_for(d: date, client) -> dict[str, dict[str, dict[str, float]]]:
    """Attribute production on a single day.

    For past days, uses whatever schedule_assignments are saved — even
    if the schedule was never formally published — because by the time
    a day is in the past, the saved draft is the closest available
    record of what actually happened. Today and future days still gate
    on `published` so an in-flight draft (e.g., supervisor mid-edit)
    doesn't pollute leaderboards / player cards / popups.

    Days with no saved assignments at all naturally produce {} via
    `attribute_for_day` (empty merged dict).
    """
    from datetime import datetime
    from . import staffing, wc_attributions
    sched = staffing.load_schedule(d)
    today = datetime.now(UTC).date()
    if d >= today and not sched.published:
        return {}
    wc_totals = _fetch_wc_totals(client, d)
    elapsed = _elapsed_minutes_for(d)
    extra = wc_attributions.people_by_wc(d)
    testing = wc_attributions.testing_windows_for_day(d)
    if testing:
        samples_by_wc = _fetch_wc_samples(client, d)
        wc_totals = _apply_testing_offsets(wc_totals, samples_by_wc, testing)
    excluded = _excluded_minutes_by_person_wc(d, _effective_now(d, datetime.now(UTC)))
    return attribute_for_day(
        sched.assignments, wc_totals, elapsed,
        extra_assignments=extra, excluded_minutes=excluded,
    )


def attribution_per_day(
    start: date,
    end: date,
) -> list[tuple[date, dict[str, dict[str, dict[str, float]]]]]:
    """Per-day attribution across [start, end] inclusive.

    Returns one (day, attribution_dict) tuple per day in the range,
    in date-ascending order. Empty days return ({}). Reads from
    production_daily.

    Production on a (person, day) the person was manager-declared Absent
    (a `manual_absences` row) is excluded — a stray meter unit crediting
    someone who was out shouldn't count as a worked day. Undo the absence
    to restore the day's production.
    """
    from datetime import timedelta
    from . import db

    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    if not days:
        return []

    rows = db.query(
        """
        SELECT day, name, wc_name,
               units, downtime, hours, days_worked
        FROM production_daily
        WHERE day BETWEEN %s AND %s
          AND NOT EXISTS (
            SELECT 1 FROM manual_absences ma
            WHERE ma.day = production_daily.day
              AND ma.name = production_daily.name
          )
        """,
        (start, end),
    )
    by_day: dict[date, dict[str, dict[str, dict[str, float]]]] = {d: {} for d in days}
    for r in rows:
        person_map = by_day[r["day"]].setdefault(r["name"], {})
        person_map[r["wc_name"]] = {
            "units":       float(r["units"]),
            "downtime":    float(r["downtime"]),
            "hours":       float(r["hours"]),
            "days_worked": float(r["days_worked"]),
        }
    return [(d, by_day[d]) for d in days]


def attribution_range(
    start: date,
    end: date,
) -> dict[str, dict[str, dict[str, float]]]:
    """Sum attribution across [start, end] inclusive.

    Reads from production_daily and reshapes into the legacy
    {person: {wc: {units, downtime, hours, days_worked}}} envelope so
    callers (player cards, leaderboards via rank_by_category) don't
    have to change.

    Excludes (person, day) pairs the person was manager-declared Absent
    (see attribution_per_day), so declared-absent days don't inflate
    days-worked or produce phantom low-output rows.
    """
    from . import db
    rows = db.query(
        """
        SELECT name,
               wc_name,
               SUM(units)       AS units,
               SUM(downtime)    AS downtime,
               SUM(hours)       AS hours,
               SUM(days_worked) AS days_worked
        FROM production_daily
        WHERE day BETWEEN %s AND %s
          AND NOT EXISTS (
            SELECT 1 FROM manual_absences ma
            WHERE ma.day = production_daily.day
              AND ma.name = production_daily.name
          )
        GROUP BY name, wc_name
        """,
        (start, end),
    )
    out: dict[str, dict[str, dict[str, float]]] = {}
    for r in rows:
        out.setdefault(r["name"], {})[r["wc_name"]] = {
            "units":       float(r["units"]),
            "downtime":    float(r["downtime"]),
            "hours":       float(r["hours"]),
            "days_worked": float(r["days_worked"]),
        }
    return out


def daily_records(start_d: date, end_d: date) -> list[dict]:
    """Return one record per (day, person, wc) where attributed units > 0.

    Reads from production_daily — the canonical source for historical
    per-(day, person, wc) production.
    """
    from . import precompute
    return precompute.daily_records_in_range(start_d, end_d)


def rank_by_category(
    range_attribution: dict[str, dict[str, dict[str, float]]],
    category_wcs: list[str],
    expected_units_per_day_by_wc: dict[str, int],
    min_days: int = 3,
) -> list[dict]:
    """Build a leaderboard for one WC category.

    Each row has: name, units (sum within the category), downtime,
    days_worked (sum of day-credits across category WCs),
    pct_of_target (sum_units / sum_expected * 100, or None if expected is 0).
    Rows are sorted by pct_of_target desc, ties broken by units desc.
    Rows below min_days are filtered out before ranking.
    """
    cat_set = set(category_wcs)
    rows: list[dict] = []
    for person, wc_map in range_attribution.items():
        units = 0.0
        downtime = 0.0
        days = 0
        expected = 0.0
        for wc_name, totals in wc_map.items():
            if wc_name not in cat_set:
                continue
            units += totals["units"]
            downtime += totals["downtime"]
            days += totals["days_worked"]
            per_day = expected_units_per_day_by_wc.get(wc_name, 0)
            expected += per_day * totals["days_worked"]
        if days < min_days:
            continue
        pct = (units / expected * 100.0) if expected > 0 else None
        rows.append({
            "name": person,
            "units": round(units, 1),
            "downtime": round(downtime, 1),
            "days_worked": days,
            "pct_of_target": round(pct, 1) if pct is not None else None,
            "expected": round(expected, 1),
        })
    rows.sort(key=lambda r: (-(r["pct_of_target"] or -1), -r["units"]))
    return rows
