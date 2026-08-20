# Conditional Worker Segment Display Design

## Goal

Keep the original department production bar for a work center whose worker
coverage did not change during the viewed part of the shift. Show the newer
per-worker segment runway only when worker coverage was actually split.

This keeps ordinary work centers familiar and compact while preserving the
transfer history that explains who produced the pallets when staffing changes.

## Approved behavior

### Original bar

Use the original bar when exactly one named worker continuously covers the
entire viewed shift window for that work center.

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
- the same worker leaving and later returning;
- overlapping workers; and
- any sequence containing more than one named worker segment.

The split runway keeps the approved independent goals, red/green results,
hatched shortfalls, completed finish lines, current goal line, worker names,
times, actuals, and goals. On a live dashboard, a station with completed worker
history but no current worker continues to say **No one here now** on the left.

### Views

Apply the conditional rule to all metered Recycling and New work centers in
horizontal, vertical, screen, and TV single-day views. Multi-day range views
remain unchanged and continue to use the aggregate legacy presentation.

## Split decision

Keep segment scoring independent from the display decision. The scorer still
produces all worker and unassigned credit so production history and station
totals remain unchanged.

For each single-day work center, determine whether its named-worker segments
form exactly one continuous segment whose start is at or before the viewed
shift-window start and whose end is at or after the viewed shift-window end.

- If yes, set `uses_split_format` to false.
- Otherwise, when named worker history exists, set `uses_split_format` to true.
- If no named worker history exists, keep the existing unassigned station
  presentation and assignment action.

The comparison uses timestamps, not formatted labels or worker-name counts.
This correctly detects a same-worker leave-and-return and avoids treating an
unassigned meter reading as a staffing handoff.

## Components and data flow

### Department data preparation

The department route already knows the single-day scoring window and the raw
scored segment start/end timestamps. It calculates the split decision per work
center and passes that decision alongside the segment view data.

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
- Two simultaneous workers use the split runway.
- One worker with multiple separated segments uses the split runway.
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
- a transfer between two workers uses independent segment runways;
- Humberto leaving Repair 4 vacant still uses the segment runway and shows
  **No one here now**;
- a late-starting worker uses the segment runway;
- the same worker leaving and returning uses the segment runway;
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
