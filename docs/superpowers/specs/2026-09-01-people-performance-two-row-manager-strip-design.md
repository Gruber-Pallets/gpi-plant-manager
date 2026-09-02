# People Performance Two-Row Manager Strip Design

## Goal

Stop the People dashboard's manager bar from overlapping or showing a
horizontal scrollbar when counts, warnings, freshness, and controls do not fit
on one line.

## Root cause

The current desktop bar is a fixed-height, single flex row. Counts, freshness,
and controls keep their natural width, while the warning region is allowed to
shrink and uses horizontal overflow. When the combined content is wider than
the page, the warning region collapses into a small scrolling area and crowds
the content beside it.

## Approved layout

The manager bar becomes two semantic rows with automatic height:

1. The top row contains counts on the left and the updated time, date, and
   Needs attention control on the right.
2. The bottom row contains source-warning pills across the full available
   width.

The top row may wrap its left or right group internally at narrow widths, but
counts and controls remain grouped in that top band. Warning pills wrap
naturally inside the bottom band. No manager-bar region uses horizontal
scrolling, so the bar never shows a horizontal scrollbar or requires a side
scroll gesture.

When no source warnings exist, the bottom row is not rendered and the bar
collapses to the top row only. The bar remains sticky during vertical scrolling.

## Structure

The live partial groups existing content without changing its meaning:

- `.pp-manager-primary` contains `.pp-counts` and a new
  `.pp-manager-actions` group.
- `.pp-manager-actions` contains the existing updated-time live region and
  `.pp-controls`.
- `.pp-source-warnings` remains conditional and follows the primary group as
  the full-width second row.

The date and attention controls keep their stable keys and automatic-submit
behavior. Live refresh continues to replace the whole manager strip and restore
focused control state.

## Responsive behavior

At desktop widths, the primary row stays on one line in ordinary use. Counts
remain left aligned and actions remain right aligned.

At tablet and phone widths:

- the primary row may wrap within its own band;
- counts, warnings, and controls wrap instead of scrolling;
- every warning remains fully readable;
- every control remains reachable without horizontal scrolling;
- the manager bar may grow vertically to fit its content; and
- the page itself must not gain horizontal overflow.

This request replaces the earlier fixed `2.75rem` desktop height and two-row
mobile height cap. Avoiding overlap and horizontal scrolling now takes priority
over keeping the bar to a fixed pixel height.

## Scope boundaries

This change does not alter counts, warning text, filters, polling, focus
restoration, schedule markers, section headers, person rows, performance
calculations, or sorting. It changes only manager-strip grouping and layout.

## Verification

Automated tests must verify:

- the primary and action groups render in the live partial;
- warnings render after the primary group and remain conditional;
- the strip uses two semantic rows when warnings exist and one when they do
  not;
- counts, warnings, and controls use wrapping with no horizontal overflow;
- no manager-strip child has a horizontal scrollbar;
- bounding boxes do not overlap at desktop, tablet, portrait-tablet, and phone
  widths;
- long warning text remains readable and controls remain reachable;
- the sticky bar stays inside the viewport; and
- live refresh still preserves date/attention focus and in-progress values.

Manual screenshot review should confirm the top row reads as one compact status
and control band, the second row reads as one full-width warning band, and no
scrollbar appears below either row.
