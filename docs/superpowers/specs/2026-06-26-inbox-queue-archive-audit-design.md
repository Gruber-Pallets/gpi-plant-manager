# Combined Exception Inbox — Queue, Archive & Audit — Design

**Date:** 2026-06-26
**Status:** Approved (brainstorming → implementation planning)

## Context

The Exception Inbox ([`/exceptions`](../../../src/zira_dashboard/routes/exceptions.py))
is the daily operational worklist. Today
[`exception_inbox.build_snapshot()`](../../../src/zira_dashboard/exception_inbox.py#L195)
composes six independent sources into a fixed list of **sections** — Assignments
To Do, Plant Schedule, Late / Absence, Missing Work Center, Missed Punch Out,
Pending Time Off — and
[exceptions.html](../../../src/zira_dashboard/templates/exceptions.html) renders
**every section, always**, even when a section is empty (it shows an "All clear"
row). With a typical day mostly clear, the page is dominated by empty cards and a
manager has to scan past them to find the one or two things that need action.

Each section carries its own inline action controls (Approve/Deny, Assign/Dismiss,
Correct, Absent/Snooze, Save reason, Open) wired to existing endpoints. When an
action resolves an item, the item simply **stops appearing** on the next snapshot —
there is no record on the page of what was done, and (with one exception) no record
of **who** did it. The single exception is time-off: the
[time-off approvals work](2026-06-24-time-off-approvals-design.md) added the
append-only [`time_off_decisions`](../../../src/zira_dashboard/_schema.py#L883)
table and actor capture via
[`time_off_audit.record_decision`](../../../src/zira_dashboard/time_off_audit.py#L15).
The other five categories resolve into suppression tables
([`missing_wc_resolved`](../../../src/zira_dashboard/_schema.py#L846),
[`missed_punch_out`](../../../src/zira_dashboard/_schema.py#L854),
`manual_absences`, `late_arrivals`) that record *that* an item was resolved but
not *who* resolved it.

Dale wants three things, settled during brainstorming:

1. **One combined queue.** Flatten the six sources into a single list ordered by
   what needs attention first, and **stop showing categories that are empty**, so
   the page only shows real work.
2. **Archive on action.** When an item is acted on it moves out of the live queue
   into a **collapsed archive** below, which can be expanded to review what was
   done. The archive persists across days so you can look back.
3. **A user audit trail on everything completed.** Every resolved item records
   **which user completed which inbox item** (extending the time-off pattern to
   all six categories), visible in the archive.

## Decisions

Settled during brainstorming:

| Decision | Choice |
|---|---|
| **Queue order** | **Urgency tier first, then oldest-first within a tier.** Genuinely urgent items never sit below low-priority ones. |
| **What enters the archive** | **Everything that leaves the live queue.** Human actions carry the actor's name; items that clear themselves are recorded as `auto_resolved` (no actor) and are hidden by default but available. **Snooze stays in the live queue** (quieted, as follow-up) — it does not archive, because it returns. **Deny** and **Dismiss** archive as decisions. |
| **Archive reach & layout** | **Rolling, grouped by day** (Today / Yesterday / Earlier) with a "show earlier" control. The log is **retained indefinitely** (rows are tiny); the inline archive queries a bounded recent window (~90 days) and pages back. |
| **Audit detail** | **Action + actor + time + the key specific + before/after where available** (the corrected punch time, the assigned work center, the denial reason). |
| **Multi-user** | **Fully live.** Items add and remove themselves on screen as anyone acts, with a stale-action safeguard so a row that changes the instant before a click cannot fire the wrong action. |
| **Undo** | **Short post-action undo window.** Clean reversal where the data/Odoo API supports it; otherwise undo **re-opens** the item in the queue. Undo is itself audited. |
| **Storage approach** | **One unified `inbox_events` activity log** (Approach 1), not per-category audit columns stitched by union, and not a surface-only change. The archive and the audit trail are both views of this one table. |

## Architecture overview

Five pieces, all reusing existing infrastructure:

1. **`inbox_events`** — a new append-only, denormalized activity log. **Source of
   truth for both the archive and the audit trail.** Every resolution — human or
   automatic, across all six categories — writes exactly one row.
2. **`inbox_open_items`** — a small bookkeeping table mirroring what is *currently*
   open. It makes the server authoritative on "what's open" (powering live
   updates) and lets a background reconciler detect items that left the queue
   without a human action (the `auto_resolved` case).
3. **Combined queue** — [`build_snapshot()`](../../../src/zira_dashboard/exception_inbox.py#L195)
   is refactored to also emit one **flat, sorted, empty-hidden** queue list, with
   each row carrying its category as a colored tag instead of a section header.
4. **Action handlers gain actor capture + an event write.** Each existing handler
   (approve/deny, assign/dismiss, correct, absent/reason) records one
   `inbox_events` row (with before/after) and removes the item from
   `inbox_open_items`. Actor comes from `request.state.user_upn` /
   `request.state.user_name`, already set on every authenticated request by the
   auth middleware ([auth.py](../../../src/zira_dashboard/auth.py#L283)).
5. **A reconcile step** on the existing background tick
   ([app.py](../../../src/zira_dashboard/app.py#L150) — already refreshes missing-WC
   and runs missed-punch close) diffs the freshly-computed open set against
   `inbox_open_items` and writes `auto_resolved` events for departures that no
   human handled.

The UI ([exceptions.html](../../../src/zira_dashboard/templates/exceptions.html),
[exceptions.js](../../../src/zira_dashboard/static/exceptions.js),
[exceptions.css](../../../src/zira_dashboard/static/exceptions.css)) becomes: a
single live queue that animates rows in/out, and a collapsed day-grouped archive
with actor/hide-auto filters and "show earlier".

The existing `time_off_decisions` table is **kept as-is** (recent and tested); the
time-off handlers simply *also* write an `inbox_events` row so the unified archive
includes them. A dual-write for one category is cheaper and lower-risk than
migrating that feature.

## Components

### 1. `inbox_events` activity log

Append-only and **denormalized on purpose**, for the same reason
`time_off_decisions` is: source rows get hard-deleted (e.g. the leave poller
deletes mirror rows; suppression tables get pruned), so the log snapshots enough
to stand alone.

```sql
CREATE TABLE IF NOT EXISTS inbox_events (
  id            SERIAL PRIMARY KEY,
  item_kind     TEXT NOT NULL,        -- time_off | late | missing_wc | missed_punch_out | assignment | plant_schedule
  item_key      TEXT NOT NULL,        -- stable identity, e.g. 'missing_wc:48213' (matches inbox_open_items)
  person_name   TEXT,                 -- snapshot (may be a work center / schedule label for non-person items)
  category_label TEXT,                -- display label: 'Time off', 'Missing WC', ...
  action        TEXT NOT NULL,        -- approve|deny|correct|assign|dismiss|absent|reason|auto_resolved|undo
  outcome       TEXT,                 -- human line: 'Corrected to 4:32 PM', 'Assigned to Saw 1'
  before_value  TEXT,                 -- nullable; prior value for corrections/assigns
  after_value   TEXT,                 -- nullable; new value
  reason        TEXT,                 -- nullable; denial / absence reason
  actor_upn     TEXT,                 -- NULL ⇒ auto-resolved / system
  actor_name    TEXT,
  source        TEXT,                 -- 'inbox' | 'auto' | future surfaces
  detail        JSONB,                -- nullable; per-kind extras / future-proofing
  reversible    BOOLEAN NOT NULL DEFAULT FALSE,
  undone_at     TIMESTAMPTZ,          -- set when this event is undone
  undo_event_id INTEGER,              -- the 'undo' event that reversed this one
  resolved_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS inbox_events_resolved_at_idx ON inbox_events (resolved_at DESC);
CREATE INDEX IF NOT EXISTS inbox_events_actor_idx       ON inbox_events (actor_upn);
CREATE INDEX IF NOT EXISTS inbox_events_item_idx        ON inbox_events (item_kind, item_key);
```

Added to [`_schema.py`](../../../src/zira_dashboard/_schema.py) with the project's
idempotent `CREATE TABLE IF NOT EXISTS` convention, alongside the other inbox
tables (~L846–906).

New module `inbox_log.py` (mirrors the shape of
[`time_off_audit.py`](../../../src/zira_dashboard/time_off_audit.py)):

- `record_event(*, item_kind, item_key, person_name, category_label, action, outcome, before_value=None, after_value=None, reason=None, actor_upn=None, actor_name=None, source='inbox', reversible=False, detail=None) -> int` — inserts one row, returns its `id` (needed so the action response can offer undo).
- `archive(*, before=None, actor_upn=None, include_auto=False, limit=200) -> list[dict]` — newest-first, optionally filtered by actor and by whether to include `auto_resolved` rows; `before` is a `resolved_at` cursor for "show earlier".
- `mark_undone(event_id, undo_event_id) -> None`.
- The archive endpoint groups rows by **plant-local day** for the Today/Yesterday/Earlier headers.

### 2. `inbox_open_items` open-set mirror

```sql
CREATE TABLE IF NOT EXISTS inbox_open_items (
  item_key      TEXT PRIMARY KEY,
  item_kind     TEXT NOT NULL,
  person_name   TEXT,
  category_label TEXT,
  priority      TEXT,                 -- urgent | warn | info | muted (drives tier + tone)
  snapshot      JSONB NOT NULL,       -- enough to render the row + compute sort
  first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

This is **bookkeeping, not the primary source** — the live queue is still computed
fresh from the six sources. On each reconcile (see Component 5) the mirror is
upserted to match the fresh open set; departures drive `auto_resolved` events.
`item_key` is the join between the mirror and the log.

**`item_key` scheme** (stable per item):

| kind | key |
|---|---|
| time_off | `time_off:{request_id}` |
| missing_wc | `missing_wc:{attendance_id}` |
| missed_punch_out | `missed_punch_out:{attendance_id}` |
| late | `late:{kind}:{emp_id}:{day}` (kind = scheduled/unscheduled/reason) |
| assignment | `assignment:{wc_name}:{first_iso}` |
| plant_schedule | `plant_schedule:{day}` |

These derive directly from the `row_key` values
[already built](../../../src/zira_dashboard/exception_inbox.py#L80) in the snapshot.

### 3. Combined queue (refactor `build_snapshot`)

`build_snapshot()` keeps composing the six sources but additionally produces a
flat `queue` list:

- **Flatten** every section's rows into one list; attach `category_label`, a
  `tone`/tag color (from the section's existing `tone`), the `item_key`, and a
  `sort_key`.
- **Sort:** by urgency tier (`urgent` → `warn` → `info`/`muted`) then ascending
  time within tier (Decision: Option A). Tier derives from the existing per-row
  `priority` field.
- **Hide-empty:** sections contributing no rows add nothing. No "All clear" cards.
  When the whole queue is empty, the template shows a single **"Inbox zero"** state.
- The existing **focus strip** (All / Urgent / Follow-up) stays; snoozed items are
  `muted` and sit at the bottom as follow-up. The per-category **count tiles**
  slim to a compact chip row (the section headers that hosted them are gone).
- Rows keep their **exact inline controls** from today; only the wrapper changes.
- The few bulk tools that lived in section headers (the "Manage" modals) move into
  a small queue toolbar.

A helper `compute_open_items()` is factored out so the same open-set computation
feeds both rendering (here) and the reconciler (Component 5), guaranteeing they
agree.

### 4. Action handlers — actor capture + event write

Every handler that resolves an item gains: (a) actor from
`request.state.user_upn`/`user_name`, (b) one `inbox_log.record_event(...)` with
the right `action`, `outcome`, and before/after, and (c) removal of the item's
`inbox_open_items` row in the same step. The middleware prefixes non-human callers
`device:`/`ip:`, so the trail distinguishes a person from a TV automatically.

| Category | Handler | Event written |
|---|---|---|
| Time off | [exceptions.py](../../../src/zira_dashboard/routes/exceptions.py#L259) approve/refuse | already actor+`time_off_decisions`; **add** `inbox_events` (`approve`/`deny`, reason, `reversible=True`) |
| Missing WC | [routes/missing_wc.py](../../../src/zira_dashboard/routes/missing_wc.py#L54) assign/dismiss; [missing_wc.resolve](../../../src/zira_dashboard/missing_wc.py#L62) | `assign` (after_value = WC; before = prior/none) / `dismiss`; pass actor into `resolve(...)` |
| Missed punch | [routes/missed_punch_out.py](../../../src/zira_dashboard/routes/missed_punch_out.py#L29) correct; [missed_punch_out.correct](../../../src/zira_dashboard/missed_punch_out.py#L155) | `correct` (before = auto-closed/midnight time, after = corrected time); pass actor through |
| Late / absence | [routes/late_report.py](../../../src/zira_dashboard/routes/late_report.py#L36) declare-absent / save-late-arrival / snooze | `absent` (reason) and `reason` archive; **snooze writes no event** (item stays open, quieted) |
| Assignments | the staffing "credit" action (paired with [`assignments_todo_payload`](../../../src/zira_dashboard/routes/staffing.py#L678); exact endpoint confirmed in implementation) | `assign`/credit event (after = person credited) |
| Plant schedule | resolved by publishing on the staffing page — no inbox action | captured as `auto_resolved` by the reconciler |

The suppression tables (`missing_wc_resolved`, `missed_punch_out`, etc.) gain
optional `actor_upn`/`actor_name` columns too, so each table is self-describing;
but the **archive reads `inbox_events`**, not the union of those tables.

### 5. Reconcile + auto-resolve

Runs on the existing background tick in
[app.py](../../../src/zira_dashboard/app.py#L150) (which already calls
`missing_wc.write_cache` and `missed_punch_out.run_close`), so no new scheduler:

1. `compute_open_items()` produces the current open set (same computation the page
   uses).
2. Diff against `inbox_open_items`:
   - **present now, absent before** → upsert (new arrival).
   - **present now and before** → bump `last_seen`.
   - **absent now, present before** → the item left. If **no** `inbox_events` row
     exists for that `item_key` since its `first_seen` (i.e. no human handled it),
     write `inbox_events(action='auto_resolved', actor_upn=NULL, source='auto')`
     and delete the mirror row. If a human event already exists (the handler wrote
     it and deleted the row), there is nothing to do.

This dedupe-by-`item_key` is what prevents a human action and the reconciler from
both logging the same resolution.

### 6. Archive UI + endpoint

- Collapsed bar under the queue → expands to the day-grouped list (read from
  `inbox_events`): action icon, name + tag, the outcome line ("Approved by Dale
  Gruber"), before/after, denial reason, time. `auto_resolved` rows render muted.
- **Filters:** "Show: everyone / [actor]" and "Hide auto-resolved" (default on).
- **"Show earlier"** pages older day-groups via a `resolved_at` cursor.
- `GET /api/exceptions/archive?before=<iso>&actor=<upn>&include_auto=<bool>` returns
  rows grouped by plant-local day. These are exactly the queries the future audit
  page (Non-Goals) will reuse.

### 7. Live updates + undo

**Live** — the page polls a light delta endpoint every few seconds (extending the
existing summary poll the page already does). The endpoint returns the current
open queue (keys + render data + sort) plus the head of the archive. The client
diffs against the DOM: new keys **slide in**; missing keys **animate down** into
the archive (attributed to whoever resolved them). **Stale-action safeguard:** if a
row's underlying target changed since render (key present but action payload
differs), its controls are disabled and the row re-rendered before any click can
fire.

**Undo** — a successful action returns its `event_id`; the row shows "Done · Undo"
with a ~5-second toast and stays undoable for a short window.
`POST /api/exceptions/undo/{event_id}`:

- Local actions (dismiss, save reason): delete the local row; re-open the item.
- Assign WC: reset the attendance work center to `before_value` (or clear).
- Correct punch / approve / deny: where the Odoo API cleanly supports the reverse
  state transition, perform it; otherwise undo **re-opens** the item in the queue
  for a re-decision and the trail notes "reverted."
- Always writes an `inbox_events(action='undo', actor=...)` row and sets the
  original event's `undone_at`/`undo_event_id`. Re-adds the `inbox_open_items` row
  if the item should reappear.

## Data flow

**Human action.** guard → perform the underlying op (Odoo call / local write) →
update the existing source-of-truth (mirror state, suppression row, etc.) →
`inbox_log.record_event(...)` with actor + before/after → delete the
`inbox_open_items` row → return JSON including `event_id`. The client animates the
row into the archive. (Time-off additionally writes `time_off_decisions`, as today.)

**Auto-resolve.** tick → `compute_open_items()` → diff vs `inbox_open_items` →
for departures with no human event since `first_seen`, write `auto_resolved` and
delete the mirror row. Next client poll animates the row into the archive, muted.

**Undo.** load event → verify reversible, within window, not already undone →
reverse (local restore / Odoo flip / re-open) → write `undo` event → mark original
undone → re-add open item if it should reappear.

## Edge cases & error handling

- **Two managers, one item.** The first handler deletes the `inbox_open_items` row
  and writes the event; the second finds the item already resolved (existing
  handlers are idempotent/guarded, e.g. time-off state guards) and no-ops. The
  open-item delete is the race guard.
- **Reconciler vs human race.** If the tick runs between a handler's *perform* and
  its *delete*, the dedupe-by-`item_key` (no `auto_resolved` when a human event
  exists since `first_seen`) prevents a double log.
- **Event-write failure after a successful op.** The underlying op is the source of
  truth and the existing pollers reconcile state regardless; a failed
  `record_event` is logged, leaving at worst a missing archive row (never a claimed
  decision that didn't happen) — consistent with the time-off chatter-post posture.
- **Snooze.** Stays in the live queue as `muted` follow-up; writes no event. When
  the snooze expires it re-surfaces (no event). If the underlying lateness later
  clears on its own, that departure is logged as `auto_resolved`.
- **Non-person items.** Plant Schedule and Assignments use a label
  (work center / schedule day) in `person_name`; the audit line reads naturally
  ("Assigned to Saw 1", "Schedule published — auto-resolved").
- **History survives deletion.** `inbox_events` is denormalized with no FK, so Odoo
  deletes / suppression-table prunes never remove history.
- **Undo expiry / already-undone / not reversible.** Each returns a clear message;
  a non-reversible Odoo action falls back to re-open rather than faking a rollback.
- **Retention.** Events are retained indefinitely (tiny rows); the inline archive
  queries a bounded recent window and pages back. A prune job is optional/future.

## Testing

Mirrors the existing style (Odoo mocked), extending
[test_exception_inbox.py](../../../tests/test_exception_inbox.py) and the
[test_missed_punch_out_db.py](../../../tests/test_missed_punch_out_db.py) /
`test_missing_wc_*` patterns:

- **`inbox_log`** — record/read; archive grouping by day; actor filter; hide-auto
  filter (null actor → auto bucket).
- **Queue** — sort by tier then time; hide-empty; inbox-zero; tag/tone mapping;
  `item_key` stability against `row_key`.
- **Handlers** — each writes the correct event and removes the open item:
  missing-WC assign records before/after WC; missed-punch correct records
  before/after time; late absent/reason; time-off approve/deny dual-writes
  `inbox_events` + `time_off_decisions`; assignment credit. Snooze writes **no**
  event and leaves the item open.
- **Reconcile** — open-set diff emits `auto_resolved` exactly once; dedupes against
  a human event; arrivals/`last_seen` upsert correctly.
- **Undo** — per-action reversal; re-open fallback for non-reversible Odoo;
  `undo` event written and original marked; expired/duplicate undo guarded.
- **Live endpoint** — delta shape; stale-action guard disables a changed row.

## Non-Goals & future work

- **Dedicated Audit page (the "C later").** A filterable, exportable history view
  (by person, by acting manager, by type, by date range). It is a different query
  against the same `inbox_events` table — no schema rework, deferred by decision.
- **Approver / role gating.** Out of scope, consistent with the time-off approvals
  decision: any authenticated `gruberpallets.com` user can act on anything; the
  `actor_upn` log is the accountability mechanism. Actor data is captured to build
  gating on later if wanted.
- **Push transport (WebSocket/SSE).** Live updates ride the existing polling
  cadence, not a new socket layer.
- **Folding `time_off_decisions` into `inbox_events`.** Kept separate (dual-write)
  to avoid churning a just-shipped, tested feature.

## Build order (shippable phases)

1. **Audit log on the four inbox-native actions** — `inbox_events` + `inbox_log` +
   actor capture and event writes wired into the **time-off, missing-WC,
   missed-punch, and late** handlers (the mutators with dedicated route files).
   Snooze writes no event. Delivers the who/when audit trail for the bulk of daily
   manager actions, with no UI change yet.
2. **Combined queue + archive (+ assignment-credit logging)** — flat sorted
   hide-empty queue, collapsed day-grouped archive with filters and "show earlier";
   and, since this phase refactors the assignments surface, event logging for the
   staffing "credit" action (`POST /api/staffing/attribute`, threaded with
   `source='inbox'`). The visible feature.
3. **Undo** — the post-action window + per-action reversal.
4. **Fully-live + auto-resolve** — `inbox_open_items` mirror, reconcile on the
   background tick, polling/diff client with the stale-action safeguard.
   Plant-schedule resolutions, and any item that clears itself, are logged here as
   `auto_resolved`.
5. *(Later)* **Audit page (C)** — filterable/exportable view over `inbox_events`.
