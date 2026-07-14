# Reset Clears Schedule Goal Design

## Goal

Make the Schedule Goal controls accurately show that Reset to defaults did not
run an automatic scheduling goal.

## Behavior

After the reset request succeeds, clear the active visual state from Optimized,
Normal, and Training. Set every goal button's `aria-pressed` value to `false`,
clear the goal-help text, and clear the client-side selected rotation-mode
value.

The change runs only after a successful server response. A failed reset keeps
the previous goal selection and help text, and clicking a goal afterward keeps
its existing automatic rebuild behavior.

## Implementation and test

Add a small client-side helper beside the existing goal-mode helpers in
`src/zira_dashboard/static/staffing.js`. Call it only from the successful
Reset to defaults branch. Add a static regression test that asserts reset
clears the buttons, accessibility state, help text, and stored mode while the
ordinary `setActiveMode` path remains intact.

## Review

The scope is limited to client presentation state; the reset API and scheduler
assignment behavior are unchanged.
