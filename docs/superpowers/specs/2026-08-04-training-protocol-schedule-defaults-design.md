# Training Protocol Schedule Defaults

## Goal

Starting a Recycled training protocol should immediately and safely place the trainee at the selected work center for every planned training day. The trainee must remain visible in that work center's Scheduled picker while the protocol is active.

## User experience

- Starting a protocol on the day currently open in Staffing immediately rebuilds that day's Recycled assignments. The trainee appears selected at the chosen work center. On the first attended day, the selected trainer appears with them.
- When a later, previously unplanned protocol day is opened, its new draft includes the same generated training reservation. The trainee is therefore already selected rather than hidden as an untrained person.
- A level-0 trainee is visible in the selected work center's picker for the active protocol day, even if a planner unselects them. They remain hidden from unrelated work centers and are still excluded when assigned elsewhere or marked off.
- The day-one trainer may make the work center one person above its normal maximum. This is a narrow, generated, training-only exception; it never permits extra routine staffing.
- Training reservations do not write to the permanent Default People setting. When the protocol is complete, the reservation stops. The existing reconciliation path promotes the trainee to level 1 after the required attended days, so they become a normal manual and automatic scheduling candidate.

## Design

The existing Recycled suggestion engine remains the authority for Recycled rebuilds. It exposes the exact day-one trainer names that are allowed to occupy a temporary extra slot, and rebuild validation accepts only that engine-produced extra capacity; all ordinary capacity violations remain hard failures.

For fresh future drafts, Staffing reads the active protocol's exact `BlockEffect` reservation and overlays only its generated trainee/trainer assignments on top of the normal default-only draft. This deliberately does not run the Auto solver, leaving all non-training default behavior unchanged. Disabled targets, unavailable trainee/trainer pairs, a full normal trainee slot, and conflicting concurrent exact reservations remain out of the seed; the engine's existing warning path remains available on rebuild. Reset to defaults applies the same guarded overlay.

The view model receives a per-work-center set of current training trainees. The template marks only those picker rows as training reservations, and CSS makes those otherwise-hidden level-0 rows visible. The JavaScript protocol submission invokes the same authoritative rebuild already used by the Auto controls, rather than constructing client-side assignments.

## Safety and error handling

- Full-day absences, manual conflicts, disabled centers, and unavailable training slots continue to use the existing `effect_for_day` and suggestion-engine protections.
- A generated day-one trainer can exceed normal capacity by one only when the protocol's trainee occupies the same exact center. The validator receives the engine's explicit exception list, so no client request can claim the exception.
- Existing schedule defaults are displaced only at the exact center needed for an applied training reservation. Permanent configuration is never changed.
- The existing reservation-attendance check continues to require generated trainee/trainer assignments before awarding an attended day or level-1 promotion.

## Tests

- Verify that an exact training pair can occupy a center with a one-person normal maximum and that the extra trainer is explicitly recorded as a training-only capacity exception.
- Verify rebuild validation accepts that explicit exception and still rejects ordinary over-capacity staffing.
- Verify a new future default draft overlays only active training reservations with generated sources.
- Verify a level-0 trainee is marked visible only in the protocol's selected picker and that the static template/CSS/JS contracts rebuild after protocol creation.
