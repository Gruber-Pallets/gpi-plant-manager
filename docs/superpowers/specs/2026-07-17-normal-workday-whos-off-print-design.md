# Normal-workday “Who’s Off” print section

## Goal

Make the printed Plant Scheduler sheet show the people who have full-day time off on normal workdays.

## Scope

- Apply only to non-Saturday scheduler sheets.
- Reuse the scheduler’s already loaded time-off entries.
- Include only people with full-day time off.
- Exclude partial-day time off.
- Leave Saturday output unchanged.

## Design

The print/PDF layout will render a full-width `Who’s Off` footer immediately after the schedule table. It will contain a comma-separated list of full-day-off names, in the existing time-off data’s display order.

When no one is fully off, the footer will not render. The screen scheduler layout is unchanged; this is a print-only presentation change.

## Data flow

`staffing_view.build_staffing_bays` already builds the time-off display model from the fetched Odoo time-off entries. The print footer will derive its names from that model, filtering to entries marked full-day, so it requires no additional service or database reads.

## Error handling

If time-off retrieval fails, the existing empty time-off model is used and the footer is omitted. This preserves the scheduler’s current best-effort rendering behavior.

## Testing

Add a regression test that verifies the print template and its supporting view context provide a full-day-only footer for non-Saturdays, exclude partial-day entries, and preserve the Saturday exclusion.
