# Department Bar-Row Alignment

## Goal

Keep every horizontal department bar row easy to read when its dashboard
widget is narrow. The person and work-center label stays at the left, the
colored production bar stays in the middle, and the actual/goal total (for
example, `393 / 409`) stays on that same row at the right.

## Scope

This changes only the shared CSS for horizontal department bar widgets on the
Recycling and New department dashboards. It does not change production data,
goal calculations, saved widget settings, vertical-bar layouts, or the text
shown in a total.

## Design

Horizontal rows use three explicit grid columns:

1. A bounded label column that can shrink without pushing the rest of the row.
2. A flexible bar column that consumes the remaining available space.
3. A fixed-width total column for the actual/goal number.

The label remains a two-line person/work-center block. If the widget becomes
too narrow to show a full label, its existing ellipsis behavior applies. The
numeric total uses tabular figures and no wrapping, so it remains a single
right-aligned value beside the bar. The bar may shrink, but it does not grow
the row or force the total onto a new line.

The same grid rule applies to ordinary bars and segmented worker-history bars
that show the time-window label inside the bar. Vertical orientation remains
unchanged and keeps its existing column-oriented presentation.

## Verification

Add a focused static regression test that asserts the shared bar-row CSS keeps
the label, flexible bar, and non-wrapping numeric total in the horizontal grid.
Run that test red before the CSS change, then green after it. Finally run the
relevant dashboard static tests, syntax checks, and a whitespace diff check.

## Self-review

This is limited to presentation and has one unambiguous outcome: at narrow
sizes, the total remains beside its corresponding bar rather than appearing
below it. The design preserves existing user-selected orientation and number
placement semantics, and it does not include unrelated refactoring.
