# Anniversary PTO Employee Reminders

**Date:** 2026-09-02
**Status:** Approved design, awaiting implementation plan
**Feedback:** GPI-PM-FB-40

## Goal

Remind an employee before their work anniversary when they still have unused
Paid Time Off. The employee must acknowledge the reminder before reaching the
time-clock punch screen, and managers must be able to review the display and
acknowledgement history on that employee's Staffing page.

The same work also restores a visible Employee tab under Staffing and fixes
employee profiles that currently activate the unrelated Performance → People
navigation.

## User experience

### Employee reminder

Plant Manager generates the reminder when all of these conditions are true:

- the employee is active and has an Odoo employee identity;
- the employee has a valid first contract date;
- the next observed work anniversary is between today and 30 calendar days
  away, inclusive;
- the anniversary completes at least one year of service;
- exactly one active, allocation-backed leave type is named `Paid Time Off`;
  and
- that employee's `available_practical` balance for the exact leave type is
  greater than zero.

The reminder appears through the existing private notification interstitial at
`/timeclock/notifications/<secure-session-token>`. It identifies the work
anniversary date and the unused balance, retaining Odoo's unit of days or hours.
The button reads **I acknowledge**. The employee cannot reach the dashboard and
punch controls until the acknowledgement request succeeds.

The existing bilingual kiosk rules apply. Spanish-primary employees see the
Spanish message first, bilingual employees see both supported languages, and
English-primary employees see English. The notification feature kill switch
continues to control the entire interstitial.

### Staffing Employee tab

Add **Employee** to the Staffing subtabs, immediately after Plant Scheduler.
The tab links to the existing `/staffing/people` landing route, which selects the
first active employee. Individual profiles keep their existing stable,
name-addressed path `/staffing/people/<url-encoded-name>`, so links from the
Skills Matrix, Plant Scheduler, dashboards, and old bookmarks keep working.

Employee profiles set their active section to `employee`, not `people`. This
keeps the main Staffing navigation and Staffing subtabs visible and highlights
Employee. The profile's existing employee picker, date controls, production,
attendance, forklift, and trophy content remain unchanged.

### Acknowledgement history

Add an **Acknowledgement history** section after the employee summary cards and
before trophies and detailed performance. It lists all kiosk notifications for
the selected employee, newest first, not only anniversary reminders.

Each row shows:

- notice type;
- when the notice was first displayed;
- privacy-safe details, including the anniversary date and snapshotted PTO
  balance for an anniversary reminder;
- acknowledgement state and timestamp.

Existing notifications that predate display tracking show **Not recorded** for
the display time. An unacknowledged row shows **Waiting for acknowledgement**.
If the employee has no notification records, the section says so plainly.

The history is loaded from a small, uncached employee-scoped partial endpoint so
the existing expensive player-card response cache can remain unchanged. The
profile includes the section at
`/staffing/people/<name>#acknowledgement-history`; the endpoint that fills it is
an implementation detail and is not added to navigation.

## Chosen architecture

Use a durable anniversary reminder in the existing `employee_notifications`
queue. This reuses the proven time-clock gate, secure employee session, private
rendering, acknowledgement write, and notification kill switch while keeping
the new anniversary calculations in a focused module.

A new background reconciliation runs every six hours. It finds only employees
inside the 30-day window, refreshes their Odoo balances in one batched request,
and reconciles one reminder per employee and anniversary date. It never calls
Odoo from the time-clock sign-in path.

Calculating the reminder at sign-in was rejected because it would put Odoo and
date calculations on the punch path and would not create a dependable audit
record. Reusing `employee_celebrations` was rejected because those records are
same-day personal celebrations, while this is a 30-day PTO notice with a
required acknowledgement.

## Dates and balance rules

Work anniversaries use the same observed-date rule as existing employee
celebrations: February 29 is observed on February 28 in non-leap years. The
window includes both 30 days before the anniversary and the anniversary itself,
so a temporary outage does not permanently miss an employee.

The reminder uses the same exact-name and ambiguity safeguards as the past-
absence PTO workflow, but accepts the source type's day, half-day, or hour unit
because the reminder does not spend the balance. No partial or guessed name
match is allowed. If the type is missing or ambiguous, the run safely does
nothing.

The source balance is `available_practical`, which subtracts pending requests
and therefore represents the amount the employee can still safely plan to use.
Zero and negative balances do not qualify. The amount and unit are snapshotted
onto the notification.

Until the reminder is first displayed, reconciliation may update its balance
snapshot or remove it when the employee no longer qualifies. Once displayed,
the snapshot is frozen so the history always describes the notice the employee
actually saw. Each anniversary date can create at most one reminder, including
across retries and process restarts.

## Data model

Extend `employee_notifications` with nullable fields used by this and future
audited notices:

- `anniversary_date DATE`;
- `balance_amount NUMERIC(8,2)`;
- `balance_unit TEXT`, restricted to `days` or `hours` when present;
- `presented_at TIMESTAMPTZ`.

Add a partial unique index for
`(person_odoo_id, anniversary_date, kind)` when `anniversary_date` is present.
The anniversary reminder kind is `anniversary_pto_reminder`. Existing time-off
and optional-workday notification rows remain valid with null new fields.

The notification title and body remain stored snapshots. Rendering uses the
structured anniversary and balance columns so bilingual text stays consistent
with the existing notification templates.

## Reconciliation flow

1. The six-hour app warmer invokes the anniversary PTO reminder reconciler off
   the async event loop.
2. The reconciler computes upcoming observed anniversaries from active local
   people rows.
3. It resolves the exact local Paid Time Off type.
4. It refreshes balances for the candidate employee IDs through one existing
   batched Odoo balance operation and persists the refreshed cache rows.
5. For each positive practical balance, it inserts the missing notification or
   updates an unpresented notification's snapshot.
6. It removes only unpresented anniversary reminders whose employee, date, or
   balance no longer qualifies. Presented and acknowledged history is retained.

Generation is idempotent. A retry after an uncertain database outcome resolves
against the unique anniversary identity instead of creating a duplicate.

## Presentation and acknowledgement flow

When the notification page loads, Plant Manager reads the signed-in employee's
unacknowledged rows, then records `presented_at` for those exact rows if it is
still null. The update is employee-scoped so a token can never mark another
employee's notice as displayed.

Posting **I acknowledge** uses the existing employee-scoped acknowledgement
route. It timestamps every notification in the visible stack and restarts the
sign-in priority flow. Repeating the request is harmless. A failed write does
not redirect to the punch dashboard.

The employee profile history performs a read only. It does not acknowledge,
edit, resend, or delete notifications.

## Failure handling

- An Odoo, leave-type, or balance refresh failure makes no reminder changes and
  retries on the next scheduled run.
- The reconciler never creates a notice from stale or uncertain balance data.
- A previously saved reminder remains locally available during an Odoo outage.
- Reminder generation failures never break the app warmer loop or kiosk.
- The punch path never makes an Odoo request.
- If acknowledgement history cannot load, the employee profile remains usable
  and the section shows **Acknowledgement history is unavailable right now.**
- Existing authorization and secure time-clock tokens remain authoritative.

## Testing

Automated coverage must prove:

- the inclusive 30-day boundary, anniversary day, year rollover, first-year
  exclusion, and February 29 observed-date behavior;
- active and inactive employee eligibility;
- exact and ambiguous Paid Time Off type handling;
- positive, zero, and negative practical balances in both days and hours;
- one batched refresh for all candidates and no Odoo call on sign-in;
- insert, retry deduplication, pre-display snapshot update, pre-display removal,
  and post-display snapshot preservation;
- employee-scoped first-presentation and acknowledgement timestamps;
- the punch dashboard remains gated until acknowledgement succeeds;
- bilingual anniversary reminder copy;
- all-notification history ordering, legacy missing display timestamps, empty
  history, and safe unavailable state;
- the Employee subtab, landing route, existing profile URLs and hyperlinks; and
- Staffing, rather than Performance, navigation on employee profiles.

Final verification includes focused tests, the full test suite, Ruff, template
loading, and `git diff --check` before the implementation is pushed.

## Non-goals

- No manager-wide reminder dashboard or new Odoo task is created.
- No email, text, or Slack reminder is sent.
- No PTO is requested, scheduled, paid, expired, or changed automatically.
- No employee can view another employee's reminder through the time clock.
- Existing employee profile URLs are not renamed or migrated.
