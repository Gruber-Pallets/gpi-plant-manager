"""Smooth quick sign-out/sign-in mistakes into continuous work stints.

Two per-person rules, both bounded by ``QUICK_PUNCH_LIMIT``:

1. Came back: a person who leaves station A and is back at A within the limit
   worked one continuous stint at A. A sign-out gap or blips at other
   stations in between become A time. Two things keep the stints apart,
   because merging would take pallets away from real work:
   - someone else relieved station A while the person was away (arrived or
     left during the gap, or exactly filled it). A partner at A from before
     the gap until after it does not count;
   - the meter proves a blip's station made pallets during the blip that no
     one else there covers. Those pallets would be left unassigned.
2. Wrong first pick: the first stint after signing in (start of day, or back
   from being away longer than the limit) that lasts no longer than the
   limit, followed within the limit by a different station, joins that next
   station's stint. It applies only when the meter proves no pallets were
   made at that station during the short stint; a short stint with real
   production is real work and stays.

Came-back runs to a fixed point first. Otherwise a detour right after sign-in
would be mistaken for a wrong first pick. Smoothing never crosses a blocking
window (a conflicting, unmapped or stale Odoo location). Only the app's
who-was-where view changes; Odoo attendance is never touched.

Pure -- no DB, no network, no clock.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
import math
from numbers import Real
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from .assignment_windows import WorkSegment

QUICK_PUNCH_LIMIT = timedelta(minutes=5)

PersonKey: TypeAlias = int | str
Window: TypeAlias = tuple[datetime, datetime]
_Stint: TypeAlias = tuple[int, "WorkSegment"]


def production_times_from_samples(
    samples_by_wc: Mapping[str, Iterable[tuple[datetime, float]]],
) -> dict[str, tuple[datetime, ...]]:
    """Timestamps where each station's meter recorded pallets (units > 0).

    Never raises. A station with any malformed sample -- units that are not a
    finite number, or a time that is not a timezone-aware datetime -- is left
    out (unknown). A bad reading might have been a real pallet, so it must not
    make the station look idle.
    """
    times: dict[str, tuple[datetime, ...]] = {}
    for wc_name, samples in samples_by_wc.items():
        pallets = _pallet_times(samples)
        if pallets is not None:
            times[wc_name] = pallets
    return times


def _pallet_times(samples) -> tuple[datetime, ...] | None:
    """One station's pallet times, or None when any sample is malformed."""
    pallets: list[datetime] = []
    try:
        for timestamp, units in samples:
            if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
                return None
            if (
                isinstance(units, bool)
                or not isinstance(units, Real)
                or not math.isfinite(units)
            ):
                return None
            if units > 0:
                pallets.append(timestamp)
    except (TypeError, ValueError):  # not iterable, or not (time, units) pairs
        return None
    return tuple(pallets)


def person_key(segment: WorkSegment) -> PersonKey:
    """Group by Odoo employee ID, falling back to the display name."""
    if segment.person_odoo_id is not None:
        return segment.person_odoo_id
    return segment.person_name


def _in_metered_scope(
    wc_name: str, metered_wc_names: Collection[str] | None
) -> bool:
    """Whether this station may be auto-smoothed.

    ``None`` means no extra restriction (unit tests). A set is the allowlist
    of production stations that have a meter; Maintenance and other
    unmetered names stay as real punches.
    """
    return metered_wc_names is None or wc_name in metered_wc_names


def smooth_quick_punches(
    segments: Sequence[WorkSegment],
    *,
    blocked_windows: Mapping[PersonKey, Sequence[Window]] | None = None,
    production_times_by_wc: Mapping[str, Sequence[datetime]] | None = None,
    metered_wc_names: Collection[str] | None = None,
    limit: timedelta = QUICK_PUNCH_LIMIT,
) -> tuple[WorkSegment, ...]:
    """Return ``segments`` with each person's quick punch mistakes merged away.

    ``blocked_windows`` maps a person to windows that smoothing must never
    bridge. Its keys must equal ``person_key(segment)``: the Odoo employee ID,
    or the display name when ``person_odoo_id`` is None. A window blocks only
    when it overlaps the open gap between two stints; one that merely touches
    the gap does not.

    ``production_times_by_wc`` maps a station to the times its meter recorded
    pallets (see ``production_times_from_samples``). A pallet at time ``t`` is
    during a stint when ``start <= t < end``. The wrong-first-pick rule fires
    only when the short stint's station has an entry with no pallet during
    it. The came-back rule keeps a blip whose station has an entry with a
    pallet during the blip that no other person's input segment at that
    station covers. None, or no entry for a station, means unknown: the
    wrong-first-pick rule does not fire there, and came-back merges as usual.

    ``metered_wc_names`` limits smoothing to production stations that have a
    meter. When it is a set, a came-back merge needs the home station and
    every absorbed blip to be in it, and a wrong first pick needs both
    stations in it. Maintenance, trucks, forklifts, and unmetered production
    stations are left as real punches. ``None`` applies no extra restriction.

    Came-back also never merges across a relief: another person's input
    segment at the same station that overlaps the gap without strictly
    spanning it. This check needs no meter data.

    Untouched segments keep their input order; a merged stint takes the
    position of its earliest input segment.
    """
    blocked_windows = blocked_windows or {}
    by_person: dict[PersonKey, list[_Stint]] = {}
    segments_by_wc: dict[str, list[WorkSegment]] = {}
    for index, segment in enumerate(segments):
        by_person.setdefault(person_key(segment), []).append((index, segment))
        segments_by_wc.setdefault(segment.wc_name, []).append(segment)

    placed: list[_Stint] = []
    for key, stints in by_person.items():
        ordered = sorted(
            stints,
            key=lambda item: (item[1].start_utc, item[1].end_utc, item[0]),
        )
        blocked = tuple(blocked_windows.get(key, ()))
        ordered = _apply_came_back(
            ordered,
            blocked,
            production_times_by_wc,
            segments_by_wc,
            metered_wc_names,
            limit,
        )
        ordered = _apply_wrong_first_pick(
            ordered,
            blocked,
            production_times_by_wc,
            segments_by_wc,
            metered_wc_names,
            limit,
        )
        placed.extend(ordered)
    return tuple(segment for _index, segment in sorted(placed, key=lambda item: item[0]))


def _crosses_blocked(left: datetime, right: datetime, blocked: Sequence[Window]) -> bool:
    """Whether the open gap ``(left, right)`` overlaps a blocking window."""
    if right <= left:
        return False
    return any(start < right and end > left for start, end in blocked)


def _came_back_match(
    stints: Sequence[_Stint],
    i: int,
    blocked: Sequence[Window],
    production_times_by_wc: Mapping[str, Sequence[datetime]] | None,
    segments_by_wc: Mapping[str, Sequence[WorkSegment]],
    metered_wc_names: Collection[str] | None,
    limit: timedelta,
) -> int | None:
    _index, current = stints[i]
    if not _in_metered_scope(current.wc_name, metered_wc_names):
        return None
    for j in range(i + 1, len(stints)):
        _candidate_index, candidate = stints[j]
        if candidate.start_utc - current.end_utc > limit:
            return None
        if candidate.wc_name != current.wc_name:
            continue
        between = stints[i + 1 : j]
        if any(
            segment.start_utc < current.end_utc or segment.end_utc > candidate.start_utc
            for _idx, segment in between
        ):
            return None
        if any(
            not _in_metered_scope(segment.wc_name, metered_wc_names)
            for _idx, segment in between
        ):
            return None
        if _crosses_blocked(current.end_utc, candidate.start_utc, blocked):
            return None
        if _was_relieved(current, candidate, segments_by_wc):
            return None
        if any(
            _has_orphaned_pallets(segment, production_times_by_wc, segments_by_wc)
            for _idx, segment in between
        ):
            return None
        return j
    return None


def _was_relieved(
    current: WorkSegment,
    candidate: WorkSegment,
    segments_by_wc: Mapping[str, Sequence[WorkSegment]],
) -> bool:
    """Whether someone else took over the station while this person was away.

    The gap runs from ``current.end_utc`` to ``candidate.start_utc``. Another
    person's input segment at the same station that overlaps it without
    strictly spanning it -- arriving or leaving during the gap, or exactly
    filling it -- is a relief: merging would split their pallets with the
    person who left. A partner there from before the gap until after it
    worked alongside them and does not block. An empty gap has no relief.
    """
    gap_start, gap_end = current.end_utc, candidate.start_utc
    key = person_key(current)
    return any(
        other.start_utc < gap_end
        and other.end_utc > gap_start
        and not (other.start_utc < gap_start and other.end_utc > gap_end)
        for other in segments_by_wc.get(current.wc_name, ())
        if person_key(other) != key
    )


def _first_pick_takes_over_someone(
    current: WorkSegment,
    following: WorkSegment,
    segments_by_wc: Mapping[str, Sequence[WorkSegment]],
) -> bool:
    """Whether folding a first pick forward would claim another person's time.

    The fold gives ``following``'s station the window from ``current.start_utc``
    to ``following.start_utc``. Another person's input segment there that
    overlaps the window without covering all of it -- arriving during it, or
    leaving before this person got there -- was running that station, so the
    fold would split their pallets. Unlike came-back's gap, this window starts
    at sign-in, when most people clock in together: someone there from the
    window's start through after it is a partner and does not block.
    """
    window_start, window_end = current.start_utc, following.start_utc
    key = person_key(current)
    return any(
        other.start_utc < window_end
        and other.end_utc > window_start
        and not (other.start_utc <= window_start and other.end_utc > window_end)
        for other in segments_by_wc.get(following.wc_name, ())
        if person_key(other) != key
    )


def _apply_came_back(
    stints: Sequence[_Stint],
    blocked: Sequence[Window],
    production_times_by_wc: Mapping[str, Sequence[datetime]] | None,
    segments_by_wc: Mapping[str, Sequence[WorkSegment]],
    metered_wc_names: Collection[str] | None,
    limit: timedelta,
) -> list[_Stint]:
    stints = list(stints)
    i = 0
    while i < len(stints):
        match = _came_back_match(
            stints,
            i,
            blocked,
            production_times_by_wc,
            segments_by_wc,
            metered_wc_names,
            limit,
        )
        if match is None:
            i += 1
            continue
        absorbed = stints[i : match + 1]
        merged = replace(
            stints[i][1],
            end_utc=max(segment.end_utc for _idx, segment in absorbed),
        )
        stints[i : match + 1] = [(min(idx for idx, _segment in absorbed), merged)]
    return stints


def _station_was_idle(
    segment: WorkSegment,
    production_times_by_wc: Mapping[str, Sequence[datetime]] | None,
) -> bool:
    """Whether the meter proves no pallets were made at the stint's station.

    Unknown -- no meter data passed, or none for that station -- counts as not
    idle, so a real short stint is never erased.
    """
    return _pallets_during(segment, production_times_by_wc) == ()


def _has_orphaned_pallets(
    segment: WorkSegment,
    production_times_by_wc: Mapping[str, Sequence[datetime]] | None,
    segments_by_wc: Mapping[str, Sequence[WorkSegment]],
) -> bool:
    """Whether the meter proves pallets during ``segment`` that no one else covers.

    Such pallets would become unassigned if the stint were merged away. A
    pallet is covered by another person's segment at the same station that
    holds it (``start <= t < end``). Unknown meter data proves nothing.
    """
    pallets = _pallets_during(segment, production_times_by_wc)
    if not pallets:
        return False
    key = person_key(segment)
    others = tuple(
        other
        for other in segments_by_wc.get(segment.wc_name, ())
        if person_key(other) != key
    )
    return any(
        not any(other.start_utc <= at < other.end_utc for other in others)
        for at in pallets
    )


def _pallets_during(
    segment: WorkSegment,
    production_times_by_wc: Mapping[str, Sequence[datetime]] | None,
) -> tuple[datetime, ...] | None:
    """Pallet times at the stint's station during it, or None when unknown.

    ``start <= t < end`` mirrors how ``production_segments.credit_work_segments``
    decides which stint covers a sample.
    """
    if production_times_by_wc is None:
        return None
    times = production_times_by_wc.get(segment.wc_name)
    if times is None:
        return None
    return tuple(at for at in times if segment.start_utc <= at < segment.end_utc)


def _is_wrong_first_pick(
    stints: Sequence[_Stint],
    k: int,
    blocked: Sequence[Window],
    production_times_by_wc: Mapping[str, Sequence[datetime]] | None,
    segments_by_wc: Mapping[str, Sequence[WorkSegment]],
    metered_wc_names: Collection[str] | None,
    limit: timedelta,
) -> bool:
    _index, current = stints[k]
    _next_index, following = stints[k + 1]
    previous_end = max((segment.end_utc for _idx, segment in stints[:k]), default=None)
    starts_presence = previous_end is None or current.start_utc - previous_end > limit
    return (
        starts_presence
        and current.end_utc - current.start_utc <= limit
        and following.wc_name != current.wc_name
        and following.start_utc - current.end_utc <= limit
        and _in_metered_scope(current.wc_name, metered_wc_names)
        and _in_metered_scope(following.wc_name, metered_wc_names)
        and not _crosses_blocked(current.end_utc, following.start_utc, blocked)
        and _station_was_idle(current, production_times_by_wc)
        and not _first_pick_takes_over_someone(current, following, segments_by_wc)
    )


def _apply_wrong_first_pick(
    stints: Sequence[_Stint],
    blocked: Sequence[Window],
    production_times_by_wc: Mapping[str, Sequence[datetime]] | None,
    segments_by_wc: Mapping[str, Sequence[WorkSegment]],
    metered_wc_names: Collection[str] | None,
    limit: timedelta,
) -> list[_Stint]:
    stints = list(stints)
    k = 0
    while k < len(stints) - 1:
        if not _is_wrong_first_pick(
            stints,
            k,
            blocked,
            production_times_by_wc,
            segments_by_wc,
            metered_wc_names,
            limit,
        ):
            k += 1
            continue
        index, current = stints[k]
        next_index, following = stints[k + 1]
        merged = replace(
            following,
            start_utc=current.start_utc,
            end_utc=max(current.end_utc, following.end_utc),
        )
        stints[k : k + 2] = [(min(index, next_index), merged)]
    return stints
