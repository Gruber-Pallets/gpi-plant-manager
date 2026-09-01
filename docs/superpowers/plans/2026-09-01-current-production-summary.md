# Current Production Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the metered production row's work-center count with its current cumulative produced-units/current-goal value, rounded to whole numbers.

**Architecture:** Extract the existing production-metric validation and summation into one internal totals helper. Both the current goal/uptime summary calculation and the new `Production` value consume those totals, so transfers, unavailable data, and numeric safety stay consistent. The template continues rendering the existing four-item summary contract without markup changes.

**Tech Stack:** Python 3.12, FastAPI/Jinja view models, pytest, Ruff, Playwright preview tests.

## Global Constraints

- Label the fourth metered production summary item `Production`.
- Format it as `produced/goal`, rounded to whole numbers with no spaces around `/`.
- Sum all scoreable production intervals through the dashboard's rendered time, including transfers.
- Display `N/A` instead of partial, missing, or non-finite production totals.
- Do not change forklift or other non-metered summaries, scoring, hover details, or goal/uptime/downtime rules.
- Preserve unrelated working-tree changes.

---

### Task 1: Show current produced units against current goal

**Files:**
- Modify: `src/zira_dashboard/people_performance.py:447-480,768-793`
- Modify: `tests/test_people_performance_rows.py:39-56,210-264`
- Modify: `scripts/preview_people_performance.py:101-107,193-315`
- Modify: `tests/test_preview_people_performance.py:55-82`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `ProductionMetric(actual_units, goal_units, productive_minutes, downtime_minutes, result)` and the existing `TimelineInterval.production` values calculated through the dashboard's `as_of_utc`.
- Produces: `_scoreable_production_totals(metrics: Sequence[ProductionMetric]) -> tuple[float, float, float, float] | None`; production row summary item `("Production", "<rounded actual>/<rounded goal>")` or `("Production", "N/A")`.
- Preserves: `weighted_production_summary(metrics) -> tuple[float | None, float | None, float]` and every non-production summary interface.

- [ ] **Step 1: Add failing row-summary tests**

Add a transfer case to `tests/test_people_performance_rows.py` that proves actual and adjusted goal units accumulate across work centers and round only for display:

```python
def test_production_summary_shows_current_units_against_goal_across_transfers():
    model = _assemble(
        spans=(
            span(86, "Current Worker", 0, 60, "Repair 1"),
            span(86, "Current Worker", 60, 120, "Repair 2"),
        ),
        scores=(
            score(86, "Current Worker", "Repair 1", 0, 60, 41.4, 50.4),
            score(86, "Current Worker", "Repair 2", 60, 120, 100.4, 110.4),
        ),
        downtime_by_wc={"Repair 1": (), "Repair 2": ()},
    )

    assert model.rows[0].summary == (
        ("Goal", "88%"),
        ("Uptime", "100%"),
        ("Downtime", "0 min"),
        ("Production", "142/161"),
    )
```

Extend the stale and unavailable production assertions so the fourth item must be `("Production", "N/A")`. Keep the existing forklift summary equality assertion unchanged to protect its contract.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_people_performance_rows.py::test_production_summary_shows_current_units_against_goal_across_transfers \
  tests/test_people_performance_rows.py::test_stale_mapped_span_keeps_identity_but_cannot_earn_metrics \
  tests/test_people_performance_rows.py::test_unavailable_sources_keep_rows_and_warning_order_without_false_zeroes -q
```

Expected: FAIL because the fourth production summary item is still `Centers` and unavailable production rows do not yet expose `Production: N/A`.

- [ ] **Step 3: Extract the shared validated totals helper**

In `src/zira_dashboard/people_performance.py`, move the current scoreable filtering and aggregation out of `weighted_production_summary`:

```python
def _scoreable_production_totals(
    metrics: Sequence[ProductionMetric],
) -> tuple[float, float, float, float] | None:
    scoreable = [
        metric
        for metric in metrics
        if metric.result in {"ahead", "behind"}
        and all(
            _is_finite_number(value)
            for value in (
                metric.actual_units,
                metric.goal_units,
                metric.productive_minutes,
                metric.downtime_minutes,
            )
        )
        and metric.goal_units > 0
        and metric.productive_minutes > 0
        and metric.downtime_minutes >= 0
    ]
    if not scoreable:
        return None
    totals = (
        sum((metric.actual_units for metric in scoreable), 0.0),
        sum((metric.goal_units for metric in scoreable), 0.0),
        sum((metric.productive_minutes for metric in scoreable), 0.0),
        sum((metric.downtime_minutes for metric in scoreable), 0.0),
    )
    return totals if all(_is_finite_number(value) for value in totals) else None
```

Update `weighted_production_summary` to call the helper and preserve its current unavailable return value:

```python
totals = _scoreable_production_totals(metrics)
if totals is None:
    return None, None, 0.0
actual, goal, available, downtime = totals
```

Keep the existing goal-percentage, uptime-percentage, post-calculation finite checks, and return tuple unchanged.

- [ ] **Step 4: Replace Centers with the formatted current total**

Update `_production_summary` to obtain the shared totals. When both `complete` and totals are available, retain the existing three calculations and add:

```python
actual_units, goal_units, _available_minutes, _downtime_minutes = totals
production = f"{actual_units:.0f}/{goal_units:.0f}"
```

When the row is incomplete or totals are unavailable, set all four values to `N/A`. Return:

```python
return (
    ("Goal", goal),
    ("Uptime", uptime),
    ("Downtime", downtime),
    ("Production", production),
)
```

Remove the unused distinct-work-center calculation.

- [ ] **Step 5: Run the focused Python tests and verify GREEN**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_people_performance_production.py \
  tests/test_people_performance_rows.py \
  tests/test_people_performance_view.py \
  tests/test_people_performance_template.py -q
```

Expected: all tests pass, including existing finite-overflow and non-production summary coverage.

- [ ] **Step 6: Update and test the visual preview**

Change `scripts/preview_people_performance.py` so `_production_summary` accepts `production` instead of `centers` and returns `("Production", production)`. Supply representative values already stated by the fixtures, including `126/168` for Amy and `190/170` for Zed. Add these assertions to `test_preview_contains_busy_people_fixture`:

```python
assert ">Production<" in html
assert ">126/168<" in html
assert ">Centers<" not in html
```

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_preview_people_performance.py -q
```

Expected: all preview tests pass. If local Chromium is sandbox-blocked, rerun this exact command with browser permission.

- [ ] **Step 7: Add the child-readable release note**

Add a new newest entry under `## 2026-09-01` in `CHANGELOG.md`:

```markdown
### See production totals at a glance

- **The People page now shows each measured worker's current production next to their current goal.** The total follows them when they move to another measured work area.
```

- [ ] **Step 8: Run final verification**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_people_performance_production.py \
  tests/test_people_performance_rows.py \
  tests/test_people_performance_view.py \
  tests/test_people_performance_template.py \
  tests/test_people_performance_static.py \
  tests/test_people_performance_end_to_end.py -q
.venv/bin/ruff check \
  src/zira_dashboard/people_performance.py \
  scripts/preview_people_performance.py \
  tests/test_people_performance_rows.py \
  tests/test_preview_people_performance.py
node --check src/zira_dashboard/static/people-performance.js
git diff --check
```

Expected: all scoped tests and checks pass.

- [ ] **Step 9: Review, commit, and push**

Request an independent spec-compliance and code-quality review. Resolve every Critical or Important finding and rerun Step 8. Then commit only the scoped files:

```bash
git add \
  src/zira_dashboard/people_performance.py \
  tests/test_people_performance_rows.py \
  scripts/preview_people_performance.py \
  tests/test_preview_people_performance.py \
  CHANGELOG.md
git commit -m "feat: show current production against goal"
git push origin main
```

Verify `git rev-parse HEAD` equals `git rev-parse origin/main` and preserve all unrelated working-tree files.
