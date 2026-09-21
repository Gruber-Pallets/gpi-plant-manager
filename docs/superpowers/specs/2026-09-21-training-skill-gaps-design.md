# Training skill-gap eligibility

## Goal

A person who is not yet qualified for a work center can start a training
protocol there. After the protocol finishes, they have every required skill
needed to be scheduled at that center without training.

This supersedes the unified training protocol rule that the trainee must be
level 0 for **every** required skill. A trainee who already has some of the
skills, but is still missing others, is eligible.

## Why this is needed

Staffing already judges qualification with the work center's Settings
required-skill list. The displayed level is the lowest of those skills, and a
person is trained only when every required skill is at least 1.

Training creation currently uses the static work-center skill from
`staffing.LOCATIONS` and rejects the trainee unless that one skill is 0.

Live example: Tablets requires `Tablets`, `New Work Stations`, and
`Recycled Work Stations`. Daniel G. has Tablets 1, New Work Stations 1, and
Recycled Work Stations 0. Staffing shows him as untrained. Start training
checks only `Tablets`, sees level 1, and returns "Trainee must be level 0 for
the target skill."

## User experience

- On Staffing, **Start training** succeeds for a person who still has a gap
  in the selected work center's required skills.
- After the planned attended days (or an early Complete), that person has
  level 1 in every skill the protocol trained. Skills they already held keep
  their existing level.
- They then appear trained for that work center and can be scheduled there
  without a training protocol.
- If they are already trained for every required skill, Start training fails
  with a message that names the person and the work center, for example
  "Daniel G. already has the skills required for Tablets."
- The Training sidebar fields stay the same. This change is eligibility,
  skill resolution, and promotion — not a new form.

## Protocol rules

These rules apply to every work center, not only Tablets.

**Required skills.** Create and update resolve the selected center's
required skills from Settings (`work_centers_store.required_skills`), the
same list Staffing uses. Loading/Jockeying and Truck Driver keep their
existing hardcoded required-skill lists from that store. Keep the existing
scheduling-group name mapping (`Dismantler` → `Dismantle`) when resolving
skill ids. A missing `person_skills` row still counts as level 0.

**Not certifications.** Skills whose `skill_type` is `Certifications` are
never protocol skills. Forklift Certified and CDL badges stay certifications.
A protocol never writes a certification level. If a center's required list
is empty after certifications are removed, reject start and update with a
message that the work center has no trainable skills.

**Trainee.** Eligible when at least one protocol skill is 0. Ineligible when
every protocol skill is already 1 or higher.

**Trainer.** Day-one trainer must be level 3 for every protocol skill.

**Snapshot.** Persist the resolved protocol `skill_ids` on the block at
create and update. Later Settings edits do not change an in-progress
protocol's skill set. Attendance, pause, resume, end, day-one pairing, and
exact-center reservation stay as they are today.

**Completion.** `reconcile_blocks` and early Complete remain the only
promotion paths. For each persisted protocol skill, if the trainee's current
level is 0, write level 1 through the existing `skill_levels` writer. Do not
write a skill that is already 1 or higher. Ending a protocol still never
promotes.

## Errors

- Already qualified: name the trainee and work center.
- Trainer below level 3: keep a clear trainer/skill failure (existing
  wording is fine).
- No trainable skills: say the work center has no trainable skills.
- Unknown center, unknown person, and invalid workday count stay as they are.

## Out of scope

- Training people from level 1 to 2 or 3.
- Changing Tablets Settings or anyone's current skill rows as part of this
  work.
- Filtering the trainee and trainer dropdowns.
- Changing how Staffing colors or hides untrained people.

## Verification

Focused tests must prove:

1. Start training uses the Settings required-skill list, not the static
   single skill, and ignores certifications.
2. A trainee at 0 in one required skill and 1+ in the others is accepted
   when the trainer is 3 in every protocol skill.
3. A trainee at 1+ in every protocol skill is rejected with the
   already-qualified message.
4. A center whose required list is only certifications is rejected as
   having no trainable skills.
5. Completion and early Complete write level 1 only for protocol skills that
   are still 0, and do not lower a skill that is already 1 or higher.
6. Existing attendance, pairing, and reservation tests stay green.
