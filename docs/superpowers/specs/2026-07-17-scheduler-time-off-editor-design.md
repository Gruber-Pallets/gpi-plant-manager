# Scheduler Time-Off Editor Design

## Goal

Let a supervisor correct a person's current scheduler time off without
leaving the Plant Scheduler. Clicking a name in the **Time Off** rail opens a
modal where the supervisor can change the leave's dates and, for a partial
leave, its off-time window, or cancel the leave. Every change must update the
local mirror immediately, sync to the same Odoo `hr.leave` record, and refresh
the current scheduler view so the person becomes available without waiting for
the periodic poller.

## Scope

This is a supervisor correction surface for scheduler-visible Odoo time-off
requests. The modal edits only the date range and time window. It does not
change the employee, leave type, note, or approval workflow.

The existing kiosk self-service edit/cancel routes remain unchanged.

## User Experience

1. Each row in the scheduler's Time Off rail is a button, not a player-card
   navigation link. The person's player-card remains available through a
   secondary link/icon if needed.
2. Selecting a row opens an accessible modal titled `Edit time off — <name>`.
   It displays the leave's current range. Full-day requests show date inputs;
   partial requests additionally show their existing start/end time inputs.
3. **Save changes** validates the range/window, stages the edit in the local
   `time_off_requests` mirror as `draft_edit`, and queues an Odoo write to the
   same `hr.leave` record.
4. **Cancel time off** asks for confirmation, then stages `draft_cancel` and
   queues Odoo's `action_refuse` for the same record.
5. After either local action succeeds, the browser reloads the current
   scheduler date. A cancelled full-day entry is absent from Time Off and the
   person is returned to Unscheduled or Reserves immediately. A changed range
   is reflected immediately.
6. A failed Odoo call leaves the change queued for the existing retry worker
   and returns a clear error/sync-pending response; it never silently restores
   stale availability in the browser.

## Backend Design

Create supervisor-scoped JSON endpoints under `/api/staffing/time-off`:

- `GET /{request_id}` returns the single scheduler-visible mirror record,
  scoped by the displayed schedule day.
- `POST /{request_id}/edit` accepts `date_from`, `date_to`, and optional
  `time_from`/`time_to`. It preserves the person, leave type, note, and Odoo
  leave id; it validates the values using the same time-window rules as the
  kiosk editor; then it transitions the local row to `draft_edit` and queues
  `time_off_sync.push_one`.
- `POST /{request_id}/cancel` transitions the local row to `draft_cancel` and
  queues `push_one`. A row that was never synced to Odoo is deleted locally.

Only requests represented by a real Odoo leave can be edited. Locally-derived
manager absences are not exposed in the editor; their existing dedicated
workflow remains authoritative.

The scheduler-facing query will expose the local request id and enough
date/time metadata to render the modal. It will continue to conceal sensitive
leave-type and note information from the scheduler rail.

## Sync Freshness Fix

An Odoo-side refusal/cancellation changes the `hr.leave` state and is already
included by incremental polling. The scheduler currently remains stale because
the poller's removal path only handles records absent from a full pull, which
runs every tenth one-minute tick. The poller will instead apply a received
`refuse`/`cancel` state during the next incremental tick, trigger the existing
reverse cascade, and make it invisible to scheduler reads. Full-pull deletion
detection remains as the fallback for hard-deleted Odoo records.

## Error Handling and Safety

- Invalid dates or partial time windows receive a 422 response and stay open
  in the modal with a human-readable error.
- The route verifies the record overlaps the selected scheduler day and is
  currently scheduler-visible; arbitrary request ids cannot be modified.
- The browser disables the action being submitted and prevents duplicate
  requests.
- Sync failures retain the staged local state and retry through the existing
  60-second sync worker. The response marks the outcome as pending rather than
  claiming Odoo has completed it.

## Testing

- Unit tests for the scheduler time-off query confirm request ids and edit
  metadata are returned without exposing leave type/notes in the rail data.
- Route tests cover full-day date edit, partial time edit, cancellation,
  invalid input, out-of-day/not-visible ids, local-only rows, and Odoo queue
  behavior.
- Poller tests prove an Odoo state transition from `validate` to `refuse` is
  removed from scheduler-visible data on the next incremental poll.
- Template/static tests verify Time Off names open the modal, keyboard/focus
  behavior, and current-day reload after a successful action.
