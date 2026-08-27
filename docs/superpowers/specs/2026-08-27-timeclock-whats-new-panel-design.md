# Timeclock What's New panel

**Date:** 2026-08-27
**Status:** Approved design, pending implementation plan

## Summary

The timeclock will replace its bottom Feedback bar with the same top-right
light-bulb control and full **What's new** panel used by the main GPI Plant
Manager app. Tapping the bulb opens the shared panel, where people can review
updates, mark them read, send feedback, or view feedback.

## Goals

- Use the same outline light-bulb icon, button styling, unread dot, and
  What's New panel as the main app.
- Place the control in the upper-right of every timeclock screen.
- Keep Send feedback and View Feedback available from the shared panel.
- Keep the bulb working after HTMX timeclock navigation.
- Remove the old bottom Feedback bar so it no longer takes screen space.

## Non-goals

- Changing the feedback form, feedback storage, changelog content, or
  read-state behavior.
- Adding a timeclock-specific copy of the What's New panel or its scripts.
- Changing the timeclock's existing calendar, brand, or punch controls.

## Chosen approach

The timeclock base template will render the existing shared footer component,
which already owns the What's New modal, the feedback controls, styles, and
scripts. The footer trigger will recognize a timeclock header as a valid host
alongside the main app header.

On the home screen, the bulb will join the existing upper-right header action
group. On all other timeclock screens, it will be added to the right edge of
the header beside the existing brand area. This keeps the control in the
expected top-right location while respecting the different header layouts.

The trigger code will run again after an HTMX swap. The panel itself remains
outside the swapped timeclock content, so an open panel and its feedback
dialogs are not removed by navigation. The current feedback open/close events
will continue to pause the kiosk idle redirect while a feedback dialog is open.

## User experience and accessibility

- The control is an accessible button labeled **What's new** and uses the
  shared 44px outline light-bulb icon.
- Its unread dot continues to reflect the same local read state as the main
  app.
- Opening it shows the same panel heading, update cards, Mark all read,
  Send feedback, View Feedback, Close button, and Escape/backdrop behavior as
  the main app.
- The old bottom Feedback button and reserved bar space are removed.

## Testing

- Static template coverage proves the timeclock uses the shared footer rather
  than its separate feedback bar.
- Static JavaScript coverage proves the shared trigger supports timeclock
  headers and reattaches after HTMX content swaps.
- Existing panel tests continue to prove the bulb icon, read state, and
  feedback actions use the shared implementation.
- The timeclock idle-redirect test continues to cover pausing while feedback
  is open.
