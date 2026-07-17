# Saturday Schedule Goal Availability Design

## Goal

Keep the Schedule Goal recommendation aligned with the people who can actually
be scheduled on a Saturday. When the Saturday Unassigned rail is empty, it
must say `Ready to schedule`; people displayed as Off must not cause a
recommendation to turn on work centers.

## Cause

The Staffing page first calculates the minimum-crew balance from the ordinary
weekday roster. It then rebuilds the Saturday view using recruiting
commitments and manager availability overrides, which places non-committed
people in Off. The previously calculated balance remains in the template, so
it still treats Off people as waiting to be scheduled.

## Design

After the Saturday roster model is rebuilt, the Staffing route will calculate
the minimum-crew balance from the final Saturday `unassigned` names rather
than the full active roster. It will retain the existing enabled-center and
minimum-slot calculation, but only those names will contribute to available
staffing capacity.

The normal weekday calculation remains unchanged. Browser-side recalculation
already reads the Unassigned rail, so it continues to agree with the corrected
initial server payload.

## Error Handling

The balance remains advisory. Existing fallbacks for an unavailable effective
minimum continue to use the configured work-center minimum, and a missing or
cancelled Saturday recruitment produces an empty eligible pool.

## Verification

Add a route-level regression test with a Saturday roster containing people
shown as Off and no Unassigned people. The page context must include a balance
with `unassigned_people == 0`, direction `ready`, and zero recommended work
centers. Existing normal-day minimum-crew tests must continue to pass.
