# Timeclock ↔ Odoo State Reconciliation — Design

**Date:** 2026-06-01
**Status:** Implemented — all 8 tasks coded & committed, `py_compile` + `ast` clean. Authoritative `pytest` run happens in CI; the manual Railway checks (below) are pending the push to `main`.

## Context

The Timeclock kiosk decides which screen to show — **clock-in** vs
**clock-out** — from the most recent row in the local `timeclock_punches_log`
table. `_current_state()` (`src/zira_dashboard/routes/timeclock.py`) is a
~5ms local SELECT, chosen deliberately over a ~200-500ms Odoo XML-RPC call so
the tap stays instant. Odoo `hr.attendance` is the stated system of record, but
on the *read* path the local log is the de-facto source of truth.

That creates a blind spot. When attendance is changed **directly in Odoo**, the
kiosk never sees it:

- **Forgot to punch in → punch added manually in Odoo.** The local log has no
  `clock_in` row, so the kiosk shows the **clock-in** screen at end of day. If
  the employee taps it, `clock_in()` (`src/zira_dashboard/odoo_client.py`)
  does a blind `create` — producing a **second open attendance** in Odoo, or
  tripping Odoo's "already checked in" guard (→ a sync error).
- **Punch closed/deleted manually in Odoo.** The local log still shows them
  clocked in, so the kiosk shows clock-out when they're actually clocked out in
  Odoo.

The **write** side is already half-correct: on clock-out,
`_retry_one()` (`src/zira_dashboard/timeclock_sync.py`) calls
`get_current_attendance()` and closes whatever is open in Odoo — including a
manually-created row. So the gap is mostly the *read*/screen decision; once the
screen is right, the existing close logic does the right thing. Clock-in is the
exception — it creates blindly.

**Time off already solves the analogous problem.**
`poll_odoo_leaves()` (`src/zira_dashboard/time_off_sync.py`) runs every 60s
(wired in `src/zira_dashboard/app.py`) and pulls every `hr.leave`
in a rolling window into the local `time_off_requests` mirror: it **inserts**
HR-entered leaves (`originating_kiosk_user=FALSE`) and **marks** locally-present
leaves that vanished from Odoo as `state='cancel'`. The kiosk and Who's-Out
calendar read the local mirror. This is the same background-poll pattern this
design applies to punches — bringing clock-in/out up to parity — plus one change
to the time-off delete behavior (hard delete, below).

## Goals

1. The kiosk punch screen reflects Odoo's true open/closed attendance state,
   including punches **added, closed, deleted, or time-edited directly in
   Odoo**, without any Odoo XML-RPC call on the tap path.
2. A tap can never corrupt Odoo state: clock-in never creates a duplicate open
   attendance; clock-out always closes the actually-open row (even a
   manually-created one).
3. Right after a kiosk punch, the screen must **not** flicker to the wrong state
   while the background cache catches up.
4. Time off: a leave **deleted in Odoo** is **hard-deleted** from the local
   mirror (the request row is removed), while the scheduler reverse-cascade and
   balance invalidation still fire so the audit log and balances stay correct.
5. Graceful degradation: if Odoo or the warmer is down, the kiosk falls back to
   today's local-log behavior rather than blanking everyone to "clocked out."

## Non-goals

- **No live Odoo call on the read path or the tap.** Freshness comes from a
  background warmer; up-to-~30s staleness on the punch screen is accepted
  (invisible for the motivating "added hours ago" case).
- **No new approval/notification logic.** This is pure state reconciliation.
- **No reconciliation of historical/closed attendance** (daily totals, hours
  math). Only the *current open* state drives the screen.
- **No change to rounding, transfers, time-off submission, or the kiosk UI
  flow** beyond showing a correct screen and a WC fallback label.
- **No webhooks.** Background polling is sufficient, matching the time-off poller.

## Architecture

Two record types, one pattern — *Odoo is authoritative; a local cache makes the
read fast; writes self-correct in the background.*

```
                 ┌──────────────────────────────────────────┐
   Odoo  ◄──────►│  background warmer / poller (asyncio loop) │
(hr.attendance,  │  pulls authoritative state every 30–60s    │
 hr.leave)       └───────────────┬────────────────────────────┘
                                 ▼ writes local cache/mirror
   ┌─────────────────────────────────────────────────────────┐
   │  Postgres: odoo_open_attendance_cache  (NEW, punches)     │
   │            time_off_requests           (existing, leaves) │
   └───────────────┬─────────────────────────────────────────┘
                   ▼ ~5ms local read, no Odoo on hot path
   kiosk screens read the cache/mirror, reconciled against the
   local write-ahead log for very-recent (not-yet-synced) punches
```

- **Punches (new work).** A warmer mirrors Odoo's *open* attendances into a new
  single-row cache; `_current_state()` reconciles that cache against the local
  punch log; `_retry_one()` makes clock-in self-correcting.
- **Leaves (mostly existing).** The 60s poller already mirrors adds and
  detects deletions; we change the delete handling from soft-cancel to hard
  delete.

---

## Part 1 — Punch state reconciliation (read path)

### New table: `odoo_open_attendance_cache`

A single-row snapshot, modeled on the existing `live_cache`
(`src/zira_dashboard/live_cache.py`) tables. A single row gives
us one **global `refreshed_at`** — essential because "this person has no entry"
must mean "Odoo shows them clocked out" only when the snapshot is known-fresh.

```sql
CREATE TABLE IF NOT EXISTS odoo_open_attendance_cache (
  id            INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- enforce single row
  snapshot      JSONB NOT NULL DEFAULT '{}'::jsonb,            -- see shape below
  refreshed_at  TIMESTAMPTZ
);
```

`snapshot` shape — keyed by `person_odoo_id` (string keys, JSON):

```json
{
  "412": {"att_id": 88123, "check_in": "2026-06-01T11:02:00+00:00", "wc_name": "Bay 3 Nailer"},
  "417": {"att_id": 88130, "check_in": "2026-06-01T12:15:00+00:00", "wc_name": null}
}
```

(`wc_name` is null for a manually-added Odoo punch, which won't have the kiosk
WC custom field set.)

*Alternative considered:* a normalized table keyed by `person_odoo_id`. Rejected
as primary because the absence of a row can't distinguish "clocked out" from
"cache never ran" without a separate freshness marker — the single-row snapshot
solves that natively and matches the house `live_cache` pattern.

### Warmer / refresh loop

A new asyncio loop in `app.py`, sibling to `_warm_live_cache_loop`, runs every
**~30s**:

```python
async def _warm_odoo_attendance_loop():
    while True:
        try:
            await asyncio.to_thread(live_cache.refresh_odoo_open_attendance)
        except Exception as e:        # never let the warmer kill itself
            _log.warning("Odoo open-attendance refresh failed: %s", e)
        await asyncio.sleep(30)
```

`refresh_odoo_open_attendance()` does **one** cheap query —
`hr.attendance search_read [("check_out","=",False)]`, fields
`["id","employee_id","check_in", <kiosk WC field>]` — builds the snapshot dict,
and UPSERTs the single row with `refreshed_at = now()`. The WC field is included
only when `odoo_client._kiosk_wc_field()` is configured. Returns every
clocked-in person plant-wide (a few dozen rows); negligible Odoo load.

On failure the previous good snapshot stays in place (the loop swallows and
logs), so a transient Odoo blip degrades to stale-but-present, then to local
fallback once it crosses the stale threshold.

### Reconciliation rule in `_current_state()`

`_current_state(person_odoo_id)` keeps its existing return contract
(`is_clocked_in`, `current_wc`, `check_in_ts`, `open_odoo_attendance_id`) — its
sole caller is `timeclock_dashboard`, so the blast radius is one route. It now
reads two **local** sources (both ~5ms, no Odoo): the new cache and the latest
`timeclock_punches_log` row.

**Decision procedure:**

1. **Cache missing or stale** (`refreshed_at IS NULL` or older than the stale
   threshold, ~2–3 min — reuse the `live_cache.is_stale` idea) → fall back to
   the **current local-log logic** unchanged. Safe degradation.
2. Otherwise, decide who is authoritative for *this* person:
   - **Trust the cache** when the latest local punch is `None`, **or** it is
     `synced_to_odoo = TRUE` **and** `cache.refreshed_at > punch.synced_at`
     (the cache was refreshed after that punch reached Odoo, so it already
     reflects it).
   - **Trust the local log** otherwise — i.e. the latest punch is still
     unsynced, or the cache was last refreshed *before* that punch finished
     syncing. **This is the race-guard** that prevents a wrong-screen flicker
     right after a kiosk punch.
3. **If cache wins:** person present in snapshot → clocked in
   (`current_wc` = entry `wc_name`, may be null; `check_in_ts` = entry
   `check_in` — so an Odoo-edited check-in time shows through;
   `open_odoo_attendance_id` = entry `att_id`). Absent → clocked out.
4. **If local wins:** today's logic — last action `clock_in`/`transfer_in` →
   clocked in at that `wc_name`; else clocked out.

The `punch.synced_at` comparison is exact because both `synced_at` and
`refreshed_at` are server-side `now()` timestamps. `timeclock_punches_log`
already has the `synced_at` column (set by `_mark_synced`), so no schema change
to the log.

**Case matrix:**

| Situation | latest local punch | cache (fresh) | Screen |
|---|---|---|---|
| Forgot to punch in; added in Odoo hours ago | none | open | **clock-out** ✅ |
| Just clocked in at kiosk 5s ago (cache lags) | unsynced or synced-after-refresh | (not yet) | **stays clocked in** ✅ |
| Kiosk clock-in synced, cache caught up | synced, `refreshed_at > synced_at` | open | clocked in ✅ |
| Punch closed/deleted manually in Odoo | synced earlier | empty | **clock-in** ✅ |
| Normal: clocked out, no punch yet | none | empty | clock-in ✅ |
| Odoo / warmer down | any | stale/missing | local-log fallback ✅ |

---

## Part 2 — Self-correcting writes

`_retry_one()` in `timeclock_sync.py` runs in the background (both the immediate
`sync_one_by_id` task and the 60s `retry_unsynced_punches` sweep), so changes
here add **zero latency to the tap**.

- **clock-out / transfer-out** — unchanged; already queries
  `get_current_attendance()` and closes whatever is open in Odoo (handles
  manually-created rows correctly).
- **clock-in / transfer-in** — make symmetric. Before creating, call
  `get_current_attendance(person_odoo_id)`:
  - **Open attendance already exists** (manual add, or a stale-window
    double-tap) → **do not create.** Adopt the existing attendance id onto the
    log row via `_mark_synced(log_id, existing_id)` so a later clock-out closes
    the right row. If the existing row has no WC and the punch carries one,
    write the WC onto it (labels a manually-added punch once the employee acts).
  - **Nothing open** → `clock_in(...)` create, exactly as today.

Net effect with Part 1: the screen is right within ~30s of any Odoo edit, and a
duplicate open punch is **structurally impossible** even if a tap slips through
the sub-30s window before the cache catches a manual add.

---

## Part 3 — Time off: hard delete on Odoo deletion

Today `_mark_missing_as_cancel()` (`src/zira_dashboard/time_off_sync.py`)
soft-cancels (sets `state='cancel'`) any local row whose `odoo_leave_id` is no
longer returned by Odoo. Change this to a **hard delete**, per the approved
decision.

**New behavior** (rename to `_delete_missing_from_odoo()`; update its docstring,
the module docstring, and the `poll_odoo_leaves` docstring that describe the
cancel behavior):

For each candidate row (selection unchanged: `odoo_leave_id IS NOT NULL`,
`state NOT IN ('cancel','refuse')`, overlapping the poll window) **not** in
`seen_ids`:

1. Fire `cascade_on_state_change(row, {**row, "state": "cancel"})` **first** —
   so an approved leave still logs its `time_off_canceled` reverse row to
   `scheduler_moves` and invalidates the person's balance. The request row is
   gone, but `scheduler_moves` keeps the breadcrumb that the time off was
   removed.
2. `DELETE FROM time_off_requests WHERE id = %s`.
3. (Nicety) invalidate the person's balance regardless of prior state, so a
   deleted *pending* leave frees its `available_practical` immediately rather
   than waiting for the 10-min balance sweep. Idempotent.

**Safeguards preserved / noted:**

- Rows with `odoo_leave_id IS NULL` (kiosk drafts not yet pushed) are never
  touched — the existing filter already excludes them.
- A leave still present in Odoo is in `seen_ids` and skipped, so a locally
  pending `draft_edit`/`draft_cancel` is not clobbered.
- If Odoo deletes a leave while a local edit was pending, the deletion wins
  (row removed). Acceptable — Odoo is the source of truth.
- Deletion detection remains scoped to the poll window
  (`today − 60d … today + 365d`); a leave entirely outside the window being
  deleted is not detected. Pre-existing limitation, unchanged.

---

## Edge cases

- **Manual Odoo punch has no WC** → cache `wc_name` is null. Dashboard shows
  "Clocked in" with WC rendered as "—"; clock-out works regardless; a transfer
  assigns a WC going forward. (Optional future nicety: seed the display with the
  person's scheduled WC as a hint.)
- **Cold start** → before the warmer's first tick, `refreshed_at IS NULL` →
  local-log fallback (today's behavior). First successful refresh flips it to
  cache-authoritative.
- **Clock skew** → none to worry about; `synced_at` and `refreshed_at` are both
  set by the same Postgres `now()`.
- **Multiple open attendances in Odoo for one person** (pre-existing data mess)
  → `get_current_attendance` already returns the most recent (`limit=1`); the
  cache snapshot stores one entry per person. Clock-out closes the one Odoo
  reports; the warmer reflects whatever remains open next tick.
- **Person without `odoo_id`** → unchanged; `_current_state(-1)` path, no cache
  entry, local-log logic.

## Testing strategy

Per the local constraint (Python 3.9, suite runs in CI/Railway, local verify via
`py_compile` + ast-exec):

- **Reconciliation rule (pure logic, no Odoo):** matrix over
  {no local punch / recent-unsynced / synced-before-refresh / synced-after-refresh}
  × {cache open / cache empty / cache stale / cache missing}. Assert the screen
  and the `current_wc` / `check_in_ts` / `open_odoo_attendance_id` outputs.
- **Self-correcting clock-in (`_retry_one`):** existing-open → adopts id, no
  `create` call, optional WC label; nothing-open → `create`. Mocked
  `odoo_client`.
- **Warmer (`refresh_odoo_open_attendance`):** maps Odoo `search_read` rows →
  snapshot dict (incl. null WC), UPSERTs single row, sets `refreshed_at`;
  includes the WC field only when configured.
- **Time-off hard delete:** Odoo-missing approved leave → reverse cascade fires
  (`scheduler_moves` reverse row + balance invalidation) **then** the row is
  gone; Odoo-missing pending leave → row gone + balance invalidated, no
  scheduler reverse. **Update** existing `tests/test_time_off_sync.py`
  expectations that assert `state='cancel'` for the missing path to assert the
  row is deleted. Employee-initiated cancel/refuse cascade tests are unaffected.

## Done criteria

_Code for every item is implemented and committed (`py_compile`/`ast` clean). Checkboxes get ticked once the CI test run is green and the manual Railway checks pass — both gated on the push to `main`._

- ☐ `odoo_open_attendance_cache` table + warmer loop shipping; one Odoo query
  per ~30s tick.
- ☐ `_current_state` reconciles cache vs. local log per the rule; no Odoo call
  on the read path; race-guard verified.
- ☐ Manual Odoo add → kiosk shows clock-out within ~30s; tapping it closes the
  manual row (no duplicate).
- ☐ Manual Odoo close/delete → kiosk shows clock-in within ~30s.
- ☐ Clock-in can never create a duplicate open attendance.
- ☐ Odoo / warmer down → local-log fallback, no false "clocked out."
- ☐ Leave deleted in Odoo → local `time_off_requests` row hard-deleted; reverse
  cascade + balance invalidation still fire.
- ☐ Tests above passing in CI.

## Open Questions

(None at design time — freshness mechanism, write-safety placement, and the
time-off delete semantics were settled during brainstorming and folded into the
sections above. Revisit during planning if new questions surface.)
