# Saturday PDF Scheduled Staff Only Design

## Goal

Make the Saturday scheduler's browser-print preview and Slack PDF show only
scheduled work-center assignments, not people who are Off or on Time Off.

## Scope

This affects only print media. The live Saturday scheduler continues to show
the Off and Time Off rails, including their availability-management controls.

## Design

Add print-only selectors in `src/zira_dashboard/static/staffing-print.css` to
hide `.section.saturday-off` and `.section.timeoff`. The existing print layout
already removes the Unassigned rail, so these rules leave the header, day
notes, and schedule table as the visible PDF content.

## Verification

Extend the static print stylesheet regression test to assert both sidebar
selectors are present and set to `display: none !important`, while the
schedule table remains assigned to the print layout.

## Constraints

- Do not change the on-screen scheduler.
- Apply the behavior only when printing or rendering the Slack PDF.
- Do not alter work-center assignment data or Saturday availability state.
