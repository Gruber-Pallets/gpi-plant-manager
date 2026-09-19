"""Plan quick-punch fixes for Odoo from one day's location spans.

Runs the dashboards' smoothing (``quick_punch_smoothing``) over valid Odoo
location spans and turns each merged stint into a ``PlannedFix`` -- but only
once it is settled, i.e. future punches can no longer change the answer.

A merged stint is reported as a ``SkippedFix`` (and retried on a later scan)
while any of these holds:

- ``break``: a blip at another station, or a filled sign-out gap, overlaps a
  scheduled break. Meters record no pallets during breaks, so idleness there
  can't be proven. Such a stint is never fixed in Odoo.
- ``relief``: the person left the stint's station and came back to it, and
  someone else took over that station meanwhile (smoothing's relief rule).
  The came-back rule already refuses that merge, but a chain of wrong first
  picks right after sign-in (A -> B -> A, all short and idle) can rebuild it,
  and in Odoo it would split the relief worker's pallets. Never fixed.
- ``settling``: less than ``SETTLE_DELAY`` has passed since the stint's last
  internal boundary (the latest start or end of an absorbed row other than
  the stint's own final end).
- ``first_pick_not_settled``: a short first pick was folded forward into the
  next station, but the person has not yet been at that station for more
  than the limit, so a quick return could still turn it into a came-back
  merge instead.
- ``meter_not_current``: a station the stint absorbed a blip from has no
  meter data known through the blip's end -- missing, truncated, read before
  the blip ended, or absent from ``production_times_by_wc`` (so smoothing
  could not check it for pallets that nobody else covers).

A first pick at a station with no meter data is never folded in the first
place (unknown means not idle), so it yields neither a fix nor a skip.

A merged stint made of one Odoo row split into several spans has nothing to
fix in Odoo and is dropped silently, as are untouched real stints.

Pure -- no DB, no network, no clock.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
from typing import TYPE_CHECKING

from . import assignment_windows, quick_punch_smoothing
from .attendance_corrections import QUICK_PUNCH_ITEM_KEY_PREFIX as ITEM_KEY_PREFIX

if TYPE_CHECKING:
    from .attendance_timeline import LocationSpan

SETTLE_DELAY = timedelta(minutes=2)


@dataclass(frozen=True)
class MeterFreshness:
    last_reading_at: datetime | None
    truncated: bool


@dataclass(frozen=True)
class SourceStint:
    wc_name: str
    start_utc: datetime
    end_utc: datetime
    is_open: bool
    attendance_ids: tuple[int, ...]


@dataclass(frozen=True)
class PlannedFix:
    employee_odoo_id: int
    person_name: str
    wc_name: str
    start_utc: datetime
    end_utc: datetime | None  # None: the person is still clocked into it
    source_attendance_ids: tuple[int, ...]
    before: tuple[SourceStint, ...]  # the absorbed real stints, in time order
    item_key: str


@dataclass(frozen=True)
class SkippedFix:
    employee_odoo_id: int
    person_name: str
    reason: str  # break | relief | settling | first_pick_not_settled | meter_not_current
    before: tuple[SourceStint, ...]


@dataclass(frozen=True)
class FixScan:
    fixes: tuple[PlannedFix, ...]
    skipped: tuple[SkippedFix, ...]


def item_key_for(employee_odoo_id: int, attendance_ids: Sequence[int]) -> str:
    """Stable for the same source rows; new for any later bounce."""
    joined = ",".join(str(value) for value in sorted(set(attendance_ids)))
    digest = hashlib.sha256(joined.encode()).hexdigest()[:24]
    return f"{ITEM_KEY_PREFIX}{employee_odoo_id}:{digest}"


def _overlaps(start: datetime, end: datetime, windows: Sequence[tuple[datetime, datetime]]) -> bool:
    return end > start and any(ws < end and we > start for ws, we in windows)


def _was_relieved(
    wc_name: str,
    absorbed: Sequence[SourceStint],
    others_at_station: Sequence[assignment_windows.WorkSegment],
) -> bool:
    """Whether someone else took over ``wc_name`` while this person was away from it.

    Each time the person leaves the station and comes back within the stint,
    the time away is checked with smoothing's relief rule: another person's
    stint there that overlaps it without strictly spanning it (arriving or
    leaving meanwhile, or exactly filling it) is a relief. A partner there
    from before until after does not count.
    """
    at_station = [stint for stint in absorbed if stint.wc_name == wc_name]
    for left, right in zip(at_station, at_station[1:]):
        away_start, away_end = left.end_utc, right.start_utc
        if away_end <= away_start:
            continue
        if any(
            other.start_utc < away_end
            and other.end_utc > away_start
            and not (other.start_utc < away_start and other.end_utc > away_end)
            for other in others_at_station
        ):
            return True
    return False


def find_fixes(
    spans: Sequence[LocationSpan],
    *,
    production_times_by_wc: Mapping[str, Sequence[datetime]] | None,
    meter_freshness_by_wc: Mapping[str, MeterFreshness],
    breaks: Sequence[tuple[datetime, datetime]],
    now_utc: datetime,
    limit: timedelta = quick_punch_smoothing.QUICK_PUNCH_LIMIT,
    settle_delay: timedelta = SETTLE_DELAY,
) -> FixScan:
    """Return the settled quick-punch merges in ``spans`` and why others wait.

    ``spans`` is one day's location spans for everyone (other people's spans
    feed the relief and orphaned-pallet guards). ``production_times_by_wc``
    and ``meter_freshness_by_wc`` come from the same meter read; ``breaks``
    are the day's scheduled breaks as UTC windows.
    """
    stints: list[tuple[assignment_windows.WorkSegment, SourceStint]] = []
    for span in spans:
        if span.status != "valid" or not span.app_work_center_name:
            continue
        segment = assignment_windows.WorkSegment(
            wc_name=span.app_work_center_name,
            person_name=span.employee_name,
            start_utc=span.start_utc,
            end_utc=span.end_utc,
            source="odoo",
            person_odoo_id=span.employee_odoo_id,
        )
        stints.append(
            (
                segment,
                SourceStint(
                    wc_name=span.app_work_center_name,
                    start_utc=span.start_utc,
                    end_utc=span.end_utc,
                    is_open=bool(span.is_open),
                    attendance_ids=tuple(span.attendance_ids),
                ),
            )
        )
    inputs = [segment for segment, _stint in stints]
    smoothed = quick_punch_smoothing.smooth_quick_punches(
        inputs,
        blocked_windows=assignment_windows.blocking_windows(spans),
        production_times_by_wc=production_times_by_wc,
        limit=limit,
    )
    input_ids = {id(segment) for segment in inputs}
    fixes: list[PlannedFix] = []
    skipped: list[SkippedFix] = []
    for merged in smoothed:
        if id(merged) in input_ids:
            continue  # untouched real stint
        absorbed = sorted(
            (
                stint
                for segment, stint in stints
                if segment.person_odoo_id == merged.person_odoo_id
                and segment.start_utc >= merged.start_utc
                and segment.end_utc <= merged.end_utc
            ),
            key=lambda item: (item.start_utc, item.end_utc),
        )
        ids = tuple(sorted({value for stint in absorbed for value in stint.attendance_ids}))
        if len(ids) < 2:
            continue  # one Odoo row split into spans: nothing to fix in Odoo
        before = tuple(absorbed)
        employee = int(merged.person_odoo_id)
        last = max(absorbed, key=lambda item: item.end_utc)
        is_open = last.is_open and last.end_utc == merged.end_utc
        others_at_station = [
            segment
            for segment in inputs
            if segment.wc_name == merged.wc_name
            and segment.person_odoo_id != merged.person_odoo_id
        ]
        reason = _unsettled_reason(
            merged,
            absorbed,
            others_at_station=others_at_station,
            is_open=is_open,
            production_times_by_wc=production_times_by_wc,
            meter_freshness_by_wc=meter_freshness_by_wc,
            breaks=breaks,
            now_utc=now_utc,
            limit=limit,
            settle_delay=settle_delay,
        )
        if reason is not None:
            skipped.append(SkippedFix(employee, merged.person_name, reason, before))
            continue
        fixes.append(
            PlannedFix(
                employee_odoo_id=employee,
                person_name=merged.person_name,
                wc_name=merged.wc_name,
                start_utc=merged.start_utc,
                end_utc=None if is_open else merged.end_utc,
                source_attendance_ids=ids,
                before=before,
                item_key=item_key_for(employee, ids),
            )
        )
    return FixScan(tuple(fixes), tuple(skipped))


def _unsettled_reason(
    merged: assignment_windows.WorkSegment,
    absorbed: Sequence[SourceStint],
    *,
    others_at_station: Sequence[assignment_windows.WorkSegment],
    is_open: bool,
    production_times_by_wc: Mapping[str, Sequence[datetime]] | None,
    meter_freshness_by_wc: Mapping[str, MeterFreshness],
    breaks: Sequence[tuple[datetime, datetime]],
    now_utc: datetime,
    limit: timedelta,
    settle_delay: timedelta,
) -> str | None:
    """Why a merged stint can't be fixed in Odoo yet, or None when it can."""
    changed_windows = [
        (stint.start_utc, stint.end_utc) for stint in absorbed if stint.wc_name != merged.wc_name
    ] + [
        (left.end_utc, right.start_utc)
        for left, right in zip(absorbed, absorbed[1:])
        if right.start_utc > left.end_utc
    ]
    if any(_overlaps(start, end, breaks) for start, end in changed_windows):
        return "break"
    if _was_relieved(merged.wc_name, absorbed, others_at_station):
        return "relief"
    boundaries = [stint.start_utc for stint in absorbed[1:]] + [
        stint.end_utc for stint in absorbed if stint.end_utc != merged.end_utc
    ]
    if boundaries and now_utc - max(boundaries) < settle_delay:
        return "settling"
    if absorbed[0].wc_name != merged.wc_name:
        final_start = min(s.start_utc for s in absorbed if s.wc_name == merged.wc_name)
        effective_end = now_utc if is_open else merged.end_utc
        if effective_end - final_start <= limit:
            return "first_pick_not_settled"
    for stint in absorbed:
        if stint.wc_name == merged.wc_name:
            continue
        fresh = meter_freshness_by_wc.get(stint.wc_name)
        if (
            fresh is None
            or fresh.truncated
            or fresh.last_reading_at is None
            or fresh.last_reading_at < stint.end_utc
            or production_times_by_wc is None
            or stint.wc_name not in production_times_by_wc
        ):
            return "meter_not_current"
    return None
