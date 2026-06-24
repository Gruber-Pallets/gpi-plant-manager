# Time Off Approvals — Design

**Date:** 2026-06-24
**Status:** Approved (brainstorming → implementation planning)

## Context

Time-off requests live in Odoo (`hr.leave`). Employees and HR create them in
Odoo directly; the kiosk also creates them
([time_off_wizard.py](../../../src/zira_dashboard/time_off_wizard.py),
[time_off_sync.py](../../../src/zira_dashboard/time_off_sync.py)). Approving or
rejecting a request, however, has historically meant logging into Odoo.

The `codex/exception-inbox-release` branch already closed most of that loop. The
**Exception Inbox** ([`/exceptions`](../../../src/zira_dashboard/routes/exceptions.py))
surfaces a **Pending Time Off** section with **Approve** and **Deny** buttons per
request ([exceptions.html:171](../../../src/zira_dashboard/templates/exceptions.html#L171)).
Those buttons hit `POST /api/exceptions/time-off/{id}/approve|refuse`
([exceptions.py:143](../../../src/zira_dashboard/routes/exceptions.py#L143)), which
walk Odoo's real workflow via `odoo_client.approve_leave` / `refuse_leave`
([odoo_client.py:1175](../../../src/zira_dashboard/odoo_client.py#L1175)) and update
the local `time_off_requests` mirror. The leave poller
(`time_off_sync.poll_odoo_leaves`) pulls requests *created in Odoo* into the
mirror (`originating_kiosk_user=FALSE`), so the queue fills automatically. The
mechanics are sound and already tested
([test_exception_inbox.py:385](../../../tests/test_exception_inbox.py#L385)).

The remaining work is **scope, context, and accountability**, not Odoo
integration. Dale evaluated the existing flow and wants three gaps closed before
trusting it in production:

1. **A full pending queue.** The inbox shows at most **8** pending requests
   ([exception_inbox.py:66](../../../src/zira_dashboard/exception_inbox.py#L66)),
   and its "Open" link points to `/staffing/time-off` — a read-only calendar of
   *already-approved* leaves, not a pending queue. With 9+ pending (a holiday
   week), the rest are unreachable from the app. Stale **past-due** pending
   requests (`date_to < today`) also drop off the inbox entirely while still
   sitting open in Odoo.
2. **An audit trail and a required denial reason.** Odoo records the API service
   user as approver, not the manager who clicked, and the app keeps no record of
   who decided or when. Denials capture no reason.
3. **Decision context.** A row shows name, dates, type, and state — but not the
   employee's remaining balance or how many of their crew are already off. A
   manager can approve into a coverage hole or a negative balance blind.

**Explicitly out of scope:** approver gating (any authenticated `gruberpallets.com`
login can still approve/deny anyone). See [Non-Goals](#non-goals--future-work).

## Decisions

These were settled during brainstorming:

| Decision | Choice |
|---|---|
| Where the workflow lives | **Dedicated page** under Staffing **+** inbox keeps a compact fast-path (not removed). |
| Coverage scope | **Requester's department/crew**, derived from their default work-center membership; **plant-wide fallback** when no department resolves. |
| Denial reason | **Required** — a denial cannot be confirmed without one. |
| Reason destination | Stored in our audit log **and pushed to the employee** via the Odoo leave chatter. |
| Over-balance approval | **Warn, never block** — Odoo stays the authority on what is permitted. |

## Architecture overview

Four pieces, all reusing existing infrastructure:

1. **New page** `GET /staffing/time-off/approvals` — the full workspace: every
   pending request (no cap; past-due flagged), each with balance + coverage
   context, plus a "Recently decided" history list.
2. **Enhanced decision endpoints** — the existing approve/refuse handlers gain
   actor capture, a required reason on deny, audit-log writes, and the Odoo
   chatter post. Paths stay put to avoid churn (see
   [Endpoint location](#endpoint-location-keep-vs-rename)).
3. **New `time_off_decisions` table** — an append-only, **denormalized** audit
   log that survives the poller's hard-delete of mirror rows.
4. **Decision context** — balance and coverage computed from **local data only**
   (the `time_off_balances` cache and the `time_off_requests` mirror), so neither
   the page nor the inbox needs a live Odoo round-trip to render.

The Exception Inbox keeps its Pending Time Off section as a **fast-path**: its
"Open" link now points to the new page, one-click **Approve** stays, and **Deny**
expands an inline required-reason field (since a bare one-click deny is no longer
allowed).

## Components

### 1. Time Off Approvals page — `GET /staffing/time-off/approvals`

New route, new module `routes/time_off_approvals.py`, new template
`time_off_approvals.html`, linked under the Staffing nav.

**Pending list.** All requests in the mirror whose `state` is pending
(`draft`, `draft_edit`, `confirm`, `validate1`) — **no date filter**, so
past-due requests appear. Ordered by `date_from`. Each row renders:

- Person (name + initials), leave type, date range (and hours for partials).
- **Balance chip** — remaining balance for that leave type from the
  `time_off_balances` cache. Red when the request amount exceeds remaining
  (warning only; approve is never disabled).
- **Coverage chip** — count of *other* people in the requester's department
  with an approved leave overlapping the request dates (see
  [Coverage](#coverage-departmentscoped)). Neutral/green when nobody else is
  off; amber when others are.
- **State badge** — `To approve`, `Awaiting 2nd approval` (Odoo `validate1`), or
  a red **Past due** flag when `date_to < today`.
- **Actions** — Approve (one click) and Deny (expands the inline required-reason
  field, then Confirm deny).

**Recently decided.** Reads `time_off_decisions` for the last **30 days**,
newest first: approve/deny, person, type, dates, the denial reason if any, and
**who decided + when**.

The page reads only local stores, so it is cheap to render and safe to refresh.

### 2. Enhanced decision endpoints

The existing handlers in
[exceptions.py](../../../src/zira_dashboard/routes/exceptions.py) already own the
hard parts — state-machine guards, no-op handling, lazy sync-before-action, and
self-healing via the poller. We extend them rather than rewrite:

- **Capture the actor.** Add `request: Request` to each handler and read
  `request.state.user_upn` / `request.state.user_name`, which the auth
  middleware already sets on every authenticated request
  ([auth.py:279](../../../src/zira_dashboard/auth.py#L279)). The middleware
  prefixes non-human callers as `device:` / `ip:`, so the audit naturally
  distinguishes a human decision from a TV.
- **Require a reason on deny.** The refuse endpoint accepts a JSON body
  `{"reason": "..."}`. A missing/blank reason returns **400** before any Odoo
  call. (Approve takes no reason.)
- **Write the audit row.** On a successful decision, insert one
  `time_off_decisions` row (below), inside the same code path that updates the
  mirror state.
- **Push the reason to the employee.** After `refuse_leave` succeeds, post the
  reason to the leave's Odoo chatter so the employee is notified. Add
  `odoo_client.post_leave_message(leave_id, body)` wrapping
  `execute("hr.leave", "message_post", [leave_id], body=...)` — note `body` is a
  **keyword** arg, because the `execute` helper forwards `**kwargs` as Odoo's
  keyword args ([odoo_client.py:135](../../../src/zira_dashboard/odoo_client.py#L135));
  passing it positionally would land it in the wrong slot. The exact
  notify parameters (message subtype / `partner_ids`) so the employee is
  actually pinged — not just an internal log note — are confirmed during
  implementation against this Odoo instance. **This is best-effort:** if the
  chatter post fails, the denial still succeeds and the mirror/audit still
  update; we log the post failure rather than rolling back a completed refusal.

Both endpoints keep returning the same JSON shape they do today, so the existing
inbox JS keeps working; the page uses the same endpoints.

### 3. `time_off_decisions` audit table

Append-only. **Denormalized on purpose:** the poller hard-deletes mirror rows
when a leave is deleted in Odoo
([time_off_sync.py:328](../../../src/zira_dashboard/time_off_sync.py#L328)), so a
foreign key to `time_off_requests` would let history vanish. We snapshot enough
to stand alone.

```sql
CREATE TABLE IF NOT EXISTS time_off_decisions (
  id              SERIAL PRIMARY KEY,
  request_id      INTEGER,            -- mirror row id at decision time (not an FK)
  odoo_leave_id   INTEGER,            -- null for a never-synced draft denied locally
  person_odoo_id  INTEGER,
  person_name     TEXT,               -- snapshot
  leave_type      TEXT,               -- snapshot
  date_from       DATE,
  date_to         DATE,
  action          TEXT NOT NULL CHECK (action IN ('approve','deny')),
  result_state    TEXT,               -- resulting Odoo state: validate / validate1 / refuse
  reason          TEXT,               -- denial reason; null for approvals
  actor_upn       TEXT,
  actor_name      TEXT,
  source          TEXT,               -- 'page' | 'inbox'
  decided_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS time_off_decisions_decided_at_idx
  ON time_off_decisions (decided_at DESC);
```

Added to [`_schema.py`](../../../src/zira_dashboard/_schema.py) with the project's
idempotent `CREATE TABLE IF NOT EXISTS` convention.

### 4. Decision context

#### Balance (local cache)

Read remaining balance for `(person_odoo_id, holiday_status_id)` from the
`time_off_balances` table ([time_off_balances.py](../../../src/zira_dashboard/time_off_balances.py)),
which is already refreshed on every poll cycle and on a 10-minute sweep. Compare
against the request's amount (days or hours per its unit). Over-balance sets the
chip red but does **not** disable approve. If no balance row exists yet, show the
chip as "balance unknown" rather than implying zero.

#### Coverage (department-scoped)

The `people` table has no department column, but a person's "home crew" is
derivable: `work_center_default_people` → `work_centers.department`
([_schema.py](../../../src/zira_dashboard/_schema.py),
[work_centers_store.py](../../../src/zira_dashboard/work_centers_store.py)).

For each pending request:

1. Resolve the requester's department(s) from their default work-center
   membership.
2. Count **other** people who have an **approved** leave (`state='validate'`) in
   the `time_off_requests` mirror overlapping `[date_from, date_to]` and whose
   resolved department intersects the requester's.
3. **Fallback:** if the requester maps to no department (no default WC), count
   plant-wide instead and label the chip "(plant-wide)" so the scope is honest.

A small helper `department_for_person(person_odoo_id) -> set[str]` belongs in
[staffing.py](../../../src/zira_dashboard/staffing.py) alongside the existing
`department_for_wc`.

### 5. Inbox fast-path changes

In [exception_inbox.py](../../../src/zira_dashboard/exception_inbox.py) and the
inbox JS/template:

- The Pending Time Off section's link points to **`/staffing/time-off/approvals`**
  (today it points to the approved-leaves calendar). The 8-row teaser cap stays —
  it is a teaser now, with the full list one click away.
- **Approve** stays one click.
- **Deny** expands an inline required-reason field (the same component the page
  uses) instead of the old bare `confirm()` — because a reason is now required.
- The inbox row stays lean (no balance/coverage chips); full context lives on the
  page.

## Data flow

**Approve** (page or inbox): guard state → ensure synced to Odoo (lazy
`push_one` for kiosk drafts) → `approve_leave` walks `draft→confirm→validate1→
validate` → update mirror state → insert `time_off_decisions` row (`approve`,
`result_state`, actor). If Odoo returns `validate1` (two-step approval), the row
re-renders as **Awaiting 2nd approval** and a second approve advances it.

**Deny** (page or inbox): require non-blank reason (else 400) → if synced,
`refuse_leave` → best-effort `post_leave_message(reason)` to the chatter →
set mirror state `refuse` → insert `time_off_decisions` row (`deny`, reason,
actor). A never-synced local draft (no `odoo_leave_id`) is refused locally and
audited; there is no chatter to post to.

## Edge cases & error handling

- **Poller race.** The decision path (read → Odoo action → local update) is not
  row-locked, and the 60s poller may touch the same row. Operations are
  idempotent and convergent — `approve_leave` re-reads Odoo state between steps,
  and both writers land the same final state — so we **accept** this rather than
  add locking. (Unchanged from today's behavior.)
- **Partial failure.** Odoo action succeeds but the local update fails → the
  poller's `cascade_on_state_change` reconciles the mirror on the next tick.
  The audit row is written in the same step as the local update; if that step
  fails, no audit row is written (we never claim a decision we didn't record).
- **History survives deletion.** Because `time_off_decisions` is denormalized and
  has no FK, deleting the leave in Odoo (which hard-deletes the mirror row) leaves
  the decision history intact.
- **Chatter post failure** is logged and swallowed; it never rolls back a
  completed refusal (the refusal is the source of truth, the notification is a
  courtesy).
- **Over-balance** is a visual warning only; approve remains enabled.
- **No department** → plant-wide coverage count, labeled as such. **Multiple
  departments** (person is a default on WCs in more than one) → union of those
  departments.
- **Two-step approval** is handled end-to-end already; the page badge makes the
  intermediate `validate1` state legible.

## Testing

- **Endpoints:** reason-required (400 on blank), audit row written with actor on
  approve and deny, chatter-post failure does not fail the denial, never-synced
  draft deny path. Extends
  [test_exception_inbox.py](../../../tests/test_exception_inbox.py).
- **Audit survival:** a decision row persists after its mirror row is deleted.
- **Context helpers:** `department_for_person` resolution + fallback; coverage
  overlap count; over-balance flag computation.
- **Page route:** renders all pending (incl. past-due) with context, and the
  recent-decisions list.
- **Odoo client:** `post_leave_message` calls `message_post` with the body
  (mock execute), mirroring the existing leave-action tests in
  [test_odoo_client_leaves.py](../../../tests/test_odoo_client_leaves.py).

## Non-Goals & future work

- **Approver gating / roles.** Out of scope by decision. Any authenticated
  `gruberpallets.com` user can decide any request, as today. The
  `time_off_decisions.actor_upn` log is the accountability mechanism instead. If
  gating is wanted later, the actor data is already captured to build on.
- **Richer notifications** (email/Slack to the employee beyond the Odoo chatter
  post) are deferred.
- **Endpoint location (keep vs rename).** The handlers stay at
  `/api/exceptions/time-off/{id}/...` to avoid churn, even though they now serve
  two surfaces. A later cosmetic rename to `/api/time-off/{id}/...` is optional
  and low value.
- **Partial approval / date edits at decision time** are not included; a manager
  who wants different dates denies with a reason and the employee resubmits (or
  it is edited in Odoo).
