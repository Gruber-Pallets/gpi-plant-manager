# Forklift Scheduled Time-to-Claim Design

## Goal

Make the compact Forklift bay cell show the operational impact of the drivers
actually scheduled for the day. The current cell shows the suggested driver count
and the time-to-claim predicted for that suggested count. The new behavior should
keep the suggestion, but calculate the displayed time-to-claim from the current
scheduled driver count so staffing changes give immediate feedback.

## User Experience

In the Forklift bay cell:

- Keep the suggested driver line, for example `6 Suggested`.
- Replace `~ x.x Time-to-Claim` with `Predicted Time-to-Claim x.x`.
- Add real vertical spacing between the suggested line and predicted line.
- Remove the check, warning, and `!!` symbols.
- Keep color coding, but base the color on the predicted scheduled-day
  time-to-claim rather than only the raw driver-count gap.

If the system suggests 6 drivers and Dale schedules 4, the cell should still say
`6 Suggested`, but the predicted time should be calculated with 4 drivers. If the
result is high, the cell should turn red.

## Calculation

Reuse the existing calibrated queue model rather than adding a separate staffing
formula:

1. Build the existing demand forecast for the target day.
2. Select the same planned demand hour/percentile already used by the SLA
   recommender.
3. Read the existing historical average handle time and calibration factor.
4. Compute the recommendation as today: the smallest driver count whose
   predicted claim wait is at or below the configured target.
5. Compute a new scheduled-driver prediction by running the same queue model with
   the actual scheduled driver count.

The scheduled-driver prediction is:

`predicted_scheduled_claim_seconds = k * erlang_c_wait_seconds(scheduled, lambda_per_hr, mean_handle_seconds)`

Where:

- `scheduled` is the current number of scheduled forklift queue drivers already
  passed into `build_advisor`.
- `lambda_per_hr` is the forecasted planned demand hour used by the recommender.
- `mean_handle_seconds` is the existing recent mean handle time.
- `k` is the existing calibration multiplier.

When scheduled drivers are zero, invalid, or the queue is mathematically
unstable, the model should mark the prediction as overloaded/unbounded. The UI
should render that as a red state instead of pretending there is a numeric wait.

## Severity

Base the cell color on the scheduled-driver prediction:

- Green: predicted scheduled time-to-claim is at or below the configured target.
- Yellow: predicted scheduled time-to-claim is above target but no more than
  1.5x the target.
- Red: predicted scheduled time-to-claim is above 1.5x target, unavailable due
  to overload/instability, or the advisor cannot compute a safe scheduled
  prediction while a recommendation exists.

This makes the color answer the practical question: "How bad will today's
staffing feel?" rather than only "How many people short are we?"

## Data Model

Extend the existing advisor render model with focused fields:

- `predicted_scheduled_claim_seconds`: numeric seconds, or `None`.
- `scheduled_prediction_overloaded`: boolean.
- `scheduled_prediction_status`: `ok`, `warn`, or `danger`.

Keep existing fields such as `recommended`, `coverage`, `target_seconds`, and
`predicted_claim_seconds` for compatibility where they are still useful. The
template should use the new scheduled prediction fields for the compact bay cell.

## Error Handling

If demand history, hourly shape, handle time, or calibration is missing, preserve
the existing quiet degradation:

- Show the demand/recommendation only when available.
- Show `TTC building` or `TTC pending` when the model cannot produce a reliable
  prediction.
- Never raise an exception into the staffing page render path.

## Testing

Add focused tests for:

- `build_advisor` computes the recommendation with the suggested driver count and
  computes the displayed scheduled prediction with the scheduled driver count.
- A suggested count of 6 with only 4 scheduled produces a worse scheduled
  prediction and a red or warning status depending on the target ratio.
- The compact Forklift bay cell renders `Predicted Time-to-Claim x.x`, removes
  the symbols, preserves `N Suggested`, and applies the new status class.
- The overloaded/unstable scheduled case renders red without fabricating a
  numeric time.

## Out of Scope

- Changing the underlying Erlang-C queue model.
- Changing how the scheduled forklift driver count is derived.
- Changing the settings page recommender preview.
- Adding new database tables or historical metrics.
