# Published PDF Empty-Notes Layout — Design

**Date:** 2026-07-16  
**Status:** Approved

## Goal

Improve the published schedule PDF’s lower table rows: replace the clipped
Transportation Bay label with Driving, move scheduled people closer to their
work center, widen Notes, and let names use the Notes area only when that row
has no note.

## Scope

The change applies only to browser print and the published Slack PDF. It does
not rename the Transportation department or Truck Driver work center, so
department-based scheduling and rounding remain unchanged.

## Design

- Change the Truck Driver location’s Bay label from `Transportation` to
  `Driving`. The label fits the existing narrow printed Bay column.
- Rebalance only the print table: Work Center 28%, Department 12%, Scheduled
  35%, and Notes 20.5% (with the existing 4.5rem Bay column). This moves the
  Scheduled column left and gives Notes more normal-row capacity.
- Keep ordinary print rows as table cells. For a row whose print-only note div
  is empty, use Chromium’s supported `:has()` selector to keep the scheduled
  summary unwrapped and visibly overflowing into the otherwise-empty Notes
  cell. The Notes cell is not hidden, and rows with notes retain normal name
  wrapping, so no text overlaps a real note.

## Verification

Add static tests for the Bay label, updated print widths, and empty-note spill
rule. Run focused staffing department/static tests and inspect the printed
stylesheet’s selector specificity against screen rules.
