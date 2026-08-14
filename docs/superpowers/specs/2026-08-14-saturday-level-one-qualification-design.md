# Saturday Level-One Qualification Alignment

**Date:** 2026-08-14  
**Status:** Approved design; implementation pending

## Problem

The normal weekday scheduler treats an operator with every required skill at
level 1, 2, or 3 as qualified. Saturday recruiting instead requires level 2 or
3 at both the timeclock-offer and publish stages. That difference blocks an
operator who is valid for weekday staffing, such as Pascual M. at Tablets,
from volunteering for or being scheduled on Saturday.

## Decision

Saturday work centers use the same qualification threshold as regular-week
work centers: every required skill must be at least level 1.

The qualification rule applies consistently to both stages:

1. An employee with all required skills at level 1 or higher may receive and
   accept a Saturday-work offer.
2. The same employee counts as qualified when management assigns and publishes
   the Saturday schedule.

An operator with any required skill at level 0 remains ineligible.

## Scope

Update the two existing Saturday qualification checks in
`saturday_recruiting`:

- employee opening eligibility; and
- publish-time qualification validation and coverage counting.

Keep all other Saturday protections unchanged: one commitment per opening,
commitment and availability checks, full-day time-off checks, duplicate
assignment prevention, requested staffing counts, and manager schedule
control.

## Verification

Add or update focused domain tests that prove:

- all required skills at level 1 qualify an employee for a Saturday opening;
- a required skill at level 0 does not qualify the employee; and
- a level-1 assigned Saturday operator is accepted by publish validation.

Run the focused Saturday recruiting test suite and the related staffing
Saturday integration tests.
