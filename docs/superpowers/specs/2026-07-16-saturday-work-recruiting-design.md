# Saturday Work Recruiting — Design

**Date:** 2026-07-16
**Status:** Approved design; implementation not started

## Context

Saturday production is optional and occasional. Managers decide which work
centers will run and how many qualified operators each one needs, usually on
the Wednesday or Thursday before. Today the staffing scheduler supports draft
and published Saturday schedules, configurable Saturday shift hours, and the
rule that a published Saturday becomes an operational workday. It does not
provide a formal way to ask qualified employees to volunteer.

The timeclock is the natural employee touchpoint. Every hourly employee taps
their name there, the screen already supports personalized interstitials and
punch-out reminders, and the staffing schedule already owns the final work-
center assignments.

## Goals

1. Let a manager open official recruiting for a specific Saturday by choosing
   positions and requested headcount.
2. Offer Saturday work only to employees who can fill a remaining position
   with level 2 or 3 qualifications.
3. Record a firm employee commitment without assigning the final work center;
   accepted employees move from **Off** to **Unassigned** on the Saturday
   staffing page.
4. Support full-shift commitments and discreet partial-shift commitments in
   30-minute increments.
5. Make the response and cancellation deadline explicit everywhere. The
   deadline is the clock-in time for the nearest earlier plant workday.
6. Remind committed employees on their final normal punch-out before Saturday.
7. Make exact Spanish level 3 select Spanish-first presentation throughout the
   personalized timeclock, with smaller English underneath. Everyone else sees
   English only.

## Non-goals

- Automatically assign committed employees to final work centers.
- Create a waitlist or accept more commitments than requested openings.
- Send SMS, email, or push notifications. Managers remain responsible for
  directly contacting employees who will not use the timeclock again after a
  late cancellation of the Saturday.
- Change normal weekday scheduling behavior.
- Allow employee self-service changes after the response deadline.

## Product decisions

### Recruiting and publishing are separate

Opening Saturday recruiting creates official employee commitments but leaves
the staffing schedule in draft. The lifecycle is:

1. **Plant closed:** before recruiting, every active employee appears in Off on
   that Saturday.
2. **Recruiting:** the manager activates requested positions and counts.
3. **Closed:** the response deadline passes; employee responses and
   self-service cancellations are locked.
4. **Published:** managers resolve shortages, place every committed employee,
   and publish the final staffing schedule.
5. **Cancelled:** the manager cancels the Saturday and committed employees are
   notified at their next timeclock interaction.

The existing `schedules.published` value remains the operational signal used by
dashboards, shift calculations, and punches. Recruiting does not make Saturday
an operational workday by itself.

### One commitment fills one opening

A full-shift or partial-shift commitment fills exactly one requested opening.
Partial hours are visible beside the employee in Unassigned and later beside
their final assignment, but do not keep the opening available.

An employee does not choose a position. If they qualify for more than one
requested position, the system may rematch their internal reservation as later
employees respond. This reservation exists only to guarantee that the accepted
group can cover the requested qualification mix; it is not the employee's
final work-center assignment.

### Commitments are firm but cancellable before the deadline

Yes requires a second confirmation that states the exact Saturday hours and
deadline. After confirming, the employee can open their Saturday commitment
from the timeclock dashboard and cancel until the same deadline. Cancellation
moves them back to Off and reopens a compatible slot. After the deadline, the
timeclock directs them to contact a manager.

No suppresses the offer for that Saturday permanently. Decide Later returns to
the normal timeclock and presents the offer on later name taps while a
compatible opening remains and the deadline has not passed. A cancelled
commitment is not prompted again automatically.

## Manager experience

On a Saturday staffing page, a **Saturday Work** panel appears above the normal
schedule controls.

Before activation, the panel lets the manager:

- confirm the Saturday shift start and end;
- add one or more existing work centers as requested positions;
- set the headcount for each position; and
- preview the calculated response deadline.

Only work centers with configured required skills may be requested. A person
qualifies for a requested work center when every required skill is level 2 or
3. A configuration message directs managers to Settings when a work center has
no required skills.

Activating recruiting displays:

- Recruiting status;
- total filled and requested openings;
- the snapshotted Saturday shift hours;
- the exact response deadline;
- filled/requested coverage for each position; and
- the committed people in Unassigned, including partial hours.

Managers may increase counts or add positions while recruiting remains open.
They may reduce only unfilled openings. The Saturday shift hours lock after the
first commitment because changing them would alter an accepted promise.

The normal Saturday staffing view is specialized as follows:

- committed and not assigned → Unassigned;
- committed and assigned → the selected work center;
- approved full-day time off → the existing Time Off section; and
- every other active employee → Off.

At the deadline, recruiting closes automatically. Publishing is unavailable
before the deadline and is blocked afterward until:

- every active commitment is assigned exactly once;
- every requested position has the requested number of assigned level 2/3
  operators; and
- no committed employee is inactive or has conflicting full-day time off.

Managers may reduce unfilled requests after the deadline to reconcile a real
shortage. They may not remove an opening already backed by a commitment without
first resolving that commitment.

When an employee contacts management to cancel, a manager can cancel that
individual commitment at any time. The action records the acting manager and
reason, returns the employee to Off, and leaves the opening unfilled. Before the
deadline that capacity becomes available to eligible employees again; after the
deadline it remains a manager-visible shortage that must be filled or removed
before publication.

Cancelling the entire Saturday requires confirmation. It cancels the recruiting
record, removes the commitments from the active Saturday roster, and queues a
timeclock notification for every committed employee. The confirmation names
the committed employees and warns that management must directly contact anyone
who may not tap the timeclock again.

## Employee experience

### Shared home banner

While recruiting is open and at least one opening remains, the center of the
shared timeclock header displays:

> **Saturday Work Available**<br>
> Trabajo disponible el sábado<br>
> Respond by Friday at 7:00 AM · Openings may fill

The real date and time replace the example. The shared banner is English-first
and bilingual because the employee has not identified themselves yet. It hides
when every opening is filled and returns if a cancellation reopens capacity.

### Offer after tapping a name

The offer appears only when all of these are true:

- recruiting is open and before its deadline;
- the employee is active and uses the hourly punch flow;
- the employee has not declined, committed, or cancelled;
- the employee has no approved full-day time off for the Saturday; and
- adding the employee still permits a complete skill-slot match.

The offer shows the Saturday date, full shift hours, exact deadline, and the
warning that openings may fill before the deadline. The primary choices are
**Yes**, **No**, and **Decide Later**. A quieter text link beneath them reads
**I can work only part of the shift**.

Yes opens a confirmation screen. The partial-shift link first asks for arrival
and departure times within the Saturday shift in 30-minute increments, then
opens the same firm confirmation. Arrival must be earlier than departure. No
minimum partial duration is imposed.

After a commitment, later timeclock visits show a compact Saturday status card
on the normal dashboard. Before the deadline it includes **Cancel Saturday
commitment**. After the deadline it replaces that action with **Contact a
manager to make a change**.

No records a declined response and returns to the normal punch flow. Decide
Later records the latest response time for audit but does not reserve a slot.

### Punch-out reminder

On an employee's normal clock-out on the nearest earlier plant workday, a
committed employee sees a one-time reminder before returning to the shared home
screen:

> **Saturday work reminder**<br>
> You're scheduled tomorrow from 7:00 AM–11:30 AM.<br>
> Work area: check with your supervisor.

The reminder uses the employee's committed full or partial hours. If the final
schedule has already been published and contains an assignment, the work-center
name replaces the supervisor message. Transfers and automatic lunch punches do
not trigger the reminder. The punch succeeds even if the reminder lookup fails.

## Timeclock language behavior

The current `spanish_speaker` boolean identifies any non-zero Spanish language
skill and renders English above smaller Spanish. That is insufficient for the
approved rule.

Odoo synchronization will store the actual Spanish level, `0` through `3`, on
the local person record. Personalized timeclock routes derive a language mode:

- Spanish level exactly 3 → Spanish first, smaller English underneath;
- every other value, including levels 0–2 or missing → English only.

The centralized timeclock translation helper will render both the existing
timeclock strings and the new Saturday strings according to this mode. Unknown
translations degrade to English rather than blank text. The shared home screen
continues to use intentionally hard-coded bilingual text because no person is
known yet.

## Data model

### `saturday_recruitments`

One row per Saturday:

- `day DATE PRIMARY KEY`;
- `status TEXT` constrained to `recruiting`, `closed`, `published`, or
  `cancelled`;
- snapshotted `shift_start TIME` and `shift_end TIME`;
- snapshotted `response_deadline TIMESTAMPTZ`;
- `activated_at`, `closed_at`, `published_at`, and `cancelled_at` timestamps;
- nullable activating/cancelling manager identity populated from the
  authenticated manager session; and
- normal creation/update timestamps.

The target date must be a Saturday. Activation is rejected if the calculated
deadline has already passed or there are no requested openings.

### `saturday_recruitment_openings`

One row per requested work center:

- `day` referencing the recruitment;
- `wc_id` referencing `work_centers`;
- positive `requested_count`; and
- primary key `(day, wc_id)`.

Required skills remain owned by `work_center_required_skills`; they are not
duplicated in this table.

### `saturday_work_responses`

One row per person and Saturday:

- `day` and `person_id` as the primary key;
- `status` constrained to `later`, `declined`, `committed`, or `cancelled`;
- committed `availability_start` and `availability_end`, null unless committed
  or retaining cancelled audit history;
- a qualification snapshot containing the requested work centers the employee
  was eligible to cover when they committed;
- `responded_at`, `committed_at`, `cancelled_at`, and
  `punch_reminder_shown_at`; and
- nullable manager-cancellation identity and reason; and
- normal creation/update timestamps.

The qualification snapshot keeps a later skill change from silently revoking
an accepted promise. Publication still validates current qualifications and
shows a blocking warning if coverage has changed.

Existing `employee_notifications` is generalized with an optional Saturday
date/key so a cancelled Saturday can produce one idempotent notification per
committed employee.

The `people` table gains the exact synchronized Spanish level. The existing
`spanish_speaker` field and object-API meaning remain unchanged for backward
compatibility: any non-zero Spanish level is true. Personalized timeclock
language ignores that boolean and derives Spanish-first mode only from exact
level 3.

## Deadline calculation

Starting from the day before the target Saturday, walk backward to the nearest
date the plant treats as a configured workday. Use that date's configured shift
start, including a saved per-day override when present; otherwise use the
company schedule. Combine the date and time in `America/Chicago` and persist the
result as the recruitment deadline.

Both the Saturday shift and deadline are snapshots. Later schedule-setting
changes do not alter an open recruitment. Every display reads the persisted
deadline rather than independently recalculating it.

## Qualification matching and concurrency

Each requested count expands into individual slots. A person has an edge to a
slot when their minimum level across all required skills for that work center
is at least 2. A deterministic bipartite maximum matching checks whether every
current commitment plus the candidate can occupy a distinct slot.

The commit transaction:

1. locks the recruitment row;
2. verifies status and deadline;
3. loads requested openings and active commitments;
4. builds the candidate's current qualification edges;
5. rejects the request unless a full matching exists;
6. upserts the committed response and qualification snapshot; and
7. commits before showing success.

This row lock serializes attempts for the last compatible opening. A stale
confirmation that loses the race displays:

> That opening was just filled. You have not been scheduled.

No slot-to-person reservation needs to be persisted. Coverage displays are
derived from the same deterministic matcher. A full or partial commitment has
identical capacity weight: one slot.

Cancellation uses the same recruitment lock, verifies that the deadline has
not passed, marks the response cancelled, and makes the resulting capacity
immediately available. Manager-initiated cancellation uses the same locked
operation without the employee deadline restriction and records the manager and
reason.

## Component boundaries

- **`saturday_recruiting` domain module:** pure deadline calculation,
  eligibility, matching, lifecycle transitions, message models, and validation.
- **`saturday_recruiting_store` module:** Postgres reads and atomic writes for
  recruiting, openings, and responses.
- **Staffing route/view adapter:** manager controls, Saturday Off/Unassigned
  derivation, assignment validation, and publish/cancel integration.
- **Timeclock route adapter:** shared banner context, personalized offer and
  response routes, commitment card, cancellation, and punch-out reminder.
- **Timeclock i18n:** exact Spanish-level mode and Spanish-first rendering.
- **Employee notifications:** idempotent cancelled-Saturday messages.

The store exposes business-shaped operations rather than raw table writes. The
routes do not implement matching or deadline logic.

## Failure and edge-case behavior

- Every response and cancellation rechecks the persisted deadline on the
  server; client clocks are irrelevant.
- A database or matching failure never produces a success screen and never
  modifies the staffing view. The employee sees a retry message.
- Reading the feature after the deadline treats `recruiting` as effectively
  closed even if a background status update has not run yet. A periodic worker
  persists the `closed` state for reporting, but correctness does not depend on
  that worker.
- If a person's skill falls below level 2 after commitment, the commitment
  remains. Publication blocks with a manager-facing qualification warning.
- If a committed person becomes inactive or receives full-day time off,
  publication blocks until management resolves the conflict.
- A partial commitment must remain within the snapshotted Saturday shift and
  use 30-minute boundaries.
- A cancelled Saturday never produces a punch-out reminder.
- The employee's punch is never rolled back because a Saturday reminder or
  notification lookup failed.
- Repeated activation, confirmation, cancellation, deadline closure, and
  cancelled-Saturday notification requests are idempotent.

## Testing strategy

### Pure unit tests

- nearest-prior-workday deadline calculation, custom prior-day start, Central
  Time, and daylight-saving boundaries;
- single- and multi-skill level 2/3 eligibility;
- deterministic multi-skilled matching and impossible coverage mixes;
- full and partial commitments consuming exactly one slot;
- partial time validation and 30-minute boundaries;
- lifecycle transition and after-deadline rejection rules; and
- Spanish level 3 selection, Spanish-first order, English-only fallback, and
  missing translations.

### Store and concurrency tests

- activation snapshots hours and deadline;
- response upserts and audit timestamps;
- two transactions racing for the final compatible slot produce one success;
- cancellation reopens capacity;
- filled requested counts cannot be reduced; and
- lifecycle operations and notifications are idempotent.

### Route and rendering tests

- shared banner visibility while capacity exists, disappearance when full, and
  return after cancellation;
- eligibility gating, No suppression, and Decide Later repetition;
- full and partial confirmation flows and stale-screen rejection;
- commitment card and cancellation cutoff behavior;
- Saturday Off, Unassigned, and assigned-list derivation;
- pre-deadline publish rejection and post-deadline coverage validation;
- cancelled-Saturday notification behavior;
- one-time full/partial punch-out reminder with assigned and unassigned text;
- Spanish-primary and English-only versions of all personalized timeclock
  screens; and
- existing weekday staffing, notification, time-off reminder, punch, transfer,
  and auto-lunch regressions.

## Success criteria

- Managers can open a Saturday with position-specific counts and see accurate
  qualified coverage as responses arrive.
- No more employees can commit than the requested skill mix can support, even
  under concurrent taps.
- A committed employee appears in Saturday Unassigned immediately and receives
  clear deadline, cancellation, and reminder messaging.
- Partial commitments are visible and consume one requested opening.
- The final Saturday schedule cannot publish with unresolved commitments or
  requested skill shortages.
- Exact Spanish level 3 reliably produces Spanish-first personalized timeclock
  screens; everyone else sees English only.
