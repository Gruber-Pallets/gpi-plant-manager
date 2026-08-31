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


Attribution: TypeAlias = dict[PersonAttributionKey, dict[str, dict[str, float]]]
SAMPLE_TOTAL_TOLERANCE = 1e-6


class AttributionResult(dict):
    """Backwards-compatible mapping carrying the already-resolved matcher."""

    def __init__(self, values: Mapping | None = None, *, is_strict: bool):
        super().__init__(values or {})
        self.is_strict = bool(is_strict)


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


def _metered_leaderboard(client, day: date, *, now_utc: datetime | None = None):
    """cached_leaderboard results for all metered WCs, or [] if none.
    Shared by _fetch_wc_totals and _fetch_wc_samples so the station-building
    block can't drift between them."""
    from . import staffing  # local import — staffing imports leaderboard.Station
    from .leaderboard import (
        cached_leaderboard as leaderboard,
    )  # local — leaderboard pulls shift_config/tzdata
    from .stations import Station

    metered = [loc for loc in staffing.LOCATIONS if loc.meter_id]
    if not metered:
        return []
    stations = [
        Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)
        for loc in metered
    ]
    if now_utc is None:
        return leaderboard(client, stations, day)
    return leaderboard(client, stations, day, now_utc)


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
    day: date, now: datetime
) -> dict[PersonAttributionKey, dict[str, float]]:
    """{person: {wc_name: minutes}} of machine-breakdown-excluded minutes for
    `day`. Open breakdown windows are capped at `now` (already clamped to
    shift end by the caller) so a live in-progress breakdown is reflected
    immediately, matching the design's "today's live averages are correct
    during the outage" requirement."""
    from . import wc_attributions, machine_breakdown
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

    shift_start, shift_end = _strict_shift_bounds(day)
    spans = attendance_timeline.timeline_for_range(shift_start, shift_end, as_of_utc=now_utc)
    segments = assignment_windows.work_segments_from_timeline(
        spans,
        window_start_utc=shift_start,
        window_end_utc=shift_end,
    )
    leaderboard_rows = _metered_leaderboard(client, day, now_utc=now_utc)
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

    try:
        excluded_by_identity = _excluded_minutes_by_person_wc(
            day, _effective_now(day, now_utc)
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
        break_windows=_strict_break_windows(day),
        testing_windows=testing,
        breakdown_windows=breakdown,
    )


def _strict_attribution_for(day: date, client, *, now_utc: datetime) -> Attribution:
    from . import shift_config

    inputs = _strict_inputs_for_day(day, client, now_utc=now_utc)
    return attribute_for_segments(
        inputs.segments,
        wc_totals=inputs.wc_totals,
        samples_by_wc=inputs.samples_by_wc,
        productive_minutes=lambda _person, _wc_name, start, end: (
            shift_config.productive_minutes_in_window(day, start, end)
        ),
        excluded_minutes=inputs.excluded_minutes,
        strict=True,
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
    runs: list[UnassignedRun] = []
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
        values = _strict_attribution_for(d, client, now_utc=now)
        return AttributionResult(values, is_strict=True)
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
