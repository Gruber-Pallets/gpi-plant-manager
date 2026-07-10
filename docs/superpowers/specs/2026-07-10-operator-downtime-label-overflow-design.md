# Operator Downtime Label Overflow Design

## Problem

The operator dashboard renders working time and downtime as adjacent green and
red flex segments. The downtime label lives inside the red segment. When that
segment is narrower than the label, the segment's `overflow: hidden` rule clips
the label at the green/red boundary, leaving only its trailing characters
visible.

## Desired behavior

Keep the downtime value right-aligned within the red segment. If the red segment
is too narrow for the full value, allow the label's left side to paint over the
adjacent green segment so every digit and the `m` suffix remain readable. The
label must remain inside the overall bar and retain its current typography.

## Design

Change only the operator downtime red segment's overflow behavior from hidden to
visible. The shared stacked track already allows visible overflow, and the red
segment is painted after the green segment, so excess label width will appear on
top of the green side of the boundary. Existing flex alignment continues to
anchor the value to the bar's right edge when it fits.

No template, percentage, route, or downtime-calculation changes are needed.
The green uptime label keeps its existing clipping behavior.

## Alternatives considered

1. Absolutely position the downtime label across the segment boundary. This
   offers more placement control but adds positioning rules that duplicate the
   existing flex alignment.
2. Place the downtime value outside the bar. This avoids overlap but changes the
   approved in-bar presentation and consumes additional widget space.

The segment-level overflow change is preferred because it directly expresses
the requested behavior with the smallest surface area.

## Testing

- Add a static CSS regression test that requires the operator downtime red
  segment to use visible overflow.
- Run the test before the production change and confirm it fails for the current
  `overflow: hidden` rule.
- After the CSS change, run the targeted test and the related operator-dashboard
  tests.
- Render a narrow red segment with a multi-character downtime value and verify
  in a browser that the complete value crosses onto green without being clipped.

## Scope

This change affects only the operator dashboard's `downtime-row` widget in
editor and TV views. Recycling/New value-stream dashboards and all downtime data
calculations are out of scope.
