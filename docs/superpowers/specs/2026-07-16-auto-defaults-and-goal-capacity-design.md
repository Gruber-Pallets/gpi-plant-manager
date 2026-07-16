# Auto Defaults and Schedule-Goal Capacity Design

## Purpose

Keep exact work-center defaults authoritative during automatic scheduling and
show, beside the Schedule Goal controls, whether the enabled Auto work-center
count is sufficient for the people still awaiting a station.

## Default-assignment behavior

- An active, available person with an exact default at an enabled Auto work
  center must be assigned to that default when Auto rebuilds.
- A prior generated assignment must never displace that exact default.
- Manual assignments remain protected: a person manually assigned to another
  station is not moved automatically.
- A generated assignment that conflicts with an exact default is regenerated
  rather than reported as an unsafe duplicate. The misleading removal warning
  must not appear for this case.
- Existing qualification, absence, capacity, and enabled-center protections
  still apply.

## Schedule-goal indicator

Display a compact indicator in the unused right side of the Schedule Goal
control:

`Auto mode  +2   12 unscheduled / 14 Auto On`

- **Unscheduled** is the current number of active, non-reserve people without
  a station and not on full-day time off.
- **Auto On** is the count of enabled Auto work centers, not the sum of their
  operator capacities.
- The signed value is `Auto On - unscheduled`: positive when more centers are
  enabled than unscheduled people, negative when more people need a station,
  and zero when the counts match.
- The indicator is initially server-rendered and updates immediately when an
  Auto checkbox changes and after a schedule rebuild changes assignments.
- It is informational only; it does not block a rebuild or declare the
  schedule safe, because qualifications, minimum staffing, and multi-person
  stations still govern feasibility.

## Verification

- Regression-test the generated-assignment/default conflict: valid exact
  defaults win and no unsafe-removal warning is produced.
- Test the server-rendered counter inputs and the browser-side count update
  after toggling an Auto work center and after applying rebuild results.
- Run the focused staffing-rotation tests and the relevant UI contract tests.
