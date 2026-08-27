# Staffing Hours Review — Design

**Date:** 2026-08-27
**Status:** Approved design, pending implementation plan

## Problem

Supervisors need one place in Staffing to review employees' hours across a
useful time range. The prior system provided weekly totals. Plant Manager must
support both the time a person actually clocked and the hours in payroll,
without requiring supervisors to assemble dates or totals by hand.

## Scope

Add an authenticated **Hours** tab to the Staffing sub-navigation. It is a
read-only report: it never creates, changes, approves, or deletes attendance,
payroll, or payslip records.

The report offers two sources:

- **Clocked time:** actual Odoo `hr.attendance` intervals. A completed
  interval contributes its elapsed time. An open interval contributes elapsed
  time through the current plant-local time and is marked *clocked in*.
- **Payroll hours:** active Odoo `hr.work.entry` records, grouped into regular
  (`WORK100`) and overtime (`OVERTIME`) hours. The table displays both the
  breakdown and the combined total.

The active source is always labeled, so a clocked-time total is never mistaken
for a payroll total.

## User experience

The toolbar contains a source switch, a date-range picker, employee search,
and department filter. All choices are URL query parameters, so reloads,
bookmarks, and shared links retain the exact report view.

Date shortcuts are:

`This week`, `Last week`, `This pay period`, `Last pay period`, `This month`,
`Last month`, and `Custom`.

Weeks run Monday through Sunday in the plant time zone. Months use their full
calendar bounds. Custom dates are inclusive and must have a start date on or
before the end date.

The table lists the filtered employees, their total, and an expandable daily
breakdown. Expanding a person reveals the source records behind each day. The
page has summary filters for people approaching 40 hours, over 40 hours, and
people needing attention because of an open or conflicting record. A team
total summarizes the currently filtered result set.

## Pay-period resolution

Plant Manager has a 14-day pay-period anchor of **2026-08-16**. The anchor and
cycle length live in an admin app setting rather than code, so a future payroll
schedule change is a configuration update. For example, the anchor makes the
current period on 2026-08-27 run from 2026-08-16 through 2026-08-29.

The report uses a hybrid resolver:

1. Calculate the selected current or prior period from the configured anchor.
2. Read Odoo `hr.payslip.run` payroll-batch date ranges, when the API user has
   access and batches are available.
3. Use a matching Odoo range as verification. If the relevant Odoo range is
   different, use the Odoo range and show a visible mismatch notice with both
   ranges. Multiple incompatible Odoo ranges are an error, not a guess.
4. If the batch model is unavailable, inaccessible, or has no relevant batch,
   use the configured anchor and disclose that Odoo has not verified it.

This gives supervisors a reliable report before payroll has created a batch,
while still following the actual payroll range when Odoo supplies one.

## Architecture

Keep the feature in small, focused parts:

- A **pay-period resolver** calculates named ranges and performs the optional
  Odoo batch verification. It owns the anchor-setting validation and exposes a
  normalized range plus verification status.
- An **hours service** retrieves source records and produces a source-neutral
  employee/day/total report. It owns interval overlap, open-attendance elapsed
  time, work-entry type classification, exception flags, and aggregation.
- A **Staffing Hours route and template** own query validation, existing
  Staffing authorization, rendering, and the expandable report UI. The route
  issues no mutations.
- Narrow additions to the Odoo client expose only the required read calls for
  attendance intervals, work entries, employee departments, and payroll
  batches.

The service returns explicit complete-or-failed results. The template does not
try to calculate hours itself.

## Data flow and failure behavior

1. The route resolves the date range and filters.
2. The pay-period resolver optionally verifies pay-period shortcuts against
   Odoo; the hours service fetches the needed records for the selected dates.
3. The service normalizes the records to plant-local days, aggregates them,
   attaches exceptions, and returns the report model.
4. The route renders the table, summaries, source label, and any verification
   notice.

A payroll-batch lookup failure does not block the report: the deterministic
anchor remains usable and the page says that it could not verify with Odoo. An
attendance, work-entry, or employee lookup failure shows a clear report error
instead of showing a partial list as complete. Invalid custom dates produce an
inline correction. Every time conversion uses the existing plant time zone,
including daylight-saving transitions.

## Testing

Tests cover:

- every named date range, custom-range validation, the 2026-08-16 anchor, and
  a future configured anchor;
- matching Odoo batch verification, an Odoo override with notice, unavailable
  batches, and incompatible batches;
- completed and open attendance intervals, midnight/DST boundaries, and daily
  totals;
- regular and overtime work-entry aggregation, employee and department
  filtering, 40-hour filters, and exception flags;
- route authorization, query-string persistence, error rendering, and
  expandable employee details.

## Out of scope

- Editing attendance, payroll work entries, or Odoo payslip batches.
- Separate report pages for exceptions or overtime; they remain filters in the
  Hours tab.
- Historical work-center allocation reporting, scheduled-versus-actual labor
  analysis, and exports. These can be added later without changing the report
  model.
