# Saturday Off Validation Design

## Goal

People who decline an optional Saturday or holiday shift must not be warned that
they are unassigned. The scheduler must continue to show real coverage and
safety problems for people who volunteered to work.

## Scope

This change applies when the Staffing page is rendering an optional workday
(Saturday recruiting or holiday recruiting). On ordinary workdays, validation
continues to expect every active, non-reserve person who is not on full-day
time off to be placed.

## Design

The staffing-page render model already determines the authoritative set of
people who are available for an optional workday: its committed-volunteer
names. The route will pass that set through the Recycled context and
current-view validation layers into the pure current-schedule validator.

The validator will accept an optional expected-working-names collection. When
it is provided, it will:

- emit `person_unplaced` only for active, non-reserve people in that set;
- skip default-placement checks for people outside that set;
- retain all station-level capacity, qualification, training, and minimum-crew
  checks for the visible schedule.

The existing full-day-time-off behavior remains separate. Declining an
optional shift does not create or imitate a Time Off record.

## Data Flow

```text
Saturday/holiday commitments
  -> staffing view's committed-volunteer names
  -> Staffing page context
  -> Recycled current-view validation
  -> current-schedule validator
  -> yellow warning list
```

On a normal day, the expected-working-names value is absent, preserving the
current all-active-roster validation behavior.

## Error Handling

If optional-workday recruiting data cannot be read, the page keeps its
existing safe behavior and does not invent an availability list. The change
does not alter error handling or availability enforcement when saving or
publishing a schedule.

## Testing

Add a pure-validator regression test with a volunteer and a person marked Off.
It must show that only the volunteer can receive an unassigned warning, while
an unfilled work center still reports its staffing shortage. Add a page-wiring
test proving optional-workday rendering passes the committed-volunteer names
to current-view validation. Existing ordinary-day validation tests cover the
unchanged default behavior.

## Non-goals

- Do not hide genuine minimum-crew, safety, capacity, or qualification issues.
- Do not record Saturday declines as Odoo or local Time Off.
- Do not change how managers set Saturday availability or publish schedules.
