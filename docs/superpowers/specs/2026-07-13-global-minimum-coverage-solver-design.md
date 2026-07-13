# Global Minimum-Coverage Scheduler Design

## Purpose

Replace the automatic scheduler's greedy person/group assignment pass with a
global minimum-coverage solver. The solver must recognize swaps across skills:
when a cross-trained operator is the only practical way to cover one work
center, it must reserve that operator there and backfill the operator's other
role with another qualified person.

The motivating case is Jose Luis being assigned to Repair while Dismantler 1
was left empty, even though an unscheduled Repair-qualified person could have
backfilled Repair. The current algorithm ranks person/group pairs independently,
assigns the first winning pair, and never reconsiders the person's remaining
skills. Center minimums are prioritized only after the group has already been
chosen, so the later warning cannot repair the earlier choice.

## Goals

1. Maximize the number of enabled Auto work centers staffed to their effective
   minimum without violating hard safety rules.
2. Make globally useful cross-skill moves and backfills instead of relying on
   greedy person-by-person placement.
3. Preserve manual/default locks and active training-block commitments.
4. Treat every skill level of 1 or higher as qualified for emergency minimum
   coverage, even when the person's saved preference is `never`.
5. Keep level 0 ineligible outside an explicit training block and report that
   training is required when level-0 people are the only future coverage path.
6. Save the best safe partial schedule when full coverage is impossible, while
   leaving unresolved Auto centers enabled and visible for manual resolution.
7. Make every unusual assignment and unresolved center explainable.
8. Use one skill-aware feasibility model for page rendering, Auto-center
   changes, and automatic rebuilds.

## Non-goals

- Automatically choose a trainee or trainer when training is required.
- Automatically disable Auto work centers when coverage is impossible.
- Move or delete manual/default assignments to improve an automatic result.
- Allow level-0 placement outside an active training block.
- Introduce a heavyweight external optimization service or solver dependency.
- Add configurable work-center priority in this change.

## Binding Scheduling Policy

The solver applies this priority order:

1. Preserve manual/default locks and active training-block commitments.
2. Enforce hard constraints: full-day absence, active/non-reserve eligibility,
   skill levels, one person per location, center capacity, effective minimums,
   Trim Saw pairing, and training-block partner rules.
3. Staff the greatest possible number of enabled Auto centers to minimum.
4. Avoid `never` preferences when an equally complete solution exists, but
   override `never` for a level-1+ person when doing so increases minimum
   coverage.
5. Protect scarce cross-trained people for centers with fewer alternatives.
6. Prefer stronger skill coverage among otherwise equivalent minimum-coverage
   solutions.
7. Apply the selected mode's preferences, training goals, and rotation fairness
   only after minimum coverage is settled.

All enabled Auto centers are equal for the coverage objective. Existing
work-center order is only a deterministic final tie-breaker; it does not grant
a center operational priority. When full coverage is impossible, the system
saves a safe partial schedule, alerts the planner, and leaves the remainder for
manual resolution.

## Architecture

### Immutable solver input

The route layer builds a normalized, immutable input model containing:

- the target day and selected schedule mode;
- enabled Auto centers in canonical order;
- effective required skills and min/max staffing for every center;
- active roster members, reserve status, and per-skill levels;
- full-day time off and other availability exclusions;
- current assignments and assignment sources;
- saved default people;
- scheduling preferences;
- bounded group/center history; and
- validated training-block effects.

The pure solver performs no database, Odoo, clock, or settings reads. Identical
inputs must always return identical outputs.

### Stage 1: protected commitments

Manual assignments, saved defaults, and active training-block commitments are
applied first. These people are removed from the automatic candidate pool.

A protected person counts toward a center's safe minimum only when they are
active, non-reserve, present for the day, and level 1+ in every required skill,
or when an active training block explicitly authorizes their level-0 placement.
Invalid, absent, or unqualified locks remain visible and preserved but produce
a structured issue; they do not falsely make the center feasible.

The solver may build around a protected commitment but never relocates or
deletes it.

### Stage 2: global minimum coverage

For each center, calculate the remaining minimum slots after valid protected
commitments. Create candidates only for level-1+ people who satisfy every
required skill and all hard availability constraints.

Centers needing one remaining person are solved with deterministic minimum-cost
maximum matching. Centers needing two or more remaining people, or enforcing a
coupled crew rule, expose complete valid crew options around that matching step:

- Trim Saw options contain only valid pairings.
- An active trainee option includes a valid level-3 partner when required.
- An ordinary multi-person option contains the full remaining qualified crew.

For each bounded combination of multi-person crew options, remove those people
from the pool and solve the remaining one-person centers by matching. Compare
the complete results using the scheduling objective below. This preserves the
polynomial fast path for most centers without allowing a slot-maximizing match
to strand partial crews across several multi-person centers.

The solver first maximizes the number of centers that reach minimum; partial
generated crews are forbidden. Among equally complete solutions, it minimizes
preference overrides, protects scarce candidates, prefers stronger skills, and
then uses stable work-center/name order.

This stage is global: a person assigned to one group is evaluated against the
coverage consequences for every other eligible group before the choice is
finalized.

### Stage 3: optional capacity and mode goals

Minimum-coverage assignments are frozen before optional capacity is filled.
The existing mode meanings remain:

- **Optimized:** prefer the strongest remaining skill coverage.
- **Normal:** balance preference, recent group history, and fair center
  rotation.
- **Training:** add capped level-1/2 development placements where a safe
  level-3 relationship is available.

Optional placements may not reduce achieved minimum coverage, exceed capacity,
assign a person twice, create an invalid pair, or use a `never` candidate.
`Never` is overridden only for Stage 2 minimum coverage.

### Stage 4: result and persistence

The solver returns a structured result with:

- final assignments;
- assignment source metadata;
- per-assignment reason codes and display text;
- achieved and unresolved center minimums;
- structured coverage issues;
- qualified but unused people; and
- deterministic summary counts.

The rebuild endpoint persists the returned safe schedule even when some centers
remain unresolved. It does not disable Auto centers. Existing non-Auto centers,
notes, schedule metadata, published state, and snapshots retain their current
preservation behavior.

## Preference Semantics

`primary`, `regular`, and `occasional` remain soft ranking inputs. `never` has
two context-dependent meanings:

- During minimum coverage, a level-1+ `never` candidate is allowed only when
  using them achieves more staffed centers than every solution that respects
  `never`.
- During optional capacity, `never` remains an absolute exclusion.

An automatic `never` override must carry the reason
`preference_override` with display text such as:

> Assigned despite Never to meet minimum coverage.

Manual assignment remains allowed regardless of preference, matching current
behavior.

## Level-0 and Training Behavior

Level 0 is never an ordinary automatic candidate. It remains eligible only
through the existing explicit training-block workflow.

When a center cannot be staffed by any valid level-1+ solution and at least one
active, present person has level 0 in the required skill, return a
`training_required` issue. The Staffing warning names the affected group or
center, for example:

> Dismantler 1 could not be staffed. Training is required for Dismantler.

The warning does not recommend or preselect a trainee, trainer, start day, or
block length. The planner makes those choices through the existing training
workflow.

## Partial-Schedule Behavior

Auto checkboxes express the centers the planner wants to run; they do not
promise that the current roster can staff them all.

If full minimum coverage is impossible, the solver:

1. finds the safe assignment covering the greatest number of enabled centers;
2. saves those assignments;
3. leaves every below-minimum center with no generated partial crew;
4. keeps unresolved centers enabled for Auto; and
5. shows an alert listing every unresolved center and its cause.

The system does not automatically select which Auto centers should be disabled.
The planner may manually move people, change Auto centers, correct skill data,
or establish training.

The current headcount-only capacity guard becomes a skill-aware advisory. An
Auto-center selection is not rejected merely because today's roster cannot
fully cover it. Page rendering, Auto-selection responses, and rebuilds all use
the same solver feasibility result so they cannot disagree about whether the
skills fit.

## Explanations and Diagnostics

Assignment reasons use stable codes with readable display text. Initial codes
include:

| Code | Meaning |
| --- | --- |
| `minimum_coverage` | Required to staff this center to minimum. |
| `preference_override` | A `never` preference was overridden for minimum coverage. |
| `primary_preference` | Selected because this is a primary assignment. |
| `rotation_fairness` | Selected because the person/group or center was least recent. |
| `strongest_coverage` | Selected by Optimized mode for skill strength. |
| `training_development` | Added by Training mode after minimums were protected. |
| `training_block` | Reserved by an active explicit training block. |

Assignment reasons are diagnostic data only. The Staffing page must not render
them beside employee names as badges, notes, icons, tooltips, or hidden text.
Only actionable schedule-level warnings and unresolved-center issues appear in
the UI; their “Why?” disclosure may show candidate rejection details.

Unresolved-center issues also use stable codes:

| Code | Meaning |
| --- | --- |
| `no_qualified_operator` | No available level-1+ person satisfies the required skills. |
| `training_required` | Level-0 people exist, but explicit training is required. |
| `qualified_people_locked` | Qualified people exist but are protected elsewhere. |
| `insufficient_qualified_headcount` | Qualified supply cannot cover all enabled minimums. |
| `no_safe_pair` | No crew satisfies a coupled pairing rule. |
| `invalid_center_configuration` | Effective min/max or required-skill configuration is inconsistent. |
| `protected_assignment_unqualified` | A preserved manual/default assignment does not count toward the safe minimum. |

The existing warning area renders concise center-specific messages. A small
“Why?” detail shows relevant candidates and rejection reasons, such as
absent, skill 0, reserve, manually locked elsewhere, incompatible pair, or
already required by a scarcer center. Diagnostics are derived from the solver's
structured result rather than reconstructed separately in the UI.

## Error Handling

- Input-read failure retains the current fail-safe behavior: do not destructively
  rebuild the schedule.
- Invalid center configuration leaves that center unresolved and reports the
  configuration issue; the solver does not guess a replacement rule.
- No generated center may end with a nonzero crew below its effective minimum.
- Manual/default locks survive every successful rebuild.
- A solver exception returns a rebuild failure and leaves the stored schedule
  unchanged.
- A successful partial result is not an exception; it is saved and returned
  with structured issues.

## Performance

Use polynomial matching for independent minimum slots and restrict bounded
crew-option search to coupled rules such as Trim Saw. Stable candidate ordering,
scarcity-first branching, memoization, and an upper-bound coverage calculation
keep the coupled search predictable.

Add a plant-sized performance fixture representing the full current location
set, a generously cross-trained roster, two-person centers, locks, and training
effects. Target a sub-100 ms in-process solve for ordinary plant schedules and
keep the focused plant-sized regression below one second in the repository's
standard local test environment. Record benchmark timing during the read-only
replay before rollout; if the one-second regression guard is unstable in CI,
retain it as a reported benchmark while keeping deterministic result and
search-bound assertions as the gating tests.

## Verification

Focused tests must cover:

1. the Jose Luis swap: move Jose Luis to Dismantler and backfill Repair with a
   lower-ranked qualified person;
2. longer swap chains involving at least three cross-trained people;
3. `never` overridden only when it increases achieved minimum coverage;
4. `never` excluded from optional placements;
5. level 0 never assigned automatically and `training_required` returned;
6. manual/default lock preservation;
7. absences, reserves, inactive people, and people assigned outside Auto;
8. multiple two-person-minimum centers receiving their minimum or no generated
   crew;
9. every valid and invalid Trim Saw pairing class;
10. training-block trainee and level-3 partner protection;
11. insufficient qualified staffing producing and persisting the best safe
    partial schedule;
12. no automatic Auto-center disabling;
13. identical inputs producing identical outputs;
14. Optimized, Normal, and Training never reducing achievable minimum coverage;
15. multi-day group and center fairness after minimum coverage is satisfied;
16. structured assignment reasons and coverage issue payloads;
17. page context, Auto-selection response, and rebuild response agreeing from
    the shared feasibility result; and
18. the plant-sized performance fixture.

For small generated fixtures, compare the solver with an exhaustive reference
oracle to prove that it achieves the maximum possible number of staffed
centers. Property-style invariants should assert one-person-one-location,
qualification, capacity, pairing safety, lock preservation, and no generated
partial crews across many compact scenarios.

## Rollout

Before making the new result authoritative, replay representative saved
schedules through the pure solver in read-only mode. Compare current and new
assignments, achieved minimums, preference overrides, and issue codes. This
replay does not write schedules.

Review cases with unexpected overrides or newly exposed skill-data problems,
then switch the rebuild and page feasibility paths together so there is no
period where the capacity guard and assignment engine use different models.

## Acceptance Criteria

- The Jose Luis scenario automatically moves Jose Luis to Dismantler and uses
  an available level-1+ Repair-qualified person to backfill Repair.
- The solver staffs the maximum possible number of enabled centers to minimum
  under the global hard constraints before optional mode preferences are
  considered.
- `Never` can be overridden for level-1+ minimum coverage and is explained.
- Level 0 is never automatically assigned; the planner is told that training is
  required without an automatic trainee/trainer recommendation.
- Impossible full coverage produces a saved safe partial schedule and an alert,
  while unresolved centers remain enabled and receive no generated partial crew.
- Manual/default locks, training blocks, Trim Saw safety, mode behavior, and
  rotation fairness remain intact.
- Page, Auto-selection, and rebuild feasibility results come from the same
  skill-aware solver output.
