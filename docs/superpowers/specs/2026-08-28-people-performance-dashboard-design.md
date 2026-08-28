# People Performance Manager Dashboard Design

**Date:** 2026-08-28
**Status:** Approved; implementation plan ready

## Goal

Give managers one live, people-first view of the plant day. The dashboard must
show everyone who clocked in, where each person worked over time, when they
transferred, whether they met the goal for each metered work-center stint, and
how downtime affected them. Tablet forklift drivers receive a role-appropriate
call-performance timeline rather than a fake production goal.

The dashboard complements the existing work-center dashboards. It does not
replace them.

## Approved product decisions

1. Show everyone who clocked in at any point on the selected day, including
   people who have already clocked out.
2. Use one shared time axis from the selected day's configured shift start to
   shift end.
3. Support a live, auto-refreshing Today view and review of one historical day
   at a time. Do not combine several days into one timeline.
4. Present one ranked roster in three fixed sections:
   - Metered production
   - Tablet forklift
   - Other non-metered people
5. Keep non-metered people below metered production and forklift drivers.
6. Within each section, show active people needing attention first, healthy
   active people next, and completed shifts last.
7. Give each person exactly one row. For Today, the current role decides the
   section. After clock-out, the final role decides the section. Earlier roles
   remain visible in that row's timeline.
8. Use a labeled location ribbon above the metric track. Work-center changes
   receive a clear divider and transfer arrow.
9. Reserve red and green for goal performance. Work-center identity uses a
   separate stable color palette plus text labels.
10. Show scheduled breaks and lunch as neutral hatched gaps. They do not create
    transfers, grow goals, or count as downtime.
11. Score every metered stint independently. A transferred worker never
    inherits an earlier stint's deficit or downtime.
12. Show rolling 30-minute uptime over metered production segments.
13. Calculate the person's overall goal percentage by dividing total credited
    units by total segment goals, not by averaging segment percentages.
14. For Tablet forklift segments, show calls per time bucket, rolling on-time
    percentage, and individual late-call markers.
15. Make the first release an interactive manager desktop/tablet view. A
    hands-off TV rotation is out of scope.

## Scope

### In scope

- Current-day and single historical-day people timelines.
- Attendance-backed locations and transfers.
- Metered production actuals, goals, results, and downtime.
- Forklift calls, on-time status, handling time, and existing driver score.
- Neutral presentation for roles without applicable metrics.
- Attention sorting, source-health warnings, and accessible segment details.
- Responsive manager desktop and tablet layouts.

### Out of scope

- Multi-day combined timelines.
- A TV or unattended rotation mode.
- Editing attendance, transfers, goals, downtime, or forklift events from this
  dashboard.
- A new production-credit or forklift-score formula.
- Replacing the existing work-center dashboards, leaderboards, player cards,
  staffing board, or exception-correction workflows.
- Notifications, paging, or manager acknowledgements.
- Building the canonical Odoo attendance-location projector. That separately
  approved project is a dependency, not a hidden part of this dashboard build.

## Manager workflow

The page opens on Today and refreshes live data approximately every 30 seconds.
The header contains:

- the selected date and a single-day date picker;
- a Today shortcut;
- counts for working now, worked earlier, and needing attention; and
- a Needs attention filter.

The fixed section order provides a predictable plant-wide scan. Each section
shows its people in this order:

1. active rows with invalid or uncertain live data;
2. active rows with performance concerns;
3. healthy active rows; and
4. completed shifts.

Names break ties so equal states do not jump randomly. Auto-refresh preserves
the user's date, filter, scroll position, keyboard focus, and an open detail
popover when the referenced segment still exists.

## Shared person-row anatomy

Every row has three regions.

### Identity

The left side shows the person's name and current status. Status is one of:

- working now;
- clocked out at a local time;
- location missing;
- location conflicting;
- location unmapped; or
- source stale.

Completed rows remain readable but are visually quieter than active rows.

### Timeline

The middle uses the page's shared configured shift window. Attendance and
location spans are clamped to that visible window, and every summary metric is
calculated over the same window.

The upper ribbon shows the exact mapped work center or other known location.
Each location is always named; color is only a secondary aid. A transfer gets a
divider and arrow at the exact boundary. Short spans retain truthful width and
expose their full label through the shared details interaction.

Planned breaks use one consistent hatched treatment across all rows. A lunch
split for the same person and same work center does not become a transfer.

The lower track changes by interval type:

- metered production: red/green goal performance plus rolling uptime;
- Tablet forklift: blue call-volume columns plus rolling on-time; and
- non-metered: neutral gray.

A row may contain all three interval types when the person changes roles.

### Summary

The right side shows four role-specific values. The current role owns an active
row's primary summary; the final role owns a completed row's primary summary.
Details for earlier roles remain available from their timeline intervals.

## Metered-production intervals

The dashboard reuses the existing resolved work segments and shared production
segment scorer. It must not implement a second sample-credit or goal engine.

For each metered interval:

- credit timestamped meter production only while the interval is active;
- retain the scorer's existing rules for simultaneous workers and sample
  conservation;
- calculate the goal from that work center's configured rate and productive
  minutes;
- exclude configured breaks and approved machine-breakdown exclusions exactly
  as the current dashboard scorer does;
- show green when credited actual is at least the interval goal;
- show red when credited actual is below the interval goal;
- compare an open interval to the goal earned through the live cap time; and
- freeze a closed interval's result at transfer or clock-out.

The next interval starts independently with the incoming work center's rate.
It does not inherit actuals, goals, deficits, or downtime from the prior
interval.

Selecting an interval reveals:

- work center;
- local start and end, or Working now;
- credited units;
- segment goal;
- ahead or behind result;
- productive minutes; and
- downtime minutes.

### Production summary calculations

For every scoreable metered interval `i` in the row:

```text
overall_goal_pct = 100 * sum(actual_units_i) / sum(goal_units_i)
```

The value is unavailable when the denominator is zero. It is never replaced
with zero.

Whole-day person uptime uses only metered minutes for which the person had a
valid location:

```text
available_minutes = productive metered minutes after planned exclusions
working_minutes = max(0, available_minutes - attributed downtime minutes)
overall_uptime_pct = 100 * sum(working_minutes) / sum(available_minutes)
```

Downtime is intersected with the person's work-center interval. Its start for a
newly arrived worker is no earlier than that worker's arrival, so a transfer
never assigns an older stop to the incoming worker.

The production summary shows:

- weighted goal percentage;
- whole-day uptime percentage;
- total attributed downtime minutes; and
- number of distinct work centers visited.

### Rolling uptime line

At each point, the line represents uptime within the preceding 30 minutes,
intersected with the active metered interval and productive schedule. It dips
during a stop, dips farther when the stop continues, and returns toward 100%
after healthy production. It does not bridge transfers, planned breaks,
unavailable data, or a period with no eligible available minutes.

The right-side uptime remains the whole-day value; the line is the recent
operating pattern.

## Tablet forklift intervals

Tablet forklift drivers use real call activity instead of a production goal.
For each Tablet interval:

- blue columns show completed-call counts in 15-minute plant-local buckets;
- a line shows on-time percentage for eligible calls completed in the preceding
  30 minutes;
- a red marker identifies each explicitly late completion; and
- a window with no eligible completed calls leaves a gap rather than claiming
  either 0% or 100% on time.

The forklift summary shows:

- total completed calls;
- whole-day on-time percentage;
- total minutes actively handling calls, derived from handling duration; and
- the existing weighted driver score.

The score remains unavailable until the existing minimum-call requirement is
met. Calls with no on-time/late classification count toward call volume but not
the on-time denominator. The dashboard does not reinterpret missing status as
late.

Forklift driver identity uses the existing explicit name map and safe unique
first-name resolution. An ambiguous driver remains unmatched rather than being
credited to the wrong plant person.

Forklift activity is performance evidence, not location truth. A call mapped to
a person without a valid Tablet location does not repair the attendance
timeline. The person keeps the amber location warning; the unattached call is
available in details and through the existing exception path, but it is not
placed inside a falsely inferred Tablet span.

## Mixed-role and non-metered behavior

A person appears once even when they move among metered production, Tablets,
and other work. The location ribbon remains continuous while the lower metric
track changes its presentation at role boundaries.

The current or final role determines the primary summary and section, but a
detail interaction exposes the earlier interval's native metrics. The row never
combines production units and forklift calls into one synthetic score.

Non-metered intervals are neutral. A person whose current or final role is
non-metered receives this summary:

- clocked duration inside the displayed shift window;
- current or final location;
- number of distinct locations visited; and
- working-now or clocked-out status.

Goal and uptime are displayed as N/A, never zero.

## Attention states and sorting

The dashboard uses explicit reason badges, not an unexplained composite
attention score.

Within the applicable section, the order is:

1. conflicting, missing, unmapped, or stale current location;
2. active metered production behind its current earned goal;
3. active metered production in an uptime warning or bad band;
4. active forklift drivers below the configured on-time floor or with a newly
   late call in the preceding 30 minutes;
5. healthy active rows; and
6. completed rows.

Production deficit percentage is the primary ordering within the behind-goal
state; rolling uptime breaks ties. The existing uptime display bands remain the
shared convention: good at 90% or higher, warning from 80% through 89.9%, and
bad below 80%. Forklift on-time uses the resolved forklift score configuration,
whose current default floor is 80%.

The fixed three-section order does not change. A missing-location person with
no role appears first within Other non-metered, not above the production or
forklift sections.

## Components and boundaries

### Canonical location timeline adapter

The dashboard consumes the canonical attendance-location spans defined by the
approved Odoo live work-location design. It must not invent a dashboard-only
schedule fallback or a different interpretation of conflicts. If that canonical
projector is not yet available, it is a prerequisite for strict live-location
behavior.

The adapter returns selected-day people, day clock spans, validated location
spans, status, and source freshness.

### People-performance assembler

A focused, mostly pure module owns:

- clipping spans to the display window;
- joining planned break presentation without creating false transfers;
- attaching existing production scores and downtime;
- attaching forklift event buckets;
- calculating row summaries and rolling series;
- determining current/final role; and
- assigning section and stable attention order.

It owns no HTTP fetching, template rendering, Odoo writes, Zira writes, or
manager corrections.

### Forklift completion event store

The current daily forklift tables are sufficient for leaderboards but not for
historical timelines. Add an idempotent completion-event store containing at
least:

- external completion identity;
- the external feed's `createdAt` event time, converted to plant-local time;
- driver identity and display name;
- workstation identity when available;
- on-time and late status;
- response duration;
- handling duration; and
- ingestion/update timestamps.

Upsert by the external identity. Repeated warmer runs, backfills, and page
refreshes must not duplicate a call. Daily aggregate tables and the existing
score calculation remain authoritative for existing leaderboard behavior. The
timeline deliberately uses `createdAt` because that is the timestamp used by
the current completion aggregator; this keeps timeline buckets and saved daily
totals reconcilable.

### Page route and partial renderer

The route validates one date, loads the four input families concurrently where
safe, passes them to the assembler, and renders the full page or live rows
partial. Browser code owns only refresh timing, filter interaction, accessible
details, and preservation of local view state.

### Templates and styling

Use one shared row template with interval-specific subcomponents. Production,
forklift, and neutral segments must not each recreate identity, time geometry,
breaks, transfer markers, accessibility labels, or summary layout.

## Live and historical behavior

Today refreshes about every 30 seconds. Open attendance and metric intervals
use the same effective plant-local cap time so the location ribbon, goals,
uptime, and calls cannot disagree about Now.

Historical dates have no open intervals. They are cacheable after source data
has settled. A confirmed historical Odoo attendance change or forklift event
update invalidates only the affected day.

## Failure policy

Source failure must never look like poor worker performance.

- Stale attendance retains the last verified location with a visible stale
  state. It does not silently fall back to the staffing plan.
- Missing, conflicting, or unmapped location is amber and uncertain. Under the
  canonical strict-attribution rules, uncertain spans earn no production.
- Missing production or forklift data displays Unavailable, not zero.
- A work center without a valid positive goal remains neutral and makes no
  ahead/behind claim.
- A forklift period with no calls is an empty volume period; missing API data is
  a distinct unavailable state.
- A per-row metric assembly failure degrades that metric region without hiding
  the person's attendance row.
- A full page remains available when one external source fails, using the last
  verified cached data and a source-health warning where possible.

## Accessibility and interaction

- Red and green always include Ahead or Behind text in details.
- Work-center identity always includes text and never relies on ribbon color.
- Transfers use a label, divider, and arrow.
- Downtime and on-time lines have textual names and right-side numeric
  summaries.
- Every interval is reachable by keyboard and exposes the same details on
  hover, focus, or tap.
- Focus is visible, Escape closes a pinned detail, and tapping outside closes
  it.
- Short intervals retain accurate geometry; their full details remain
  accessible without expanding the row.
- Completed rows may be visually quieter but must retain readable contrast.

## Validation

### Pure calculations

- Clip early and late attendance to the configured display window.
- Keep one worker across a planned lunch without creating a transfer.
- Mark real work-center and role changes as transfers.
- Score open and closed production intervals independently.
- Change the goal rate at a work-center transfer without inherited deficit.
- Preserve the existing simultaneous-worker production-credit rules.
- Calculate weighted overall goal percentage from actual and goal sums.
- Intersect downtime with person-location spans and exclude downtime before
  arrival.
- Calculate whole-day and rolling 30-minute uptime with planned exclusions.
- Bucket forklift calls in plant-local 15-minute intervals.
- Calculate rolling and whole-day on-time without treating unknown status as
  late.
- Leave rolling lines discontinuous across no-data periods.
- Select the correct primary role and summary for mixed-role rows.
- Apply stable attention ordering and completed-shift placement.

### Persistence and ingestion

- Re-ingesting the same forklift completion is idempotent.
- A changed completion updates the existing event without duplication.
- Driver-name ambiguity never credits the wrong person.
- Historical event rows reconstruct the same daily totals as the existing
  aggregate path within rounding tolerance.

### Route and rendering

- Today includes active and earlier clocked-out people.
- A historical day contains only closed intervals.
- The section order is always production, forklift, then other.
- Production, forklift, neutral, break, transfer, stale, and unavailable states
  render with complete accessible labels.
- A mixed-role day renders one person row, not duplicates.
- The Needs attention filter keeps all qualifying reason states.
- Auto-refresh preserves the active date, filter, scroll position, focus, and
  open details where possible.

### Failure and visual checks

- Attendance, production, and forklift failures each degrade independently.
- No-data states cannot be confused with zero performance.
- Desktop and tablet widths retain a readable timeline and summary.
- A busy realistic fixture verifies scrolling, sticky time context, attention
  ordering, short transfers, several transfers, and completed shifts.
- Keyboard, tap, pointer, contrast, and non-color status checks pass.

## Success criteria

A manager can answer these questions from one page without opening a work-center
dashboard:

- Who worked today, and who is still here?
- Where is each person now, and where were they earlier?
- Who transferred, and when?
- Which production stints are ahead or behind their own work-center goals?
- Who is being affected by recent downtime?
- How many forklift calls did each Tablet driver complete, and were they on
  time?
- Which people need attention, and why?
