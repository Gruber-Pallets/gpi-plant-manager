# Task 1 Report — Capture and Store Exact Lateness

## Status

DONE_WITH_CONCERNS

## Delivered

- Changed the default attendance grace period from seven to five minutes.
- `compute_status` now calculates and stores exact `minutes_late` before selecting `clocked_out`, `late`, or `on_time`.
- Added an idempotent `late_arrivals.minutes_late` column, allowing only positive non-null values.
- Added `clear_snooze` and idempotent `record_late_arrival`; records at five minutes or less are ignored and the first qualifying exact-minute value is preserved.
- Changed the default late snooze from 30 to 60 minutes.
- Preserved the legacy history `reason` fields and added `minutes_late` to late-arrival history after independent review found the current player-card route still consumes `reason`. This is the task-owner-approved compatibility resolution; no UI code changed.

## TDD evidence

- The requested bare `pytest tests/test_attendance.py tests/test_late_report.py -q` could not run because `pytest` is not on the shell PATH.
- RED: `uv run pytest tests/test_attendance.py tests/test_late_report.py -q` produced the expected six-minute boundary failure: `on_time` instead of `late` (1 failed, 21 passed, 6 skipped).
- GREEN: the same focused command passed with 22 passed and 6 database-gated skips.

## Verification

- Final focused suite: `uv run pytest tests/test_attendance.py tests/test_late_report.py -q` — 22 passed, 6 skipped.
- Player-card compatibility: `uv run pytest tests/test_player_card.py -q` — 9 passed.
- Lint: `uv run ruff check src/zira_dashboard/attendance.py src/zira_dashboard/_schema.py src/zira_dashboard/late_report.py tests/test_attendance.py tests/test_late_report.py` — passed.
- Required full suite: `uv run pytest -q` — 4,168 passed, 24 skipped, 2 failed, 38 errors in 12m43s. The failures were outside Task 1: an external Zira API returned HTTP 403, and Saturday-recruiting tests had pre-existing/parallel database-fixture collisions (duplicate/foreign-key data and one related 422 assertion).
- `git diff --check` passed.

## Review

An independent review found that removing `reason` from the history helpers would make the existing player-card route raise `KeyError`. The task owner directed this task to preserve `reason` and add `minutes_late`; the later UI task owns removing the reason consumer.

## Scope

No automatic payload recording or UI changes were made. The only additional changed file is `CHANGELOG.md`, required for a push to `main` by the repository instructions.
