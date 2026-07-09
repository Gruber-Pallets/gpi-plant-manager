# Recycling Leaderboard TV And Normalized Production Averages

**Date:** 2026-07-09
**Status:** Design approved visually; expanded metric scope awaiting written spec review

## Goal

Add a new TV dashboard for the recycling area that shows a fair, easy-to-read
leaderboard for Repairs and Dismantlers, plus monthly gold ribbon holders for
both roles over the last 12 months.

Also make the same normalized full-day average available across production
leaderboards and employee/player cards so every personal production average uses
the same fairness rule.

The TV uses the approved three-column layout:

1. Repairs leaderboard
2. Dismantlers leaderboard
3. Gold ribbons for Repairs and Dismantlers by month

The TV screen is meant for a wall display, so it must be dense enough to be
useful and simple enough to read from a distance.

## Core Metric

The displayed production average is **normalized pallets per full standard
day**. It replaces simple units-per-appearance or raw pallets-per-hour wherever
the app is showing a personal production average.

For each person, metric scope, and date:

1. Sum all production rows for that scope on that date.
   - A work-center scope includes only that work center.
   - A group scope includes all member work centers in that group.
   - The Repairs role scope includes every work center whose skill/category is
     `Repair`.
   - The Dismantlers role scope includes every work center whose skill/category is
     `Dismantler`.
2. If credited scope hours are under 4.0 hours, disregard that
   person-scope-day.
3. If credited scope hours are 4.0 or more, normalize production to a full
   standard day:

   ```text
   normalized_units = units / credited_hours * standard_full_day_hours
   ```

4. Count that as one qualified day.

The dashboard average is:

```text
average = sum(normalized_units for qualified days) / qualified_day_count
```

`standard_full_day_hours` comes from
`shift_config.productive_minutes_per_day() / 60`, not the specific day's custom
hours. This makes short Saturdays and partial custom-hour days comparable to a
normal full day.

## Shared Metric Consumers

Use this normalized full-day metric in:

- New Recycling Leaderboard TV.
- Existing `/staffing/leaderboards` Best Averages tables:
  - per-WC averages
  - group averages
- Employee/player cards:
  - production group stat bubbles
  - per-work-center average column

Do not change live throughput KPIs such as `/recycling` `pallets/hr/person`.
Those are live operational rates, not personal historical averages.

Do not change raw totals such as total units, total downtime, days absent, or
days late.

Do not change Trophy Case awards in this pass. Trophy/award scoring currently
uses its own pph-based rules and manual override workflows. Trophy normalized
averages are deferred to a separate future design.

## Eligibility Rules

YTD and L30 qualify independently.

For each role and span:

1. Compute every person's qualified day count.
2. Find the leader's qualified day count for that span.
3. Minimum days required:

   ```text
   ceil(leader_qualified_days * 0.10)
   ```

4. A person's cell for that span is eligible only if their qualified day count
   is at least that minimum.

Rows include the union of people who qualify in either span. This allows
someone who has enough recent L30 days to appear even if their YTD sample is too
small.

When a person appears because one span qualifies but the other does not, the
unqualified cell shows:

```text
not enough days
```

The visible count label is plain `days`, not `qualified days`, `q-days`,
or `actual times`. For shared non-TV surfaces, the same rule applies: `days`
means days that met the 4-hour cutoff for that metric scope.

## Sorting

Each role table is sorted by YTD first:

1. Eligible YTD average descending.
2. YTD qualified day count descending.
3. Name ascending.

People who only qualify for L30 but not YTD appear after YTD
qualified people. Within that lower group, sort by:

1. L30 eligible average descending.
2. L30 qualified day count descending.
3. Name ascending.

## Visual Design

Use the approved Layout A mockup:

- Full TV header with title `Leaderboard`.
- Right-side date/range label showing YTD and L30 ranges.
- No green rule pill in the header.
- Three content columns:
  - Repairs
  - Dismantlers
  - Gold Ribbons
- Dark TV theme consistent with the existing `/tv` dashboards.
- Names render white on the dark background.
- Each leaderboard row shows:
  - Rank
  - Name
  - YTD Avg with days underneath, or `not enough days`
  - L30 Avg with days underneath, or `not enough days`
- Do not show a days label underneath the name.
- Column thresholds render near the section title, for example:
  - `YTD min 13 days`
  - `L30 min 2 days`

The final route relies on app CSS/templates, not the brainstorming mockup
file under `.superpowers/`.

## Gold Ribbons

The ribbon column shows the last 12 calendar months, newest first. Each month
has two gold holders:

- Repair gold
- Dismantler gold

Each gold holder is the best single qualifying person-role-day in that month,
using the same 4-hour cutoff and normalized full-day score as the leaderboards.

Each ribbon cell shows:

- Role label: `Repair` or `Dism`
- Person name
- Day and amount, for example `Jul 2 - 118`

The amount shown is the normalized full-day amount, rounded for display.

Manual trophy override behavior is not part of this v1. This dashboard uses
computed production history as its source of truth.

## Data Source

Use `production_history.daily_records(start, end)` / `production_daily` as the
source. It already provides per-day, per-person, per-work-center:

- `day`
- `person`
- `wc`
- `units`
- `hours`
- `downtime`

Use `staffing.LOCATIONS` to map work centers to role/category (`Repair` or
`Dismantler`).

Create a shared pure helper module for normalized production metrics:

```text
src/zira_dashboard/production_metrics.py
```

The helper accepts explicit records, scope definitions, and standard-day
hours so it can be unit-tested without the database.

Use this lower-level shape:

```python
normalized_average_by_person(
    records: list[dict],
    *,
    wc_names: set[str],
    standard_full_day_hours: float,
    min_hours: float = 4.0,
) -> list[dict]
```

This returns one row per person:

- `name`
- `avg_units`
- `days`
- `total_units`
- `total_hours`

It groups records by `(person, day)` before applying the 4-hour cutoff, so
multiple work centers inside the same scope combine into one qualified day.

The Recycling Leaderboard TV can use a route-specific wrapper:

```python
build_recycling_leaderboard(
    records: list[dict],
    *,
    today: date,
    standard_full_day_hours: float,
    wc_role_by_name: dict[str, str],
) -> dict
```

It returns:

- `ytd_start`, `ytd_end`, `l30_start`, `l30_end`
- `roles["Repair"].rows`
- `roles["Dismantler"].rows`
- per-role span thresholds
- `ribbons`, one row per month with Repair and Dismantler winners

## Routes And TV Registry

Add a dedicated route for the new dashboard:

```text
GET /tv/recycling-leaderboard
```

Also add a TV display kind so the Settings -> TVs picker can point a physical
TV at this view:

```text
vs_recycling_leaderboard
```

Update all kind allowlists/checks consistently:

- `tv_displays_store.save`
- `routes/tv_displays.py`
- `routes/settings.py` dashboard picker
- `_schema.py` `tv_displays.kind` constraint
- tests for TV display store/routes

Add `Recycling Leaderboard` to the default TV display seed list with kind
`vs_recycling_leaderboard`. Existing installations with TV rows already present
will not be reseeded, matching the current store behavior.

## Template And CSS

Add a new Jinja template and CSS file rather than overloading
`recycling.html`.

Files:

- `src/zira_dashboard/routes/recycling_leaderboard.py`
- `src/zira_dashboard/templates/recycling_leaderboard_tv.html`
- `src/zira_dashboard/static/recycling_leaderboard.css`

The template includes:

- `/static/tv-mode.css`
- `/static/tv-refresh.js`, matching the existing `/tv/recycling`,
  `/tv/new`, and `/tv/{wc}` behavior
- the shared `_tv_header.html` macro with `name="Leaderboard"` and a recycling
  crumb so it matches the existing TV chrome

The dashboard is TV-only in v1. No interactive screen-mode editor is needed.

## Existing Leaderboards

Update `/staffing/leaderboards` Best Averages to use the shared normalized
metric for `Avg/day`.

Per-WC averages:

- Scope is one work center.
- Multiple records for the same person/date/WC collapse into one day.
- Under 4 hours in that WC on that date is ignored.
- `name_count` becomes the normalized metric's `days`.
- `avg_units` becomes normalized full-day pallets/day.

Group averages:

- Scope is all member work centers in the group.
- A person can work multiple group member WCs on the same date; sum the group's
  units and hours before applying the 4-hour cutoff.
- `top_wc` remains the work center the person most often worked in the raw
  records for that range. It is descriptive only and does not affect the
  normalized average.

Keep `Avg %` behavior as-is for now. It is goal-relative and already uses
per-day productive minutes; changing it is separate from normalized pallets/day.

## Employee / Player Cards

Update the player card production metrics to use normalized full-day averages.

Group stat bubbles:

- Replace pph values with normalized full-day averages.
- Label them `Full-day avg`.
- Unit suffix is `pallets/day` or `per day`, not `pph`.
- Days shown, when shown, use the 4-hour qualified-day count.

Per-work-center table:

- Replace `Avg (pph)` with `Full-day avg`.
- Use the same per-WC normalized helper as the leaderboards.
- The existing `Days` column shows qualified days for that work center in
  the selected range.
- Keep `Units` and `Downtime` as raw totals.

The per-day breakdown table remains raw production by date and work
center. It is an audit trail, not an average metric.

## Empty And Edge States

- If there is no production history for a role, show an empty state inside that
  role column.
- If no one reaches the 4-hour cutoff in a span, show no average cells for that
  span and threshold `min 0 days`.
- If a person has zero hours after filtering, exclude the sample to avoid divide
  by zero.
- If the standard full day is zero for any configuration issue, fall back to no
  averages and show an empty state rather than crashing.
- Current-day YTD/L30 rows may include today's partial production only after a
  person reaches 4 hours in the role. Before 4 hours, today contributes nothing.

## Testing

Unit tests for the pure helper:

- Under 4 hours is excluded.
- Exactly 4 hours qualifies.
- 4+ hour short day is normalized to standard full-day hours.
- Multiple Repair WCs on the same person/date sum before the 4-hour cutoff.
- Repair and Dismantler samples qualify independently.
- YTD and L30 thresholds qualify independently.
- A person who qualifies in L30 but not YTD appears with YTD `not enough days`.
- Sorting puts YTD-qualified rows first, then L30-only rows.
- Monthly ribbons use the same cutoff and normalized score.
- Shared helper groups by `(person, day)` before applying the 4-hour cutoff.
- Shared helper ignores under-4-hour days.
- Shared helper normalizes exactly-4-hour and longer days to standard full-day
  hours.

Existing leaderboard tests:

- Best Averages `Avg/day` normalizes 4+ hour short days.
- Best Averages excludes under-4-hour days from both average and day count.
- Group Best Averages sum multiple group WCs on the same date before the cutoff.

Player card tests:

- Group stat bubbles show normalized full-day averages, not pph.
- Per-WC table shows `Full-day avg`, not `Avg (pph)`.
- Per-WC `Days` uses qualified days.
- Raw units and downtime totals are unchanged.

Route/render tests:

- `/tv/recycling-leaderboard` returns 200.
- The page includes the TV theme attribute and expected title.
- Names render via CSS intended for dark TV mode.
- Settings TV picker includes `Recycling Leaderboard`.
- TV display registry dispatches `vs_recycling_leaderboard`.

Static/visual guards:

- No days are rendered underneath the name.
- `q-days` does not appear.
- `actual times` does not appear.
- `not enough days` appears for unqualified cells.

## Out Of Scope

- Screen-mode manager page for this leaderboard.
- Per-display layout editing.
- Manual overrides for this new ribbon column.
- Goal/%-of-goal scoring.
- Showing raw actual appearances in addition to qualified days.
