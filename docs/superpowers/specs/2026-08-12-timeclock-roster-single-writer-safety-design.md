# Timeclock Roster Single-Writer Safety Design

**Status:** Implemented

## Problem

The timeclock has rendered an empty employee list four times within 24 hours.
The page itself was healthy, but its query returned no rows because every
record in the local `people` table had `active = FALSE`.

The August 12 production investigation established two unsafe write paths:

1. `staffing.save_roster()` rewrites `people.active` for every cached roster
   entry while saving unrelated local fields such as `reserve`. A stale cached
   all-inactive roster can therefore overwrite a newer successful Odoo sync.
2. `odoo_sync.sync()` infers that any employee missing from a non-empty
   active-only Odoo response was archived. A truncated response can therefore
   deactivate valid employees. An empty response skips deactivation but is
   still recorded as a successful sync and clears the manager warning.

Production evidence distinguished the current incident from the earlier
suspected malformed-payload failures:

- Odoo returned 36 valid records, all explicitly active.
- The local database contained 44 records and zero active records.
- All 44 local records had `local_dirty = TRUE`, which is stamped by the local
  roster-save path.
- Their `last_pulled_at` values were unchanged after the mass deactivation,
  proving the status rewrite did not come from the Odoo upsert.

At 12:58 UTC, an approved forced Odoo sync restored the 36 active employees.
This operational recovery does not remove either underlying failure path.

## Goals

- Make Odoo sync the sole owner of employment status for Odoo-backed people.
- Prevent stale local caches and unrelated form saves from changing that
  status.
- Never interpret absence from an upstream response as proof of archival.
- Reject empty or malformed Odoo roster snapshots without advancing the
  last-success marker or clearing the alert.
- Keep the kiosk visibly diagnosable if its roster is ever empty again.
- Preserve the existing Reserve, skills, roster-filter, and new-local-person
  behavior.

## Non-goals

- Redesigning the People Matrix.
- Changing employee names, wage types, skills, schedules, or attendance
  ownership.
- Adding a manager workflow for approving bulk Odoo staffing changes.
- Deleting historical or test-contaminated `people` rows as part of this fix.

## Decision

Use a single-writer ownership boundary plus explicit Odoo status reads.

### Local roster writes

`staffing.save_roster()` may insert a new local person as active, but an
upsert of an existing person will not update `active`. It will update only the
locally owned fields that the caller is actually allowed to change, including
`reserve`, `odoo_id` attachment when appropriate, and local skill rows.

This protects both Odoo-backed people and existing local-only people from a
stale whole-roster replay. The already-read-only People Matrix and object API
remain read-only for employment status.

### Odoo roster reads

The Odoo client will expose a roster read that includes both active and
inactive employee records by disabling Odoo's default active-only context.
Every returned record must have a positive unique ID, a nonblank name, and a
Boolean `active` value.

The sync will upsert status only for records explicitly present in that
validated response. It will remove the current set-difference update that
deactivates every locally known ID absent from the response. If Odoo omits a
record, the last known local status remains unchanged until a later response
explicitly includes it.

This trades delayed archival during an incomplete response for safety: a
temporarily missing employee may remain visible, but a partial response cannot
erase the workforce.

### Snapshot health and alert lifecycle

An empty response, malformed record, or duplicate employee ID rejects the
entire roster sync before any employee or skill write. A rejection:

- returns `ok = FALSE` and `refreshed = FALSE`;
- leaves the last known-good people and skills untouched;
- does not update `odoo_last_sync`;
- persists the existing urgent Timeclock Roster alert with a safe reason and
  detected time; and
- does not clear an earlier alert.

Only a non-empty, fully validated, transactionally committed roster refresh
may advance `odoo_last_sync` and clear the alert. Cache-only TTL hits do
neither.

### Kiosk empty-state defense

The `/timeclock` route will continue reading active, non-excluded people from
Postgres. If that query returns zero rows, the template will show a prominent
bilingual message instead of a blank grid:

> The employee list is unavailable. Please tell a manager.
>
> La lista de empleados no está disponible. Avísale a un gerente.

The server will also emit a high-severity log containing the roster count and
last sync/alert state. The kiosk will not guess at names or bypass the active
status rule.

## Data Flow

```text
Odoo full employee roster (active + inactive)
  -> validate non-empty, IDs, names, and Boolean status
      -> invalid: preserve local roster + retain alert + report failure
      -> valid: one transaction
          -> upsert each explicitly returned employee and status
          -> refresh synced skills
          -> commit
          -> advance last-success time
          -> clear prior alert

People Matrix / local roster save
  -> save Reserve and locally owned skill values
  -> never update employment status on an existing row

Timeclock home
  -> query active, non-excluded local people
      -> one or more: render employee buttons
      -> zero: render bilingual manager-facing failure message + critical log
```

## Alternatives Considered

### Percentage-drop guard

Reject any snapshot that falls below a percentage of the previous roster.
This is smaller, but every threshold is a guess: a truncated response can land
above it, while a legitimate staffing change can land below it. It also leaves
multiple local writers able to overwrite `active`.

### Database trigger

Use a trigger to reject `active` changes except from the sync. The application
currently uses one database role, so identifying an authorized caller would
require session flags or another credential. That adds operational complexity
and still would not correct the sync's unsafe absence inference.

### Selected approach

The single-writer boundary addresses the proven stale-cache replay directly,
while explicit status reads remove the destructive absence inference without
introducing an arbitrary threshold.

## Testing

Test-driven implementation will add regressions proving that:

- saving Reserve from a stale cached roster cannot change any existing
  person's `active` value;
- adding a new local person still inserts that person as active;
- an explicit inactive Odoo record deactivates that one person;
- a person missing from a non-empty Odoo response keeps the last known status;
- an empty, malformed, or duplicate-ID response performs no roster/skill
  writes, does not advance the success timestamp, and retains an alert;
- a valid refresh advances the timestamp and clears the alert only after the
  transaction commits;
- the timeclock renders employee buttons for a healthy roster; and
- a zero-person roster renders the bilingual failure state rather than a
  silent blank page.

Focused roster, Odoo-client, object-API, timeclock-route, and inbox tests will
run first. The full test suite, Ruff, and a production build/import check will
run before the implementation is pushed.

## Rollout and Verification

After the implementation reaches `main` and Railway reports a healthy deploy:

1. Read production counts independently and confirm 36 active, non-excluded
   kiosk people unless Odoo has legitimately changed meanwhile.
2. Load the authenticated `/timeclock` page and confirm employee buttons are
   present.
3. Run a normal Odoo refresh and confirm the active count remains stable.
4. Exercise a Reserve-only save and confirm the active count and statuses are
   unchanged.
5. Confirm there is no open Timeclock Roster alert after the verified healthy
   sync.

Any mismatch stops rollout verification and remains an active incident; the
task is not complete merely because code was deployed.
