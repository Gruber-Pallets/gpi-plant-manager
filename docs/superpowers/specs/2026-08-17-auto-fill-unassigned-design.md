# Auto Fill Unassigned Design

## Goal

When a planner clicks a Staffing schedule-goal button (Optimized, Normal, or
Training), keep every person who already has a work center and only assign
people who are still unassigned into remaining open Auto spots.

This supersedes the complete-rebuild rule that a goal-button click reshuffles
generated Auto seats and must place every available person or save nothing.

## Product rules

- Everyone already on the day's schedule stays on that same work center.
- Auto may add people only from the unassigned pool.
- Open spots are remaining seats up to each Auto-checked work center's
  maximum, including empty centers that are already checked on.
- Auto never turns a work center on or off.
- If leftover people cannot all be placed, Auto fills every safe open spot it
  can, leaves the rest unassigned, and saves that result.
- **Reset to defaults** and **Clear schedule** remain the start-over actions.
  There is no separate reshuffle control.

The three goal buttons still choose how leftover people are ranked
(Optimized, Normal, Training). They no longer rebuild seats that are already
filled.

## Who stays, who can move

A person is seated when they appear in the current day's assignment map for
any work center, Auto-on or Auto-off.

Seated people:

- are not moved, swapped, or removed by Auto;
- keep their current assignment source (`default`, `generated`, or `manual`);
- count as placed, so Auto will not assign them a second station.

Unassigned people are active, non-reserve employees who are not on full-day
time off and do not already have a station. Reserves stay out of Auto unless
they are already seated.

Auto-off centers are never written by Auto. People already there stay there.

## Open spots and new seats

For each Auto-on work center, remaining capacity is
`max(0, configured maximum - current headcount)`. Training-block extra
partners continue to use the existing extra-capacity exception.

Auto may add a leftover person only when:

- the center is Auto-on;
- that center has remaining capacity;
- the person is qualified (level 1+ for the center's required skills), except
  a validated training-block trainee at level 0;
- the person is not already seated;
- coupled safety still holds after the add (Trim Saw pairing, training
  day-one green partner);
- adding them does not exceed the center's maximum.

A leftover person's saved default is used when that default center is Auto-on
and still has remaining capacity. Otherwise Auto may seat them at another
safe open Auto center. Defaults never pull a seated person back to a default
center.

Level 0 people without a validated training block remain unassigned.

## What this does not change

- New-day seeding and **Reset to defaults** still load only configured
  defaults and leave everyone else unassigned.
- **Clear schedule** still empties the day.
- Manual picker edits, autosave, publish, notes, hours, and Auto checkboxes
  keep their current behavior.
- Training-block lifecycle, Trim Saw pairing, qualification, and maximum-crew
  safety still apply to **new** Auto seats.
- The "turn N work centers on" capacity advisory remains advisory. Auto still
  does not check centers on by itself.

Saturday and holiday Auto use the same fill-only rule: seated volunteers stay
put; Auto only places remaining available volunteers into remaining Auto-on
capacity.

## Scheduling flow

1. Load the current draft, roster, time off, Auto-on centers, capacities,
   qualifications, preferences, defaults, and training-block effects.
2. Start from the current assignment map. Do not strip Auto-on centers before
   solving.
3. Apply validated training-block reservations only by adding unassigned
   block people into remaining capacity. A seated trainee or trainer is not
   relocated to satisfy a block.
4. Build solver candidates only for leftover unassigned people and Auto-on
   centers with remaining maximum capacity.
5. Rank leftover candidates with the selected goal (Optimized, Normal, or
   Training), then existing preference, history, and deterministic
   name/center tie-breakers. Prefer a leftover person's default when that
   center still has room.
6. Place as many leftover people as will fit without moving anyone already
   seated. People with no safe opening stay in `unplaced`.
7. Keep existing assignment sources for seated people. Mark only newly added
   Auto seats `generated`.
8. Save the merged schedule, including when zero new people could be placed.
   Persist the clicked rotation mode.

Hard failures that save nothing remain limited to a malformed request, a
missing or unreadable Auto configuration, zero Auto-on work centers, or an
engine crash. Unplaced leftovers, unmet minimums, and unused defaults are
warnings, not rollbacks.

Post-save validation may reject a fill-only result only when **this Auto
run** created an unsafe new seat (duplicate person, new seat over maximum,
unqualified generated seat, or generated seat on an Auto-off center). A
pre-existing over-capacity, under-minimum, or seated-unqualified assignment
is left in place and reported, not used to discard the fill.

## UI

No new buttons. Update the schedule-goal help line so it says Auto fills
unassigned people without moving the current board, while each mode still
describes how leftover seats are chosen.

The staffing page continues to reconcile the returned assignment map into
Scheduled cells. Unassigned names and capacity advice stay in the existing
warning area. The red "previous schedule kept" state is only for the hard
failures above.

## Architecture

The rebuild endpoint (`POST /api/rotations/rebuild`) stays authoritative.
The browser still posts the day and selected mode and applies the returned
assignment map.

The route currently drops Auto-on assignments before solving and re-locks
only `manual` sources. Fill-only starts from the current map instead, treats
every seated person as already placed, and asks the existing best-effort
solver to fill leftover people into remaining maximum capacity.

Do not reuse the manual-lock path for this. Stamping every seated person as
`manual` would erase `default` / `generated` history and is not required
once seated people are never stripped.

Weekday rebuilds must not cap leftover fill at remaining minimum crew. Open
spots are remaining maximum seats.

## Testing

Prove that a goal-button rebuild:

- leaves every previously seated person on the same work center, including
  defaults and earlier generated seats;
- assigns only previously unassigned people into remaining Auto-on capacity;
- does not turn a work center on;
- saves a partial fill when some leftovers have no safe opening;
- preserves existing assignment sources and marks only new seats `generated`;
- prefers a leftover person's default only when that center still has room;
- still respects training-block reservations, Trim Saw pairing, and
  maximum crew for new seats, without relocating a seated trainee, trainer,
  or Trim Saw partner;
- still hard-fails when no Auto work center is enabled;
- leaves **Reset to defaults** and **Clear schedule** behavior unchanged.

Focused coverage lives in the existing rotation rebuild and solver tests.
