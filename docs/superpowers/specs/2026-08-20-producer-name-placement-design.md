# Producer Name Placement Design

## Goal

Keep a worker's name in the normal left-side label whenever that worker is the
only named person with production credit at the work center for the viewed
day. Put worker names inside the production bar only when two or more distinct
named people have production credit at that work center.

This rule applies independently from whether a worker is currently active,
finished early, started late, or left the work center vacant.

## Approved behavior

### No named producers

Keep the existing unassigned work-center presentation. Scheduled assignments
and the existing assignment action continue to control the left-side label.

### One named producer

Show the producer's name in the normal left-side primary label and the work
center name beneath it. Do this whether the producer is active or has stopped.

The scored production presentation remains unchanged:

- an active worker keeps the live goal marker;
- a stopped worker keeps the visible stop point, completed finish line, time,
  actual units, goal units, and red/green result;
- a late start, early stop, or productive-time gap may still use scored
  segment geometry when needed; and
- the producer's name is not repeated inside the bar.

Unassigned units do not count as another producer. For example, Jose O. stays
in the normal left-side position on Repair 1 even when the meter includes a
small amount of unassigned production.

### Multiple named producers

When two or more distinct named people have production credit at a work
center, keep the approved multi-worker layout. Each producer's name appears
inside that producer's scored bar segment with the producer's time, actual
units, goal units, finish marker, and result.

The left-side label continues to identify the work center or its current
vacancy state instead of choosing one producer to represent the whole row.

## Decision rule

Name placement uses the count of distinct non-empty `person_name` values in
the work center's normalized display scores:

- zero names: use the existing unassigned presentation;
- one distinct name: expose that name as the sole producer and place it on the
  left; and
- two or more distinct names: place producer names inside their scored bar
  segments.

Count unique people, not score records. If the same worker has two scored
segments, that is still one producer for name placement. The segments and
their finish markers remain separate when the existing scoring policy keeps
them separate.

The rule must not use the current assignment alone. A worker who stopped still
owns the credited production and therefore remains the sole producer shown on
the left.

The rule also must not treat unassigned production as a person. Unassigned
units remain included in station totals and existing history but do not move a
sole named producer into the multi-worker name layout.

## Architecture and data flow

### Department data preparation

The department route already has normalized scored segments before it builds
the visible segment dictionaries. At this boundary, derive the ordered set of
distinct named producers per work center and carry the sole-producer identity
with the existing single-day segment display data.

Production credit, goal calculation, lunch normalization, worker-coverage
split detection, and red/green scoring remain unchanged.

### Bar model

`recycling_data.build_bars` receives the producer identity alongside the
existing segment data. Each bar row exposes explicit presentation fields for:

- the sole producer name, when exactly one exists; and
- whether worker names belong inside segments, when two or more exist; and
- the current worker or occupied/vacant state, independently from historical
  producer name placement.

The bar model uses the sole producer for the left-side label even when the
worker is no longer active. It must not overwrite the current-worker value
used to decide whether a multi-producer station is occupied or vacant. It does
not disable scored segment geometry or remove completed finish markers.

Keeping these fields explicit prevents the shared template from reimplementing
producer-count business logic.

### Shared template

Horizontal and vertical layouts use the same presentation fields:

- a sole producer renders through the existing normal operator-name macro on
  the left;
- segment labels omit the producer name when there is only one producer while
  retaining time, actual/goal, and result details; and
- multi-producer segments continue to render every producer name inside the
  bar.

For one producer, the sole producer label takes precedence over the existing
**No one here now** primary label. The completed stop marker supplies the
requested visual evidence that the worker has stopped. For multiple producers,
the current occupied/vacant state continues to control whether the left side
shows the work center or **No one here now**.

Screen and TV views inherit the same rule from the shared template. Multi-day
range views remain unchanged.

## Edge cases

- One active named producer appears on the left.
- One stopped named producer appears on the left and keeps the stop and finish
  markers.
- One named producer plus unassigned units still appears on the left.
- One named producer with multiple scored records still counts as one distinct
  producer.
- The same named producer split only by scheduled lunch remains one producer;
  lunch continues to be excluded from the goal.
- Two different producers across lunch use the multi-producer layout.
- Two overlapping named producers use the multi-producer layout.
- An active multi-producer station identifies the work center on the left and
  does not duplicate the current worker there.
- A vacant multi-producer station keeps **No one here now** on the left.
- Unassigned-only production keeps the existing unassigned presentation.
- A scoring failure keeps the existing fail-soft aggregate bar.

## Validation

Add tests that prove:

- one active producer is the left-side worker in horizontal and vertical
  layouts;
- one stopped producer is the left-side worker while the completed stop and
  finish marker remains visible;
- one producer plus unassigned units still uses the left-side name;
- the same producer in multiple scored segments is not treated as multiple
  people;
- two distinct producers keep both names inside their own bar segments;
- active and vacant multi-producer stations retain correct current-status
  labels without duplicating a producer on the left;
- a sole producer's name is not duplicated inside the bar;
- screen and TV rendering follow the same rule; and
- multi-day ranges remain unchanged.

## Out of scope

- Changing production credit or sample attribution.
- Changing worker goals, lunch subtraction, or ahead/behind scoring.
- Removing independent stop or finish markers.
- Redesigning bar colors, scaling, or work-center ordering.
