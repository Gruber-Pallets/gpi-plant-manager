# Persistent Bay Cell Color Design

## Goal

Bay cells in the interactive staffing schedule retain the exact same visible
background and text color whether their work center is enabled or disabled.

## Scope

- Apply only to the interactive staffing schedule in `staffing.css`.
- Preserve the existing disabled appearance for non-bay cells.
- Do not change print styles, templates, JavaScript, or scheduling data.

## Design

Disabled work-center rows currently use row opacity to dim their contents.
Move that dimming to a selector that targets only non-bay cells in a disabled
row. The bay cell then receives no opacity at all, while its existing
state-specific background override keeps it on `--panel-3`.

## Testing

Add a static CSS regression test that verifies disabled-row dimming targets
non-bay cells and never the row itself. Retain the existing test that verifies
the bay background override remains after active and inactive background rules.
