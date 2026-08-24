# Reason-Free Late and Absence Tracking Design

## Goal

Let supervisors record an absence or temporarily snooze a missing clock-in without supplying a reason. Automatically record every eligible employee who clocks in more than five minutes late, including the exact number of minutes late, so their employee page shows when and how often they were absent or late.

## Scope and Decisions

- The existing missing-clock-in alert remains active after its current 15-minute threshold.
- The automatic late-record threshold is strictly more than 5 minutes after the configured shift start.
- The late/absence flow continues to apply only to the existing eligible population: scheduled, hourly employees with a fixed schedule.
- A supervisor-recorded absence is authoritative for the day. It prevents an automatic late record even if a later punch arrives.
- A running-late snooze is transient operational state, not an attendance-history record.
- Existing historical `reason` values remain stored for safety, but new records do not require or show them.

## User Workflow

### Missing clock-in

When an eligible scheduled employee has no punch after the existing report threshold, the Late / Absence report shows the employee with two actions:

1. **Mark absent** writes the current-day absence immediately. It sends no reason, creates the existing local absence record, and follows the existing best-effort Odoo Time Off sync behavior.
2. **Running late — 60 min** creates a 60-minute snooze. The employee remains out of the actionable report until the snooze expires. If they still have no punch then, the report shows them again. If they punch in during the snooze, the snooze is cleared from the live report.

No reason select, free-text field, or reason validation appears in this workflow.

### Confirmed late clock-in

The existing background inbox warmer already refreshes the late-report payload independently of a person viewing the page. During each refresh it examines eligible scheduled employees' attendance status.

For each person whose status is `late`, whose recorded lateness is greater than five minutes, and who does not have a same-day manual absence, the system creates exactly one `late_arrivals` record for that employee and day. It records the exact `minutes_late` calculated from the punch. The write is idempotent: after the record exists, later refreshes neither duplicate it nor replace its original lateness.

The automatic record clears the formerly actionable late-with-reason row, because it now represents completed attendance history rather than work awaiting a supervisor explanation.

## Data Model

Add a nullable integer `minutes_late` column to `late_arrivals` through the app's existing idempotent schema bootstrap/migration pattern.

- Automatic late records set `minutes_late` to the exact positive number of minutes late and set `reason` to `NULL`.
- Absences continue using `manual_absences`; their `reason` remains nullable and is no longer populated by the report UI.
- Old late records without a value remain valid historical rows and render without a minutes value.

The existing unique key of employee plus day remains the sole deduplication rule.

## Employee Page

The Attendance section retains its current date-range behavior, day links, and count cards:

- **Days Absent** counts absence records in the selected range.
- **Days Late** counts late-arrival records in the selected range.
- The dated history table displays Date, Type, and Minutes Late.
- Absence rows show an em dash in Minutes Late.
- Automatically recorded late rows show their exact minutes, such as `17 min`.
- The editable Reason column and client-side reason-saving behavior are removed.

## Error Handling

- An absence remains saved locally even when the best-effort Odoo Time Off update cannot be completed, matching current behavior.
- A background refresh failure remains non-fatal and retains the current degraded-report behavior.
- The automatic late-record write is isolated per refresh cycle; a transient database failure does not create a false history record and may be retried by the next refresh.

## Testing

Automated coverage will prove:

1. the reason-free absence action sends no reason and succeeds;
2. the running-late action creates a 60-minute snooze and does not create history;
3. a punch more than five minutes late creates one record with exact minutes;
4. a five-minute-or-earlier punch does not create a late record;
5. a same-day absence prevents automatic late recording;
6. refresh retries do not duplicate or overwrite the original late record;
7. the employee page reports counts and dated history with minutes, not an editable reason field.

## Out of Scope

- Capturing or editing late/absence explanations.
- Changing the existing 15-minute missing-clock-in alert threshold.
- Recording lateness for unscheduled, salaried, flexible-schedule, inactive, or excluded people.
- Rewriting or deleting legacy reason data.
