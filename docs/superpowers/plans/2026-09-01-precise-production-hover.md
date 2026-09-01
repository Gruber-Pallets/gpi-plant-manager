# Precise Production Hover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show cumulative produced units, cumulative adjusted goal, and rolling uptime for the exact minute selected on every metered production bar.

**Architecture:** Preserve each timestamped unit credit through the existing production-scoring pipeline, then build one-minute local hover points with the same productive-time rules used by interval goals. Convert those points to day-cumulative values across a person's transfers, serialize the compact values into production triggers, and let the browser select and format a point locally without per-hover network traffic.

**Tech Stack:** Python 3.12 dataclasses, Jinja2, vanilla JavaScript, CSS, pytest, Node.js controller tests, Ruff.

## Global Constraints

- The hover card has exactly three lines: the selected local time, `Production: <produced> / <goal>`, and `Uptime <percent>%`.
- Production and goal display with one decimal place; uptime displays as a whole percentage.
- Produced and goal are cumulative for the person from the beginning of the displayed day through the selected minute and continue across transfers.
- Goal uses each metered work center's target and the existing productive-time rules; planned breaks, approved breakdown exclusions, and non-metered intervals add no goal.
- Actual production must come from timestamped credited samples. Never spread an interval total evenly over time.
- Uptime is the latest existing rolling 30-minute value at or before the selected minute.
- Exact-time hover applies only to metered production. Forklift and non-metered details remain unchanged.
- Pointer movement makes the selected minute and a thin vertical marker follow the cursor without a network request.
- Keyboard focus, tablet tap, and short-move targets select the closed interval end or the rendered current time for an open interval.
- A pinned minute survives a live refresh when it still belongs to the refreshed interval; otherwise it clamps to the new interval end.
- Incomplete or non-finite production data displays `Production: N/A`; unavailable rolling uptime displays `Uptime N/A`.
- Preserve existing bar colors, scoring, summaries, section ordering, attention ordering, accessibility, viewport clamping, polling, and navigation behavior.
- Add a short child-readable `CHANGELOG.md` note with the implementation push.

---

### Task 1: Preserve timestamped credited units through segment scoring

**Files:**
- Modify: `src/zira_dashboard/production_segments.py:20-53,285-480`
- Test: `tests/test_production_segments.py:180-225,260-310`

**Interfaces:**
- Produces: `CreditedUnitPoint(at_utc: datetime, units: float)`.
- Extends: `SegmentCredit.unit_points: tuple[CreditedUnitPoint, ...]` and `SegmentScore.unit_points: tuple[CreditedUnitPoint, ...]`, both defaulting to `()` for compatibility with historical aggregate callers.
- Guarantees: every timestamped sample share assigned to a named segment is recorded once and the point sum equals that segment's timestamped `actual_units` when total fallback is disabled.

- [ ] **Step 1: Write failing credit-point tests**

Add imports and tests in `tests/test_production_segments.py`:

```python
from zira_dashboard.production_segments import CreditedUnitPoint


def test_timestamped_sample_shares_are_kept_with_each_worker_credit():
    segments = [
        WorkSegment("Repair 1", "Alex", t(12), t(13), "punch", person_odoo_id=44),
        WorkSegment("Repair 1", "Blair", t(12), t(12, 30), "punch", person_odoo_id=45),
    ]

    credits = credit_work_segments(
        segments,
        wc_totals={"Repair 1": 30.0},
        samples_by_wc={"Repair 1": [(t(12, 15), 20.0), (t(12, 45), 10.0)]},
        productive_minutes=lambda _person, _wc, start, end: (
            end - start
        ).total_seconds() / 60.0,
        allow_total_fallback=False,
    )["Repair 1"]

    alex, blair = credits
    assert alex.unit_points == (
        CreditedUnitPoint(t(12, 15), 10.0),
        CreditedUnitPoint(t(12, 45), 10.0),
    )
    assert blair.unit_points == (CreditedUnitPoint(t(12, 15), 10.0),)
    assert sum(point.units for point in alex.unit_points) == alex.actual_units
    assert sum(point.units for point in blair.unit_points) == blair.actual_units


def test_scoring_and_display_coalescing_preserve_unit_point_order():
    first = SegmentCredit(
        1, "Repair 1", "Alex", t(12), t(12, 30), "odoo", 30, 5, False, 44,
        (CreditedUnitPoint(t(12, 10), 5),),
    )
    second = SegmentCredit(
        2, "Repair 1", "Alex", t(13), t(13, 30), "odoo", 30, 7, False, 44,
        (CreditedUnitPoint(t(13, 20), 7),),
    )

    scored = score_work_segments(
        {"Repair 1": (first, second)}, target_per_hour={"Repair 1": 10}
    )["Repair 1"]
    (joined,) = coalesce_display_scores(
        scored, ignored_gaps=((t(12, 30), t(13)),)
    )

    assert joined.unit_points == (
        CreditedUnitPoint(t(12, 10), 5),
        CreditedUnitPoint(t(13, 20), 7),
    )
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_production_segments.py::test_timestamped_sample_shares_are_kept_with_each_worker_credit \
  tests/test_production_segments.py::test_scoring_and_display_coalescing_preserve_unit_point_order -q
```

Expected: collection fails because `CreditedUnitPoint` and `unit_points` do not exist.

- [ ] **Step 3: Add the timestamped credit type and fields**

Add before `SegmentCredit` in `src/zira_dashboard/production_segments.py`:

```python
@dataclass(frozen=True)
class CreditedUnitPoint:
    at_utc: datetime
    units: float
```

Add this final field to both `SegmentCredit` and `SegmentScore`:

```python
    unit_points: tuple[CreditedUnitPoint, ...] = ()
```

Initialize each named segment row with `"unit_points": []`. When a sample is shared in `credit_work_segments()`, update both values:

```python
share = units / len(active_by_person)
for index in active_by_person.values():
    rows[index]["actual_units"] += share
    rows[index]["unit_points"].append(CreditedUnitPoint(timestamp, share))
```

Give every unassigned row `"unit_points": []`, convert every row list to a tuple before `SegmentCredit(**row)`, and copy points in `score_work_segments()`:

```python
unit_points=tuple(credit.unit_points),
```

Merge points in `_join_display_scores()`:

```python
unit_points=tuple(sorted((*left.unit_points, *right.unit_points), key=lambda point: point.at_utc)),
```

- [ ] **Step 4: Run focused and production-history regressions**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_production_segments.py \
  tests/test_production_history_testing.py \
  tests/test_production_history_odoo_strict.py -q
.venv/bin/ruff check src/zira_dashboard/production_segments.py tests/test_production_segments.py
```

Expected: all tests pass and Ruff prints `All checks passed!`.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/zira_dashboard/production_segments.py tests/test_production_segments.py
git commit -m "feat: preserve timestamped worker production"
```

---

### Task 2: Calculate exact-minute local and day-cumulative hover metrics

**Files:**
- Modify: `src/zira_dashboard/people_performance.py:20-52,115-140,150-315,780-920`
- Modify: `tests/people_performance_fixtures.py:35-68`
- Test: `tests/test_people_performance_production.py`

**Interfaces:**
- Produces: `ProductionHoverPoint(at_utc: datetime, actual_units: float, goal_units: float, uptime_pct: float | None)`.
- Extends: `ProductionMetric.hover_points: tuple[ProductionHoverPoint, ...] = ()`.
- Produces: `cumulative_production_hover_points(intervals: Sequence[TimelineInterval]) -> dict[str, tuple[ProductionHoverPoint, ...]]` keyed by stable interval key.
- Consumes: Task 1 `SegmentScore.unit_points`.

- [ ] **Step 1: Extend the fixture helper and write failing minute-metric tests**

Import `CreditedUnitPoint` in `tests/people_performance_fixtures.py`, then replace the fixture score helper with this compatibility form. Existing fixture callers receive one trustworthy timestamped point just before their interval end; a test can pass an explicit tuple when the event time matters:

```python
def score(
    employee_id,
    name,
    wc,
    start_minute,
    end_minute,
    actual,
    goal,
    *,
    unit_points=None,
):
    start_utc = START + timedelta(minutes=start_minute)
    end_utc = START + timedelta(minutes=end_minute)
    points = (
        (
            CreditedUnitPoint(end_utc - timedelta(microseconds=1), actual),
        )
        if unit_points is None and actual > 0
        else () if unit_points is None else tuple(unit_points)
    )
    minutes = end_minute - start_minute
    return SegmentScore(
        segment_id=employee_id,
        wc_name=wc,
        person_name=name,
        start_utc=start_utc,
        end_utc=end_utc,
        source="odoo",
        productive_minutes=minutes,
        actual_units=actual,
        goal_units=goal,
        runway_units=max(actual, goal),
        is_active=end_minute == 480,
        result="ahead" if actual >= goal else "behind",
        person_odoo_id=employee_id,
        unit_points=points,
    )
```

Add imports and tests to `tests/test_people_performance_production.py`:

```python
from zira_dashboard.people_performance import (
    TimelineInterval,
    cumulative_production_hover_points,
)
from zira_dashboard.production_segments import CreditedUnitPoint


def test_production_hover_points_use_real_samples_adjusted_goal_and_latest_uptime():
    score = replace(
        _score(10, 50),
        productive_minutes=50,
        unit_points=(
            CreditedUnitPoint(START + timedelta(minutes=5), 6),
            CreditedUnitPoint(START + timedelta(minutes=25), 4),
        ),
    )

    metric = production_metric(
        score,
        downtime_windows=((START + timedelta(minutes=25), START + timedelta(minutes=30)),),
        breaks=(BreakSpan(START + timedelta(minutes=10), START + timedelta(minutes=20), "Break"),),
    )
    at_30 = next(
        point for point in metric.hover_points
        if point.at_utc == START + timedelta(minutes=30)
    )
    at_15 = next(
        point for point in metric.hover_points
        if point.at_utc == START + timedelta(minutes=15)
    )

    assert at_30.actual_units == pytest.approx(10.0)
    assert at_30.goal_units == pytest.approx(20.0)
    assert at_30.uptime_pct == pytest.approx(75.0)
    assert at_15.goal_units == pytest.approx(10.0)
    assert at_15.uptime_pct is None
    assert metric.hover_points[-1].actual_units == pytest.approx(metric.actual_units)
    assert metric.hover_points[-1].goal_units == pytest.approx(metric.goal_units)


def test_cumulative_hover_points_continue_across_transfer_targets():
    first_metric = production_metric(
        replace(
            _score(10, 30, START, START + timedelta(minutes=30)),
            productive_minutes=30,
            unit_points=(CreditedUnitPoint(START + timedelta(minutes=20), 10),),
        ),
        downtime_windows=(),
        breaks=(),
    )
    second_score = SegmentScore(
        segment_id=2,
        wc_name="Dismantler 1",
        person_name="Alex Worker",
        start_utc=START + timedelta(minutes=30),
        end_utc=START + timedelta(minutes=60),
        source="odoo",
        productive_minutes=30,
        actual_units=5,
        goal_units=20,
        runway_units=20,
        is_active=False,
        result="behind",
        person_odoo_id=44,
        unit_points=(CreditedUnitPoint(START + timedelta(minutes=50), 5),),
    )
    second_metric = production_metric(second_score, downtime_windows=(), breaks=())
    intervals = (
        TimelineInterval("first", START, START + timedelta(minutes=30), "Repair 1", "valid", "production", False, production=first_metric),
        TimelineInterval("second", START + timedelta(minutes=30), START + timedelta(minutes=60), "Dismantler 1", "valid", "production", True, production=second_metric),
    )

    cumulative = cumulative_production_hover_points(intervals)

    assert cumulative["first"][-1].actual_units == pytest.approx(10.0)
    assert cumulative["first"][-1].goal_units == pytest.approx(30.0)
    assert cumulative["second"][-1].actual_units == pytest.approx(15.0)
    assert cumulative["second"][-1].goal_units == pytest.approx(50.0)


def test_untrusted_earlier_production_poison_later_cumulative_hover():
    missing_points = production_metric(_score(10, 30), downtime_windows=(), breaks=())
    valid = production_metric(
        replace(
            _score(5, 30, START + timedelta(hours=1), START + timedelta(hours=2)),
            unit_points=(CreditedUnitPoint(START + timedelta(hours=1, minutes=30), 5),),
        ),
        downtime_windows=(),
        breaks=(),
    )
    intervals = (
        TimelineInterval("missing", START, START + timedelta(hours=1), "Repair 1", "valid", "production", False, production=missing_points),
        TimelineInterval("later", START + timedelta(hours=1), START + timedelta(hours=2), "Repair 2", "valid", "production", True, production=valid),
    )

    assert cumulative_production_hover_points(intervals) == {
        "missing": (),
        "later": (),
    }
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_people_performance_production.py::test_production_hover_points_use_real_samples_adjusted_goal_and_latest_uptime \
  tests/test_people_performance_production.py::test_cumulative_hover_points_continue_across_transfer_targets \
  tests/test_people_performance_production.py::test_untrusted_earlier_production_poison_later_cumulative_hover -q
```

Expected: collection fails because `ProductionHoverPoint`, `hover_points`, and `cumulative_production_hover_points` do not exist.

- [ ] **Step 3: Build trustworthy one-minute local points**

Add after `RollingPoint`:

```python
@dataclass(frozen=True)
class ProductionHoverPoint:
    at_utc: datetime
    actual_units: float
    goal_units: float
    uptime_pct: float | None
```

Add `hover_points: tuple[ProductionHoverPoint, ...] = ()` as the final `ProductionMetric` field. Add helpers with these exact contracts:

```python
def _minute_points(start_utc: datetime, end_utc: datetime) -> tuple[datetime, ...]:
    values = [start_utc]
    cursor = start_utc.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while cursor < end_utc:
        values.append(cursor)
        cursor += timedelta(minutes=1)
    if values[-1] != end_utc:
        values.append(end_utc)
    return tuple(values)


def _production_hover_points(
    score: SegmentScore,
    *,
    available_windows: Sequence[TimeWindow],
    rolling_uptime: Sequence[RollingPoint],
) -> tuple[ProductionHoverPoint, ...]:
    credited = tuple(sorted(score.unit_points, key=lambda point: point.at_utc))
    if any(
        not _is_finite_number(point.units)
        or point.units < 0
        or not isinstance(point.at_utc, datetime)
        or point.at_utc.utcoffset() is None
        or point.at_utc < score.start_utc
        or point.at_utc >= score.end_utc
        for point in credited
    ):
        return ()
    if abs(sum(point.units for point in credited) - score.actual_units) > 1e-6:
        return ()
    available_minutes = _intersection_minutes(
        score.start_utc, score.end_utc, available_windows
    )
    if available_minutes <= 0:
        return ()
    rate = score.goal_units / available_minutes
    values = []
    for at_utc in _minute_points(score.start_utc, score.end_utc):
        actual = sum(point.units for point in credited if point.at_utc <= at_utc)
        elapsed = _intersection_minutes(score.start_utc, at_utc, available_windows)
        is_available = any(left < at_utc <= right for left, right in available_windows)
        uptime = (
            next(
                (
                    point.value_pct
                    for point in reversed(rolling_uptime)
                    if point.at_utc <= at_utc
                ),
                None,
            )
            if is_available
            else None
        )
        values.append(ProductionHoverPoint(at_utc, actual, min(score.goal_units, rate * elapsed), uptime))
    return tuple(values)
```

After the existing bounds/value guards in `production_metric()`, replace the inline rolling call with one shared value and return both series:

```python
rolling_uptime = rolling_uptime_points(
    start_utc=score.start_utc,
    end_utc=score.end_utc,
    available_windows=available,
    downtime_windows=eligible_stops,
)
hover_points = _production_hover_points(
    score,
    available_windows=available,
    rolling_uptime=rolling_uptime,
)
return ProductionMetric(
    actual_units=score.actual_units,
    goal_units=score.goal_units,
    productive_minutes=available_minutes,
    downtime_minutes=downtime,
    result=state,
    rolling_uptime=rolling_uptime,
    hover_points=hover_points,
)
```

- [ ] **Step 4: Add day-cumulative transfer handling**

Add:

```python
def cumulative_production_hover_points(
    intervals: Sequence[TimelineInterval],
) -> dict[str, tuple[ProductionHoverPoint, ...]]:
    production_keys = [interval.key for interval in intervals if interval.role == "production"]
    if len(set(production_keys)) != len(production_keys):
        raise ValueError("production interval keys must be unique")
    actual_base = 0.0
    goal_base = 0.0
    trusted = True
    result = {}
    for interval in intervals:
        if interval.role != "production":
            continue
        metric = interval.production
        if (
            not trusted
            or not interval.metric_available
            or metric is None
            or metric.result == "unavailable"
            or not metric.hover_points
        ):
            trusted = False
            result[interval.key] = ()
            continue
        result[interval.key] = tuple(
            ProductionHoverPoint(
                point.at_utc,
                actual_base + point.actual_units,
                goal_base + point.goal_units,
                point.uptime_pct,
            )
            for point in metric.hover_points
        )
        actual_base += metric.actual_units
        goal_base += metric.goal_units
    return result
```

Reject duplicate interval keys with `ValueError("production interval keys must be unique")` before returning so one interval cannot overwrite another.

- [ ] **Step 5: Run focused and row-assembly regressions**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_people_performance_production.py \
  tests/test_people_performance_rows.py -q
.venv/bin/ruff check \
  src/zira_dashboard/people_performance.py \
  tests/people_performance_fixtures.py \
  tests/test_people_performance_production.py
```

Expected: all tests pass and Ruff prints `All checks passed!`.

- [ ] **Step 6: Commit Task 2**

```bash
git add \
  src/zira_dashboard/people_performance.py \
  tests/people_performance_fixtures.py \
  tests/test_people_performance_production.py
git commit -m "feat: calculate precise production hover values"
```

---

### Task 3: Serialize cumulative hover values into production triggers

**Files:**
- Modify: `src/zira_dashboard/people_performance_view.py:15-180`
- Modify: `src/zira_dashboard/templates/_people_performance_rows.html:48-125`
- Test: `tests/test_people_performance_view.py`
- Test: `tests/test_people_performance_template.py`

**Interfaces:**
- Consumes: Task 2 `cumulative_production_hover_points()`.
- Produces per production interval: `hover_points`, `hover_start_ms`, and `hover_end_ms`.
- HTML contract: production triggers and production short-move buttons expose `data-production-hover`, `data-hover-start-ms`, and `data-hover-end-ms`; non-production triggers do not.

- [ ] **Step 1: Write failing presenter and template tests**

Add to `tests/test_people_performance_view.py`:

```python
def test_production_hover_values_are_cumulative_finite_and_timestamped():
    context = dashboard_context(busy_dashboard_model())
    row = _row_named(context, "Mia Mixed")
    production = [item for item in row["intervals"] if item["role"] == "production"]

    assert production[0]["hover_points"][0]["at_ms"] == production[0]["hover_start_ms"]
    assert production[-1]["hover_points"][-1]["at_ms"] == production[-1]["hover_end_ms"]
    assert production[-1]["hover_points"][-1]["production"] >= production[0]["hover_points"][-1]["production"]
    assert production[-1]["hover_points"][-1]["goal"] >= production[0]["hover_points"][-1]["goal"]
    assert all(
        math.isfinite(value)
        for item in production
        for point in item["hover_points"]
        for value in (point["production"], point["goal"])
    )


def test_nonproduction_intervals_do_not_receive_production_hover_values():
    context = dashboard_context(busy_dashboard_model())
    forklift = _row_named(context, "Ben Driver")["intervals"][0]

    assert forklift["hover_points"] == ()
    assert forklift["hover_start_ms"] is None
    assert forklift["hover_end_ms"] is None
```

Add to `tests/test_people_performance_template.py`:

```python
def test_only_production_triggers_carry_precise_hover_data_and_marker(rendered_html):
    assert 'data-production-hover="[[' in rendered_html
    assert 'data-hover-start-ms="' in rendered_html
    assert 'data-hover-end-ms="' in rendered_html
    assert 'class="pp-hover-marker" aria-hidden="true"' in rendered_html
    assert 'data-production-hover=' not in next(
        tag for tag in rendered_html.split('<button')
        if 'role-forklift' in tag
    ).split('</button>', 1)[0]
```

Import `math` in the view test.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_people_performance_view.py::test_production_hover_values_are_cumulative_finite_and_timestamped \
  tests/test_people_performance_view.py::test_nonproduction_intervals_do_not_receive_production_hover_values \
  tests/test_people_performance_template.py::test_only_production_triggers_carry_precise_hover_data_and_marker -q
```

Expected: failures because the presenter and template do not expose precise hover values.

- [ ] **Step 3: Add the presenter serialization contract**

Import `ProductionHoverPoint` and `cumulative_production_hover_points`. Add:

```python
def _epoch_ms(value: datetime) -> int:
    return round(value.timestamp() * 1000)


def _hover_point_view(point) -> tuple[int, float, float, float | None]:
    return (
        _epoch_ms(point.at_utc),
        round(point.actual_units, 6),
        round(point.goal_units, 6),
        None if point.uptime_pct is None else round(point.uptime_pct, 6),
    )
```

Change the `_interval_view()` signature to:

```python
def _interval_view(
    item: TimelineInterval,
    model: DashboardModel,
    location_class: str,
    production_hover: tuple[ProductionHoverPoint, ...],
) -> dict:
```

Keep its existing body and add these keys to the returned interval dictionary:

```python
"hover_points": tuple(_hover_point_view(point) for point in production_hover),
"hover_start_ms": _epoch_ms(item.start_utc) if item.role == "production" else None,
"hover_end_ms": _epoch_ms(item.end_utc) if item.role == "production" else None,
```

In `_row_view()`, calculate once and pass by key:

```python
production_hover = cumulative_production_hover_points(row.intervals)
intervals = tuple(
    _interval_view(
        item,
        model,
        location_classes[item.location_name],
        production_hover.get(item.key, ()),
    )
    for item in row.intervals
)
```

The compact point tuple order is exactly `[at_ms, produced, goal, uptime]` when serialized to JSON.

- [ ] **Step 4: Put precise data and a decorative marker on production targets**

On `.pp-interval-trigger`, add only when `item.role == 'production'`:

```jinja2
data-production-hover="{{ item.hover_points|tojson|forceescape }}"
data-hover-start-ms="{{ item.hover_start_ms }}"
data-hover-end-ms="{{ item.hover_end_ms }}"
```

Add inside the production metric track, after the SVG:

```html
<span class="pp-hover-marker" aria-hidden="true"></span>
```

Add the same three data attributes to a production `.pp-interval-shortcut`; do not add a marker to the shortcut.

- [ ] **Step 5: Run presentation and template regressions**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_people_performance_view.py \
  tests/test_people_performance_template.py \
  tests/test_people_performance_end_to_end.py -q
.venv/bin/ruff check \
  src/zira_dashboard/people_performance_view.py \
  tests/test_people_performance_view.py \
  tests/test_people_performance_template.py
```

Expected: all tests pass and Ruff prints `All checks passed!`.

- [ ] **Step 6: Commit Task 3**

```bash
git add \
  src/zira_dashboard/people_performance_view.py \
  src/zira_dashboard/templates/_people_performance_rows.html \
  tests/test_people_performance_view.py \
  tests/test_people_performance_template.py
git commit -m "feat: expose production hover checkpoints"
```

---

### Task 4: Make the precise tooltip and marker follow the selected minute

**Files:**
- Modify: `src/zira_dashboard/static/people-performance.js:1-145,300-390`
- Modify: `src/zira_dashboard/static/people-performance.css:275-320,482-500`
- Modify: `tests/test_people_performance_static.py:150-410`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 3 compact `[at_ms, produced, goal, uptime]` arrays and interval millisecond bounds.
- Produces: local cursor selection, exact three-line tooltip text, pinned selected timestamp restoration, and `.is-visible` marker placement.
- Preserves: existing non-production `data-detail` behavior and controller lifecycle.

- [ ] **Step 1: Extend the JavaScript harness and write failing interaction assertions**

Extend `makeTrigger()` in the embedded Node harness so a production trigger can carry `productionHover`, `hoverStartMs`, `hoverEndMs`, and a fake `.pp-hover-marker` with `style` and `classList`. Change its signature to `function makeTrigger(key, detail, kind, top, datasetValues)`. Immediately after the existing `attributes` declaration, add:

```javascript
const markerClasses = new Set();
const marker = {
  style: {},
  classList: {
    add(value) { markerClasses.add(value); },
    remove(value) { markerClasses.delete(value); },
    contains(value) { return markerClasses.has(value); },
  },
};
```

Replace the existing trigger `dataset` property with:

```javascript
dataset: Object.assign({intervalKey: key, detail}, datasetValues || {}),
marker,
```

Add this method to the existing trigger object:

```javascript
querySelector(selector) {
  return selector === '.pp-hover-marker' ? marker : null;
}
```

Add this scenario before the refresh race tests:

```javascript
const preciseEnv = makeEnvironment('1');
const startMs = Date.UTC(2026, 7, 28, 12, 0);
const precise = preciseEnv.makeTrigger(
  'precise',
  'old interval detail',
  'interval',
  80,
  {
    productionHover: JSON.stringify([
      [startMs, 0, 0, null],
      [startMs + 30 * 60000, 12, 20, 75.4],
      [startMs + 60 * 60000, 30, 40, 92.6],
    ]),
    hoverStartMs: String(startMs),
    hoverEndMs: String(startMs + 60 * 60000),
  }
);
preciseEnv.document.rows.triggers = [precise];
const preciseController = makeController(preciseEnv.document, preciseEnv.windowObject);
preciseController.init();
preciseEnv.document.emit('pointerover', event(precise));
preciseEnv.document.emit('pointermove', {...event(precise), clientX: 290});
const preciseTip = preciseEnv.getPopover();
if (preciseTip.textContent !== '7:30 AM\nProduction: 12.0 / 20.0\nUptime 75%') {
  throw new Error('precise production tooltip format or value is wrong: ' + preciseTip.textContent);
}
if (!precise.marker.classList.contains('is-visible') || precise.marker.style.left !== '50%') {
  throw new Error('precise hover marker did not follow the selected minute');
}
preciseEnv.document.emit('click', event(precise));
preciseEnv.document.emit('pointermove', {...event(precise), clientX: 309});
if (preciseTip.textContent.includes('30.0 / 40.0')) {
  throw new Error('a pinned precise minute changed during pointer movement');
}
```

Add separate assertions that focus/tap chooses the last point, a production trigger with `[]` shows `Production: N/A\nUptime N/A`, and a forklift trigger still shows its unchanged `data-detail` text.

- [ ] **Step 2: Run the controller test and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_people_performance_static.py::test_controller_runtime_handles_details_races_navigation_and_teardown -q
```

Expected: failure because pointer movement does not select production hover values and no marker is shown.

- [ ] **Step 3: Add compact parsing, selection, and exact formatting**

Add controller state `var selectedAtMs = null;`. Add these helpers:

```javascript
function productionPoints(trigger) {
  if (!trigger || trigger.dataset.productionHover == null) return null;
  try {
    var parsed = JSON.parse(trigger.dataset.productionHover);
    return Array.isArray(parsed) ? parsed : [];
  } catch (_error) {
    return [];
  }
}

function pointAt(points, atMs) {
  var selected = null;
  points.forEach(function (point) {
    if (Array.isArray(point) && point.length === 4 && Number(point[0]) <= atMs) {
      selected = point;
    }
  });
  return selected;
}

function localTime(atMs) {
  var IntlObject = windowObject.Intl || Intl;
  return new IntlObject.DateTimeFormat('en-US', {
    timeZone: 'America/Chicago',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(atMs));
}

function productionDetail(trigger, requestedAtMs) {
  var points = productionPoints(trigger);
  if (points === null) return null;
  var start = Number(trigger.dataset.hoverStartMs);
  var end = Number(trigger.dataset.hoverEndMs);
  var fallback = points.length ? Number(points[points.length - 1][0]) : end;
  var atMs = clamp(Number.isFinite(requestedAtMs) ? requestedAtMs : fallback, start, end);
  atMs = Math.round(atMs / 60000) * 60000;
  var point = pointAt(points, atMs);
  var time = localTime(atMs);
  if (!point || !Number.isFinite(point[1]) || !Number.isFinite(point[2])) {
    return {atMs: atMs, text: time + '\nProduction: N/A\nUptime N/A'};
  }
  var uptime = Number.isFinite(point[3]) ? Math.round(point[3]) + '%' : 'N/A';
  return {
    atMs: atMs,
    text: time + '\nProduction: ' + Number(point[1]).toFixed(1)
      + ' / ' + Number(point[2]).toFixed(1) + '\nUptime ' + uptime,
  };
}
```

Use `windowObject.Intl || Intl` in production code so the formatter remains available in browsers and the Node harness can inject it deterministically.

- [ ] **Step 4: Integrate precise selection with open, pin, close, and refresh**

Add marker cleanup and replace `open()` with:

```javascript
function hideMarker(trigger) {
  var marker = trigger && trigger.querySelector
    ? trigger.querySelector('.pp-hover-marker')
    : null;
  if (marker) marker.classList.remove('is-visible');
}

function open(trigger, shouldPin, requestedAtMs) {
  if (!trigger) return;
  var tip = ensurePopover();
  if (active && active !== trigger) {
    hideMarker(active);
    active.setAttribute('aria-expanded', 'false');
    active.removeAttribute('aria-describedby');
  }
  active = trigger;
  pinned = shouldPin ? trigger : null;
  var precise = productionDetail(trigger, requestedAtMs);
  if (precise) {
    selectedAtMs = precise.atMs;
    tip.textContent = precise.text;
    updateMarker(trigger, precise.atMs);
  } else {
    selectedAtMs = null;
    hideMarker(trigger);
    tip.textContent = trigger.dataset.detail || trigger.getAttribute('aria-label') || '';
  }
  tip.hidden = false;
  trigger.setAttribute('aria-expanded', 'true');
  trigger.setAttribute('aria-describedby', tip.id);
  position(trigger);
}
```

In `close()`, call `hideMarker(active)` before clearing `active`, then set `selectedAtMs = null`. Replace the pin branch in `onClick()` with:

```javascript
else open(trigger, true, trigger === active ? selectedAtMs : null);
```

Add:

```javascript
function updateMarker(trigger, atMs) {
  var marker = trigger && trigger.querySelector
    ? trigger.querySelector('.pp-hover-marker')
    : null;
  if (!marker) return;
  var start = Number(trigger.dataset.hoverStartMs);
  var end = Number(trigger.dataset.hoverEndMs);
  var percent = end > start ? 100 * (atMs - start) / (end - start) : 100;
  marker.style.left = clamp(percent, 0, 100) + '%';
  marker.classList.add('is-visible');
}

function onPointerMove(event) {
  var trigger = triggerFor(event.target);
  if (!trigger || pinned || productionPoints(trigger) === null) return;
  var box = trigger.getBoundingClientRect();
  var fraction = box.width > 0 ? clamp((event.clientX - box.left) / box.width, 0, 1) : 1;
  var start = Number(trigger.dataset.hoverStartMs);
  var end = Number(trigger.dataset.hoverEndMs);
  open(trigger, false, start + fraction * (end - start));
}
```

Listen for `pointermove`. Add `pinnedAtMs: pinned ? selectedAtMs : null` to `captureState()`, replace the pinned restore call with `open(pinTarget, true, state.pinnedAtMs)`, and let `productionDetail()` clamp it after refresh.

- [ ] **Step 5: Style the exact card and cursor marker**

Add:

```css
.pp-hover-marker {
  position: absolute;
  inset: 0 auto 0 0;
  z-index: 24;
  display: none;
  width: 2px;
  background: #fff;
  box-shadow: 0 0 0 1px #0f172a;
  pointer-events: none;
}

.pp-hover-marker.is-visible {
  display: block;
}

.pp-detail-popover {
  white-space: pre-line;
}
```

Do not change the tooltip's existing viewport bounds, colors, font sizing, or z-index.

- [ ] **Step 6: Run interaction, accessibility, and live-refresh regressions**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_people_performance_static.py \
  tests/test_people_performance_template.py \
  tests/test_people_performance_view.py \
  tests/test_people_performance_end_to_end.py \
  tests/test_people_performance_route.py -q
node --check src/zira_dashboard/static/people-performance.js
git diff --check
```

Expected: all tests pass, Node prints nothing, and `git diff --check` prints nothing.

- [ ] **Step 7: Add the child-readable patch note**

Add at the top of `## 2026-09-01` in `CHANGELOG.md`:

```markdown
### See production at the exact minute

- **Point at a measured work bar to see that worker's production, goal, and uptime at that exact minute.** The numbers keep adding up when the worker moves to another measured work area.
```

- [ ] **Step 8: Run the full feature verification**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_production_segments.py \
  tests/test_production_history_testing.py \
  tests/test_production_history_odoo_strict.py \
  tests/test_people_performance_production.py \
  tests/test_people_performance_forklift.py \
  tests/test_people_performance_rows.py \
  tests/test_people_performance_data.py \
  tests/test_people_performance_view.py \
  tests/test_people_performance_route.py \
  tests/test_people_performance_template.py \
  tests/test_people_performance_static.py \
  tests/test_people_performance_end_to_end.py -q
.venv/bin/ruff check \
  src/zira_dashboard/production_segments.py \
  src/zira_dashboard/people_performance.py \
  src/zira_dashboard/people_performance_view.py \
  tests/test_production_segments.py \
  tests/people_performance_fixtures.py \
  tests/test_people_performance_production.py \
  tests/test_people_performance_view.py \
  tests/test_people_performance_template.py \
  tests/test_people_performance_static.py
node --check src/zira_dashboard/static/people-performance.js
git diff --check
```

Expected: all tests pass, Ruff prints `All checks passed!`, Node prints nothing, and `git diff --check` prints nothing.

- [ ] **Step 9: Commit and push Task 4**

```bash
git add \
  src/zira_dashboard/static/people-performance.js \
  src/zira_dashboard/static/people-performance.css \
  tests/test_people_performance_static.py \
  CHANGELOG.md
git commit -m "feat: show precise production on hover"
git push origin main
```

Expected: the commit and push succeed without staging `.superpowers/sdd/task-1-report.md`, `.superpowers/sdd/task-7-report.md`, `.cursorignore`, `.python-version`, or `uv.lock`.
