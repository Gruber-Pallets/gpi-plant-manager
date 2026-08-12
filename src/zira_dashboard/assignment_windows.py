"""Resolve per-work-center work segments for a day by merging three sources
of "who worked where, when":

  1. The published schedule (full-shift assignments).
  2. Odoo attendance work-center windows (tablet sign-ins and transfers).
  3. Open-ended retro WC attributions (end_utc may be None = still running).

Hybrid precedence: a person's ODOO ATTENDANCE WINDOWS win over both their schedule
segment and any manual attribution for that day -- they were physically where
they punched. People with no punches fall back to schedule + attributions.

Every resolved segment carries a CLOSED [start_utc, end_utc] window. Open
inputs (attribution end_utc is None, or a trailing punch with no close yet)
are closed at the start of that person's NEXT segment that day (transfer /
reassignment) or at `cap_utc` = min(now, shift_end). Starts are floored to
shift start; ends capped to `cap_utc`; non-positive segments dropped.

Pure -- no DB, no network. The route supplies already-loaded inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Callable


@dataclass(frozen=True)
class WorkSegment:
    wc_name: str
    person_name: str
    start_utc: datetime
    end_utc: datetime
    source: str  # 'schedule' | 'punch' | 'attribution'


def resolve_segments(
    *,
    assignments: dict[str, list[str]],
    attributions: list[dict],
    punch_windows: dict[str, list[tuple]],
    shift_start_utc: datetime,
    cap_utc: datetime,
    time_off_key: str = "__time_off",
    excluded_people: set[str] | None = None,
) -> list[WorkSegment]:
    """Merge schedule + punches + attributions into closed work segments.

    `attributions`: rows with keys wc_name, person_name, start_utc, end_utc(None ok).
    `punch_windows`: {person_name: [(wc_name, start_utc, end_utc|None), ...]}.
    `excluded_people`: roster names to omit from every source, used by live
    dashboards for full-day absent people without mutating the saved schedule.
    """
    excluded = set(excluded_people or ())
    punched = set(punch_windows)
    raw: dict[str, list[tuple]] = {}

    def _add(person, wc, start, end, source):
        if person in excluded:
            return
        raw.setdefault(person, []).append((wc, start, end, source))

    # 1. Schedule -- only for people WITHOUT punches (punches win).
    for wc, ops in (assignments or {}).items():
        if wc == time_off_key or not ops:
            continue
        for person in ops:
            if person in punched:
                continue
            _add(person, wc, shift_start_utc, None, "schedule")

    # 2. Odoo attendance windows -- authoritative for the people who have them.
    for person, windows in punch_windows.items():
        for (wc, start, end) in windows:
            if not wc:
                continue
            _add(person, wc, start, end, "punch")

    # 3. Attributions -- only for people WITHOUT punches.
    for a in (attributions or []):
        person = a["person_name"]
        if person in punched:
            continue
        _add(person, a["wc_name"], a["start_utc"], a.get("end_utc"), "attribution")

    out: list[WorkSegment] = []
    for person, items in raw.items():
        items.sort(key=lambda x: x[1])

        # A scheduled WC is this person's baseline for the whole shift. A
        # closed manual attribution is a temporary detour from that baseline,
        # not a permanent replacement for it. Preserve the schedule on both
        # sides of the detour so someone who briefly helps at Repair and then
        # returns to their scheduled Dismantler station accrues the correct
        # goal at each WC. (Multiple scheduled WCs for one person are invalid
        # schedule input; retain the established chronological behavior below.)
        scheduled = [item for item in items if item[3] == "schedule"]
        attributed = [item for item in items if item[3] == "attribution"]
        if len(scheduled) == 1 and attributed:
            schedule_wc = scheduled[0][0]
            resolved_attributions: list[WorkSegment] = []
            for i, (wc, start, end, source) in enumerate(attributed):
                eff_start = max(start, shift_start_utc)
                eff_end = end if end is not None else cap_utc
                if i + 1 < len(attributed):
                    eff_end = min(eff_end, attributed[i + 1][1])
                eff_end = min(eff_end, cap_utc)
                if eff_end > eff_start:
                    resolved_attributions.append(
                        WorkSegment(wc, person, eff_start, eff_end, source)
                    )

            schedule_cursor = shift_start_utc
            for attribution in resolved_attributions:
                if schedule_cursor < attribution.start_utc:
                    out.append(WorkSegment(
                        schedule_wc, person, schedule_cursor,
                        attribution.start_utc, "schedule",
                    ))
                out.append(attribution)
                schedule_cursor = max(schedule_cursor, attribution.end_utc)
            if schedule_cursor < cap_utc:
                out.append(WorkSegment(
                    schedule_wc, person, schedule_cursor, cap_utc, "schedule"
                ))
            continue

        for i, (wc, start, end, source) in enumerate(items):
            eff_start = max(start, shift_start_utc)
            eff_end = end if end is not None else cap_utc
            if i + 1 < len(items):
                eff_end = min(eff_end, items[i + 1][1])
            eff_end = min(eff_end, cap_utc)
            if eff_end <= eff_start:
                continue
            out.append(WorkSegment(wc, person, eff_start, eff_end, source))
    return out


def expected_by_wc(
    segments: list[WorkSegment],
    target_per_hour: dict[str, float],
    productive_minutes: Callable[[str, str, datetime, datetime], float],
) -> dict[str, float]:
    """Sum prorated expected pallets per WC.

    `productive_minutes(person, wc_name, start, end)` returns the working
    minutes in the window. Since the June 2026 pace-goal fix the route passes
    a closure over shift_config.productive_minutes_in_window (with the `day`
    bound), which subtracts breaks only -- deliberately NOT
    effective_minutes_worked, since netting out partial time-off would
    wrongly shrink the pace goal on partial-leave days. The July 2026
    breakdown feature added `wc_name` to this signature so the closure can
    also subtract a machine-breakdown exclusion window scoped to this WC."""
    out: dict[str, float] = {}
    for s in segments:
        thr = target_per_hour.get(s.wc_name, 0.0)
        if thr <= 0:
            continue
        mins = productive_minutes(s.person_name, s.wc_name, s.start_utc, s.end_utc)
        if mins <= 0:
            continue
        out[s.wc_name] = out.get(s.wc_name, 0.0) + thr * mins / 60.0
    return out


def who_by_wc(segments: list[WorkSegment]) -> dict[str, str]:
    """{wc_name: 'A + B'} operator labels, deduped, ordered by segment start."""
    order: dict[str, list[str]] = {}
    for s in sorted(segments, key=lambda x: (x.wc_name, x.start_utc)):
        names = order.setdefault(s.wc_name, [])
        if s.person_name not in names:
            names.append(s.person_name)
    return {wc: " + ".join(ns) for wc, ns in order.items()}


def current_who_by_wc(
    segments: list[WorkSegment], *, cap_utc: datetime
) -> dict[str, str]:
    """Live operator labels for work segments still open at ``cap_utc``.

    ``resolve_segments`` closes an otherwise-open current segment at ``cap_utc``
    for pacing math. Earlier transfer segments retain their real end time, so
    selecting only the cap-ending segments keeps a live board from showing a
    person at both the station they left and the station where they now work.
    """
    return who_by_wc([s for s in segments if s.end_utc == cap_utc])


def dashboard_who_by_wc(
    segments: list[WorkSegment], *, cap_utc: datetime, is_live: bool
) -> dict[str, str]:
    """Operator labels appropriate for a live or completed-day dashboard."""
    if is_live:
        return current_who_by_wc(segments, cap_utc=cap_utc)
    return who_by_wc(segments)
