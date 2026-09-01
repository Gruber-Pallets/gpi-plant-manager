# Current Production Summary Design

## Goal

Replace the production row's `Centers` summary with the person's cumulative production against their cumulative adjusted goal at the dashboard's current rendered time.

## Display

- Label the fourth production summary value `Production`.
- Display the value as `produced/goal`, with both values rounded to the nearest whole number and no spaces around the slash. Example: `142/157`.
- Keep the existing `Goal`, `Uptime`, and `Downtime` summary values unchanged.
- This change applies only to metered production rows. Forklift and other non-metered summaries remain unchanged.

## Data and Availability

- Sum actual production and adjusted goal across every scoreable production interval for the person, including work-center transfers, through the dashboard's current rendered time.
- Use the same validated production metrics that calculate the row's existing goal percentage so the two summary values cannot disagree.
- If any production interval makes the row's production summary incomplete, display `N/A` instead of a partial or estimated ratio.
- Reject non-finite aggregate values and display `N/A`.

## Testing

- Verify a production row with multiple work centers shows cumulative rounded production and goal values.
- Verify unavailable or incomplete production data shows `Production: N/A`.
- Verify forklift and non-metered summaries are unchanged.
- Update the visual preview fixture and its expected production summary labels.

## Out of Scope

- Changing the precise hover card.
- Changing goal, uptime, or downtime calculations.
- Changing production scoring or transfer rules.
