# Weekly Odoo Calendar-Conflict Monitor — Design

Date: 2026-06-29

## Context

`scripts/diagnose_odoo_calendar_conflicts.py` (PR #9) lists, on demand,
active employees whose Odoo `resource.calendar` would make Odoo reject an
absence on a plant workday — the "not supposed to work during that period"
mismatch from PR #7. Running it is manual. This feature turns it into a
recurring background check that proactively flags **changes** in that set,
filing the result as a task in Odoo so HR works it like anything else.

## Goal

A weekly in-app check that compares the current calendar-conflict set to the
last reported one and, when it changes, maintains a single Odoo `project.task`
describing the conflicts — staying silent when nothing changed.

## Non-goals

- No new alert channels (Odoo task only — not Slack/email/the inbox).
- No per-day schedule modeling — plain weekday coverage, same as the script.
- No UI, no admin trigger button.
- No external scheduler (Railway/GitHub cron) — uses the in-process warmer.

## Components

### 1. Detection module — `src/zira_dashboard/calendar_conflicts.py`

Extract the detection logic out of the CLI script so the script and the
monitor share one path (no duplication):

- `classify_conflict(plant_weekdays, covered_weekdays, is_flexible, has_calendar) -> str`
  — pure; `no_calendar` / `flexible` / `missing_days` / `ok` (moved verbatim).
- `current_conflicts() -> list[dict]` — the Odoo + optional-roster gather
  (moved from the script's `_gather_rows`), returning only conflict rows
  (verdict ≠ ok), each `{name, odoo_id, cal_name, covered, missing, verdict}`.
- `plant_weekdays()` helper (moved) for the Mon–Fri/`schedule_store` source.

`scripts/diagnose_odoo_calendar_conflicts.py` becomes a thin CLI importing
`classify_conflict` / `current_conflicts` / report formatting from here. Its
existing tests move to import from `zira_dashboard.calendar_conflicts`.

### 2. Scheduling — in-process warmer (`app.py`)

Add `_tick_calendar_conflicts` to the `_WARMERS` registry with a ~6h
interval. Real cadence is enforced inside `run_once` by a persisted
`last_run_at` gate (≥7 days since last real run), so frequent redeploys only
trigger a cheap gate check, and the first-ever run (NULL `last_run_at`) fires
on the next boot. The warmer skeleton already logs-and-swallows, so the check
is best-effort and can never kill the loop.

### 3. State — singleton Postgres row `calendar_conflict_monitor`

Additive schema (one row):

```
CREATE TABLE IF NOT EXISTS calendar_conflict_monitor (
  id                INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  odoo_task_id      INTEGER,
  reported_emp_ids  INTEGER[] NOT NULL DEFAULT '{}',
  last_run_at       TIMESTAMPTZ
);
```

A tiny store module reads/writes the row (`_load_state()`, `_save_state(...)`).

### 4. Monitor — `src/zira_dashboard/calendar_conflict_monitor.py`

- `decide(current_ids: set[int], reported_ids: set[int]) -> dict` — pure.
  Returns `{"changed": bool, "added": [...], "removed": [...], "now_empty": bool}`.
- `run_once(force: bool = False) -> dict` — orchestration:
  1. Read state. If not `force` and `last_run_at` is within 7 days → return
     `{"skipped": "throttled"}` (no Odoo calls).
  2. `conflicts = calendar_conflicts.current_conflicts()`; `current_ids` =
     their `odoo_id` set.
  3. `decision = decide(current_ids, reported_ids)`.
  4. If not `decision["changed"]` → just bump `last_run_at`, return
     `{"changed": False}` (silent).
  5. If changed and non-empty: ensure an open task (create via
     `create_feedback_task` using `ensure_feedback_project()` +
     `authenticate()` when there's no stored open task), rewrite its
     `description` to the formatted conflict list, and `post_task_message`
     naming who was added/resolved.
  6. If changed and now empty: `post_task_message("all resolved")` and archive
     the task (`active=False`); clear `odoo_task_id`.
  7. Persist `odoo_task_id`, `reported_emp_ids = current_ids`, `last_run_at`.

### 5. Odoo helpers (`odoo_client`)

Reuse `ensure_feedback_project()`, `authenticate()`, `create_feedback_task()`.
Add two thin wrappers mirroring the existing `post_leave_message` pattern:

- `update_task(task_id, **fields)` → `execute("project.task", "write", [task_id], fields)`
- `post_task_message(task_id, body)` → `execute("project.task", "message_post", [task_id], body=body)`

Archiving the task on resolution is `update_task(task_id, active=False)`.

## Data flow

warmer tick → `run_once()` → (gate) → `calendar_conflicts.current_conflicts()`
(Odoo + optional roster) → `decide()` vs stored `reported_emp_ids` → Odoo task
create/update/comment/archive via `odoo_client` → persist state.

## Error handling

- Best-effort: any exception in `run_once` propagates to the warmer skeleton,
  which logs and swallows. `last_run_at` is only advanced after a successful
  run, so a transient Odoo failure simply retries on the next tick (no false
  "resolved", no lost state).
- A failed Odoo task op leaves state unchanged so the next run re-attempts.
- If the stored Odoo task was deleted, the next changed run's update fails and the monitor falls back to creating a fresh task (a transient update failure can therefore create at most one duplicate task — acceptable for a weekly check).

## Testing

- `classify_conflict` — existing cases (moved).
- `current_conflicts` — reserve exclusion + Postgres-unavailable fallback
  (moved from the script tests).
- `decide()` — pure: unchanged → not changed; new id → added; dropped id →
  removed; set→empty → now_empty.
- `run_once` with mocked `odoo_client` + state store: throttled (recent
  `last_run_at`) → no Odoo calls; first run with conflicts → creates task +
  comment; unchanged → silent, bumps `last_run_at`; changed → updates +
  comments; emptied → archives. State persisted each time.
- Run via `ZIRA_API_KEY=test .venv/bin/python -m pytest`.
