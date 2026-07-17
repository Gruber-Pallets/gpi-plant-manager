# Future Draft Defaults Design

## Goal

When a supervisor first opens an unsaved future schedule day, create a saved
draft containing the configured defaults. Exact work-center defaults remain at
their configured work center. People defaulted to a rotation group are placed
across that group's enabled work centers using the existing rotation history
and least-loaded eligible-center rules.

## Scope and lifecycle

- Seeding applies only to a selected day after the plant's current day and only
  when that day has no persisted schedule row.
- The initial result is saved as a draft immediately. It has normal draft
  metadata and assignment sources of `default`.
- Any persisted schedule row, including an intentionally blank draft, is
  authoritative and is never reseeded or overwritten by a page visit.
- Today, past days, and posted schedules keep their present behavior.

## Placement rules

- Exact work-center defaults are placed first at their configured centers.
- Full-day time off, inactive people, reserve people, and duplicate names are
  excluded.
- Group defaults may only use enabled work centers that belong to the same
  configured group. They choose among centers with available configured
  capacity, prioritizing least-loaded centers and then existing rotation
  history for fair ties.
- A group member that cannot be safely placed is left unscheduled; seeding
  does not turn into a complete automatic scheduling rebuild.

## Architecture

The current Reset to defaults route already owns the correct default-only
placement algorithm. Move that pure builder to the staffing route module so it
can be called by both the reset endpoint and the staffing-page initializer,
without creating a circular dependency between the two route modules.

When the staffing page loads, it will determine whether the selected future
day has a saved row. For a new future date it will load the same authoritative
inputs used by reset (roster, time off, exact defaults, group defaults, enabled
centers, capacities, and recycled history), build default assignments, save a
draft, and render that saved schedule. If any authoritative input cannot be
read, it will fail safe by rendering the existing blank schedule rather than
writing a partial draft.

## Testing

- A first visit to an unsaved future day persists exact defaults as a draft.
- A first visit balances group-default people across enabled work centers in
  their own group, accounting for existing exact placements and capacity.
- A saved future draft, including one with no assignments, is not reseeded.
- Reset to defaults retains the same behavior through the shared builder.
