# No-Goal Metered Ordering Design

**Date:** 2026-09-01

## Goal

Keep every worker at a metered work center in the Metered production section, but place workers whose current or final metered work center has no goal below all metered workers whose current or final work center has a goal.

## Sorting contract

- The row's current interval, or final interval for a completed day, determines its subgroup.
- A production interval has a goal when its production score contains a positive goal value.
- Metered rows with a goal sort before metered rows without a goal.
- The existing attention, deficit, rolling-performance, name, and employee-ID ordering remains unchanged within each subgroup.
- An earlier no-goal interval does not demote a person who later transfers to a goal-based metered work center. Likewise, a person who ends at a no-goal metered work center moves to the lower subgroup even if an earlier interval had a goal.
- Work-center names do not affect the subgroup. Trim Saw follows the same rule as every other metered work center.
- The fixed Metered production, Tablet forklift, and Other non-metered people section order does not change.

## Implementation boundary

The production loader identifies configured metered work centers with a known non-positive or unusable goal and passes those names into dashboard assembly. The subgroup helper consumes the final role, final canonical work-center name, known no-goal names, and final production score, then contributes one subgroup rank to the existing `PersonRow.sort_key`.

Do not change source loading, production scoring, goal calculation, attention reasons, section assignment, summaries, or templates.

## Failure behavior

The loader's known no-goal set is authoritative even when no production score is built for that center. A known zero, negative, or unusable goal sorts in the lower metered subgroup. If configuration cannot establish goal status and the score is also missing, the row retains the normal metered subgroup and its existing unavailable-data attention order. Existing unavailable-location and non-production behavior remains unchanged.

## Verification

- A metered row without a goal sorts below a metered row with a goal, even when the no-goal row needs more attention.
- A no-goal work center other than Trim Saw receives the lower placement.
- A Trim Saw row with a positive goal stays with the normal goal-based metered rows.
- A transfer uses the current or final interval's goal status.
- A stale-location row with a known positive goal retains its existing attention placement.
- Trim Saw and Hand Build rows without goals sort below a goal-based metered row even though no score is built for them.
- Rows within each subgroup retain the existing attention ordering.
- Forklift and Other ordering remains unchanged.
