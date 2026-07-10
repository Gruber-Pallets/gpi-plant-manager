# Recycling Leaderboard TV Header Design

## Goal

Make the Recycling leaderboard TV header more compact and visually unified:

- Move the YTD and L30 date ranges out of the row below the header and place them immediately to the right of the `Recycling-leaderboard` title.
- Reduce the date-range emphasis with roughly 70% opacity while preserving legibility on dark and light TV themes.
- Add one large decorative `🐐` icon to the left of the two Current GOAT tiles so they read as one section.

## Scope

This change applies only to the TV-mode Recycling leaderboard at `/tv/recycling-leaderboard`. The regular desktop Recycling leaderboard and the New leaderboard remain unchanged.

## Structure

Extend the shared TV header macro with two optional presentation slots:

1. Title metadata, rendered beside the main title.
2. A decorative icon, rendered beside the right-hand item group.

The Recycling leaderboard TV template will pass the formatted YTD/L30 range as title metadata and `🐐` as the right-hand section icon. Callers that omit these values retain the existing header markup and layout behavior.

## Styling

- The title and range metadata share the left header region, with the range aligned near the title baseline and allowed to shrink without overlapping the GOAT area.
- The range uses the existing muted foreground color and approximately `0.7` opacity.
- The GOAT area becomes a two-column layout: a large, vertically centered icon followed by the existing label and two-tile grid.
- Responsive sizing uses `clamp()` so the icon and metadata remain proportional across TV resolutions.
- The existing two GOAT tiles, their data, and their colors remain unchanged.

## Accessibility

The goat icon is decorative and will be hidden from assistive technology. The date ranges remain text in the document and preserve their YTD/L30 labels.

## Testing

- Add a rendering test that first fails unless the TV response contains the title metadata and decorative icon hooks.
- Confirm the non-TV Recycling leaderboard still renders its existing below-header range.
- Run the focused route/static tests and the relevant broader test suite.
- Render the TV page in the browser and visually verify the header at the available viewport, including the dark theme.

