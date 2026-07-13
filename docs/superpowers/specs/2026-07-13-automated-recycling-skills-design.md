# Automated Repair and Dismantle Skills

## Goal

Automatically maintain the `Repair` and `Dismantle` skill levels in Odoo from
each person's last-30-day (L30) production performance. The automation applies
after two qualified days, reevaluates daily, and can both promote and lower a
level. Supervisors configure the policy where it is most useful: from the
corresponding People Matrix column header.

## Scope

Only the People Matrix columns backed by the `Repair` and `Dismantle` skills
participate. `Dismantle` is the matrix/Odoo skill name for work centers in the
`Dismantler` scheduling group. All other matrix columns retain their current
manual-only behavior.

## People Matrix interaction

- The existing text area of every matrix header continues to sort rows on click
  and keyboard activation.
- The Repair and Dismantle headers gain a separate, icon-only settings chevron.
  It appears while the header is hovered or focused and has an accessible name
  such as “Configure automatic Repair skills.”
- The chevron is an independent button. Its click and key events do not bubble
  into the header's sort handler.
- Selecting the chevron opens one reusable modal for that group. Escape,
  backdrop click, and an explicit close button dismiss it; focus returns to the
  chevron.

## Settings modal

Each group has independent settings, stored independently and initialized with
these defaults:

| Skill level | Minimum L30 attainment |
| --- | ---: |
| 3 | 90% |
| 2 | 80% |
| 1 | 70% |
| 0 | Below Level 1 |

The modal permits the Level 3, Level 2, and Level 1 minimums to be edited.
Values must be percentages from 0 through 100 and must be ordered Level 3 >=
Level 2 >= Level 1. Level 0 is derived, not independently editable.

Beside the inputs, a live reference table lists each active work center in the
group. For every center it shows the current daily goal and the whole-center
unit values represented by each configured threshold. It also shows one- and
two-operator examples, because the actual daily target is divided equally by
the number of operators assigned to that center. Changing a percentage updates
the reference table before saving.

The modal also shows the fixed automation policy in plain language:

- rolling 30 calendar-day window;
- a minimum of two qualified days in that group;
- a qualified day requires at least four hours in that group;
- output and goal are shared equally among operators on a work center;
- the system runs daily and pushes changed levels to Odoo.

The primary action is **Save & Recalculate**. It saves just that group's
thresholds, starts an immediate calculation and Odoo synchronization for that
group, and shows in-place progress. On completion it reports evaluated,
changed, unchanged, skipped-for-insufficient-days, and failed counts along
with the most recent run time and any per-person Odoo failures.

## Calculation

For a run and a single group:

1. Load the preceding 30 calendar days of `production_daily` attribution,
   excluding manual absences as current normalized L30 reporting does.
2. Restrict records to the group's work centers (`Repair` or `Dismantler`).
3. Combine a person's records for the same day across those centers. A day is
   qualified only if the combined group time is at least four hours.
4. For each contributing work-center assignment, determine the operator count
   for that day and divide both the work-center's attributed output and its
   configured goal equally. Normalize output to the standard full-day hours,
   consistent with the existing L30 average.
5. Compute a daily attainment percentage from normalized person output divided
   by that person's full-day goal share. When the person worked more than one
   center in the group that day, combine the corresponding goal shares before
   calculating the daily percentage.
6. Average the daily attainment percentages across the person's qualified days.
   Do not alter a skill until at least two qualified days exist.
7. Map the average to the configured bucket and compare it to the current
   matrix/Odoo level for that group. Do nothing when it already matches.
8. For a difference, use the existing Odoo-first `skill_levels` writer. This
   updates Odoo before mirroring local `person_skills` data and invalidates the
   appropriate roster and response caches.

Manual edits to these two skills remain available in the matrix. They are
overridden by the next immediate or daily automated run once the person meets
the two-qualified-day requirement.

## Execution, concurrency, and failures

- A daily scheduled run evaluates both groups after the day's production data
  is ready. It is safe to rerun; unchanged levels do not create Odoo writes.
- **Save & Recalculate** runs the selected group immediately and uses the same
  calculation service as the daily job.
- A shared run lock prevents a daily job and a manual recalculation from
  overlapping. A conflicting manual request returns a clear “already running”
  state instead of duplicating Odoo writes.
- Each person's Odoo update is isolated. A failed update leaves that person's
  local level untouched, is captured in the run summary, and does not stop
  other people from being evaluated.
- Invalid modal inputs are rejected before settings are persisted or any Odoo
  call begins. Existing settings remain intact.

## Data and module boundaries

- A dedicated `automated_skills` domain module owns threshold defaults,
  validation, attainment calculation, bucket selection, run locking, run
  summaries, and orchestration of Odoo writes. Pure calculation helpers accept
  records, goals, and operator counts so they can be unit-tested without a DB
  or Odoo.
- A small settings store persists one typed configuration per group and the
  last run summary. It uses the project's existing `app_settings` JSON storage
  convention.
- The People Matrix route loads both group configurations and the work-center
  reference data. It exposes authenticated endpoints to save/recalculate and
  to return run status. The template and `skills-page.js` only handle modal
  state, client-side previews, and request feedback.
- The daily runner calls the domain module after the normal production-daily
  refresh for the completed plant day. It does not duplicate production
  attribution logic.

## Verification

Tests cover:

- defaults, persistence, validation, and independent Repair/Dismantle buckets;
- exact bucket boundaries and below-Level-1 mapping;
- two-day and four-hour qualification rules;
- equal per-person shares, mixed work centers, different work-center goals,
  and normalized partial-day production;
- fewer-than-two-day no-change behavior;
- promotion, demotion, unchanged-level no-op, and manual recalculation;
- daily-run idempotence and lock behavior;
- Odoo-first successful writes and partial Odoo failures;
- API/modal payload validation and the sort-versus-settings-chevron interaction.

## Non-goals

- Automatically changing skills outside Repair and Dismantle.
- Modifying the current production attribution or manual People Matrix editing
  model.
- Introducing configurable qualifying-day duration, window length, or minimum
  number of days in this first release; these remain the agreed L30, four-hour,
  and two-day policy.
