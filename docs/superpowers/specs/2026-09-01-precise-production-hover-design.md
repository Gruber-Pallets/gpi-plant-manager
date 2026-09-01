# Precise Production Hover Design

## Goal

Let a manager point at any moment on a metered production bar and read that person's cumulative production, cumulative adjusted goal, and rolling uptime through that exact minute.

## Scope

This change applies only to metered production intervals on the People performance page. Forklift intervals keep their call and on-time details. Non-metered and unavailable-location intervals keep their existing location details. The row summaries, scoring, section ordering, attention ordering, and bar colors do not change.

## Hover Card

The production hover card contains only three lines:

```text
10:37 AM
Production: 142.0 / 156.8
Uptime 84%
```

The first production number is the person's total credited units from the beginning of the displayed day through the hovered minute. The second number is the person's total adjusted goal over that same period. Both totals continue across transfers and do not restart when the cursor enters a later bar.

The goal uses the target of each metered work center during the time the person worked there. Planned breaks and non-metered intervals add no goal. The calculation must use the same productive-time and goal rules as the existing interval score so the hover card cannot disagree with the bar or day summary at the same endpoint.

`Uptime` is the existing rolling 30-minute uptime reading at the hovered moment, not cumulative day uptime. It uses the latest available five-minute rolling point at or before the selected minute.

Values display with one decimal place for production and goal and as a whole percentage for uptime.

## Interaction

Pointer movement across a production interval maps the horizontal cursor position to an exact minute within that interval. The displayed time follows that minute. A thin vertical marker inside the interval shows the selected position.

The browser must not call the server or an external API for each pointer movement. All trustworthy hover checkpoints arrive with the rendered row, and the browser selects from them locally.

The existing hover, focus, tap-to-pin, outside-click, Escape, viewport clamping, and live-refresh behavior remains. Keyboard focus and tablet tap have no pointer coordinate, so they select the interval end for a closed interval and the current rendered time for an open interval. The separate short-move target uses the same fallback.

When live rows refresh, a pinned interval is restored by its stable interval key. Its selected time should be restored when that minute still belongs to the refreshed interval; otherwise it clamps to the refreshed interval end.

## Data Model and Calculation

The production data path will expose timestamped credited-unit checkpoints instead of estimating progress by spreading an interval total evenly over time. This preserves the meaning of real production events.

For each person, the server builds an ordered cumulative hover series across all metered intervals in the displayed day. Every checkpoint contains:

- its UTC timestamp;
- cumulative credited units through that timestamp;
- cumulative adjusted goal through that timestamp; and
- the rolling uptime value for the interval containing that timestamp, when available.

The series must include interval boundaries and enough goal checkpoints to answer every minute consistently. Actual production changes only when a timestamped credited production sample is reached. Goal increases with productive time under the active work center's target. Planned breaks do not add goal. Transfers switch the target at the transfer time while retaining the earlier cumulative totals.

The presenter serializes only validated, finite values needed by the browser. Each production trigger receives its relevant cumulative series and explicit interval start/end timestamps. The browser converts the pointer position to an exact minute, finds the latest production checkpoint at or before it, calculates or selects the matching cumulative goal, and uses the latest rolling uptime point at or before it.

## Unavailable Data

The interface must not invent values from incomplete totals, truncated production samples, missing targets, or unavailable interval metrics. At an unavailable moment, the card reads:

```text
10:37 AM
Production: N/A
Uptime N/A
```

If cumulative production becomes untrustworthy at any earlier metered interval, later day-to-point cumulative production and goal remain unavailable because a partial total would be misleading. Existing source warnings remain visible.

## Accessibility and Presentation

The hover card remains a real tooltip associated through `aria-describedby`. Its text is available for pointer, keyboard, and touch users. The vertical marker is decorative and does not become a separate focus target. The card stays inside the viewport and does not obscure the selected minute when there is room to place it above or below the bar.

The server-rendered fallback accessible label remains meaningful when JavaScript is unavailable. It may keep the existing interval summary because precise cursor selection requires interaction.

## Testing

Pure metric tests will prove cumulative production and adjusted goal at interval boundaries, production event timestamps, planned breaks, transfers between different targets, and the end of the day. They will also prove that incomplete earlier production makes later cumulative values unavailable.

Presenter tests will verify finite serialized checkpoints, exact timestamps, one-decimal production formatting, whole-percent uptime formatting, open and closed interval fallbacks, and unchanged forklift/non-metered detail contracts.

Browser-controller tests will cover cursor positions at the start, middle, and end of a bar; exact-minute time selection; latest-five-minute uptime selection; vertical-marker movement; pinning; keyboard and tablet fallbacks; viewport clamping; live-refresh restoration; and unavailable values. Existing dashboard, template, accessibility, and polling regression suites must remain green.

## Non-Goals

- No per-hover network request.
- No estimated or linearly spread production units.
- No change to the forklift metric or forklift hover format.
- No change to non-metered hover details.
- No new dashboard filters or summary columns.
