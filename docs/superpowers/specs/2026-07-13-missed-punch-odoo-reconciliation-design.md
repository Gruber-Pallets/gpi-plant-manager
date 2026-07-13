# Missed Punch-Out Odoo Reconciliation — Design

**Date:** 2026-07-13  
**Status:** Approved (ready for plan review)

## Context

The Missed Punch Out inbox keeps the Odoo attendance ID that Plant Manager
automatically closed at midnight. A manager can later correct the checkout
time. If the same attendance was edited or replaced in Odoo first, the saved ID
may no longer exist. Today, the correction request then exposes Odoo's raw XML-
RPC fault and leaves an already-resolved operational item open.

Odoo remains the source of truth. The inbox must reconcile the current Odoo
attendance before treating a deleted saved ID as a failure.

## Goal

When an inline missed-punch correction reaches a stale Odoo attendance ID:

1. Refresh the relevant employee's attendance records from Odoo for the
   original plant-local check-in day.
2. Dismiss the inbox item with a clear message when Odoo already has a closed
   replacement record whose checkout differs from Plant Manager's automatic
   midnight close.
3. Apply the entered correction to a single, safely identified open replacement
   record when one exists.
4. Keep the item open with a specific, human-actionable explanation when Odoo
   has no safe replacement record or its current state cannot be read.

## Decisions

- **Dismissal condition:** A record is already resolved only when Odoo reports
  a closed attendance for the flagged employee on the original local check-in
  day and its `check_out` is different from `auto_closed_at`. The original
  automatic midnight record is intentionally *not* dismissed: it remains a
  missed punch awaiting its real checkout time.
- **Replacement selection:** Reconciliation considers only attendance records
  for the same employee whose `check_in` falls on the original plant-local day.
  A single open record is a safe replacement and receives the entered
  correction. A closed non-midnight record settles the item. Multiple candidate
  records with no decisive closed non-midnight result are ambiguous and must not
  be changed automatically.
- **No silent dismissal on missing data:** No matching attendance, an ambiguous
  set, or an Odoo read failure leaves the alert open. The response tells the
  manager what to verify in Odoo and preserves the local flag.
- **Audit:** Every automatic settlement or rebound correction logs an inbox
  event. Automatic settlement records the Odoo checkout as the resolved value
  and states that Odoo already resolved the conflict.

## Design

### Odoo attendance lookup

Add an Odoo-client helper that reads all `hr.attendance` records for one
employee and one plant-local day. It queries with UTC day boundaries and
returns normalized records containing at least `id`, `check_in`, and
`check_out`. It is an explicit refresh and bypasses the local missed-punch
table/cache.

### Correction flow

`POST /missed-punch-out/correct` keeps its existing input validation and normal
write path. When its `clock_out(saved_attendance_id, corrected_time)` fails
because that Odoo attendance no longer exists, the route:

1. Reads the current Odoo attendances for the flagged employee and check-in
   day.
2. If it finds a closed record with a checkout other than `auto_closed_at`, it
   marks the local flag resolved using that Odoo checkout, logs the settlement,
   and returns `{ok: true, message: "Odoo already resolved this conflict."}`.
3. Otherwise, if it finds exactly one open record, it writes the entered time to
   that record, marks the local flag resolved, logs that the current Odoo record
   was updated, and returns success.
4. Otherwise, it returns a concise 409 response that leaves the local item
   unresolved. The message distinguishes no attendance, multiple current
   attendances, and unavailable Odoo state; it never exposes raw XML-RPC fault
   formatting.

The regular happy path retains its current write and audit behavior. A stale-ID
fault that is not a missing-record fault remains a normal failure; it is cleaned
into concise text but must not trigger reconciliation.

### User interface

Both the Exception Inbox row and global Missed Punch Out modal already remove a
row whenever the endpoint returns `ok: true`. They will display the returned
success message briefly, so the manager sees why an already-corrected Odoo row
disappeared. A 409 stays inline and identifies the exact verification needed.

## Tests

- Odoo client: lookup queries the intended employee/day window and normalizes
  `check_in`/`check_out` values.
- Route: missing saved record + non-midnight closed replacement resolves the
  flag, logs an automatic Odoo settlement, and returns the explanation.
- Route: missing saved record + exactly one open replacement corrects that
  replacement and resolves the flag.
- Route: original automatic-midnight closure is not automatically dismissed.
- Route: no candidate, ambiguous candidates, Odoo read failure, and unrelated
  Odoo write failures preserve the local flag and yield a friendly actionable
  response.
- Front-end: successful response messages are shown in both correction
  surfaces; errors stay visible and do not remove the row.

## Non-goals

- Changing Odoo attendance data before a manager submits a correction.
- Guessing across different employee IDs or check-in days.
- Auto-merging or deleting duplicate Odoo attendance records.
- Changing the normal automatic midnight-close worker.

## File touch list

- `src/zira_dashboard/_odoo_attendance.py` and
  `src/zira_dashboard/odoo_client.py` — current attendance-by-employee/day
  read.
- `src/zira_dashboard/routes/missed_punch_out.py` — stale-ID reconciliation,
  friendly outcomes, and audit.
- `src/zira_dashboard/static/exceptions.js` and
  `src/zira_dashboard/static/footer.js` — display returned success messages.
- `tests/test_odoo_attendance_for_day.py` and
  `tests/test_missed_punch_out_routes.py` — regression coverage.
