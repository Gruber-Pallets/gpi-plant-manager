# Task 2 — Live recruiting-demand display

## Implementation

- Added `data-saturday-recruit-demand` to the scheduler Recruit control. Before
  activation it displays the enabled-work-center count; while recruiting it
  displays the server-rendered remaining demand.
- Added `renderSaturdayRecruitingDemand(bundle, enabledCenters)` to
  `staffing.js`. It uses the returned `coverage.requested` and `coverage.total`,
  and falls back to the enabled-center count when no recruiting bundle is
  returned.
- Called the renderer immediately after `applyEnabledCenters(...)` only in the
  successful `saveAutoCenters` path. The failure path remains unchanged.
- Added static contracts for the demand target and successful-save renderer
  invocation; updated the existing Recruit-control assertion for its new span.

## TDD evidence

### RED

The requested bare command, `pytest tests/test_saturday_recruiting_static.py
tests/test_staffing_rotations.py -k 'recruiting_demand' -v`, could not run
because `pytest` is not on `PATH`. The equivalent managed command was used:

```sh
uv run pytest tests/test_saturday_recruiting_static.py tests/test_staffing_rotations.py -k 'recruiting_demand' -v
```

Result before implementation: `2 failed, 1 passed, 108 deselected`.

- The template target was absent.
- The JavaScript renderer and successful-save invocation were absent.

### GREEN

```sh
uv run pytest tests/test_saturday_recruiting_static.py tests/test_staffing_rotations.py -k 'recruiting_demand' -v
```

Result: `3 passed, 108 deselected in 0.25s`.

### Approved API-contract correction

The initial brief named `coverage.requested_count` and `coverage.filled_count`.
Inspection showed Task 1's actual response contract is `coverage.requested` and
`coverage.total`. The user approved using the actual fields. A focused static
contract for those fields was added and run before changing the renderer:

```sh
uv run pytest tests/test_saturday_recruiting_static.py tests/test_staffing_rotations.py -k 'recruiting_demand' -v
```

Result before the correction: `1 failed, 2 passed, 108 deselected`; the
renderer still referenced `requested_count`. After updating it to
`requested - total`, the same command passed: `3 passed, 108 deselected in
0.18s`.

## Regression verification

```sh
uv run pytest tests/test_saturday_recruiting_static.py tests/test_saturday_recruiting_manager_routes.py tests/test_staffing_rotations.py -v
git diff --check
```

Result: `119 passed, 2 skipped in 9.25s`; `git diff --check` exited 0.

## Self-review

- The server response is the only post-save source used for active recruiting
  demand.
- The pre-recruit display preserves the enabled-center count.
- Error handling does not mutate the recruiting-demand target.
- The changes are limited to the requested template, JavaScript, static test,
  and this report.

## Concerns

None. The API-field deviation from the brief was explicitly approved and is
covered by the static contract.

## Commit scope repair

An initial scoped commit accidentally included this unrelated, already-staged
file. It was removed from the commit index without changing its worktree
content, restoring it to its pre-commit untracked state:

- `docs/superpowers/specs/2026-07-16-scheduler-draft-posted-delivery-lifecycle-design.md`

Final scoped commit: current `HEAD` — `feat: show live Saturday recruiting demand`.
