# Conditional Worker Segment Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the original production bar for uninterrupted single-worker shifts while retaining independent worker runways for real staffing splits and treating scheduled lunch as continuous coverage.

**Architecture:** Keep sample credit and independent goal scoring unchanged. Add pure dashboard-presentation helpers that coalesce same-worker, same-work-center scores across touching windows or configured scheduled breaks, then explicitly decide whether named-worker coverage is split. Carry that decision through single-day aggregation into `build_bars`, where legacy or segment geometry is selected without template inference.

**Tech Stack:** Python 3.12, frozen dataclasses, FastAPI route preparation, Jinja2 shared dashboard widgets, pytest, Ruff.

## Global Constraints

- Apply to every metered Recycling and New work center in horizontal, vertical, screen, and TV single-day views.
- Keep multi-day range views unchanged.
- Preserve sample credit, production-history data, station totals, partial-time-off behavior, and machine-breakdown goal exclusions.
- A configured scheduled break is not a staffing split when the same worker returns to the same work center.
- A real worker or work-center change during lunch remains a split.
- Humberto leaving Repair 4 vacant remains a split and continues to show **No one here now**.
- Unassigned readings alone do not trigger the segment runway.
- Do not modify user-owned `.cursorignore`, `.python-version`, or `uv.lock` files.
- Add child-friendly What's New text before the implementation push to `main`.

---

### Task 1: Normalize Display Scores and Classify Worker Coverage

**Files:**
- Modify: `src/zira_dashboard/production_segments.py:13-223`
- Test: `tests/test_production_segments.py`

**Interfaces:**
- Consumes: existing `SegmentScore` values and UTC scheduled-break windows as `Sequence[tuple[datetime, datetime]]`.
- Produces: `coalesce_display_scores(scores, *, ignored_gaps) -> tuple[SegmentScore, ...]` and `worker_coverage_is_split(scores, *, window_start_utc, window_end_utc, ignored_gaps=()) -> bool`.

- [ ] **Step 1: Write failing scheduled-break normalization tests**

Add imports and a focused score factory to `tests/test_production_segments.py`:

```python
from zira_dashboard.production_segments import (
    SegmentScore,
    coalesce_display_scores,
    credit_work_segments,
    score_work_segments,
    worker_coverage_is_split,
)


def _score(
    person,
    start,
    end,
    *,
    actual,
    goal,
    active=False,
    minutes=60,
    wc="Dismantler 1",
    segment_id=0,
):
    return SegmentScore(
        segment_id=segment_id,
        wc_name=wc,
        person_name=person,
        start_utc=start,
        end_utc=end,
        source="punch" if person else "unassigned",
        productive_minutes=minutes,
        actual_units=actual,
        goal_units=goal,
        runway_units=max(actual, goal),
        is_active=active,
        result=(
            "neutral"
            if person is None or goal <= 0
            else "ahead" if actual >= goal else "behind"
        ),
    )


def test_display_scores_join_same_worker_across_scheduled_lunch():
    morning = _score(
        "Jesus G.", t(12), t(16), actual=311, goal=260, minutes=210
    )
    afternoon = _score(
        "Jesus G.", t(16, 30), t(19, 30),
        actual=256, goal=260, active=True, minutes=210, segment_id=1,
    )

    (joined,) = coalesce_display_scores(
        (morning, afternoon), ignored_gaps=((t(16), t(16, 30)),)
    )

    assert (joined.start_utc, joined.end_utc) == (t(12), t(19, 30))
    assert (joined.actual_units, joined.goal_units) == (567, 520)
    assert joined.productive_minutes == 420
    assert (joined.result, joined.runway_units, joined.is_active) == (
        "ahead", 567, True,
    )


def test_display_scores_keep_productive_gap_and_lunch_transfer_split():
    productive_gap = coalesce_display_scores(
        (
            _score("Jesus G.", t(12), t(15), actual=100, goal=100),
            _score("Jesus G.", t(16), t(17), actual=40, goal=50, segment_id=1),
        ),
        ignored_gaps=((t(15, 15), t(15, 30)),),
    )
    lunch_transfer = coalesce_display_scores(
        (
            _score("Jesus G.", t(12), t(16), actual=100, goal=100),
            _score("Ana M.", t(16, 30), t(17), actual=40, goal=50, segment_id=1),
        ),
        ignored_gaps=((t(16), t(16, 30)),),
    )

    assert len(productive_gap) == 2
    assert worker_coverage_is_split(
        productive_gap, window_start_utc=t(12), window_end_utc=t(17)
    ) is True
    assert [row.person_name for row in lunch_transfer] == ["Jesus G.", "Ana M."]
```

- [ ] **Step 2: Write failing split-policy tests, including the live lunch boundary**

```python
def test_worker_coverage_split_policy_ignores_scheduled_break_boundaries():
    full = _score("Jesus G.", t(12), t(19), actual=500, goal=480, active=True)
    lunch_now = _score("Jesus G.", t(12), t(16), actual=300, goal=260)
    left_early = _score("Humberto S.", t(12), t(18), actual=516, goal=700)
    late_start = _score("Ana M.", t(13), t(19), actual=400, goal=360, active=True)
    second_worker = _score(
        "Ana M.", t(16, 30), t(19),
        actual=200, goal=180, active=True, segment_id=1,
    )
    overlapping_worker = _score(
        "Ana M.", t(14), t(15), actual=50, goal=60, segment_id=3
    )
    unassigned = _score(
        None, t(16, 10), t(16, 10), actual=3, goal=0, segment_id=2
    )

    assert worker_coverage_is_split(
        (full,), window_start_utc=t(12), window_end_utc=t(19)
    ) is False
    assert worker_coverage_is_split(
        (lunch_now,),
        window_start_utc=t(12),
        window_end_utc=t(16, 15),
        ignored_gaps=((t(16), t(16, 30)),),
    ) is False
    assert worker_coverage_is_split(
        (left_early,), window_start_utc=t(12), window_end_utc=t(19)
    ) is True
    assert worker_coverage_is_split(
        (late_start,), window_start_utc=t(12), window_end_utc=t(19)
    ) is True
    assert worker_coverage_is_split(
        (lunch_now, second_worker),
        window_start_utc=t(12),
        window_end_utc=t(19),
        ignored_gaps=((t(16), t(16, 30)),),
    ) is True
    assert worker_coverage_is_split(
        (full, overlapping_worker),
        window_start_utc=t(12),
        window_end_utc=t(19),
    ) is True
    assert worker_coverage_is_split(
        (full, unassigned), window_start_utc=t(12), window_end_utc=t(19)
    ) is False
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q \
  tests/test_production_segments.py::test_display_scores_join_same_worker_across_scheduled_lunch \
  tests/test_production_segments.py::test_display_scores_keep_productive_gap_and_lunch_transfer_split \
  tests/test_production_segments.py::test_worker_coverage_split_policy_ignores_scheduled_break_boundaries
```

Expected: collection fails because `coalesce_display_scores` and `worker_coverage_is_split` do not exist.

- [ ] **Step 4: Implement result reuse, display-score coalescing, and split classification**

Add these pure helpers to `src/zira_dashboard/production_segments.py`, and use `_segment_result` from `score_work_segments` so merged and original scores classify results identically:

```python
def _segment_result(
    person_name: str | None, actual_units: float, goal_units: float
) -> SegmentResult:
    if person_name is None or goal_units <= 0:
        return "neutral"
    return "ahead" if actual_units >= goal_units else "behind"


def _score_order(row: SegmentScore) -> tuple[bool, float, int]:
    return (
        row.start_utc is None,
        row.start_utc.timestamp() if row.start_utc is not None else float("inf"),
        row.segment_id,
    )


def _gap_is_ignored(
    start_utc: datetime,
    end_utc: datetime,
    ignored_gaps: Sequence[tuple[datetime, datetime]],
) -> bool:
    if end_utc <= start_utc:
        return True
    return any(
        gap_start <= start_utc and end_utc <= gap_end
        for gap_start, gap_end in ignored_gaps
    )


def _can_join_display_scores(
    left: SegmentScore,
    right: SegmentScore,
    ignored_gaps: Sequence[tuple[datetime, datetime]],
) -> bool:
    return bool(
        left.person_name is not None
        and left.person_name == right.person_name
        and left.wc_name == right.wc_name
        and left.end_utc is not None
        and right.start_utc is not None
        and _gap_is_ignored(left.end_utc, right.start_utc, ignored_gaps)
    )


def _join_display_scores(left: SegmentScore, right: SegmentScore) -> SegmentScore:
    actual = left.actual_units + right.actual_units
    goal = left.goal_units + right.goal_units
    return SegmentScore(
        segment_id=min(left.segment_id, right.segment_id),
        wc_name=left.wc_name,
        person_name=left.person_name,
        start_utc=min(
            value for value in (left.start_utc, right.start_utc) if value is not None
        ),
        end_utc=max(
            value for value in (left.end_utc, right.end_utc) if value is not None
        ),
        source=left.source if left.source == right.source else "mixed",
        productive_minutes=left.productive_minutes + right.productive_minutes,
        actual_units=actual,
        goal_units=goal,
        runway_units=max(actual, goal),
        is_active=left.is_active or right.is_active,
        result=_segment_result(left.person_name, actual, goal),
    )


def coalesce_display_scores(
    scores: Sequence[SegmentScore],
    *,
    ignored_gaps: Sequence[tuple[datetime, datetime]] = (),
) -> tuple[SegmentScore, ...]:
    """Join administrative break splits without changing sample credit."""
    named = sorted(
        (score for score in scores if score.person_name is not None),
        key=_score_order,
    )
    unassigned = [score for score in scores if score.person_name is None]
    merged: list[SegmentScore] = []
    for score in named:
        if merged and _can_join_display_scores(merged[-1], score, ignored_gaps):
            merged[-1] = _join_display_scores(merged[-1], score)
        else:
            merged.append(score)
    return tuple(sorted([*merged, *unassigned], key=_score_order))


def worker_coverage_is_split(
    scores: Sequence[SegmentScore],
    *,
    window_start_utc: datetime,
    window_end_utc: datetime,
    ignored_gaps: Sequence[tuple[datetime, datetime]] = (),
) -> bool:
    """Whether named-worker coverage needs independent runway presentation."""
    named = [score for score in scores if score.person_name is not None]
    if not named:
        return False
    if len(named) != 1:
        return True
    score = named[0]
    if score.start_utc is None or score.end_utc is None:
        return True
    starts_in_time = score.start_utc <= window_start_utc or _gap_is_ignored(
        window_start_utc, score.start_utc, ignored_gaps
    )
    ends_in_time = score.end_utc >= window_end_utc or _gap_is_ignored(
        score.end_utc, window_end_utc, ignored_gaps
    )
    return not (starts_in_time and ends_in_time)
```

In `score_work_segments`, replace the inline result branches with:

```python
result = _segment_result(credit.person_name, credit.actual_units, goal)
```

- [ ] **Step 5: Run Task 1 tests and Ruff**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q tests/test_production_segments.py
.venv/bin/ruff check src/zira_dashboard/production_segments.py tests/test_production_segments.py
```

Expected: all production-segment tests pass and Ruff prints `All checks passed!`.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/zira_dashboard/production_segments.py tests/test_production_segments.py
git commit -m "feat: normalize scheduled-break worker segments"
```

---

### Task 2: Carry the Split Decision Through Department Data

**Files:**
- Modify: `src/zira_dashboard/routes/departments.py:458-505,692,940`
- Modify: `src/zira_dashboard/recycling_range.py:6-81`
- Modify: `tests/test_department_operator_labels.py`
- Modify: `tests/test_recycling_range.py`

**Interfaces:**
- Consumes: `coalesce_display_scores`, `worker_coverage_is_split`, and the route's existing `breaks_utc`, `window_start_utc`, and `window_end_utc` values.
- Produces: `_prepare_segment_display(...) -> tuple[dict[str, tuple[dict, ...]], dict[str, bool], dict[str, str]]`, per-day `per_wc_segment_display: dict[str, bool]`, aggregated `single_day_segment_display: dict[str, bool]`, and lunch-continuity operator labels on a live board.

- [ ] **Step 1: Write failing route and aggregation assertions**

In `tests/test_department_operator_labels.py`, extend the existing transfer test after its score assertions:

```python
assert live["per_wc_segment_display"] == {
    "Repair 2": True,
    "Dismantler 2": True,
}
```

Add this focused route-preparation regression:

```python
def test_department_segment_display_keeps_scheduled_lunch_continuous():
    from zira_dashboard.production_segments import SegmentScore
    from zira_dashboard.routes import departments

    def score(segment_id, start, end, actual, goal, *, active=False):
        return SegmentScore(
            segment_id=segment_id,
            wc_name="Dismantler 1",
            person_name="Jesus G.",
            start_utc=start,
            end_utc=end,
            source="punch",
            productive_minutes=goal,
            actual_units=actual,
            goal_units=goal,
            runway_units=max(actual, goal),
            is_active=active,
            result="ahead" if actual >= goal else "behind",
        )

    morning = score(
        0,
        datetime(2026, 6, 2, 12, tzinfo=timezone.utc),
        datetime(2026, 6, 2, 16, tzinfo=timezone.utc),
        311,
        260,
    )
    afternoon = score(
        1,
        datetime(2026, 6, 2, 16, 30, tzinfo=timezone.utc),
        datetime(2026, 6, 2, 19, 30, tzinfo=timezone.utc),
        256,
        260,
        active=True,
    )

    views, decisions, live_workers = departments._prepare_segment_display(
        {"Dismantler 1": (morning, afternoon)},
        break_windows=((
            datetime(2026, 6, 2, 16, tzinfo=timezone.utc),
            datetime(2026, 6, 2, 16, 30, tzinfo=timezone.utc),
        ),),
        window_start_utc=datetime(2026, 6, 2, 12, tzinfo=timezone.utc),
        window_end_utc=datetime(2026, 6, 2, 19, 30, tzinfo=timezone.utc),
        is_live=True,
    )

    assert len(views["Dismantler 1"]) == 1
    assert views["Dismantler 1"][0]["time_label"] == "since 7a"
    assert views["Dismantler 1"][0]["goal_units"] == 520
    assert decisions == {"Dismantler 1": False}
    assert live_workers == {"Dismantler 1": "Jesus G."}
```

In `tests/test_recycling_range.py`, add `per_wc_segment_display` to `_day`:

```python
"per_wc_segment_display": {"Dismantler 1": False},
```

Then extend the single-day and range assertions:

```python
assert result.single_day_segment_display is item["per_wc_segment_display"]
assert result.single_day_segment_display == {}
```

- [ ] **Step 2: Run the route and aggregation tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q \
  tests/test_department_operator_labels.py \
  tests/test_recycling_range.py
```

Expected: failures show the missing `per_wc_segment_display` data and `single_day_segment_display` aggregate field.

- [ ] **Step 3: Add the focused route-preparation boundary**

Add this helper beside `_segment_view` in `routes/departments.py`:

```python
def _prepare_segment_display(
    scored_by_wc,
    *,
    break_windows,
    window_start_utc,
    window_end_utc,
    is_live,
):
    display_scored = {
        wc_name: production_segments.coalesce_display_scores(
            scores, ignored_gaps=break_windows
        )
        for wc_name, scores in scored_by_wc.items()
    }
    decisions = {
        wc_name: production_segments.worker_coverage_is_split(
            scores,
            window_start_utc=window_start_utc,
            window_end_utc=window_end_utc,
            ignored_gaps=break_windows,
        )
        for wc_name, scores in display_scored.items()
    }
    views = {
        wc_name: tuple(_segment_view(score) for score in scores)
        for wc_name, scores in display_scored.items()
    }
    continuous_live_workers = {
        wc_name: next(
            score.person_name
            for score in scores
            if score.person_name is not None
        )
        for wc_name, scores in display_scored.items()
        if is_live
        and not decisions.get(wc_name, False)
        and any(score.person_name is not None for score in scores)
    }
    return views, decisions, continuous_live_workers
```

Call the helper immediately after `score_work_segments`:

```python
per_wc_segments, per_wc_segment_display, continuous_live_workers = (
    _prepare_segment_display(
        scored,
        break_windows=breaks_utc,
        window_start_utc=window_start_utc,
        window_end_utc=window_end_utc,
        is_live=is_live_dashboard,
    )
)
```

The fail-soft branch must initialize every new value:

```python
except Exception:
    per_wc_segments = {}
    per_wc_segment_display = {}
    continuous_live_workers = {}
```

After creating `per_wc_who`, restore the same worker during the configured break only when coverage is non-split:

```python
for wc_name, person_name in continuous_live_workers.items():
    if not per_wc_who.get(wc_name):
        per_wc_who[wc_name] = person_name
```

Add the new map to the returned day dictionary:

```python
"per_wc_segment_display": per_wc_segment_display,
```

- [ ] **Step 4: Preserve the single-day decision in `RangeAggregate`**

Add the dataclass field:

```python
single_day_segment_display: dict[str, bool]
```

Initialize, populate, and return it in `aggregate_range`:

```python
single_day_segment_display: dict[str, bool] = {}

# inside `if not is_range`
single_day_segment_display = item.get("per_wc_segment_display", {})

# in RangeAggregate(...)
single_day_segment_display=single_day_segment_display,
```

Pass it into both `build_bars` calls in `routes/departments.py`:

```python
agg_segment_display=aggregate.single_day_segment_display,
```

- [ ] **Step 5: Run Task 2 tests and Ruff**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q \
  tests/test_department_operator_labels.py \
  tests/test_recycling_range.py
.venv/bin/ruff check \
  src/zira_dashboard/routes/departments.py \
  src/zira_dashboard/recycling_range.py \
  tests/test_department_operator_labels.py \
  tests/test_recycling_range.py
```

Expected: tests pass and Ruff prints `All checks passed!`.

- [ ] **Step 6: Commit Task 2**

```bash
git add \
  src/zira_dashboard/routes/departments.py \
  src/zira_dashboard/recycling_range.py \
  tests/test_department_operator_labels.py \
  tests/test_recycling_range.py
git commit -m "feat: carry worker split display decisions"
```

---

### Task 3: Select Legacy or Split Bar Geometry and Ship the Fix

**Files:**
- Modify: `src/zira_dashboard/recycling_data.py:69-173`
- Modify: `tests/test_recycling_data.py`
- Modify: `tests/test_new_dashboard_template.py`
- Modify: `tests/test_dashboards_polish.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `agg_segment_display: dict[str, bool]` from Task 2.
- Produces: bar rows where `uses_split_format` is explicit, `has_segments` means segment markup should render, and uninterrupted rows restore aggregate scaling and the normal target line.

- [ ] **Step 1: Write failing bar-model tests for legacy, split, and unassigned behavior**

Update existing segmented `build_bars` tests to pass:

```python
agg_segment_display={"Repair 4": True},
```

Add this uninterrupted-worker regression to `tests/test_recycling_data.py`:

```python
def test_build_bars_uses_legacy_geometry_for_uninterrupted_worker():
    bars = rd.build_bars(
        "Dismantler",
        agg_active_names={"Dismantler 1"},
        agg_category={"Dismantler 1": "Dismantler"},
        agg_units={"Dismantler 1": 567},
        agg_expected={"Dismantler 1": 520.0},
        agg_who_today={"Dismantler 1": "Jesus G."},
        is_range=False,
        agg_downtime={},
        agg_segments={
            "Dismantler 1": ({
                "person_name": "Jesus G.",
                "person_label": "Jesus G.",
                "actual_units": 567.0,
                "goal_units": 520.0,
                "runway_units": 567.0,
                "is_active": True,
                "result": "ahead",
                "result_label": "47 ahead",
            },),
        },
        agg_segment_display={"Dismantler 1": False},
        is_live=True,
    )

    (bar,) = bars
    assert bar["uses_split_format"] is False
    assert bar["has_segments"] is False
    assert bar["who"] == "Jesus G."
    assert bar["target_pct"] is not None
    assert bar["no_one_here_now"] is False
```

Add or extend assertions for the existing Humberto-vacancy test:

```python
assert bars[0]["uses_split_format"] is True
assert bars[0]["has_segments"] is True
assert bars[0]["target_pct"] is None
assert bars[0]["no_one_here_now"] is True
```

- [ ] **Step 2: Write failing shared-template regressions**

Add this helper and these tests to `tests/test_new_dashboard_template.py`:

```python
def _legacy_worker_bar():
    bar = _segmented_bar()
    bar.update(
        name="Dismantler 1",
        who="Jesus G.",
        units=567,
        expected=520,
        pct=90.0,
        target_pct=80.0,
        has_segments=False,
        has_worker_history=True,
        uses_split_format=False,
        no_one_here_now=False,
    )
    return bar


def test_new_uninterrupted_worker_uses_legacy_horizontal_bar():
    html = _render_new(new_bars=[_legacy_worker_bar()])
    assert 'class="bar-fill"' in html
    assert 'class="bar-target-line"' in html
    assert 'class="worker-segment-fill' not in html
    assert "Jesus G." in html


def test_new_uninterrupted_worker_uses_legacy_vertical_bar():
    html = _render_new(
        customs={"new-bars": {"orientation": "vertical"}},
        new_bars=[_legacy_worker_bar()],
    )
    assert 'class="vbar-fill"' in html
    assert 'class="vbar-target-line"' in html
    assert 'class="vworker-segment-fill' not in html


def test_new_tv_uninterrupted_worker_keeps_legacy_bar():
    html = _render_new(tv_mode=True, new_bars=[_legacy_worker_bar()])
    assert 'class="bar-fill"' in html
    assert 'class="worker-segment-fill' not in html
    assert "Jesus G." in html
```

Keep the existing split-runway tests unchanged so both formats remain covered. In `tests/test_dashboards_polish.py`, extend `test_recycling_transferred_worker_moves_into_bar_and_left_says_no_one_here_now` with:

```python
assert "worker-segment-goal completed" in html
```

- [ ] **Step 3: Run the model and template tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q \
  tests/test_recycling_data.py \
  tests/test_new_dashboard_template.py \
  tests/test_dashboards_polish.py
```

Expected: the uninterrupted row still reports `has_segments=True` or lacks `uses_split_format`, proving the legacy selection is not implemented.

- [ ] **Step 4: Make `build_bars` use the explicit decision**

Add the keyword argument:

```python
agg_segment_display: dict | None = None,
```

Normalize it before scale calculation:

```python
agg_segments = agg_segments or {}
agg_segment_display = agg_segment_display or {}
for row in out:
    row["uses_split_format"] = bool(
        not is_range and agg_segment_display.get(row["name"], False)
    )
```

Select each row's scale input with the explicit decision:

```python
base = (
    max(
        spans[row["name"]]
        if row["uses_split_format"] and spans[row["name"]] > 0
        else max(float(row["units"]), float(row["expected"]))
        for row in out
    )
    if out
    else 0.0
)
```

After geometry is built, make `has_segments` a rendering decision and restore the normal target line for uninterrupted rows:

```python
row["segments"] = geometry
row["has_segments"] = bool(row["uses_split_format"] and geometry)
row["has_worker_history"] = any(
    segment.get("person_name") for segment in geometry
)
row["no_one_here_now"] = bool(
    is_live
    and row["has_segments"]
    and not row["who"]
    and row["has_worker_history"]
)
row["pct"] = float(row["units"]) / scale * 100.0
row["target_pct"] = (
    float(row["expected"]) / scale * 100.0
    if scale and has_target_line and not row["has_segments"]
    else None
)
```

No Jinja changes are required: the shared template already selects the legacy markup when `has_segments` is false and the segment markup when it is true.

- [ ] **Step 5: Add child-friendly What's New text**

Add a new newest-first section under `## 2026-08-20` in `CHANGELOG.md`, without editing prior entries:

```markdown
### Lunch stays one shift

#### Fixes

- **Lunch no longer splits one person's work into two bars.** If the same person comes back to the same work center after a planned break, the normal bar stays on screen. A real move to another work center still shows each person's part.
```

- [ ] **Step 6: Run focused and regression verification**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q \
  tests/test_production_segments.py \
  tests/test_production_history.py \
  tests/test_department_operator_labels.py \
  tests/test_recycling_range.py \
  tests/test_recycling_data.py \
  tests/test_new_dashboard_template.py \
  tests/test_dashboards_polish.py \
  tests/test_productive_minutes_window.py \
  tests/test_assignment_windows_breakdown.py \
  tests/test_production_history_breakdown.py \
  tests/test_recycling_scaling_static.py \
  tests/test_recycling_leaderboard_static.py
```

Expected: focused production, goal, data, and rendering tests pass; database-gated tests may skip when `DATABASE_URL` is empty.

Run:

```bash
.venv/bin/ruff check \
  src/zira_dashboard/production_segments.py \
  src/zira_dashboard/routes/departments.py \
  src/zira_dashboard/recycling_range.py \
  src/zira_dashboard/recycling_data.py \
  tests/test_production_segments.py \
  tests/test_department_operator_labels.py \
  tests/test_recycling_range.py \
  tests/test_recycling_data.py \
  tests/test_new_dashboard_template.py \
  tests/test_dashboards_polish.py
git diff --check
```

Expected: Ruff prints `All checks passed!` and `git diff --check` prints nothing.

- [ ] **Step 7: Commit and push the implementation**

```bash
git add \
  CHANGELOG.md \
  src/zira_dashboard/recycling_data.py \
  tests/test_recycling_data.py \
  tests/test_new_dashboard_template.py \
  tests/test_dashboards_polish.py
git commit -m "fix: keep scheduled lunch in one production bar"
git push origin main
```

- [ ] **Step 8: Confirm delivery state**

Run:

```bash
git status --short --branch
git rev-list --left-right --count main...origin/main
git log -6 --oneline --decorate
```

Expected: `main...origin/main` has zero ahead and zero behind; only the user's pre-existing untracked files remain.
