# Retroactive PTO for Recorded Absences — Design

**Date:** 2026-08-28

**Task:** 3629 · GPI-PM-FB-42

**Status:** Approved design; implementation has not started

## Context

Employees currently request time off from the Timeclock kiosk. The full-day
request form sets the earliest selectable date to today. Its submit handler can
parse an earlier date, but self-service gives employees no safe way to choose
one. A manager therefore has to correct a missed day manually when an employee
wants to use PTO so the absence is paid.

Plant Manager already records a manager-declared absence in
`manual_absences`. It also creates an approved Odoo `Absence` leave when Odoo
can represent the day. That attendance fact must remain in history even if the
employee later uses PTO. Odoo does not allow an approved Absence leave and a
second PTO leave to overlap, so an ordinary retroactive `time_off_requests`
draft cannot be pushed while the Absence leave remains active.

The product therefore needs to distinguish two facts:

- **Attendance:** the employee missed work and remains recorded as absent.
- **Pay treatment:** the employee may ask to spend one PTO day so that absence
  is paid.

## Decisions

1. Retroactive self-service is limited to recorded full-day absences.
2. The absence must be earlier than the current plant day and within the
   configured current pay period.
3. One request covers one absence day. Multiple days require separate requests.
4. The employee cannot choose an arbitrary historical date or request a past
   partial day.
5. The request follows manager approval. It never changes Odoo or payroll while
   pending.
6. The attendance absence remains in `manual_absences` after approval.
7. Staffing shows the combined attendance and pay status: `Absent`,
   `Absent · PTO pending`, or `Absent · PTO`.
8. A request that reaches the next configured pay period before approval is not
   converted automatically. It becomes a Wendy review case.
9. Closed-period review creates one deduplicated Odoo task per request, assigned
   to the Odoo user for `wendy@gruberpallets.com`.

## Goals

- Let an employee request one PTO day for one recorded absence in the current
  pay period.
- Keep the absence in attendance counts and history.
- Keep Odoo's approved Absence leave unchanged until a manager approves PTO.
- On approval, safely replace the Odoo pay treatment from Absence to PTO and
  deduct the employee's real Odoo PTO balance.
- Make every manager decision and every Odoo ID transition auditable.
- Recover safely from retries, lost responses, concurrent approvals, and app
  restarts.
- Escalate closed-period or unrecoverable cases to Wendy without creating
  duplicate Odoo tasks.

## Non-goals

- Retroactive arriving-late, leaving-early, or mid-day-gap requests.
- Requests for dates that are not present in `manual_absences`.
- Multi-day retroactive requests.
- Automatic changes to a pay period after its configured window has rolled
  over.
- Removing, undoing, or reclassifying the attendance absence.
- Replacing the existing future and same-day time-off request flow.

## Employee Experience

The Time Off landing page gains a separate **Use PTO for a Past Absence**
action. It is visually and verbally distinct from **Full Day(s) Off**, which
continues to handle today and future dates.

The new screen lists the signed-in employee's candidate recorded absences. Each
row shows the date and current pay state. A row is submit-eligible only when all
of the following are true:

- the `manual_absences` row belongs to the signed-in employee;
- the employee has an Odoo employee ID;
- the date is before the current plant day;
- the date is within the current configured pay-period bounds;
- no active or approved retroactive PTO request already covers that absence;
- no approved PTO leave already pays that date;
- Plant Manager resolves exactly one active allocation-required full-day leave
  type named `Paid Time Off`; and
- the cached Paid Time Off balance has at least one practical day available.

A candidate absence remains visible but disabled when the PTO type is
unavailable or the balance is below one day. The screen explains the exact
reason so the employee is not left wondering where the missed day went. A
missing or ambiguous Paid Time Off type is also surfaced to managers as a
configuration problem; Plant Manager never guesses another paid leave type.

The server is authoritative for every condition. The kiosk does not trust a
posted employee ID, date, balance, or PTO type. It derives them again from the
authenticated kiosk token, the absence row, the pay-period configuration, and
the leave-type/balance cache.

The employee selects one date and sees:

- `Paid Time Off` as the fixed pay type;
- the absence date;
- available PTO;
- `This request: 1 day`; and
- the remaining balance after approval.

Submitting creates a local linked request and shows a confirmation. No Odoo
leave is created yet. The request appears in **My Requests** with a `Past
absence` marker and a Pending, Approved, Denied, Needs review, or Handled
status. A denied absence may be submitted again while it is still eligible;
only one active request is allowed at a time.

## Manager Experience

The existing Time Off approvals panel includes linked absence-PTO requests in
the same chronological queue as ordinary time off. A linked row shows:

- employee and absence date;
- `Past absence` and `Paid Time Off` labels;
- current treatment: `Absent · unpaid`;
- current PTO balance and one-day request amount;
- whether the configured pay-period window is still open; and
- Approve and Deny actions.

Denial continues to require a reason. Denying changes no Odoo record. It leaves
the approved Absence leave and `manual_absences` row intact.

Approval is one final manager action. The app performs the Absence-to-PTO
conversion and returns success only after it verifies the approved PTO in
Odoo. If the request is no longer safe to approve, the manager sees a specific
reason instead of a generic sync error.

## Staffing and History Presentation

`manual_absences` remains authoritative for whether the employee was absent.
The linked request supplies only the pay-treatment suffix:

| Attendance fact | Linked PTO state | Staffing label |
|---|---|---|
| Absent | none or denied | `Absent` |
| Absent | pending or converting | `Absent · PTO pending` |
| Absent | approved | `Absent · PTO` |
| Absent | needs review | `Absent · PTO review` |
| Absent | resolved manually without detected PTO | `Absent · handled` |

The existing light-red absence treatment remains. A PTO suffix does not turn
the row into an ordinary blue time-off row, remove it from absence totals, or
make the employee appear present. Past Staffing views and any absence report
continue to count the day as an absence.

## Pay-period Boundary

Eligibility reuses the pay-period configuration already owned by
`staffing_hours`: its saved anchor and cycle length, with the approved 14-day
default. Date calculations use the plant timezone. A small public pure helper
will expose the inclusive current-period bounds so the Hours report and this
feature cannot drift.

For this workflow, the configured period rollover is the automatic-mutation
boundary. A pending request whose absence date is no longer in the current
period moves to `needs_review`; Plant Manager does not try to infer that a
late payroll mutation is safe.

The boundary is checked:

- when the eligible-absence list is rendered;
- when the employee submits;
- immediately before manager approval; and
- by a periodic reconciliation worker so an untouched pending request is
  escalated after rollover.

## Data Model

Use a dedicated `absence_pto_requests` table rather than overloading
`time_off_requests`. Ordinary drafts are automatically pushed by the existing
time-off retry worker; storing a linked request there would prematurely collide
with the approved Odoo Absence leave.

The new table stores:

- request ID;
- absence day and `emp_id`, matching the composite `manual_absences` key;
- Odoo employee ID and employee-name snapshot;
- Paid Time Off leave-type ID and name snapshot;
- PTO practical balance at submission;
- original Odoo Absence leave ID, when one exists;
- replacement Odoo PTO leave ID, once created;
- state: `pending`, `converting`, `approved`, `denied`, `needs_review`, or
  `resolved_manually`;
- durable conversion step: `not_started`, `absence_refused`, `pto_created`, or
  `pto_approved`;
- sync/recovery error text;
- Odoo review-task ID and task-delivery retry metadata;
- employee note, denial reason, or manual-resolution note;
- requester, decision-maker, and resolver identity snapshots; and
- requested, decided, resolved, created, and updated timestamps.

A partial unique index permits only one active request for `(day, emp_id)` in
`pending`, `converting`, or `needs_review`. Approved or manually resolved cases
make the absence ineligible. Denied history remains append-only but does not
prevent a later request if the date is still otherwise eligible.

The existing `time_off_decisions` and unified inbox audit remain the manager
decision history. Their denormalized records gain a request-kind/key field so
an absence-PTO request cannot collide numerically with an ordinary
`time_off_requests` ID. The audit snapshots both original and replacement Odoo
leave IDs in structured detail.

## State Transitions

```text
eligible absence
    -> pending

pending
    -> denied                     manager denies; no Odoo mutation
    -> needs_review               pay period rolls over
    -> converting/not_started     manager approves in open period

converting/not_started
    -> converting/absence_refused original Odoo Absence refused or absent
    -> pending                    refusal failed before any change

converting/absence_refused
    -> converting/pto_created     matching PTO found or created
    -> pending                    PTO failed and Absence restoration verified
    -> needs_review               PTO failed and restoration was not verified

converting/pto_created
    -> converting/pto_approved    PTO approval verified in Odoo
    -> pending                    PTO closed and Absence restoration verified
    -> needs_review               compensation was not verified

converting/pto_approved
    -> approved                   local mirrors, links, and audit finalized

needs_review
    -> approved                   matching approved PTO later detected
    -> resolved_manually          Wendy records another resolution with a note
```

State and conversion-step writes occur before the next external side effect.
The background reconciler can therefore resume from the last verified point
after a process crash.

## Approval Conversion

Approval acquires a transaction-scoped lock for the linked request and absence.
It then rechecks:

- request state and absence ownership;
- presence of the `manual_absences` row;
- current pay-period eligibility;
- absence date is before today;
- Paid Time Off leave-type identity;
- practical PTO balance is at least one day; and
- the current Odoo state of any linked Absence/PTO record.

If safe, the converter performs these idempotent steps:

1. Persist `converting/not_started`.
2. If an approved original Absence leave exists, refuse it and verify the
   refused state. A locally recorded absence with no active Odoo leave skips
   this step but keeps the same durable transition.
3. Search Odoo for a matching one-day PTO leave before creating one. This
   adopts a record created before a lost network response instead of creating
   a duplicate.
4. Create the PTO leave if no match exists, persist its ID, and verify it.
5. Confirm and fully approve the PTO through the existing Odoo leave helpers.
6. Reread Odoo and require final state `validate` with the expected employee,
   day, and leave type.
7. Mirror the approved PTO into `time_off_requests`, settle the old Absence
   mirror as refused, and update `manual_absences.odoo_leave_id` to the current
   pay-treatment leave. The linked request retains both old and new IDs.
8. Persist `approved`, cascade cache invalidations, create the employee result,
   and append manager/inbox audit records.

If another manager retries approval, the lock and Odoo rereads return the
already-verified result without spending a second PTO day.

## Compensation and Failure Handling

- **Original Absence refusal fails:** stop before creating PTO, return the
  request to `pending`, and show the manager the safe retry error.
- **PTO creation/approval fails after refusal:** close any incomplete PTO copy,
  reset the original Absence to the approval flow, approve it, and verify the
  restored `validate` state before returning to `pending`.
- **No original Odoo Absence existed:** if PTO creation/approval fails, verify
  that no active PTO copy remains and return to `pending`; the local attendance
  absence was never changed.
- **Compensation cannot be verified:** do not guess. Move to `needs_review`,
  preserve every known ID and error, and create the Wendy task immediately.
- **Balance falls below one day:** make no Odoo change. Leave the request
  pending and tell the manager the current balance prevents approval; the
  manager may deny it.
- **Absence was removed:** block approval and retain the request for manager
  denial or manual review. Never recreate an attendance absence from a PTO
  request.
- **Odoo is unavailable:** preserve the durable step. The background worker
  resumes only after rereading the affected Odoo records.
- **Post-Odoo local finalization fails:** the durable PTO ID and step let the
  worker adopt the verified approved PTO and finish local mirrors/audit without
  another Odoo mutation.

## Wendy Odoo Review Task

A `needs_review` request owns one Odoo task in the existing **Plant Manager**
project. The task title is deterministic and includes the linked request ID,
employee, and absence date. Creation first searches by the exact title; after
an ambiguous timeout it searches again before retrying. The local task ID is
saved as soon as creation is confirmed.

The assignee resolver searches for one active Odoo user whose normalized login
matches `wendy@gruberpallets.com`. Zero or multiple matches are treated as a
delivery error, not silently assigned to the API account. Delivery errors stay
on the request and retry without changing its review state.

The task includes:

- employee and absence date;
- original Absence and any replacement PTO Odoo IDs;
- requested PTO type and balance at submission;
- request date and manager attempt, if any;
- the reason automatic conversion stopped;
- the last verified conversion step;
- a link to the Plant Manager review surface; and
- a next-business-day deadline.

One task is created per request so each case can be owned and closed
independently. The reconciler updates the task body when the verified state
changes rather than creating a new task.

If an approved matching PTO later appears in Odoo, Plant Manager marks the
linked request `approved`, updates Staffing to `Absent · PTO`, posts a resolved
message, and closes the Odoo task. If Wendy chooses another solution, the
Plant Manager action **Mark handled** requires a note, stores
`resolved_manually`, appends audit history, posts the note, and closes the
task. Merely moving the Odoo task to Done never asserts that PTO was paid.

## Components and Boundaries

- **Eligibility/query module:** owns employee/date/pay-period/absence checks
  and returns presentation-safe eligible rows.
- **Linked-request store:** owns state transitions, locks, unique active
  requests, task metadata, and audit snapshots. It does not call Odoo.
- **Conversion service:** orchestrates Odoo reads/mutations and durable steps.
  It exposes one approve/resume operation and depends on narrow store and Odoo
  interfaces.
- **Review-task service:** owns Wendy lookup, deterministic task identity,
  create/adopt/update/close behavior, and retry classification.
- **Reconciler:** finds interrupted conversions, period-rollover requests, task
  delivery retries, and externally approved PTO. It delegates all mutations to
  the conversion or task service.
- **Routes/templates:** authenticate, validate ownership, render the employee
  and manager surfaces, and translate service results into clear messages.
- **Staffing projection:** combines `manual_absences` with the linked request
  pay state. It never decides eligibility or performs writes.

These boundaries keep ordinary time-off sync unchanged and make the risky
cross-system conversion independently testable.

## Security and Privacy

- Kiosk routes use the existing short-lived signed token and derive employee
  identity server-side.
- A request date must resolve through that employee's own `manual_absences`
  row. Posted employee IDs, Odoo IDs, PTO type IDs, and balances are ignored.
- Manager approval and manual-resolution endpoints retain the existing
  authenticated manager boundary.
- Odoo task descriptions contain only operational facts needed to resolve pay;
  they do not copy private absence reasons or unrelated leave details.
- Notes and identifiers are HTML-escaped before entering an Odoo task.

## Testing

### Pure rules

- current pay-period bounds before, on, and after the anchor;
- plant-day boundary around midnight and daylight-saving changes;
- past-only and current-period eligibility;
- existing absence required;
- active/approved duplicate exclusion and denied-request resubmission;
- fixed Paid Time Off type and one-day practical-balance requirement; and
- Staffing label projection for every linked state.

### Routes and stores

- an employee sees and submits only their own eligible absences;
- forged employee/date/type/balance values are rejected;
- one active request wins under concurrent submissions;
- My Requests and manager approvals include the linked request markers;
- denial requires a reason and performs no Odoo call;
- append-only decision and inbox audit contain request kind plus both Odoo IDs;
  and
- cache invalidation refreshes Staffing, approvals, and employee history.

### Conversion matrix

- absence with active Odoo leave, local-only absence, and already-refused
  absence;
- successful refuse, create/adopt, approve, verify, and local finalization;
- double approval and two concurrent managers;
- failure before refusal, after refusal, after PTO create, after PTO approval,
  and during local finalization;
- successful and failed compensation at each relevant step;
- lost create response adopts the one matching PTO record;
- app restart resumes from every durable conversion step;
- balance loss and pay-period rollover immediately before approval; and
- no duplicate PTO deduction or Odoo leave under any retry path.

### Wendy-task lifecycle

- rollover and failed compensation produce `needs_review`;
- exact Wendy-user lookup, zero-match, and multiple-match behavior;
- create timeout followed by task adoption;
- retries never create a second active task;
- task body escaping and required case details;
- externally approved PTO closes the task and marks the request approved; and
- manual handling requires a note and never claims PTO was approved.

### Regression

- ordinary today/future full-day requests;
- all three partial-day request shapes;
- existing Odoo time-off approval/denial;
- declare/undo absence and local-record fallback;
- scheduler full-day removal and absence counts; and
- Staffing Hours pay-period presets.

## Acceptance Criteria

1. A worker with an eligible current-period recorded absence can submit one
   full-day PTO request for that day without manager data entry.
2. Submission leaves the approved Odoo Absence and payroll treatment unchanged.
3. The manager can approve or deny the linked request from the existing time-off
   approval surface.
4. Approval produces exactly one verified approved PTO leave, spends one PTO
   day, and keeps the attendance absence.
5. Staffing distinguishes unpaid, pending-PTO, approved-PTO, and review states
   without removing the absence from counts.
6. Denial leaves the original absence unchanged and records the reason.
7. Closed-period and unrecoverable cases create exactly one task assigned to
   Wendy and never mutate payroll automatically.
8. Retries, concurrent actions, lost responses, and restarts cannot create a
   duplicate leave or spend PTO twice.
9. Ordinary time-off and absence workflows continue to behave as they do now.
