# Maintenance Attendance Continuity Design

## Goal

Keep hourly attendance accurate between GPI Maintenance work orders without
inventing a production work center or misclassifying legitimate Maintenance
time as the employee's home department.

The system must preserve separate attendance records for individual
Maintenance work orders. When one work order ends, the employee remains in
Maintenance until an explicit clock-out or another app assigns a different
work center or work order. General time between Maintenance jobs is valid
Maintenance time and does not require a work center.

## Confirmed incident

On September 4, 2026, Odoo attendance `5434` recorded Jose Ochoa from
11:13:02Z through 11:15:37Z in his home department, Recycled, with no work
center. It was directly between two Maintenance records. Odoo's audit fields
showed that the Maintenance integration account closed Maintenance, created
the Recycled fallback one second later, then closed it and reopened
Maintenance when the next work order began.

This was not a mirror or synchronization failure. The source transition used
the employee's home department where it should have carried Maintenance
forward. Since Recycled requires a work center, Plant Manager correctly
reported the raw row as missing a location, but the underlying time was valid
general Maintenance time.

## Responsibility boundaries

### GPI Maintenance app

The Maintenance app owns the source transition that follows completion of a
Maintenance work order.

- Starting a Maintenance work order closes the current attendance assignment
  when necessary and creates a distinct Maintenance work-order attendance
  record.
- Finishing that work order closes its record and immediately creates a new
  Maintenance holding record at the same timestamp.
- The holding record has department `Maintenance`, no production work center,
  and a durable transition reason such as `maintenance:between_jobs`.
- Starting another Maintenance work order closes the holding record and opens
  the next distinct work-order attendance record.
- An explicit assignment from GPI Forklift, the Plant Manager Timeclock, or
  another supported work app closes the Maintenance holding record and wins.
- Clock-out closes the current record and creates no replacement.
- Repeated delivery of the same finish event reuses the same transition rather
  than creating duplicate attendance rows.

Finishing a Maintenance work order must never restore the employee's home
department by itself.

### Odoo

Odoo remains the attendance system of record. App-created transitions should
carry enough source metadata to explain why they exist. At minimum, a durable
source/reason and an idempotent source-event identifier must distinguish
Maintenance holding rows from Timeclock, Forklift, lunch, and manual Odoo
actions.

The exact storage mechanism may be Odoo custom fields or an existing durable
integration audit model, but it must survive process restarts and be readable
during verification. A shared Odoo user name alone is not sufficient
provenance because both manual and automated actions may use that account.

### GPI Plant Manager

Plant Manager provides defense in depth. It projects recognized Maintenance
continuity as Maintenance for reports and Live readiness, while offering a
one-click correction for inaccurate raw Odoo rows. It does not fabricate a
work center and does not silently rewrite Odoo.

GPI Forklift behavior is unchanged. Its existing pre-lunch assignment
continues to restore after lunch, and all other Forklift attendance remains
driven by the Forklift app.

## Effective continuity rules

Plant Manager treats an interval as Maintenance continuity when either of
these proofs exists:

1. The attendance row has the explicit `maintenance:between_jobs` source
   reason; or
2. A closed legacy row forms an exact, unambiguous continuity chain:
   - its work center is blank;
   - it begins exactly when the preceding Maintenance row ends;
   - it ends exactly when the next Maintenance row or mapped work-center
     assignment begins;
   - no overlapping attendance or competing app assignment exists; and
   - the raw department is the employee's home department rather than an
     explicit app-selected work location.

There is no arbitrary maximum duration. The employee remains in Maintenance
until an explicit action changes the assignment, whether the between-job time
lasts two minutes or much longer.

An explicit work center, Maintenance work order, Forklift assignment, manual
transfer, or clock-out boundary always takes precedence. Missing timestamps,
overlaps, conflicting provenance, or an open legacy fallback without an
explicit Maintenance reason are ambiguous and continue through the normal
missing-location decision flow.

The effective projection changes only the interpreted department for the
proven interval. It leaves the raw Odoo row visible for audit. Recognized
Maintenance continuity does not count as missing-location time and does not
block Live readiness.

## Lunch behavior

The lunch worker restores the complete effective pre-lunch assignment.

- Pre-lunch Maintenance returns as Maintenance after lunch, even when no work
  order is active at the return instant.
- Pre-lunch Forklift continues to use the existing Forklift carry-over rule.
- A post-lunch explicit work-center or app action replaces the restored
  assignment normally.

Lunch restoration must preserve its source and reason so a restored
Maintenance row is not confused with a home-department fallback.

## Inbox and one-click repair

An inferred legacy continuity interval appears as a non-error suggestion named
`Maintenance carry-forward`, not as `Odoo Location Missing`. The card shows
the employee, exact interval, raw home department, preceding Maintenance
assignment, and following Maintenance or mapped work-center assignment.

The action label is:

> Carry previous Maintenance department forward

Before applying, Plant Manager rereads the exact Odoo attendance row and its
boundary neighbors. The action proceeds only if the attendance ID, write
version, timestamps, blank work center, and continuity proof still match the
preview. It changes only the attendance department to the current authoritative
Maintenance department ID. It does not alter check-in, check-out, work center,
employee, or neighboring rows.

The correction records the approving user, time, attendance ID, prior value,
new value, and source versions. Retries reuse the same operation key. If the
row changed or the evidence became ambiguous, the action makes no write and
refreshes the card for a new decision.

Explicitly sourced future Maintenance holding rows need no correction card.
The card is a migration and regression safety net for inaccurate legacy or
unexpected app-created rows.

## Failure handling and monitoring

The Maintenance finish transition is a durable, idempotent operation. If the
work-order record closes but its Maintenance holding row cannot be created,
the integration retains a retryable operation rather than falling back to the
home department. An unresolved partial transition raises a technical alert.

Plant Manager tracks separate aggregate counts for:

- explicitly sourced Maintenance holding intervals;
- inferred legacy Maintenance continuity intervals;
- one-click repairs completed or rejected because evidence changed;
- ambiguous missing-location intervals; and
- home-department fallbacks produced after the Maintenance source fix.

Any new inferred fallback after the source fix is a regression signal. It is
shown to managers as a correction suggestion and to maintainers as a source
transition defect. Logs and stored audit data must avoid unnecessary employee
details while retaining Odoo attendance and event identifiers needed to trace
the failure.

## Validation

Jose's September 4 sequence is the primary acceptance fixture:

- Maintenance -> Recycled with no work center -> Maintenance projects as
  continuous Maintenance.
- The 155-second interval contributes zero missing-location time and does not
  block Live readiness.
- The Inbox displays the non-error Maintenance carry-forward suggestion.
- One click changes only the raw department to Maintenance and records the
  approval.

Automated coverage also proves:

- finishing a Maintenance work order creates one idempotent Maintenance
  holding row instead of a home-department row;
- the next Maintenance work order closes the holding row and remains a
  distinct attendance record;
- a different work center, Forklift assignment, or clock-out ends Maintenance;
- Maintenance and Forklift each retain their approved lunch behavior;
- production departments still require a work center;
- ambiguous and overlapping rows remain blocking exceptions;
- stale one-click previews cannot write;
- retries cannot duplicate holding rows or repairs; and
- existing Forklift, salaried-exemption, attendance correction, production
  attribution, and Live-readiness behavior does not regress.

Before enabling this behavior in Live calculations, replay at least the
available September attendance history in Shadow. The replay must identify
Jose's known interval and no unrelated rows. Then verify one current
Maintenance finish/start cycle end to end in Odoo, the local mirror, Plant
Manager projection, Inbox, and readiness report.

## Delivery decomposition

This design has two coordinated implementation tracks:

1. **Plant Manager safety track:** effective continuity projection,
   non-blocking suggestion, proof-fenced one-click repair, readiness behavior,
   audit, replay, and monitoring in `gpi-plant-manager`.
2. **Maintenance source track:** sticky Maintenance holding transitions,
   durable provenance, idempotent retries, and integration tests in the GPI
   Maintenance app's repository or Odoo automation owner.

The shared transition-reason and source-event contract must be finalized in
both tracks before source-created holding rows are relied upon. Plant Manager's
legacy inference remains conservative and independently testable.

## Non-goals

- Do not change GPI Forklift assignment behavior.
- Do not assign a fake production work center to general Maintenance time.
- Do not merge separate Maintenance work-order attendance records.
- Do not make all home-department gaps valid.
- Do not auto-write inferred legacy corrections to Odoo.
- Do not weaken missing-location checks for Recycled, New, or other production
  departments outside the proved Maintenance continuity rule.
