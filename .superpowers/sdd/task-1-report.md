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
