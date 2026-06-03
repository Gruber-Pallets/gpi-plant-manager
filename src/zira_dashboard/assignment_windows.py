"""Resolve per-work-center work segments for a day by merging three sources
of "who worked where, when":

  1. The published schedule (full-shift assignments).
  2. Kiosk punch windows (clock_in/transfer_in -> transfer_out/clock_out).
  3. Open-ended retro WC attributions (end_utc may be None = still running).

Hybrid precedence: a person's KIOSK PUNCHES win over both their schedule
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
from typing import Callable


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
) -> list[WorkSegment]:
    """Merge schedule + punches + attributions into closed work segments.

    `attributions`: rows with keys wc_name, person_name, start_utc, end_utc(None ok).
    `punch_windows`: {person_name: [(wc_name, start_utc, end_utc|None), ...]}.
    """
    punched = set(punch_windows)
    raw: dict[str, list[tuple]] = {}

    def _add(person, wc, start, end, source):
        raw.setdefault(person, []).append((wc, start, end, source))

    # 1. Schedule -- only for people WITHOUT punches (punches win).
    for wc, ops in (assignments or {}).items():
        if wc == time_off_key or not ops:
            continue
        for person in ops:
            if person in punched:
                continue
            _add(person, wc, shift_start_utc, None, "schedule")

    # 2. Punches -- authoritative for the people who have them.
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
    productive_minutes: Callable[[str, datetime, datetime], float],
) -> dict[str, float]:
    """Sum prorated expected pallets per WC.

    `productive_minutes(person, start, end)` returns the working minutes in the
    window. Since the June 2026 pace-goal fix the route passes a closure over
    shift_config.productive_minutes_in_window (with the `day` bound), which
    subtracts breaks only -- deliberately NOT effective_minutes_worked, since
    netting out partial time-off would wrongly shrink the pace goal on
    partial-leave days."""
    out: dict[str, float] = {}
    for s in segments:
        thr = target_per_hour.get(s.wc_name, 0.0)
        if thr <= 0:
            continue
        mins = productive_minutes(s.person_name, s.start_utc, s.end_utc)
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
