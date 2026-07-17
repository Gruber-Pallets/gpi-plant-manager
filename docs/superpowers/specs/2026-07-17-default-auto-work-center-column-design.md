# Default Auto Work Center Column — Design

## Goal

Make each default Auto work-center checkbox part of its corresponding Work
Centers & Goals table row instead of showing all checkboxes in a separate grid
above the table.

## Current behavior

The Settings page renders the **Default Auto Work Centers** control as a
standalone section before the work-center table. Its wrapping grid lets labels
flow across lines, disconnecting a checkbox from the work-center data it
controls.

## Chosen approach

Add an **Auto** column to the existing `wc-table`. For every `wc_rows` entry,
render that row's existing `default_auto_work_centers` checkbox in the column.
Keep the hidden presence field in the same settings form so the existing save
endpoint and field name continue to work unchanged.

Remove the standalone checkbox grid and its explanatory heading. Add a short
description to the table-level note so managers still understand that these
choices apply only to newly created staffing days.

This approach is preferred over keeping the grid and changing its CSS because
the checkbox belongs to an individual work-center record, and table-row
placement preserves that association at every viewport width.

## Data flow

The page continues to receive `default_auto_work_centers` from the Settings
route. The template continues to submit one or more
`default_auto_work_centers` form values plus
`default_auto_work_centers_present=1`. No route, persistence, or API change is
needed.

## Testing

Update the focused template regression test to assert that the table includes
the Auto column and that the checkbox is rendered inside the table row loop.
Run the Settings auto-work-centers tests and the relevant static-template
suite.

## Non-goals

- Do not change which work centers are selected by default.
- Do not change the Settings save behavior or the per-day Auto settings.
- Do not alter other work-center table columns or controls.
