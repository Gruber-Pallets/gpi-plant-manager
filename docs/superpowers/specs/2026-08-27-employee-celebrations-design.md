# Employee Birthday and Work-Anniversary Celebrations

**Task:** 3605 / GPI-PM-FB-39

**Status:** Approved design; awaiting written-spec review
**Source:** GPI Plant Manager feedback #39

## Goal

Privately celebrate an employee's birthday and completed work anniversary
when they use the Timeclock kiosk. The celebration appears on its event date,
or remains available until the employee next uses the kiosk. It must never
reveal another employee's personal dates.

## Approved employee experience

1. After an employee selects their name in Timeclock, the app checks for their
   due celebrations before opening the normal dashboard.
2. A due birthday shows a full-screen card: “Happy Birthday, [first name]!”
3. A due work anniversary shows a full-screen card: “Happy [N]-Year Work
   Anniversary, [first name]!”, starting with the first completed year and
   recurring every year.
4. The card uses light celebratory confetti, respects the device's
   reduced-motion preference, and has one large Continue button.
5. A celebration stays pending when the employee does not sign in on its date.
   It appears at their next kiosk use, even after several missed workdays.
6. If more than one celebration is pending, the kiosk presents one card at a
   time before continuing.
7. Existing time-off notices keep their current priority. After the employee
   acknowledges those required notices, the kiosk displays any due celebration
   before proceeding to the normal Timeclock flow.
8. English remains the default. Employees already configured for
   Spanish-primary kiosk screens receive the same stacked Spanish-first copy.

## Odoo and local data

- Odoo `hr.employee` is the source for the standard `birthday` and
  `first_contract_date` fields.
- The integration checks that those fields are available before using them.
  A field unavailable in this Odoo configuration, a blank value, or malformed
  value excludes that event without interrupting roster sync or kiosk use.
- The local `people` mirror stores only birthday month and day, not the birth
  year, to avoid retaining an employee's age. It stores the first-contract date
  because that date is required to calculate the anniversary number.
- Values refresh through the existing hourly Odoo roster-sync path. A failed
  Odoo read leaves the last known-safe local values in place and never blocks a
  kiosk sign-in.

## Durable celebration queue

Use a new feature-owned `employee_celebrations` table rather than extending
`employee_notifications`. Time-off notices have independent acknowledgement
rules, and a celebration must never clear or be cleared by a required time-off
message.

Each row represents one event for one employee:

| Field | Purpose |
| --- | --- |
| `person_odoo_id` | Connects the event to the signed-in employee. |
| `kind` | `birthday` or `work_anniversary`. |
| `event_day` | The plant-local calendar date on which the event occurs. |
| `completed_years` | Anniversary number; empty for birthdays. |
| `acknowledged_at` | Records the single successful Continue action. |

The database enforces one row per employee, kind, and event date. The roster
sync creates events only for today through the next 370 calendar days. This
deliberately avoids showing a whole year's worth of old birthdays when the
feature first ships. Once queued, a row persists until the employee
acknowledges it, so an absence cannot lose the celebration. An Odoo date change
can update or remove only a future, unacknowledged event; it never rewrites
history already due or shown.

At kiosk sign-in, a small indexed local query finds that employee's
unacknowledged events whose event date is today or earlier. A successful
Continue action acknowledges only the displayed celebration, atomically, so
refreshes, retries, or two tabs cannot repeat the same event.

For a February 29 birthday or first-contract date in a non-leap year, use
February 28 as the celebration date.

## Boundaries and safeguards

- This feature is private to the celebrating employee. It adds no staffing,
  manager-dashboard, TV, coworker, email, or Slack announcement.
- The celebration never displays the employee's birthday date or contract date.
- It never calls Odoo on the kiosk request path.
- If no valid celebration data is available, Timeclock behaves exactly as it
  does today.
- The feature does not change time-clock punches, attendance, pay, time-off,
  schedules, or existing notification acknowledgements.

## Verification

Automated tests will cover:

- Odoo field discovery, valid values, blank values, malformed values, and an
  unavailable field without breaking the normal roster sync.
- Birthday and anniversary-date calculation, completed-year counts, leap-day
  behavior, and active-employee filtering.
- Future event creation, queue deduplication, no historical backlog at feature
  launch, and safe handling of a future Odoo date correction.
- Showing an event on its date, retaining it through missed days, showing it
  exactly once after return, and supporting multiple pending events.
- Route order with existing time-off notices, token validation, atomic
  acknowledgement, and unchanged kiosk routing when no event is due.
- English and Spanish-primary rendering plus reduced-motion styling.

## Acceptance criteria

- A valid active employee sees only their own birthday celebration on the
  birthday, or their first later kiosk use if absent.
- A valid active employee sees only their own completed-year anniversary
  celebration on the anniversary, or their first later kiosk use if absent.
- The same event cannot appear twice after it is continued.
- No employee receives a celebration for another employee or a date that is
  missing or invalid in Odoo.
- An Odoo outage, field mismatch, or local queue error does not prevent normal
  kiosk use.
