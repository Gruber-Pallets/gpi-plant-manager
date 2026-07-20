# Clear a worker's schedule assignment when declared absent

**Date:** 2026-07-20  
**Status:** Approved

## Problem

The late-report **Mark absent** action records a full-day absence, and the
Staffing page visually filters that person from the work-center rows. Their
name nevertheless remains in the day's persisted `schedule_assignments` rows.
This allows the person to remain selected in the schedule picker and means the
assignment can reappear once the visual absence filter is removed.

## Decision

Declaring an employee absent removes that employee from every work center in
the saved schedule for the plant day. It also removes their assignment-source
metadata, because an assignment that no longer exists must not become a
protected automatic-scheduler lock.

Undoing the absence only removes the absence record. It deliberately does not
restore a prior schedule assignment: managers must make a conscious new
assignment if the employee is available after all.

## Design

Add a focused staffing-domain helper that accepts a date and roster name. In a
single schedule persistence operation it will:

1. Load the day's current schedule.
2. Remove the name from every assignment list, preserving the order of all
   other people.
3. Remove the same name from `assignment_sources` for each work center,
   dropping empty source maps.
4. Save the otherwise unchanged schedule through the existing schedule store,
   which invalidates the day-level schedule cache.

The `declare-absent` route will call this helper only after its local absence
record is written successfully. Cache invalidation remains at the route level,
so the next Staffing render reflects both the absence and the persisted
schedule change. An already-unscheduled employee is a harmless no-op.

The absence action remains successful if the Odoo mirror is unavailable; the
schedule clear is local and applies regardless of Odoo-sync outcome.

## Alternatives considered

1. Continue to hide absent workers only at render time. Rejected because the
   stale saved assignment remains selectable and can silently return.
2. Add a new exclusion layer to every scheduler read/write path. Rejected:
   that duplicates existing absence state and leaves stale assignments in the
   durable source of truth.
3. Remove persisted assignments at absence declaration. Chosen because it
   directly matches manager intent and keeps the saved schedule, picker, and
   automatic scheduler consistent.

## Testing

- A regression test starts with a schedule containing the absent employee in
  one or more work centers and assignment-source entries.
- Declaring the absence must remove only that employee from the persisted
  assignments and source maps, while preserving the rest of the schedule.
- A second declaration for an already-unscheduled employee must remain safe.
- Existing Odoo-sync failure behavior remains covered: local absence and the
  schedule clear still occur.

