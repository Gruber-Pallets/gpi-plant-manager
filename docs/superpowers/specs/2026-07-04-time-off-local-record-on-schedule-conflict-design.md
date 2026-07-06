# Record absences locally when Odoo rejects them for a Working Schedule conflict

**Date:** 2026-07-04
**Status:** Approved (autonomous session — decision documented here in lieu of interactive review)

## Problem

Approving a pending time-off request (Exception Inbox or `/staffing/time-off/approvals`)
calls `odoo_client.approve_leave`. When the employee's Odoo Working Schedule
(`resource.calendar`) has no attendance on the requested day(s), Odoo raises
`ValidationError: The following employees are not supposed to work during that period: …`
and the app hard-fails with a friendly 500 ("Ask HR to fix their Working Schedule").
Nothing is recorded; the card stays pending forever.

This bites whenever an absence falls on a day the standard schedule doesn't include
(e.g. Gerardo Garcia, Absence on Fri 2026-07-03). Dale's requirement: **the absence
must still get recorded**, even when Odoo won't validate it.

## Decision

On that specific rejection, approve the request **locally**: settle the Odoo copy by
refusing it (with an explanatory chatter note), and keep the local mirror row as the
authoritative approved record, protected from the sync engine by a new
`local_record` flag.

## Approaches considered

1. **Force it into Odoo** (write `number_of_days`, temporary calendar swaps, hour-based
   rewrites). Rejected: Odoo recomputes duration from the calendar, so the validation
   re-fires; calendar swaps mutate payroll-relevant HR data and are racy.
2. **Record in `manual_absences`** (the declare-absent precedent, which already treats
   this exact Odoo error as best-effort). Rejected: `manual_absences` is single-day,
   full-day only, and invisible to Who's Out, coverage/"off peak" counts, the day-before
   reminder, and the kiosk My Requests list — the employee would see their request as
   "Rejected".
3. **Detach the row** (`odoo_leave_id = NULL`, `state='validate'`) and let the poller
   re-mirror Odoo's refused copy as a separate row. Rejected: the kiosk My Requests list
   shows all rows for a person, so the employee would see both "Approved" and "Rejected"
   entries for the same day; also leaves insert-branch race windows.
4. **`local_record` flag** (chosen): one row, one leave id, all display surfaces keep
   working, and the sync engine gets explicit, testable guards.

## Design

### Schema

`time_off_requests.local_record BOOLEAN NOT NULL DEFAULT FALSE`, added both to the
`CREATE TABLE` and as `ALTER TABLE … ADD COLUMN IF NOT EXISTS` (existing pattern —
`bootstrap_schema` does not reconcile columns). Meaning: *this row's state is owned
locally; the Odoo poller must neither overwrite nor delete it.*

### Approve-path fallback (`routes/exceptions.py`)

`_approve_time_off_sync` catches the `approve_leave` exception as today. New behavior
when the fault text contains `not supposed to work during that period` (shared constant
with `_friendly_odoo_error`):

1. **Pre-insert a suppression notification** — a pre-acknowledged
   `(time_off_request_id, 'time_off_denied')` row in `employee_notifications`
   (new helper; `ON CONFLICT DO UPDATE` acking any pre-existing row, so even a
   stale unacknowledged "denied" popup is neutralized). The unique index makes any
   later poller-generated "denied" popup for this request a silent no-op. This
   closes the race where a poll tick lands between the Odoo refuse and our local
   write. If the fallback aborts (refuse fails), the suppression row is deleted
   again (`unsuppress_resolution`, acknowledged rows only) so a later genuine
   Odoo-side denial can still notify.
2. **Refuse the leave in Odoo** (`refuse_leave`). Odoo permits refusing from
   `confirm`/`validate1` regardless of calendars. If the refuse itself fails, abort the
   fallback and return the friendly 500 exactly as today (nothing recorded; retry later).
   The still-pending Odoo copy would otherwise resurrect pending cards via the poller.
3. **Record locally**: `UPDATE time_off_requests SET state='validate',
   local_record=TRUE, synced_to_odoo=TRUE, sync_error=NULL, last_pushed_at,
   updated_at` and fire `time_off_sync.cascade_on_state_change` + cache busts
   (sibling of `_set_time_off_state`). `sync_error` stays NULL — the kiosk detail
   page renders it as a red error box, so the *why* lives in the decision audit,
   the inbox log, and the Odoo chatter note instead.
4. **Best-effort chatter note** on the refused Odoo leave explaining that the absence
   was approved and recorded in Plant Manager and the Odoo copy was closed because the
   Working Schedule does not include the day(s). Failure is logged, not surfaced.
5. **Audit + response**: `time_off_audit.record_decision` (action `approve`, result
   `validate`, reason "Recorded in Plant Manager only — Odoo Working Schedule does not
   include the requested day(s)"), `inbox_log` event (outcome "Approved (recorded
   locally)", not reversible), response
   `{ok: true, state: 'validate', approved: true, recorded_locally: true, warning, decision}`.

Any other Odoo error keeps today's friendly-500 contract. Approve stays idempotent:
a re-click hits the existing `state == 'validate'` no-op guard.

### Sync-engine guards (`time_off_sync.py`)

- `_existing_rows_by_leave_id` also selects `local_record`.
- `_upsert_one`: early-return when the existing row has `local_record` (no compare, no
  write, no cascade, no notification). Defense in depth for the stale pre-fetched map:
  the UPDATE gains `AND NOT local_record` in its WHERE clause and runs via
  `RETURNING id`; cascade + notification fire only when the write actually landed.
- `_delete_missing_from_odoo`: the candidate SELECT excludes `local_record` rows —
  if HR later deletes the refused Odoo leave, the local record survives.
- `_push_cancel`: when the row is `local_record`, skip the Odoo RPC (the Odoo copy is
  already refused; `action_refuse` from `refuse` raises) and just settle the row
  locally. This keeps the employee-initiated kiosk cancel of a locally-recorded
  absence from wedging the retry sweep.
- `_push_edit`: defense in depth — if a `draft_edit` ever lands on a `local_record`
  row, skip the Odoo write and settle the row back to `state='validate'` instead of
  writing to the refused leave (which would strand the row or drag it to `confirm`).

### Manager deny & kiosk edit of a local record

- `_refuse_time_off_sync` skips `refuse_leave` for `local_record` rows (the Odoo
  copy is already refused — the RPC would raise forever) and settles locally; the
  deny reason still posts to the Odoo chatter. `_set_time_off_state` now also
  clears `local_record`, handing ownership back to the poller once local and Odoo
  states agree again. This keeps deny as the manager's undo for a fallback approval.
- The kiosk **Edit** flow is closed for `local_record` rows: the detail template
  hides the Edit button, and both edit routes bounce even a hand-crafted request
  (an edit would write to the refused Odoo leave and corrupt the record). Kiosk
  **Cancel** stays available and settles locally via the `_push_cancel` guard.

### Employee-facing surfaces

- Kiosk My Requests: the single row shows **Approved** (bucket for `validate`).
- Who's Out, scheduler, coverage/"off peak", day-before clock-out reminder, balances,
  duplicate-request blocking: all read `state='validate'` rows from the mirror and thus
  see the local record with zero changes.
- No "approved" kiosk popup is generated — identical to every other inbox-made
  approval today (popups only fire for Odoo-side state changes observed by the poller).
- The pre-acknowledged suppression row means the employee can never receive a wrongful
  "denied" popup for this request.

### Manager-facing UI

Both approve surfaces label the resolved row using `resp.recorded_locally`:
"Approved — recorded here (Odoo schedule conflict)" instead of plain "Approved"
(`static/exceptions.js`, `static/time_off_approvals.js`; the approvals page still
prepends the decision row, whose reason spells out the local-only recording).

### Backfill into Odoo (added 2026-07-04, same day)

Dale wants these absences visible in Odoo. Root-cause diagnosis showed the real
trigger for the first live case was not a Working Schedule gap but Odoo's global
"4th of July" holiday record covering 2026-07-03 while the plant actually ran
(17 people punched in). New module `time_off_local_backfill` (hourly warmer),
hardened by a second adversarial review (13 confirmed findings):
for each approved `local_record` row it predicts whether Odoo would accept the
leave now — ≥1 day in the span is a covered weekday not blocked by a holiday
record *that applies to this employee* (company-wide, or scoped to their
calendar via `calendar_id`; bounds converted UTC→plant time so day-after-holiday
absences aren't frozen) — and only then replays it: `action_draft` on the
refused/cancelled copy, then confirm + approve. Adoption is a guarded UPDATE
(`WHERE local_record AND state='validate' RETURNING id`): a kiosk cancel or
manager deny that settled the row mid-replay wins, and the replayed Odoo
approval is rolled back to refused. On success the denied-popup suppression is
retired and a pre-acked approved-suppression is armed (a poll tick that
photographed a stale mid-replay state can't fire a spurious popup). Bounded by
design: prediction skips defer the row (`backfill_next_at`, rotating it out of
the LIMIT window so dead rows can't starve replayable ones); real failures
re-refuse the Odoo copy and back off exponentially (`backfill_attempts`, capped
weekly). There is deliberately NO recreate path — if HR deleted the refused
copy, the absence stays app-only. Zero RPCs on ticks with no local records.
Making Odoo *able* to accept a given day (scoping/removing a holiday record,
fixing a calendar) stays a human data decision — the backfill reacts to it.

## Known limitations (accepted)

- Odoo shows the leave as *Refused* (with an explanatory chatter note) **until
  the underlying Odoo data allows the backfill to replay it**. While refused,
  Odoo-side reports/allocations do not count the absence; the app is
  authoritative for it. The weekly calendar-conflict monitor keeps nudging HR
  to fix Working Schedules, which remains the long-term fix.
- The Odoo error-string match is locale-dependent (existing `_friendly_odoo_error`
  fragility, unchanged).
- If the process dies between the Odoo refuse and the local write, the poller mirrors
  the refuse: the request shows as denied and nothing was recorded (the wrongful
  "denied" popup is already suppressed). The manager can still record the day via
  declare-absent. The window is milliseconds wide.
- A `local_record` row whose Odoo leave is later deleted keeps a dangling
  `odoo_leave_id`; Odoo ids are never reused, so this is inert.

## Testing

- Schema string tests for the new column (CREATE + ALTER).
- `routes/exceptions`: rejection → recorded-locally happy path (refuse called,
  suppression inserted, local UPDATE with flag, cascade, audit reason, inbox event,
  response shape); refuse-failure → friendly 500 abort; non-schedule fault → friendly
  500 unchanged; chatter failure tolerated.
- `time_off_sync`: `_upsert_one` skips flagged rows entirely; RETURNING-gate does not
  cascade/notify when the guarded UPDATE writes nothing; delete pass skips flagged
  rows; `_push_cancel` skips the RPC for flagged rows.
- `employee_notifications`: suppression helper inserts pre-acknowledged row; dedupe
  holds.
- JS static-text assertions for the new label where the existing convention does so.
