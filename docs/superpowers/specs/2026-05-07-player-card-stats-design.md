# Player Card Stats Redesign

**Date:** 2026-05-07
**Status:** Approved (brainstorming → implementation planning)

## Context

The player card at `/staffing/people/{name}` opens with a row of summary
"bubbles" (Days worked, Total units, Total downtime, Days Absent, Days
Late), followed by a per-WC totals table, then a per-day breakdown,
then an Attendance section.

The current header doesn't tell Dale what he actually wants to see at a
glance: "how fast is this person on the stations they actually work?"
The Total Units bubble sums everything regardless of station, the WC
table shows raw totals but no rate, and the most natural grouping
(Repairs vs. Dismantlers vs. Juniors) isn't surfaced anywhere.

## Goals

1. Replace the **Total units (split)** bubble with one **group-average pph**
   tile per registered group (e.g. Repairs, Dismantlers, Juniors).
2. Auto-hide group tiles when the person has no recorded hours in any
   WC belonging to that group.
3. Add an **Avg (pph)** column to the per-WC table, between **Units**
   and **Downtime (min)**.
4. Fix the table header alignment — `th.num` is currently left-aligned
   while `td.num` is right-aligned, so headers don't sit over their
   numbers.

## Non-goals

- No changes to the per-day breakdown table or Attendance section.
- No new data sources. Hours are already attributed per (person, WC)
  via `production_history.attribute_for_range`; the player card just
  isn't reading them yet.
- No re-thinking how hours are attributed across split-shift assignments.
  The player card uses the same `units / hours` math as the
  leaderboards and dashboard. If that math has known issues, fixing
  them is out of scope for this redesign.
- No filtering of the per-WC table to "Zira-only." The table continues
  to show every WC the person worked (Loading, Recycled, Tablets,
  etc.). The group filter is implicit because those non-Zira WCs
  aren't members of the Repairs/Dismantlers/Juniors groups.

## Design

### 1. Header layout

The existing `.pc-totals` grid stays. Drop the **Total units** stat
tile. Keep the other four (**Days worked**, **Total downtime**,
**Days Absent**, **Days Late**).

Below that, add a second tile row — `.pc-group-avgs` — one tile per
registered group with data:

```
[ Days worked ] [ Total downtime ] [ Days Absent ] [ Days Late ]

[ Repairs    ] [ Dismantlers      ] [ Juniors                  ]
[ 12.4 pph   ] [  9.8 pph         ] [  6.1 pph                 ]
```

Tiles render at the same visual weight as the existing stats but are
slightly larger so the pph reads as the headline number on the page.
A group with zero hours for this person renders no tile (no empty
slot, no "—").

If the person has zero hours in **every** registered group (e.g. an
office-only employee somehow viewed via the player card), the second
row is omitted entirely — no header, no empty grid.

### 2. Per-WC table

Updated columns:

```
Work Center | Days | Units | Avg (pph) | Downtime (min)
```

`Avg (pph)` is `units / hours` for that (person, WC) row, formatted
with one decimal. If hours == 0 (shouldn't happen for a row with
units, but defensive), show `—`.

CSS fix: add `table.pc th.num { text-align: right; }` so headers and
numeric cells right-align together.

### 3. Route changes

`routes/people.py::staffing_player_card` already iterates the per-WC
totals returned by `production_history.attribution_range`. The dict it
gets back per WC is `{units, downtime, hours, days_worked}`. Today the
route ignores `hours`. Two changes:

- The per-WC `rows` list gains an `avg_pph` field (rounded to one
  decimal, `0` when hours == 0).
- A new `group_avgs` list is computed: `[{name, pph}, ...]` —
  one entry per registered group with hours > 0 for this person.
  Computed by:
  1. `groups = work_centers_store.registered_groups()`
  2. For each group, find its WC names via
     `work_centers_store.members("group", group_name)`. Keep just
     `loc.name` strings.
  3. Sum `units` and `hours` across the person's rows whose WC name is
     in that set. If hours > 0, append
     `{name: group_name, pph: round(units / hours, 1)}`.
  4. Preserve registry order — the order the user defined groups in
     the Settings page is the order they appear on the card.

The template:
- `{{ '{:,.0f}'.format(total_units) }}` line (Total units bubble) is removed.
- A `{% if group_avgs %} <div class="pc-group-avgs"> ... {% endif %}`
  block is added under the existing `.pc-totals`.
- The per-WC table grows an `Avg (pph)` `<th class="num">` between
  Units and Downtime, and a matching `<td class="num">` per row.

## Components and data flow

```
GET /staffing/people/{name}?start=...&end=...
        ↓
attribution_range(start, end, client) — already includes hours
        ↓
person dict: {wc_name: {units, downtime, hours, days_worked}}
        ↓
        ├── rows[] — per-WC, now with avg_pph
        └── group_avgs[] — registered groups with units > 0
              (units/hours weighted across the group's WCs)
        ↓
template: header + group avgs row + WC table (with Avg column)
```

## Testing

**Unit tests** (`tests/test_player_card_stats.py` — new):

1. `test_avg_pph_per_wc_added_to_rows` — given a stub
   `attribution_range` return with `units=100, hours=10` for one WC,
   the route's context dict has `rows[0].avg_pph == 10.0`.

2. `test_group_avgs_hides_groups_with_no_hours` — stub
   `registered_groups()` to return `["Repairs", "Dismantlers"]` and
   stub `members("group", "Repairs")` and `members("group",
   "Dismantlers")` to return disjoint WC sets. Provide a person with
   hours only in Repairs WCs. Assert `group_avgs` contains exactly
   one entry, named "Repairs".

3. `test_group_avgs_hours_weighted_across_wcs` — Repairs has
   WCs A and B; person has units=50/hours=5 on A and units=100/hours=10
   on B. Expected pph for Repairs = (50+100)/(5+10) = 10.0.

4. `test_group_avgs_preserves_registry_order` — registered order is
   ["Juniors", "Repairs", "Dismantlers"]. Person has data in all
   three. `group_avgs` returns in that order, not alphabetical.

**Visual / manual:**

- Open `/staffing/people/<active-person>?start=<thismonth>&end=today`.
  Confirm: no "Total units" bubble; group tiles appear for the groups
  the person has worked in; per-WC table has Avg (pph) column with
  right-aligned headers.

DB-bound tests skip without `DATABASE_URL` (existing project pattern).
The new tests stub the attribution / work_centers_store calls and
don't need a DB.

## Files touched

- `src/zira_dashboard/routes/people.py` — compute `avg_pph` per row;
  build `group_avgs`; pass both to template.
- `src/zira_dashboard/templates/player_card.html` — drop Total units
  bubble; add `.pc-group-avgs` row under `.pc-totals`; add Avg (pph)
  column; CSS fix for `th.num` alignment.
- `tests/test_player_card_stats.py` (new) — unit tests for `avg_pph`
  and `group_avgs`.
- `CHANGELOG.md` — entry for the deploy.

## Implementation notes

- `hours` is already in the per-(person, WC) totals — no plumbing
  changes through `production_history` or `staffing` are needed.
- `registered_groups()` returns groups sorted by `lower(name)` (see
  `work_centers_store.py:272`). The route just iterates that list —
  no re-sort downstream.
- One-decimal formatting for pph matches the leaderboards/dashboard
  convention.
- `.pc-group-avgs` tiles reuse the same `.stat` styling as
  `.pc-totals` but bump the value `font-size` from `1.4rem` to
  `1.8rem` so the pph reads as the headline number on the page.
  Reuse existing CSS variables (`--accent`, `--panel`, `--border`) —
  no new color tokens.
