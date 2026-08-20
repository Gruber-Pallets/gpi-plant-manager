# Metered Worker Production Segments Design

## Goal

Make every single-day department production bar distinguish between the worker
currently at a metered work center and the workers who produced pallets there
earlier in the day.

Each worker must be scored independently for only the time and production in
their own attendance segment. A worker who transfers in never inherits the
previous worker's deficit. A worker who transfers out keeps a visible,
completed result in the production bar.

## Existing behavior and root cause

The scheduler is the planned placement, while the live department dashboards
correctly treat Odoo's work-center-tagged attendance as the source of truth.
When a worker transfers, the dashboard removes that worker from the old work
center's live label. The old work center still shows all meter production, but
its label falls back to `(no assignment)` because the bar model contains only
the current operator name.

On August 20, Humberto S. was scheduled at Repair 4 and worked there from
7:00 AM until 2:33 PM. He then transferred to Hand Build #1. At 2:41 PM,
Repair 4 correctly had no current worker and correctly showed 516 pallets, but
the dashboard no longer named Humberto as the person who produced them.

The dashboard already resolves complete worker segments for pace and labor
math, and Zira totals already contain timestamped positive-unit samples. The
missing layer is a shared per-segment score model that the department bar can
render.

## Scope

- Apply the behavior to every metered work center shown on the Recycling and
  New department dashboards. Do not special-case Repair 4.
- Apply worker segments to all completed and live single-day views, including
  their TV variants.
- Keep multi-day range bars unchanged. Combining worker segments from several
  days into one row would be unreadable and is not needed for this feature.
- Preserve the scheduler, attendance precedence, production totals, targets,
  production-history credit, inline attribution action, saved widget layout,
  and widget customization behavior.

## Approved presentation

### Left-side identity

On today's live dashboard:

- If one or more workers are currently at the work center, show their names on
  the left with the work-center name beneath, as today.
- If earlier worker segments exist but nobody is there now, show
  **No one here now** with the work-center name beneath.
- If all production is unattributed and no worker history exists, keep the
  work-center-first `(no assignment)` presentation and its existing assignment
  action.

On a completed single-day view, show the work-center name on the left. The
worker names and times live in the completed production segments, so the past
view does not imply that anyone is there now.

### Continuous independent segments

Keep one continuous horizontal track per work center. Within it, render one
ordered block for each worker segment:

- Credit `actual_units` from meter samples recorded while that worker's segment
  was active.
- Calculate `goal_units` only from that segment's productive time.
- Give the segment a runway of `max(actual_units, goal_units)` pallet units.
- Fill the actual portion red when `actual_units < goal_units` and green when
  `actual_units >= goal_units`.
- When a worker finishes behind, preserve the missing portion as a visible
  neutral hatched gap between the actual fill and the finish line.
- When a worker finishes ahead, extend the green fill beyond the finish line.
- Begin the next worker's block after the prior block's full runway. This makes
  the next worker independent: the prior shortfall stays visible but never
  becomes the incoming worker's goal.

The track uses one pallet-unit scale across the work centers in the widget so
their visual sizes remain comparable. A station's track span is the sum of its
segment runways; the widget scale is based on the largest station span with the
same breathing room used by the current bars.

Completed segments use a clearly labeled checkered finish line. The active
segment keeps the familiar solid moving goal line. When the active worker
transfers out or the shift ends, that line freezes as the segment's checkered
finish line. The incoming worker receives a new solid goal line that begins at
zero goal at their transfer-in time.

Show each worker's name, local start/end time, actual units, goal units, and
ahead/behind result with their segment. Keep the right-side station summary as
total actual pallets divided by the sum of scored worker goals. The segment
color and result remain independent even when that station-wide summary is
behind.

If a segment is too narrow to hold its full visible label, keep its truthful
width and place an attached label immediately beneath the track. Do not rely on
hover text because the TV dashboards must remain understandable.

### Vertical orientation

For the existing vertical widget orientation, stack the same independent
segment runways bottom-to-top with horizontal finish markers. Because a narrow
column cannot hold full horizontal names, show the complete worker/time/result
list directly below that column. Color must never be the only indication of a
result.

## Scoring and attribution rules

The segment scorer must use the same rules as production history:

- A positive Zira sample belongs to every worker segment active at that work
  center at that instant.
- If two or more workers overlap, split that sample equally among them. This is
  the app's existing shared-credit rule.
- Use half-open intervals: `start_utc <= sample_time < end_utc`. A reading
  exactly at a transfer time belongs to the incoming worker.
- Keep separate segments when the same person leaves and later returns to the
  same work center.
- Calculate goal minutes with the dashboard's existing breaks-only productive
  time and machine-breakdown exclusions. Do not reintroduce the old partial
  time-off goal shrinkage.
- Exclude full-day absent people through the existing segment-resolution path.

The station summary includes every meter unit. Worker actuals include only the
units credited to worker segments. Units with no active worker segment become
neutral **Unassigned production** rather than being silently credited or
dropped.

## Components and data flow

### Shared segment scorer

Add a focused production-segment module with a small immutable score model and
two pure layers:

1. A credit function assigns timestamped units and productive minutes to each
   resolved segment. Production history and the dashboard both use this layer.
2. A dashboard scoring function adds the target rate, goal units, live state,
   result, and runway measurements to the credited segments.

The combined inputs are:

- resolved `assignment_windows.WorkSegment` values;
- timestamped samples by work center;
- total units by work center for the no-sample compatibility fallback;
- target rate by work center;
- the existing productive-minutes callback; and
- the effective live cap time.

Its output is an ordered per-work-center list containing at least:

- worker name or the explicit unassigned label;
- start and end UTC timestamps;
- actual and goal units;
- credited productive minutes;
- active/completed state;
- ahead/behind/neutral result; and
- a stable ordering key for rendering.

The module owns sample-to-segment credit and pure goal calculation, but it owns
no database, Odoo, Zira, request, template, or CSS behavior. Callers supply the
productive-minutes callback appropriate to their existing contract: the
dashboard callback includes its approved breakdown exclusions, while
production history preserves its current hours and separate excluded-minutes
fields.

### Production history

Refactor `production_history.attribute_for_segments` to aggregate the shared
credit layer's units and productive minutes into its existing
person/work-center output. This creates one source of truth for sample credit
without changing the production-history API, its separate breakdown exclusion
field, or stored results.

### Department data preparation

`routes/departments.py` continues to resolve attendance-backed work segments.
For single-day department output, pass those segments plus each
`StationTotal.samples` collection into the shared scorer. Return the score lists
beside the existing per-work-center units, goals, states, and current operator
labels.

`recycling_data.build_bars` attaches the single-day score list and the runway
measurements needed by the shared template. Range aggregation continues to
discard segment detail and use its existing aggregate values.

### Shared template and styling

Extend `_department_dashboard_widgets.html` and `recycling.css` so the same
segment markup serves both department dashboards, screen and TV modes, and
horizontal and vertical orientations. Keep the existing bar value positions,
target configuration, sorting, operator dashboard links, assignment action,
and responsive layout behavior.

## Fallbacks and error handling

- If live Odoo attendance is unavailable or stale, preserve the existing saved
  schedule fallback. Do not blank worker identity merely because the live source
  failed.
- If a station has a total but lacks timestamped samples, distribute the
  otherwise-unaccounted units among its segments by productive time, matching
  the existing production-history compatibility rule. For multiple segments
  belonging to the same worker, distribute by each segment's productive time.
- If a sample has no active worker, keep it in the station total and create or
  extend the neutral Unassigned production segment.
- If a segment has no positive target, render its actual production neutrally
  with no ahead/behind claim and no false finish line.
- Sort valid samples by timestamp and ignore non-positive sample units, matching
  the upstream leaderboard contract.
- Preserve the page's existing fail-soft behavior during data-source errors; a
  segment-scoring failure must not hide the station's production total.

## Accessibility and wording

- Pair red and green with plain `ahead` or `behind` text.
- Distinguish a completed checkered finish line from a live solid goal line by
  shape, wording, and color contrast.
- Keep full worker identity and times visible on TVs without hover.
- Use `No one here now`, not `(no assignment)`, when the station has credited
  worker history but no active worker.
- Keep `(no assignment)` and the assign action for truly unattributed work.

## Validation

### Pure scoring tests

- One live segment below and above its moving goal.
- One completed segment below, equal to, and above its finish goal.
- A transfer from Humberto at Repair 4 to an incoming worker, proving two
  independent blocks and no inherited deficit.
- A sample exactly on the transfer boundary goes only to the incoming worker.
- Multiple sequential transfers retain every completed segment.
- The same worker leaves and returns, producing two separate segments.
- Simultaneous workers split each sample equally.
- Breaks and approved machine breakdowns reduce goal minutes exactly as they do
  today.
- Unattributed samples remain in a neutral segment.
- Totals without samples use the productive-time fallback.
- Zero-target segments remain neutral.

### Integration and rendering tests

- The department route returns current and completed segments while keeping the
  current operator only at the live work center.
- Repair 4 shows Humberto's completed segment after he transfers out.
- Recycling and New metered work centers use the shared behavior.
- Production-history totals remain unchanged after adopting the shared scorer.
- Completed single-day and live single-day views render the correct left label.
- Multi-day range views remain unchanged.
- Horizontal bars render the continuous blocks, shortfall gaps, current goal,
  completed finish lines, and narrow-label fallback.
- Vertical bars render equivalent segment/result information with a visible
  worker list.
- Screen and TV modes both expose full names, times, and non-color status text.

## Out of scope

- Changing scheduler assignments or transfer records.
- Changing work-center target rates or the definition of productive time.
- Changing historical production credit or simultaneous-worker sharing.
- Showing worker segments in multi-day aggregate bars.
- Adding a new manual correction workflow; the existing attribution action
  remains the correction path for unattributed production.
