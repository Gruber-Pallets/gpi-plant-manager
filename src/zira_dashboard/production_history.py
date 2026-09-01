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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, UTC
import hashlib
import json
import math
from numbers import Real
from typing import TypeAlias

from .production_segments import (
    PersonAttributionKey,
    UnassignedRun,
    attribution_key,
    credit_work_segments,
    unassigned_runs_for_samples,
)


class ProductionSourceUnavailable(RuntimeError):
    """Raised when a precompute cannot safely replace saved production."""


@dataclass(frozen=True)
class StrictSourceSnapshot:
    day: date
    shift_start_utc: datetime
    shift_end_utc: datetime
    break_windows: tuple[tuple[datetime, datetime], ...]
    shift_by_day: dict
    stations: tuple
    work_center_by_odoo_id: dict[int, str]
    source_fingerprint: str


_STRICT_LOCAL_SOURCE_SQL = """
SELECT md5(concat_ws('|',
  (SELECT COALESCE(jsonb_agg(jsonb_build_array(
       odoo_attendance_id, employee_odoo_id, employee_name,
       check_in_utc, check_out_utc, odoo_work_center_id,
       odoo_work_center_name, odoo_department_id, odoo_department_name,
       odoo_write_date
     ) ORDER BY odoo_attendance_id)::text, '[]')
     FROM odoo_attendance_mirror
    WHERE deleted_at IS NULL
      AND (check_out_utc IS NULL OR check_out_utc > check_in_utc)
      AND check_in_utc < %s
      AND (check_out_utc IS NULL OR check_out_utc > %s)),
  (SELECT COALESCE(jsonb_agg(jsonb_build_array(
       singleton,
       CASE WHEN last_incremental_completed_at IS NULL THEN NULL
            ELSE LEAST(last_incremental_completed_at, %s) END,
       baseline_completed_at IS NOT NULL
     ) ORDER BY singleton)::text, '[]')
     FROM odoo_attendance_sync_state),
  (SELECT COALESCE(jsonb_agg(jsonb_build_array(
       id, name, meter_id, category, cell, odoo_work_center_id,
       odoo_work_center_name, department
     ) ORDER BY id)::text, '[]') FROM work_centers),
  (SELECT COALESCE(jsonb_agg(jsonb_build_array(name, requires_work_center)
     ORDER BY name)::text, '[]') FROM departments),
  (SELECT COALESCE(jsonb_agg(jsonb_build_array(
       id, odoo_id, department_name, wage_type, active)
     ORDER BY id)::text, '[]') FROM people
    WHERE odoo_id IN (
      SELECT employee_odoo_id FROM odoo_attendance_mirror
       WHERE deleted_at IS NULL
         AND (check_out_utc IS NULL OR check_out_utc > check_in_utc)
         AND check_in_utc < %s
         AND (check_out_utc IS NULL OR check_out_utc > %s)
    )),
  (SELECT COALESCE(jsonb_agg(jsonb_build_array(
       id, day, wc_name, employee_odoo_id, person_name,
       start_utc, end_utc, source, breakdown_id
     ) ORDER BY id)::text, '[]')
     FROM wc_time_attributions WHERE day = %s),
  (SELECT COALESCE(jsonb_agg(jsonb_build_array(
       id, day, wc_name, detected_stop_utc, source,
       resolved_at, resolution, resume_utc
     ) ORDER BY id)::text, '[]')
     FROM machine_breakdowns WHERE day = %s)
)) AS source_fingerprint
"""


def lock_strict_sources_cur(cur) -> None:
    cur.execute(
        "LOCK TABLE odoo_attendance_mirror, odoo_attendance_sync_state, "
        "work_centers, departments, people, schedules, global_schedule, "
        "saturday_schedule, company_holidays, saturday_recruitments, "
        "wc_time_attributions, machine_breakdowns IN SHARE MODE"
    )


def strict_local_source_fingerprint(
    day: date,
    *,
    cur=None,
    source_snapshot: StrictSourceSnapshot | None = None,
) -> str:
    from . import db, shift_config

    source = source_snapshot or strict_source_snapshot(day, cur=cur)
    local_midnight = datetime.combine(
        day, datetime.min.time(), tzinfo=shift_config.SITE_TZ,
    ).astimezone(UTC)
    params = (
        source.shift_end_utc,
        local_midnight,
        source.shift_end_utc,
        source.shift_end_utc,
        local_midnight,
        day,
        day,
    )

    if cur is None:
        rows = db.query(_STRICT_LOCAL_SOURCE_SQL, params)
        row = rows[0] if rows else None
    else:
        cur.execute(_STRICT_LOCAL_SOURCE_SQL, params)
        row = cur.fetchone()
    if not row or not row.get("source_fingerprint"):
        raise ProductionSourceUnavailable("strict local source fingerprint unavailable")
    exact = f"{source.source_fingerprint}:{row['source_fingerprint']}"
    return hashlib.sha256(exact.encode("utf-8")).hexdigest()


def strict_source_snapshot(day: date, *, cur=None) -> StrictSourceSnapshot:
    """Capture exact shift and station inputs from the current DB snapshot."""
    from . import db, shift_config
    from .stations import Station

    shift_by_day = {}
    snapshots = {}
    for resolved_day in (day - timedelta(days=1), day, day + timedelta(days=1)):
        snapshot = shift_config.snapshot_for(resolved_day, cur=cur)
        snapshots[resolved_day] = snapshot
        shift_by_day[resolved_day] = (
            snapshot.is_workday,
            snapshot.shift_start,
            snapshot.shift_end,
            snapshot.breaks,
        )
    sql = (
        "SELECT name, meter_id, category, cell, odoo_work_center_id "
        "FROM work_centers ORDER BY name"
    )
    if cur is None:
        rows = db.query(sql)
    else:
        cur.execute(sql)
        rows = list(cur.fetchall())
    stations = tuple(
        Station(
            meter_id=str(row["meter_id"]),
            name=str(row["name"]),
            category=str(row.get("category") or "Other"),
            cell=str(row.get("cell") or ""),
        )
        for row in rows
        if row.get("meter_id") is not None and str(row["meter_id"]) != ""
    )
    work_center_by_odoo_id = {
        int(row["odoo_work_center_id"]): str(row["name"])
        for row in rows
        if row.get("odoo_work_center_id") is not None
    }
    target = snapshots[day]
    start = datetime.combine(
        day, target.shift_start, tzinfo=shift_config.SITE_TZ
    ).astimezone(UTC)
    end = datetime.combine(
        day, target.shift_end, tzinfo=shift_config.SITE_TZ
    ).astimezone(UTC)
    if end <= start:
        raise ProductionSourceUnavailable(
            f"strict production has an invalid shift window for {day.isoformat()}"
        )
    breaks = tuple(
        (
            datetime.combine(
                day, item.start, tzinfo=shift_config.SITE_TZ
            ).astimezone(UTC),
            datetime.combine(
                day, item.end, tzinfo=shift_config.SITE_TZ
            ).astimezone(UTC),
        )
        for item in target.breaks
        if item.end > item.start
    )
    payload = {
        "version": 1,
        "days": [
            [
                resolved_day.isoformat(),
                snapshot.is_workday,
                snapshot.shift_start.isoformat(),
                snapshot.shift_end.isoformat(),
                [
                    [item.start.isoformat(), item.end.isoformat(), item.name]
                    for item in snapshot.breaks
                ],
            ]
            for resolved_day, snapshot in sorted(snapshots.items())
        ],
        "stations": [
            [item.meter_id, item.name, item.category, item.cell] for item in stations
        ],
        "odoo_work_centers": [
            [odoo_id, name]
            for odoo_id, name in sorted(work_center_by_odoo_id.items())
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return StrictSourceSnapshot(
        day=day,
        shift_start_utc=start,
        shift_end_utc=end,
        break_windows=breaks,
        shift_by_day=shift_by_day,
        stations=stations,
        work_center_by_odoo_id=work_center_by_odoo_id,
        source_fingerprint=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


Attribution: TypeAlias = dict[PersonAttributionKey, dict[str, dict[str, float]]]
SAMPLE_TOTAL_TOLERANCE = 1e-6


class AttributionResult(dict):
    """Backwards-compatible mapping carrying the already-resolved matcher."""

    def __init__(
        self,
        values: Mapping | None = None,
        *,
        is_strict: bool,
        source_fingerprint: str | None = None,
        request_fingerprint: str | None = None,
    ):
        super().__init__(values or {})
        self.is_strict = bool(is_strict)
        self.source_fingerprint = source_fingerprint
        self.request_fingerprint = request_fingerprint


@dataclass(frozen=True)
class _StrictDayInputs:
    segments: tuple
    wc_totals: dict[str, tuple[float, float]]
    samples_by_wc: dict[str, list[tuple[datetime, float]]]
    active_intervals_by_wc: dict[str, tuple[tuple[datetime, datetime], ...]]
    excluded_minutes: dict[PersonAttributionKey, dict[str, float]]
    break_windows: tuple[tuple[datetime, datetime], ...]
    testing_windows: dict[str, list[tuple[datetime, datetime]]]
    breakdown_windows: dict[tuple, list[tuple[datetime, datetime | None]]]
    location_spans: tuple = ()
    shift_start_utc: datetime | None = None
    shift_end_utc: datetime | None = None
    source_fingerprint: str | None = None
    request_fingerprint: str | None = None


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


def attribute_for_segments(
    segments,
    *,
    wc_totals: dict[str, tuple[int, int]],
    samples_by_wc: dict[str, list[tuple]],
    productive_minutes: Callable[[str, str, datetime, datetime], float],
    excluded_minutes: Mapping[PersonAttributionKey, Mapping[str, float]] | None = None,
    strict: bool = False,
) -> Attribution:
    """Credit output to the people at each WC when the unit was recorded.

    ``segments`` is the Odoo-attendance-backed output of
    :func:`assignment_windows.resolve_segments`.  Every positive Zira sample
    is split only among the people whose segment contains the sample's
    timestamp.  This lets a tablet work-center sign-in move both credit and
    worked hours immediately, while the schedule remains the fallback for
    people with no Odoo work-center attendance.

    Downtime has no per-event timestamp, so it is divided by each person's
    productive minutes at that work center.  Any total without samples uses
    the same worked-time ratio as a safe compatibility fallback.
    """
    excluded_minutes = excluded_minutes or {}
    credits = credit_work_segments(
        segments,
        wc_totals={wc_name: totals[0] for wc_name, totals in wc_totals.items()},
        samples_by_wc=samples_by_wc,
        productive_minutes=productive_minutes,
        allow_total_fallback=not strict,
    )
    if strict:
        for wc_name in set(wc_totals) | set(samples_by_wc) | set(credits):
            credited = sum(credit.actual_units for credit in credits.get(wc_name, ()))
            sampled = sum(float(units) for _timestamp, units in samples_by_wc.get(wc_name, ()))
            expected = float(wc_totals.get(wc_name, (0, 0))[0] or 0)
            if (
                abs(credited - sampled) > SAMPLE_TOTAL_TOLERANCE
                or abs(credited - expected) > SAMPLE_TOTAL_TOLERANCE
            ):
                raise ProductionSourceUnavailable(
                    f"strict sample credit did not conserve {wc_name} production"
                )
    out: Attribution = {}

    def entry(person: PersonAttributionKey, wc_name: str) -> dict[str, float]:
        return out.setdefault(person, {}).setdefault(
            wc_name,
            {
                "units": 0.0,
                "downtime": 0.0,
                "hours": 0.0,
                "days_worked": 1,
                "excluded_minutes": excluded_minutes.get(person, {}).get(wc_name, 0.0),
            },
        )

    for wc_name, wc_credits in credits.items():
        for credit in wc_credits:
            if credit.person_name is None:
                continue
            if credit.productive_minutes <= 0 and credit.actual_units <= 0:
                continue
            totals = entry(attribution_key(credit), wc_name)
            totals["units"] += credit.actual_units
            totals["hours"] += credit.productive_minutes / 60.0

    for wc_name, (_units, downtime) in wc_totals.items():
        people = [(person, wc_map[wc_name]) for person, wc_map in out.items() if wc_name in wc_map]
        total_hours = sum(totals["hours"] for _person, totals in people)
        if total_hours <= 0:
            continue
        for _person, totals in people:
            totals["downtime"] += float(downtime or 0) * totals["hours"] / total_hours
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


def metered_station_totals(
    client,
    day: date,
    now_utc: datetime | None = None,
):
    from . import staffing
    from .leaderboard import cached_leaderboard
    from .stations import Station

    stations = [
        Station(loc.meter_id, loc.name, loc.skill, loc.bay)
        for loc in staffing.LOCATIONS
        if loc.meter_id
    ]
    if not stations:
        return []
    if now_utc is None:
        return cached_leaderboard(client, stations, day)
    return cached_leaderboard(client, stations, day, now_utc)


def _metered_leaderboard(
    client,
    day: date,
    *,
    now_utc: datetime | None = None,
    stations=None,
    shift_by_day=None,
    cache_variant: str | None = None,
    persist: bool = True,
):
    """cached_leaderboard results for all metered WCs, or [] if none.
    Shared by _fetch_wc_totals and _fetch_wc_samples so the station-building
    block can't drift between them."""
    from . import staffing  # local import — staffing imports leaderboard.Station
    from .leaderboard import leaderboard as uncached_leaderboard

    frozen_inputs = bool(
        stations is not None
        or shift_by_day is not None
        or cache_variant is not None
        or not persist
    )
    if not frozen_inputs:
        return metered_station_totals(client, day, now_utc)
    if stations is None:
        from .stations import Station

        metered = [loc for loc in staffing.LOCATIONS if loc.meter_id]
        if not metered:
            return []
        stations = [
            Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)
            for loc in metered
        ]
    return uncached_leaderboard(
        client,
        list(stations),
        day,
        now_utc,
        shift_by_day=shift_by_day,
    )


def _fetch_wc_totals(client, day: date) -> dict[str, tuple[int, int]]:
    """Returns {wc_name: (units, downtime_minutes)} for every metered WC.

    Only consults staffing.LOCATIONS and pulls the WCs that have a meter_id.
    Unmetered WCs return no entry; callers should treat missing entries as
    zero output (which is what attribute_for_day does).
    """
    return {
        r.station.name: (r.units, r.downtime_minutes) for r in _metered_leaderboard(client, day)
    }


def _fetch_wc_samples(client, day: date) -> dict[str, list[tuple]]:
    """``{wc_name: [(event_dt_utc, units), ...]}`` for metered WCs on ``day``.
    Reuses the cached leaderboard (same call _fetch_wc_totals makes), so this
    is cheap when both run for the same day."""
    return {r.station.name: list(r.samples) for r in _metered_leaderboard(client, day)}


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
            u for (t, u) in samples if any(e is not None and s <= t < e for (s, e) in windows)
        )
        units, downtime = out[wc]
        out[wc] = (max(0, units - testing_units), downtime)
    return out


def _without_testing_samples(
    samples_by_wc: dict[str, list[tuple]],
    testing_windows: dict[str, list[tuple]],
) -> dict[str, list[tuple]]:
    """Drop samples that were produced in a no-credit testing window."""
    if not testing_windows:
        return samples_by_wc
    return {
        wc_name: [
            (timestamp, units)
            for timestamp, units in samples
            if not any(
                end is not None and start <= timestamp < end
                for start, end in testing_windows.get(wc_name, [])
            )
        ]
        for wc_name, samples in samples_by_wc.items()
    }


def _elapsed_minutes_for(d: date) -> int:
    """Productive minutes available on day d, evaluated as of right now."""
    from datetime import datetime
    from .shift_config import shift_elapsed_minutes  # local — pulls tzdata

    return shift_elapsed_minutes(d, datetime.now(UTC))


def _effective_now(day: date, now: datetime) -> datetime:
    """`now`, clamped to `day`'s shift end. Used to cap an OPEN breakdown
    exclusion window for a past day (or a today read taken after hours) so
    excluded-minutes math never runs past the shift that actually happened."""
    from .shift_config import shift_end_for, SITE_TZ

    shift_end_utc = datetime.combine(day, shift_end_for(day), tzinfo=SITE_TZ).astimezone(UTC)
    return min(now, shift_end_utc)


def _breakdown_window_identity(key: tuple) -> tuple[int | None, str, str]:
    """Normalize new ID-backed and legacy name-backed breakdown keys."""
    if len(key) == 3:
        employee_odoo_id, person_name, wc_name = key
        return employee_odoo_id, person_name, wc_name
    person_name, wc_name = key
    return None, person_name, wc_name


def _excluded_minutes_by_person_wc(
    day: date,
    now: datetime,
    productive_minutes_in_window=None,
) -> dict[PersonAttributionKey, dict[str, float]]:
    """{person: {wc_name: minutes}} of machine-breakdown-excluded minutes for
    `day`. Open breakdown windows are capped at `now` (already clamped to
    shift end by the caller) so a live in-progress breakdown is reflected
    immediately, matching the design's "today's live averages are correct
    during the outage" requirement."""
    from . import wc_attributions, machine_breakdown
    if productive_minutes_in_window is None:
        from .shift_config import productive_minutes_in_window

    windows_by_key = wc_attributions.breakdown_windows_for_day(day)
    out: dict[PersonAttributionKey, dict[str, float]] = {}
    for raw_key, windows in windows_by_key.items():
        employee_odoo_id, person, wc = _breakdown_window_identity(raw_key)
        closed = [(s, e if e is not None else now) for (s, e) in windows]
        minutes = machine_breakdown.excluded_minutes_for_windows(
            closed, day, productive_minutes_in_window
        )
        if minutes > 0:
            identity: PersonAttributionKey = (
                (employee_odoo_id, person)
                if employee_odoo_id is not None
                else person
            )
            out.setdefault(identity, {})[wc] = minutes
    return out


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise TypeError(f"{field_name} must be an aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be an aware UTC datetime")
    return value.astimezone(UTC)


def _strict_shift_bounds(day: date) -> tuple[datetime, datetime]:
    from . import shift_config

    start = datetime.combine(
        day, shift_config.shift_start_for(day), tzinfo=shift_config.SITE_TZ
    ).astimezone(UTC)
    end = datetime.combine(
        day, shift_config.shift_end_for(day), tzinfo=shift_config.SITE_TZ
    ).astimezone(UTC)
    if end <= start:
        raise ProductionSourceUnavailable(
            f"strict production has an invalid shift window for {day.isoformat()}"
        )
    return start, end


def _strict_break_windows(day: date) -> tuple[tuple[datetime, datetime], ...]:
    from . import shift_config

    windows = []
    for configured in shift_config.breaks_for(day):
        start = datetime.combine(day, configured.start, tzinfo=shift_config.SITE_TZ).astimezone(UTC)
        end = datetime.combine(day, configured.end, tzinfo=shift_config.SITE_TZ).astimezone(UTC)
        if end > start:
            windows.append((start, end))
    return tuple(windows)


def _normalize_strict_leaderboard(
    totals: Sequence,
    *,
    shift_start_utc: datetime,
    shift_end_utc: datetime,
) -> tuple[
    dict[str, tuple[float, float]],
    dict[str, list[tuple[datetime, float]]],
    dict[str, tuple[tuple[datetime, datetime], ...]],
]:
    wc_totals: dict[str, tuple[float, float]] = {}
    samples_by_wc: dict[str, list[tuple[datetime, float]]] = {}
    active_by_wc: dict[str, tuple[tuple[datetime, datetime], ...]] = {}
    for total in totals:
        wc_name = getattr(getattr(total, "station", None), "name", None)
        if not isinstance(wc_name, str) or not wc_name or wc_name in wc_totals:
            raise ProductionSourceUnavailable("strict production station is malformed")
        raw_total = getattr(total, "units", None)
        if isinstance(raw_total, bool) or not isinstance(raw_total, Real):
            raise ProductionSourceUnavailable(f"strict production total for {wc_name} is malformed")
        units_total = float(raw_total)
        if not math.isfinite(units_total) or units_total < 0:
            raise ProductionSourceUnavailable(
                f"strict production total for {wc_name} cannot be negative"
            )
        raw_downtime = getattr(total, "downtime_minutes", 0)
        if isinstance(raw_downtime, bool) or not isinstance(raw_downtime, Real):
            raise ProductionSourceUnavailable(f"strict downtime total for {wc_name} is malformed")
        downtime = float(raw_downtime)
        if not math.isfinite(downtime):
            raise ProductionSourceUnavailable(f"strict downtime total for {wc_name} is malformed")
        wc_totals[wc_name] = (units_total, max(0.0, downtime))

        normalized_samples = []
        for raw_sample in getattr(total, "samples", ()):
            if not isinstance(raw_sample, (tuple, list)) or len(raw_sample) != 2:
                raise ProductionSourceUnavailable(
                    f"strict production sample for {wc_name} is malformed"
                )
            raw_timestamp, raw_units = raw_sample
            try:
                timestamp = _aware_utc(raw_timestamp, "sample timestamp")
            except (TypeError, ValueError) as exc:
                raise ProductionSourceUnavailable(
                    f"strict production sample for {wc_name} is malformed"
                ) from exc
            if not shift_start_utc <= timestamp < shift_end_utc:
                raise ProductionSourceUnavailable(
                    f"strict production sample for {wc_name} is outside the shift"
                )
            if isinstance(raw_units, bool) or not isinstance(raw_units, Real):
                raise ProductionSourceUnavailable(
                    f"strict production sample for {wc_name} is malformed"
                )
            units = float(raw_units)
            if not math.isfinite(units) or units <= 0:
                raise ProductionSourceUnavailable(
                    f"strict production sample for {wc_name} must be positive"
                )
            normalized_samples.append((timestamp, units))
        normalized_samples.sort(key=lambda item: item[0])
        samples_by_wc[wc_name] = normalized_samples

        intervals = []
        for raw_interval in getattr(total, "active_intervals", ()):
            if not isinstance(raw_interval, (tuple, list)) or len(raw_interval) != 2:
                raise ProductionSourceUnavailable(
                    f"strict active interval for {wc_name} is malformed"
                )
            try:
                start = _aware_utc(raw_interval[0], "active interval start")
                end = _aware_utc(raw_interval[1], "active interval end")
            except (TypeError, ValueError) as exc:
                raise ProductionSourceUnavailable(
                    f"strict active interval for {wc_name} is malformed"
                ) from exc
            start = max(start, shift_start_utc)
            end = min(end, shift_end_utc)
            if end > start:
                intervals.append((start, end))
        active_by_wc[wc_name] = tuple(sorted(intervals))
    return wc_totals, samples_by_wc, active_by_wc


def _strict_meter_fingerprint(
    wc_totals: Mapping[str, tuple[float, float]],
    samples_by_wc: Mapping[str, Sequence[tuple[datetime, float]]],
    active_intervals_by_wc: Mapping[
        str, Sequence[tuple[datetime, datetime]]
    ],
) -> str:
    """Hash the normalized meter facts that strict attribution actually used."""

    payload = {
        "version": 1,
        "totals": [
            [wc_name, float(values[0]).hex(), float(values[1]).hex()]
            for wc_name, values in sorted(wc_totals.items())
        ],
        "samples": [
            [wc_name, timestamp.astimezone(UTC).isoformat(), float(units).hex()]
            for wc_name, samples in sorted(samples_by_wc.items())
            for timestamp, units in samples
        ],
        "active": [
            [
                wc_name,
                start.astimezone(UTC).isoformat(),
                end.astimezone(UTC).isoformat(),
            ]
            for wc_name, intervals in sorted(active_intervals_by_wc.items())
            for start, end in intervals
        ],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _strict_request_fingerprint(
    local_source_fingerprint: str,
    wc_totals: Mapping[str, tuple[float, float]],
    samples_by_wc: Mapping[str, Sequence[tuple[datetime, float]]],
    active_intervals_by_wc: Mapping[
        str, Sequence[tuple[datetime, datetime]]
    ],
) -> str:
    meter = _strict_meter_fingerprint(
        wc_totals,
        samples_by_wc,
        active_intervals_by_wc,
    )
    encoded = json.dumps(
        {"version": 1, "local": local_source_fingerprint, "meter": meter},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_strict_sample_totals(
    wc_totals: Mapping[str, tuple[float, float]],
    samples_by_wc: Mapping[str, Sequence[tuple[datetime, float]]],
) -> None:
    for wc_name in set(wc_totals) | set(samples_by_wc):
        source_total = float(wc_totals.get(wc_name, (0.0, 0.0))[0])
        sample_total = sum(units for _timestamp, units in samples_by_wc.get(wc_name, ()))
        if abs(source_total - sample_total) > SAMPLE_TOTAL_TOLERANCE:
            raise ProductionSourceUnavailable(
                f"strict production samples for {wc_name} do not match the adjusted total"
            )


def _identity_safe_excluded_minutes(
    segments: Sequence,
    excluded_by_identity: Mapping[PersonAttributionKey, Mapping[str, float]],
) -> dict[PersonAttributionKey, dict[str, float]]:
    ids_by_name_wc: dict[tuple[str, str], set[int]] = {}
    for segment in segments:
        if segment.person_odoo_id is not None:
            ids_by_name_wc.setdefault((segment.person_name, segment.wc_name), set()).add(
                segment.person_odoo_id
            )
    safe: dict[PersonAttributionKey, dict[str, float]] = {}
    segment_identities_by_id_wc: dict[
        tuple[int, str], set[PersonAttributionKey]
    ] = {}
    for segment in segments:
        if segment.person_odoo_id is None:
            continue
        segment_identities_by_id_wc.setdefault(
            (segment.person_odoo_id, segment.wc_name), set()
        ).add((segment.person_odoo_id, segment.person_name))
    for identity, wc_map in excluded_by_identity.items():
        if isinstance(identity, tuple):
            employee_odoo_id, _stored_name = identity
            for wc_name, minutes in wc_map.items():
                candidates = segment_identities_by_id_wc.get(
                    (employee_odoo_id, wc_name), set()
                )
                target = (
                    identity
                    if identity in candidates
                    else next(iter(candidates))
                    if len(candidates) == 1
                    else None
                )
                if target is not None:
                    wc_out = safe.setdefault(target, {})
                    wc_out[wc_name] = wc_out.get(wc_name, 0.0) + float(minutes)
            continue
        name = identity
        for wc_name, minutes in wc_map.items():
            employee_ids = ids_by_name_wc.get((name, wc_name), set())
            if len(employee_ids) != 1:
                continue
            employee_id = next(iter(employee_ids))
            safe.setdefault((employee_id, name), {})[wc_name] = float(minutes)
    return safe


def _legacy_name_excluded_minutes(
    excluded_by_identity: Mapping[PersonAttributionKey, Mapping[str, float]],
) -> dict[str, dict[str, float]]:
    """Project ID-backed rows onto the legacy matcher's name-only identity."""
    projected: dict[str, dict[str, float]] = {}
    for identity, wc_map in excluded_by_identity.items():
        person_name = identity[1] if isinstance(identity, tuple) else identity
        for wc_name, minutes in wc_map.items():
            wc_out = projected.setdefault(person_name, {})
            wc_out[wc_name] = wc_out.get(wc_name, 0.0) + float(minutes)
    return projected


def _strict_inputs_for_day(
    day: date,
    client,
    *,
    now_utc: datetime,
    leaderboard_rows=None,
    map_work_center=None,
    shift_bounds: tuple[datetime, datetime] | None = None,
    break_windows: tuple[tuple[datetime, datetime], ...] | None = None,
    source_config_fingerprint: str | None = None,
) -> _StrictDayInputs:
    from . import db

    source_snapshot = None
    if shift_bounds is None or break_windows is None or leaderboard_rows is None:
        with db.read_snapshot() as cur:
            source_snapshot = strict_source_snapshot(day, cur=cur)
        source_config_fingerprint = source_snapshot.source_fingerprint
        shift_bounds = (
            source_snapshot.shift_start_utc,
            source_snapshot.shift_end_utc,
        )
        break_windows = source_snapshot.break_windows
        if map_work_center is None:
            mapping = dict(source_snapshot.work_center_by_odoo_id)
            map_work_center = lambda odoo_id: mapping.get(odoo_id)
    if leaderboard_rows is None:
        assert source_snapshot is not None
        leaderboard_rows = _metered_leaderboard(
            client,
            day,
            now_utc=now_utc,
            stations=source_snapshot.stations,
            shift_by_day=source_snapshot.shift_by_day,
            cache_variant=source_snapshot.source_fingerprint,
            persist=False,
        )
    with db.read_snapshot() as cur:
        current_source = strict_source_snapshot(day, cur=cur)
        if (
            source_config_fingerprint is not None
            and current_source.source_fingerprint != source_config_fingerprint
        ):
            raise ProductionSourceUnavailable(
                "strict production configuration changed during collection"
            )
        local_source_fingerprint = strict_local_source_fingerprint(
            day,
            cur=cur,
            source_snapshot=current_source,
        )
        return _strict_inputs_for_day_in_snapshot(
            day,
            now_utc=now_utc,
            leaderboard_rows=leaderboard_rows,
            map_work_center=map_work_center,
            shift_bounds=shift_bounds,
            break_windows=break_windows,
            source_fingerprint=local_source_fingerprint,
        )


def _strict_inputs_for_day_in_snapshot(
    day: date,
    *,
    now_utc: datetime,
    leaderboard_rows,
    map_work_center,
    shift_bounds: tuple[datetime, datetime],
    break_windows: tuple[tuple[datetime, datetime], ...],
    source_fingerprint: str,
) -> _StrictDayInputs:
    from . import (
        assignment_windows,
        attendance_mirror,
        attendance_timeline,
        wc_attributions,
    )

    health = attendance_mirror.health_snapshot()
    if health.baseline_completed_at is None:
        raise ProductionSourceUnavailable(
            f"Odoo attendance mirror baseline is unavailable for {day.isoformat()}"
        )
    if health.last_incremental_completed_at is None:
        raise ProductionSourceUnavailable(
            f"Odoo attendance has no verified snapshot for {day.isoformat()}"
        )

    shift_start, shift_end = shift_bounds
    resolved_break_windows = tuple(break_windows)
    timeline_kwargs = {
        "as_of_utc": now_utc,
        "health_snapshot": health,
    }
    if map_work_center is not None:
        timeline_kwargs["map_work_center"] = map_work_center
    spans = attendance_timeline.timeline_for_range(
        shift_start,
        shift_end,
        **timeline_kwargs,
    )
    segments = assignment_windows.work_segments_from_timeline(
        spans,
        window_start_utc=shift_start,
        window_end_utc=shift_end,
    )
    wc_totals, samples_by_wc, active_by_wc = _normalize_strict_leaderboard(
        leaderboard_rows,
        shift_start_utc=shift_start,
        shift_end_utc=shift_end,
    )
    testing = wc_attributions.testing_windows_for_day(day)
    if testing:
        wc_totals = _apply_testing_offsets(wc_totals, samples_by_wc, testing)
        samples_by_wc = _without_testing_samples(samples_by_wc, testing)
    _validate_strict_sample_totals(wc_totals, samples_by_wc)
    request_fingerprint = _strict_request_fingerprint(
        source_fingerprint,
        wc_totals,
        samples_by_wc,
        active_by_wc,
    )

    try:
        def productive_minutes(_day, start, end):
            total = int(max(0.0, (end - start).total_seconds()) // 60)
            for break_start, break_end in resolved_break_windows:
                lo = max(start, break_start)
                hi = min(end, break_end)
                if hi > lo:
                    total -= int((hi - lo).total_seconds() // 60)
            return max(0, total)

        excluded_by_identity = _excluded_minutes_by_person_wc(
            day,
            min(now_utc, shift_end),
            productive_minutes,
        )
    except Exception:
        excluded_by_identity = {}
    excluded = _identity_safe_excluded_minutes(segments, excluded_by_identity)
    breakdown = wc_attributions.breakdown_windows_for_day(day)
    return _StrictDayInputs(
        segments=tuple(segments),
        wc_totals=wc_totals,
        samples_by_wc=samples_by_wc,
        active_intervals_by_wc=active_by_wc,
        excluded_minutes=excluded,
        break_windows=resolved_break_windows,
        testing_windows=testing,
        breakdown_windows=breakdown,
        location_spans=tuple(spans),
        shift_start_utc=shift_start,
        shift_end_utc=shift_end,
        source_fingerprint=source_fingerprint,
        request_fingerprint=request_fingerprint,
    )


def _strict_attribution_from_inputs(day: date, inputs: _StrictDayInputs) -> Attribution:
    def productive_minutes(_person, _wc_name, start, end):
        total = int(max(0.0, (end - start).total_seconds()) // 60)
        for break_start, break_end in inputs.break_windows:
            lo = max(start, break_start)
            hi = min(end, break_end)
            if hi > lo:
                total -= int((hi - lo).total_seconds() // 60)
        return max(0, total)

    return attribute_for_segments(
        inputs.segments,
        wc_totals=inputs.wc_totals,
        samples_by_wc=inputs.samples_by_wc,
        productive_minutes=productive_minutes,
        excluded_minutes=inputs.excluded_minutes,
        strict=True,
    )


def _strict_attribution_for(
    day: date, client, *, now_utc: datetime
) -> AttributionResult:
    inputs = _strict_inputs_for_day(day, client, now_utc=now_utc)
    return AttributionResult(
        _strict_attribution_from_inputs(day, inputs),
        is_strict=True,
        source_fingerprint=inputs.source_fingerprint,
        request_fingerprint=inputs.request_fingerprint,
    )


def _subtract_intervals(
    intervals: Sequence[tuple[datetime, datetime]],
    exclusions: Sequence[tuple[datetime, datetime]],
) -> tuple[tuple[datetime, datetime], ...]:
    chunks = list(intervals)
    for exclusion_start, exclusion_end in sorted(exclusions):
        next_chunks = []
        for start, end in chunks:
            if exclusion_end <= start or exclusion_start >= end:
                next_chunks.append((start, end))
                continue
            if start < exclusion_start:
                next_chunks.append((start, exclusion_start))
            if exclusion_end < end:
                next_chunks.append((exclusion_end, end))
        chunks = next_chunks
    return tuple(chunks)


def _assigned_sample_times(
    samples: Sequence[tuple[datetime, float]],
    segments: Sequence,
    wc_name: str,
) -> set[datetime]:
    wc_segments = [segment for segment in segments if segment.wc_name == wc_name]
    return {
        timestamp
        for timestamp, _units in samples
        if any(segment.start_utc <= timestamp < segment.end_utc for segment in wc_segments)
    }


def _strict_unassigned_runs_from_inputs(
    day: date,
    inputs: _StrictDayInputs,
    *,
    now_utc: datetime,
) -> tuple[UnassignedRun, ...]:
    now = _aware_utc(now_utc, "now_utc")
    runs: list[UnassignedRun] = []
    shift_end = inputs.shift_end_utc
    if shift_end is None:
        _shift_start, shift_end = _strict_shift_bounds(day)
    for wc_name, samples in inputs.samples_by_wc.items():
        exclusions = [*inputs.break_windows]
        exclusions.extend(inputs.testing_windows.get(wc_name, ()))
        for raw_key, windows in inputs.breakdown_windows.items():
            _employee_odoo_id, _person_name, breakdown_wc = (
                _breakdown_window_identity(raw_key)
            )
            if breakdown_wc != wc_name:
                continue
            exclusions.extend(
                (start, end if end is not None else min(now, shift_end)) for start, end in windows
            )
        intervals = _subtract_intervals(inputs.active_intervals_by_wc.get(wc_name, ()), exclusions)
        runs.extend(
            unassigned_runs_for_samples(
                samples,
                _assigned_sample_times(samples, inputs.segments, wc_name),
                intervals,
                wc_name=wc_name,
            )
        )
    return tuple(sorted(runs, key=lambda run: (run.start_utc, run.wc_name)))


def unassigned_runs_for_day(
    day: date,
    client,
    *,
    now_utc: datetime | None = None,
) -> tuple[UnassignedRun, ...]:
    """Return uncovered sample runs only when the strict matcher owns the day."""
    from . import attendance_location_policy

    now = _aware_utc(now_utc or datetime.now(UTC), "now_utc")
    state = attendance_location_policy.match_state_for_day(day, now_utc=now)
    if state == "pending":
        raise ProductionSourceUnavailable(
            f"strict production cutover is pending for {day.isoformat()}"
        )
    if state == "legacy":
        return ()
    inputs = _strict_inputs_for_day(day, client, now_utc=now)
    return _strict_unassigned_runs_from_inputs(day, inputs, now_utc=now)


def attribution_for(d: date, client, *, now_utc: datetime | None = None) -> AttributionResult:
    """Choose the matcher once, before loading any attribution source."""
    from . import attendance_location_policy

    now = _aware_utc(now_utc or datetime.now(UTC), "now_utc")
    try:
        state = attendance_location_policy.match_state_for_day(d, now_utc=now)
    except RuntimeError as exc:
        # Unit-only legacy callers intentionally run without a database. The
        # deployed app cannot reach this path because startup requires a pool.
        if "DATABASE_URL is not set" not in str(exc):
            raise
        state = "legacy"
    if state == "pending":
        raise ProductionSourceUnavailable(
            f"strict production cutover is pending for {d.isoformat()}"
        )
    if state == "strict":
        return _strict_attribution_for(d, client, now_utc=now)
    return AttributionResult(_legacy_attribution_for(d, client), is_strict=False)


def _legacy_attribution_for(d: date, client) -> Attribution:
    """Attribute production on a single day.

    Odoo work-center attendance is authoritative whenever it is available.
    The schedule fills only people without a tagged Odoo interval. For a
    current unpublished draft, tagged Odoo attendance can still be saved but
    the draft itself is never used as a fallback.

    Days with no saved assignments at all naturally produce {} via
    `attribute_for_day` (empty merged dict).
    """
    from datetime import datetime
    from . import (
        assignment_windows,
        shift_config,
        staffing,
        timeclock_windows,
        wc_attributions,
    )

    sched = staffing.load_schedule(d)
    site_today = datetime.now(shift_config.SITE_TZ).date()
    attendance_windows, attendance_available = (
        timeclock_windows.attendance_windows_for_day_with_availability(d)
    )
    if not attendance_available:
        # precompute_day replaces every saved row for the day. A schedule
        # fallback during an Odoo outage could therefore erase an accurate
        # earlier snapshot or move production back to the scheduled station.
        raise ProductionSourceUnavailable(
            f"Odoo attendance is unavailable for {d.isoformat()}; "
            "saved production was left unchanged"
        )

    if d == site_today:
        current_windows, _refreshed_at = timeclock_windows.current_attendance_windows()
        attendance_windows = timeclock_windows.with_current_attendance_overrides(
            attendance_windows, current_windows
        )

    if d >= site_today and not sched.published and not attendance_windows:
        return {}

    wc_totals = _fetch_wc_totals(client, d)
    testing = wc_attributions.testing_windows_for_day(d)
    if attendance_windows:
        assignments = sched.assignments if sched.published or d < site_today else {}

        shift_start = datetime.combine(
            d, shift_config.shift_start_for(d), tzinfo=shift_config.SITE_TZ
        ).astimezone(UTC)
        shift_end = datetime.combine(
            d, shift_config.shift_end_for(d), tzinfo=shift_config.SITE_TZ
        ).astimezone(UTC)
        cap_utc = min(datetime.now(UTC), shift_end) if d == site_today else shift_end
        segments = assignment_windows.resolve_segments(
            assignments=assignments,
            attributions=wc_attributions.creditable_for_day(d),
            punch_windows=attendance_windows,
            shift_start_utc=shift_start,
            cap_utc=cap_utc,
            time_off_key=staffing.TIME_OFF_KEY,
        )
        if not segments:
            return {}
        samples_by_wc = _fetch_wc_samples(client, d)
        if testing:
            wc_totals = _apply_testing_offsets(wc_totals, samples_by_wc, testing)
            samples_by_wc = _without_testing_samples(samples_by_wc, testing)
        try:
            excluded = _excluded_minutes_by_person_wc(d, _effective_now(d, datetime.now(UTC)))
        except Exception:
            excluded = {}
        excluded = _legacy_name_excluded_minutes(excluded)
        return attribute_for_segments(
            segments,
            wc_totals=wc_totals,
            samples_by_wc=samples_by_wc,
            productive_minutes=lambda _person, _wc_name, start, end: (
                shift_config.productive_minutes_in_window(d, start, end)
            ),
            excluded_minutes=excluded,
        )

    elapsed = _elapsed_minutes_for(d)
    extra = wc_attributions.people_by_wc(d)
    if testing:
        samples_by_wc = _fetch_wc_samples(client, d)
        wc_totals = _apply_testing_offsets(wc_totals, samples_by_wc, testing)
    # Defensive like the two calls above: a breakdown-lookup DB hiccup should
    # degrade to "no exclusions" rather than take the whole attribution down.
    try:
        excluded = _excluded_minutes_by_person_wc(d, _effective_now(d, datetime.now(UTC)))
    except Exception:
        excluded = {}
    excluded = _legacy_name_excluded_minutes(excluded)
    return attribute_for_day(
        sched.assignments,
        wc_totals,
        elapsed,
        extra_assignments=extra,
        excluded_minutes=excluded,
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
            "units": float(r["units"]),
            "downtime": float(r["downtime"]),
            "hours": float(r["hours"]),
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
            "units": float(r["units"]),
            "downtime": float(r["downtime"]),
            "hours": float(r["hours"]),
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


def normalized_daily_records(start_d: date, end_d: date) -> list[dict]:
    """Return records for normalized production averages.

    Includes zero-unit worked days so one-hour-or-longer stints count fairly in normalized
    average denominators. Award/trophy paths should keep using
    ``daily_records``.
    """
    from . import precompute

    return precompute.normalized_daily_records_in_range(start_d, end_d)


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
        rows.append(
            {
                "name": person,
                "units": round(units, 1),
                "downtime": round(downtime, 1),
                "days_worked": days,
                "pct_of_target": round(pct, 1) if pct is not None else None,
                "expected": round(expected, 1),
            }
        )
    rows.sort(key=lambda r: (-(r["pct_of_target"] or -1), -r["units"]))
    return rows
