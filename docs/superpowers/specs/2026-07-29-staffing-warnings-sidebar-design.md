# Staffing Warnings Sidebar Design

**Date:** 2026-07-29
**Status:** Approved for implementation planning

## Summary

Move the existing Plant Scheduler warning panel out of the center schedule
panel and into the right sidebar. The right-sidebar source order becomes:

1. Notes for the day
2. the floating schedule Automater
3. schedule warnings

The change is presentational only. Warning generation, live validation,
structured issue details, accessibility behavior, and schedule actions remain
unchanged.

## Goals

- Keep the schedule table clear when many warnings are present.
- Put warnings beside the related daily notes and schedule automation controls.
- Preserve live warning updates after assignments or Auto settings change.
- Keep long warning lists usable within the narrow sidebar.
- Preserve the existing responsive single-column layout.

## Non-goals

- Changing which schedule conditions produce warnings.
- Changing warning text, severity, ordering, or deduplication.
- Changing the Notes or Automater controls.
- Adding a second warning view or a new dismissal workflow.
- Redesigning the scheduler's three-column layout.

## Layout

The existing `rotation-warnings` element moves from the top of the center
`main.panel` into `aside.day-context`, after the conditional
`rotation-controls` block. The warning element keeps its existing IDs and
`role="alert"` so the current client-side renderer continues to update the
same DOM node and assistive technology continues to announce changes.

The Automater retains its existing fixed desktop position and appearance. Its
markup remains before the warning panel in the sidebar. The warning panel uses
the sidebar's normal document flow below Notes, with sidebar-specific spacing
and width rules.

Because a typical unsafe schedule can produce dozens of messages, the warning
list receives a bounded vertical scrolling area on desktop. Text wraps within
the sidebar width, structured “Why?” details stay available, and the warning
panel does not widen the page or cover the schedule table.

## Responsive Behavior

At the existing `1100px` breakpoint, the right sidebar continues to become the
third full-width row. The Automater returns to normal flow through its existing
responsive rule. Notes, Automater, and warnings therefore appear in their
source order without fixed positioning or overlap.

The warning panel uses the available row width at this breakpoint. Its long
list remains bounded and scrollable so a large warning set does not make the
entire page excessively tall.

## Behavior and Data Flow

No server or API contract changes are needed. The route still provides
`rotation_warnings` and `rotation_issues`, Jinja still renders the initial
messages, and `renderCoverageIssues` still replaces the list after live
validation.

The existing empty-state behavior remains: the panel is hidden when there are
no warnings or structured issues and becomes visible again when live
validation adds one.

## Testing

Update the existing template placement test to require the warning element
inside `aside.day-context`, after both `day-notes` and `rotation-controls`, and
to reject the warning element inside the center `main.panel`.

Update the sidebar CSS contract test to cover the warning panel's sidebar
spacing, bounded overflow, and responsive behavior. Keep the existing live
warning renderer tests unchanged to prove that moving the node did not alter
warning updates, safe text rendering, deduplication, or visibility.

Run the focused staffing template and rotation tests, then the full test suite
and static checks before shipping.
