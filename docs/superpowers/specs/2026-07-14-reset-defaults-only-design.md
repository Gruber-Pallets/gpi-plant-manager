# Reset to Defaults-Only Design

## Goal

Make **Reset to defaults** replace the entire draft schedule with configured
defaults only. It must not run the automatic scheduler, retain previous
assignments, or emit automatic-scheduling placement failures.

## Behavior

Reset replaces every work-center assignment, including manual assignments.
It preserves schedule metadata: published state and snapshot, notes,
work-center notes, testing-day state, custom hours, Auto/Training settings,
time off, and the selected schedule goal.

For each configured exact default, the reset assigns that person to the named
work center when they are on the day's roster. For each configured group
default, it assigns that person to the next eligible work center in that group
according to the existing per-person center-rotation history. Group assignment
uses the existing deterministic `choose_center` rule, which favors the least
used center and avoids the most recently used center on a tie.

People who are absent for the full day are not assigned. A person configured
in more than one default target is assigned once using the existing stable
target order; the reset remains best-effort rather than retaining the old
schedule. Non-defaulted people remain unassigned. No capacity, minimum crew,
qualification, training, or automatic-scheduler completion validation blocks
the reset.

Assignments created by the reset are marked as defaults (rather than
`generated`) so subsequent automatic rebuilds can treat them as saved default
placements. Assignments outside the configured default set are removed.

## Components and Data Flow

`POST /api/rotations/rebuild` retains the existing `reset_to_defaults` request
flag. When true, it takes a dedicated default-only path:

1. Load the day, roster, time off, configured exact defaults, group defaults,
   group membership, and bounded rotation history.
2. Build a clean assignment map from exact defaults and next group centers.
3. Replace the saved schedule's assignment map and assignment-source map while
   preserving all non-assignment schedule fields.
4. Return the normal assignment response shape with no solver coverage or
   placement errors.

The normal schedule-goal buttons continue to call the automatic scheduler
unchanged. The browser keeps calling the same endpoint, but the Reset button's
confirmation text describes replacement with defaults and group rotation,
rather than automatic rebuilding or retained schedules.

## Error Handling and Tests

The reset endpoint returns a service error only when it cannot read the
required schedule/default data or save the replacement. It never declines a
reset merely because defaults cannot form a safe complete automatic schedule.

Tests will prove that reset:

- removes manual and generated assignments that are not defaults;
- applies exact defaults and excludes full-day absences;
- chooses the next center for group defaults using rotation history;
- preserves schedule metadata and records default sources; and
- bypasses automatic-solver completeness failures while the ordinary rebuild
  path remains unchanged.

The static client test will verify the reset request still uses the dedicated
flag and that its confirmation describes the new behavior.
