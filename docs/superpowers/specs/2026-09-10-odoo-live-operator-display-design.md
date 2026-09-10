# Odoo Live Operator Display

**Date:** 2026-09-10
**Status:** Approved through brainstorming

## Goal

Every live dashboard that identifies the current operator at a work center
must distinguish the staffing plan from physical presence:

- a planned operator is shown in gray until Odoo has that employee actively
  signed into that exact work center;
- an operator with an active Odoo attendance at that work center is shown in
  the normal full-strength text color, whether or not the person was planned
  there.

The Plant Scheduler remains a plan-only editing surface. Odoo attendance must
not alter its saved assignments or its presentation.

## Product decisions

1. Odoo work-center attendance is the authority for physical presence.
2. The saved schedule remains the authority for the staffing plan.
3. A planned name on a live dashboard is gray when the employee is not
   currently signed into that same work center in Odoo.
4. A name is full-strength only while Odoo currently places that employee at
   that work center.
5. An unplanned employee appears full-strength at the Odoo work center. The
   display does not add a special “from Odoo” or “not planned” mark.
6. A planned employee signed into a different work center remains gray at the
   planned center and appears full-strength at the actual Odoo center.
7. When an Odoo attendance closes or transfers away, the full-strength name
   disappears from that work center. Its gray planned name returns when
   applicable; otherwise the live display says “No one here now.”
8. Two valid active Odoo attendances at one work center show both people as
   physically present.
9. An attendance without a mapped work center does not make the employee
   full-strength at any work center.
10. Stale or unavailable Odoo data never leaves an old name displayed as
    physically present.

## Scheduler boundary

The Plant Scheduler must retain its old plan-only behavior and appearance.

Remove the Odoo preview additions from this surface:

- the Odoo preview banner;
- live-location badges below scheduled names;
- the Odoo preview / not-scheduled list;
- inbound live people inserted into work-center rows; and
- any other gray/black physical-presence treatment.

Attendance synchronization must not add, move, or remove saved schedule
assignments. The recently added attendance-to-schedule seating path must be
removed from the synchronization flow. The Scheduler’s Auto, defaults, manual
assignments, posting, reset, and schedule history continue to operate only on
the staffing plan.

No broad cleanup may infer that every generated assignment came from Odoo;
generated assignments also have legitimate scheduling sources. If deployment
left any persisted attendance-created seat, repair it only when its provenance
can be established safely.

## Display scope

Use the shared presence model anywhere the application presents a person as
the current operator of a work center, including:

- Recycling and New single-day dashboards;
- Recycling and New TV views;
- individual work-center dashboards;
- individual work-center TV views;
- current-operator labels used by live breakdown presentation; and
- future current-operator displays.

The Plant Scheduler is expressly excluded. Historical and multi-day views are
also excluded from gray/black current-presence styling unless they contain a
separate, explicitly live “current operator” field.

## Shared current-operator model

Create one read-only presentation model per work center with two ordered sets:

- `planned`: present-day scheduled operators after the existing full-day
  absence filtering;
- `physically_present`: valid, current, mapped Odoo work-center locations from
  the canonical attendance mirror.

The model derives display rows by employee identity:

- planned and physically present at this center → one full-strength name;
- planned here but not physically present here → one gray name;
- physically present here but not planned here → one full-strength name;
- neither → no name.

Use Odoo employee identity for matching whenever available, with the
application’s existing canonical roster-name mapping for display. Do not use
display-name equality as the primary identity because two employees can share
a name.

The model is read-only. It must not call schedule save functions, create
attendance records, transfer employees, or modify production attribution.

## Attendance source and freshness

Read current physical presence from the canonical local Odoo attendance
mirror, not from direct request-time XML-RPC calls and not from the retired
legacy open-attendance cache.

A physically present employee requires:

- a valid projected location;
- a mapped Plant Manager work center;
- an interval containing the frozen response time; and
- a healthy, non-stale mirror snapshot.

Freeze the mirror freshness and response time once per rendered response so
every current-operator label on that page uses the same truth boundary.

If canonical attendance is unavailable or stale:

- render planned names in gray;
- render no full-strength physical-presence names;
- expose the existing live-source unavailable indication where the surface has
  one; and
- never fall back to old open attendance as current truth.

## Live and historical separation

This feature changes only the current-operator presentation. It must not change
the existing work-segment pipeline used by dashboard bar charts.

Preserve without alteration:

- Odoo-derived historical work segments;
- midday transfers and worker handoffs;
- each segment’s start and end time;
- production allocation and station totals;
- per-segment and aggregate goals;
- red/green results, shortfalls, and finish markers;
- scheduled-break normalization;
- same-worker lunch joining;
- different-worker lunch transfers;
- overlapping-worker handling;
- conditional legacy-bar versus split-runway selection;
- “No one here now” behavior for a station with worker history but no current
  worker; and
- multi-day aggregate behavior.

The current-operator model must not become input to production scoring,
`coalesce_display_scores`, `worker_coverage_is_split`, bar geometry, or
production-history persistence.

Past worker names and segments remain visible in the bars after an attendance
closes. Only the separate current name changes from full-strength to gray or
empty.

## Example: Christian on Dismantler 3

Given:

- the saved plan has operators on Dismantlers 1, 2, and 4;
- the saved plan has no operator on Dismantler 3; and
- Odoo attendance places Christian C. at mapped work center Dismantler 3 from
  7:00 AM through 11:00 AM.

Expected behavior:

- at 8:15 AM, every live dashboard current-operator display shows Christian C.
  full-strength on Dismantler 3;
- the Plant Scheduler remains unchanged and does not schedule Christian there;
- Christian’s production and historical bar segment continue to use the
  existing attendance/production pipeline;
- after 11:00 AM, Christian is no longer the full-strength current operator;
- because nobody was planned there, the live current label says “No one here
  now”; and
- Christian’s completed 7:00–11:00 bar segment remains visible.

## Error and edge behavior

- A transfer closes physical presence at the old center and opens it at the
  new center at the same canonical boundary.
- A normal clock-out removes physical presence.
- A lunch closure removes full-strength current presence. A later attendance
  without a work center does not silently inherit physical presence for this
  display.
- An unmapped Odoo work center is not assigned to a Plant Manager station.
- A conflicting projected location produces no full-strength name at either
  conflicting center and retains the existing conflict signal.
- Full-day absences continue to filter the planned layer, but valid Odoo
  attendance remains authoritative for actual presence according to the
  existing attendance rules.
- Future-day and past-day schedule views do not claim current physical
  presence.

## Validation

Add shared model tests proving:

- planned-only names are gray;
- planned-and-present names are full-strength and deduplicated;
- unplanned but present names are full-strength;
- a person planned at one center and present at another has the correct style
  at both;
- multiple present people appear together;
- closed, unmapped, conflicting, stale, and unavailable attendance never
  appears full-strength; and
- Odoo employee identity, rather than display name, controls matching.

Add surface tests proving the same state renders consistently on:

- Recycling and New dashboards;
- their TV views;
- individual work-center screen and TV views; and
- live breakdown current-operator labels.

Add Scheduler regressions proving:

- no Odoo preview banner, badge, list, or inbound person renders;
- attendance sync never changes schedule assignments; and
- Auto, reset, manual assignment, posting, and schedule history retain their
  existing behavior.

Keep and extend the existing midday dashboard regressions proving:

- a morning worker and afternoon worker retain independent split segments;
- a transfer during lunch remains a split;
- the same worker across a scheduled lunch remains one display segment;
- a worker leaving a station vacant retains history and “No one here now”;
- overlapping operators retain split behavior;
- horizontal, vertical, screen, and TV bars retain their current rendering;
- goals, units, finish lines, and production credit remain unchanged; and
- multi-day range views remain unchanged.

## Out of scope

- Changing production attribution or enabling the full strict-attendance
  cutover.
- Redesigning the dashboard bar charts.
- Changing the saved staffing plan based on Odoo.
- Adding an Odoo status legend or unplanned-worker badge.
- Writing routine work-center transfers from Plant Manager.
