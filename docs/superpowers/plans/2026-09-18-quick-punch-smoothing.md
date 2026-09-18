# Quick-Punch Smoothing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge quick sign-out/sign-in and wrong-station mistakes (5 minutes or less) into one continuous stint before pallets are credited, and fix the display join that let another worker's stint block a lunch join.

**Architecture:**
- A new pure module, `quick_punch_smoothing.py`, applies two per-person rules to `WorkSegment`s: "came back" and "wrong first pick".
- `assignment_windows.work_segments_from_timeline` is the single funnel from Odoo location spans to credited stints. It will build unclipped segments, collect each person's blocking windows (conflicting, unmapped, or stale locations), smooth, then clip.
- Separately, `production_segments.coalesce_display_scores` will join a stint with the same person's latest stint at that station instead of only the immediately preceding one.

**Tech Stack:** Python 3.12, pytest, ruff. Run tests with `ZIRA_API_KEY=test .venv/bin/python -m pytest`.

**Spec:** `docs/superpowers/specs/2026-09-18-quick-punch-smoothing-design.md`

**Safety:** Never run pytest with `DATABASE_URL` pointing at production. The conftest guard aborts on Railway hosts. Leave `DATABASE_URL` unset so DB-gated tests skip. The only production contact in this plan is the read-only `SELECT` check in Task 4.

---

## File Structure

- **Create** `src/zira_dashboard/quick_punch_smoothing.py`: pure smoothing rules. No DB, network, or clock. It imports `WorkSegment` only under `TYPE_CHECKING`, which avoids a circular import with `assignment_windows`.
- **Create** `tests/test_quick_punch_smoothing.py`: unit tests for the rules.
- **Modify** `src/zira_dashboard/assignment_windows.py` (`work_segments_from_timeline`, currently lines 42-71): smooth before clipping.
- **Modify** `tests/test_production_history_odoo_strict.py`: integration tests next to the existing `test_work_segments_from_timeline_keeps_only_clipped_positive_valid_spans`, which ends around line 175.
- **Modify** `src/zira_dashboard/production_segments.py` (`coalesce_display_scores`, around lines 256-270): per-person join.
- **Modify** `tests/test_production_segments.py`: display-join regression test after `test_display_scores_keep_productive_gap_and_lunch_transfer_split`, which ends around line 127.
- **Modify** `CHANGELOG.md`: a plain-language What's New entry.

---

### Task 1: Pure smoothing module

**Files:**
- Create: `src/zira_dashboard/quick_punch_smoothing.py`
- Test: `tests/test_quick_punch_smoothing.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_quick_punch_smoothing.py`:

```python
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from zira_dashboard.assignment_windows import WorkSegment
from zira_dashboard.quick_punch_smoothing import QUICK_PUNCH_LIMIT, smooth_quick_punches

CT = ZoneInfo("America/Chicago")


def ct(hour, minute=0, second=0):
    return datetime(2026, 9, 18, hour, minute, second, tzinfo=CT).astimezone(UTC)


def seg(wc, start, end, *, person="Christian C.", odoo_id=8):
    return WorkSegment(wc, person, start, end, "odoo", odoo_id)


def shape(segments):
    return [
        (segment.person_name, segment.wc_name, segment.start_utc, segment.end_utc)
        for segment in segments
    ]


def test_limit_is_five_minutes():
    assert QUICK_PUNCH_LIMIT == timedelta(minutes=5)


def test_empty_input_stays_empty():
    assert smooth_quick_punches(()) == ()


def test_christians_detour_on_2026_09_18_becomes_one_dismantler_3_stint():
    now = ct(14, 25, 53)
    segments = (
        seg("Dismantler 3", ct(7), ct(7, 2, 10)),
        seg("Dismantler 2", ct(7, 2, 10), ct(7, 4, 27)),
        seg("Dismantler 3", ct(7, 4, 27), ct(11)),
        seg("Dismantler 3", ct(11, 30), now),
    )

    assert shape(smooth_quick_punches(segments)) == [
        ("Christian C.", "Dismantler 3", ct(7), ct(11)),
        ("Christian C.", "Dismantler 3", ct(11, 30), now),
    ]


def test_lunch_split_is_not_bridged():
    segments = (
        seg("Dismantler 2", ct(7), ct(11), person="Jose C.", odoo_id=24),
        seg("Dismantler 2", ct(11, 30), ct(14, 25), person="Jose C.", odoo_id=24),
    )

    assert smooth_quick_punches(segments) == segments


def test_same_station_gap_is_bridged_up_to_exactly_the_limit():
    for gap_seconds, expected_count in ((299, 1), (300, 1), (301, 2)):
        back_at = ct(9) + timedelta(seconds=gap_seconds)
        result = smooth_quick_punches(
            (seg("Repair 1", ct(8), ct(9)), seg("Repair 1", back_at, ct(10)))
        )
        assert len(result) == expected_count, gap_seconds

    (bridged,) = smooth_quick_punches(
        (seg("Repair 1", ct(8), ct(9)), seg("Repair 1", ct(9, 5), ct(10)))
    )
    assert (bridged.start_utc, bridged.end_utc) == (ct(8), ct(10))


def test_detour_through_several_stations_inside_the_limit_is_absorbed():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 2", ct(9), ct(9, 1)),
        seg("Repair 3", ct(9, 1), ct(9, 3)),
        seg("Repair 1", ct(9, 4), ct(10)),
    )

    assert shape(smooth_quick_punches(segments)) == [
        ("Christian C.", "Repair 1", ct(8), ct(10)),
    ]


def test_detour_longer_than_the_limit_is_kept():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 2", ct(9), ct(9, 6)),
        seg("Repair 1", ct(9, 6), ct(10)),
    )

    assert smooth_quick_punches(segments) == segments


def test_wrong_first_pick_at_start_of_day_joins_the_next_station():
    segments = (
        seg("Dismantler 1", ct(7), ct(7, 3)),
        seg("Dismantler 4", ct(7, 3), ct(11)),
    )

    assert shape(smooth_quick_punches(segments)) == [
        ("Christian C.", "Dismantler 4", ct(7), ct(11)),
    ]


def test_wrong_first_pick_after_lunch_joins_the_next_station_and_fills_the_gap():
    segments = (
        seg("Dismantler 4", ct(7), ct(11)),
        seg("Dismantler 1", ct(11, 30), ct(11, 32)),
        seg("Dismantler 4", ct(11, 34), ct(15, 30)),
    )

    assert shape(smooth_quick_punches(segments)) == [
        ("Christian C.", "Dismantler 4", ct(7), ct(11)),
        ("Christian C.", "Dismantler 4", ct(11, 30), ct(15, 30)),
    ]


def test_chain_of_first_picks_settles_on_the_final_station_within_the_limit():
    segments = (
        seg("Repair 1", ct(7), ct(7, 1)),
        seg("Repair 2", ct(7, 1), ct(7, 3)),
        seg("Repair 3", ct(7, 3), ct(11)),
    )

    assert shape(smooth_quick_punches(segments)) == [
        ("Christian C.", "Repair 3", ct(7), ct(11)),
    ]


def test_chain_of_first_picks_stops_once_the_combined_stint_passes_the_limit():
    segments = (
        seg("Repair 1", ct(7), ct(7, 3)),
        seg("Repair 2", ct(7, 3), ct(7, 6)),
        seg("Repair 3", ct(7, 6), ct(11)),
    )

    assert shape(smooth_quick_punches(segments)) == [
        ("Christian C.", "Repair 2", ct(7), ct(7, 6)),
        ("Christian C.", "Repair 3", ct(7, 6), ct(11)),
    ]


def test_mid_day_short_stop_between_two_different_stations_is_kept():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 2", ct(9), ct(9, 2)),
        seg("Repair 3", ct(9, 2), ct(10)),
    )

    assert smooth_quick_punches(segments) == segments


def test_came_back_rule_runs_before_wrong_first_pick():
    # Run alone, wrong-first-pick would move 07:00-07:02 onto Dismantler 2.
    segments = (
        seg("Dismantler 3", ct(7), ct(7, 2)),
        seg("Dismantler 2", ct(7, 2), ct(7, 4)),
        seg("Dismantler 3", ct(7, 4), ct(9)),
    )

    assert shape(smooth_quick_punches(segments)) == [
        ("Christian C.", "Dismantler 3", ct(7), ct(9)),
    ]


def test_live_detour_waits_until_the_person_comes_back():
    at_0703 = (
        seg("Dismantler 3", ct(7), ct(7, 2, 10)),
        seg("Dismantler 2", ct(7, 2, 10), ct(7, 3)),
    )
    assert shape(smooth_quick_punches(at_0703)) == [
        ("Christian C.", "Dismantler 2", ct(7), ct(7, 3)),
    ]

    mid_detour_after_real_work = (
        seg("Dismantler 3", ct(7), ct(9)),
        seg("Dismantler 2", ct(9), ct(9, 2)),
    )
    assert smooth_quick_punches(mid_detour_after_real_work) == mid_detour_after_real_work


def test_other_people_are_never_changed_and_order_is_preserved():
    bob = seg("Repair 3", ct(7), ct(15), person="Bob T.", odoo_id=6)
    christian_before = seg("Repair 1", ct(8), ct(9))
    ana = seg("Repair 2", ct(9), ct(9, 2), person="Ana M.", odoo_id=5)
    christian_after = seg("Repair 1", ct(9, 2), ct(10))

    result = smooth_quick_punches((bob, christian_before, ana, christian_after))

    assert len(result) == 3
    assert result[0] is bob
    assert shape(result[1:2]) == [("Christian C.", "Repair 1", ct(8), ct(10))]
    assert result[2] is ana


def test_same_name_with_different_odoo_ids_stays_separate():
    segments = (
        seg("Repair 1", ct(8), ct(9), person="Jose O.", odoo_id=31),
        seg("Repair 1", ct(9, 1), ct(10), person="Jose O.", odoo_id=32),
    )

    assert smooth_quick_punches(segments) == segments


def test_blocked_windows_stop_both_rules():
    came_back = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 1", ct(9, 3), ct(10)),
    )
    assert (
        smooth_quick_punches(came_back, blocked_windows={8: ((ct(9, 1), ct(9, 2)),)})
        == came_back
    )

    first_pick = (
        seg("Repair 1", ct(7), ct(7, 2)),
        seg("Repair 2", ct(7, 3), ct(11)),
    )
    assert (
        smooth_quick_punches(first_pick, blocked_windows={8: ((ct(7, 2), ct(7, 3)),)})
        == first_pick
    )


def test_blocked_window_that_only_touches_the_gap_does_not_block():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 1", ct(9, 3), ct(10)),
    )

    (bridged,) = smooth_quick_punches(
        segments, blocked_windows={8: ((ct(8, 50), ct(9)),)}
    )
    assert (bridged.start_utc, bridged.end_utc) == (ct(8), ct(10))


def test_detour_that_runs_past_the_return_is_left_alone():
    segments = (
        seg("Repair 1", ct(8), ct(9)),
        seg("Repair 2", ct(9), ct(11)),
        seg("Repair 1", ct(9, 2), ct(10)),
    )

    assert smooth_quick_punches(segments) == segments
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_quick_punch_smoothing.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'zira_dashboard.quick_punch_smoothing'`.

- [ ] **Step 3: Write the implementation**

Create `src/zira_dashboard/quick_punch_smoothing.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_quick_punch_smoothing.py -q`
Expected: all 19 tests pass. (Review follow-up `6d37f59d` added the overlap guard shown above plus edge, rule-order and key tests; the file now has 29 tests.)

- [ ] **Step 5: Lint**

Run: `.venv/bin/ruff check src/zira_dashboard/quick_punch_smoothing.py tests/test_quick_punch_smoothing.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/quick_punch_smoothing.py tests/test_quick_punch_smoothing.py
git commit -m "feat: add quick-punch smoothing rules

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Smooth inside the span-to-stint funnel

**Files:**
- Modify: `src/zira_dashboard/assignment_windows.py` (imports near the top; `work_segments_from_timeline` at lines 42-71)
- Test: `tests/test_production_history_odoo_strict.py` (add after `test_work_segments_from_timeline_keeps_only_clipped_positive_valid_spans`)

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_production_history_odoo_strict.py`, directly after `test_work_segments_from_timeline_keeps_only_clipped_positive_valid_spans`. The file already defines `START` (12:00 UTC), `END` (20:00 UTC), `at(hour, minute)`, `span(...)` with default `wc="Repair 4"`, and the `work_segments_from_timeline` wrapper.

```python
def _segment_shape(segments):
    return [
        (segment.person_odoo_id, segment.wc_name, segment.start_utc, segment.end_utc)
        for segment in segments
    ]


def test_work_segments_from_timeline_smooths_quick_punches_before_clipping():
    spans = (
        # Long before the window, only 2 minutes inside it: not a wrong first pick.
        span(1, "Ana", at(11), at(12, 2), wc="Repair 1"),
        span(1, "Ana", at(12, 2), at(14)),
        # Signed out 3 minutes and came back to the same station.
        span(2, "Bob", at(13), at(14)),
        span(2, "Bob", at(14, 3), at(15)),
    )

    segments = work_segments_from_timeline(spans, window_start_utc=START, window_end_utc=END)

    assert _segment_shape(segments) == [
        (1, "Repair 1", START, at(12, 2)),
        (1, "Repair 4", at(12, 2), at(14)),
        (2, "Repair 4", at(13), at(15)),
    ]


def test_work_segments_from_timeline_never_bridges_a_location_conflict():
    spans = (
        span(1, "Ana", at(13), at(14)),
        span(1, "Ana", at(14), at(14, 2), status="conflicting_location", wc=None),
        span(1, "Ana", at(14, 2), at(15)),
        span(2, "Bob", at(13), at(14)),
        span(2, "Bob", at(14), at(14, 2), status="missing_required_location", wc=None),
        span(2, "Bob", at(14, 2), at(15)),
    )

    segments = work_segments_from_timeline(spans, window_start_utc=START, window_end_utc=END)

    assert _segment_shape(segments) == [
        (1, "Repair 4", at(13), at(14)),
        (1, "Repair 4", at(14, 2), at(15)),
        (2, "Repair 4", at(13), at(15)),
    ]


def test_samples_in_a_smoothed_gap_are_credited_to_the_person():
    from zira_dashboard.production_segments import credit_work_segments

    spans = (
        span(1, "Ana", at(13), at(14)),
        span(1, "Ana", at(14, 3), at(15)),
    )
    segments = work_segments_from_timeline(spans, window_start_utc=START, window_end_utc=END)

    credits = credit_work_segments(
        segments,
        wc_totals={"Repair 4": 30},
        samples_by_wc={"Repair 4": [(at(13, 30), 10), (at(14, 1), 8), (at(14, 30), 12)]},
        productive_minutes=lambda *_args: 60,
        allow_total_fallback=False,
    )["Repair 4"]

    named = sum(row.actual_units for row in credits if row.person_name == "Ana")
    unassigned = sum(row.actual_units for row in credits if row.person_name is None)
    assert (named, unassigned) == (30.0, 0.0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_production_history_odoo_strict.py -q -k "smooths_quick_punches or never_bridges or smoothed_gap"`
Expected: 3 FAILED. The smoothing test sees Bob as two segments, the conflict test sees Bob as two segments, and the credit test gets `(22.0, 8.0)`.

- [ ] **Step 3: Implement**

In `src/zira_dashboard/assignment_windows.py`, change the imports near the top from:

```python
from dataclasses import dataclass
from datetime import datetime
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .attendance_timeline import LocationSpan
```

to:

```python
from dataclasses import dataclass, replace
from datetime import datetime
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from . import quick_punch_smoothing

if TYPE_CHECKING:
    from .attendance_timeline import LocationSpan


# Location states where Odoo's data conflicts or is unknown. Smoothing never
# bridges across these, so pallet credit is never invented there.
_SMOOTHING_BLOCKING_STATUSES = frozenset(
    {"conflicting_location", "unmapped_location", "stale_open_location"}
)
```

Replace the whole body of `work_segments_from_timeline` (from the `segments: list[WorkSegment] = []` line through `return tuple(segments)`) with:

```python
    raw: list[WorkSegment] = []
    blocked: dict[int, list[tuple[datetime, datetime]]] = {}
    for span in spans:
        if span.status in _SMOOTHING_BLOCKING_STATUSES:
            blocked.setdefault(span.employee_odoo_id, []).append(
                (span.start_utc, span.end_utc)
            )
        if span.status != "valid" or not span.app_work_center_name:
            continue
        raw.append(
            WorkSegment(
                wc_name=span.app_work_center_name,
                person_name=span.employee_name,
                start_utc=span.start_utc,
                end_utc=span.end_utc,
                source="odoo",
                person_odoo_id=span.employee_odoo_id,
            )
        )
    segments: list[WorkSegment] = []
    for segment in quick_punch_smoothing.smooth_quick_punches(raw, blocked_windows=blocked):
        start = max(segment.start_utc, window_start_utc)
        end = min(segment.end_utc, window_end_utc)
        if end <= start:
            continue
        segments.append(replace(segment, start_utc=start, end_utc=end))
    return tuple(segments)
```

Keep the two validation checks at the top of the function (timezone-aware bounds, positive window) exactly as they are. Update the docstring to:

```python
    """Convert valid Odoo location spans into smoothed, clipped work segments.

    Quick punch mistakes are merged per person (see ``quick_punch_smoothing``)
    before clipping, never across a conflicting, unmapped or stale location.
    """
```

- [ ] **Step 4: Run the new and neighbouring tests**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_production_history_odoo_strict.py tests/test_quick_punch_smoothing.py tests/test_attendance_location_end_to_end.py tests/test_current_operator_surfaces.py tests/test_department_operator_labels.py tests/test_production_history.py tests/test_production_segments.py -q`
Expected: all pass (DB-gated tests skip). If an existing test fails, read its spans. A failure means it encodes a sub-5-minute blip for one person. Stop and report it rather than editing the assertion.

- [ ] **Step 5: Lint**

Run: `.venv/bin/ruff check src/zira_dashboard/assignment_windows.py tests/test_production_history_odoo_strict.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/assignment_windows.py tests/test_production_history_odoo_strict.py
git commit -m "feat: smooth quick punches before crediting stints

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Display join looks for the same person's latest stint

**Files:**
- Modify: `src/zira_dashboard/production_segments.py` (`coalesce_display_scores`, around lines 256-270)
- Test: `tests/test_production_segments.py` (add after `test_display_scores_keep_productive_gap_and_lunch_transfer_split`)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_production_segments.py` after `test_display_scores_keep_productive_gap_and_lunch_transfer_split`. The file's `t(hour, minute)` returns 2026-08-20 UTC, and `_score(...)` builds a `SegmentScore`.

```python
def test_display_scores_join_worker_across_lunch_when_another_worker_visited_between():
    # 2026-09-18 Dismantler 2: Christian's 2-minute visit sat between Jose's
    # morning and afternoon stints and blocked the lunch join.
    jose_morning = _score(
        "Jose C.", t(12), t(16), actual=100, goal=90, wc="Dismantler 2", segment_id=0
    )
    christian = _score(
        "Christian C.", t(12, 2), t(12, 4), actual=0, goal=1, wc="Dismantler 2", segment_id=1
    )
    jose_afternoon = _score(
        "Jose C.",
        t(16, 30),
        t(19),
        actual=50,
        goal=70,
        active=True,
        wc="Dismantler 2",
        segment_id=2,
    )

    rows = coalesce_display_scores(
        (jose_morning, christian, jose_afternoon),
        ignored_gaps=((t(16), t(16, 30)),),
    )

    assert [(row.person_name, row.start_utc, row.end_utc) for row in rows] == [
        ("Jose C.", t(12), t(19)),
        ("Christian C.", t(12, 2), t(12, 4)),
    ]
    assert (rows[0].actual_units, rows[0].goal_units, rows[0].is_active) == (150, 160, True)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_production_segments.py -q -k another_worker_visited_between`
Expected: FAIL. Three rows come back, and Jose is still split.

- [ ] **Step 3: Implement**

In `src/zira_dashboard/production_segments.py`, inside `coalesce_display_scores`, replace:

```python
    merged: list[SegmentScore] = []
    for score in named:
        if merged and _can_join_display_scores(merged[-1], score, ignored_gaps):
            merged[-1] = _join_display_scores(merged[-1], score)
        else:
            merged.append(score)
```

with:

```python
    merged: list[SegmentScore] = []
    latest_by_person: dict[tuple[str | None, int | None], int] = {}
    for score in named:
        key = (score.person_name, score.person_odoo_id)
        latest = latest_by_person.get(key)
        if latest is not None and _can_join_display_scores(merged[latest], score, ignored_gaps):
            merged[latest] = _join_display_scores(merged[latest], score)
        else:
            latest_by_person[key] = len(merged)
            merged.append(score)
```

Leave the docstring, the `unassigned` handling and the final `sorted(...)` return unchanged.

- [ ] **Step 4: Run the display tests**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_production_segments.py tests/test_department_operator_labels.py -q`
Expected: all pass.

- [ ] **Step 5: Lint**

Run: `.venv/bin/ruff check src/zira_dashboard/production_segments.py tests/test_production_segments.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/production_segments.py tests/test_production_segments.py
git commit -m "fix: join a worker's lunch split past other workers' stints

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Patch notes, full validation, real-data check, push

**Files:**
- Modify: `CHANGELOG.md` (insert above `## 2026-09-15`)

- [ ] **Step 1: Add the What's New entry**

Insert directly above the `## 2026-09-15` line in `CHANGELOG.md`:

```markdown
## 2026-09-18

### Quick sign-in mix-ups smoothed out

#### Features

- **Tapping the wrong station, or signing out and back in by mistake, no longer chops up the dashboard bars.** If someone fixes it within 5 minutes, the bars show one steady stretch of work, and the pallets made in those minutes still count for them.

#### Fixes

- **A worker's bar no longer splits at lunch just because someone else stopped by their station that day.**

```

- [ ] **Step 2: Run the full suite and lint**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: no failures. DB-gated tests skip without `DATABASE_URL`. Compare against the pre-change run if anything unrelated fails, and report pre-existing reds separately.

Run: `.venv/bin/ruff check src tests scripts`
Expected: `All checks passed!`

- [ ] **Step 3: Check today's real stints (read-only, no pytest)**

This runs app code against the production database inside a read-only transaction. It never runs tests.

```bash
DB_PUB=$(~/.local/bin/railway variables --service Postgres-rAoP --kv 2>/dev/null | grep '^DATABASE_PUBLIC_URL=' | cut -d= -f2-)
case "$DB_PUB" in *\?*) RO="$DB_PUB&options=-c%20default_transaction_read_only%3Don";; *) RO="$DB_PUB?options=-c%20default_transaction_read_only%3Don";; esac
DATABASE_URL="$RO" ZIRA_API_KEY=test .venv/bin/python - <<'EOF'
from datetime import UTC, date, datetime, time
from zira_dashboard import shift_config
from zira_dashboard.routes import departments
d = date(2026, 9, 18)
ws = datetime.combine(d, time(7), tzinfo=shift_config.SITE_TZ).astimezone(UTC)
we = datetime.combine(d, time(15, 30), tzinfo=shift_config.SITE_TZ).astimezone(UTC)
proj = departments._canonical_department_segments(d, ws, we, now_utc=datetime.now(UTC))
for s in proj.segments:
    if s.person_name.startswith(("Jose C", "Christian C")):
        print(s.person_name, s.wc_name, s.start_utc.astimezone(shift_config.SITE_TZ).time(),
              s.end_utc.astimezone(shift_config.SITE_TZ).time())
EOF
```

Expected:
- Christian C.: Dismantler 3 07:00:00–11:00:00, and Dismantler 3 11:30:00–(now or 15:30).
- Jose C.: Dismantler 2 07:00:00–11:00:00, and Dismantler 2 11:30:00–(now or 15:30).
- No Dismantler 2 row for Christian.

- [ ] **Step 4: Commit and push**

```bash
git add CHANGELOG.md
git commit -m "docs: explain quick-punch smoothing

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push origin main
```

- [ ] **Step 5: Confirm the deploy**

Run: `~/.local/bin/railway deployment list --service web | head -5`, then `curl -s -o /dev/null -w '%{http_code}\n' https://gpiplantmanager.com/healthz`
Expected: the newest deployment is SUCCESS for the pushed commit, and healthz returns `200`.
