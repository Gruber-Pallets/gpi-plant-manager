# Scheduling Preferences Design

## Goal

Replace the People Matrix's Recycled-only rotation control with an
eligibility-aware Scheduling Preferences control. It must let managers express
how often an operator should be scheduled into every work-center group or
standalone center they are qualified to work, and the automatic scheduler must
honor those choices.

## User experience

The compact `⟳` button beside a person's name becomes an accessible inline SVG
button showing two circular arrows. Its tooltip and accessible name are
"Scheduling preferences". Selecting it opens a dialog titled "Scheduling
Preferences — <person>".

The dialog shows a preference row only when the operator is eligible:

- A **group** contains two or more locations with the same single required
  skill. Its label is that skill name. Current examples include Repair,
  Dismantler, Hand Build, and Junior.
- A location that does not belong to such a group is a **standalone center**.
  Its label is the work-center name. Current examples include Woodpecker #1,
  Master Recycler, Chop/Notch, Tablets, Work Orders, Truck Driver, and
  Loading/Jockeying.
- Eligibility requires level 1 or higher in every skill required by that
  group or standalone center. Loading/Jockeying therefore requires level 1+
  in all three of its required skills.

Each visible row offers Primary, Regular, Occasional, and Never. Changes save
immediately. A missing preference remains Regular. The dialog keeps the
existing level-0 Recycled training-block controls in a clearly separate
section; routine preferences do not expand the level-0 training feature.

Preferences do not depend on today's Auto toggles: they can be prepared for a
center before it is enabled. If a person later loses a qualification, the
stored value is hidden and ignored. It becomes visible again if the person
regains the qualification.

## Scheduling rules

The existing generic auto-scheduling engine receives a dynamically derived
group-to-locations map for all enabled Auto centers. The same shared grouping
rule drives both the People Matrix dialog and the engine:

- Primary strongly favors eligible candidates.
- Regular is neutral and remains the default.
- Occasional is a low-priority fallback.
- Never removes the candidate from automatic placement in that group or
  standalone center.

All existing safety and manager controls remain in force: a person is never
automatically placed with a zero required skill; manual assignments and saved
default people remain locked; per-center capacity and Trim Saw safe pairing
remain enforced; and a grouped operator continues to cycle fairly through its
sibling centers (for example, Repair 1 → Repair 2 → Repair 3). Disabled Auto
centers stay untouched by a rebuild.

## Architecture and persistence

One shared helper, derived solely from `staffing.LOCATIONS` and
`staffing.required_skills_for`, supplies the current scheduling preference
targets. It uses a single required skill shared by at least two locations for
a group; every other location is a standalone target. The target key is the
skill name for a group and the work-center name for a standalone center.

`person_rotation_preferences` continues to store the target key and the
preference. Validation accepts the dynamically derived target keys rather than
the former fixed Recycled list. Existing Recycled records retain their keys and
continue to work without migration. The Matrix route provides per-person
eligible targets to the browser, keeping qualification logic authoritative on
the server.

## Error handling

The save endpoint rejects a target not currently defined by the work-center
configuration or a preference outside the four allowed values. The UI restores
the previous choice and reports the error on a failed save. The Matrix remains
usable if preferences cannot be read, falling back to Regular for scheduling.

## Verification

Automated tests will cover the shared target derivation, level-1 and multi-skill
eligibility, persistence and validation of grouped and standalone targets, and
automatic scheduling behavior for New, Recycled, and standalone Auto centers.
They will also protect fair sibling-center rotation, the existing training
block workflow, the revised dialog copy, and the accessible scheduling icon.
