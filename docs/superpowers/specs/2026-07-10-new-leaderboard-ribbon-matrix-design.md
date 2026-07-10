# New-Leaderboard Ribbon Matrix Design

## Goal

Make the New-Leaderboard TV easy to scan at a glance by changing Gold Ribbons
from a tall monthly list into a compact calendar matrix. All twelve calendar
months must remain visible across the screen at once, without scrolling, so
the space released below the leaderboard panels makes the three-family view
substantially taller.

## Layout

Gold Ribbons becomes a fixed 13-column matrix:

- The first column identifies the work center: Juniors, Woodpecker, or Hand
  Build.
- The remaining columns are January through December, in that exact
  left-to-right order.
- Each work center receives one row. A ribbon cell contains its winning name,
  qualifying date, and normalized amount, or a compact dash when no winner is
  available.
- The matrix never uses horizontal or vertical scrolling. It uses a fixed grid
  with small, responsive typography and padding; long names truncate safely.

On a three-family TV, the three leaderboard panels occupy the large top region
and the short three-row ribbon matrix occupies the lower region. This replaces
the current twelve-row ribbon area. The one-family layout retains its
side-by-side composition and applies the same calendar-matrix ordering inside
its ribbon panel.

## Rendering and Data

No production metric, eligibility, family selection, or Gold Ribbon winner
logic changes. The existing `data.ribbons` source remains the last twelve
monthly records. The template reorders those records by calendar month
(`Jan` through `Dec`) before emitting headers and cells. The payload must carry
the numeric month already supplied by each ribbon record; calendar order does
not depend on labels or locale.

The template first emits the month headers, then renders one work-center row at
a time. Each row looks up that family in every calendar-ordered monthly record.
The family label is rendered as a row header, never inferred from the winner.

## Responsive Rules

- TV targets keep every Jan–Dec column on screen with no scroll container.
- CSS uses `minmax(0, 1fr)`, restrained gaps, responsive type, and ellipsis to
  prevent a long winner name from forcing a wider column.
- At narrow browser widths, the calendar matrix still stays a 13-column grid;
  type and padding reduce rather than introducing scrolling or wrapping months
  into a second line.
- The normal dashboard follows the same matrix semantics, while retaining its
  document-height layout rather than TV viewport clipping.

## Verification

Tests will assert that:

- The template renders work centers as rows and months as column headers.
- Calendar headers are explicitly January through December in order.
- The matrix is family-driven and retains a no-winner state.
- CSS defines a non-scrolling 13-column calendar grid and gives the
  multi-family leaderboard region more height than the prior ribbon layout.
- Existing route and preview-fixture tests still render both one-family and
  three-family states.
