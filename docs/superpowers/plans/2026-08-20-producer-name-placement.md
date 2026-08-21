# Producer Name Placement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the only named producer in the normal left-side work-center label while retaining scored stop and finish markers, and show names inside the bar only when two or more distinct people produced there.

**Architecture:** Derive an ordered tuple of distinct named producers from normalized `SegmentScore` records, carry it through the existing single-day range aggregate, and let `build_bars` expose explicit sole-producer and multi-producer presentation fields. The shared horizontal/vertical template consumes those fields without counting people itself, while production credit, split geometry, current occupancy, lunch handling, and red/green scoring remain unchanged.

**Tech Stack:** Python 3.12, FastAPI/Starlette, Jinja2, pytest, Ruff.

## Global Constraints

- Zero named producers keep the existing unassigned presentation.
- Exactly one distinct named producer appears in the normal left-side label whether active or stopped.
- A sole producer keeps all scored segment geometry, stop points, finish lines, time, actual/goal, and red/green results.
- Two or more distinct named producers keep every producer name inside that producer's scored bar segment.
- Count unique non-empty `person_name` values, not segment records or current assignments.
- Unassigned production does not count as a person.
- Current occupied/vacant state remains independent from historical producer name placement.
- Scheduled lunch normalization and goal subtraction remain unchanged.
- Horizontal, vertical, screen, TV, Recycling, and New single-day views use the same rule.
- Multi-day range views remain unchanged.
- Follow test-driven development: add each regression first, run it red for the expected missing behavior, then make the smallest production change.
- Preserve unrelated working-tree files and concurrent commits.

---

## File Map

- `src/zira_dashboard/production_segments.py`: owns the pure, ordered distinct-producer calculation for normalized scores.
- `src/zira_dashboard/routes/departments.py`: derives producer tuples during segment display preparation, returns them in day data, and passes the aggregate to Recycling and New bars.
- `src/zira_dashboard/recycling_range.py`: carries single-day producer tuples without leaking them into multi-day ranges.
- `src/zira_dashboard/recycling_data.py`: converts producer tuples plus current occupancy into explicit per-row presentation fields.
- `src/zira_dashboard/templates/_department_dashboard_widgets.html`: places sole names on the left and multi-producer names inside scored segments for horizontal and vertical layouts.
- `tests/test_production_segments.py`: proves distinct-person counting, ordering, duplicate suppression, and unassigned exclusion.
- `tests/test_department_operator_labels.py`: proves day-data producer identity is based on credited production rather than current assignment.
- `tests/test_recycling_range.py`: proves single-day producer tuples propagate and multi-day ranges discard them.
- `tests/test_recycling_data.py`: proves bar rows separate producer name placement from active/vacant state and scored geometry.
- `tests/test_new_dashboard_template.py`: proves horizontal, vertical, and TV shared markup follows the rule.
- `tests/test_dashboards_polish.py`: full-page regression for stopped Humberto on Repair 4 when a database test environment is available.
- `CHANGELOG.md`: explains the visible change in child-friendly language.

---

### Task 1: Derive distinct named producers from normalized scores

**Files:**
- Modify: `src/zira_dashboard/production_segments.py:52-160`
- Test: `tests/test_production_segments.py:1-160`

**Interfaces:**
- Consumes: `Sequence[SegmentScore]`, after `coalesce_display_scores(...)` has normalized scheduled-break records.
- Produces: `distinct_named_producers(scores: Sequence[SegmentScore]) -> tuple[str, ...]`, ordered by the existing `_score_order` and excluding empty/unassigned names and repeated appearances by the same person.

- [ ] **Step 1: Write the failing pure-policy test**

Add `distinct_named_producers` to the import list in `tests/test_production_segments.py`, then add:

```python
def test_distinct_named_producers_counts_people_not_segments_or_unassigned():
    humberto_morning = _score(
        "Humberto S.", t(12), t(15), actual=200, goal=240, segment_id=0
    )
    unassigned = _score(
        None, t(15), t(15), actual=3, goal=0, segment_id=1
    )
    empty_name = _score(
        "", t(15), t(15), actual=0, goal=0, segment_id=4
    )
    humberto_afternoon = _score(
        "Humberto S.", t(16), t(18), actual=316, goal=460, segment_id=2
    )
    ana = _score(
        "Ana M.", t(18), t(19), actual=40, goal=50, segment_id=3
    )

    assert distinct_named_producers(
        (humberto_morning, unassigned, empty_name, humberto_afternoon)
    ) == ("Humberto S.",)
    assert distinct_named_producers(
        (ana, humberto_afternoon, unassigned, humberto_morning)
    ) == ("Humberto S.", "Ana M.")
    assert distinct_named_producers((unassigned, empty_name)) == ()
```

- [ ] **Step 2: Run the test and verify the missing API is the only failure**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q tests/test_production_segments.py::test_distinct_named_producers_counts_people_not_segments_or_unassigned
```

Expected: test collection fails because `distinct_named_producers` cannot yet be imported.

- [ ] **Step 3: Implement ordered unique-person extraction**

Add this next to `coalesce_display_scores` in `src/zira_dashboard/production_segments.py`:

```python
def distinct_named_producers(
    scores: Sequence[SegmentScore],
) -> tuple[str, ...]:
    """Return named producers once each, in scored-segment time order."""
    names: list[str] = []
    seen: set[str] = set()
    for score in sorted(scores, key=_score_order):
        person_name = score.person_name
        if not person_name or person_name in seen:
            continue
        seen.add(person_name)
        names.append(person_name)
    return tuple(names)
```

- [ ] **Step 4: Run the production-segment tests green**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q tests/test_production_segments.py
.venv/bin/ruff check src/zira_dashboard/production_segments.py tests/test_production_segments.py
```

Expected: all tests in the file pass and Ruff reports no issues.

- [ ] **Step 5: Commit the pure producer policy**

```bash
git add src/zira_dashboard/production_segments.py tests/test_production_segments.py
git commit -m "feat: identify distinct work center producers"
```

---

### Task 2: Carry producer identity through day and range data

**Files:**
- Modify: `src/zira_dashboard/routes/departments.py:96-130, 510-555`
- Modify: `src/zira_dashboard/recycling_range.py:1-105`
- Test: `tests/test_department_operator_labels.py:20-190`
- Test: `tests/test_recycling_range.py:1-65`

**Interfaces:**
- Consumes: `production_segments.distinct_named_producers(scores)` and normalized `display_scored` values from `_prepare_segment_display`.
- Produces: `per_wc_producers: dict[str, tuple[str, ...]]` in single-day data and `RangeAggregate.single_day_producers: dict[str, tuple[str, ...]]` for bar construction.

- [ ] **Step 1: Write failing day-data and range propagation assertions**

In `tests/test_department_operator_labels.py`, extend `test_department_day_data_shows_transfer_at_current_wc_but_keeps_both_active` after the existing `per_wc_segment_display` assertion:

```python
    assert live["per_wc_producers"] == {
        "Repair 2": ("Jesus G.",),
        "Dismantler 2": ("Jesus G.",),
    }
```

In the `_day` fixture in `tests/test_recycling_range.py`, add:

```python
        "per_wc_producers": {"Dismantler 1": (who,)},
```

Then extend `test_single_day_keeps_who_and_assignments` with:

```python
    assert result.single_day_producers is item["per_wc_producers"]
```

And extend `test_multi_day_sums_work_center_metrics_without_single_day_labels` with:

```python
    assert result.single_day_producers == {}
```

- [ ] **Step 2: Run the focused tests red**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q \
  tests/test_department_operator_labels.py::test_department_day_data_shows_transfer_at_current_wc_but_keeps_both_active \
  tests/test_recycling_range.py::test_single_day_keeps_who_and_assignments \
  tests/test_recycling_range.py::test_multi_day_sums_work_center_metrics_without_single_day_labels
```

Expected: failures report missing `per_wc_producers` and missing `single_day_producers`.

- [ ] **Step 3: Derive producer tuples once during display preparation**

In `_prepare_segment_display` in `src/zira_dashboard/routes/departments.py`, derive producer identity from `display_scored` and return it before `continuous_live_workers`:

```python
    producers = {
        wc_name: production_segments.distinct_named_producers(scores)
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
    return views, decisions, producers, continuous_live_workers
```

Update the `_department_day_data` unpacking and fail-soft branch:

```python
        (
            per_wc_segments,
            per_wc_segment_display,
            per_wc_producers,
            continuous_live_workers,
        ) = _prepare_segment_display(
            scored,
            break_windows=breaks_utc,
            window_start_utc=window_start_utc,
            window_end_utc=window_end_utc,
            is_live=is_live_dashboard,
        )
    except Exception:
        per_wc_segments = {}
        per_wc_segment_display = {}
        per_wc_producers = {}
        continuous_live_workers = {}
```

Add the mapping to the returned day dictionary:

```python
        "per_wc_segments": per_wc_segments,
        "per_wc_segment_display": per_wc_segment_display,
        "per_wc_producers": per_wc_producers,
```

- [ ] **Step 4: Carry only single-day producer tuples through `RangeAggregate`**

Add the field to `RangeAggregate` in `src/zira_dashboard/recycling_range.py`:

```python
    single_day_segments: dict[str, tuple[dict, ...]]
    single_day_segment_display: dict[str, bool]
    single_day_producers: dict[str, tuple[str, ...]]
    single_day_is_live: bool
```

Initialize, populate only for non-range data, and return it:

```python
    single_day_segments: dict[str, tuple[dict, ...]] = {}
    single_day_segment_display: dict[str, bool] = {}
    single_day_producers: dict[str, tuple[str, ...]] = {}
    single_day_is_live = False
```

```python
        if not is_range:
            agg_who_today = item["per_wc_who"]
            schedule_today_assignments = item["schedule_assignments"]
            single_day_segments = item.get("per_wc_segments", {})
            single_day_segment_display = item.get("per_wc_segment_display", {})
            single_day_producers = item.get("per_wc_producers", {})
            single_day_is_live = bool(item.get("is_live_dashboard", False))
```

```python
        single_day_segments=single_day_segments,
        single_day_segment_display=single_day_segment_display,
        single_day_producers=single_day_producers,
        single_day_is_live=single_day_is_live,
```

- [ ] **Step 5: Run route/range tests green**

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

Expected: all focused tests pass and Ruff reports no issues.

- [ ] **Step 6: Commit the data-flow change**

```bash
git add \
  src/zira_dashboard/routes/departments.py \
  src/zira_dashboard/recycling_range.py \
  tests/test_department_operator_labels.py \
  tests/test_recycling_range.py
git commit -m "feat: carry work center producer identity"
```

---

### Task 3: Separate producer name placement from current occupancy in bar rows

**Files:**
- Modify: `src/zira_dashboard/routes/departments.py:734-747, 984-996`
- Modify: `src/zira_dashboard/recycling_data.py:69-180`
- Test: `tests/test_recycling_data.py:170-335`

**Interfaces:**
- Consumes: `agg_producers: dict[str, tuple[str, ...]]`, existing `agg_who_today`, `agg_segments`, and `agg_segment_display`.
- Produces per row: `producer_names: tuple[str, ...]`, `sole_producer_name: str | None`, `show_segment_worker_names: bool`, unchanged `who` as current occupancy, and `no_one_here_now` only for a vacant multi-producer segmented row.

- [ ] **Step 1: Write failing bar-model tests for stopped sole and multiple producers**

Update `test_build_bars_places_independent_runways_on_one_scale` to pass:

```python
        agg_producers={"Repair 4": ("Humberto S.", "Ana M.")},
```

Then add these assertions:

```python
    assert bar["producer_names"] == ("Humberto S.", "Ana M.")
    assert bar["sole_producer_name"] is None
    assert bar["show_segment_worker_names"] is True
```

Rename `test_build_bars_marks_live_station_empty_when_history_remains` to
`test_build_bars_keeps_stopped_sole_producer_on_left_with_segment_geometry`,
pass this new keyword:

```python
        agg_producers={"Repair 4": ("Humberto S.",)},
```

Replace its final expectations with:

```python
    bar = bars[0]
    assert bar["uses_split_format"] is True
    assert bar["has_segments"] is True
    assert bar["target_pct"] is None
    assert bar["who"] is None
    assert bar["sole_producer_name"] == "Humberto S."
    assert bar["show_segment_worker_names"] is False
    assert bar["no_one_here_now"] is False
    assert bar["segments"][0]["finish_pct"] is not None
```

Add a focused unassigned-credit test:

```python
def test_build_bars_unassigned_units_do_not_displace_sole_named_producer():
    bars = rd.build_bars(
        "Repair",
        agg_active_names={"Repair 1"},
        agg_category={"Repair 1": "Repair"},
        agg_units={"Repair 1": 638},
        agg_expected={"Repair 1": 611},
        agg_who_today={},
        is_range=False,
        agg_downtime={},
        agg_segments={
            "Repair 1": (
                {
                    "person_name": "Jose O.",
                    "actual_units": 635.0,
                    "goal_units": 611.0,
                    "runway_units": 635.0,
                    "is_active": False,
                    "result": "ahead",
                },
                {
                    "person_name": None,
                    "actual_units": 3.0,
                    "goal_units": 0.0,
                    "runway_units": 3.0,
                    "is_active": False,
                    "result": "neutral",
                },
            )
        },
        agg_segment_display={"Repair 1": True},
        agg_producers={"Repair 1": ("Jose O.",)},
        is_live=True,
    )

    (bar,) = bars
    assert bar["sole_producer_name"] == "Jose O."
    assert bar["show_segment_worker_names"] is False
    assert bar["no_one_here_now"] is False
```

- [ ] **Step 2: Run the bar-model tests red**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q \
  tests/test_recycling_data.py::test_build_bars_places_independent_runways_on_one_scale \
  tests/test_recycling_data.py::test_build_bars_keeps_stopped_sole_producer_on_left_with_segment_geometry \
  tests/test_recycling_data.py::test_build_bars_unassigned_units_do_not_displace_sole_named_producer
```

Expected: failures report that `build_bars` does not accept `agg_producers` or that the new row fields are absent.

- [ ] **Step 3: Add producer presentation fields without changing occupancy**

Add the optional keyword to `build_bars` in `src/zira_dashboard/recycling_data.py`:

```python
    agg_segments: dict | None = None,
    agg_segment_display: dict | None = None,
    agg_producers: dict[str, tuple[str, ...]] | None = None,
    is_live: bool = True,
```

Normalize it beside the other optional inputs and add explicit fields before scale calculation:

```python
    agg_segments = agg_segments or {}
    agg_segment_display = agg_segment_display or {}
    agg_producers = agg_producers or {}
    for row in out:
        row["uses_split_format"] = bool(
            not is_range and agg_segment_display.get(row["name"], False)
        )
        producer_names = (
            tuple(agg_producers.get(row["name"], ())) if not is_range else ()
        )
        row["producer_names"] = producer_names
        row["sole_producer_name"] = (
            producer_names[0] if len(producer_names) == 1 else None
        )
        row["show_segment_worker_names"] = len(producer_names) >= 2
```

Replace `no_one_here_now` with a current-occupancy check that only controls the multi-producer left label:

```python
        row["no_one_here_now"] = bool(
            is_live
            and row["has_segments"]
            and row["show_segment_worker_names"]
            and not row["who"]
            and row["has_worker_history"]
        )
```

Do not overwrite `row["who"]`; it remains the active/current worker used for occupancy and tooltips.

- [ ] **Step 4: Pass the producer map to Recycling and New bar calls**

Add the new keyword at both `build_bars` call sites in
`src/zira_dashboard/routes/departments.py`:

```python
            agg_segments=aggregate.single_day_segments,
            agg_segment_display=aggregate.single_day_segment_display,
            agg_producers=aggregate.single_day_producers,
            is_live=aggregate.single_day_is_live,
```

- [ ] **Step 5: Run all bar-model tests green**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q tests/test_recycling_data.py
.venv/bin/ruff check \
  src/zira_dashboard/routes/departments.py \
  src/zira_dashboard/recycling_data.py \
  tests/test_recycling_data.py
```

Expected: all bar tests pass and Ruff reports no issues.

- [ ] **Step 6: Commit the explicit bar presentation model**

```bash
git add \
  src/zira_dashboard/routes/departments.py \
  src/zira_dashboard/recycling_data.py \
  tests/test_recycling_data.py
git commit -m "feat: place sole producer beside work center"
```

---

### Task 4: Render sole and multiple producers consistently in shared markup

**Files:**
- Modify: `src/zira_dashboard/templates/_department_dashboard_widgets.html:1-165`
- Modify: `tests/test_new_dashboard_template.py:45-135, 280-345`
- Modify: `tests/test_dashboards_polish.py:100-155`
- Modify: `CHANGELOG.md:1-35`

**Interfaces:**
- Consumes per bar: `sole_producer_name`, `show_segment_worker_names`, current `who`, `no_one_here_now`, and existing segment dictionaries.
- Produces: left-side sole-producer labels; inside-bar names only for multi-producer named segments; visible unassigned-production labels; unchanged stop, finish, time, actual/goal, result, and range markup.

- [ ] **Step 1: Add complete single-producer and multi-producer render fixtures**

In `_segmented_bar` in `tests/test_new_dashboard_template.py`, add:

```python
        "producer_names": ("Humberto S.", "Ana M."),
        "sole_producer_name": None,
        "show_segment_worker_names": True,
```

Add this helper below `_segmented_bar`:

```python
def _stopped_sole_producer_bar():
    bar = _segmented_bar()
    bar.update(
        who=None,
        units=516,
        expected=700,
        producer_names=("Humberto S.",),
        sole_producer_name="Humberto S.",
        show_segment_worker_names=False,
        no_one_here_now=False,
        segments=[bar["segments"][0]],
    )
    return bar
```

Also add the three producer fields to `_legacy_worker_bar`:

```python
        producer_names=("Jesus G.",),
        sole_producer_name="Jesus G.",
        show_segment_worker_names=False,
```

- [ ] **Step 2: Write failing shared-template assertions**

Add these tests to `tests/test_new_dashboard_template.py`:

```python
def test_stopped_sole_producer_name_is_left_while_finish_marker_stays_in_bar():
    html = _render_new(new_bars=[_stopped_sole_producer_bar()])

    assert '<span class="name-primary">Humberto S.</span>' in html
    assert '<span class="name-secondary">Repair 4</span>' in html
    assert 'class="worker-segment-goal completed"' in html
    assert "7a-2:33p" in html and "516/700" in html and "184 behind" in html
    assert '<span class="worker-segment-person">Humberto S.</span>' not in html
    assert "No one here now" not in html


def test_multiple_producers_keep_each_name_inside_horizontal_bar():
    html = _render_new(new_bars=[_segmented_bar()])

    assert '<span class="worker-segment-person">Humberto S.</span>' in html
    assert '<span class="worker-segment-person">Ana M.</span>' in html
    assert '<span class="name-primary">Humberto S.</span>' not in html
    assert '<span class="name-primary">Ana M.</span>' not in html


def test_active_multi_producer_row_identifies_work_center_on_left():
    bar = _segmented_bar()
    bar.update(who="Ana M.", no_one_here_now=False)

    html = _render_new(new_bars=[bar])

    assert '<span class="name-primary">Repair 4</span>' in html
    assert '<span class="worker-segment-person">Humberto S.</span>' in html
    assert '<span class="worker-segment-person">Ana M.</span>' in html
    assert '<span class="name-primary">Ana M.</span>' not in html


def test_vacant_multi_producer_row_keeps_empty_status_on_left():
    html = _render_new(new_bars=[_segmented_bar()])

    assert '<span class="name-primary current-empty">No one here now</span>' in html
    assert '<span class="worker-segment-person">Humberto S.</span>' in html
    assert '<span class="worker-segment-person">Ana M.</span>' in html


def test_vertical_and_tv_views_keep_sole_producer_left_without_duplication():
    vertical = _render_new(
        customs={"new-bars": {"orientation": "vertical"}},
        new_bars=[_stopped_sole_producer_bar()],
    )
    tv = _render_new(tv_mode=True, new_bars=[_stopped_sole_producer_bar()])

    for html in (vertical, tv):
        assert '<span class="name-primary">Humberto S.</span>' in html
        assert '<span class="worker-segment-person">Humberto S.</span>' not in html
        assert "7a-2:33p" in html and "516/700" in html
        assert 'worker-segment-goal completed' in html
```

Update the database-backed test in `tests/test_dashboards_polish.py`:

```python
def test_recycling_stopped_sole_producer_stays_left_and_keeps_finish_line(
    monkeypatch,
):
```

Keep its setup, then replace the final assertions with:

```python
    assert "Humberto S." in html
    assert "Repair 4" in html
    assert "No one here now" not in html
    assert "worker-segment-fill" in html
    assert "worker-segment-goal completed" in html
```

- [ ] **Step 3: Run the render tests red**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q \
  tests/test_new_dashboard_template.py::test_stopped_sole_producer_name_is_left_while_finish_marker_stays_in_bar \
  tests/test_new_dashboard_template.py::test_multiple_producers_keep_each_name_inside_horizontal_bar \
  tests/test_new_dashboard_template.py::test_active_multi_producer_row_identifies_work_center_on_left \
  tests/test_new_dashboard_template.py::test_vacant_multi_producer_row_keeps_empty_status_on_left \
  tests/test_new_dashboard_template.py::test_vertical_and_tv_views_keep_sole_producer_left_without_duplication
```

Expected: failures show that sole producers still render through the old vacancy/work-center branches and segment names are not conditionally wrapped.

- [ ] **Step 4: Put the sole producer first in both left-label branch trees**

In both the vertical `.vbar-name` and horizontal `.name` branches in
`src/zira_dashboard/templates/_department_dashboard_widgets.html`, place this
branch immediately after `is_range` and before `no_one_here_now`:

```jinja2
{% elif b.sole_producer_name %}
  {% set operator_href = operator_links_by_wc.get(b.name) if operator_links_by_wc else none %}
  {{ operator_name(b.sole_producer_name, operator_href, true) }}
  <span class="name-secondary">{{ b.name }}</span>
```

For multi-producer history, keep the work-center identity on the left even if
one of those workers is currently active. Place this branch after
`no_one_here_now` and before the existing `b.who` branch:

```jinja2
{% elif b.show_segment_worker_names %}
  <span class="name-primary">{{ b.name }}</span>
```

- [ ] **Step 5: Render worker names only when the bar has multiple named producers**

In the visible horizontal segment block, wrap the producer label but retain
the detail text:

```jinja2
<span class="worker-segment-name">
  {% if b.show_segment_worker_names or not s.person_name %}
    <span class="worker-segment-person">{{ s.person_label }}</span>
  {% endif %}
  <small class="worker-segment-result">
    {{ s.time_label }} · {{ s.actual_units|round|int }}/{{ s.goal_units|round|int }} · {{ s.result_label }}
  </small>
</span>
```

Apply the same conditional prefix to horizontal callouts:

```jinja2
<span class="worker-segment-callout result-{{ s.result }}">
  {% if b.show_segment_worker_names or not s.person_name %}
    <span class="worker-segment-person">{{ s.person_label }}</span> ·
  {% endif %}
  {{ s.time_label }} · {{ s.actual_units|round|int }}/{{ s.goal_units|round|int }} · {{ s.result_label }}
</span>
```

And to the vertical segment list:

```jinja2
<span class="result-{{ s.result }}">
  {% if b.show_segment_worker_names or not s.person_name %}
    <span class="worker-segment-person">{{ s.person_label }}</span> ·
  {% endif %}
  {{ s.time_label }} · {{ s.actual_units|round|int }}/{{ s.goal_units|round|int }} · {{ s.result_label }}
</span>
```

Do not change segment `title` text; hover details may still name the credited
person without visually duplicating the name in the bar.

- [ ] **Step 6: Add child-friendly patch notes**

Add this newest entry under `## 2026-08-20` in `CHANGELOG.md`:

```markdown
### Worker names stay easy to find

#### Improvements

- **One worker's name now stays beside the work center.** Their stop line and score still show in the bar. When more than one person made pallets there, every name stays with that person's part of the bar.
```

- [ ] **Step 7: Run all render and full-page regressions**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q \
  tests/test_new_dashboard_template.py \
  tests/test_dashboards_polish.py
.venv/bin/ruff check tests/test_new_dashboard_template.py tests/test_dashboards_polish.py
```

Expected without `DATABASE_URL`: template tests pass and database-backed full-page tests skip. With the project test database configured: the renamed Humberto full-page regression also passes.

- [ ] **Step 8: Commit the shared display behavior and patch note**

```bash
git add \
  CHANGELOG.md \
  src/zira_dashboard/templates/_department_dashboard_widgets.html \
  tests/test_new_dashboard_template.py \
  tests/test_dashboards_polish.py
git commit -m "fix: keep sole producer names beside work centers"
```

---

### Task 5: Run cross-feature verification and push `main`

**Files:**
- Verify only; no planned source changes.

**Interfaces:**
- Consumes: all producer identity, aggregation, bar-model, and template changes from Tasks 1-4.
- Produces: verified `main` synchronized with `origin/main` while preserving unrelated untracked files.

- [ ] **Step 1: Run the complete focused regression set**

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

Expected: all database-independent tests pass; database-dependent tests skip only when `DATABASE_URL` is intentionally empty.

- [ ] **Step 2: Run static validation on every changed source and test file**

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

Expected: Ruff reports no issues and `git diff --check` prints nothing.

- [ ] **Step 3: Run the full database-free repository suite and compare only known environment failures**

Run:

```bash
DATABASE_URL= .venv/bin/pytest -q
```

Expected: no new failures. Existing tests that require PostgreSQL or a permitted Playwright browser may remain skipped or report the same documented environment failures as the baseline; compare failing node IDs, not only totals.

- [ ] **Step 4: Inspect the final repository state before pushing**

Run:

```bash
git status --short --branch
git log --oneline -8
git rev-list --left-right --count main...origin/main
```

Expected: only known user-owned untracked files remain, all feature files are committed, and `main` is ahead only by the intended current commits.

- [ ] **Step 5: Push and verify synchronization**

Run:

```bash
git push origin main
git rev-list --left-right --count main...origin/main
git status --short --branch
```

Expected: push succeeds, the rev-list output is `0 0`, and status shows `main...origin/main` plus only the known user-owned untracked files.
