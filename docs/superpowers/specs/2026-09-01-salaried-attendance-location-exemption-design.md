# Salaried attendance-location exemption

## Purpose

Odoo identifies salaried employees with `wage_type = "monthly"`.  They may
clock in without selecting a work center because their work is often not tied
to one production location.  Those no-location attendance records must not
create an attendance exception or prevent the Odoo location rollout from
becoming ready.

At the same time, managers can work in the plant.  When Luke records an Odoo
department and work center for a salaried employee, that attendance span stays
normal location truth and remains available for staffing and production.

## Rule

The shared attendance timeline will decide whether a missing work center is
required for each employee rather than only for their department:

- `monthly` (Odoo Fixed Wage / salaried) means a missing work center is
  `exempt_no_location`, even when the department normally requires one.
- `hourly` and an absent or unknown wage type remain location-required under
  the existing department policy.  This deliberately fails safe for unclear
  Odoo data.
- A present, mapped Odoo work center is always projected as a normal valid
  span regardless of wage type.  The exemption never removes or changes that
  location data.

## Data flow

The local `people.wage_type` value already comes from the Odoo employee sync.
The local-only attendance projection will receive the employee wage type while
building its source rows and use it only when choosing the status for an
otherwise missing work center.  Every existing consumer of that projection
(Inbox, readiness, Staffing, and shadow comparison) therefore shares the same
answer.

The shadow-comparison origin digest will include the wage type used for each
employee.  A later Odoo employee-sync change from hourly to salaried, or the
reverse, clears stale clean evidence before it can certify rollout readiness.

## Failure behavior

No Odoo call is made during a dashboard/readiness request.  If a local wage
type is missing or unrecognized, the employee is treated as location-required.
This avoids accidentally exempting hourly production work because Odoo data
was incomplete.

## Tests

Tests will first demonstrate that a salaried employee without a work center is
exempt, while an hourly/unknown employee in the same department still produces
the existing missing-location state.  They will also prove that a salaried
employee with a real mapped work center remains valid and that changing wage
type invalidates shadow readiness evidence.
