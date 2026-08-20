# Conditional Worker Segment Display Design

## Goal

Keep the original department production bar for a work center whose worker
coverage did not change during the productive part of the viewed shift. Show
the newer per-worker segment runway only when worker coverage was actually
split.

This keeps ordinary work centers familiar and compact while preserving the
transfer history that explains who produced the pallets when staffing changes.

## Approved behavior

### Original bar

Use the original bar when exactly one named worker continuously covers the
entire productive portion of the viewed shift window for that work center.

Odoo may represent one uninterrupted work-center assignment as separate
morning and afternoon attendance records around lunch. When the same worker
returns to the same work center across a configured scheduled break, treat
those records as one continuous worker segment. The scheduled break still
subtracts from the worker's goal; it does not create another finish line or
switch the station to the split format.

The original bar keeps:

- the current worker on the left for a live day;
- the work-center identity used by completed-day views;
- the single production fill;
- the normal station goal line; and
- the existing actual/goal station total.

A small amount of unassigned production does not switch an otherwise
continuous single-worker station to the segment runway. That production stays
included in the station total, as it did before worker segments were added.

### Split runway

Use the per-worker segment runway when named-worker coverage does not consist
of one continuous segment spanning the entire viewed shift window. This
includes:

- one worker transferring out and another transferring in;
- a worker leaving while the station remains vacant, including Humberto at
  Repair 4;
- a worker starting after the viewed shift window begins;
- a worker leaving before the viewed shift window ends;
- the same worker leaving and later returning across productive time;
- overlapping workers; and
- any sequence containing more than one named worker segment after scheduled
  break normalization.

A same-worker, same-work-center attendance gap contained within a configured
scheduled break is not a split. A worker change during lunch is still a real
split because the names or work centers differ.

The split runway keeps the approved independent goals, red/green results,
hatched shortfalls, completed finish lines, current goal line, worker names,
times, actuals, and goals. On a live dashboard, a station with completed worker
history but no current worker continues to say **No one here now** on the left.

### Views

Apply the conditional rule to all metered Recycling and New work centers in
horizontal, vertical, screen, and TV single-day views. Multi-day range views
remain unchanged and continue to use the aggregate legacy presentation.

## Split decision

Keep production credit independent from the display decision. The scorer still
produces all worker and unassigned credit so production history and station
totals remain unchanged.

Before building dashboard labels and finish lines, normalize the scored worker
segments for display:

- Merge touching or overlapping scores for the same named worker at the same
  work center.
- Merge consecutive scores for the same named worker at the same work center
  when the entire gap between them is contained within one of that day's
  configured scheduled breaks.
- Sum the actual units, productive minutes, and goal units of merged scores.
  Recalculate the combined runway and ahead/behind result from those sums.
- Keep the first start, final end, and final live/completed state.
- Do not merge different workers, different work centers, or gaps containing
  productive time.
- Keep unassigned production separate. Normalization changes presentation,
  not sample credit or station totals.

For each single-day work center, determine whether its named-worker segments
after normalization form exactly one continuous segment whose start is at or
before the viewed shift-window start and whose end is at or after the viewed
shift-window end.

- If yes, set `uses_split_format` to false.
- Otherwise, when named worker history exists, set `uses_split_format` to true.
- If no named worker history exists, keep the existing unassigned station
  presentation and assignment action.

The comparison uses timestamps and configured break windows, not formatted
labels or worker-name counts. This detects a same-worker leave-and-return
during productive time, ignores an administrative lunch split, and avoids
treating an unassigned meter reading as a staffing handoff.

## Components and data flow

### Department data preparation

The department route already knows the single-day scoring window, the day's
configured break windows, and the raw scored segment start/end timestamps. It
uses a pure production-segment helper to normalize display scores, calculates
the split decision per work center, and passes that decision alongside the
segment view data.

The visible segment dictionaries remain focused on rendering. No template
should infer staffing changes from display text.

### Bar model

`recycling_data.build_bars` keeps the full segment geometry available but adds
the explicit `uses_split_format` display flag to each row. Geometry continues
to support split rows without changing unit credit, goals, or scaling.

For a non-split row, scaling and the target line use the existing aggregate
station actual and expected values. This restores the original visual size and
goal line instead of retaining segment-runway scaling invisibly.

`no_one_here_now` remains true only for a live split row that has named worker
history and no current worker.

### Shared template

The shared department widget chooses segment markup only when
`uses_split_format` is true. Otherwise it renders the existing legacy
horizontal or vertical bar markup. The widget-level time axis remains visible
when every row in that widget uses the legacy format.

No CSS redesign is needed. Existing segment and legacy styles remain in place.

## Edge cases and fallbacks

- A full-window scheduled worker uses the original bar.
- A full-window attendance-backed worker uses the original bar.
- Morning and afternoon attendance records for the same worker and work center
  across scheduled lunch use one original bar and one goal line.
- The combined lunch-spanning goal still excludes lunch minutes.
- A worker transfer during lunch uses the split runway.
- Two simultaneous workers use the split runway.
- One worker with multiple segments separated by productive time uses the
  split runway.
- A worker who leaves a live station vacant uses the split runway and keeps
  **No one here now**.
- A worker who begins after the shift-window start uses the split runway even
  if nobody worked there before them.
- Unassigned-only production keeps the existing unassigned legacy bar and
  assignment action.
- Unassigned readings mixed into one otherwise continuous worker segment do
  not trigger the split runway.
- If segment scoring fails, the existing fail-soft aggregate bar remains.
- Multi-day views ignore segment detail as they do today.

## Validation

Add tests that prove:

- one continuous active worker uses the original horizontal bar and target
  line;
- one continuous completed-day worker uses the original format;
- same-worker, same-work-center attendance records separated by scheduled
  lunch combine into one display score, one legacy bar, and one goal line;
- the combined lunch-spanning goal equals the sum of the two break-adjusted
  goals and does not add lunch minutes;
- a different worker returning after lunch still creates a split;
- a transfer between two workers uses independent segment runways;
- Humberto leaving Repair 4 vacant still uses the segment runway and shows
  **No one here now**;
- a late-starting worker uses the segment runway;
- the same worker leaving and returning across productive time uses the
  segment runway;
- overlapping workers use the segment runway;
- unassigned readings alone do not trigger the segment runway;
- vertical and TV views follow the same conditional rule;
- split rows retain their worker details and finish markers; and
- range views remain unchanged.

## Out of scope

- Changing production credit or goal calculations.
- Changing the definition of current attendance.
- Changing production-history records.
- Adding a user setting to force one presentation.
- Redesigning segment colors, labels, or finish-line styling.
