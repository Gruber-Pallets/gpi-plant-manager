# Rebuild default Auto-center regression fix

## Scope

Fix the final review P2: an unsaved current or past Staffing day has no
day-owned Auto-center list yet, so `POST /api/rotations/rebuild` must use the
same Settings default template that Staffing displays and persist it with the
rebuilt schedule.

## Root cause

Staffing checks `schedule_revision(day)`: when it is absent, the page displays
`_default_auto_work_centers(day)`. Rebuild instead read
`_enabled_auto_work_centers(day)`, which loaded the blank ephemeral schedule
and therefore reported that no Auto centers were selected.

## Change

Rebuild now follows the same revision boundary:

- no revision: load the Settings default template into the draft schedule;
- existing revision: retain the day-owned Auto-center state;
- derive solver inputs and the saved schedule from that resolved state.

The normal rebuild save and reset-to-defaults save already persist
`sched.auto_enabled_work_centers`, so the selected defaults become durable on
the first successful rebuild.

## TDD evidence

Added `test_rebuild_unsaved_day_uses_and_persists_default_auto_centers`.

1. Before the production change, it failed with HTTP 422 because rebuild saw
   an empty day-owned center list.
2. After the change, it passes and verifies the solver input, response, and
   saved schedule all contain the Settings default center.

## Verification

```
uv run pytest tests/test_staffing_rotations.py::test_rebuild_unsaved_day_uses_and_persists_default_auto_centers -v
# 1 passed

uv run pytest tests/test_staffing_rotations.py -v
# 125 passed

git diff --check
# clean
```

Pre-existing `.superpowers/sdd/task-2-report.md` and untracked `uv.lock` were
left untouched.
