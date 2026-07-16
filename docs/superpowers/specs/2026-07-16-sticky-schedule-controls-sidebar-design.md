# Sticky Schedule Controls Sidebar Design

## Goal

Keep the schedule goal, live work-center balance, and schedule automation actions available while a supervisor scrolls the daily staffing table.

## Scope

The staffing page's existing right-side Notes area will become the persistent control context on desktop. The existing warning area remains in the main scheduler column.

## Layout

### Desktop

The right `day-context` sidebar is sticky within the viewport. It contains, in order:

1. The existing day notes and custom-hours banner.
2. The Schedule Goal mode buttons (optimized, normal, and training).
3. The existing live minimum-crew balance/action message, including its current green/red state.
4. Reset to defaults and Clear schedule actions.

The page's top action row retains the date and schedule-navigation controls, and groups the schedule state and delivery actions: Draft/Posted state, Print, Post to Slack, Publish/Re-publish, plus existing context-sensitive Edit/Discard Draft actions.

### Responsive behavior

At the existing mobile breakpoint, the layout becomes one column and the right sidebar returns to normal document flow (not sticky). All actions remain available without horizontal crowding.

## Behavior and accessibility

Existing JavaScript hooks, element IDs, form actions, disabled/loading states, confirmation behavior, ARIA labels, and live balance updates remain unchanged. Moving markup must not alter the auto-scheduler, reset, clear, print, Slack, or publishing behavior.

## Warnings

Rotation warnings remain in the main scheduler column below the top actions. They are intentionally excluded from the sticky sidebar.

## Verification

Add focused static template/CSS tests that verify the controls' new containment, the sticky desktop rule, and the non-sticky mobile override. Run the affected staffing rotation tests and the related full test suite after the implementation.
