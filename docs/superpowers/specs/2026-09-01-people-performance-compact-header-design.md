# People Performance Compact Header Design

## Goal

Make the People dashboard faster to scan by removing repeated page identity,
compressing all day-level details into one thin banner, and showing only useful
schedule times above each timeline.

The People tab already tells the manager which dashboard is open. The page must
not repeat “People performance,” “Today,” or a separate heading block below that
tab.

## Approved direction

Use a compact **manager scan strip** above the green section bars. The strip is
sticky at the top of the page while the manager scrolls. It contains all
day-level status and controls in one row:

- working-now count;
- worked-earlier count;
- needs-attention count;
- source warnings, only when warnings exist;
- the last-updated time;
- the selected date;
- the Needs attention filter; and
- the Today shortcut when a different date is selected.

The date and Needs attention controls update the dashboard directly. The
separate Apply button is removed. On narrow screens, the strip wraps into no
more than two compact rows. Warnings use horizontal overflow within their part
of the strip when needed, while date and filter controls remain reachable.

## Page structure

The dashboard starts immediately below the existing Performance tabs:

1. one sticky manager scan strip;
2. the Metered production section;
3. the Tablet forklift section; and
4. the Other non-metered people section.

There is no separate page-title block, date-control block, count row, warning
row, or full hourly axis. Existing person rows and section order remain
unchanged.

## Manager scan strip

The strip is visually quiet and no taller than the existing 2.75rem controls on
desktop. Counts sit first because they describe the overall workforce. Warnings
follow them and retain their amber warning treatment so exceptions remain
noticeable. The updated time is subdued. Date and filter controls sit at the
trailing edge.

Warnings are conditional. Their absence leaves useful breathing room rather
than an empty warning region. Long or numerous warnings remain readable through
compact wrapping or horizontal overflow; they must not make controls
unreachable.

The strip remains pinned during vertical scrolling. Its sticky offset must
respect the app navigation already above it, and it must not cover section
content or focused controls.

## Green section headers

Each green section header keeps the section name and row count in the identity
column. The timeline column replaces the current hourly axis with only these
schedule markers:

- shift start;
- the start of every configured break; and
- shift end.

The summary-column label remains aligned over the row summaries. No intermediate
hour labels are shown.

Markers come from the schedule resolved for the selected date, not from fixed
times. This keeps regular weekdays, Saturdays, holidays, and published custom
hours accurate. Each time is positioned against the same shift window used by
the timelines below it.

The default weekday would therefore show `7:00 AM`, `9:00`, `11:00`, `1:30`,
`3:15`, and `3:30 PM`. When markers are close together, their labels are grouped
or staggered without dropping either time. The first and last labels stay inside
the timeline boundary. Break names remain available to assistive technology;
the visible header stays time-only.

Empty or unusual schedules must still render truthfully:

- a day with no breaks shows only shift start and shift end;
- duplicate marker times display once;
- an invalid or missing shift window uses the existing safe dashboard failure
  behavior instead of inventing times; and
- a day with many configured breaks preserves every start time through compact
  grouping or horizontal timeline overflow.

## Live refresh and interaction

The existing live refresh continues to update counts, warnings, updated time,
sections, and rows. It must preserve the selected date, attention filter,
horizontal scroll, keyboard focus, and any open interval detail as it does now.

Changing the date performs the same validated single-day request as the current
Apply action. Toggling Needs attention updates that request immediately. The
Today shortcut returns to the live unfiltered date while preserving the
existing meaning of that link.

The sticky strip uses normal form labels or accessible names even when visible
labels are shortened. Keyboard focus remains visible. Color is not the only way
warnings or selected controls are identified.

## Responsive behavior

Desktop is the primary layout and remains a single thin row in ordinary use.
At narrower widths:

- controls remain reachable;
- the strip wraps into no more than two compact rows;
- warning text uses horizontal overflow rather than being clipped;
- the section header keeps the same three-column alignment as its person rows;
- schedule markers share the timeline's existing horizontal scrolling; and
- the compact header must not restore the removed large title or hourly axis.

## Scope boundaries

This change does not alter performance calculations, attention rules, section
ordering, person-row content, timeline intervals, break shading, summaries, or
data-source behavior. It is a header and schedule-label simplification only.

## Verification

Automated coverage should verify:

- the repeated page title and standalone hourly axis are absent;
- all counts, conditional warnings, freshness text, date control, attention
  filter, and Today behavior appear in the manager strip;
- no Apply button remains and date/filter changes submit the expected request;
- each green section header receives shift start, every configured break start,
  and shift end from the selected day's schedule;
- close, duplicate, no-break, Saturday, and custom-day marker cases are safe;
- live refresh preserves current interaction state;
- the manager strip is sticky without hiding focused or section content; and
- desktop and narrow viewport screenshots remain compact, aligned, and legible.

Manual review should compare the page with the approved compact mockup and
confirm that a manager can identify staffing, exceptions, selected day, filter
state, and meaningful timeline boundaries without scanning multiple header
rows.
