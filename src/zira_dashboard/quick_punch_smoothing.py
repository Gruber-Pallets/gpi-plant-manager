"""Smooth quick sign-out/sign-in mistakes into continuous work stints.

Two per-person rules, both bounded by ``QUICK_PUNCH_LIMIT``:

1. Came back: a person who leaves station A and is back at A within the limit
   worked one continuous stint at A. A sign-out gap or blips at other
   stations in between become A time.
2. Wrong first pick: the first stint after signing in (start of day, or back
   from being away longer than the limit) that lasts no longer than the
   limit, followed within the limit by a different station, joins that next
   station's stint.

Came-back runs to a fixed point first. Otherwise a detour right after sign-in
would be mistaken for a wrong first pick. Smoothing never crosses a blocking
window (a conflicting, unmapped or stale Odoo location). Only the app's
who-was-where view changes; Odoo attendance is never touched.

Pure -- no DB, no network, no clock.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from .assignment_windows import WorkSegment

QUICK_PUNCH_LIMIT = timedelta(minutes=5)

PersonKey: TypeAlias = int | str
Window: TypeAlias = tuple[datetime, datetime]
_Stint: TypeAlias = tuple[int, "WorkSegment"]


def person_key(segment: WorkSegment) -> PersonKey:
    """Group by Odoo employee ID, falling back to the display name."""
    if segment.person_odoo_id is not None:
        return segment.person_odoo_id
    return segment.person_name


def smooth_quick_punches(
    segments: Sequence[WorkSegment],
    *,
    blocked_windows: Mapping[PersonKey, Sequence[Window]] | None = None,
    limit: timedelta = QUICK_PUNCH_LIMIT,
) -> tuple[WorkSegment, ...]:
    """Return ``segments`` with each person's quick punch mistakes merged away.

    ``blocked_windows`` maps a person to windows that smoothing must never
    bridge. Its keys must equal ``person_key(segment)``: the Odoo employee ID,
    or the display name when ``person_odoo_id`` is None. A window blocks only
    when it overlaps the open gap between two stints; one that merely touches
    the gap does not.

    Untouched segments keep their input order; a merged stint takes the
    position of its earliest input segment.
    """
    blocked_windows = blocked_windows or {}
    by_person: dict[PersonKey, list[_Stint]] = {}
    for index, segment in enumerate(segments):
        by_person.setdefault(person_key(segment), []).append((index, segment))

    placed: list[_Stint] = []
    for key, stints in by_person.items():
        ordered = sorted(
            stints,
            key=lambda item: (item[1].start_utc, item[1].end_utc, item[0]),
        )
        blocked = tuple(blocked_windows.get(key, ()))
        ordered = _apply_came_back(ordered, blocked, limit)
        ordered = _apply_wrong_first_pick(ordered, blocked, limit)
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
    limit: timedelta,
) -> int | None:
    _index, current = stints[i]
    for j in range(i + 1, len(stints)):
        _candidate_index, candidate = stints[j]
        if candidate.start_utc - current.end_utc > limit:
            return None
        if candidate.wc_name != current.wc_name:
            continue
        if any(
            segment.start_utc < current.end_utc or segment.end_utc > candidate.start_utc
            for _idx, segment in stints[i + 1 : j]
        ):
            return None
        if _crosses_blocked(current.end_utc, candidate.start_utc, blocked):
            return None
        return j
    return None


def _apply_came_back(
    stints: Sequence[_Stint],
    blocked: Sequence[Window],
    limit: timedelta,
) -> list[_Stint]:
    stints = list(stints)
    i = 0
    while i < len(stints):
        match = _came_back_match(stints, i, blocked, limit)
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


def _is_wrong_first_pick(
    stints: Sequence[_Stint],
    k: int,
    blocked: Sequence[Window],
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
        and not _crosses_blocked(current.end_utc, following.start_utc, blocked)
    )


def _apply_wrong_first_pick(
    stints: Sequence[_Stint],
    blocked: Sequence[Window],
    limit: timedelta,
) -> list[_Stint]:
    stints = list(stints)
    k = 0
    while k < len(stints) - 1:
        if not _is_wrong_first_pick(stints, k, blocked, limit):
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
