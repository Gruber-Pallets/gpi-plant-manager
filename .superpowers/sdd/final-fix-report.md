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
