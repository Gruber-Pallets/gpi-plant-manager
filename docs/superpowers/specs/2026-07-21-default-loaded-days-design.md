# Default-Loaded Days Design

**Supersedes** `2026-07-14-reset-defaults-only-design.md` and the placement
rules of `2026-07-17-future-draft-defaults-design.md` (its lifecycle and
fail-safe rules stand). Decided by Dale on 2026-07-21 after next-day pages
started rendering blank: with only three exact default people configured in
production, defaults-only placement left new days and reset days empty.

## Product decision

"The defaults" means the complete automatic schedule, not just the configured
default people. A brand-new day and a just-reset day are the same product
state: everyone available placed by the Auto scheduler, with exact/group
defaults honored as hard constraints.

## Behavior

**New future days.** The first view of a future day with no persisted schedule
row seeds a saved draft from the clean-slate complete rebuild — the goal-button
engine run with mode `normal`, the default Auto work centers, no base
assignments, no manual locks, and no prior sources. When that solve is
unavailable, incomplete, or unsafe, seeding falls back to the previous
defaults-only placement rather than leaving the day blank. All other 07-17
lifecycle rules stand: only days after the plant's current day, a persisted row
(even a blank one) is authoritative and never reseeded, and unreadable
authoritative inputs fail safe to a blank render with no partial draft.

**Reset to defaults.** `POST /api/rotations/rebuild` with
`reset_to_defaults: true` discards every assignment — manual picks and
non-Auto-center assignments included — and every assignment source, then runs
the same complete rebuild through the endpoint's normal validate-and-save path
with the day's enabled Auto centers and the requested mode. Schedule metadata
(notes, work-center notes, testing day, snapshot, custom hours, Auto toggles)
is preserved. Because reset rebuilds from an empty base, an incomplete solve
must never be saved (it would wipe the day): the endpoint returns 422 with the
standard structured placement issues and keeps the prior schedule.

## Implementation

- `routes/rotations.py` — `default_complete_schedule(d, roster,
  time_off_entries, *, mode, enabled_centers)`: the shared "default schedule"
  builder (suggestion → merge → sources → `_validate_complete_rebuild`);
  returns `(assignments, sources)` or `None`. The rebuild endpoint's reset
  path uses the same inputs (`base_assignments={}`, `locked_assignments={}`,
  `assignment_sources={}`) plus a `suggestion.complete` gate.
- `routes/staffing.py` — `_seed_new_future_draft` tries
  `default_complete_schedule` first, then falls back to
  `_defaults_only_assignments`.
- `static/staffing.js` — the Reset confirmation now describes clearing and
  reloading the default schedule.

## Local QA notes (embedded pgserver)

`AUTH_DISABLED=1` + `DATABASE_URL=<pgserver uri>` runs the real app locally
(`.claude/launch.json` → `staffing-dev`). Fixture gotchas found while
verifying: the Dismantler scheduling group reads the Odoo skill named
`Dismantle` (see `_SCHEDULING_GROUP_SKILL_NAMES`), and `save_schedule` silently
persists zero assignment rows for work centers missing from the
`work_centers` table — seed those rows (with real `min_ops`/`max_ops`; the
column default of 1 breaks Trim Saw's paired minimum) before trusting a local
end-to-end run.

## Addendum 2026-07-23 — why new days stayed blank in prod, and the fix

The seed above shipped running the engine with `minimum_only=True` (the goal
button's minimum-crew mode). In production that solve is deterministically
incomplete: the default Auto set's minimum slots (27) are fewer than the
available people (~30), and Work Orders carries 3 exact defaults on a min-1
center — in minimum-crew mode exact defaults are hard edge restrictions, so
two of them can never seat. Every midnight the page warmer rendered
`/staffing` (next working day), the solve failed, and the seed *persisted*
the defaults-only fallback (3 people, sources `default`). That row made
`schedule_revision` non-None, so no later view ever reseeded — the day was
frozen near-blank.

Two changes (2026-07-23):

1. **The clean-slate rebuild fills past minimums.** `default_complete_schedule`
   and the rebuild endpoint's `reset_to_defaults` path now pass
   `minimum_only=False`: capacity-bounded, so "place everyone exactly once"
   is feasible unattended. Verified against prod data: 30/30 placed, all 3
   Work Orders defaults honored, only Woodpecker #1 goes one above its
   minimum. The non-reset goal button keeps minimum-crew + advisory.
2. **The fallback is display-only.** When the solve fails, the seed renders
   the defaults-only placement but persists nothing, so the next view retries
   the complete rebuild.

Rows created by the old code before the fix (e.g. 07/24, 07/27) still exist
and block seeding; one Reset-to-defaults click per affected day replaces them
with the full default schedule (Reset now succeeds — it uses the same
capacity-bounded solve).
