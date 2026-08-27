# Task 1 Report: Durable local celebration queue foundation

## Summary

Added the local-only foundation for private employee birthday and work-anniversary celebrations. The schema stores only birthday month/day and first-contract date on `people`, then keeps durable employee-owned celebration events in a separately indexed `employee_celebrations` queue. The queue generates only today through 370 days ahead, observes February 29 on February 28 in non-leap years, retains past or acknowledged events, removes only stale future unacknowledged events, and scopes lookup/acknowledgement to the signed-in Odoo employee ID. The module has no Odoo dependency.

## Files changed

- `src/zira_dashboard/_schema.py` — idempotent person-field migration and private queue/index DDL.
- `src/zira_dashboard/employee_celebrations.py` — models, safe date normalization, event generation, future reconciliation, local due lookup, and atomic owner-scoped acknowledgement.
- `tests/test_schema_employee_celebrations.py` — static DDL contract coverage.
- `tests/test_employee_celebrations.py` — date, queue, delivery, and Postgres reconciliation coverage.
- `CHANGELOG.md` — child-friendly private-celebration preparation note.

## Tests

### RED

- `uv run --extra dev pytest tests/test_schema_employee_celebrations.py tests/test_employee_celebrations.py -q` — failed during collection with `ImportError` because `employee_celebrations` did not exist.
- The same command after adding the remaining queue tests — failed as intended with six `AttributeError` failures for the missing contract-date, queue-generation, due-lookup, and acknowledgement API.

### GREEN

- `uv run --extra dev pytest tests/test_schema_employee_celebrations.py tests/test_employee_celebrations.py -q` — 3 passed after the schema and pure-date implementation.
- The same command — 9 passed, 2 skipped after the full local queue implementation. The skips are the Postgres reconciliation tests because no `DATABASE_URL` is configured in this worktree.
- `uv run --extra dev pytest tests/test_schema_employee_celebrations.py tests/test_employee_celebrations.py tests/test_employee_notifications.py -q` — 32 passed, 2 skipped.
- `uv run --extra dev ruff check src/zira_dashboard/employee_celebrations.py tests/test_schema_employee_celebrations.py tests/test_employee_celebrations.py` — passed.
- `git diff --check` — passed.

## Self-review

- The birthday normalizer returns only `(month, day)` and rejects malformed values; no birth year is written to schema or queue data.
- `event_day_for` applies the February 29 observation rule to birthdays and anniversaries.
- Reconciliation reads only local active `people` data, uses the exact dedupe conflict key, never deletes due/old events, never deletes acknowledged events, and clears stale future unacknowledged events for inactive people.
- `next_due` and `acknowledge` both include `person_odoo_id`, so another employee cannot read or acknowledge an event.
- This task does not alter Timeclock routes, so it cannot add an Odoo call to a kiosk request path.

## Commits

- `feat: add employee celebration queue` (current task branch; intentionally not pushed pending independent review).

## Remaining risks

- The two reconciliation tests could not run here because `DATABASE_URL` is not configured. They are included and skip safely; run them against the approved local/integration Postgres database before release.
- Odoo date import and Timeclock presentation are intentionally deferred to later scoped tasks.

## Repair: atomic reconciliation source snapshot

### Summary

Independent review identified that `reconcile_future()` read `people` in one transaction and later cleaned/inserted queue rows in separate transactions. A concurrent roster sync could change a source row between those operations. Reconciliation now runs its source read, inactive cleanup, stale-future cleanup, and queue inserts through one `db.cursor()` transaction. It locks every locally mirrored Odoo source row with `FOR UPDATE`, then derives the active subset while those source rows remain locked. A concurrent source sync therefore either commits before reconciliation reads or waits until reconciliation has committed its queue state.

### RED

- `uv run --extra dev pytest tests/test_employee_celebrations.py -q` — failed as expected: `test_reconcile_future_locks_sources_and_mutates_the_queue_in_one_transaction` detected the old direct `db.query` call instead of using its transaction cursor.

### GREEN

- `uv run --extra dev pytest tests/test_employee_celebrations.py -q` — 9 passed, 2 skipped.
- `uv run --extra dev pytest tests/test_schema_employee_celebrations.py tests/test_employee_celebrations.py tests/test_employee_notifications.py -q` — 33 passed, 2 skipped.
- `uv run --extra dev ruff check src/zira_dashboard/employee_celebrations.py tests/test_schema_employee_celebrations.py tests/test_employee_celebrations.py` — passed.
- `git diff --check` — passed.

### Repair commit

- `d0b413cc8807c3b8fa04668e79b6de0e78404cad fix: lock celebration queue reconciliation` (local only; not pushed).

## Second repair: shared source serialization and corrected anniversary years

### Summary

The source-row locks added in the first repair could deadlock with the roster writer because that writer upserts people in source-list order. Both the queue rebuild and the roster writer now acquire the same transaction-scoped PostgreSQL advisory lock before touching `people`, so those workflows cannot interleave their source locks. The queue keeps its row-level locks for its own consistent source snapshot.

The event insert now uses a guarded conflict update for `work_anniversary` rows. If Odoo corrects only the year of a first-contract date, the existing unique `(person_odoo_id, kind, event_day)` row receives the new `completed_years` value only when it is future and unacknowledged. Birthday conflicts, due/past events, and acknowledged history remain unchanged.

### Files changed

- `src/zira_dashboard/employee_celebrations.py` — shared advisory-lock helper and guarded anniversary-year conflict update.
- `src/zira_dashboard/odoo_sync.py` — roster write transaction acquires the same source lock.
- `tests/test_employee_celebrations.py` — queue lock and safe conflict-update contract coverage.
- `tests/test_odoo_sync_celebration_locking.py` — roster writer lock contract coverage without requiring Postgres.

### RED

- `uv run --extra dev pytest tests/test_employee_celebrations.py tests/test_odoo_sync_celebration_locking.py -q` — 3 failed, 8 passed, 2 skipped. The failures showed the queue did not acquire the shared advisory lock, the upsert used `DO NOTHING`, and the roster writer did not acquire the lock.

### GREEN

- `uv run --extra dev pytest tests/test_employee_celebrations.py tests/test_odoo_sync_celebration_locking.py -q` — 11 passed, 2 skipped. An intermediate run exposed a misplaced local import in `odoo_sync.sync()`; it was traced to `_read_last_sync()` and corrected before this green run.
- `uv run --extra dev pytest tests/test_schema_employee_celebrations.py tests/test_employee_celebrations.py tests/test_employee_notifications.py tests/test_odoo_sync_celebration_locking.py tests/test_odoo_sync.py -q` — 35 passed, 17 skipped.
- `uv run --extra dev ruff check src/zira_dashboard/employee_celebrations.py src/zira_dashboard/odoo_sync.py tests/test_schema_employee_celebrations.py tests/test_employee_celebrations.py tests/test_odoo_sync_celebration_locking.py` — passed.
- `git diff --check` — passed.

### Repair commit

- `3b15ab1d919b5d3e25e48807fbbf5c5d98bcda05 fix: serialize celebration source updates` (local only; not pushed).

## Final repair: serialize Skills roster writes

### Summary

`staffing.save_roster()` is a production `people` writer used by the Skills save route. It now acquires the existing transaction-scoped celebration source lock immediately after entering its already-existing database transaction and before the first `people` upsert. This uses the same lock as reconciliation and Odoo roster sync, preventing a Skills save from deadlocking with the queue's locked source snapshot. No roster values, write ordering, cache invalidation, or route behavior changed.

### Files changed

- `src/zira_dashboard/staffing.py` — acquire the shared celebration source lock inside `save_roster()` before writing `people`.
- `tests/test_staffing_roster_status_ownership.py` — focused lock-before-people-write contract while retaining the active-status ownership assertion.

### RED

- `uv run --extra dev pytest tests/test_staffing_roster_status_ownership.py -q` — 1 failed, 1 passed. The new test showed that the first SQL statement was the `people` upsert instead of the shared advisory lock.

### GREEN

- `uv run --extra dev pytest tests/test_staffing_roster_status_ownership.py -q` — 2 passed.
- `uv run --extra dev pytest tests/test_schema_employee_celebrations.py tests/test_employee_celebrations.py tests/test_employee_notifications.py tests/test_odoo_sync_celebration_locking.py tests/test_odoo_sync.py tests/test_staffing_roster_status_ownership.py tests/test_skills_cache.py -q` — 37 passed, 19 skipped.
- `uv run --extra dev ruff check src/zira_dashboard/employee_celebrations.py src/zira_dashboard/odoo_sync.py src/zira_dashboard/staffing.py tests/test_employee_celebrations.py tests/test_odoo_sync_celebration_locking.py tests/test_staffing_roster_status_ownership.py` — passed.
- `git diff --check` — passed.

### Repair commit

- `3043525d5d68e65dabef6f7fd1a8e1be40ff54f3 fix: lock staffing roster source writes` (local only; not pushed).
