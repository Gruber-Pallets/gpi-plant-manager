# Block overlapping time-off requests

**Problem:** A kiosk time-off request that overlaps time off the worker
already has gets **silently stuck in an errored state**. The submit
handler (`routes/timeclock_time_off.py:558` `request_submit`) does no
overlap check — it inserts a `draft` row, queues a background push, and
shows the success page immediately. The overlap is rejected later by
**Odoo** during `_push_create` (`time_off_sync.py:112`), the exception is
caught in `push_one` (`time_off_sync.py:103`), stamped into `sync_error`,
and the row stays `synced_to_odoo=FALSE` — so the 60s sweep
(`retry_unsynced_requests`, `time_off_sync.py:208`) **retries it forever**
(the conflict is permanent). The worker thinks it worked; HR never sees a
valid request; the landing page shows a perpetual sync-error banner. The
edit handler (`mine_edit_submit`, `:995`) has the identical exposure.

**Fix:** Catch the overlap *before* posting.

1. **Pre-check at submit + edit (server-side, authoritative).** Before
   persisting, check the local `time_off_requests` mirror for a
   non-rejected request by the same person whose date range overlaps. On
   conflict: persist nothing, queue no push, and re-render the wizard with
   a **modal pop-up** carrying the message below. Nothing reaches Odoo.
2. **Self-healing re-check on the push path (backstop + cleanup).** At the
   top of `_push_create`, re-run the same local check (established rows
   only). If a real conflict now exists, **delete the phantom draft** and
   skip the Odoo call instead of recording another `sync_error`.

The pre-check covers the normal flow. The push-path re-check covers two
residual cases without brittle Odoo-error-string parsing or a schema
change:

- **The rare race** — HR books a conflicting leave in the split-second
  after the worker's check passed. The first push may still hit Odoo's
  rejection, but on the next 60s sweep the poller has mirrored HR's leave,
  so the re-check finds the conflict and deletes the phantom. It
  **self-heals within ~a minute** — never stuck forever.
- **Existing stuck rows** from before this fix — the leave each collided
  with is already mirrored, so they're cleaned up on the next sweep. No
  migration.

The re-check is **surgical**: it deletes only when a genuine overlapping
row exists. A row stuck for a *transient* reason (Odoo briefly down) has
no local conflict, so it's left alone and syncs normally on recovery.

## Message (verbatim, bilingual)

> You already have time off for this time so we can't add a second. Either
> cancel your request via the My Requests button or contact management for
> help.

Rendered through `t()` (`timeclock_i18n.py`) so Spanish-speaking workers
see it stacked under the English, like the rest of the kiosk.

## What counts as a conflict

- **Scope:** the *same person* only — never a coworker's time off.
- **States:** non-rejected — `draft`, `draft_edit`, `confirm`,
  `validate1`, `validate`. Excludes `refuse`, `cancel`, `draft_cancel`.
- **Overlap rule:** date-level, type-agnostic — `date_to >= requested_from
  AND date_from <= requested_to`. Mirrors Odoo's own constraint (a worker
  can't hold two leaves on the same day regardless of type). Consequence:
  two partial-day leaves on the same day are treated as a conflict even if
  their hours don't overlap — rare, and blocking it with a clear message
  is acceptable (confirmed in design).
- **Why the local mirror, not a live Odoo call:** it's the source the
  kiosk already trusts everywhere (landing counts, calendar, My Requests);
  it's a millisecond indexed read with no XML-RPC on the submit path; and
  it catches the worker's *own* just-submitted draft that hasn't synced
  yet, so a rapid double-tap is blocked too. The 60s poller keeps it fresh
  against HR-entered leaves.

## Behavior

| Situation | Result |
|---|---|
| Submit a request that overlaps an existing non-rejected request | Modal pops; nothing saved or pushed |
| Submit with no overlap | Saved + pushed as today (unchanged) |
| Submit overlapping a *refused/cancelled* request | Allowed (terminal states don't conflict) |
| Rapid double-submit of the same dates | 2nd is blocked by the modal (1st draft already in mirror) |
| Edit a request onto a date that overlaps a *different* request | Modal pops; no update, no push |
| Edit a request, keeping its own dates | Allowed (`exclude_rid` skips self) |
| Race: HR books a conflict just after the check passed | Push re-check deletes the phantom on the next sweep (≤~60s) |
| Pre-existing stuck overlap row | Deleted on next sweep by the re-check |
| Row stuck for a transient reason (Odoo down) | Left alone; syncs on recovery |

## Implementation

### 1. Shared overlap helper — `time_off_sync.py`

Add a module-level helper (low-level module the route already imports, so
both the route pre-check and the push-path re-check share one definition —
no circular import, no duplicated SQL):

```python
_ACTIVE_STATES = ("draft", "draft_edit", "confirm", "validate1", "validate")

def find_conflicting_request(person_odoo_id, date_from, date_to,
                             exclude_rid=None, established_only=False):
    """First non-rejected request for this person whose [date_from,
    date_to] overlaps the given range, else None.

    exclude_rid       — skip this row (an edit can't conflict with itself).
    established_only   — push-path mode: only count a row as a conflict if
                         it's already synced (synced_to_odoo=TRUE) OR was
                         created earlier (id < exclude_rid), so two
                         simultaneous duplicate drafts don't delete each
                         other — the earlier/established one wins.
    """
```

Query: `WHERE person_odoo_id=%s AND state IN (_ACTIVE_STATES) AND date_to
>= %s AND date_from <= %s`, plus `AND id <> exclude_rid` when given, plus
`AND (synced_to_odoo = TRUE OR id < %s)` when `established_only`. `ORDER BY
id LIMIT 1`.

### 2. Push-path re-check — `time_off_sync.py` `_push_create`

At the top of `_push_create` (before `find_duplicate_leave`/`create_leave`):

```python
conflict = find_conflicting_request(
    row["person_odoo_id"], row["date_from"], row["date_to"],
    exclude_rid=row["id"], established_only=True,
)
if conflict is not None:
    _log.info("push_create: row %s overlaps established row %s — deleting phantom",
              row["id"], conflict["id"])
    db.execute("DELETE FROM time_off_requests WHERE id = %s", (row["id"],))
    return
```

Reuses the same delete-the-never-synced-row disposition the cancel handler
already uses (`mine_cancel`, `:855`). Only reached for the create path
(`_push_create` is the `odoo_leave_id IS NULL` branch).

### 3. Pre-check + modal context — `routes/timeclock_time_off.py`

- **`request_submit`:** after date normalization (`:610`–`:614`), before
  the shift/time validation, call
  `time_off_sync.find_conflicting_request(p["odoo_id"], df, dt)`. If a
  conflict is returned, re-render the details template with `conflict=True`
  + a fresh `my_requests` token, **HTTP 409**, and return — no
  `_insert_request_row`, no `background_tasks.add_task`.
- **`mine_edit_submit`:** same check after normalization (`:1048`–`:1051`),
  passing `exclude_rid=rid`. On conflict, re-render in `edit_mode` with
  `conflict=True`, 409, and return — no `_update_request_row`, no push.
- **Tidy:** the details-form context dict is currently built three times
  (GET `request_details`, the time-error re-render, the edit re-render).
  Adding a fourth copy for the conflict path crosses the threshold —
  factor a small `_details_context(p, shape, token, **extra)` helper and
  route all paths through it. Keeps the conflict branch a few lines.

### 4. Modal markup + styles

- **`templates/timeclock_base.html`** (next to `.k-error`, `:105`): add
  `.k-modal-overlay` (fixed, full-screen, dimmed, centered) and
  `.k-modal-card` styles. Touch-friendly sizing consistent with the kiosk.
- **`templates/timeclock_time_off_request_details.html`** (after the
  `error` banner, `:31`): `{% if conflict %}` block rendering the overlay
  with the `t()` message and two big buttons —
  **"Go to My Requests"** (`href="/timeclock/time-off/mine/{{ token }}"`)
  and **"OK"** (`onclick` hides the overlay so they can pick new dates).
  A few lines of inline JS for the OK close; no framework.

### 5. Spanish translation — `timeclock_i18n.py`

Add the message (and the two button labels if not already present —
"My Requests" exists; add "Go to My Requests"/"OK" as needed) to
`TRANSLATIONS`.

### 6. Tests

- **`tests/test_time_off_sync.py`** — `find_conflicting_request` unit
  tests (inclusive overlap edges, state filtering, `exclude_rid`,
  `established_only` earlier-id/synced rules); `_push_create` deletes the
  phantom when an established conflict exists and makes **no** Odoo call;
  `_push_create` with no conflict creates as before; two simultaneous
  drafts → only the later id is deleted.
- **`tests/test_time_off_routes.py`** — submit with an overlapping row →
  409, **no** row inserted, **no** push queued, `conflict` in the
  response; submit with no overlap still inserts + queues; edit onto a
  conflicting date → no update + 409; edit keeping its own dates →
  allowed.

## Verification

- Per repo setup, the Python suite can't run locally (3.9; `fastapi` not
  importable) — tests are written to run in CI. Logic verified by
  reasoning + the behavior table.
- Manual kiosk check: submit a request over an existing one → modal pops,
  nothing posts; tap "My Requests" → lands on the cancel surface; cancel
  the original, resubmit → succeeds.

## Out of scope

- **Other Odoo rejection reasons** (balance checks, config errors) keep
  today's record-`sync_error`-and-retry behavior — the re-check is narrow
  to genuine local overlaps only.
- **Rare-race visibility:** in the pure sub-60s race the phantom is
  deleted silently (the worker never saw the modal, since their check
  passed). Accepted given how narrow the window is and that the leave
  genuinely can't exist in Odoo; surfacing a terminal "couldn't submit"
  state was considered and deferred (would need a new state + migration).
- **Preserving the worker's entered dates** on the conflict re-render —
  matches the existing time-error path, which also re-renders without
  prefill. Not changed here.
- **Coworker overlap / staffing-level limits** (too many people out the
  same day) — unrelated; not addressed.
