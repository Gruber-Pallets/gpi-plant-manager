# Time-Off Resolution Notifications & Day-Before Reminder

**Date:** 2026-06-29
**Status:** Approved (design)

## Problem

Employees file time-off requests at the kiosk (or HR enters them in Odoo), but they
have no feedback loop telling them what happened. When a request is approved, denied,
or cancelled, the employee currently has to ask someone or guess. And there's no
"don't forget" nudge before time off actually begins.

Give employees two pieces of closure at the one place they reliably show up — the
time clock:

1. **Resolution popup** — the next time they sign in after a request is resolved,
   show a popup telling them it was approved, denied, or cancelled.
2. **Day-before reminder** — when they clock out on the last working day before
   approved time off, show a "final warning" reminding them they're off next.

## Decisions (from brainstorming)

- **Triggers:** approved, denied, **and** cancelled (cancelled only when it is *not*
  the employee's own cancellation).
- **Source:** all of the employee's time off — kiosk-filed *and* HR/manager-entered.
- **Dismissal:** tap-to-acknowledge; recorded per notification so it never repeats.
- **Reminder scope:** full-day *and* partial-day leaves (late arrival, early leave,
  midday gap).
- **Architecture:** dedicated `employee_notifications` table for resolution popups;
  the day-before reminder is computed live at clock-out (no stored row).

## Architecture

### 1. Data model — `employee_notifications`

One row per thing to tell an employee. Snapshots leave dates so the message stays
correct even if the source request later changes or is deleted from the local mirror.
Added to `_schema.py` (idempotent bootstrap, same as every other table).

```sql
CREATE TABLE IF NOT EXISTS employee_notifications (
  id                  BIGSERIAL PRIMARY KEY,
  person_odoo_id      INTEGER NOT NULL,
  kind                TEXT NOT NULL,          -- time_off_approved | time_off_denied | time_off_cancelled
  time_off_request_id BIGINT,                 -- source row in time_off_requests
  odoo_leave_id       INTEGER,
  title               TEXT NOT NULL,
  body                TEXT NOT NULL,
  leave_date_from     DATE,
  leave_date_to       DATE,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  acknowledged_at     TIMESTAMPTZ
);

-- hard dedupe backstop: at most one notification per (request, kind)
CREATE UNIQUE INDEX IF NOT EXISTS employee_notifications_dedupe
  ON employee_notifications (time_off_request_id, kind);

-- fast unacknowledged lookup at sign-in
CREATE INDEX IF NOT EXISTS employee_notifications_unack
  ON employee_notifications (person_odoo_id) WHERE acknowledged_at IS NULL;
```

A new module `employee_notifications.py` wraps it:

- `create_time_off_notification(person_odoo_id, kind, request_row) -> int | None`
  — renders title/body, inserts with `ON CONFLICT (time_off_request_id, kind) DO NOTHING`.
- `list_unacknowledged(person_odoo_id) -> list[dict]`
- `acknowledge(notification_id, person_odoo_id) -> None`
  — person-scoped (won't ack a row that isn't theirs).

### 2. Generation — rides the existing state-change hook

Generation lives in `cascade_on_state_change(old, new, request_row)` in
`time_off_sync.py`, which already fires only on a real state transition (so re-polling
doesn't re-fire). Mapping:

| New state | Notification | Suppress when |
|-----------|--------------|---------------|
| `validate` | `time_off_approved` | — |
| `refuse`   | `time_off_denied`   | prior local state was `draft_cancel` |
| `cancel`   | `time_off_cancelled`| prior local state was `draft_cancel` |

**Self-cancel is not a denial.** When an employee cancels an already-approved request,
the push calls `action_refuse`, so Odoo reports `refuse`. The `draft_cancel`
suppression prevents a false "Denied" popup for the employee's own action.

**Insert-already-resolved.** HR can enter and approve in one step (e.g.
`absence_sync.mirror_approved_absence` inserts directly as `validate`). This is an
insert, not a transition, so `cascade_on_state_change` won't see it. Generation is
also called from the upsert insert path for rows that land directly in a notify-state.

**Guards (safety, modeled on the inbox-reconcile false-resolve guards):**

- **Future-only:** generate only when `leave_date_to >= today`. Past leaves are moot,
  and this is the natural backfill shield.
- **No-backfill-blast:** on the first full sync after deploy, do not generate for the
  whole existing dataset. Only generate for (a) transitions observed live during a
  poll and (b) inserts whose Odoo `write_date` is recent. Shipping the feature must
  not fire hundreds of historical popups.

### 3. Sign-in popup

Current flow: tap name → `/timeclock/start/{person_id}` mints a 60s HMAC token →
`/timeclock/dashboard/{token}`.

- The dashboard route (`timeclock_dashboard`) checks `list_unacknowledged(person_odoo_id)`.
  If any exist, it redirects to a new route **`/timeclock/notifications/{token}`**.
- The notifications screen (`timeclock_notifications.html`) stacks the cards:
  ✅ approved / ❌ denied / ⚠️ cancelled, each with the leave dates.
- A single **"Got it"** button POSTs to **`/timeclock/notifications/ack/{token}`**,
  which acks all currently-shown notifications (stamps `acknowledged_at`) and redirects
  to the dashboard.
- Tap-to-acknowledge means a fast sign-in cannot skip past it.
- If the 60s token expires mid-read, the employee re-taps their name and sees the
  still-unacknowledged notifications again — acceptable and arguably desirable.

### 4. Day-before clock-out reminder (live)

In `kiosk_clock_out()` (`routes/timeclock.py`), after the punch is closed and before
rendering the success screen:

1. Fetch the person's resource calendar (`odoo_client.fetch_resource_calendar`,
   10-min cached) to find their **next scheduled working day** after today.
2. Query approved leaves (full or partial) covering that day.
3. If found, render the clock-out confirmation with a prominent
   **"Heads up — time off tomorrow"** card that requires a tap to finish (no
   auto-redirect past it):
   - Full day → *"You're off tomorrow (Mon, Jun 30). See you Tuesday!"*
   - Partial → *"Approved late arrival tomorrow (Mon) — not due in until 10:00 AM."*

Nothing is stored; it is recomputed on each clock-out. Transfers (`kiosk_transfer`)
and auto-lunch sign-outs use different code paths, so only a real "I'm leaving"
clock-out triggers it. Re-showing on a second clock-out the same day is acceptable for
a "final warning."

**Fallbacks:** if no resource calendar is available, fall back to "next calendar day".
If the person has no `odoo_id`, skip silently.

## Edge cases

- Self-cancellation suppressed (see §2).
- Dedupe via observed-transition-only generation + unique `(request_id, kind)` index.
- No backfill blast on deploy (future-only + recent-`write_date` gating).
- Multi-day/consecutive leaves: reminder targets the next working day, so it reminds
  once about the start of a block; weekends skipped via the calendar.
- Missing calendar → next-calendar-day fallback for the reminder.
- Person without `odoo_id` → skip silently.
- Ack is person-scoped: the ack route verifies the token's `person_id` owns the
  notification.

## Enablement

Tied to the existing time-off feature (meaningless without it). One env kill-switch,
`KIOSK_TIME_OFF_NOTIFY_ENABLED` (default on when time-off is enabled), disables just
the popups/reminders without touching the rest of time off.

## Testing

Following existing patterns (`tests/test_time_off_sync.py`, route tests):

- **Generation rules:** approved/denied/cancelled produce the correct `kind`;
  `draft_cancel→refuse` and `draft_cancel→cancel` produce nothing; future-only guard;
  dedupe holds across re-polls; insert-already-resolved fires exactly once.
- **Reminder computation:** last-working-day math across a weekend; full vs. partial
  wording; no approved leave ⇒ no card; missing-calendar fallback.
- **Routes:** sign-in with unacked notifications redirects to the interstitial;
  "Got it" acks and lands on the dashboard; ack rejects a notification the token does
  not own; clock-out shows the reminder card when due.

## Files touched

- `src/zira_dashboard/_schema.py` — new `employee_notifications` table + indexes.
- `src/zira_dashboard/employee_notifications.py` — **new** module (create/list/ack).
- `src/zira_dashboard/time_off_sync.py` — generate notifications in
  `cascade_on_state_change` + the insert-already-resolved path.
- `src/zira_dashboard/routes/timeclock.py` — sign-in interstitial redirect, ack
  routes, day-before reminder at clock-out.
- `src/zira_dashboard/templates/timeclock_notifications.html` — **new** interstitial.
- `src/zira_dashboard/templates/timeclock_success.html` (or the clock-out path) —
  reminder card.
- `tests/` — new tests per above.

## Out of scope (YAGNI)

- Notification types beyond time-off resolution (the table generalizes, but we add
  only these three kinds now).
- Persisting/auditing the day-before reminder.
- Push/SMS/email — kiosk-only.
- Surfacing Odoo denial reasons (chatter) in the popup — possible later.
