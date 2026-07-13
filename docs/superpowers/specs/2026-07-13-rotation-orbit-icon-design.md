# Rotation Orbit Icon Design

## Purpose

Make the scheduling-preferences control in the People Matrix read as a
continuous rotation rather than a two-way swap.

## Scope

Replace the inline SVG inside `.rotation-open-btn` in
`src/zira_dashboard/templates/skills.html` with the selected **Orbit** symbol:

- a circular, clockwise path with an arrowhead;
- three filled dots placed around the path to represent connected assignments;
- the existing `currentColor` stroke and muted button color.

The icon retains the current `viewBox`, rounded stroke treatment, and compact
fit within the existing 32 px control.

## Unchanged behavior and accessibility

The button continues to open the same Scheduling Preferences dialog. Its
`data-person`, title, accessible label, dialog semantics, click handler, and
hover/focus styles remain unchanged.

## Verification

Run the focused Staffing rotation template test and inspect the rendered
template source to confirm the new SVG is present while the control's existing
attributes remain intact.
