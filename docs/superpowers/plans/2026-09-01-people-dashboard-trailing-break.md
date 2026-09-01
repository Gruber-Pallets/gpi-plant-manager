# People Dashboard Trailing Break Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** End People dashboard timelines at the start of any break that reaches shift end, hide that trailing break, and prevent crowded final axis labels.

**Architecture:** Add one People-dashboard-specific window helper in the data loader so attendance, production, forklift, hover, break, and presentation inputs share the same effective endpoint. Keep the presenter responsible only for regular axis tick generation and stop forcing a partial final tick.

**Tech Stack:** Python 3, frozen dataclasses, FastAPI/Jinja presentation context, pytest

## Global Constraints

- Apply this rule only to the People dashboard; do not change the configured plant schedule or other dashboards.
- Detect a trailing break by `break.end_utc == shift_end_utc`, never by its label.
- Preserve every ordinary break that ends on or before the effective dashboard endpoint.
- Preserve current behavior when no valid trailing break reaches shift end.
- Write new `CHANGELOG.md` text in short, common words that explain the user benefit.

---

### Task 1: Derive One Effective People Dashboard Window

**Files:**
- Modify: `tests/test_people_performance_data.py`
- Modify: `src/zira_dashboard/people_performance_data.py`

**Interfaces:**
- Consumes: `_bounds(day: date, now_utc: datetime) -> tuple[datetime, datetime, datetime, bool]` and `_breaks(day: date) -> tuple[BreakSpan, ...]`.
- Produces: `_dashboard_window(day: date, now_utc: datetime) -> tuple[datetime, datetime, datetime, bool, tuple[BreakSpan, ...]]` containing start, effective end, effective cap, today flag, and visible breaks.
- `load_dashboard()` consumes the produced tuple for every downstream source and for `assemble_dashboard()`.

- [ ] **Step 1: Write failing effective-window tests**

Add these tests to `tests/test_people_performance_data.py`:

```python
from zira_dashboard.people_performance import BreakSpan


def test_dashboard_window_ends_at_renamed_trailing_break_start(monkeypatch):
    midday = BreakSpan(
        START + timedelta(hours=2),
        START + timedelta(hours=2, minutes=15),
        "Lunch",
    )
    wind_down = BreakSpan(
        END - timedelta(minutes=15),
        END,
        "Put tools away",
    )
    monkeypatch.setattr(data, "_bounds", lambda day, now: (START, END, END, False))
    monkeypatch.setattr(data, "_breaks", lambda day: (midday, wind_down))

    start, end, cap, is_today, breaks = data._dashboard_window(DAY, END)

    assert start == START
    assert end == wind_down.start_utc
    assert cap == wind_down.start_utc
    assert is_today is False
    assert breaks == (midday,)


def test_dashboard_window_keeps_shift_end_without_valid_trailing_break(monkeypatch):
    midday = BreakSpan(
        START + timedelta(hours=2),
        START + timedelta(hours=2, minutes=15),
        "Lunch",
    )
    monkeypatch.setattr(data, "_bounds", lambda day, now: (START, END, END, False))
    monkeypatch.setattr(data, "_breaks", lambda day: (midday,))

    assert data._dashboard_window(DAY, END) == (
        START,
        END,
        END,
        False,
        (midday,),
    )
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest \
  tests/test_people_performance_data.py::test_dashboard_window_ends_at_renamed_trailing_break_start \
  tests/test_people_performance_data.py::test_dashboard_window_keeps_shift_end_without_valid_trailing_break -q
```

Expected: both tests fail with `AttributeError` because `_dashboard_window` does not exist.

- [ ] **Step 3: Implement the effective window helper**

Add this helper immediately after `_breaks()` in `src/zira_dashboard/people_performance_data.py`:

```python
def _dashboard_window(
    day: date,
    now_utc: datetime,
) -> tuple[datetime, datetime, datetime, bool, tuple[BreakSpan, ...]]:
    start, shift_end, cap, is_today = _bounds(day, now_utc)
    breaks = _breaks(day)
    trailing_starts = tuple(
        item.start_utc
        for item in breaks
        if item.end_utc == shift_end and start < item.start_utc < shift_end
    )
    end = min(trailing_starts, default=shift_end)
    visible_breaks = tuple(
        item for item in breaks if item.start_utc < end and item.end_utc <= end
    )
    return start, end, min(cap, end), is_today, visible_breaks
```

At the start of `load_dashboard()`, replace the separate `_bounds()` and `_breaks()` calls with:

```python
    start, end, cap, is_today, breaks = _dashboard_window(day, now)
```

Do not change downstream call signatures: their existing `start`, `end`, `cap`, and `breaks` arguments will now receive the coherent effective values.

- [ ] **Step 4: Run focused data tests and verify GREEN**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_people_performance_data.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the effective-window change**

```bash
git add src/zira_dashboard/people_performance_data.py tests/test_people_performance_data.py
git commit -m "fix: end people data before trailing break"
```

---

### Task 2: Keep the Final Axis Label Readable

**Files:**
- Modify: `tests/test_people_performance_view.py`
- Modify: `src/zira_dashboard/people_performance_view.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `DashboardModel.window_start_utc`, `DashboardModel.window_end_utc`, and the existing `_pct()` geometry helper.
- Produces: `dashboard_context(model)["axis_labels"]` containing only regular hourly or half-hour ticks; exact regular ticks at the endpoint remain included.

- [ ] **Step 1: Write a failing partial-endpoint axis test**

Add this test to `tests/test_people_performance_view.py`:

```python
def test_axis_omits_partial_final_tick_and_bars_end_at_effective_window():
    effective_end = END - timedelta(minutes=15)
    model = replace(busy_dashboard_model(), window_end_utc=effective_end)

    context = dashboard_context(model)
    row = _row_named(context, "Amy Behind")

    assert context["axis_labels"][-1]["label"] == "1:00 PM"
    assert context["axis_labels"][-1]["left_pct"] < 100.0
    assert row["intervals"][0]["left_pct"] == 0.0
    assert row["intervals"][0]["width_pct"] == 100.0
```

This fixture starts at 6:00 AM Central. Its effective endpoint is 1:45 PM Central, so 1:00 PM is the final regular hourly tick and the interval geometry clamps to the effective endpoint.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest \
  tests/test_people_performance_view.py::test_axis_omits_partial_final_tick_and_bars_end_at_effective_window -q
```

Expected: FAIL because the presenter currently appends a forced `1:45 PM` endpoint label at `100.0` percent.

- [ ] **Step 3: Remove only the forced partial endpoint label**

In `dashboard_context()` in `src/zira_dashboard/people_performance_view.py`, keep the existing axis loop:

```python
    while value <= model.window_end_utc:
        axis_labels.append(
            {
                "label": _time(value),
                "left_pct": _pct(value, model.window_start_utc, model.window_end_utc),
            }
        )
        value += timedelta(minutes=axis_step)
```

Delete the following forced-endpoint block:

```python
    if not axis_labels or axis_labels[-1]["left_pct"] != 100.0:
        axis_labels.append({"label": _time(model.window_end_utc), "left_pct": 100.0})
```

An endpoint that lands exactly on the regular cadence remains in the loop; only the extra partial tick disappears.

- [ ] **Step 4: Add the shipped fix note**

Add a new entry at the top of the `2026-09-01` section in `CHANGELOG.md`:

```markdown
### Keep the end of People work bars clear

- **People work bars now stop when the last working time ends, before a cleanup break.** The final times also stay apart, so they are easy to read.
```

- [ ] **Step 5: Run the People dashboard regression suite and lint**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest \
  tests/test_people_performance_data.py \
  tests/test_people_performance_view.py \
  tests/test_people_performance_rows.py \
  tests/test_people_performance_template.py \
  tests/test_people_performance_static.py \
  tests/test_people_performance_end_to_end.py -q
.venv/bin/ruff check \
  src/zira_dashboard/people_performance_data.py \
  src/zira_dashboard/people_performance_view.py \
  tests/test_people_performance_data.py \
  tests/test_people_performance_view.py
```

Expected: all tests pass and Ruff reports no errors.

- [ ] **Step 6: Commit and push the completed fix**

```bash
git add CHANGELOG.md src/zira_dashboard/people_performance_view.py tests/test_people_performance_view.py
git commit -m "fix: clean up people dashboard day ending"
git push origin main
```

Expected: both implementation commits are present on `origin/main` and the working tree retains only unrelated pre-existing user changes.
