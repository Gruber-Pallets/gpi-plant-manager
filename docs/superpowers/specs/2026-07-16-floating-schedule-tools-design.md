# Floating Schedule Tools Design

## Goal

Make the staffing page's Schedule Goal controls compact, visually distinct from
the day-notes area, and continuously available while a supervisor scrolls the
staffing table.

## Approved Direction

Use a compact **Schedule tools** card fixed at the desktop viewport's lower
right. It is a standalone utility surface, rather than another bordered notes
panel. The card contains the three scheduling goals, the live scheduling
action/status, and both schedule-maintenance actions.

## Layout

- Replace Optimized's three lightning characters with a single `⚡`; Normal
  (`⚖`) and Training (`🎓`) remain unchanged.
- Keep all three goal buttons in one horizontal row, with equal compact hit
  targets.
- Place the existing dynamic `#minimum-crew-action` status below the modes.
- Move **Reset to defaults** and **Clear schedule** into the same card,
  side-by-side beneath the status. Clear preserves its danger styling.
- On desktop widths above the current single-column breakpoint, use
  `position: fixed` with a lower-right viewport offset and a constrained card
  width. Its `z-index` sits above the staffing table but below modal/popover
  layers.
- At the current mobile/single-column breakpoint, return the card to normal
  document flow below the notes area so it cannot cover controls or table
  content.

## Visual Treatment

The card has a rounded, lifted surface with a soft shadow, a subtle green-tint
header, and a small green status dot next to the Schedule tools label. Its
spacing and segmented controls are intentionally unlike the Notes textarea.
Hover and keyboard-focus states are clear without motion that distracts during
table work. Respect reduced-motion preferences if any transitions are added.

## Behavior and Accessibility

All existing IDs, button types, `data-rotation-mode` values, ARIA labels,
pressed states, disabled/loading states, confirmation behavior, and JavaScript
update hooks remain unchanged. The status continues to be written to
`#minimum-crew-action`; only its visual location changes. Reset and Clear keep
their existing behavior.

## Error Handling

No new requests or state transitions are introduced. Existing server rebuild
errors, reset failures, clear confirmation, and disabled states remain visible
within the card's original controls.

## Verification

- Extend static staffing coverage for the single Optimized lightning icon and
  the control-card containment of Reset and Clear.
- Add CSS contract coverage for desktop fixed positioning and the mobile normal
  flow override.
- Run the focused staffing template/rotation tests and the relevant full suite.
