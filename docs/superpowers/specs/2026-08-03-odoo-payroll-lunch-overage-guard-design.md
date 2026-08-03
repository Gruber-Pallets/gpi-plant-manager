# Odoo payroll lunch-overage guard

**Date:** 2026-08-03

## Goal

Prevent Odoo's payroll Work Entries from leaving a 30-minute unpaid lunch in
regular hours when a weekly overtime rule moves part or all of an attendance
day above 40 hours. Correct only the exact verified draft-entry defect, retain
an audit trail, and escalate every case that is not safe to change
automatically.

## Confirmed failure

Odoo Attendance is calculating the source records correctly. For an affected
week, `hr.attendance.expected_hours` totals 40 regular hours and
`validated_overtime_hours` contains the remaining worked time. After overtime
approval, Odoo creates `hr.work.entry` rows whose regular `Attendance`
duration totals 40.5 hours.

On the affected final day, the extra 0.5 hour is the scheduled unpaid lunch.
For example, Darren Donahue's July 24 clock span was 9:01. Odoo correctly
removed the 0:30 lunch to produce 8:31 worked and correctly classified all
8:31 as overtime, but Payroll also retained 0:30 as regular Attendance. The
same exact 0.5-hour overage was confirmed across multiple people and weeks.

Plant Manager does not create `hr.work.entry` rows today, so the defect occurs
after the punch path, when Odoo converts approved attendance overtime into
payroll Work Entries.

## Chosen approach

Add a focused Plant Manager background reconciler. It will inspect recently
created or changed Odoo Work Entries, compare them with Odoo's own correct
Attendance totals, and repair only the exact known draft-only discrepancy.

This is safer than the rejected alternatives:

- An Odoo Studio automation would be unversioned, difficult to test, and easy
  to make recursive or overly broad.
- Changing the 40-hour rule, employee schedules, or lunch configuration would
  change valid payroll behavior.
- Forcing every lunch to split into two punches would make payroll correctness
  depend on a background punch occurring every day and would not repair
  already-generated entries.

## Architecture

### Odoo payroll facade

Create a private `_odoo_payroll.py` module and expose narrow wrappers from
`odoo_client.py`. The facade will:

- fetch recently written, active Work Entries that are linked to an
  attendance;
- fetch all Work Entries and Attendances for the candidate employee/date
  window;
- write one draft Work Entry's duration; and
- reread that Work Entry after the write.

The guard will not call the generic XML-RPC executor directly. Keeping model
names, domains, fields, and normalization in the facade makes the decision
logic independently testable and limits the payroll write surface.

### Pure daily classifier

The reconciler will group source rows by `(employee_id, local work date)` and
produce one of three decisions:

- `noop`: payroll and attendance agree;
- `correct`: the group is the exact known lunch defect; or
- `review`: a mismatch exists but is not safe to change automatically.

A group is eligible for `correct` only when every condition below is true:

1. The regular Work Entry type has payroll code `WORK100` and is linked to an
   Odoo Attendance.
2. The day has approved attendance overtime greater than zero.
3. Every attendance row with worked overtime reports `approved`, and its
   worked and validated overtime agree within one minute.
4. The Odoo `OVERTIME` Work Entry total agrees with validated attendance
   overtime within one minute.
5. The regular Work Entry total exceeds the summed Attendance Regular Hours
   by 0.5 hour, within one minute of floating-point tolerance.
6. Every Work Entry involved is still `draft`.
7. Exactly one regular Work Entry exists for the employee/day, so the target
   is unambiguous.
8. Removing the measured excess cannot make the duration negative.

The correction uses the measured excess, rather than blindly subtracting
`0.5`, so sub-minute Odoo float noise is removed and the corrected payroll
total exactly matches Attendance Regular Hours. A second pass is therefore a
`noop`.

Any positive or negative regular-time mismatch over one minute on a day with
overtime becomes `review` when it fails an eligibility rule. An overtime-total
mismatch also becomes `review`. The guard never guesses which record to edit.

### Reconciler and verification

Create `payroll_work_entry_guard.py` with `run_once()` as its I/O
orchestrator. Each pass will:

1. no-op when `PAYROLL_WORK_ENTRY_GUARD_ENABLED` is one of `0`, `false`, or
   `no`; the default is enabled;
2. fetch active, attendance-linked Work Entries written in the last 90 days;
3. batch-fetch the matching Work Entry and Attendance rows;
4. classify every employee/day before performing any write;
5. write corrections sequentially so one failure cannot hide another;
6. reread each corrected Work Entry and require its duration to match the
   planned value within one minute; and
7. append an audit row only after verification succeeds.

The 90-day `write_date` window catches the initial recent backlog and delayed
approval of an older attendance, because approval creates or rewrites its Work
Entries now. It also bounds every pass. The warmer will run every five minutes,
including shortly after application startup.

Fetch, write, verification, or audit failures are isolated per group and
logged at warning level. A failed write or failed verification becomes a
review issue; no follow-up write is attempted in that pass.

### Audit trail

Add an append-only `payroll_work_entry_corrections` table containing:

- Odoo Work Entry id;
- employee Odoo id and display name;
- work date;
- before and after duration;
- Attendance regular and approved-overtime totals;
- Work Entry regular and overtime totals before correction;
- verification status and detail; and
- correction timestamp.

The current values, not the audit table, determine idempotency. If Odoo later
regenerates the same record incorrectly, the guard may correct it again and
append another audit event. That preserves the complete history rather than
hiding recurrence behind a uniqueness constraint.

### Review alert

Keep one Odoo task named **Payroll work entries need review** in the existing
Plant Manager project. Persist its task id and the currently reported issue
keys in a singleton `payroll_work_entry_guard_monitor` row, following the
calendar-conflict monitor pattern.

The task body lists employee, date, mismatch, and the safety rule that blocked
automatic correction. When the set changes, update the task and add a concise
summary comment. When no issues remain, post a resolved comment and archive
the task. If the stored task was deleted, create a replacement. Failure to
create or update the alert is logged but never broadens the write rules.

Validated Work Entries and payslips are never modified. They remain review
issues for a payroll manager.

## Data safety

- Never change `hr.attendance`, check-in/out timestamps, employee schedules,
  overtime rules, Overtime Work Entries, validated Work Entries, or payslips.
- Never call Odoo's Regenerate Work Entries action.
- Never correct an ambiguous multi-row regular group.
- Never correct a discrepancy other than the exact verified 30-minute lunch
  pattern.
- Keep a runtime kill switch that requires no deployment.
- Treat Odoo as the source of truth for current payroll state and Postgres as
  the immutable correction audit only.

## Rollout

The first production pass scans the 90-day window. Recent affected rows that
are still draft, including the confirmed W30 and W31 cases, are eligible for
automatic correction. Validated historical rows are reported for human review
instead of changed.

Production logs will report the number of corrected, review, and no-op groups
on each non-empty pass. The first pass must be checked against the known
affected people and dates. `PAYROLL_WORK_ENTRY_GUARD_ENABLED=0` is the immediate
rollback if anything unexpected appears.

## Testing

- Characterize the exact 40.5 regular / 40.0 attendance / matching-overtime
  defect and prove it produces one correction.
- Prove the corrected values produce `noop` on the next classification.
- Prove no overtime, unapproved overtime, overtime-total disagreement,
  non-0.5 mismatches, validated rows, multiple regular rows, and negative
  results all refuse automatic correction.
- Prove `run_once()` writes one eligible draft duration, rereads it, and appends
  one audit event.
- Prove failed writes and failed verification do not append a successful audit
  and do create review issues.
- Prove one group failure does not block another eligible group.
- Prove the Odoo review task is created, updated, recreated after deletion, and
  archived after all issues clear.
- Prove the five-minute warmer is registered and the kill switch performs zero
  Odoo calls.
- Run focused tests, Ruff on touched Python, and the full local test suite.

## Out of scope

- Repairing validated Work Entries or already-issued payslips.
- Changing Odoo's overtime rules, working schedules, lunch rows, or Work Entry
  source.
- Building a payroll editor in Plant Manager.
- Automatically filing or managing an Odoo Support ticket.
