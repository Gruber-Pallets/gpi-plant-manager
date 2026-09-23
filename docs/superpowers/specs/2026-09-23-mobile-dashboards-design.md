# Mobile manager dashboards

Approved by Dale after reviewing interactive mockups on September 23, 2026.

## Scope and boundaries

Implement Recycling first. Dale explicitly deferred New, Operator, People, both department leaderboards, Leaderboards, Forklift, and Trophies to a later round. Apply the new presentation only at widths of 760px or less and never in TV mode. Desktop and TV presentation, calculations, permissions, polling, and saved layouts remain unchanged. No new data source or dependency.

## Approved presentation

A compact menu and dashboard selector replace crowded phone navigation. Dashboard widgets become content-sized cards; mobile cannot drag, resize, reset, or persist desktop layouts. Keep all existing metrics and details available. Date controls must fit the phone. Tables become readable rows/cards, People timelines use the full card width, and overlays stay within the viewport. Long names wrap. Controls have at least 44px touch targets.

Production rows show the operator name first and smaller muted work-center text beside it. This label shares the line immediately above the bar with the Now label. The track represents 120% of the current goal; Now is at 100%, five-sixths of the track. Actual production fills to its corresponding position, capped visually at the track end. At/above goal is green; below goal is red (no gray tolerance). Do not display the ahead/behind or goal-so-far sentences. Preserve actual numbers and over-range values in accessible/tappable details. Missing goals have a neutral state, no invented target. Historical ranges label the reference as Goal rather than Now. Existing server calculations are authoritative, including operator changes; do not attribute station totals to a single worker as individual totals.

Downtime uses tall connected horizontal rows with person/work-center labels and minutes inside. Red fill uses a common minutes scale within the report; sort by downtime descending. Names describe station staffing, not responsibility for downtime. Do not show the removed '6% downtime during tracked time' sentence.

## Validation

Use deterministic local fixtures, not production edits. Test Recycling at 320, 390, and 760px widths, long names, missing goals, zero/above-goal production, empty data, multiple operators, navigation, and details. Test resize between mobile and desktop without layout writes. Compare desktop and TV renderings before/after at laptop/TV widths. Run relevant existing dashboard suites and browser checks. Commit and push implementation only after required checks pass.
