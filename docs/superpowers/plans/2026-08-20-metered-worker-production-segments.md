# Metered Worker Production Segments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show independently scored worker production segments inside every single-day metered work-center bar while keeping the current worker on the left.

**Architecture:** Add a pure segment-credit/scoring module that shares timestamped Zira credit with production history. Thread its single-day score output through the existing department range aggregate and bar builder, then render continuous horizontal and vertical worker runways in the shared dashboard macro. Existing aggregate bars remain the fallback for multi-day ranges and source failures.

**Tech Stack:** Python 3.12, dataclasses, FastAPI route data preparation, Jinja2 templates, CSS, pytest, Ruff.

## Global Constraints

- Apply this to every metered work center on both Recycling and New department dashboards, including TV variants.
- Show worker segments only for single-day views; multi-day range bars must remain unchanged.
- Score every worker independently from their own start time. A later worker never inherits an earlier shortfall.
- Split each timestamped meter reading equally among distinct workers active at that work center at that instant.
- Use half-open windows: `start_utc <= sample_time < end_utc`; a transfer-boundary reading belongs to the incoming worker.
- Preserve the existing breaks-only goal calculation and approved machine-breakdown exclusions; do not shrink goals again for partial time off.
- Preserve scheduler fallback, total pallet counts, production-history totals, inline attribution, sorting, number placement, widget customization, screen/TV behavior, and operator links.
- Keep full names, times, and `ahead`/`behind` wording visibly available without hover; color cannot be the only status cue.
- Use the existing dependency set; add no package.
- Add the user-facing `CHANGELOG.md` note in Task 6, using short sentences and common words that a 10-year-old can understand.
- Commit only task-scoped files and push each task commit to `origin/main`.

---

## File Map

- Create `src/zira_dashboard/production_segments.py`: pure sample credit, productive-minute credit, unassigned credit, and per-worker score calculation.
- Create `tests/test_production_segments.py`: exhaustive pure tests for transfers, overlap, boundaries, fallback totals, unassigned output, and result states.
- Modify `src/zira_dashboard/production_history.py`: aggregate the shared credit output without changing its public result shape.
- Modify `tests/test_production_history.py`: prove shared credit preserves historical totals, hours, downtime sharing, and returning-worker behavior.
- Modify `src/zira_dashboard/routes/departments.py`: score active station segments, format display labels, and expose single-day live state.
- Modify `src/zira_dashboard/recycling_range.py`: retain segment detail only for a single day.
- Modify `tests/test_department_operator_labels.py`: prove Humberto remains on Repair 4 after transferring and the incoming worker starts fresh.
- Modify `tests/test_recycling_range.py`: prove one-day segment pass-through and multi-day suppression.
- Modify `src/zira_dashboard/recycling_data.py`: compute continuous runway geometry and current/no-current left-side state.
- Modify `tests/test_recycling_data.py`: pin geometry, scaling, range fallback, and no-current wording flags.
- Modify `src/zira_dashboard/templates/_department_dashboard_widgets.html`: shared horizontal and vertical segment markup.
- Modify `src/zira_dashboard/static/recycling.css`: fills, shortfall hatching, live/finished goal lines, labels, vertical lists, TV-safe text, and responsive fallback.
- Modify `tests/test_new_dashboard_template.py`: render the shared macro through the New dashboard for horizontal, vertical, narrow-label, and legacy-range cases.
- Modify `tests/test_dashboards_polish.py`: full Recycling render regression for current and transferred worker wording.
- Modify `CHANGELOG.md`: user-facing release note.

---

### Task 1: Pure Worker Segment Credit and Scoring

**Files:**
- Create: `src/zira_dashboard/production_segments.py`
- Create: `tests/test_production_segments.py`

**Interfaces:**
- Consumes: `assignment_windows.WorkSegment` and a caller-supplied productive-minutes callback.
- Produces: `credit_work_segments(...) -> dict[str, tuple[SegmentCredit, ...]]` and `score_work_segments(...) -> dict[str, tuple[SegmentScore, ...]]`.

- [ ] **Step 1: Write failing transfer and independent-goal tests**

Create `tests/test_production_segments.py` with the core example and exact interface:

```python
from datetime import datetime, timezone

from zira_dashboard.assignment_windows import WorkSegment
from zira_dashboard.production_segments import (
    credit_work_segments,
    score_work_segments,
)

UTC = timezone.utc


def t(hour, minute=0):
    return datetime(2026, 8, 20, hour, minute, tzinfo=UTC)


def test_transfer_segments_keep_independent_actual_goal_and_runway():
    segments = [
        WorkSegment("Repair 4", "Humberto S.", t(12), t(19, 33), "punch"),
        WorkSegment("Repair 4", "Ana M.", t(19, 35), t(19, 50), "punch"),
    ]
    minutes = {"Humberto S.": 420.0, "Ana M.": 15.0}

    credits = credit_work_segments(
        segments,
        wc_totals={"Repair 4": 548.0},
        samples_by_wc={
            "Repair 4": [
                (t(18), 516),
                (t(19, 40), 32),
            ]
        },
        productive_minutes=lambda person, _wc, _start, _end: minutes[person],
        live_cap_utc=t(19, 50),
    )
    scored = score_work_segments(credits, target_per_hour={"Repair 4": 100.0})

    humberto, ana = scored["Repair 4"]
    assert (humberto.actual_units, humberto.goal_units) == (516.0, 700.0)
    assert (humberto.result, humberto.runway_units, humberto.is_active) == (
        "behind", 700.0, False,
    )
    assert (ana.actual_units, ana.goal_units) == (32.0, 25.0)
    assert (ana.result, ana.runway_units, ana.is_active) == (
        "ahead", 32.0, True,
    )
```

- [ ] **Step 2: Run the core test and confirm the module is missing**

Run:

```bash
.venv/bin/pytest tests/test_production_segments.py::test_transfer_segments_keep_independent_actual_goal_and_runway -v
```

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'zira_dashboard.production_segments'`.

- [ ] **Step 3: Implement the immutable models and core credit/scoring path**

Create `src/zira_dashboard/production_segments.py` with these public types and functions:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Callable, Mapping, Sequence
from typing import Literal

from .assignment_windows import WorkSegment

SegmentResult = Literal["ahead", "behind", "neutral"]


@dataclass(frozen=True)
class SegmentCredit:
    segment_id: int
    wc_name: str
    person_name: str | None
    start_utc: datetime | None
    end_utc: datetime | None
    source: str
    productive_minutes: float
    actual_units: float
    is_active: bool


@dataclass(frozen=True)
class SegmentScore:
    segment_id: int
    wc_name: str
    person_name: str | None
    start_utc: datetime | None
    end_utc: datetime | None
    source: str
    productive_minutes: float
    actual_units: float
    goal_units: float
    runway_units: float
    is_active: bool
    result: SegmentResult


def _ordered_segments(segments: Sequence[WorkSegment]) -> list[WorkSegment]:
    return sorted(
        segments,
        key=lambda segment: (
            segment.start_utc.timestamp(),
            segment.end_utc.timestamp(),
            segment.wc_name,
            segment.person_name,
        ),
    )


def credit_work_segments(
    segments: Sequence[WorkSegment],
    *,
    wc_totals: Mapping[str, float],
    samples_by_wc: Mapping[str, Sequence[tuple[datetime, float]]],
    productive_minutes: Callable[[str, str, datetime, datetime], float],
    live_cap_utc: datetime | None = None,
) -> dict[str, tuple[SegmentCredit, ...]]:
    ordered = _ordered_segments(segments)
    rows: list[dict] = []
    indices_by_wc: dict[str, list[int]] = {}
    for segment_id, segment in enumerate(ordered):
        minutes = max(
            0.0,
            float(productive_minutes(
                segment.person_name,
                segment.wc_name,
                segment.start_utc,
                segment.end_utc,
            )),
        )
        row = {
            "segment_id": segment_id,
            "wc_name": segment.wc_name,
            "person_name": segment.person_name,
            "start_utc": segment.start_utc,
            "end_utc": segment.end_utc,
            "source": segment.source,
            "productive_minutes": minutes,
            "actual_units": 0.0,
            "is_active": live_cap_utc is not None and segment.end_utc == live_cap_utc,
        }
        indices_by_wc.setdefault(segment.wc_name, []).append(len(rows))
        rows.append(row)

    sampled_units: dict[str, float] = {}
    unassigned: dict[str, dict] = {}
    for wc_name, samples in samples_by_wc.items():
        wc_indices = indices_by_wc.get(wc_name, [])
        for timestamp, raw_units in sorted(samples, key=lambda item: item[0]):
            units = float(raw_units or 0)
            if units <= 0:
                continue
            sampled_units[wc_name] = sampled_units.get(wc_name, 0.0) + units
            active_by_person: dict[str, int] = {}
            for index in wc_indices:
                row = rows[index]
                if row["start_utc"] <= timestamp < row["end_utc"]:
                    active_by_person.setdefault(row["person_name"], index)
            if active_by_person:
                share = units / len(active_by_person)
                for index in active_by_person.values():
                    rows[index]["actual_units"] += share
                continue
            bucket = unassigned.setdefault(
                wc_name,
                {
                    "actual_units": 0.0,
                    "start_utc": timestamp,
                    "end_utc": timestamp,
                },
            )
            bucket["actual_units"] += units
            bucket["start_utc"] = min(bucket["start_utc"], timestamp)
            bucket["end_utc"] = max(bucket["end_utc"], timestamp)

    for wc_name, raw_total in wc_totals.items():
        remaining = max(0.0, float(raw_total or 0) - sampled_units.get(wc_name, 0.0))
        if remaining <= 0:
            continue
        eligible = [
            index for index in indices_by_wc.get(wc_name, [])
            if rows[index]["productive_minutes"] > 0
        ]
        total_minutes = sum(rows[index]["productive_minutes"] for index in eligible)
        if total_minutes > 0:
            for index in eligible:
                rows[index]["actual_units"] += (
                    remaining * rows[index]["productive_minutes"] / total_minutes
                )
        else:
            bucket = unassigned.setdefault(
                wc_name,
                {"actual_units": 0.0, "start_utc": None, "end_utc": None},
            )
            bucket["actual_units"] += remaining

    for wc_name, bucket in unassigned.items():
        rows.append({
            "segment_id": len(rows),
            "wc_name": wc_name,
            "person_name": None,
            "start_utc": bucket["start_utc"],
            "end_utc": bucket["end_utc"],
            "source": "unassigned",
            "productive_minutes": 0.0,
            "actual_units": bucket["actual_units"],
            "is_active": False,
        })

    by_wc: dict[str, list[SegmentCredit]] = {}
    for row in rows:
        credit = SegmentCredit(**row)
        by_wc.setdefault(credit.wc_name, []).append(credit)
    for wc_rows in by_wc.values():
        wc_rows.sort(key=lambda row: (
            row.start_utc is None,
            row.start_utc.timestamp() if row.start_utc is not None else float("inf"),
            row.segment_id,
        ))
    return {wc_name: tuple(wc_rows) for wc_name, wc_rows in by_wc.items()}


def score_work_segments(
    credits_by_wc: Mapping[str, Sequence[SegmentCredit]],
    *,
    target_per_hour: Mapping[str, float],
) -> dict[str, tuple[SegmentScore, ...]]:
    out: dict[str, tuple[SegmentScore, ...]] = {}
    for wc_name, credits in credits_by_wc.items():
        target = max(0.0, float(target_per_hour.get(wc_name, 0.0) or 0.0))
        scored = []
        for credit in credits:
            goal = (
                target * credit.productive_minutes / 60.0
                if credit.person_name is not None and target > 0
                else 0.0
            )
            if credit.person_name is None or goal <= 0:
                result: SegmentResult = "neutral"
            elif credit.actual_units >= goal:
                result = "ahead"
            else:
                result = "behind"
            scored.append(SegmentScore(
                **credit.__dict__,
                goal_units=goal,
                runway_units=max(credit.actual_units, goal),
                result=result,
            ))
        out[wc_name] = tuple(scored)
    return out
```

- [ ] **Step 4: Run the transfer test and confirm it passes**

Run:

```bash
.venv/bin/pytest tests/test_production_segments.py::test_transfer_segments_keep_independent_actual_goal_and_runway -v
```

Expected: PASS.

- [ ] **Step 5: Add boundary, overlap, returning-worker, unassigned, fallback, and neutral tests**

Append these cases to `tests/test_production_segments.py`:

```python
def test_transfer_boundary_and_overlap_credit_each_sample_once():
    segments = [
        WorkSegment("Hand Build #1", "A", t(12), t(13), "punch"),
        WorkSegment("Hand Build #1", "B", t(12), t(14), "punch"),
        WorkSegment("Hand Build #1", "C", t(13), t(14), "punch"),
    ]
    credits = credit_work_segments(
        segments,
        wc_totals={"Hand Build #1": 60},
        samples_by_wc={"Hand Build #1": [(t(12, 30), 20), (t(13), 40)]},
        productive_minutes=lambda _person, _wc, start, end: (
            end - start
        ).total_seconds() / 60,
    )["Hand Build #1"]
    assert [(row.person_name, row.actual_units) for row in credits] == [
        ("A", 10.0),
        ("B", 30.0),
        ("C", 20.0),
    ]


def test_same_worker_returning_keeps_two_segments():
    segments = [
        WorkSegment("Repair 4", "Humberto S.", t(12), t(13), "punch"),
        WorkSegment("Repair 4", "Humberto S.", t(14), t(15), "punch"),
    ]
    credits = credit_work_segments(
        segments,
        wc_totals={"Repair 4": 30},
        samples_by_wc={"Repair 4": [(t(12, 30), 10), (t(14, 30), 20)]},
        productive_minutes=lambda *_args: 60,
    )["Repair 4"]
    assert [(row.start_utc, row.actual_units) for row in credits] == [
        (t(12), 10.0),
        (t(14), 20.0),
    ]


def test_unassigned_and_total_without_samples_are_never_dropped():
    credits = credit_work_segments(
        [],
        wc_totals={"Repair 4": 50},
        samples_by_wc={"Repair 4": [(t(12, 30), 30)]},
        productive_minutes=lambda *_args: 0,
    )["Repair 4"]
    assert len(credits) == 1
    assert credits[0].person_name is None
    assert credits[0].source == "unassigned"
    assert credits[0].actual_units == 50.0


def test_remaining_total_uses_each_segment_productive_time():
    segments = [
        WorkSegment("Repair 4", "A", t(12), t(13), "punch"),
        WorkSegment("Repair 4", "B", t(13), t(15), "punch"),
    ]
    credits = credit_work_segments(
        segments,
        wc_totals={"Repair 4": 90},
        samples_by_wc={},
        productive_minutes=lambda _person, _wc, start, end: (
            end - start
        ).total_seconds() / 60,
    )["Repair 4"]
    assert [(row.person_name, row.actual_units) for row in credits] == [
        ("A", 30.0),
        ("B", 60.0),
    ]


def test_zero_target_stays_neutral_without_false_finish_goal():
    segment = WorkSegment("Repair 4", "A", t(12), t(13), "punch")
    credits = credit_work_segments(
        [segment],
        wc_totals={"Repair 4": 12},
        samples_by_wc={"Repair 4": [(t(12, 30), 12)]},
        productive_minutes=lambda *_args: 60,
    )
    (score,) = score_work_segments(
        credits, target_per_hour={"Repair 4": 0}
    )["Repair 4"]
    assert (score.goal_units, score.runway_units, score.result) == (
        0.0, 12.0, "neutral",
    )


def test_sequential_transfers_cover_completed_and_live_result_states():
    segments = [
        WorkSegment("Repair 4", "A", t(12), t(13), "punch"),
        WorkSegment("Repair 4", "B", t(13), t(14), "punch"),
        WorkSegment("Repair 4", "C", t(14), t(15), "punch"),
        WorkSegment("Repair 4", "D", t(15), t(16), "punch"),
    ]
    credits = credit_work_segments(
        segments,
        wc_totals={"Repair 4": 220},
        samples_by_wc={"Repair 4": [
            (t(12, 30), 50),
            (t(13, 30), 60),
            (t(14, 30), 80),
            (t(15, 30), 30),
        ]},
        productive_minutes=lambda *_args: 60,
        live_cap_utc=t(16),
    )
    scored = score_work_segments(
        credits, target_per_hour={"Repair 4": 60}
    )["Repair 4"]
    assert [
        (row.person_name, row.result, row.is_active)
        for row in scored
    ] == [
        ("A", "behind", False),
        ("B", "ahead", False),
        ("C", "ahead", False),
        ("D", "behind", True),
    ]
    assert [(row.actual_units, row.goal_units) for row in scored] == [
        (50.0, 60.0),
        (60.0, 60.0),
        (80.0, 60.0),
        (30.0, 60.0),
    ]
```

- [ ] **Step 6: Run the complete pure suite**

Run:

```bash
.venv/bin/pytest tests/test_production_segments.py -v
```

Expected: 7 tests PASS, covering active and completed workers below, equal to, and above their independent goals.

- [ ] **Step 7: Commit and push Task 1**

```bash
git add src/zira_dashboard/production_segments.py tests/test_production_segments.py
git commit -m "feat: score worker production segments"
git push origin main
```

Expected: the commit is on `origin/main`; no unrelated untracked files are staged.

---

### Task 2: Make Production History Use Shared Segment Credit

**Files:**
- Modify: `src/zira_dashboard/production_history.py:91-188`
- Modify: `tests/test_production_history.py:63-111`

**Interfaces:**
- Consumes: `production_segments.credit_work_segments(...)` from Task 1.
- Produces: the unchanged `attribute_for_segments(...) -> dict[str, dict[str, dict[str, float]]]` API.

- [ ] **Step 1: Add a returning-worker production-history regression test**

Append to `tests/test_production_history.py`:

```python
def test_attribute_for_segments_aggregates_returning_worker_without_merging_credit_windows():
    from datetime import datetime, timezone

    utc = timezone.utc
    t0 = datetime(2026, 8, 20, 12, tzinfo=utc)
    t1 = datetime(2026, 8, 20, 13, tzinfo=utc)
    t2 = datetime(2026, 8, 20, 14, tzinfo=utc)
    t3 = datetime(2026, 8, 20, 15, tzinfo=utc)
    out = attribute_for_segments(
        [
            WorkSegment("Repair 4", "Humberto S.", t0, t1, "punch"),
            WorkSegment("Repair 4", "Humberto S.", t2, t3, "punch"),
        ],
        wc_totals={"Repair 4": (30, 8)},
        samples_by_wc={"Repair 4": [(t0, 10), (t2, 20)]},
        productive_minutes=lambda _person, _wc, start, end: (
            end - start
        ).total_seconds() / 60,
    )
    assert out["Humberto S."]["Repair 4"] == {
        "units": 30.0,
        "downtime": 8.0,
        "hours": 2.0,
        "days_worked": 1,
        "excluded_minutes": 0.0,
    }
```

- [ ] **Step 2: Run the production-history tests before the refactor**

Run:

```bash
.venv/bin/pytest tests/test_production_history.py -v
```

Expected: the new regression and existing tests PASS, establishing the behavior to preserve.

- [ ] **Step 3: Replace local sample allocation with shared credit aggregation**

In `src/zira_dashboard/production_history.py`, import the shared function:

```python
from .production_segments import credit_work_segments
```

Replace `attribute_for_segments` with this implementation while retaining its current docstring intent:

```python
def attribute_for_segments(
    segments,
    *,
    wc_totals: dict[str, tuple[int, int]],
    samples_by_wc: dict[str, list[tuple]],
    productive_minutes: Callable[[str, str, datetime, datetime], float],
    excluded_minutes: dict[str, dict[str, float]] | None = None,
) -> dict[str, dict[str, dict[str, float]]]:
    excluded_minutes = excluded_minutes or {}
    credits = credit_work_segments(
        segments,
        wc_totals={wc_name: totals[0] for wc_name, totals in wc_totals.items()},
        samples_by_wc=samples_by_wc,
        productive_minutes=productive_minutes,
    )
    out: dict[str, dict[str, dict[str, float]]] = {}

    def entry(person: str, wc_name: str) -> dict[str, float]:
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
            if credit.person_name is None or credit.productive_minutes <= 0:
                continue
            totals = entry(credit.person_name, wc_name)
            totals["units"] += credit.actual_units
            totals["hours"] += credit.productive_minutes / 60.0

    for wc_name, (_units, downtime) in wc_totals.items():
        people = [
            (person, wc_map[wc_name])
            for person, wc_map in out.items()
            if wc_name in wc_map
        ]
        total_hours = sum(totals["hours"] for _person, totals in people)
        if total_hours <= 0:
            continue
        for _person, totals in people:
            totals["downtime"] += float(downtime or 0) * totals["hours"] / total_hours
    return out
```

- [ ] **Step 4: Run production-history and pure segment tests**

Run:

```bash
.venv/bin/pytest tests/test_production_segments.py tests/test_production_history.py -v
```

Expected: all tests PASS; the existing move-between-work-centers test still reports Jesus `45`, Christian Dismantler `15`, and Christian Repair `40`.

- [ ] **Step 5: Commit and push Task 2**

```bash
git add src/zira_dashboard/production_history.py tests/test_production_history.py
git commit -m "refactor: share worker production credit"
git push origin main
```

Expected: the refactor commit is on `origin/main`.

---

### Task 3: Thread Single-Day Scores Through Department Data

**Files:**
- Modify: `src/zira_dashboard/routes/departments.py:150-435, 558-571, 849-865`
- Modify: `src/zira_dashboard/recycling_range.py:6-73`
- Modify: `tests/test_department_operator_labels.py:28-120`
- Modify: `tests/test_recycling_range.py:6-57`

**Interfaces:**
- Consumes: `credit_work_segments` and `score_work_segments` from Task 1.
- Produces: per-day `per_wc_segments`, `is_live_dashboard`, aggregate `single_day_segments`, and aggregate `single_day_is_live`.

- [ ] **Step 1: Extend the department transfer fixture with samples and expected score assertions**

In `tests/test_department_operator_labels.py`, update the two `SimpleNamespace` station rows in `test_department_day_data_shows_transfer_at_current_wc_but_keeps_both_active` to include `samples`. Use:

```python
samples=(
    (datetime(2026, 6, 2, 12, 2, tzinfo=timezone.utc), 34),
),
```

for Repair 2 and:

```python
samples=(
    (datetime(2026, 6, 2, 12, 10, tzinfo=timezone.utc), 384),
),
```

for Dismantler 2. Then add:

Change the partial-time-off fixture from `{}` to a 30-minute interval during Jesus's Dismantler segment:

```python
monkeypatch.setattr(attendance, "partial_off_intervals", lambda _d: {
    "Jesus G.": [(
        datetime(2026, 6, 2, 13, tzinfo=timezone.utc),
        datetime(2026, 6, 2, 13, 30, tzinfo=timezone.utc),
    )],
})
```

Change the breakdown fixtures so Repair 2 has two excluded goal minutes:

```python
monkeypatch.setattr(wc_attributions, "breakdown_windows_for_day", lambda _d: {
    ("Jesus G.", "Repair 2"): [(
        datetime(2026, 6, 2, 12, tzinfo=timezone.utc),
        datetime(2026, 6, 2, 12, 2, tzinfo=timezone.utc),
    )],
})
monkeypatch.setattr(
    machine_breakdown,
    "excluded_minutes_overlapping",
    lambda windows, *_args, **_kwargs: 2.0 if windows else 0.0,
)
```

Replace the existing `assert live["total_man_hours"] == 7.0` with `assert live["total_man_hours"] == 6.5`, then add:

```python
repair_score = live["per_wc_segments"]["Repair 2"][0]
assert repair_score["person_name"] == "Jesus G."
assert repair_score["actual_units"] == 34.0
assert repair_score["goal_units"] == 3.0
assert repair_score["is_active"] is False
assert repair_score["time_label"] == "7-7:05a"

dismantler_score = live["per_wc_segments"]["Dismantler 2"][0]
assert dismantler_score["person_name"] == "Jesus G."
assert dismantler_score["actual_units"] == 384.0
assert dismantler_score["goal_units"] == 415.0
assert dismantler_score["is_active"] is True
assert dismantler_score["time_label"] == "since 7:05a"
assert live["is_live_dashboard"] is True
assert live["per_wc_expected"] == {
    "Repair 2": 3.0,
    "Dismantler 2": 415.0,
}
```

These assertions prove the same callback subtracts an approved machine breakdown, while the 30-minute partial time off changes labor hours but does not shrink the Dismantler segment goal. Segment goals must continue to use `_productive_minutes_less_breakdown`, not `effective_minutes_worked`.

- [ ] **Step 2: Run the focused route test and confirm the new keys are absent**

Run:

```bash
.venv/bin/pytest tests/test_department_operator_labels.py::test_department_day_data_shows_transfer_at_current_wc_but_keeps_both_active -v
```

Expected: FAIL with `KeyError: 'per_wc_segments'`.

- [ ] **Step 3: Score segments in `_department_day_data` and format view dictionaries**

Add these imports in `src/zira_dashboard/routes/departments.py`:

```python
from .. import production_segments, time_format
```

Add the formatter near `_present_assignments`:

```python
def _segment_view(score) -> dict:
    start_local = (
        score.start_utc.astimezone(shift_config.SITE_TZ)
        if score.start_utc is not None else None
    )
    end_local = (
        score.end_utc.astimezone(shift_config.SITE_TZ)
        if score.end_utc is not None else None
    )
    if score.person_name is None:
        time_label = ""
    elif score.is_active and start_local is not None:
        time_label = f"since {time_format.fmt_time_short(start_local.isoformat())}"
    elif start_local is not None and end_local is not None:
        time_label = time_format.fmt_time_range(
            start_local.isoformat(), end_local.isoformat()
        )
    else:
        time_label = ""
    delta = score.actual_units - score.goal_units
    return {
        "segment_id": score.segment_id,
        "person_name": score.person_name,
        "person_label": score.person_name or "Unassigned production",
        "time_label": time_label,
        "actual_units": score.actual_units,
        "goal_units": score.goal_units,
        "runway_units": score.runway_units,
        "productive_minutes": score.productive_minutes,
        "is_active": score.is_active,
        "result": score.result,
        "result_label": (
            "unassigned"
            if score.person_name is None
            else f"{abs(round(delta))} {'ahead' if delta >= 0 else 'behind'}"
            if score.goal_units > 0
            else "no goal"
        ),
    }
```

Immediately after `_productive_minutes_less_breakdown` and the existing `per_wc_expected` calculation, add:

```python
    try:
        credits = production_segments.credit_work_segments(
            [segment for segment in segments if segment.wc_name in active_wc_names],
            wc_totals={r.station.name: r.units for r in active_results},
            samples_by_wc={
                r.station.name: list(getattr(r, "samples", ()) or ())
                for r in active_results
            },
            productive_minutes=_productive_minutes_less_breakdown,
            live_cap_utc=window_end_utc if is_live_dashboard else None,
        )
        scored = production_segments.score_work_segments(
            credits,
            target_per_hour=target_per_hour,
        )
        per_wc_segments = {
            wc_name: tuple(_segment_view(score) for score in scores)
            for wc_name, scores in scored.items()
        }
    except Exception:
        per_wc_segments = {}
```

Add these return keys:

```python
        "per_wc_segments": per_wc_segments,
        "is_live_dashboard": is_live_dashboard,
```

At the end of the same route test, prove the fail-soft path cannot erase station totals:

```python
from zira_dashboard import production_segments


def fail_credit(*_args, **_kwargs):
    raise RuntimeError("sample source unavailable")


monkeypatch.setattr(production_segments, "credit_work_segments", fail_credit)
fallback = departments._department_day_data(
    day,
    datetime(2026, 6, 2, 19, tzinfo=timezone.utc),
    True,
    stations=[repair, dismantler],
    labor_department="Recycled",
    group_categories=("Repair", "Dismantler"),
)
assert fallback["per_wc_segments"] == {}
assert fallback["per_wc_units"] == {"Repair 2": 34, "Dismantler 2": 384}
```

- [ ] **Step 4: Run the focused route test**

Run:

```bash
.venv/bin/pytest tests/test_department_operator_labels.py::test_department_day_data_shows_transfer_at_current_wc_but_keeps_both_active -v
```

Expected: PASS with a completed Repair 2 segment and a live Dismantler 2 segment.

- [ ] **Step 5: Add single-day pass-through to `recycling_range.py` with tests**

In the `RangeAggregate` dataclass add:

```python
    single_day_segments: dict[str, tuple[dict, ...]]
    single_day_is_live: bool
```

Initialize both before the aggregation loop:

```python
    single_day_segments: dict[str, tuple[dict, ...]] = {}
    single_day_is_live = False
```

Inside `if not is_range:` add:

```python
            single_day_segments = item.get("per_wc_segments", {})
            single_day_is_live = bool(item.get("is_live_dashboard", False))
```

Return both fields in `RangeAggregate(...)`.

Update `tests/test_recycling_range.py::_day` with:

```python
        "per_wc_segments": {
            "Dismantler 1": ({"person_name": who, "actual_units": units},)
        },
        "is_live_dashboard": True,
```

Add to `test_single_day_keeps_who_and_assignments`:

```python
assert result.single_day_segments is item["per_wc_segments"]
assert result.single_day_is_live is True
```

Add to `test_multi_day_sums_work_center_metrics_without_single_day_labels`:

```python
assert result.single_day_segments == {}
assert result.single_day_is_live is False
```

- [ ] **Step 6: Run route and range tests**

Run:

```bash
.venv/bin/pytest tests/test_department_operator_labels.py tests/test_recycling_range.py tests/test_new_dashboard_data.py -v
```

Expected: all tests PASS; New still discovers all configured metered work centers through `_department_day_data`.

- [ ] **Step 7: Commit and push Task 3**

```bash
git add src/zira_dashboard/routes/departments.py src/zira_dashboard/recycling_range.py tests/test_department_operator_labels.py tests/test_recycling_range.py
git commit -m "feat: expose worker production segments"
git push origin main
```

Expected: the data-pipeline commit is on `origin/main`.

---

### Task 4: Build Continuous Runway Geometry

**Files:**
- Modify: `src/zira_dashboard/recycling_data.py:69-110`
- Modify: `src/zira_dashboard/routes/departments.py:608-618, 855-864`
- Modify: `tests/test_recycling_data.py:84-155`

**Interfaces:**
- Consumes: `RangeAggregate.single_day_segments` and `single_day_is_live` from Task 3.
- Produces: bar dictionaries with `segments`, `has_segments`, `no_one_here_now`, and per-segment percentage geometry.

- [ ] **Step 1: Add failing geometry and fallback tests**

Append to `tests/test_recycling_data.py`:

```python
def test_build_bars_places_independent_runways_on_one_scale():
    segments = {
        "Repair 4": (
            {
                "person_name": "Humberto S.", "person_label": "Humberto S.",
                "time_label": "7a-2:33p", "actual_units": 516.0,
                "goal_units": 700.0, "runway_units": 700.0,
                "is_active": False, "result": "behind",
                "result_label": "184 behind",
            },
            {
                "person_name": "Ana M.", "person_label": "Ana M.",
                "time_label": "since 2:35p", "actual_units": 32.0,
                "goal_units": 25.0, "runway_units": 32.0,
                "is_active": True, "result": "ahead",
                "result_label": "7 ahead",
            },
        )
    }
    bars = rd.build_bars(
        "Repair",
        agg_active_names={"Repair 4"},
        agg_category={"Repair 4": "Repair"},
        agg_units={"Repair 4": 548},
        agg_expected={"Repair 4": 725.0},
        agg_who_today={"Repair 4": "Ana M."},
        is_range=False,
        agg_downtime={},
        agg_segments=segments,
        is_live=True,
    )
    (bar,) = bars
    humberto, ana = bar["segments"]
    scale = 732.0 * 1.1
    assert humberto["start_pct"] == 0.0
    assert humberto["actual_pct"] == 516.0 / scale * 100
    assert humberto["shortfall_pct"] == 184.0 / scale * 100
    assert humberto["finish_pct"] == 700.0 / scale * 100
    assert ana["start_pct"] == 700.0 / scale * 100
    assert ana["finish_pct"] == 725.0 / scale * 100
    assert bar["who"] == "Ana M."
    assert bar["no_one_here_now"] is False


def test_build_bars_marks_live_station_empty_when_history_remains():
    bars = rd.build_bars(
        "Repair",
        agg_active_names={"Repair 4"},
        agg_category={"Repair 4": "Repair"},
        agg_units={"Repair 4": 10},
        agg_expected={"Repair 4": 20},
        agg_who_today={},
        is_range=False,
        agg_downtime={},
        agg_segments={"Repair 4": ({
            "person_name": "Humberto S.", "person_label": "Humberto S.",
            "time_label": "7-8a", "actual_units": 10.0, "goal_units": 20.0,
            "runway_units": 20.0, "is_active": False, "result": "behind",
            "result_label": "10 behind",
        },)},
        is_live=True,
    )
    assert bars[0]["no_one_here_now"] is True


def test_range_bars_ignore_single_day_segments():
    bars = rd.build_bars(
        "Repair",
        agg_active_names={"Repair 4"},
        agg_category={"Repair 4": "Repair"},
        agg_units={"Repair 4": 10},
        agg_expected={"Repair 4": 20},
        agg_who_today={"Repair 4": "Humberto S."},
        is_range=True,
        agg_downtime={},
        agg_segments={"Repair 4": ({"runway_units": 20.0},)},
        is_live=False,
    )
    assert bars[0]["segments"] == []
    assert bars[0]["who"] is None


def test_completed_shift_hides_worker_from_left_but_keeps_segment_history():
    bars = rd.build_bars(
        "Repair",
        agg_active_names={"Repair 4"},
        agg_category={"Repair 4": "Repair"},
        agg_units={"Repair 4": 516},
        agg_expected={"Repair 4": 700},
        agg_who_today={"Repair 4": "Humberto S."},
        is_range=False,
        agg_downtime={},
        agg_segments={"Repair 4": ({
            "person_name": "Humberto S.", "person_label": "Humberto S.",
            "time_label": "7a-2:33p", "actual_units": 516.0,
            "goal_units": 700.0, "runway_units": 700.0,
            "is_active": False, "result": "behind",
            "result_label": "184 behind",
        },)},
        is_live=False,
    )
    assert bars[0]["who"] is None
    assert bars[0]["no_one_here_now"] is False
    assert bars[0]["segments"][0]["person_name"] == "Humberto S."
```

- [ ] **Step 2: Run the geometry tests and confirm the new arguments fail**

Run:

```bash
.venv/bin/pytest tests/test_recycling_data.py -k 'independent_runways or live_station_empty or range_bars_ignore' -v
```

Expected: FAIL with `TypeError: build_bars() got an unexpected keyword argument 'agg_segments'`.

- [ ] **Step 3: Extend `build_bars` with segment geometry**

Add optional keyword arguments:

```python
    agg_segments: dict | None = None,
    is_live: bool = False,
```

Use this geometry path after the base row dictionaries are created and before returning:

```python
    agg_segments = agg_segments or {}
    spans = {
        row["name"]: sum(
            float(segment.get("runway_units", 0.0) or 0.0)
            for segment in agg_segments.get(row["name"], ())
        )
        for row in out
    }
    base = max(
        (
            spans[row["name"]]
            if not is_range and spans[row["name"]] > 0
            else max(float(row["units"]), float(row["expected"]))
        )
        for row in out
    ) if out else 0.0
    scale = base * 1.1 if base > 0 else 1.0
    has_target_line = any(row["expected"] > 0 for row in out)

    for row in out:
        source_segments = (
            tuple(agg_segments.get(row["name"], ())) if not is_range else ()
        )
        cursor = 0.0
        geometry = []
        for segment in source_segments:
            actual = max(0.0, float(segment.get("actual_units", 0.0) or 0.0))
            goal = max(0.0, float(segment.get("goal_units", 0.0) or 0.0))
            runway = max(actual, goal)
            item = dict(segment)
            item.update({
                "start_pct": cursor / scale * 100.0,
                "actual_pct": actual / scale * 100.0,
                "shortfall_start_pct": (cursor + actual) / scale * 100.0,
                "shortfall_pct": max(goal - actual, 0.0) / scale * 100.0,
                "finish_pct": (cursor + goal) / scale * 100.0 if goal > 0 else None,
                "runway_pct": runway / scale * 100.0,
                "label_below": actual / scale * 100.0 < 18.0,
            })
            geometry.append(item)
            cursor += runway
        row["segments"] = geometry
        row["has_segments"] = bool(geometry)
        row["no_one_here_now"] = bool(
            is_live
            and geometry
            and not row["who"]
            and any(segment.get("person_name") for segment in geometry)
        )
        row["pct"] = float(row["units"]) / scale * 100.0
        row["target_pct"] = (
            float(row["expected"]) / scale * 100.0
            if scale and has_target_line and not geometry else None
        )
```

Remove the previous `max_u`, `max_e`, `base`, `scale`, and final percentage loop so there is only one scale calculation.

Also change the base row's current-worker value so completed shifts keep history in the bar without presenting a stale current worker on the left:

```python
            "who": (
                agg_who_today.get(name)
                if not is_range and is_live
                else None
            ),
```

- [ ] **Step 4: Pass aggregate segment arguments from both department renderers**

In `_render_recycling` and `_render_new_dept`, add to every `build_bars(...)` call:

```python
            agg_segments=aggregate.single_day_segments,
            is_live=aggregate.single_day_is_live,
```

- [ ] **Step 5: Run all pure bar and route-data tests**

Run:

```bash
.venv/bin/pytest tests/test_recycling_data.py tests/test_recycling_range.py tests/test_new_dashboard_data.py -v
```

Expected: all tests PASS, including the pre-existing color and range assertions.

- [ ] **Step 6: Commit and push Task 4**

```bash
git add src/zira_dashboard/recycling_data.py src/zira_dashboard/routes/departments.py tests/test_recycling_data.py
git commit -m "feat: build continuous worker runways"
git push origin main
```

Expected: the geometry commit is on `origin/main`.

---

### Task 5: Render Horizontal Worker Segments and Current Status

**Files:**
- Modify: `src/zira_dashboard/templates/_department_dashboard_widgets.html:9-99`
- Modify: `src/zira_dashboard/static/recycling.css:371-450`
- Modify: `tests/test_new_dashboard_template.py:15-158`

**Interfaces:**
- Consumes: bar and segment dictionaries from Task 4.
- Produces: `.worker-segment-*` horizontal markup, visible shortfall/finish states, and `No one here now` left label.

- [ ] **Step 1: Add focused horizontal Jinja render tests**

Extend `_render_new` in `tests/test_new_dashboard_template.py` with a `tv_mode=False` keyword argument and pass it into the render context. Also add `is_today=True`, `now_label="2:41"`, and `shift_start_label="07:00"` to that context. Add:

```python
def _segmented_bar():
    return {
        "name": "Repair 4", "who": None, "units": 548, "expected": 725,
        "pct": 68.0, "target_pct": None, "pct_of_target": 75.6,
        "color": None, "downtime_minutes": 0, "has_segments": True,
        "no_one_here_now": True,
        "segments": [
            {
                "person_name": "Humberto S.", "person_label": "Humberto S.",
                "time_label": "7a-2:33p", "actual_units": 516.0,
                "goal_units": 700.0, "result": "behind",
                "result_label": "184 behind", "is_active": False,
                "start_pct": 0.0, "actual_pct": 59.0,
                "shortfall_start_pct": 59.0, "shortfall_pct": 21.0,
                "finish_pct": 80.0, "label_below": False,
            },
            {
                "person_name": "Ana M.", "person_label": "Ana M.",
                "time_label": "since 2:35p", "actual_units": 32.0,
                "goal_units": 25.0, "result": "ahead", "result_label": "7 ahead",
                "is_active": True, "start_pct": 80.0, "actual_pct": 15.0,
                "shortfall_start_pct": 95.0, "shortfall_pct": 0.0,
                "finish_pct": 92.0, "label_below": True,
            },
        ],
    }


def test_new_horizontal_bar_renders_worker_segments_and_finish_states():
    html = _render_new(new_bars=[_segmented_bar()])
    assert 'class="worker-segment-fill result-behind"' in html
    assert 'class="worker-segment-shortfall"' in html
    assert 'class="worker-segment-goal completed"' in html
    assert 'class="worker-segment-goal live"' in html
    assert "Humberto S." in html and "7a-2:33p" in html
    assert "Ana M." in html and "since 2:35p" in html
    assert "184 behind" in html and "7 ahead" in html
    assert 'class="worker-segment-result"' in html
    assert "516/700" in html and "32/25" in html
    assert "No one here now" in html
    assert 'class="bar-target-line"' not in html


def test_new_segmented_bar_keeps_widget_number_position():
    html = _render_new(
        customs={"new-bars": {"number_position": "inside"}},
        new_bars=[_segmented_bar()],
    )
    assert 'class="segment-total in"' in html
    assert ">548<" in html


def test_new_unsegmented_bar_keeps_legacy_fill_and_target():
    bar = _segmented_bar()
    bar.update(has_segments=False, segments=[], no_one_here_now=False, target_pct=80.0)
    html = _render_new(new_bars=[bar])
    assert 'class="bar-fill"' in html
    assert 'class="bar-target-line"' in html


def test_new_tv_keeps_full_worker_text_visible_in_shared_markup():
    html = _render_new(tv_mode=True, new_bars=[_segmented_bar()])
    assert "Humberto S." in html and "7a-2:33p" in html
    assert "516/700" in html and "184 behind" in html
    assert "Ana M." in html and "32/25" in html and "7 ahead" in html
```

- [ ] **Step 2: Run the horizontal template tests and confirm missing markup**

Run:

```bash
.venv/bin/pytest tests/test_new_dashboard_template.py -k 'worker_segments or segmented_bar or unsegmented_bar' -v
```

Expected: FAIL because `.worker-segment-*` and `No one here now` are not rendered.

- [ ] **Step 3: Add shared horizontal segment markup**

In `_department_dashboard_widgets.html`, change the horizontal name branch before the existing `b.who` branch:

```jinja2
          {% elif b.no_one_here_now %}
            <span class="name-primary current-empty">No one here now</span>
            <span class="name-secondary">{{ b.name }}</span>
```

Inside `.bar-track`, render segmented bars before the legacy `.bar-fill`:

```jinja2
          {% if b.has_segments %}
            {% for s in b.segments %}
              {% if s.actual_pct > 0 %}
                <div class="worker-segment-fill result-{{ s.result }}"
                     style="left:{{ s.start_pct }}%;width:{{ s.actual_pct }}%"
                     title="{{ s.person_label }} · {{ s.time_label }} · {{ s.actual_units|round|int }} / {{ s.goal_units|round|int }} · {{ s.result_label }}">
                  {% if not s.label_below %}
                    <span class="worker-segment-name">
                      {{ s.person_label }}
                      <small class="worker-segment-result">
                        {{ s.time_label }} · {{ s.actual_units|round|int }}/{{ s.goal_units|round|int }} · {{ s.result_label }}
                      </small>
                    </span>
                  {% endif %}
                </div>
              {% endif %}
              {% if s.shortfall_pct > 0 %}
                <div class="worker-segment-shortfall"
                     style="left:{{ s.shortfall_start_pct }}%;width:{{ s.shortfall_pct }}%"
                     aria-hidden="true"></div>
              {% endif %}
              {% if s.finish_pct is not none %}
                <div class="worker-segment-goal {{ 'live' if s.is_active else 'completed' }}"
                     style="left:{{ s.finish_pct }}%"
                     title="{{ 'Goal now' if s.is_active else 'Finish goal' }} {{ s.goal_units|round|int }}"></div>
              {% endif %}
            {% endfor %}
            {% if numpos == 'inside' %}<span class="segment-total in">{{ b.units }}</span>{% endif %}
            {% if numpos == 'bar' %}<span class="segment-total edge">{{ b.units }}</span>{% endif %}
          {% else %}
            <div class="bar-fill" style="width: {{ b.pct }}%; background: {{ fill_color }}">
              {% if numpos == 'inside' %}<span class="in">{{ b.units }}</span>{% endif %}
              {% if numpos == 'bar' %}<span class="edge">{{ b.units }}</span>{% endif %}
            </div>
            {% if b.target_pct is not none %}<div class="bar-target-line" style="left: {{ b.target_pct }}%" title="goal {{ b.expected }}"></div>{% endif %}
          {% endif %}
```

Immediately below `.bar-track`, add visible narrow labels:

```jinja2
        {% if b.has_segments %}
          <div class="worker-segment-labels">
            {% for s in b.segments if s.label_below %}
              <span class="worker-segment-callout result-{{ s.result }}">
                {{ s.person_label }} · {{ s.time_label }} · {{ s.actual_units|round|int }}/{{ s.goal_units|round|int }} · {{ s.result_label }}
              </span>
            {% endfor %}
          </div>
        {% endif %}
```

Change the axis-row condition to:

```jinja2
    {% if widget_target_pct is not none and not is_range and not (items | selectattr('has_segments') | list) %}
```

- [ ] **Step 4: Add horizontal segment CSS**

In `recycling.css`, change `.bar-track` to `overflow: visible` and add:

```css
  .worker-segment-fill,
  .worker-segment-shortfall {
    position: absolute;
    top: 0;
    bottom: 0;
  }
  .worker-segment-fill.result-ahead { background: var(--good); }
  .worker-segment-fill.result-behind { background: var(--bad); }
  .worker-segment-fill.result-neutral { background: var(--muted); }
  .worker-segment-shortfall {
    background: repeating-linear-gradient(
      135deg,
      var(--panel-2) 0 5px,
      color-mix(in srgb, var(--muted) 28%, var(--panel-2)) 5px 10px
    );
  }
  .worker-segment-goal {
    position: absolute;
    z-index: 4;
    top: -4px;
    bottom: -4px;
    transform: translateX(-50%);
    pointer-events: none;
  }
  .worker-segment-goal.live {
    width: 3px;
    background: var(--fg);
    box-shadow: 0 0 0 1px var(--panel);
  }
  .worker-segment-goal.completed {
    width: 7px;
    border: 1px solid var(--fg);
    background: repeating-linear-gradient(
      to bottom, var(--fg) 0 4px, var(--panel) 4px 8px
    );
  }
  .worker-segment-name {
    position: absolute;
    left: 0.45rem;
    top: 50%;
    transform: translateY(-50%);
    max-width: calc(100% - 0.7rem);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: white;
    font-size: 0.68rem;
    font-weight: 700;
    text-shadow: 0 1px 2px rgb(0 0 0 / 35%);
  }
  .worker-segment-name small {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    font-size: 0.82em;
    font-weight: 600;
  }
  .worker-segment-labels {
    display: flex;
    flex-wrap: wrap;
    gap: 0.2rem 0.45rem;
    margin-top: 0.25rem;
    color: var(--muted);
    font-size: 0.65rem;
  }
  .worker-segment-callout::before {
    content: '';
    display: inline-block;
    width: 0.5rem;
    height: 0.5rem;
    margin-right: 0.2rem;
    border-radius: 2px;
    background: var(--muted);
  }
  .worker-segment-callout.result-ahead::before { background: var(--good); }
  .worker-segment-callout.result-behind::before { background: var(--bad); }
  .segment-total { position: absolute; z-index: 5; color: white; font-weight: 700; }
  .segment-total.in { left: 50%; top: 50%; transform: translate(-50%, -50%); }
  .segment-total.edge { right: 0.25rem; top: 50%; transform: translateY(-50%); }
  .current-empty { color: var(--bad); }
```

- [ ] **Step 5: Run horizontal render and scaling guards**

Run:

```bash
.venv/bin/pytest tests/test_new_dashboard_template.py tests/test_recycling_scaling_static.py -v
```

Expected: all tests PASS; no fixed min/max track height is introduced.

- [ ] **Step 6: Commit and push Task 5**

```bash
git add src/zira_dashboard/templates/_department_dashboard_widgets.html src/zira_dashboard/static/recycling.css tests/test_new_dashboard_template.py
git commit -m "feat: render worker production runways"
git push origin main
```

Expected: horizontal segmented bars are on `origin/main`.

---

### Task 6: Vertical Layout, Full-Page Wording, and Accessibility

**Files:**
- Modify: `src/zira_dashboard/templates/_department_dashboard_widgets.html:20-52`
- Modify: `src/zira_dashboard/static/recycling.css:488-505, 686-693`
- Modify: `tests/test_new_dashboard_template.py`
- Modify: `tests/test_dashboards_polish.py:78-145`
- Modify: `CHANGELOG.md:1-20`

**Interfaces:**
- Consumes: the same segment geometry from Task 4.
- Produces: vertical stacked runways, visible worker result lists, live/past left wording, and a user-facing release note.

- [ ] **Step 1: Add failing vertical and full-page wording tests**

Append to `tests/test_new_dashboard_template.py`:

```python
def test_new_vertical_bar_renders_segment_blocks_finish_markers_and_visible_list():
    html = _render_new(
        customs={"new-bars": {"orientation": "vertical"}},
        new_bars=[_segmented_bar()],
    )
    assert 'class="vworker-segment-fill result-behind"' in html
    assert 'class="vworker-segment-shortfall"' in html
    assert 'class="vworker-segment-goal completed"' in html
    assert 'class="vworker-segment-goal live"' in html
    assert 'class="vworker-segment-list"' in html
    assert "Humberto S." in html and "184 behind" in html
    assert "Ana M." in html and "7 ahead" in html
```

Add a new full-page regression in `tests/test_dashboards_polish.py` using the existing frozen-clock and leaderboard fixtures:

```python
def test_recycling_transferred_worker_moves_into_bar_and_left_says_no_one_here_now(monkeypatch):
    _freeze_route_clock_mid_shift(monkeypatch)
    from zira_dashboard import timeclock_windows, wc_attributions
    from zira_dashboard.plant_day import today as plant_today
    from zira_dashboard import shift_config
    from zira_dashboard.leaderboard import StationTotal
    from zira_dashboard.stations import Station

    day = plant_today()
    start = datetime.combine(day, time(7), tzinfo=shift_config.SITE_TZ).astimezone(timezone.utc)
    moved = datetime.combine(day, time(12), tzinfo=shift_config.SITE_TZ).astimezone(timezone.utc)
    monkeypatch.setattr(staffing, "load_schedule", lambda d: staffing.Schedule(
        day=d, published=True, assignments={"Repair 4": ["Humberto S."]},
    ))
    monkeypatch.setattr(timeclock_windows, "attendance_windows_for_day", lambda _d: {
        "Humberto S.": [("Repair 4", start, moved)],
    })
    monkeypatch.setattr(timeclock_windows, "current_attendance_windows", lambda: ({}, moved))
    monkeypatch.setattr(wc_attributions, "creditable_for_day", lambda _d: [])
    station = Station("44483", "Repair 4", "Repair", "Recycling")
    with patch("zira_dashboard.routes.departments.leaderboard") as lb:
        lb.return_value = [StationTotal(
            station, units=516, reading_count=1, truncated=False,
            downtime_minutes=0, active_minutes=300, last_reading_at=moved,
            last_status="Working", samples=((start, 516),), active_intervals=(),
        )]
        html = TestClient(app).get("/recycling").text
    assert "No one here now" in html
    assert "Humberto S." in html
    assert "worker-segment-fill" in html
```

- [ ] **Step 2: Run the focused tests and confirm vertical markup is missing**

Run:

```bash
.venv/bin/pytest tests/test_new_dashboard_template.py::test_new_vertical_bar_renders_segment_blocks_finish_markers_and_visible_list tests/test_dashboards_polish.py::test_recycling_transferred_worker_moves_into_bar_and_left_says_no_one_here_now -v
```

Expected: the vertical test FAILS on missing `.vworker-segment-*`; the full-page test reaches the segmented route path and fails until final wording/markup is complete.

- [ ] **Step 3: Add vertical segment markup**

In the vertical `.vbar-track`, branch on `b.has_segments` exactly as the horizontal track does, but use bottom/height geometry:

```jinja2
          <div class="vbar-track">
            {% if b.has_segments %}
              {% for s in b.segments %}
                {% if s.actual_pct > 0 %}
                  <div class="vworker-segment-fill result-{{ s.result }}"
                       style="bottom:{{ s.start_pct }}%;height:{{ s.actual_pct }}%"></div>
                {% endif %}
                {% if s.shortfall_pct > 0 %}
                  <div class="vworker-segment-shortfall"
                       style="bottom:{{ s.shortfall_start_pct }}%;height:{{ s.shortfall_pct }}%"></div>
                {% endif %}
                {% if s.finish_pct is not none %}
                  <div class="vworker-segment-goal {{ 'live' if s.is_active else 'completed' }}"
                       style="bottom:{{ s.finish_pct }}%"></div>
                {% endif %}
              {% endfor %}
            {% else %}
              <div class="vbar-fill" style="height: {{ b.pct }}%; background: {{ fill_color }}">
                {% if numpos == 'inside' %}<span class="in">{{ b.units }}</span>{% endif %}
                {% if numpos == 'bar' %}<span class="edge">{{ b.units }}</span>{% endif %}
              </div>
              {% if b.target_pct is not none %}<div class="vbar-target-line" style="bottom: {{ b.target_pct }}%" title="goal {{ b.expected }}"></div>{% endif %}
            {% endif %}
          </div>
```

Below `.vbar-name`, add the always-visible list:

```jinja2
          {% if b.has_segments %}
            <div class="vworker-segment-list">
              {% for s in b.segments %}
                <span class="result-{{ s.result }}">
                  {{ s.person_label }} · {{ s.time_label }} · {{ s.actual_units|round|int }}/{{ s.goal_units|round|int }} · {{ s.result_label }}
                </span>
              {% endfor %}
            </div>
          {% endif %}
```

Use the same `b.no_one_here_now` name branch as the horizontal layout.

- [ ] **Step 4: Add vertical and accessibility CSS**

Add:

```css
  .vworker-segment-fill,
  .vworker-segment-shortfall {
    position: absolute;
    left: 0;
    right: 0;
  }
  .vworker-segment-fill.result-ahead { background: var(--good); }
  .vworker-segment-fill.result-behind { background: var(--bad); }
  .vworker-segment-fill.result-neutral { background: var(--muted); }
  .vworker-segment-shortfall {
    background: repeating-linear-gradient(
      135deg,
      var(--panel-2) 0 5px,
      color-mix(in srgb, var(--muted) 28%, var(--panel-2)) 5px 10px
    );
  }
  .vworker-segment-goal {
    position: absolute;
    z-index: 4;
    left: -3px;
    right: -3px;
    transform: translateY(50%);
  }
  .vworker-segment-goal.live {
    height: 3px;
    background: var(--fg);
    box-shadow: 0 0 0 1px var(--panel);
  }
  .vworker-segment-goal.completed {
    height: 7px;
    border: 1px solid var(--fg);
    background: repeating-linear-gradient(
      to right, var(--fg) 0 4px, var(--panel) 4px 8px
    );
  }
  .vworker-segment-list {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    max-width: 100%;
    margin-top: 0.2rem;
    color: var(--muted);
    font-size: clamp(0.55rem, min(3.2cqh, 1.4cqw), 0.78rem);
    line-height: 1.15;
  }
  .vworker-segment-list span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
```

Ensure title attributes and visible text both include worker, time, actual/goal, and result. Do not hide `.worker-segment-labels` or `.vworker-segment-list` in TV mode.

- [ ] **Step 5: Add the user-facing changelog entry**

Immediately after the format comment and before the existing August 19 entry in `CHANGELOG.md`, add:

```markdown
## 2026-08-20

### Production follows each worker

#### Features

- **Work-center bars now remember who made the pallets.** When someone moves to another job, their name, work time, pallet count, and goal stay in the bar. The next person gets a fresh goal, so they do not carry the first person's missed work.
```

Do not modify historical entries.

- [ ] **Step 6: Run all UI, route, and scaling tests**

Run:

```bash
.venv/bin/pytest tests/test_new_dashboard_template.py tests/test_dashboards_polish.py tests/test_recycling_scaling_static.py tests/test_tv_dashboards_vs.py -v
```

Expected: all tests PASS or existing `DATABASE_URL`-gated cases SKIP when no database is configured.

- [ ] **Step 7: Commit and push Task 6**

```bash
git add src/zira_dashboard/templates/_department_dashboard_widgets.html src/zira_dashboard/static/recycling.css tests/test_new_dashboard_template.py tests/test_dashboards_polish.py CHANGELOG.md
git commit -m "feat: show worker history in production bars"
git push origin main
```

Expected: the complete user-facing feature and child-friendly changelog note are on `origin/main`.

---

### Task 7: Full Regression Verification

**Files:**
- Modify only files required to fix a failure caused by Tasks 1-6; do not broaden scope.

**Interfaces:**
- Consumes: the completed scoring, data, geometry, and rendering paths.
- Produces: verified repository health and a final pushed fix commit only if verification exposes an in-scope regression.

- [ ] **Step 1: Run the full focused feature suite**

```bash
.venv/bin/pytest \
  tests/test_production_segments.py \
  tests/test_production_history.py \
  tests/test_department_operator_labels.py \
  tests/test_recycling_range.py \
  tests/test_recycling_data.py \
  tests/test_new_dashboard_data.py \
  tests/test_new_dashboard_template.py \
  tests/test_dashboards_polish.py \
  tests/test_productive_minutes_window.py \
  tests/test_assignment_windows_breakdown.py \
  tests/test_production_history_breakdown.py \
  tests/test_recycling_scaling_static.py \
  tests/test_tv_dashboards_vs.py -v
```

Expected: all runnable tests PASS; environment-gated tests may SKIP with their existing reason.

- [ ] **Step 2: Run the full project test suite**

```bash
.venv/bin/pytest -q
```

Expected: PASS with only documented pre-existing skips.

- [ ] **Step 3: Run Ruff on every changed Python file**

```bash
.venv/bin/ruff check \
  src/zira_dashboard/production_segments.py \
  src/zira_dashboard/production_history.py \
  src/zira_dashboard/routes/departments.py \
  src/zira_dashboard/recycling_range.py \
  src/zira_dashboard/recycling_data.py \
  tests/test_production_segments.py \
  tests/test_production_history.py \
  tests/test_department_operator_labels.py \
  tests/test_recycling_range.py \
  tests/test_recycling_data.py \
  tests/test_new_dashboard_template.py \
  tests/test_dashboards_polish.py
```

Expected: `All checks passed!`

- [ ] **Step 4: Check the final diff and repository state**

```bash
git diff --check
git status --short
git log -7 --oneline --decorate
```

Expected: `git diff --check` is silent; only the user's pre-existing untracked `.cursorignore`, `.python-version`, and `uv.lock` remain; the feature commits are visible on `main` and `origin/main`.

- [ ] **Step 5: Confirm verification did not create a correction commit**

If a feature-caused failure appears in Steps 1-4, return to the task that owns that exact file, add a focused failing test there, make the smallest correction, rerun that task's listed tests, and use that task's explicit `git add`, commit, and push command. Do not create an empty verification commit.

Expected: the working tree still contains only the user's pre-existing untracked files and every feature commit remains pushed to `origin/main`.
