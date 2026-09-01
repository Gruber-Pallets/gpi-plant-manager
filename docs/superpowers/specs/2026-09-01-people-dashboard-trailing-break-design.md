# People Dashboard Trailing Break Design

## Goal

End the People dashboard at the final working time when the schedule finishes with a break, and keep the final time labels from overlapping the Summary heading.

## Effective Dashboard Window

- Treat any configured break whose end time equals the shift end as an end-of-shift wind-down period, regardless of the break's name.
- For the People dashboard only, use the start of that trailing break as the effective end of the workday.
- Apply the effective endpoint consistently to attendance spans, production and forklift timelines, hover data, and the displayed time axis.
- Keep the configured shift and all calculations outside the People dashboard unchanged.
- If no break ends at shift end, keep the current shift endpoint.

## Break Display

- Do not draw the trailing end-of-shift break.
- Continue to draw every ordinary break that ends before the effective dashboard endpoint.
- Do not identify a trailing break by the word `Cleanup`; custom and renamed end-of-shift breaks follow the same rule.

## Axis Labels

- Keep regular hourly labels for a day longer than six hours and regular half-hour labels for a shorter day.
- Do not force an extra endpoint label when it is closer than one regular axis step to the preceding label.
- This leaves the final regular time label readable and prevents it from colliding with the Summary heading.

## Testing

- Verify a renamed break ending at shift end shortens the effective People dashboard window to that break's start.
- Verify the trailing break is absent while an ordinary mid-shift break remains.
- Verify attendance bars and dashboard geometry stop at the effective endpoint.
- Verify the axis does not add a crowded final endpoint label.
- Verify a schedule without a trailing break keeps the configured shift endpoint.

## Out of Scope

- Changing the plant's configured shift or break schedule.
- Changing other dashboards' treatment of cleanup or end-of-shift periods.
- Identifying trailing breaks by their display name.
