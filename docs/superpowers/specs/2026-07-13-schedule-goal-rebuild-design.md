# Schedule Goal Rebuild Design

## Purpose

Simplify the Staffing schedule-goal controls by removing the separate Reset
action. The three schedule-goal buttons become the only way to recalculate
automated staffing.

## Behavior

- Remove the `Reset auto assignments` control from the Staffing page.
- Clicking Optimized, Normal, or Training always requests a rebuild with that
  mode, including when the clicked button is already active.
- A rebuild replaces generated automated assignments according to the selected
  mode while preserving manual assignments and non-Recycled work centers.
- Existing loading, warning, autosave, and published-schedule protections stay
  in place.

## Implementation

- Delete the reset button markup from `templates/staffing.html`.
- Remove the reset-button lookup, disable/enable handling, and click listener
  from `static/staffing.js`.
- Retain the current mode-button click handler, which invokes the rebuild API on
  every click.
- Update the Staffing rotation UI contract test to assert that the removed
  control and JavaScript identifier are absent and that mode buttons still call
  `rebuild`.

## Verification

Run the focused rotation UI/API test file and the template/static contract
test. The regression verifies the page no longer exposes a separate reset
control and that selecting any schedule goal continues to rebuild with the
chosen configuration.
