# Disabled work centers return people to the scheduler rail

## Goal

When a manager turns off a populated work center, its assigned people must no
longer be hidden in the collapsed row. They must be removed from that day's
draft assignments and immediately appear in the correct left-rail section.

## Behavior

- Turning a work center off removes every assignment at that center from the
  current day's draft, while leaving assignments at all other centers intact.
- The settings update and draft-assignment removal are persisted together.
  A failed request leaves both the enabled-center state and the visible
  assignments unchanged.
- The API returns the resulting assignment map. The browser clears the
  returned-off center's picker selections and synchronizes the left rail from
  that authoritative map.
- On a normal day, removed active non-reserve people return to **Unassigned**.
- On a Saturday recruiting schedule, committed volunteers return to
  **Unassigned**; people who did not volunteer remain in **Off**.
- Reserves and full-day time-off entries retain their existing placement.

## Scope

The behavior applies only to an explicit work-center-off action. Turning a
center on does not assign anyone, and no automatic rebuild is triggered.

## Validation

- A route test will verify that turning off a populated center persists a
  draft without that center's assignments, preserves unrelated assignments,
  and returns the changed assignment map.
- A client regression test will verify that the returned map reconciles the
  picker and left rail after a successful toggle.
