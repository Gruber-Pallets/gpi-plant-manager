# People Production Meter Warning Group Design

## Goal

Make the People page manager strip shorter and easier to scan when several
production meters are unavailable. Replace separate production-meter warning
buttons with one compact group button in the primary manager row. The button
reads **Production Meters Unavailable** and shows the number of affected
meters in a badge.

## Approved interaction

- Group only warnings whose kind is `production_metric_unavailable`.
- Keep every other warning type as its own button.
- Place all warning buttons in the primary manager row with the totals, update
  time, and date controls.
- Remove the dedicated full-width warning band.
- Let the primary row wrap naturally when the viewport cannot hold every
  control. Do not add horizontal scrolling or shrink touch targets below the
  existing accessible size.
- The grouped button uses the visible label **Production Meters Unavailable**
  followed by a distinct numeric badge. Its accessible name includes the
  count.

## Grouped details

Clicking, tapping, or keyboard-activating the grouped warning opens the
existing anchored warning panel. The panel shows a short overview followed by
one entry for each affected meter. Each entry includes:

- work-center name;
- manager-safe reason the metric is unavailable;
- People page impact;
- latest check time and last successful update when known; and
- the actions allowed for that meter, such as checking again, opening its work
  center dashboard, or reviewing settings when configuration is the cause.

The individual warnings remain the source of truth. Grouping is presentation
logic and must not discard their keys, facts, reasons, timestamps, or action
capabilities. Meter entries use a deterministic order so live refreshes do not
rearrange the panel unexpectedly.

## Server and browser flow

The server converts the dashboard's warning sequence into display groups.
Multiple production-meter warnings become one grouped summary with a stable,
opaque key and count; one production-meter warning uses the same grouped label
and a badge of `1`. Other warning summaries pass through unchanged.

The warning-detail route applies the same grouping to freshly loaded dashboard
data before looking up the requested key. It renders the current grouped
details, so a refresh cannot expose stale members. If no member remains, the
route returns the existing cleared state. The browser continues to treat the
group as one warning trigger and can reuse its current open, close, focus,
positioning, polling, and Check again behavior.

## Layout and responsive behavior

The warning container becomes a normal flex child of the primary manager row
instead of a second grid row. The production group label stays on one line at
normal desktop widths, and the count badge remains visually separate from the
label. At smaller widths, the existing primary row wrapping moves complete
controls onto another line without clipping content or introducing a local
scrollbar.

The strip keeps its current padding, border, sticky behavior, and touch-target
sizes. Its common desktop case becomes one row tall because the former warning
band no longer reserves a second row.

## Accessibility

- The grouped control is a real button with `aria-expanded` and the existing
  panel relationship.
- The button's accessible name reports both the warning label and affected
  meter count.
- The visible badge is not the only way the count is communicated.
- The grouped panel retains its labelled region, keyboard focus behavior,
  Escape handling, outside-click handling, and focus return.
- Each meter entry has a heading so screen-reader users can navigate the list.

## Error handling

Detail-loading failures use the existing retry state and do not remove the
grouped warning. A Check again request refreshes all group members. The group
disappears only after refreshed dashboard data contains no unavailable
production meters.

## Validation

Automated coverage will verify:

- zero, one, and several production-meter warnings group correctly;
- unrelated warnings stay separate;
- the badge count and accessible name match the group membership;
- grouped details retain each meter's reason, facts, timestamps, and allowed
  actions;
- the detail route resolves the grouped key against fresh data;
- polling and Check again preserve or clear the grouped trigger correctly; and
- supported viewport geometry keeps the manager strip and its children
  contained without overlap or horizontal scrolling.

The deterministic People preview will include two unavailable production
meters and at least one unrelated warning so the compact top-row arrangement
can be checked visually.
