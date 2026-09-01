# Auto-Lunch Pre-Break Department Continuity

**Date:** 2026-09-01
**Status:** Approved design, pending implementation plan

## Problem

Auto-Lunch can create the post-lunch Odoo attendance without the department
from the attendance it closed before lunch. The first confirmed production
case was Lauro on September 1:

- the morning attendance ran from 6:40 AM to 11:00 AM plant time;
- its Odoo department was Supervisor;
- the local morning punch still identified the app work center as Tablets;
- Auto-Lunch stored no work center on its run; and
- the 11:30 AM attendance was created with neither a work center nor a
  department.

Because the afternoon department was blank, the attendance timeline treated
the row as requiring a work center and raised an urgent Odoo Location Missing
item. Supervisor attendances are department-exempt and should not have raised
that alert.

## Root cause

The attendance-mirror cutover changed Auto-Lunch's batched input path. In
mirror-owned mode, the worker copied `wc_name` from the open-attendance
snapshot into its pre-fetched work-center map. When the mirror had no mapped
Odoo work center, that map contained an explicit `None`.

Auto-Lunch then passed the explicit blank value into the lunch-out action.
That prevented its established fallback from reading the employee's latest
local clock-in or transfer-in punch. In Lauro's case, the fallback would have
found Tablets, whose configured department is Supervisor.

The existing design also derives a department from an app work center rather
than preserving the actual department on the pre-break Odoo attendance. That
is insufficient when another system moved the employee or when the Odoo
department and local app work center disagree.

## Confirmed behavior

- The exact department on the Odoo attendance immediately before lunch is
  authoritative for the post-lunch attendance.
- A pre-break Odoo department wins when it disagrees with the department
  derived from the local app work center.
- Auto-Lunch continues the employee's local app work center independently.
- If Odoo has no usable pre-break department, the current work-center-derived
  department remains the fallback.
- A missing department must not prevent the employee from being clocked back
  in after lunch.
- The selected department must survive application restarts and delayed Odoo
  synchronization.

## Considered approaches

### 1. Restore only the local work-center fallback

Treat a blank mirror work center as missing and read the latest local
clock-in or transfer-in punch. This would fix Lauro because Tablets resolves
to Supervisor. It would not preserve an Odoo department changed by another
system, so it does not implement the confirmed rule.

### 2. Patch the new attendance after clock-in

Persist the pre-break department on the Auto-Lunch run, create the afternoon
attendance through the existing pipeline, and then issue a separate Odoo
department write. This leaves a retry gap: if the initial clock-in sync fails,
the later generic retry can create the attendance without replaying the
department patch.

### 3. Carry the department through the durable punch pipeline — selected

Capture the exact pre-break Odoo department, persist it on the Auto-Lunch run,
copy it onto the afternoon punch, and let both immediate sync and retry use the
same explicit department override. This preserves the source value through
crashes and delayed synchronization without adding an Odoo call to each
Auto-Lunch action.

## Architecture

### Open-attendance source

The canonical open-attendance snapshot will expose the current attendance's
Odoo department ID and display name alongside its attendance ID, check-in,
and mapped app work center.

The mirror reader already stores both department fields. Its current-open
query and the source adapter will retain them. The legacy Odoo cache path will
retain the same fields so rollback mode follows the same contract.

### Auto-Lunch run state

`auto_lunch_runs` will gain nullable `odoo_department_id` and
`odoo_department_name` columns. At lunch-out, Auto-Lunch will save the
department from the exact open attendance it is closing in the same
transaction as the lunch-out punch and run-state transition.

The run will continue to store `wc_name`. A blank mirror work center will no
longer suppress the local latest-punch fallback. The worker will use the
mirror's mapped work center when present and otherwise use the latest local
clock-in or transfer-in work center.

### Punch and synchronization contract

`timeclock_punches_log` will gain a nullable desired Odoo department ID for
attendance-opening punches. The Auto-Lunch return punch copies the saved
pre-break department ID into this field.

The shared synchronization reader will pass the optional department ID to
both immediate sync and retry. The Odoo attendance-create payload will use an
explicit department ID when supplied. When it is absent, the existing
work-center-to-department resolver remains unchanged.

The override applies only to the attendance opened by that punch. Ordinary
kiosk punches continue deriving their department from their chosen work
center unless a future caller deliberately supplies an explicit ID.

## Data flow

```text
pre-break Odoo attendance
  department ID/name --------+
  mapped app work center -----|----> auto_lunch_runs
                              |        department + work center
latest local in/transfer punch+              |
                                             v
                                  post-lunch local punch
                                    work center + department ID
                                             |
                              +--------------+--------------+
                              |                             |
                       immediate Odoo sync            retry worker
                              |                             |
                              +--------------+--------------+
                                             v
                                  new Odoo attendance
                              exact pre-break department
```

## Failure and safety behavior

- If the open-attendance source is unavailable or stale, Auto-Lunch keeps its
  existing behavior and performs no automatic action.
- If the source has no valid department ID, the return punch omits the
  override and the shared Odoo writer derives a department from the restored
  app work center.
- If neither source supplies a department, the return still clocks the
  employee in and the existing Exception Inbox behavior remains available.
- The implementation will not infer a department from an Odoo display name or
  employee home department when the pre-break attendance has no department.
- Existing run idempotency, lunch cancellation, stale-source guards, and
  one-person failure isolation remain unchanged.
- A retry adopts an already-open Odoo attendance only under the current
  duplicate-prevention rule. If the punch carries an explicit department, the
  adopted attendance must receive that department just as it already receives
  a carried work center.

## Production repair

After the implementation is pushed and validated, re-read Lauro's September 1
pre- and post-lunch Odoo attendances. If the post-lunch attendance still has no
department and the pre-break attendance still proves Supervisor, update only
the post-lunch department to the same Supervisor ID. Read the row back and
verify it. Do not change its employee, times, work center, or unrelated fields.

## Test design

Add focused tests that first fail against current behavior:

- mirror-owned Auto-Lunch falls back to the latest local app work center when
  the mirrored open attendance has no mapped work center;
- lunch-out saves the exact pre-break Odoo department on the durable run;
- the return punch carries that department through the immediate sync path;
- the periodic retry uses the same department after an initial sync failure;
- the pre-break Odoo department wins over a different work-center-derived
  department;
- an absent pre-break department retains the current derived fallback;
- an adopted already-open attendance receives the explicit department; and
- legacy and mirror-owned open-attendance snapshots expose the same department
  contract.

Run the focused Auto-Lunch, attendance source, synchronization, Odoo attendance
writer, attendance timeline, department-policy, and Exception Inbox suites.
Then run the full project tests and lint checks required by the repository.

## Out of scope

- Changing Auto-Lunch times, eligibility, or one-lunch-per-day behavior.
- Guessing or auto-creating Odoo departments.
- Mapping Tablets to an Odoo Manufacturing Work Center.
- Bulk rewriting historical attendance records.
- Changing the separate Auto-Salaried lunch workflow.
