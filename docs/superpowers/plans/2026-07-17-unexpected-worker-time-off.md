# Unexpected Worker Time-Off Override Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow a worker approved off today to cancel that leave at the kiosk, clock in with a work center, and surface a durable staffing-resolution task in the Exception Inbox.

**Architecture:** Keep leave discovery and override state in a focused `unexpected_worker` module. The timeclock uses it to gate clock-in and synchronously cancel Odoo leave before creating a punch; the Exception Inbox reads its open events and resolves them only when the published schedule contains the worker.

**Tech Stack:** FastAPI, Jinja templates, PostgreSQL, Odoo XML-RPC, pytest.

## Global Constraints

- Only an approved (`validate`) full-day leave covering the current plant day can trigger the kiosk override.
- Odoo cancellation completes before the clock-in log is created; an Odoo failure must not clock the worker in.
- One event per employee per plant day; events resolve only after published schedule placement.
- No new Odoo model is required.

---

### Task 1: Persist and query unexpected-worker events

**Files:**
- Modify: `src/zira_dashboard/_schema.py`
- Create: `src/zira_dashboard/unexpected_worker.py`
- Test: `tests/test_unexpected_worker.py`

- [ ] Write failing tests for finding today’s approved full-day leave, creating one idempotent event, and determining resolution from a published schedule.
- [ ] Run `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_unexpected_worker.py -q` and verify the tests fail because the module does not exist.
- [ ] Add the schema table, module functions `approved_full_day_leave`, `record`, and `open_events`; resolve matching events when the published schedule includes the event worker.
- [ ] Re-run the focused tests and verify they pass.

### Task 2: Gate and confirm kiosk clock-in

**Files:**
- Modify: `src/zira_dashboard/routes/timeclock.py`
- Create: `src/zira_dashboard/templates/timeclock_time_off_override_confirm.html`
- Modify: `src/zira_dashboard/templates/timeclock_dashboard.html`
- Test: `tests/test_timeclock_unexpected_worker.py`

- [ ] Write failing route tests covering the off-day dashboard work-center route, confirmation rendering, successful immediate refusal then clock-in/event recording, and refusal failure with no punch.
- [ ] Run `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_timeclock_unexpected_worker.py -q` and verify the missing behavior fails.
- [ ] Implement the confirmation gate and template; preserve selected center and variance tracking, refuse Odoo leave first, update local mirror, then record the event and queue the attendance sync.
- [ ] Re-run the focused route tests and verify they pass.

### Task 3: Surface and clear management Inbox work

**Files:**
- Modify: `src/zira_dashboard/exception_inbox.py`
- Modify: `src/zira_dashboard/inbox_keys.py`
- Test: `tests/test_exception_inbox.py`

- [ ] Write failing Inbox tests for urgent unexpected-worker rows, shortage recommendation details, staffing link, and auto-clear after a published assignment.
- [ ] Run `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exception_inbox.py -q` and verify the new assertions fail.
- [ ] Add an Unexpected Workers section and its stable item key; link each event to today’s staffing page and include enabled-center shortages when any exist.
- [ ] Re-run the focused Inbox tests and verify they pass.

### Task 4: Verify the complete feature

**Files:**
- Test: `tests/test_unexpected_worker.py`
- Test: `tests/test_timeclock_unexpected_worker.py`
- Test: `tests/test_exception_inbox.py`

- [ ] Run the focused suite: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_unexpected_worker.py tests/test_timeclock_unexpected_worker.py tests/test_exception_inbox.py -q`.
- [ ] Run the related regression suite: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_timeclock_notifications_routes.py tests/test_timeclock_dashboard_tile.py tests/test_time_off_sync.py tests/test_exception_inbox.py -q`.
- [ ] Inspect `git diff --check` and commit the feature with `git commit -m "feat: handle unexpected workers on time off"`.
