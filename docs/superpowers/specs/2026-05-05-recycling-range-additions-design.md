# Recycling Range Additions — Design

**Date:** 2026-05-05
**Status:** Approved (brainstorming → implementation planning)

## Context

The Recycling dashboard has a range toolbar with five preset chips
(`Today | Yesterday | This Week | This Month`) plus a `Custom` popover
that opens an inline `<details>` panel with start/end date inputs.

Two issues:

1. **No "Last Week" or "Last Month" presets.** Dale wants to see
   bounded prior-period data (e.g. last calendar month) without having
   to use the Custom date picker each time. These are the two ranges
   he reaches for most often after the current "This X" view.
2. **Custom popup overflows the right edge of the screen.** The
   `.rc-custom-panel` is positioned with `left: 0` relative to its
   trigger chip. The Custom chip is the rightmost element in the
   toolbar, so the panel grows rightward into / past the viewport edge
   and clips out of view.

## Goals

1. Add `Last Week` and `Last Month` preset chips to the Recycling
   range toolbar.
2. Custom popup stays fully on-screen regardless of where the Custom
   chip sits in the toolbar.

## Non-goals

- Adding the new presets to other dashboards (`/staffing/leaderboards`,
  `/time-off`). Those have their own range UIs and weren't asked about.
- Adding the same presets to `/new-vs` — that page is single-day only
  (no `start`/`end` query params, no range toolbar).
- Reworking the Custom popover into a modal, dropdown, or HTML5
  `popover` API. The existing `<details>` element stays.
- Mobile responsiveness work beyond the popup-overflow fix.

## Design

### 1. Last Week / Last Month presets

**Scope of "Last":** Calendar-aligned, mirroring the existing "This"
presets:

- `last_week` → Monday through Sunday of the **previous** calendar
  week. If today is `2026-05-05` (Tuesday), Last Week = `2026-04-27`
  (Mon) → `2026-05-03` (Sun).
- `last_month` → 1st through last day of the **previous** calendar
  month. If today is `2026-05-05`, Last Month = `2026-04-01` →
  `2026-04-30`.

**Toolbar order:** Buttons paired so each "this" sits next to its
"last":

```
Today | Yesterday | This Week | Last Week | This Month | Last Month | Custom
```

**Implementation surface:**

- `src/zira_dashboard/deps.py` — `_window_dates()` gains two new branches:

  ```python
  if window == "last_week":
      monday = today_d - timedelta(days=today_d.weekday())
      last_monday = monday - timedelta(days=7)
      last_sunday = monday - timedelta(days=1)
      return last_monday, last_sunday
  if window == "last_month":
      first_of_this = today_d.replace(day=1)
      last_of_prev = first_of_this - timedelta(days=1)
      first_of_prev = last_of_prev.replace(day=1)
      return first_of_prev, last_of_prev
  ```

- `src/zira_dashboard/templates/recycling.html` — extend the `windows`
  tuple:

  ```jinja
  {% set windows = [
    ('today', 'Today'),
    ('yesterday', 'Yesterday'),
    ('week', 'This Week'),
    ('last_week', 'Last Week'),
    ('month', 'This Month'),
    ('last_month', 'Last Month'),
  ] %}
  ```

- The `_window_dates()` docstring updates from `"today|yesterday|week|month|quarter|year"`
  to include the two new keys.

### 2. Popup positioning

In `static/recycling.css`, change `.rc-custom-panel` from:

```css
.rc-custom-panel {
  position: absolute;
  top: calc(100% + 0.3rem);
  left: 0;
  ...
}
```

to:

```css
.rc-custom-panel {
  position: absolute;
  top: calc(100% + 0.3rem);
  right: 0;
  left: auto;
  ...
}
```

Right-aligns the popup's right edge with the Custom chip's right edge,
growing leftward into space already occupied by chips (popup overlays
them — fine since chips aren't usable while popup is open). The Custom
chip is the rightmost chip in the toolbar, so growth-leftward doesn't
overflow on either side.

## Testing

**Unit tests** (`tests/test_deps_window_dates.py` — new file, or
appended to an existing helper-test file if one fits):

1. `test_last_week_returns_prev_mon_to_prev_sun` — for a known
   reference date (e.g. Tuesday `2026-05-05`), `_window_dates("last_week", d)`
   returns `(date(2026, 4, 27), date(2026, 5, 3))`.
2. `test_last_week_when_today_is_monday` — Monday edge case:
   reference `date(2026, 5, 4)` (a Monday) → `(2026-04-27, 2026-05-03)`.
3. `test_last_month_returns_full_prev_calendar_month` — for reference
   date `2026-05-05`, returns `(date(2026, 4, 1), date(2026, 4, 30))`.
4. `test_last_month_first_of_month_edge` — reference `date(2026, 5, 1)`
   → `(2026-04-01, 2026-04-30)`.
5. `test_last_month_january_crosses_year` — reference
   `date(2026, 1, 15)` → `(2025-12-01, 2025-12-31)`.

**Visual / manual:**

- Toolbar shows new chips in the right order.
- Click each new chip — URL updates to `?window=last_week` or
  `?window=last_month` and dashboard renders for the right date range.
- Custom popup — open it on a desktop viewport with the Custom chip
  near the right edge of the screen. Confirm the popup is fully
  visible. Confirm the date inputs and Apply button are clickable.

No automated visual regression test — eyeball on Railway.

## Files touched

- `src/zira_dashboard/deps.py` — add two branches in `_window_dates()`,
  update its docstring.
- `src/zira_dashboard/templates/recycling.html` — extend the `windows`
  tuple with two new entries.
- `src/zira_dashboard/static/recycling.css` — flip `.rc-custom-panel`
  left/right anchor.
- `tests/test_deps_window_dates.py` (new) — unit tests for the two new
  branches and reasonable edge cases.
- `CHANGELOG.md` — entry for the deploy.
