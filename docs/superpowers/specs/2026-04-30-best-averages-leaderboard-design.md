# Best Averages Leaderboard — Design

**Date:** 2026-04-30
**Status:** Approved (brainstorming → implementation planning)

## Context

The leaderboards page (`/staffing/leaderboards`) currently shows **top
5 single-day records** per work center and per group. That view answers
"who had the best single shift in this range?"

Dale wants a parallel view that answers a different question: "who
averages the most output per day they worked at this WC/group?" The
two views complement each other — best days celebrates outliers,
averages surfaces consistent contributors. He wants both visible
side-by-side on the same page.

The page should also gain an expand affordance on the new averages
side so a manager can click "Show all" and see every operator who
worked the WC/group in the range, not just the top 5.

## Goals

1. Split the leaderboards page into two columns:
   - **Left:** Best Days (current top-5 single-day widgets — unchanged).
   - **Right:** Best Averages (new — top-5 per-person averages).
2. Both columns share the existing toolbar (date range + metric
   toggle), section ordering, and active/inactive flags. One drag
   reorders the whole row.
3. Best Averages tables show TWO numeric columns: Avg units/day AND
   Avg % of goal. The metric toggle drives the SORT column.
4. Per-day expected for the % calculation factors in custom hours /
   breaks for that specific day (not a static daily target).
5. Best Averages tables expand to show ALL operators in the range
   when the user clicks `▼ Show all (N)`. Best Days does NOT get an
   expand button (it'd grow long with one row per (person, day)).
6. WCs/groups with no records in the range fall into the existing
   inactive collapsible block at the bottom — same auto-empty rule
   for both columns.

## Non-goals

- Changing the existing top-5 Best Days computation, layout, or
  ordering. Left column stays as-is.
- Per-column independent ordering, active/inactive flags, or metric
  toggles. Both columns are two views of the same metadata.
- Persisting expanded/collapsed state per section across reloads. The
  expand button resets on every page load.
- Line graphs / trend visualization. Tables only.
- A separate URL or tab for the averages view. Same page, two
  columns.

## Design

### Page layout

The current single-column flow becomes a two-column grid. Top-of-page
toolbar (date range + metric) stays full-width. Each WC/group section
emits TWO `<div class="lb-section ...">` siblings — one for Best Days
(with class `lb-side-days`) and one for Best Averages (`lb-side-avg`)
— wrapped in a row container so the drag handle, hide button, and
section header sit at the row level and apply to both halves.

```
.lb-row[draggable=true]
├── .lb-section-header (drag handle, name, hide ✕)
├── .lb-side-days   (Best Days table)
└── .lb-side-avg    (Best Averages table)
```

CSS:

```css
.lb-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-template-areas:
    "header header"
    "days   avg";
  gap: 0.6rem;
  margin-bottom: 1rem;
}
.lb-row .lb-section-header { grid-area: header; }
.lb-row .lb-side-days { grid-area: days; }
.lb-row .lb-side-avg  { grid-area: avg; }

@media (max-width: 900px) {
  .lb-row {
    grid-template-columns: 1fr;
    grid-template-areas:
      "header"
      "days"
      "avg";
  }
}
```

Inactive sections (auto-empty + manually hidden) collapse into the
existing full-width `<details class="lb-inactive-wrap">` block at the
bottom of the page. Inside that block, the same row-with-two-halves
shape is preserved.

### Best Averages computation

In `routes/leaderboards.py`'s GET handler, alongside the existing
top-5 computation (`top5_for(...)` per WC + per group), add a new
helper `averages_for(loc)` that returns a sorted list of per-person
average rows.

Inputs (already in scope):
- `daily_records(start_d, end_d, client)` — yields one record per
  (day, person, wc) with `units`, `downtime`, `hours` for the range.
- `settings_store.station_target(station)` — hourly per-station
  target.
- `shift_config.productive_minutes_per_day(day)` — productive minutes
  for a specific day, accounting for that day's custom shift hours +
  breaks.

Per WC:

```python
def averages_for(loc, mode):
    """Return [{rank, name, name_count, avg_units, avg_pct, ...}]
    sorted by `mode` ('units' or 'pct'), descending. Tiebreak: more
    days_worked ranks higher (more samples = more reliable).
    `name_count` = days_worked, displayed as `(N)` after the name."""
    rows = [r for r in records_for(loc) if r.units > 0]
    by_person: dict[str, list[record]] = {}
    for r in rows:
        by_person.setdefault(r.person, []).append(r)

    hourly_target = settings_store.station_target(loc)

    out = []
    for person, recs in by_person.items():
        days_worked = len(recs)
        total_units = sum(r.units for r in recs)
        avg_units = total_units / days_worked

        # Per-day pct accounts for that day's custom hours / breaks.
        pct_per_day = []
        for r in recs:
            prod_hr = shift_config.productive_minutes_per_day(r.day) / 60.0
            expected = hourly_target * prod_hr
            if expected > 0:
                pct_per_day.append(r.units / expected)
            else:
                pct_per_day.append(0.0)
        avg_pct = sum(pct_per_day) / len(pct_per_day) if pct_per_day else 0.0

        out.append({
            "name": person,
            "name_count": days_worked,
            "avg_units": avg_units,
            "avg_pct": avg_pct,
        })

    if mode == "pct":
        out.sort(key=lambda r: (-r["avg_pct"], -r["name_count"], r["name"].lower()))
    else:
        out.sort(key=lambda r: (-r["avg_units"], -r["name_count"], r["name"].lower()))

    for i, row in enumerate(out, 1):
        row["rank"] = i
    return out
```

Group averages aggregate across the group's WCs (same pattern as the
current group top-5).

For ranges spanning days where the user worked multiple WCs in the
group, each (person, day) is one record per WC — the per-day pct uses
that WC's `expected_for_day`. Cross-WC averaging within a group sums
all those per-WC daily percentages and divides by total person-days.
Reads as "average daily output per day they worked anywhere in the
group."

### Best Averages table shape

Per WC:

```
| # | Operator (N)              | Avg/day | Avg % |
|---|---------------------------|---------|-------|
| 1 | Alice Smith (12)          |  287    | 96%   |
| 2 | Bob Jones (8)             |  264    | 89%   |
| ...                                                |
```

Per group (extra "WC" column showing where they worked, since group
spans multiple WCs):

```
| # | Operator (N)   | Top WC      | Avg/day | Avg % |
|---|----------------|-------------|---------|-------|
| 1 | Alice Smith(12)| Repair 1    |  287    | 96%   |
```

`Top WC` = the WC the operator most often worked in the range
(highest day count); ties broken by WC name (alphabetical) to be
deterministic.

The active sort column gets a small `▼` caret in the header.

### Expansion behavior (Best Averages only)

When a section has more than 5 operators in range, render top 5
visible + a button at the bottom of the table:

```html
<button type="button" class="lb-expand-btn" onclick="toggleAll(this)">
  ▼ Show all ({{ section.full_count }})
</button>
```

`<tr>` elements beyond row 5 are emitted with `class="lb-row-hidden"`,
hidden by default via CSS. `toggleAll(btn)` flips that class on the
section's hidden rows, swaps the button label between
`▼ Show all (N)` and `▲ Hide`. Per-section state in DOM only — no
URL persistence, no localStorage, no fancy scroll restoration.

Best Days side: NO expand button. Top-5 only, always.

### Toolbar (unchanged behavior)

The existing toolbar (date range chips, custom from-to, metric
toggle) stays as-is. Both columns react to it via the same query
params (`window`, `start`, `end`, `metric`). No new toolbar controls.

### Section ordering, drag, inactive

The current `leaderboard_settings_store` keeps per-WC and per-group
ordering + `is_inactive` flags. Both columns read the SAME store for
ordering, so the row container drives placement of both halves
together. Drag-reorder POSTs to the existing `/staffing/leaderboards/order`
endpoint with the `kind=wc` or `kind=group` body — unchanged.

The hide ✕ button (`/staffing/leaderboards/wc/{name}/inactive`) and
unhide ↶ button (`/staffing/leaderboards/wc/{name}/active`) work
unchanged — they hide/show the WHOLE row (both halves).

### Empty / sparse cases

- WC with no records in the range → falls into the inactive group
  on the page, same as today. Both halves render with empty
  table-bodies inside the inactive collapsible block.
- WC with 1–5 operators in the range → renders the rows with no
  expand button. Best Days might also have only a few rows.
- Days where a person had no attributed units (e.g., they were on
  time off) → their record's `units` is 0; we filter `r.units > 0`
  before averaging, so they don't drag down the average.

## Acceptance criteria

- The leaderboards page renders TWO columns per WC/group: "Best
  Days" on the left (current behavior, unchanged) and "Best
  Averages" on the right (new).
- The averages table shows columns: rank, operator name + (days
  worked), Avg/day (units), Avg %. Group averages also include a
  "Top WC" column.
- The metric toggle (Units / % of Goal) drives the SORT column on
  the averages side. Both numeric columns remain visible regardless.
- Custom-hours days are factored into the % calculation: a day where
  the user shortened the shift to 4 hours produces a smaller
  `expected_for_that_day` than a standard 8-hour day, and the
  per-day pct adjusts accordingly.
- WCs/groups with more than 5 operators in the range show a `▼ Show
  all (N)` button below the averages top-5 table. Click expands the
  table to all operators sorted by the active metric. Click again
  hides them.
- Best Days widgets do NOT get a Show all button.
- Drag-reorder a row → both halves move together. Hide ✕ → both
  halves move into the inactive section together.
- Toolbar (date range + metric) controls both columns simultaneously.
- Below 900px viewport, the columns stack vertically (Best Days
  above Best Averages per WC/group).

## Risks

- **Group-level avg_pct math.** When a person works multiple WCs in
  a group across the range, averaging pct-per-day across days where
  expected varies (different WC, different hours) is mathematically
  fine but worth a sanity check on the deployed numbers. Mitigation:
  visual smoke test on a known operator after deploy.
- **`productive_minutes_per_day(day)` per-day overrides.** The
  scheduler stores per-day custom hours in `schedule.custom_hours`.
  The helper needs to read that — verify it does. If it falls back
  to the company schedule for days where custom hours weren't
  explicitly set, that's correct.
- **Inactive section split.** Currently the inactive collapsible
  groups WC and group sections together. With two-column rows, the
  inactive items inside that collapsible also need the row layout.
  Make sure the CSS grid rule applies inside `.lb-inactive-wrap`
  too.
- **Best Days inside the row container.** The current Best Days
  widget HTML is fine to drop into a grid cell; verify nothing
  about its sticky drag handle / position breaks when its parent
  becomes `display: grid`.

## File touch list

- Modify: `src/zira_dashboard/routes/leaderboards.py`
  - Add `averages_for(loc, mode)` (per WC) and group equivalent.
  - Pass new context: `active_avg_sections`, `inactive_avg_sections`,
    `active_avg_groups`, `inactive_avg_groups` (mirroring the existing
    section/group lists, with averages rows attached).
- Modify: `src/zira_dashboard/templates/leaderboards.html`
  - Re-shape each section into a `.lb-row` with two halves.
  - Add the Best Averages table (columns + sort caret + expand button).
  - Add CSS for `.lb-row` grid, `.lb-side-days`, `.lb-side-avg`,
    `.lb-row-hidden`, `.lb-expand-btn`, responsive collapse < 900px.
  - Add `toggleAll(btn)` JS for expand/collapse.
- Modify: `src/zira_dashboard/static/leaderboards.css`
  - Or place the new CSS rules here instead of inline (file already
    extracted in Round-1 perf #5; either path is fine).

No DB migrations. No new endpoints. No new dependencies.
