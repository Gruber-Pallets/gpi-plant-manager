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

from datetime import date


def attribute_for_day(
    assignments: dict[str, list[str]],
    wc_totals: dict[str, tuple[int, int]],
    elapsed_minutes: int,
    extra_assignments: dict[str, list[str]] | None = None,
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

    Returns:
        {person: {wc_name: {"units": float, "downtime": float, "hours": float,
                            "days_worked": int}}}
    """
    from .staffing import TIME_OFF_KEY  # local import avoids circular at module load

    out: dict[str, dict[str, dict[str, float]]] = {}
    hours = elapsed_minutes / 60.0

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


def _fetch_wc_totals(client, day: date) -> dict[str, tuple[int, int]]:
    """Returns {wc_name: (units, downtime_minutes)} for every metered WC.

    Only consults staffing.LOCATIONS and pulls the WCs that have a meter_id.
    Unmetered WCs return no entry; callers should treat missing entries as
    zero output (which is what attribute_for_day does).
    """
    from . import staffing  # local import — staffing imports leaderboard.Station
    from .leaderboard import cached_leaderboard as leaderboard  # local — leaderboard pulls shift_config/tzdata
    from .stations import Station

    metered = [loc for loc in staffing.LOCATIONS if loc.meter_id]
    if not metered:
        return {}
    stations = [
        Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)
        for loc in metered
    ]
    results = leaderboard(client, stations, day)
    return {r.station.name: (r.units, r.downtime_minutes) for r in results}


def _elapsed_minutes_for(d: date) -> int:
    """Productive minutes available on day d, evaluated as of right now."""
    from datetime import datetime, timezone
    from .shift_config import shift_elapsed_minutes  # local — pulls tzdata
    return shift_elapsed_minutes(d, datetime.now(timezone.utc))


def attribution_for(d: date, client) -> dict[str, dict[str, dict[str, float]]]:
    """Attribute production on a single published day. Returns {} for drafts."""
    from . import staffing, wc_attributions
    sched = staffing.load_schedule(d)
    if not sched.published:
        return {}
    wc_totals = _fetch_wc_totals(client, d)
    elapsed = _elapsed_minutes_for(d)
    extra = wc_attributions.people_by_wc(d)
    return attribute_for_day(
        sched.assignments, wc_totals, elapsed, extra_assignments=extra
    )


def attribution_range(
    start: date,
    end: date,
    client,
) -> dict[str, dict[str, dict[str, float]]]:
    """Sum attribution_for() across [start, end] inclusive.

    Days are fetched concurrently via a thread pool so a multi-day
    range doesn't pay sequential per-day latency. The downstream
    `attribute_for_range` consumes the list in date order, so we
    rely on `pool.map` preserving input order before returning.
    """
    from datetime import timedelta
    from concurrent.futures import ThreadPoolExecutor

    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)

    if not days:
        return attribute_for_range([])

    # Bound parallelism: we don't want to slam Zira on a cold cache.
    # Most calls are cache hits (Postgres) which are cheap; on cold
    # cache the bottleneck is Zira's per-station ThreadPoolExecutor
    # inside leaderboard.py (max_workers=10), so 6 concurrent days
    # gives room without saturating.
    with ThreadPoolExecutor(max_workers=min(6, len(days))) as pool:
        # Map preserves order.
        daily = list(pool.map(lambda d: attribution_for(d, client), days))

    return attribute_for_range(daily)


def daily_records(
    start_d: date, end_d: date, client
) -> list[dict]:
    """Return one record per (day, person, wc) where attributed units > 0.

    Used by the leaderboards top-5 single-day computation.
    Each record:
        {"day": date, "person": str, "wc": str,
         "units": float, "downtime": float, "hours": float}

    Days fetched concurrently via thread pool.
    """
    from datetime import timedelta
    from concurrent.futures import ThreadPoolExecutor

    days: list[date] = []
    cursor = start_d
    while cursor <= end_d:
        days.append(cursor)
        cursor += timedelta(days=1)

    if not days:
        return []

    def _records_for(d):
        out: list[dict] = []
        day_attr = attribution_for(d, client)
        for person, wc_map in day_attr.items():
            for wc_name, totals in wc_map.items():
                if totals["units"] > 0:
                    out.append({
                        "day": d,
                        "person": person,
                        "wc": wc_name,
                        "units": totals["units"],
                        "downtime": totals["downtime"],
                        "hours": totals["hours"],
                    })
        return out

    with ThreadPoolExecutor(max_workers=min(6, len(days))) as pool:
        per_day = list(pool.map(_records_for, days))

    records: list[dict] = []
    for day_records in per_day:
        records.extend(day_records)
    return records


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
