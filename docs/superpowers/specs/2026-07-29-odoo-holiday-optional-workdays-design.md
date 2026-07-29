# Odoo Holiday Optional Workdays Design

**Date:** 2026-07-29
**Status:** Approved for implementation planning

## Summary

Plant Manager will pull company-wide Public Holidays from Odoo and treat each
holiday as a closed plant day, even when it falls on a normal Monday-Friday
workday. A manager may deliberately reopen that date as an optional workday by
using the scheduler's existing Saturday recruiting, volunteer scheduling,
custom-hours, and publishing workflow.

Odoo remains the source of truth for the holiday's name, dates, and payroll
treatment. Plant Manager's holiday integration is read-only. Publishing an
optional holiday schedule changes Plant Manager's operational behavior for
that date; it does not remove or modify the Odoo Public Holiday.

The existing Plant Scheduler layout will not be redesigned. Holiday dates reuse
the controls and visual states already used for Saturday work, with
holiday-aware labels.

## Goals

- Mirror whole-company Odoo Public Holidays into Plant Manager.
- Make a mirrored holiday closed by default, overriding the normal workweek.
- Prevent an ordinary saved or auto-seeded weekday schedule from accidentally
  reopening a holiday.
- Let managers recruit volunteers and publish optional holiday work through the
  existing scheduler UI.
- Default optional holiday hours to the configured Saturday hours while keeping
  the existing per-date custom-hours editor available.
- Make all workday-dependent behavior agree on whether the holiday is closed or
  explicitly worked.
- Preserve current Saturday and normal-weekday behavior.

## Non-goals

- Creating, editing, deleting, or classifying holidays in Odoo.
- Changing Odoo's paid/unpaid setup, Work Entry Type, payroll rules, or
  attendance processing.
- Creating employee time-off requests for a company holiday.
- Replacing or rearranging the Plant Scheduler UI.
- Supporting department- or work-schedule-specific Odoo holidays as plant-wide
  closures. Only Odoo holidays with no Working Hours scope apply to the whole
  plant.
- Making holiday work mandatory. It remains an optional volunteer workflow.

## Existing Behavior to Reuse

Plant Manager already:

- reads `resource.calendar.leaves` records with `resource_id = false` for
  calendar displays;
- has a configurable Saturday default shift and breaks;
- starts Saturday recruiting from enabled work centers and their minimum crew;
- collects employee accept, decline, partial-hours, and cancellation responses;
- keeps non-volunteers under Off;
- limits Saturday staffing to available volunteers plus explicit manager
  availability corrections;
- prevents publishing while recruiting is still open;
- publishes a Saturday schedule as the explicit signal that the otherwise
  non-working date is active; and
- uses published special-day hours for punches, dashboards, targets, reminders,
  and other operational calculations.

The implementation will extract the date-neutral parts of that behavior into an
optional-workday concept. Saturday remains one kind of optional workday; a
mirrored company holiday becomes the other.

## Architecture

### 1. Read-only Odoo holiday source

Add an Odoo client operation that reads all company-wide Public Holidays:

- model: `resource.calendar.leaves`;
- `resource_id = false`; and
- `calendar_id = false`, meaning the holiday is not limited to one Working
  Hours schedule.

The read returns the Odoo id, name, start datetime, and end datetime. Plant
Manager will not expose any create, write, or unlink operation for this model.

The existing `fetch_public_holidays(start, end)` facade remains a cached live
Odoo reader because the time-off repair job needs both company-wide and
Working-Hours-scoped calendar leaves. A separate full-list Odoo reader, limited
to `calendar_id = false`, feeds the new company-holiday mirror. Calendar and
scheduling renders switch to that local mirror, so an outage cannot make known
plant closures disappear without weakening the repair job's scoped-calendar
checks.

### 2. Persisted holiday mirror

Add a local `company_holidays` table with:

- `odoo_id` as the stable primary key;
- `name`;
- plant-local `date_from` and `date_to`;
- raw `odoo_date_from` and `odoo_date_to` text for diagnostics;
- `last_pulled_at`; and
- standard created/updated timestamps.

Add a singleton `company_holiday_sync_state` row with `last_success_at`,
`last_attempt_at`, and a bounded `last_error` message. This distinguishes a
valid empty holiday list from a new installation that has never synced.

The synchronizer performs a transactional replacement of the fetched set:

1. normalize the complete Odoo response;
2. upsert current rows;
3. delete mirror rows absent from the successful full Odoo response; and
4. update `last_success_at` and reload the in-process holiday-date cache only
   after the transaction commits.

An Odoo datetime is interpreted in Odoo's UTC representation and converted to
the plant timezone before its calendar date is derived. Multi-day records are
expanded in the in-process cache so lookup is a cheap date-to-holiday mapping.
If any returned row is structurally invalid, the whole refresh is rejected and
the last-known-good mirror is retained. This avoids deleting a known holiday
because one changed Odoo row could not be understood.

The mirror refreshes through an immediate background startup tick and every ten
minutes thereafter. A refresh failure records and logs the error and preserves
both the persisted and in-process last-known-good data.

Until a brand-new installation completes its first successful sync, existing
saved schedules and weekday operations continue rather than treating unknown
dates as closed. Automatic creation of new future drafts pauses, and the
scheduler uses its existing warning/banner area to say that the Odoo holiday
list has not synced yet. The warning clears after the first successful refresh.

### 3. Optional-workday classifier

Introduce one date-neutral classifier that returns:

- whether the date is a Saturday;
- whether it is a mirrored whole-company holiday;
- the display kind (`saturday` or `holiday`);
- the holiday name when present;
- whether recruiting exists and its lifecycle state; and
- whether an optional-workday schedule is explicitly published.

A holiday takes display precedence when a date is both a Saturday and an Odoo
holiday, while retaining the same Saturday default hours.

All scheduler, shift, and recruiting entry points use this classifier instead
of scattered `weekday() == 5` tests.

## Workday Resolution

The shared workday rule becomes:

1. If the date is a mirrored company holiday, it is closed unless both its
   optional-workday recruiting lifecycle and its Plant Manager schedule are
   published.
2. If the date is not a holiday but is a configured normal work weekday, it is
   a workday.
3. Otherwise, such as an ordinary Saturday, the existing escape hatch remains:
   a published Plant Manager schedule makes it a workday. Saturday recruiting
   still governs the normal UI and publication flow, but legacy published
   Saturday schedules do not become inactive merely because they predate the
   recruiting lifecycle.

Requiring explicit optional-workday publication for holidays is important. A
normal weekday draft or posted schedule may have been created before Odoo added
the holiday. A posted version remains in its existing posted snapshot for
audit, but it cannot silently reopen the plant. An unposted draft is retained
until recruiting starts but is ignored while the holiday is closed.

When a holiday is closed:

- `is_workday()` returns false;
- in-shift and elapsed-shift calculations return no active shift;
- production targets and progress remain inactive;
- auto-lunch and workday finalizers do not run for the date;
- lateness and required-attendance behavior remain inactive; and
- the scheduler's next-working-day chooser skips the date.

When optional holiday work is published, the same consumers resolve the
published date's hours and behave as they do for a published Saturday.

## Scheduler Behavior

### Existing layout

The `/staffing` page keeps its current structure. The feature only adds
holiday context and generalizes existing Saturday conditions:

- show the Odoo holiday name in the existing day/status area;
- use the current nonstandard-day styling;
- reuse the current Hours pill and editor;
- reuse work-center Auto toggles;
- reuse the Recruit button and response summary;
- reuse Off, Unassigned, and Time Off rails;
- reuse schedule-goal and Publish controls after recruiting closes; and
- reuse the existing custom-hours banner.

No new page, scheduler column, modal sequence, or navigation section is
introduced.

### Closed holiday

Opening a future holiday with no active recruiting shows:

- the holiday name and “Plant closed by default” context;
- Saturday default hours as the proposed optional-work hours;
- editable per-date custom hours;
- enabled work-center toggles and calculated recruiting demand;
- everyone under Off; and
- the existing Recruit action.

The page must not seed normal default assignments for this date. If a normal
draft already exists because the holiday arrived later, the closed-day view
does not make those people schedulable.

### Activating recruiting

Recruiting activation:

- validates that the date is currently an optional workday;
- uses enabled work centers and their configured minimum crew;
- uses per-date custom hours when present, otherwise the Saturday default;
- atomically clears ordinary draft assignments before recruiting begins;
- turns an existing posted schedule into a draft with the posted version
  preserved through the scheduler's existing snapshot behavior;
- persists the optional-workday kind and holiday name for audit-safe display;
  and
- leaves the date closed if any activation step fails.

The existing Saturday tables remain in place to avoid a risky data move.
`saturday_recruitments` receives additive `day_kind`, `event_name`, and nullable
`holiday_odoo_id` columns; existing rows are backfilled as
`day_kind = 'saturday'`. A holiday activation stores the current holiday's
Odoo id and name. A neutral optional-workday service owns eligibility,
deadlines, lifecycle rules, and display context, while existing
Saturday-facing Python entry points remain compatibility wrappers. Existing
Saturday responses and lifecycle history are not rewritten.

### Recruiting deadline

The deadline is the plant shift start on the previous normal plant workday.
The search:

- considers configured normal work weekdays;
- skips mirrored holidays, including consecutive holidays;
- does not count a separately published optional Saturday or holiday as the
  “normal” prior workday; and
- retains the existing bounded-search failure behavior when no prior normal
  workday can be found.

For Black Friday following Thanksgiving, recruiting therefore closes at the
Wednesday shift start.

### Volunteer-only staffing

Employees receive the same eligibility checks and response choices used for
Saturday work. User-facing copy changes by kind:

- `Saturday Work Available` / `Holiday Work Available — Black Friday`;
- `Saturday recruiting` / `Holiday recruiting`;
- `Saturday schedule` / `Holiday work schedule`; and
- `Saturday work reminder` / `Holiday work reminder`.

Committed employees and manager-corrected Unassigned employees are schedulable.
Everyone else stays Off. Existing full-day employee Time Off still excludes a
person. Partial availability remains visible and enforceable.

Recruiting must close before assignments can be published. Publishing reuses
the current Saturday validation and delivery behavior and then marks both the
recruiting lifecycle and the Plant Manager schedule published. Canceling
recruiting returns the holiday to closed.

## Odoo and Payroll Boundary

Plant Manager does not change the Odoo Public Holiday when the plant chooses to
work:

- the holiday remains visible in Odoo;
- its paid/unpaid or other Work Entry Type configuration remains Odoo-owned;
- attendance writes continue through the existing Odoo attendance path; and
- any Odoo payroll treatment of employees who work a holiday remains governed
  by the existing Odoo configuration.

The implementation must include a test that the holiday integration calls only
Odoo read operations.

## Cache and Invalidation

A successful holiday sync invalidates:

- the in-process holiday-date cache;
- staffing-page response caches for affected dates or the staffing cache
  generation when date-targeted invalidation is not available; and
- any next-working-day result that can contain changed dates.

Schedule and recruiting mutations continue to invalidate their existing
staffing and shift caches. Workday lookup must never perform an XML-RPC call in
the punch, progress, or production hot paths.

The time-off local-backfill path keeps its existing cached live Odoo range read
because it must distinguish a whole-company holiday from one scoped to an
employee's Working Hours. It does not use the plant-wide mirror for that
decision.

## Failure and Race Handling

- Odoo refresh failure keeps the last-known-good mirror.
- Invalid Odoo rows do not replace valid mirror data with an empty result.
- A full successful empty Odoo response is valid and clears the mirror.
- Recruiting activation locks the schedule and recruiting date so a concurrent
  save cannot preserve ordinary assignments as volunteer assignments.
- Any failed activation leaves the date closed.
- A holiday added after a normal schedule exists overrides that schedule until
  optional work is explicitly recruited and published.
- A holiday removed from Odoo returns to normal weekday classification after a
  successful sync. Existing optional-workday recruiting remains historical
  data and no longer controls the date as a holiday; the ordinary schedule then
  follows normal weekday rules.
- If an Odoo holiday name or range changes, the next successful sync updates
  future renders without rewriting recruiting history already captured at
  activation.

## Testing

### Holiday mirror

- Reads only unscoped, whole-company Odoo calendar leaves.
- Upserts additions and changes transactionally.
- Deletes rows absent from a successful full response.
- Preserves last-known-good rows after fetch or normalization failure.
- Converts UTC datetimes to correct plant-local dates.
- Expands single- and multi-day holidays.
- Refreshes the in-process lookup after commit.
- Keeps schedule-scoped holiday rows available to the time-off repair job
  without classifying them as plant-wide closures.

### Workday and hours

- A weekday holiday is closed despite the configured Monday-Friday workweek.
- An ordinary saved or posted weekday schedule does not reopen a holiday.
- A holiday becomes active only when recruiting and schedule publication agree.
- A worked holiday uses Saturday default hours without a per-date override.
- A per-date hours override wins for a worked holiday.
- A closed holiday produces no active shift, targets, or workday automation.
- The next-working-day chooser skips single and consecutive holidays.

### Scheduler and recruiting

- A new holiday draft has no normal default assignments.
- Holiday pages render the existing controls with holiday-aware labels.
- Recruiting can start on Saturdays and mirrored holidays but not ordinary
  closed dates.
- Recruiting demand uses enabled centers and their minimums.
- Deadline selection skips holidays.
- Only committed or manager-corrected available people can be assigned.
- Recruiting cancellation restores the closed state.
- Publishing activates Plant Manager operations without writing the Odoo
  holiday.

### Regression

- Existing Saturday recruiting, scheduling, reminders, and publication remain
  unchanged.
- A legacy published Saturday schedule remains an active workday even when it
  has no recruiting lifecycle row.
- Normal weekdays still seed and resolve schedules as before.
- Existing employee Time Off calendar behavior remains unchanged.
- Dashboards, punches, progress, auto-lunch, lateness, reminders, and
  finalization use the same shared workday decision.

## Acceptance Criteria

1. A whole-company holiday configured in Odoo appears in Plant Manager after
   sync without any Plant Manager holiday entry.
2. A normal weekday holiday starts closed and does not load the normal roster.
3. The scheduler layout remains the same and exposes the existing recruiting
   controls with holiday-aware copy.
4. A manager can select centers, recruit volunteers, customize the date's
   hours, schedule only available people, and publish.
5. Closed holiday dates have no required attendance or production behavior.
6. Published optional holiday work behaves operationally like published
   Saturday work.
7. Plant Manager performs no Odoo Public Holiday writes.
8. Existing Saturday and normal weekday workflows pass their regression tests.
