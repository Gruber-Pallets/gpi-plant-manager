# Compact Worker Stint Bars Design

## Goal

Make work-center production bars easy to scan again. Keep every worker stint
visible in one normal-size bar, keep the finish-line markers, and move the
person, time, production, goal, and ahead-or-behind details out of the row and
into details that appear only when someone asks for them.

## Approved behavior

### One compact bar

- Keep the horizontal production track at its normal 20-pixel height.
- Keep every worker stint in its correct position and width inside that one
  track.
- Add a subtle divider where one worker stint ends and the next begins so two
  neighboring stints remain distinguishable even when they use the same color.
- Keep completed checkered finish lines, live solid goal lines, red and green
  fills, and hatched shortfalls. Their scoring and geometry do not change.
- Keep the work-center or sole-producer label on the left and the station total
  on the right.
- Remove the detail text currently squeezed inside the bar and the detail rows
  currently shown below it.

The compact rule also applies to vertical orientation: keep the existing
stacked stint geometry and finish markers, but remove the always-visible worker
detail list below each column.

### Details on demand

Each stint is one interactive section. Hovering it, focusing it with a
keyboard, or tapping it opens a small popover with:

- the worker name, or **Unassigned production**;
- the local start and end time;
- actual production and the stint goal; and
- the ahead, behind, or neutral result.

Only one popover may be open at a time. Moving away ends a hover preview.
Tapping another stint replaces the open details. Tapping elsewhere or pressing
Escape closes the popover. Keyboard users can move through stints and see the
same information on focus.

Screen, touch, and TV markup use the same compact presentation. A TV with no
pointer or touch input simply shows the compact bar, stint boundaries, and
finish lines without opening details.

## Considered approaches

### Recommended: shared compact popover

Render a focusable hit area over each existing stint runway and use one small,
shared popover controller for hover, focus, and tap. This keeps the row compact,
works with touch and keyboard input, and avoids duplicating a popover for every
stint.

### Native browser title only

This would require little code and would work for a mouse, but browser title
text is inconsistent, cannot be controlled, and does not reliably support
touch or keyboard use.

### Expand the row on selection

This would make the details easy to read, but selecting a stint would make the
row tall again and move nearby work centers. That conflicts with the requested
scan-friendly layout.

## Architecture

### Existing bar data stays authoritative

The department route and production-segment scorer continue to calculate stint
ownership, actual production, goals, results, active state, and percentages.
No scoring, attribution, totals, or scheduling behavior changes.

### Shared dashboard template

The shared department dashboard bar template will:

- keep rendering the existing fill, shortfall, and finish-marker geometry;
- stop rendering `worker-segment-name`, `worker-segment-labels`, and the
  vertical always-visible stint list;
- render one transparent, focusable stint hit area over each complete stint
  runway;
- give each hit area a concise accessible label containing the same details as
  the popover; and
- preserve a native title as a fail-soft fallback if the interaction script is
  unavailable.

The hit-area width is the stint's actual-fill percentage plus its shortfall
percentage. This covers the full independent runway whether the worker was
ahead or behind. Its starting percentage remains the existing stint start.

### Styling

Shared Recycling dashboard CSS will keep the track at its existing size, style
the hit areas without changing geometry, draw subtle stint boundaries, and
style the popover above or below the selected section according to available
space. The popover must not resize the grid row or cover the selected finish
line unnecessarily.

### Interaction controller

A small shared dashboard script will own one popover element. It reads details
from the selected stint's accessible label or data attributes, positions the
popover near that section, and handles hover, focus, tap, outside-tap, resize,
scroll, and Escape. Reusing one controller prevents duplicate open details and
keeps the rendered page small.

## Accessibility and fail-soft behavior

- Stint hit areas are reachable by keyboard and expose a meaningful accessible
  name without requiring the visual popover.
- Hover is never the only way to reach details.
- The popover does not steal focus when opened.
- Escape closes it and returns focus to the selected stint when appropriate.
- Result colors remain paired with words inside the details.
- If JavaScript fails, the production bar, stint boundaries, finish lines, and
  native title details remain available. Production rendering must not fail.

## Validation

Add focused tests proving that:

- segmented horizontal rows retain fill, shortfall, and finish markers;
- each stint renders one focusable hit area with complete detail text;
- names, times, actual-versus-goal values, and results are no longer rendered
  as always-visible bar or below-bar text;
- horizontal rows retain the existing normal track height and no longer gain a
  second detail row;
- vertical columns retain their segment and finish geometry without the visible
  worker detail list;
- sole-producer, multiple-producer, unassigned, active, completed, screen, and
  TV cases use the same compact rules;
- hover, focus, tap, outside-tap, and Escape open and close one shared popover;
  and
- unsegmented and multi-day bars remain unchanged.

Run the focused template and interaction tests, then the related dashboard test
suite and style checks. Visually check a narrow multi-stint row to confirm the
bar remains one normal-height line and the popover stays readable near widget
edges.

## Out of scope

- Changing production credit, goals, breaks, downtime, or ahead/behind scoring.
- Changing station totals or the work-center ordering.
- Changing the meaning or styling of completed and live finish lines.
- Adding an expandable history panel or permanently visible legend.
- Redesigning unsegmented bars or multi-day range bars.
