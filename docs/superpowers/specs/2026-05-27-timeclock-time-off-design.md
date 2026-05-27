# Timeclock Time Off Requests + Calendar — Design

**Date:** 2026-05-27
**Status:** Approved (brainstorming → implementation planning)

## Context

The Timeclock kiosk (Phase 0 pilot) currently handles clock-in / clock-out
/ mid-shift work-center transfers, writing punches to Odoo `hr.attendance`
via a local `kiosk_punches_log` mirror + BackgroundTask sync + 60s sweep
worker. The admin time-off calendar at `/staffing/time-off` is read-only
and sourced from StratusTime.

Dale wants to extend the kiosk so employees can submit time-off requests,
view the company-wide "who's out" calendar, and see their own pending /
approved / rejected requests with edit + cancel actions. Approvals stay
in Odoo's native `hr.leave` workflow. Once approved, leaves cascade
automatically into the existing staffing scheduler — populating the
Time-Off bucket for full-day leaves and writing partial-day working
hours into the existing `custom_day_hours` table so supervisors see
"Bob — working 6:00–10:00 + 12:00–14:30" on the scheduler.

The bigger frame: Odoo replaces StratusTime as the system of record for
time off (and eventually all time-clock data). This feature runs in
parallel with StratusTime during the pilot so nothing breaks; a settings
toggle switches the admin calendar from dual-source to Odoo-only at
cutover.

## Goals

1. Kiosk employees can submit time off in four shapes: **Full Day(s)**,
   **Arriving Late**, **Leaving Early**, **Mid-Day Gap**.
2. Submissions sync to Odoo `hr.leave` as draft records using the
   employee's configured leave type; approvals happen in Odoo's native
   two-stage flow (Employee's Approver + Time Off Officer).
3. Employee sees their pending / approved / rejected requests on the
   kiosk and can edit or cancel any of them.
4. Approved leaves auto-populate `/staffing/time-off` (admin calendar)
   and the staffing scheduler's Time-Off bucket within ~60s of approval.
5. Partial-day approvals (late / early / gap) auto-write working hours
   into `custom_day_hours` so the scheduler renders custom hours by
   the employee's name.
6. Kiosk shows live balance (allocated − taken − pending) and a live
   in-flight calculation so employees never submit a request that
   would exceed their balance.
7. Admin can hide specific leave types from the kiosk picker in settings.
8. Parallel-run mode overlays StratusTime entries on the admin calendar
   during the pilot; one-flip cutover when ready.

## Non-goals

- No notifications beyond what Odoo emails natively. Odoo's existing
  mail templates handle "request submitted" / "approved" / "rejected"
  notifications to the right parties.
- No webhook integration with Odoo for state changes. A 60s poller is
  sufficient.
- No allocation management UI. Allocations are granted in Odoo by HR.
- No replacement of the existing punch flow. This feature ships
  alongside, doesn't touch `kiosk_punches_log` or `hr.attendance`.
- No leave-type-specific approval routing in our app — we surface
  whatever approval flow Odoo's `hr.leave.type.allocation_validation_type`
  is configured for.
- No future-accrual projection. Balances shown are current; small-print
  copy disclaims accruals during the request window.

## Architecture

Four subsystems, all mirroring the existing punch architecture:

1. **Local mirror (Postgres)** — `time_off_requests` table is the
   authoritative read source for every UI surface. Submits, edits, and
   cancels write here first, render confirmation immediately, queue
   Odoo sync as a `BackgroundTask`.

2. **Odoo writer (background tasks)** — same shape as `kiosk_sync.py`.
   One immediate XML-RPC call per local mutation; failed rows retry on
   the existing 60s sweep loop. `sync_error` column records per-row
   structured error messages.

3. **Odoo poller (new background loop)** — every 60s, pulls all
   `hr.leave` records for active employees in a rolling window
   (`today - 60 days` to `today + 365 days`). Upserts state, dates,
   and hours into the local mirror. Catches three cases: (a) state
   changes from Odoo-side approvals, (b) HR-entered leaves added
   directly in Odoo, (c) deletions.

4. **Read fan-out** — kiosk, admin calendar, and staffing scheduler all
   read from the local mirror, never from Odoo directly. Up-to-60s
   staleness on approval state is accepted; a manual "Refresh now"
   button lets admins force-poll.

### Data flow

```
Kiosk submit → INSERT time_off_requests (state='draft', synced=FALSE)
            → render confirmation page
            → BackgroundTask: hr.leave.create(...)
                ↓ on success
            UPDATE row SET odoo_leave_id=..., state='confirm',
                           synced_to_odoo=TRUE, last_pushed_at=now()

(later, Dale approves in Odoo)
            ↓ (≤60s)
Poller cycle detects state='validate' on this leave
            → UPDATE local row state='validate'
            → cascade_on_approve():
                · staffing schedules: add person to TIME_OFF_KEY bucket
                  for [date_from..date_to]
                · custom_day_hours: write working_hours_json ranges
                  for partial-day shapes
            → calendar + scheduler render automatically on next request
```

## Data Model

### New table: `time_off_requests`

```sql
CREATE TABLE time_off_requests (
  id                       BIGSERIAL PRIMARY KEY,
  person_odoo_id           INT NOT NULL,
  originating_kiosk_user   BOOLEAN NOT NULL DEFAULT TRUE,
  shape                    TEXT NOT NULL,    -- 'full_day' | 'late_arrival' | 'early_leave' | 'midday_gap'
  holiday_status_id        INT NOT NULL,     -- Odoo hr.leave.type.id
  date_from                DATE NOT NULL,
  date_to                  DATE NOT NULL,
  hour_from                NUMERIC(4,2),     -- NULL for full_day
  hour_to                  NUMERIC(4,2),
  working_hours_json       JSONB,            -- computed working ranges, e.g. [{"from":6.0,"to":10.0},{"from":12.0,"to":14.5}]
  note                     TEXT,
  state                    TEXT NOT NULL DEFAULT 'draft', -- mirrors Odoo: draft|confirm|validate1|validate|refuse|cancel
  odoo_leave_id            INT,
  synced_to_odoo           BOOLEAN NOT NULL DEFAULT FALSE,
  sync_error               TEXT,
  last_pulled_at           TIMESTAMPTZ,
  last_pushed_at           TIMESTAMPTZ,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX time_off_requests_person_date_idx
  ON time_off_requests (person_odoo_id, date_from);
CREATE INDEX time_off_requests_range_idx
  ON time_off_requests (date_from, date_to);
CREATE INDEX time_off_requests_unsynced_idx
  ON time_off_requests (id) WHERE synced_to_odoo = FALSE;
CREATE INDEX time_off_requests_state_idx
  ON time_off_requests (state, date_from);
```

### Field mapping to Odoo `hr.leave`

| Local column        | Odoo `hr.leave` field        | Notes |
|---------------------|------------------------------|-------|
| `person_odoo_id`    | `employee_id`                | direct |
| `holiday_status_id` | `holiday_status_id`          | direct |
| `date_from`, `date_to` | `request_date_from`, `request_date_to` | direct |
| `hour_from`, `hour_to` | `request_hour_from`, `request_hour_to` | set when `shape != 'full_day'`; also `request_unit_hours=True` |
| `note`              | `name`                       | Odoo calls the description "name" |
| `state`             | `state`                      | one-way pull from Odoo, mirrored |

### State semantics

UI grouping rolls Odoo's six states into three buckets:

- **Pending** = `confirm` OR `validate1` (the latter is intermediate
  in the two-stage approval flow)
- **Approved** = `validate` only
- **Rejected** = `refuse` OR `cancel`

The cascade-on-approval logic triggers ONLY on the transition into
`validate`; intermediate `validate1` does not yet count as approved.

### `originating_kiosk_user` flag

`TRUE` for kiosk submissions, `FALSE` for rows the poller discovers
in Odoo. Lets the "My Requests" tab hide HR-entered rows from the
employee's edit/cancel actions, since those didn't originate from
their kiosk session.

### `working_hours_json`

Computed at submit time for partial-day shapes. Stores the *complement*
of the leave window against the shift. The staffing scheduler reads
this directly so no recompute on render.

Example for an early-leave at 2pm on a 6:00–14:30 shift with no lunch:
```json
[{"from": 6.0, "to": 14.0}]
```

Example for a midday gap 10:00–12:00 on a 6:00–14:30 shift with no lunch:
```json
[{"from": 6.0, "to": 10.0}, {"from": 12.0, "to": 14.5}]
```

### Settings additions

Extend the existing `settings_store`:

- `time_off.hidden_leave_type_ids` — JSON list of `hr.leave.type` ids
  to suppress from the kiosk picker.
- `time_off.show_stratustime_overlay` — bool, default `TRUE` during
  parallel run.
- `time_off.default_shift_start` — float, default `6.0`.
- `time_off.default_shift_end` — float, default `14.5`.

Leave-type *names* are NOT stored locally — only ids. The Odoo poller
refreshes a cached `hr.leave.type` map (id → name, request_unit,
requires_allocation, active) every ~10min so renames in Odoo flow
through.

### New cache table: `time_off_balances`

```sql
CREATE TABLE time_off_balances (
  person_odoo_id      INT NOT NULL,
  holiday_status_id   INT NOT NULL,
  unit                TEXT NOT NULL,        -- 'days' or 'hours' (from hr.leave.type)
  allocated_total     NUMERIC(8,2) NOT NULL,
  taken               NUMERIC(8,2) NOT NULL, -- Odoo-approved (state=validate)
  pending             NUMERIC(8,2) NOT NULL DEFAULT 0, -- confirm + validate1
  available           NUMERIC(8,2) NOT NULL, -- allocated - taken
  available_practical NUMERIC(8,2) NOT NULL, -- allocated - taken - pending  ← enforcement floor
  last_pulled_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (person_odoo_id, holiday_status_id)
);
```

`available` is Odoo's number ("you have 12.5 days"). `available_practical`
subtracts already-submitted-but-not-yet-approved requests so concurrent
pending requests can't both pass validation.

## Sync Logic

### Write path

Mirrors `kiosk_sync.py`:

```
Kiosk submit → INSERT row (state='draft', synced=FALSE)
            → render confirmation page immediately
            → BackgroundTask.add_task(time_off_sync.push_one, row_id)
                ↓
            XML-RPC: hr.leave.create({...}) → odoo_leave_id + state='confirm'
                ↓ on success
            UPDATE row SET odoo_leave_id=..., state='confirm',
                           synced_to_odoo=TRUE, last_pushed_at=now()
                ↓ on failure
            UPDATE row SET sync_error=...   (stays synced_to_odoo=FALSE)
```

A 60s sweep worker (parallel to `retry_unsynced_punches`) picks up
unsynced rows and retries.

### Edit and cancel

Reuse the same shape — they're additional row mutations:

- **Edit pending** (state `confirm`/`validate1`):
  XML-RPC `hr.leave.write(odoo_leave_id, {new fields})`.
- **Cancel pending**: `hr.leave.action_refuse(odoo_leave_id)` (Odoo's
  standard refuse-from-pending method), then mirror local state to
  `cancel`.
- **Edit/cancel approved**: also routes through `action_refuse` — Odoo's
  accepted way to back out an approved leave. The refusal goes back
  through the employee's approver chain. Copy on the kiosk warns the
  user that approval will be required again.

### Pull path

New background loop in `app.py`, runs every 60s:

```python
def poll_odoo_leaves():
    leaves = odoo_client.fetch_leaves_for_range(
        start_d=today - timedelta(days=60),
        end_d=today + timedelta(days=365),
    )
    for L in leaves:
        upsert_local_row(L)
    mark_local_rows_missing_from_odoo_as_cancel()
```

Catches three cases:
1. **State changes** — Odoo-side approvals flip `confirm` → `validate`
   → mirror updates.
2. **HR-entered leaves** — new rows discovered in Odoo, inserted with
   `originating_kiosk_user=FALSE`.
3. **Deletions** — Odoo records unlinked → local row state goes to
   `cancel`.

### Cascade on state change

When the poller flips a local row to `validate` (approved):

1. **Staffing scheduler integration**: for each date in
   `[date_from, date_to]`, place the person into the `TIME_OFF_KEY`
   bucket in `schedules` for that date. If the person was previously
   placed in a work-center bucket, remove them from that bucket and
   log to a `scheduler_moves` audit log so supervisors can understand
   the move.
2. **Custom hours integration**: for partial-day shapes, write the
   `working_hours_json` ranges into the existing `custom_day_hours`
   table for those dates. Scheduler picks it up on next render.
3. **Reverse on refuse/cancel**: remove the person from `TIME_OFF_KEY`
   and delete the `custom_day_hours` rows we wrote.

These cascades happen inside the poller's transaction so the mirror,
scheduler, and custom hours stay consistent.

### Duplicate-write guard

If an Odoo `create` succeeded but the local UPDATE failed (network
blip between two ops), the retry would create a duplicate `hr.leave`.
Mitigation: before retrying any `hr.leave.create`, search Odoo for
an existing leave matching `(employee_id, request_date_from,
request_date_to, holiday_status_id)` in
`state IN ('confirm', 'validate1', 'validate')` — if found, claim
it instead of creating a new one.

## Kiosk UI & Flow

### Entry point

New "Time Off" tile on `kiosk_dashboard.html` (alongside Clock In /
Transfer / Clock Out). Badge shows count of pending requests for this
employee.

### Routes

All gated behind a fresh kiosk session token, same pattern as punches:

```
GET  /kiosk/time-off/{token}                   landing — three big buttons
GET  /kiosk/time-off/request/{token}           step 1: shape picker (4 cards)
GET  /kiosk/time-off/request/{token}/details   step 2: type + date(s) + time(s) + note
POST /kiosk/time-off/request/{token}/submit    writes local row + queues Odoo sync
GET  /kiosk/time-off/mine/{token}              "My Requests" — list of own requests
GET  /kiosk/time-off/mine/{token}/{request_id} request detail w/ edit + cancel buttons
POST /kiosk/time-off/mine/{token}/{request_id}/cancel
POST /kiosk/time-off/mine/{token}/{request_id}/edit
GET  /kiosk/time-off/calendar/{token}          "Who's Out" — month calendar
```

### Landing screen

Three large touch cards:
- **Request Time Off** — starts the wizard
- **My Requests (N)** — N is count of own pending/approved/rejected,
  color-coded by status
- **Who's Out** — opens the calendar

### Request wizard — Step 1 (shape picker)

Four large cards (icon + one-liner), 2×2 grid or stacked:
- "Full Day(s) Off"
- "Arriving Late"
- "Leaving Early"
- "Out for Part of the Day"

### Request wizard — Step 2 (details), branched by shape

| Shape | Inputs |
|---|---|
| Full Day(s) | Type picker · Start date · End date · Optional note |
| Arriving Late | Type picker (defaults to Custom Hours) · Date · "I'll arrive at [time]" · Note |
| Leaving Early | Type picker (defaults to Custom Hours) · Date · "I'll leave at [time]" · Note |
| Mid-Day Gap | Type picker (defaults to Custom Hours) · Date · "Gone from [time] to [time]" · Note |

**Date picker**: native HTML5 `<input type="date">`.

**Time picker**: a big stepper grid (15-min increments from shift start
to shift end) rather than `<input type="time">` — easier on a kiosk thumb,
and constrains input to valid working-hours windows so we don't have to
reject post-hoc.

**Type picker** shows only leave types not in
`time_off.hidden_leave_type_ids`. Type picker for partial-day shapes
filters to types with `request_unit='hour'`; full-day shape filters to
types with `request_unit='day'` or `'half_day'`. Prevents unit
mismatches.

### Balance + live-calc panel (step 2)

```
┌─────────────────────────────────────────────┐
│ Type:  [PTO ▼]                              │
│ ─────────────────────────────────────────── │
│  Available: 12.5 days  (4.0 days pending)   │
│  This request: ──                           │
│  Remaining after: 12.5 days                 │
└─────────────────────────────────────────────┘
```

Numbers update live as inputs change. Submit button shades and
disables if request > `available_practical`.

Live calc is a client-side JS estimate. Server-side authoritative
check on submit; if server says request > `available_practical`,
return 422 and re-render with an error banner.

**Custom Hours special case** (`requires_allocation=NO`):
```
Available: Unpaid · no balance required
This request: 4 hours
```
No remaining/exceeded checks. Submit always enabled (subject to
working-calendar validity).

### Balance refresh strategy

Three triggers:
1. **On wizard open** — synchronous refresh for this employee before
   showing step 2. ~200-500ms blocking call to Odoo is acceptable for
   one screen, one user, one click.
2. **After every poller cycle** — when the leave poller detects a
   state change for person X, invalidate X's balance entries.
3. **Periodic safety net** — every 10min, refresh all balance rows
   older than 10min.

Manual "Refresh now" button on Time Off landing forces immediate refresh.

### My Requests screen

Vertical list, each row shows:
- Type · dates · times (if partial) · state badge (Pending / Approved /
  Rejected / Canceled)
- Tap row → detail screen with **Edit** + **Cancel** buttons.

Edit/Cancel buttons always visible; if state=`validate`, copy reads
"Cancel an approved request will need approval again."

Pending requests with no `leave_manager_id` set in Odoo (so the
approval is stalled) show a hint: "Pending — your approver isn't
set in Odoo, contact HR."

### Who's Out calendar

Month grid (same shape as the admin `/staffing/time-off` template, but
kiosk-styled with bigger tap targets). Each day cell shows up to ~4
names with timing:
- "Bob — full day"
- "Alice — leaves 2pm"
- "Carl — 10–12"
- "Dana — arrives 9am"

No leave type shown (privacy). Tap a day → full list of everyone out
that day. **Approved only** — no pending on this shared view.

### Sync warning banner

Same `_sync_error_warning` pattern from the punch flow — if any of this
employee's recent time-off submissions are stuck with a `sync_error`,
surface a small warning at the top of the Time Off landing screen.

### Token TTL

Existing 60s token refresh from `kiosk.py` — each render mints a
fresh token so a slow wizard step doesn't time out mid-flow.

## Admin Integration

### 1. `/staffing/time-off` — source switch + parallel overlay

Existing route reads from `stratustime_client.time_off_entries_for_range()`.
New behavior reads from `time_off_requests` (state=`validate`) joined
with `people` for names. Existing template renders unchanged — same
`off_map: {date: [entries...]}` shape.

The `time_off.show_stratustime_overlay` setting (default TRUE during
pilot) layers StratusTime entries on top with a muted style and badge
("from StratusTime"). At cutover, flip the setting and the overlay
disappears.

Small data-source indicator in calendar header: "Showing: Odoo +
StratusTime overlay" or "Showing: Odoo only" — source state is never
ambiguous during the parallel period.

### 2. Staffing scheduler — auto-route to TIME_OFF_KEY

`cascade_on_approve()` (from sync logic) writes the approved-leave
person into `assignments[TIME_OFF_KEY]` for each affected day in the
existing `schedules` table. `staffing.html` already renders this
bucket — no template work.

Conflict resolution: if a supervisor had placed the person in a WC
bucket and their leave then approved, the cascade *moves* the person:
removes from WC, adds to TIME_OFF_KEY, logs the move to a new
`scheduler_moves` audit log.

### 3. Custom day hours — partial-day passthrough

`cascade_on_approve()` writes `working_hours_json` ranges into the
existing `custom_day_hours` table — one row per affected person-day
with the working (complement) hours. Scheduler template already reads
custom_day_hours and renders "Bob (6:00–10:00 + 12:00–14:30)" next to
the name.

On refuse/cancel, the cascade deletes the custom_day_hours rows we wrote.

### 4. Settings panel — new "Time Off" section in `/staffing/settings`

- **Hidden leave types** — checklist of all `hr.leave.type` from Odoo;
  checking hides from the kiosk picker.
- **StratusTime overlay** — toggle (ON during parallel run, OFF at
  cutover).
- **Default shift hours** — start + end (pair of time inputs), used
  when an employee has no `resource_calendar_id` in Odoo.
- **Refresh now** — manual button to force-poll Odoo + balance refresh.

Settings persist in the existing `settings_store` (no new table).

## Odoo Client Extensions

New functions added to `src/zira_dashboard/odoo_client.py` alongside
existing `hr.attendance` writers.

### Reads (called by poller and balance refresher)

```python
def fetch_leave_types() -> list[dict]:
    """All active hr.leave.type. Returns [{id, name, request_unit,
    requires_allocation, color, active}, ...]. Cached ~10min."""

def fetch_leaves_for_range(start_d, end_d) -> list[dict]:
    """All hr.leave records in [start_d, end_d] for active employees.
    Fields: id, employee_id, holiday_status_id, state, date_from,
    date_to, request_date_from, request_date_to, request_hour_from,
    request_hour_to, request_unit_hours, number_of_days,
    number_of_hours_display, name."""

def fetch_balances_for(employee_odoo_id: int) -> list[dict]:
    """Per-leave-type balance for one employee. Returns [{
    holiday_status_id, unit, allocated_total, taken, available}, ...].
    Tries hr.leave.type.get_allocation_data first; falls back to
    direct aggregation over hr.leave.allocation + hr.leave if the
    helper isn't exposed on this Odoo version."""

def fetch_resource_calendar(employee_odoo_id: int) -> dict | None:
    """Returns {hour_from, hour_to, lunch_from, lunch_to, tz} for the
    employee's resource_calendar_id, derived from
    resource.calendar.attendance rows. Used to validate partial-day
    requests + compute working_hours_json. Returns None if no calendar
    set — caller falls back to default shift setting."""
```

### Writes (called from `time_off_sync.py`)

```python
def create_leave(employee_odoo_id, holiday_status_id, date_from,
                 date_to, hour_from=None, hour_to=None,
                 note=None) -> int:
    """Create an hr.leave in 'confirm' state. Sets request_unit_hours=True
    when hour_from/hour_to are given. Returns the new leave_id."""

def write_leave(leave_id: int, **fields) -> None:
    """Update an existing hr.leave. Used for edit-pending flow."""

def refuse_leave(leave_id: int) -> None:
    """Call hr.leave.action_refuse — handles both pending-cancel and
    approved-cancel via the same Odoo workflow."""

def find_duplicate_leave(employee_odoo_id, holiday_status_id,
                         date_from, date_to) -> int | None:
    """Dedupe helper used before a create retry — returns existing
    leave_id if one matches, else None."""
```

### Odoo permissions the API user needs

- **Read**: `hr.employee`, `hr.leave`, `hr.leave.type`,
  `hr.leave.allocation`, `resource.calendar`,
  `resource.calendar.attendance`
- **Create + write**: `hr.leave`
- **Execute**: `hr.leave.action_refuse`

Typically covered by "Time Off → Officer" or "Time Off → Administrator"
security group on the API user.

### Error envelope

Every write wraps Odoo's XML-RPC `Fault` exceptions with structured
error strings like `"odoo_validation: hours outside working calendar"`
or `"odoo_state: cannot refuse leave in state validate"` so
`sync_error` columns get classifiable messages, not raw tracebacks.

### Connection reuse

No new connection pattern. Reuses existing module-level XML-RPC proxy
+ `execute_kw` helper.

## Testing, Edge Cases, Phasing & Risks

### Testing strategy

- **Unit tests for `odoo_client` new methods** — mocked XML-RPC
  `execute_kw`, fixture payloads modeled on real Odoo responses.
  Includes the `get_allocation_data` fallback path so both branches
  are covered.
- **Unit tests for `time_off_sync`** — push path, pull path,
  dedupe-by-search, cascade-on-approve. Mocked Odoo client; real
  Postgres via existing test harness.
- **State-mapping table tests** — every Odoo state routed to the
  correct UI bucket.
- **Wizard request-shape tests** — each shape produces correct
  `hour_from`/`hour_to` + `working_hours_json` from valid inputs,
  rejects out-of-shift inputs cleanly.
- **End-to-end smoke** against a live Odoo dev tenant: submit from
  kiosk → approve in Odoo → poller picks up → calendar + scheduler
  reflect approval. Manual pre-cutover.

### Edge cases

- **Timezone**: Odoo `hr.leave` datetimes are naive UTC strings. Same
  boundary conversion as `kiosk.py` does for `hr.attendance`. Hour
  floats interpreted in employee's `resource_calendar.tz`; Odoo
  handles TZ math.
- **Leap day / DST**: `request_unit_hours` on a DST transition day
  may yield non-standard hour computations. Known quirk; trust
  Odoo's calendar math.
- **Employee with no `leave_manager_id`**: submission succeeds (we
  create `hr.leave` in `confirm`), but stalls until Odoo gets a
  manager assigned. Flag in My Requests with a hint: "Pending —
  your approver isn't set in Odoo, contact HR."
- **Custom Hours on a non-working day** (e.g., Sunday for a Mon–Fri
  employee): `resource_calendar` returns 0 working hours, Odoo
  rejects. Catch in pre-validation: "You're not scheduled on [date]."
- **Person renamed / deactivated in Odoo mid-request**: poller writes
  latest Odoo state; archived employees' pending requests transition
  to `cancel`.
- **Public holidays inside a multi-day full-day request**: Odoo skips
  holidays in the day count; we mirror that count on poll.

### Phasing

- **Phase A (build + test)**: All code shipped, parallel overlay ON,
  only Dale's own requests go through the kiosk. Time Off button on
  the kiosk hidden behind feature flag (env var
  `KIOSK_TIME_OFF_ENABLED=1`).
- **Phase B (small-group pilot)**: Enable for 3-5 volunteers across
  departments. Watch sync_error / sync_warning UI. Validate
  cascade-to-scheduler in production.
- **Phase C (full rollout)**: Feature flag goes default-on.
  StratusTime overlay setting stays ON for ~2 weeks of dual-entry,
  then flipped OFF; StratusTime becomes legacy until decommissioned.

### Open risks

1. **Odoo `get_allocation_data` API drift**: helper isn't part of
   Odoo's documented public API and can move between versions. The
   direct-aggregation fallback hedges this, but the fallback's
   correctness needs a dev probe to confirm (does our manual sum
   match Odoo's UI number, including future accruals?).
2. **Two-stage approval friction**: every request needs two
   signatures. If slow during Phase B, switching the leave type to
   single-stage is a 30-second Odoo config change — no app code
   change.
3. **Polling lag UX**: up to 60s between Odoo approval and the
   kiosk/scheduler reflecting it. If annoying in practice, shorten
   poll to 15-30s. Per-employee "Refresh now" button on My Requests
   ships in v1.
4. **Concurrent submissions**: two devices, same person, same instant —
   both pass `available_practical` check, both create Odoo leaves.
   Unlikely on a single kiosk but possible in multi-kiosk future.
   Mitigation: brief row-level lock on the balance row during submit,
   or accept it and let one fail at Odoo's allocation check.

### Done criteria (v1 ship)

- ☐ Kiosk landing → request wizard → submit → confirmation, all four
  shapes
- ☐ Pending requests visible in My Requests; balance shown on
  type-pick
- ☐ Approval in Odoo cascades to `/staffing/time-off` + staffing
  scheduler within 60s
- ☐ Partial-day approvals render in scheduler via `custom_day_hours`
- ☐ Settings panel filters leave types + toggles parallel overlay
- ☐ Sync errors surface in My Requests warning banner
- ☐ All unit tests passing

## Open Questions

(None at design time — answered during brainstorming and folded into
sections above. To be revisited during planning phase if new questions
surface.)
