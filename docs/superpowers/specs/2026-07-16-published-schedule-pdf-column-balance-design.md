# Published Schedule PDF Column Balance — Design

**Date:** 2026-07-16  
**Status:** Approved

## Goal

Make the published-schedule PDF easier to scan by narrowing the Bay column,
tightening the horizontal spacing around Work Center and Department, and using
the reclaimed width to keep two scheduled names on one line whenever they fit.

## Scope

This is a print/PDF-only adjustment. The interactive scheduler retains its
current table widths and its one-name-per-line presentation.

## Design

`src/zira_dashboard/static/staffing-print.css` will override the screen table
rules only during browser printing and Playwright PDF generation:

- Set an explicit, narrower Bay-column width.
- Preserve enough width for Work Center names, but reduce the cell padding at
  the Work Center/Department boundary.
- Constrain the Department column to its short label content.
- Allocate the recovered table width to Scheduled.
- Replace the current print-only block display for assigned-name spans with
  inline flow and a small separation. Browser wrapping remains enabled, so a
  long pair stays readable instead of shrinking the type.

The existing 9pt print font, portrait letter page, row borders, badges, and
single-name layout remain unchanged. The notes column is not reduced.

## Verification

Add a focused static regression test that asserts the new print-only width,
padding, and inline-name rules. Run the focused test and the existing staffing
static tests. A generated PDF should show common pairs (for example, Adrian A.
and Porfirio C.) inline when their combined content fits and wrap longer pairs
without clipping.
