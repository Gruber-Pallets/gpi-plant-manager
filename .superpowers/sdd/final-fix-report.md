# Final review P1 fixes — 2026-07-17

## Scope

Addressed only the four final whole-branch review findings for default auto
work centers by day. The pre-existing dirty `.superpowers/sdd/task-2-report.md`
and untracked `uv.lock` were not changed or staged.

## RED evidence

Before production changes, ran:

```text
ZIRA_API_KEY=test uv run --extra dev python -m pytest \
  tests/test_settings_auto_work_centers.py tests/test_rotation_store.py \
  tests/test_staffing_schedule_metadata.py \
  -k 'settings_missing_default or schema_initializes_missing or schema_normalizes_legacy or narrow_auto_center or legacy_snapshot_omits' -q
```

Result: `5 failed, 46 deselected`.

- Settings had no shared first-run resolver.
- Schema did not seed from recent assignment history or normalize the legacy
  template before copying it into schedules.
- No narrow row-locked auto-center update existed.
- Posted legacy snapshots without the field resolved to `[]` rather than the
  persisted daily list.

## GREEN evidence

Implemented:

- Schema migration seeds a missing template from the prior 28 days of
  non-testing assignment history, normalizes legacy template values to the
  canonical known-center order, then snapshots that resolved value to legacy
  schedule rows.
- Settings calls the staffing first-run resolver using the plant day.
- The toggle transaction row-locks and reloads the schedule, narrow-updates
  only the daily enabled-center value (and explicitly disabled assignment
  rows), and retains concurrently saved assignments loaded after the lock.
- Posted-view hydration falls back to the schedule row’s value when a legacy
  snapshot lacks `auto_enabled_work_centers`.

Focused GREEN run:

```text
5 passed, 46 deselected in 0.19s
```

Final verification:

```text
ZIRA_API_KEY=test uv run --extra dev ruff check [modified files]
All checks passed!

ZIRA_API_KEY=test uv run --extra dev python -m pytest \
  tests/test_rotation_store.py tests/test_settings_auto_work_centers.py \
  tests/test_settings_group_defaults.py tests/test_staffing_rotations.py \
  tests/test_saturday_recruiting_manager_routes.py \
  tests/test_exception_inbox.py tests/test_staffing_schedule_metadata.py -q
231 passed, 3 skipped in 8.78s
```

`uv run --extra dev` was used because the checked-in `.venv` lacks pytest.

---

# Player-card identity regression fix — 2026-08-18

## Scope

Fixed only the final-review finding that the shared identity-safe leaderboard
aggregation changed historical player-card totals for same-display-name
employees. The player-card route remains explicitly name-addressed; leaderboard
identity behavior is unchanged. The pre-existing untracked `uv.lock` was not
changed or staged.

## Change

- The player-card route now clears `emp_id` on its already name-filtered metric
  records before passing them to the shared metric helper.
- Added a route regression with two `Test Person` records carrying distinct
  employee IDs. It asserts the per-work-center and group card values retain the
  historical combined average of `105.0` across two qualifying days.

## RED evidence

The bare `pytest` command was unavailable on `PATH`:

```text
$ pytest -q tests/test_player_card_stats.py::test_player_card_preserves_name_scoped_metrics_for_historical_namesakes
zsh:1: command not found: pytest
```

Before the route change, the existing worktree runner produced the intended
failure:

```text
$ .venv/bin/pytest -q tests/test_player_card_stats.py::test_player_card_preserves_name_scoped_metrics_for_historical_namesakes
F                                                                        [100%]
E       assert 140.0 == 105.0
=========================== short test summary info ============================
FAILED tests/test_player_card_stats.py::test_player_card_preserves_name_scoped_metrics_for_historical_namesakes
1 failed in 0.37s
```

The `140.0` result was the arbitrary first employee-ID row rather than the
legacy name-scoped average.

## GREEN evidence and test output

Focused regression after the route change:

```text
$ .venv/bin/pytest -q tests/test_player_card_stats.py::test_player_card_preserves_name_scoped_metrics_for_historical_namesakes
.                                                                        [100%]
1 passed in 0.24s
```

Relevant player-card suites and static checks:

```text
$ .venv/bin/pytest -q tests/test_player_card_stats.py tests/test_player_card.py
...............                                                          [100%]
15 passed in 0.31s

$ .venv/bin/ruff check src/zira_dashboard/routes/people.py tests/test_player_card_stats.py
All checks passed!

$ git diff --check
(no output; passed)
```

## Changed files

- `src/zira_dashboard/routes/people.py`
- `tests/test_player_card_stats.py`
- `.superpowers/sdd/final-fix-report.md`

## Self-review

- Employee identity is removed only from the local player-card metric copies,
  after the existing exact-name filter; source records and leaderboard inputs
  are not mutated.
- Per-work-center and group-card calls continue to use the shared metric helper
  with their existing work-center scopes and qualification behavior.
- The regression observes rendered route context rather than helper internals,
  covering both player-card metric consumers that previously selected an
  arbitrary identity row.
- No patch-note text or unrelated application behavior was changed.

## Concerns

- The public player-card URL is name-based, so historical namesakes remain
  intentionally combined on that card. This is the required legacy contract;
  leaderboards remain employee-identity-safe.
