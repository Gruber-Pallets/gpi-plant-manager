# Timeclock feedback access

**Date:** 2026-08-03

## Goal

Make the existing Bug / Feature request form available from every Timeclock
screen, including its screenshot paste and file-upload support. Keep the
desktop feedback experience unchanged.

## Success criteria

- Every page that extends `timeclock_base.html` shows a touch-sized Feedback
  button.
- The button remains available while HTMX swaps between Timeclock screens.
- Opening it shows the same Bug / Feature request form used by the desktop app.
- A person can describe the issue, paste a screenshot, upload an image or PDF,
  and submit it through the existing feedback endpoint.
- The form records the current Timeclock URL so the report says where it came
  from.
- The button and modal remain keyboard-accessible and do not cover the primary
  Timeclock controls.
- Desktop pages retain Send feedback and View Feedback without a behavior or
  appearance regression.

## Design

### Shared feedback component

Extract the feedback modal markup from `_footer.html` into
`_feedback.html`. Extract its styles and JavaScript into `feedback.css` and
`feedback.js`. The desktop footer will include that component, preserving the
current element IDs and behavior. This avoids maintaining a second kiosk-only
copy of screenshot handling and submission logic.

The shared component owns only feedback behavior: type selection, description,
attachment previews, pasted images, upload selection, submission, and the
submitter's feedback list. Desktop-only changelog and inbox behavior stays in
`footer.css` and `footer.js`.

### Timeclock trigger

`timeclock_base.html` will include one fixed, clearly labeled Feedback button
and the shared feedback component outside `#timeclock-screen`. Because HTMX
only replaces `#timeclock-screen`, navigation through the kiosk cannot remove
the button, modal, or event handlers.

The trigger opens the shared Send feedback modal directly. It will use a high
enough stacking layer to remain visible but sit below the modal. Its placement
will include viewport safe-area spacing and a compact touch target so it does
not interfere with the Timeclock's main actions.

### Data flow and failures

The existing form continues to POST multipart data to `/feedback`, including
`window.location.href` and any attachments. The server continues creating the
Odoo task and local feedback index row. Existing validation and user-facing
messages remain: an empty description is rejected, a server failure is shown
in the modal, and a successful submission closes after confirmation.

No new endpoint, database field, or authentication path is needed.

## Testing

- Add a focused static contract test proving the Timeclock base includes a
  persistent Feedback trigger outside the HTMX swap target.
- Prove the trigger is wired to the shared Send feedback modal.
- Prove the shared component retains Bug / Feature selection, screenshot paste,
  upload support, current-page capture, submission, and View Feedback behavior.
- Update desktop feedback contract tests to follow the extracted component.
- Run the focused feedback/Timeclock tests, the base-template guard, lint for
  touched Python tests, and the full test suite.

## Out of scope

- Automatic full-page screenshot capture.
- Changes to Odoo task creation, feedback status rules, or attachment limits.
- Showing What's New or desktop inbox alerts inside the Timeclock.
- Adding feedback controls to read-only TV displays.
