# Claude Handoff — Recycled Smart Rotations

## Start here

Read these two approved documents before changing code:

1. `docs/superpowers/specs/2026-07-10-recycled-rotations-design.md`
2. `docs/superpowers/plans/2026-07-10-recycled-smart-rotations.md`

The work is intentionally being done directly on `main`.

## Current status

**All implementation-plan tasks (1–7) are complete, committed, reviewed, and deployed.** Pushed to `origin/main` on 2026-07-11 (commit `44a7a10`); Railway auto-deployed and the `web` service is Online (healthz 200). Full test suite: 1,616 passed / 301 skipped.

Each task passed a spec-compliance review and a code-quality review before the next began. Feature commits, newest first:

- `44a7a10 docs: explain recycled rotations` (Task 7 — regression + README)
- `b94678a feat: manage recycled rotation preferences` (Task 6 — People Matrix editor + block lifecycle)
- `aab7af1 feat: add recycled staffing controls` (Task 5 — staffing mode control + reasons/warnings)
- `b93a99a fix: bound rotation absence window and tighten staffing wiring` (Task 4 review fixes)
- `b3063dd feat: schedule recycled rotations` (Task 4 — rotation APIs + staffing wiring)
- `8d7262a fix: make reconcile the sole owner of training-block completion` (Task 3 review fix)
- `a636218 test: patch shared odoo_client for skill-cell writer tests` (Task 3)
- `04c81fd feat: add recycled training blocks` (Task 3 — training lifecycle + shared promotion)
- `b3594a3 fix: harden recycled rotation review findings` (Task 2 review fixes)
- `1291238 feat: add recycled rotation recommendations` (Task 2 — pure scoring engine)
- Task 1 (persistence): `97cfe8c`, `e45a4fd`, `5ca596e`, `f1ef5e7`

The approved design and plan are committed as `8096a7e` (design) and `0e2c3d2` (plan).

### Remaining follow-ups (post-deploy)

- **Live smoke test the two UI surfaces** — the staffing "Recycled schedule goal" control and the People Matrix rotation editor / training-block form. All routes are Azure-AD-gated, so these were verified by template-render + logic checks, never in a real browser.
- **Confirm the blank-day behavior** reads right: a fresh Recycled day is now seeded by the rotation engine (not the static `default_people`), with manual locks and Trim Saw pairing preserved.

### Key invariants (for future changes)

- `rotation_training.reconcile_blocks` is the SOLE owner of training-block completion + the level-0→1 promotion. `rotation_store.record_attended_day` is a pure recorder that must NOT auto-complete (auto-completing would let a block finish without ever promoting, since `active_blocks()` only returns `status='active'`).
- Manual assignment locks (`assignment_sources[wc][name] == "manual"`) survive rebuilds; only `generated` entries are recomputed; non-Recycled centers are never touched.
- `_absence_by_day_for_block` is capped at `planned_block_days`' scan horizon to avoid O(days) DB fan-out on the hot staffing page.

### Global Auto scheduling

- A successful goal-button rebuild assigns every available active, non-reserve person exactly once; full-day absences are not available.
- Infeasible rebuilds never save a partial schedule. They return structured placement issues and leave the prior schedule and metadata unchanged.
- Automatic assignments may use only work centers whose Auto checkbox is enabled; the solver never enables or populates another center on its own.
- `schedule_solver.solve_complete_schedule` is the pure authority for full-person placement plus enabled-center min/max feasibility. Generated multi-person crews are atomic: complete or absent.
- Each person may have at most one default target across exact work-center defaults and user-managed group defaults.
- Exact defaults are hard work-center constraints. Group defaults are hard user-group constraints and rotate evenly only among qualified, enabled member centers.
- Manual locks and exact/group defaults are hard constraints. A `never` preference may be overridden only when required to place everyone safely.
- Reset to defaults calls the same server-side complete rebuild; it clears manual locks only inside enabled Auto centers and preserves assignments outside them.
- Level 0 is automatic only through a validated training block; otherwise the complete rebuild fails without saving.
- Page context, Auto selection advisories, and rebuild failures must serialize the same structured placement issues.
- Focused checks: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_schedule_solver.py tests/test_schedule_solver_properties.py tests/test_rotation_suggestions.py tests/test_staffing_rotations.py -q`.

## Binding product decisions

- Scope automatic rotation to the Recycled groups `Dismantler`, `Repair`, and `Trim Saw`.
- Per-person/group soft preferences are exactly `primary`, `regular`, `occasional`, and `never`; missing means `regular`.
- Daily modes are exactly `optimized`, `normal` (default), and `training`.
- Rotate people within their group across individual centers fairly. For example, Repair 1 → Repair 2 → Repair 3 rather than repeatedly choosing Repair 1.
- Optimized maximizes level-3 coverage; Normal balances coverage, preference, and history; Training develops a capped number of level-1/2 operators while preserving level-3 pairing.
- Level 0 is only eligible through a training block: day one pairs the trainee with a chosen level-3 trainer; later attended workdays reserve only the trainee; full-day absences extend the block; completion promotes the target skill to level 1.
- Manual assignment locks must survive rebuilds. Generated assignment sources are exactly `generated` or `manual`.
- Preserve existing Trim Saw pairing guarantees, next-day default seeding, and safe fallbacks.

## What Task 1 added

- Additive schema for rotation preferences, training blocks, completed/absent block days, schedule mode, and assignment sources.
- `src/zira_dashboard/rotation_store.py` for preference/block persistence and validation.
- `Schedule.rotation_mode` and `Schedule.assignment_sources`, including hydration, snapshots, save/load, posted view isolation, and all known pass-through save paths.
- Validation that training blocks are only for the three Recycled groups and assignment sources have the exact `generated`/`manual` vocabulary.

Task 1 review coverage included metadata preservation through notes-only saves, regular saves, discard-draft, clear-testing-day, object API saves, posted-to-draft cache usage, and next-day smart-default seeding.

## Test baseline and environment note

Before implementation, the suite was clean except that the Playwright browser test could not launch inside the restricted sandbox. The focused browser test passed once Chromium was allowed to run outside the sandbox. Baseline result is therefore **1,499 passed, 300 skipped** with normal local browser permissions.

Use:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest -q
```

The following database-backed tests are expected to skip when `DATABASE_URL` is not configured:

```bash
tests/test_staffing_schedules_bulk.py
tests/test_staffing_custom_hours.py
```

## Workspace notes

- The pre-existing untracked `.claude/` directory is not part of this work. Do not add, remove, or modify it.
- `.superpowers/` is gitignored scratch state. It contains Codex task briefs/reports and is not required to continue; the committed plan is authoritative.
- Current branch: `main`.

## Recommended continuation

1. Run the Task 2 red tests from the implementation plan.
2. Implement only the pure recommendation engine changes in `rotation_suggestions.py` and its tests.
3. Keep the existing Trim Saw public functions/regressions intact.
4. Review Task 2 before starting Task 3.
