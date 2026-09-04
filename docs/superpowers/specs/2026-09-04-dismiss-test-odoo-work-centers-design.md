# Dismiss Test Odoo Work Centers

## Goal

Let a manager dismiss an Exception Inbox item when its unknown Odoo work
center is clearly a system test. Keep real unknown work centers actionable so
they cannot be hidden instead of mapped.

## Eligibility

An `attendance_unmapped_location` item is dismissible only when it has at least
one raw Odoo work-center label and every raw label contains `test`, ignoring
letter case. For example, `Test Workcenter` and `Night TEST Cell` qualify.
An item with a blank label, a real label, or a mixture of test and non-test
labels does not qualify.

## Inbox Behavior

For an eligible item, the row shows a **Dismiss** button instead of the current
**Map this Odoo work center** link. Dismissing removes that specific attendance
issue from the open inbox and records a human `dismiss` event in the inbox
archive.

All ineligible unknown-work-center items keep the mapping link and have no
dismiss action.

## Server Behavior and Safety

The browser sends the stable attendance exception item key when the manager
clicks Dismiss. The server reloads the current attendance exception snapshot,
requires exactly one matching open item, and applies the same test-label rule.
It rejects missing, stale, ambiguous, or non-test items without changing any
records.

For an accepted dismissal, the server writes suppression rows for every Odoo
attendance ID carried by the item using the existing missing-work-center
resolution store. Attendance exception composition consults those suppressed
IDs so the dismissed item stays out of both the full inbox and its summary.
The dismissal does not modify Odoo attendance or work-center mappings.

The server records one audit event under the attendance exception's stable item
key, including the employee and category shown at dismissal time. This action
is not presented as undoable because one exception can cover multiple source
attendance records and the existing undo contract is for one legacy missing
work-center record.

## Error Handling

The row stays visible if validation, suppression, or request processing fails.
The page shows the returned plain-language error and permits a retry. Audit
logging remains best-effort after the suppression succeeds, matching existing
Exception Inbox behavior.

## Tests

Focused tests will prove that:

- test-label matching is case-insensitive and requires every label to match;
- eligible rows render Dismiss while real and mixed-label rows retain Map;
- the endpoint rejects stale, ambiguous, blank-label, and non-test items;
- a valid dismissal suppresses every attendance ID and writes the audit event;
- suppressed attendance issues disappear from the full snapshot and summary;
- the browser posts the item key, removes the row only after success, and shows
  an error without removing the row after failure.
