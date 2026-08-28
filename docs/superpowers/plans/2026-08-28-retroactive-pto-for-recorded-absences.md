# Retroactive PTO for Recorded Absences Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a worker request one paid PTO day for one recorded current-pay-period absence while preserving the attendance absence and safely escalating closed-period cases to Wendy in Odoo.

**Architecture:** Store retroactive pay-treatment requests in a dedicated Postgres table so the ordinary Odoo time-off push worker cannot create an overlapping leave before approval. A focused conversion service advances a durable, leased state machine through verified Odoo Absence refusal, PTO creation/approval, compensation, and local finalization. Separate kiosk/admin routers, a Staffing projection, and a reconciliation/task service consume that domain API without owning its rules.

**Tech Stack:** Python 3.11, FastAPI, Jinja2, vanilla JavaScript, PostgreSQL/psycopg2, Odoo XML-RPC, pytest, Ruff.

## Global Constraints

- Canonical repository: `gpi-plant-manager`.
- Source design: `docs/superpowers/specs/2026-08-28-retroactive-pto-for-recorded-absences-design.md`.
- Retroactive self-service is full-day only, one recorded absence day per request.
- The absence date must be before the plant day and inside the configured current pay period.
- `manual_absences` remains the attendance source of truth in every terminal state.
- Pending requests must not create, refuse, edit, or approve an Odoo leave.
- Paid leave type resolution accepts exactly one active, allocation-required, day/half-day type named `Paid Time Off`; ambiguity blocks submission.
- All Odoo mutations require a fresh read and a verified postcondition. Unclear results move to review; they are never guessed successful.
- A configured pay-period rollover blocks automatic conversion and creates one deduplicated Wendy task per request.
- Wendy identity is the one active Odoo user whose normalized login is exactly `wendy@gruberpallets.com`; zero or multiple matches are delivery errors.
- Do not expose absence reasons or unrelated leave details in employee pages or Odoo task descriptions.
- Preserve all unrelated dirty-worktree files.
- Every task updates `CHANGELOG.md` with short, child-friendly copy, commits only its scoped files, and pushes the commit to `origin/main`.

## File Structure

### New files

- `src/zira_dashboard/absence_pto_store.py` — typed rows, validation, persistence, leases, compare-and-set transitions, and due-work queries; no Odoo calls.
- `src/zira_dashboard/absence_pto.py` — Paid Time Off type resolution, eligibility, submission, employee list/detail projection, and denial rules.
- `src/zira_dashboard/absence_pto_conversion.py` — verified Odoo conversion, compensation, resume, and local finalization.
- `src/zira_dashboard/absence_pto_review.py` — Wendy task create/adopt/update/close, period rollover, reconciliation, and manual resolution.
- `src/zira_dashboard/routes/timeclock_absence_pto.py` — signed-token employee list, submit, and detail routes.
- `src/zira_dashboard/routes/absence_pto_admin.py` — manager approve, deny, and mark-handled JSON routes.
- `src/zira_dashboard/templates/timeclock_absence_pto_list.html` — eligible/disabled recorded absences and balance explanation.
- `src/zira_dashboard/templates/timeclock_absence_pto_detail.html` — linked request status for My Requests.
- `tests/test_absence_pto_store.py` — schema/store/lease/transition tests.
- `tests/test_absence_pto.py` — pure eligibility, PTO resolution, submission, employee projection, and denial tests.
- `tests/test_timeclock_absence_pto_routes.py` — token ownership and employee-route tests.
- `tests/test_absence_pto_conversion.py` — success, idempotency, every failure/compensation branch, and restart tests.
- `tests/test_absence_pto_review.py` — rollover, Wendy identity/task lifecycle, external resolution, and manual handling tests.
- `tests/test_absence_pto_admin_routes.py` — manager-route and response-contract tests.

### Modified files

- `src/zira_dashboard/_schema.py` — linked-request table, constraints/indexes, and request-kind audit columns.
- `src/zira_dashboard/staffing_hours.py` — public configured-current-pay-period bounds helper.
- `src/zira_dashboard/_odoo_time_off.py` / `odoo_client.py` — narrow leave snapshot and exact matching helpers.
- `src/zira_dashboard/_odoo_feedback.py` / `odoo_client.py` — exact Odoo-user lookup and task-stage/close helpers.
- `src/zira_dashboard/time_off_audit.py` — namespaced request kind/key and structured conversion detail.
- `src/zira_dashboard/routes/time_off_approvals.py` — merge linked requests into the manager queue.
- `src/zira_dashboard/templates/_time_off_approvals_panel.html` — linked-request labels and route metadata.
- `src/zira_dashboard/static/time_off_approvals.js` — dispatch ordinary versus absence-PTO actions.
- `src/zira_dashboard/routes/timeclock_time_off.py` — merge linked rows into counts and My Requests.
- `src/zira_dashboard/templates/timeclock_time_off_landing.html` / `timeclock_time_off_mine.html` — entry point and generic detail URLs/status markers.
- `src/zira_dashboard/scheduler_time_off.py` — combine absence attendance with linked PTO state.
- `src/zira_dashboard/app.py` / `src/zira_dashboard/routes/README.md` — register routers and the 60-second reconciler.
- Existing focused tests listed in the tasks below — regression contracts.
- `CHANGELOG.md` — one plain-language note per pushed task.

---

### Task 1: Share the configured current pay-period boundary

**Files:**
- Modify: `src/zira_dashboard/staffing_hours.py`
- Test: `tests/test_staffing_hours.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: `current_pay_period_bounds(today: date) -> tuple[date, date]`.
- Preserves: `resolve_hours_range` behavior and Odoo verification notices.

- [ ] **Step 1: Write the failing boundary tests**

Add exact anchor, pre-anchor, rollover, and saved-config cases:

```python
def test_current_pay_period_bounds_uses_configured_cycle(monkeypatch):
    monkeypatch.setattr(
        hours,
        "current_pay_period_config",
        lambda: hours.PayPeriodConfig(date(2026, 8, 16), 14),
    )
    assert hours.current_pay_period_bounds(date(2026, 8, 29)) == (
        date(2026, 8, 16), date(2026, 8, 29)
    )
    assert hours.current_pay_period_bounds(date(2026, 8, 30)) == (
        date(2026, 8, 30), date(2026, 9, 12)
    )


def test_current_pay_period_bounds_works_before_anchor(monkeypatch):
    monkeypatch.setattr(
        hours,
        "current_pay_period_config",
        lambda: hours.PayPeriodConfig(date(2026, 8, 16), 14),
    )
    assert hours.current_pay_period_bounds(date(2026, 8, 15)) == (
        date(2026, 8, 2), date(2026, 8, 15)
    )
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_staffing_hours.py -q
```

Expected: FAIL with `AttributeError: module 'zira_dashboard.staffing_hours' has no attribute 'current_pay_period_bounds'`.

- [ ] **Step 3: Add the pure public helper and reuse it**

Insert beside `_preset_bounds` and replace its duplicated current-period math:

```python
def current_pay_period_bounds(today: date) -> tuple[date, date]:
    """Inclusive configured pay-period bounds containing ``today``."""
    config = current_pay_period_config()
    period_index = (today - config.anchor).days // config.cycle_days
    start = config.anchor + timedelta(days=period_index * config.cycle_days)
    return start, start + timedelta(days=config.cycle_days - 1)
```

For `this_pay_period`, call this helper. For `last_pay_period`, subtract one configured cycle from both returned bounds. Do not call Odoo from this helper.

- [ ] **Step 4: Verify focused behavior and lint**

Run:

```bash
.venv/bin/python -m pytest tests/test_staffing_hours.py tests/test_staffing_hours_route.py -q
.venv/bin/ruff check src/zira_dashboard/staffing_hours.py tests/test_staffing_hours.py
```

Expected: all tests pass; Ruff exits 0.

- [ ] **Step 5: Add the patch note, commit, and push**

Add under the newest date:

```markdown
### Pay-period dates stay together

- **Plant Manager now uses one shared pay-period calendar.** This helps new pay tools use the same dates as the Hours page. Nothing new is showing to workers yet.
```

Then run:

```bash
git add CHANGELOG.md src/zira_dashboard/staffing_hours.py tests/test_staffing_hours.py
git commit -m "refactor: share current pay period bounds"
git push origin main
```

### Task 2: Add the durable linked-request store

**Files:**
- Modify: `src/zira_dashboard/_schema.py`
- Create: `src/zira_dashboard/absence_pto_store.py`
- Create: `tests/test_absence_pto_store.py`
- Modify: `tests/test_ci_workflow.py`
- Modify: `.github/workflows/tests.yml`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: `AbsencePtoRequest`, `create_request`, `get_request`, `list_for_person`, `list_pending`, `claim_request`, `renew_claim`, `transition`, `release_claim`, `mark_needs_review`, and `save_task_delivery`.
- State values: `pending | converting | approved | denied | needs_review | resolved_manually`.
- Conversion steps: `not_started | absence_refused | pto_created | pto_approved`.
- No function in this file may import or call `odoo_client`.

- [ ] **Step 1: Write failing schema and store-contract tests**

Pin the DDL, type validation, one-active-request index, create/load mapping, lease ownership, compare-and-set transitions, and due queries:

```python
def test_schema_has_linked_request_constraints():
    assert "CREATE TABLE IF NOT EXISTS absence_pto_requests" in SCHEMA_DDL
    assert "absence_pto_requests_state_check" in SCHEMA_DDL
    assert "absence_pto_requests_step_check" in SCHEMA_DDL
    assert "absence_pto_requests_active_uniq" in SCHEMA_DDL
    assert "lease_owner UUID" in SCHEMA_DDL


def test_claim_is_atomic_and_lease_bounded(monkeypatch):
    seen = {}
    monkeypatch.setattr(store.db, "query", lambda sql, params: seen.update(
        sql=sql, params=params
    ) or [_row(lease_owner=OWNER)])
    claim = store.claim_request(41, OWNER, NOW, lease_seconds=120)
    assert claim and claim.lease_owner == OWNER
    assert "FOR UPDATE" in seen["sql"]
    assert "lease_until <=" in seen["sql"]


def test_transition_requires_current_owner_and_expected_step(monkeypatch):
    calls = []
    monkeypatch.setattr(store.db, "query", lambda sql, params: calls.append(
        (sql, params)
    ) or [_row(state="converting", conversion_step="absence_refused")])
    out = store.transition(
        41,
        OWNER,
        expected_state="converting",
        expected_step="not_started",
        new_state="converting",
        new_step="absence_refused",
    )
    assert out.conversion_step == "absence_refused"
    assert "lease_owner = %s" in calls[0][0]
    assert "conversion_step = %s" in calls[0][0]
```

Also add a guarded live-Postgres test, enabled only by `ABSENCE_PTO_TEST_DATABASE=1`, proving the partial unique index rejects two active rows but permits a new request after denial. Add that variable to the CI workflow test and workflow environment when the live test is introduced.

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_absence_pto_store.py tests/test_ci_workflow.py -q
```

Expected: collection fails because `absence_pto_store` and the schema table do not exist.

- [ ] **Step 3: Add the schema with explicit invariants**

Append idempotent DDL with these columns and constraints:

```sql
CREATE TABLE IF NOT EXISTS absence_pto_requests (
  id BIGSERIAL PRIMARY KEY,
  absence_day DATE NOT NULL,
  emp_id TEXT NOT NULL,
  person_odoo_id INTEGER NOT NULL,
  person_name TEXT NOT NULL,
  holiday_status_id INTEGER NOT NULL,
  leave_type_name TEXT NOT NULL,
  balance_at_submit NUMERIC NOT NULL CHECK (balance_at_submit >= 0),
  original_absence_leave_id INTEGER,
  pto_leave_id INTEGER,
  state TEXT NOT NULL DEFAULT 'pending',
  conversion_step TEXT NOT NULL DEFAULT 'not_started',
  employee_note TEXT,
  denial_reason TEXT,
  manual_resolution_note TEXT,
  sync_error TEXT,
  odoo_task_id INTEGER,
  task_attempts INTEGER NOT NULL DEFAULT 0,
  task_next_at TIMESTAMPTZ,
  lease_owner UUID,
  lease_until TIMESTAMPTZ,
  requested_by_person_id INTEGER,
  decided_by_upn TEXT,
  decided_by_name TEXT,
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at TIMESTAMPTZ,
  resolved_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT absence_pto_requests_state_check CHECK
    (state IN ('pending','converting','approved','denied','needs_review','resolved_manually')),
  CONSTRAINT absence_pto_requests_step_check CHECK
    (conversion_step IN ('not_started','absence_refused','pto_created','pto_approved'))
);

CREATE UNIQUE INDEX IF NOT EXISTS absence_pto_requests_active_uniq
  ON absence_pto_requests (absence_day, emp_id)
  WHERE state IN ('pending','converting','needs_review');
CREATE UNIQUE INDEX IF NOT EXISTS absence_pto_requests_pto_leave_uniq
  ON absence_pto_requests (pto_leave_id) WHERE pto_leave_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS absence_pto_requests_due_idx
  ON absence_pto_requests (state, task_next_at, lease_until);
```

Use explicit, idempotent `ALTER TABLE` statements with `ADD COLUMN IF NOT EXISTS` for future-safe bootstrap parity, matching the repository's schema style.

Deliberately do **not** add a foreign key to `manual_absences`. Existing managers must remain able to undo an absence. The linked request keeps its audit key, and eligibility/conversion code rereads `manual_absences`; if the source absence was removed, approval stops safely instead of blocking the delete.

- [ ] **Step 4: Implement the typed store and durable lease**

The public type and claim shape must be exact:

```python
@dataclass(frozen=True)
class AbsencePtoRequest:
    id: int
    absence_day: date
    emp_id: str
    person_odoo_id: int
    person_name: str
    holiday_status_id: int
    leave_type_name: str
    balance_at_submit: Decimal
    original_absence_leave_id: int | None
    pto_leave_id: int | None
    state: str
    conversion_step: str
    employee_note: str | None
    denial_reason: str | None
    manual_resolution_note: str | None
    sync_error: str | None
    odoo_task_id: int | None
    task_attempts: int
    task_next_at: datetime | None
    lease_owner: UUID | None
    lease_until: datetime | None
    requested_by_person_id: int | None
    decided_by_upn: str | None
    decided_by_name: str | None
    requested_at: datetime
    decided_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
```

`claim_request` must use one short transaction with `SELECT pg_advisory_xact_lock(%s::bigint)`, a row read ending in `FOR UPDATE`, and an ownership/expiry-checked update ending in `RETURNING`. Never hold a database transaction open during an Odoo call. `transition` must require the same lease owner plus expected state/step, and raise `StaleTransition` when zero rows return.

- [ ] **Step 5: Run store, schema, and full schema-bootstrap tests**

Run:

```bash
ABSENCE_PTO_TEST_DATABASE=1 .venv/bin/python -m pytest tests/test_absence_pto_store.py tests/test_ci_workflow.py -q
.venv/bin/ruff check src/zira_dashboard/absence_pto_store.py tests/test_absence_pto_store.py
```

Expected: all tests pass; live DB tests skip only when the guarded database is unavailable; Ruff exits 0.

- [ ] **Step 6: Add the patch note, commit, and push**

Use:

```markdown
### Past PTO requests have a safe home

- **Plant Manager can now safely remember a request to pay a missed day with PTO.** Workers cannot use it yet, and no Odoo time off changes yet.
```

Then:

```bash
git add CHANGELOG.md src/zira_dashboard/_schema.py src/zira_dashboard/absence_pto_store.py tests/test_absence_pto_store.py tests/test_ci_workflow.py .github/workflows/tests.yml
git commit -m "feat: store linked absence PTO requests"
git push origin main
```

### Task 3: Implement eligibility, submission, and employee routes

**Files:**
- Create: `src/zira_dashboard/absence_pto.py`
- Create: `src/zira_dashboard/routes/timeclock_absence_pto.py`
- Create: `src/zira_dashboard/templates/timeclock_absence_pto_list.html`
- Create: `src/zira_dashboard/templates/timeclock_absence_pto_detail.html`
- Create: `tests/test_absence_pto.py`
- Create: `tests/test_timeclock_absence_pto_routes.py`
- Modify: `src/zira_dashboard/app.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `staffing_hours.current_pay_period_bounds`, `absence_pto_store.create_request/get_request/list_for_person`, `time_off_balances.refresh_for_employee/get_for_employee`.
- Produces: `PtoType`, `AbsenceCandidate`, `SubmissionError`, `resolve_paid_time_off_type()`, `list_candidates(person_odoo_id, today)`, `submit(person_id, person_odoo_id, person_name, day, note, today)`, and `employee_requests(person_odoo_id)`.
- Employee routes: `GET /timeclock/time-off/past-absence/{token}`, `POST /timeclock/time-off/past-absence/{token}/{day}`, and `GET /timeclock/time-off/past-absence/{token}/requests/{request_id}`.

- [ ] **Step 1: Write failing pure eligibility tests**

Cover exact type resolution, ambiguous/missing types, current/future dates, prior-period dates, another employee, active/approved/manual-resolution exclusions, denied resubmission, low balance visibility, and server-side revalidation:

```python
def test_resolve_paid_time_off_type_requires_one_exact_allocated_day_type(monkeypatch):
    monkeypatch.setattr(domain.db, "query", lambda *_: [{
        "holiday_status_id": 7,
        "name": "Paid Time Off",
        "request_unit": "day",
        "requires_allocation": "yes",
        "active": True,
    }])
    assert domain.resolve_paid_time_off_type() == domain.PtoType(7, "Paid Time Off")


def test_candidate_stays_visible_but_disabled_when_balance_is_low(monkeypatch):
    _wire_candidate(monkeypatch, absence_day=date(2026, 8, 20), balance=0.5)
    rows = domain.list_candidates(44, date(2026, 8, 28))
    assert rows[0].eligible is False
    assert rows[0].blocked_reason == "You need 1 PTO day. You have 0.5."


def test_submit_rejects_forged_or_prior_period_day(monkeypatch):
    _wire_candidate(monkeypatch, absence_day=date(2026, 8, 15), balance=4.0)
    with pytest.raises(domain.SubmissionError, match="current pay period"):
        domain.submit(3, 44, "Ana", date(2026, 8, 15), "", date(2026, 8, 28))
```

- [ ] **Step 2: Write failing token/ownership route tests**

Use the existing `_verify_token`, `_person_by_id`, and `_mint_token` seams. Assert a bad token redirects, a valid token renders only the authenticated person's rows, a forged date returns 422, duplicate submission returns 409, and the detail route rejects another person's request.

- [ ] **Step 3: Run the tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_absence_pto.py tests/test_timeclock_absence_pto_routes.py -q
```

Expected: collection fails because the modules and routes do not exist.

- [ ] **Step 4: Implement the pure-facing domain API**

Use frozen result types and one authoritative validator:

```python
@dataclass(frozen=True)
class PtoType:
    holiday_status_id: int
    name: str


@dataclass(frozen=True)
class AbsenceCandidate:
    day: date
    eligible: bool
    blocked_reason: str | None
    available_practical: float | None


def _validate_submission(person_odoo_id: int, day: date, today: date) -> tuple[PtoType, float, int | None]:
    start, end = staffing_hours.current_pay_period_bounds(today)
    if day >= today:
        raise SubmissionError("Choose an absence before today.")
    if not start <= day <= end:
        raise SubmissionError("That absence is not in the current pay period.")
```

After those date checks, query `manual_absences` by `(day, str(person_odoo_id))` and require exactly one row. Resolve exactly one PTO type, reject active/approved/manually-resolved duplicates, refresh and reread the matching balance, and require `available_practical >= 1.0`. Return the resolved PTO type, the practical balance, and `manual_absences.odoo_leave_id`. Unit-test each validation branch instead of accepting posted copies of these values.

Catch the partial-unique-index race in `submit` and translate only that named constraint into `SubmissionError("A PTO request already exists for this absence.")`; re-raise unrelated database errors.

- [ ] **Step 5: Implement the routes and accessible templates**

The POST must derive every identity from the token and route day:

```python
@router.post("/timeclock/time-off/past-absence/{token}/{day}")
def submit_past_absence(request: Request, token: str, day: date, note: str = Form("")):
    person_id = _verify_token(token)
    if person_id is None:
        return _expired_redirect(request)
    person = _person_by_id(person_id)
    if not person or not person.get("odoo_id"):
        return RedirectResponse("/timeclock", status_code=303)
    try:
        linked = absence_pto.submit(
            person_id,
            int(person["odoo_id"]),
            person["name"],
            day,
            note.strip(),
            plant_today(),
        )
    except absence_pto.SubmissionError as error:
        return _render_list(request, person, person_id, error=str(error), status_code=422)
    return RedirectResponse(
        f"/timeclock/time-off/past-absence/{_mint_token(person_id)}/requests/{linked.id}",
        status_code=303,
    )
```

Use large touch targets, `aria-describedby` for disabled explanations, HTML escaping through Jinja, and personalized `timeclock_i18n` context. Register the router in `app.py`, but do not add the landing-page link until Task 9.

- [ ] **Step 6: Verify the domain and route slice**

Run:

```bash
.venv/bin/python -m pytest tests/test_absence_pto.py tests/test_timeclock_absence_pto_routes.py tests/test_time_off_routes.py tests/test_timeclock_time_off_static.py -q
.venv/bin/ruff check src/zira_dashboard/absence_pto.py src/zira_dashboard/routes/timeclock_absence_pto.py tests/test_absence_pto.py tests/test_timeclock_absence_pto_routes.py
```

Expected: all tests pass; existing time-off tests remain green; Ruff exits 0.

- [ ] **Step 7: Add the patch note, commit, and push**

Use:

```markdown
### Missed-day PTO checks are ready

- **Plant Manager can now tell which missed days may use PTO.** It checks the worker, pay period, PTO kind, and balance. The new choice is not on the time clock yet.
```

Commit only the listed files and push:

```bash
git add CHANGELOG.md src/zira_dashboard/absence_pto.py src/zira_dashboard/routes/timeclock_absence_pto.py src/zira_dashboard/templates/timeclock_absence_pto_list.html src/zira_dashboard/templates/timeclock_absence_pto_detail.html src/zira_dashboard/app.py tests/test_absence_pto.py tests/test_timeclock_absence_pto_routes.py
git commit -m "feat: validate retroactive absence PTO requests"
git push origin main
```

### Task 4: Add narrow Odoo leave and Wendy-task primitives

**Files:**
- Modify: `src/zira_dashboard/_odoo_time_off.py`
- Modify: `src/zira_dashboard/_odoo_feedback.py`
- Modify: `src/zira_dashboard/odoo_client.py`
- Modify: `tests/test_odoo_client.py`
- Modify: `tests/test_feedback_odoo.py`
- Modify: `tests/test_odoo_task_helpers.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: `fetch_leave_snapshot(leave_id)`, `find_matching_leaves(employee_id, type_id, day, include_terminal=True)`, `find_active_users_by_login(login, limit=2)`, and `close_task(task_id)`.
- Leave snapshots normalize `id`, `employee_id`, `holiday_status_id`, `request_date_from`, `request_date_to`, and `state`.
- Exact find methods return lists so callers can treat multiple matches as unsafe.

- [ ] **Step 1: Write failing Odoo-facade tests**

Pin exact models, fields, limits, context, and normalization:

```python
def test_find_active_users_by_login_is_exact_and_bounded(monkeypatch):
    execute = MagicMock(return_value=[{"id": 17, "login": "wendy@gruberpallets.com"}])
    monkeypatch.setattr(odoo_client, "execute", execute)
    assert odoo_client.find_active_users_by_login("wendy@gruberpallets.com") == [
        {"id": 17, "login": "wendy@gruberpallets.com"}
    ]
    execute.assert_called_once_with(
        "res.users",
        "search_read",
        [("active", "=", True), ("login", "=ilike", "wendy@gruberpallets.com")],
        fields=["id", "login"],
        limit=2,
    )


def test_fetch_leave_snapshot_reads_verified_identity(monkeypatch):
    execute = MagicMock(return_value=[{
        "id": 91,
        "employee_id": [44, "Ana"],
        "holiday_status_id": [7, "Paid Time Off"],
        "request_date_from": "2026-08-20",
        "request_date_to": "2026-08-20",
        "state": "validate",
    }])
    row = time_off.fetch_leave_snapshot(execute, 91)
    assert row == {
        "id": 91, "employee_id": 44, "holiday_status_id": 7,
        "date_from": date(2026, 8, 20), "date_to": date(2026, 8, 20),
        "state": "validate",
    }
```

Also test malformed IDs, wrong login echo, multiple users, no leave, multiple matching PTO leaves, active-test inclusion for terminal recovery, and `close_task` writing `active=False` only.

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_odoo_client.py tests/test_feedback_odoo.py tests/test_odoo_task_helpers.py -q
```

Expected: FAIL because the new facade methods are missing.

- [ ] **Step 3: Implement exact, validated helpers**

The user lookup must normalize then verify the echoed login:

```python
def find_active_users_by_login(execute_fn, login: str, limit: int = 2) -> list[dict]:
    normalized = login.strip().casefold()
    if login != normalized or "@" not in normalized or limit != 2:
        raise ValueError("login must be a normalized email and limit must be 2")
    rows = execute_fn(
        "res.users", "search_read",
        [("active", "=", True), ("login", "=ilike", normalized)],
        fields=["id", "login"], limit=limit,
    ) or []
    return [row for row in rows if str(row.get("login") or "").casefold() == normalized]
```

`find_matching_leaves` must query the exact employee/type/day range, return at most two IDs/snapshots, include terminal states only when requested, and never collapse multiple matches to one.

- [ ] **Step 4: Verify Odoo contracts and existing sync tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_odoo_client.py tests/test_feedback_odoo.py tests/test_odoo_task_helpers.py tests/test_time_off_sync.py tests/test_absence_sync.py -q
.venv/bin/ruff check src/zira_dashboard/_odoo_time_off.py src/zira_dashboard/_odoo_feedback.py src/zira_dashboard/odoo_client.py
```

Expected: all tests pass; Ruff exits 0.

- [ ] **Step 5: Add the patch note, commit, and push**

Use:

```markdown
### Odoo checks the exact PTO record

- **Plant Manager can now read back the exact Odoo time-off record and find Wendy's Odoo account safely.** It still does not change a past absence yet.
```

Then commit and push the scoped files.

```bash
git add CHANGELOG.md src/zira_dashboard/_odoo_time_off.py src/zira_dashboard/_odoo_feedback.py src/zira_dashboard/odoo_client.py tests/test_odoo_client.py tests/test_feedback_odoo.py tests/test_odoo_task_helpers.py
git commit -m "feat: add verified Odoo PTO helpers"
git push origin main
```

### Task 5: Implement the verified conversion happy path

**Files:**
- Create: `src/zira_dashboard/absence_pto_conversion.py`
- Create: `tests/test_absence_pto_conversion.py`
- Modify: `src/zira_dashboard/absence_pto_store.py`
- Modify: `src/zira_dashboard/time_off_audit.py`
- Modify: `src/zira_dashboard/_schema.py`
- Modify: `tests/test_time_off_audit.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 2 lease/transition API and Task 4 Odoo snapshots/matches.
- Produces: `approve(request_id, actor_upn, actor_name, source, now=None) -> ConversionResult` and `resume(request_id, now=None) -> ConversionResult`.
- `ConversionResult.status`: `approved | pending | needs_review | busy`.

Define the public result exactly so routes and the reconciler share one contract:

```python
@dataclass(frozen=True)
class ConversionResult:
    status: Literal["approved", "pending", "needs_review", "busy"]
    message: str
    request: AbsencePtoRequest | None
```

- [ ] **Step 1: Write the failing happy-path and idempotency tests**

Use a stateful fake Odoo, not ordered mocks, so each read sees the fake's current state:

```python
def test_approve_refuses_absence_then_creates_and_approves_pto(monkeypatch):
    fake = FakeOdoo(absence=_leave(70, 44, 9, "validate"))
    wire(monkeypatch, fake, _request(original_absence_leave_id=70))
    result = conversion.approve(41, "dale@gruberpallets.com", "Dale", "page", NOW)
    assert result.status == "approved"
    assert fake.events == [
        ("refuse", 70),
        ("create", 44, 7, date(2026, 8, 20)),
        ("confirm", 71),
        ("approve", 71),
    ]
    assert fake.read(71)["state"] == "validate"
    assert store.get_request(41).pto_leave_id == 71


def test_second_approve_adopts_verified_result_without_mutating_odoo(monkeypatch):
    fake = FakeOdoo(pto=_leave(71, 44, 7, "validate"))
    wire(monkeypatch, fake, _request(state="approved", pto_leave_id=71))
    assert conversion.approve(41, "dale@gruberpallets.com", "Dale", "page", NOW).status == "approved"
    assert fake.events == []
```

Also cover local-only absence (no original leave), exact PTO match adoption after a lost create response, balance recheck, wrong Odoo identity, two matching PTO records, lease contention, and two concurrent managers.

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_absence_pto_conversion.py tests/test_time_off_audit.py -q
```

Expected: collection fails because `absence_pto_conversion` does not exist.

- [ ] **Step 3: Extend the audit namespace before conversion code**

Add `request_kind TEXT NOT NULL DEFAULT 'time_off'`, `request_key TEXT`, and `detail JSONB` to `time_off_decisions`. Extend the existing keyword-only `record_decision` signature with these final parameters so old callers remain source-compatible: `request_kind: str = "time_off"`, `request_key: str | None = None`, and `detail: dict[str, Any] | None = None`. Keep the current insert behavior and add the three new columns/values to that same insert.

Serialize `detail` with psycopg2 JSON support or `json.dumps`; do not interpolate JSON into SQL.

- [ ] **Step 4: Implement the leased, verified happy path**

The orchestrator must follow this shape:

```python
def approve(request_id: int, actor_upn: str | None, actor_name: str | None,
            source: str | None, now: datetime | None = None) -> ConversionResult:
    owner = uuid4()
    current = store.claim_request(request_id, owner, _now(now), lease_seconds=120)
    if current is None:
        return ConversionResult("busy", "This request is already being checked.", None)
    try:
        return _resume_claim(current, owner, actor_upn, actor_name, source, _now(now))
    finally:
        store.release_claim(request_id, owner)
```

`_resume_claim` must reread and validate the absence, current period, PTO type, practical balance, and all known Odoo IDs before each mutation. Persist/commit each step before the next Odoo call. After PTO reaches verified `validate`, finalize in one local transaction: upsert the PTO mirror, settle the old Absence mirror, update `manual_absences.odoo_leave_id`, mark the linked request approved, append namespaced decision/inbox audit, and invalidate balance/Staffing/Time Off caches.

If the fresh practical balance is below one day, perform no Odoo mutation, transition `converting` back to `pending`, release the lease, and return `ConversionResult("pending", "The current PTO balance is below one day.", refreshed_request)`. This is a retryable manager-visible block, not a Wendy review case.

- [ ] **Step 5: Verify happy path, audit, and ordinary approvals**

Run:

```bash
.venv/bin/python -m pytest tests/test_absence_pto_conversion.py tests/test_time_off_audit.py tests/test_time_off_decisions.py tests/test_time_off_sync.py -q
.venv/bin/ruff check src/zira_dashboard/absence_pto_conversion.py src/zira_dashboard/absence_pto_store.py src/zira_dashboard/time_off_audit.py
```

Expected: all tests pass; ordinary time-off audit defaults remain unchanged.

- [ ] **Step 6: Add the patch note, commit, and push**

Use:

```markdown
### Past PTO can change safely in Odoo

- **Plant Manager can now safely change an approved missed day into approved PTO after a manager says yes.** The worker's absence record stays in place. The approval button is not connected yet.
```

Commit the scoped files and push to `origin/main`.

```bash
git add CHANGELOG.md src/zira_dashboard/absence_pto_conversion.py src/zira_dashboard/absence_pto_store.py src/zira_dashboard/time_off_audit.py src/zira_dashboard/_schema.py tests/test_absence_pto_conversion.py tests/test_time_off_audit.py
git commit -m "feat: convert approved absences to PTO"
git push origin main
```

### Task 6: Add compensation and restart-safe reconciliation

**Files:**
- Modify: `src/zira_dashboard/absence_pto_conversion.py`
- Modify: `src/zira_dashboard/absence_pto_store.py`
- Modify: `tests/test_absence_pto_conversion.py`
- Create: `src/zira_dashboard/absence_pto_review.py`
- Create: `tests/test_absence_pto_review.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: `reconcile_once(now=None, limit=25) -> ReconcileResult` initially for interrupted conversion and period rollover; Task 7 extends it with task delivery/external resolution.
- Compensation invariant: return to `pending` only after the original Absence is verified `validate` and no active PTO remains; otherwise move to `needs_review`.

- [ ] **Step 1: Add failing tests for every durable restart point**

Parameterize `not_started`, `absence_refused`, `pto_created`, and `pto_approved`. For each, assert the service rereads Odoo, resumes only the missing operation, and never repeats a verified mutation.

```python
@pytest.mark.parametrize("step", [
    "not_started", "absence_refused", "pto_created", "pto_approved",
])
def test_resume_from_each_durable_step(monkeypatch, step):
    fake, request = scenario_for(step)
    wire(monkeypatch, fake, request)
    result = conversion.resume(request.id, NOW)
    assert result.status == "approved"
    assert fake.duplicate_creates == 0
    assert fake.final_pto_count == 1
```

Add failures: refusal error before change, PTO create validation error, approve error, ambiguous timeout after create, incomplete PTO refusal, original Absence restore failure, app crash after Odoo approval before local finalization, missing absence row, expired lease takeover, and period rollover immediately before approval.

- [ ] **Step 2: Run the failure matrix and verify failures**

Run:

```bash
.venv/bin/python -m pytest tests/test_absence_pto_conversion.py tests/test_absence_pto_review.py -q
```

Expected: new cases fail because compensation/reconciliation is absent.

- [ ] **Step 3: Implement one compensation function with verified postconditions**

Use one path from every failure after refusal:

```python
def _compensate(request: AbsencePtoRequest, owner: UUID, error: Exception) -> ConversionResult:
    _close_incomplete_pto_if_present(request)
    pto_matches = odoo_client.find_matching_leaves(
        request.person_odoo_id, request.holiday_status_id, request.absence_day,
        include_terminal=False,
    )
    absence_ok = _restore_and_verify_absence(request.original_absence_leave_id)
    if not pto_matches and absence_ok:
        store.transition_to_pending(request.id, owner, error=_friendly(error))
        return ConversionResult("pending", "PTO was not applied. The absence was restored.", None)
    store.mark_needs_review(request.id, owner, error=_friendly(error))
    return ConversionResult("needs_review", "This needs payroll review.", None)
```

For a local-only absence, `absence_ok` means there was no original Odoo Absence to restore. `_restore_and_verify_absence` must reset/approve/read and require exact employee/type/day identity, not state alone.

- [ ] **Step 4: Implement due claims and period rollover**

`reconcile_once` must:

1. mark expired-current-period `pending` requests as `needs_review` without Odoo mutation;
2. claim expired leases on `converting` rows and call `conversion.resume`;
3. isolate one bad row so the batch continues; and
4. return exact counts for scanned, resumed, escalated, and failed rows.

Use bounded `FOR UPDATE SKIP LOCKED` claims in the store. A row leaving `pending` at period rollover must store `sync_error="Configured pay period closed before approval."`.

- [ ] **Step 5: Verify recovery and ordinary absence behavior**

Run:

```bash
.venv/bin/python -m pytest tests/test_absence_pto_conversion.py tests/test_absence_pto_review.py tests/test_absence_sync.py tests/test_late_report_absence_odoo.py -q
.venv/bin/ruff check src/zira_dashboard/absence_pto_conversion.py src/zira_dashboard/absence_pto_review.py
```

Expected: all tests pass; no compensation case creates a duplicate leave.

- [ ] **Step 6: Add the patch note, commit, and push**

Use:

```markdown
### Past PTO changes can recover

- **If Odoo stops in the middle of a past PTO change, Plant Manager can safely continue or put the old absence back.** Unclear cases stop for a person to review.
```

Commit the scoped files and push.

```bash
git add CHANGELOG.md src/zira_dashboard/absence_pto_conversion.py src/zira_dashboard/absence_pto_store.py src/zira_dashboard/absence_pto_review.py tests/test_absence_pto_conversion.py tests/test_absence_pto_review.py
git commit -m "feat: recover interrupted absence PTO changes"
git push origin main
```

### Task 7: Deliver and close Wendy's Odoo review tasks

**Files:**
- Modify: `src/zira_dashboard/absence_pto_review.py`
- Modify: `src/zira_dashboard/absence_pto_store.py`
- Modify: `src/zira_dashboard/_odoo_feedback.py`
- Modify: `src/zira_dashboard/odoo_client.py`
- Modify: `tests/test_absence_pto_review.py`
- Modify: `tests/test_odoo_task_helpers.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: `sync_review_task(request_id, now=None)`, `resolve_external_pto(request_id, now=None)`, `resolve_manually(request_id, actor_upn, actor_name, note, now=None)`, and completed `reconcile_once`.
- Task title: `[GPI-PM-PTO-{request_id}] Review {person_name} — {YYYY-MM-DD}`.
- Task deadline: next configured plant business day.

- [ ] **Step 1: Write failing Wendy identity and task lifecycle tests**

Cover exact one-user success, zero/multiple-user delivery errors, escaped task HTML, deterministic title, deadline, create timeout followed by exact adoption, duplicate exact tasks causing a blocked error, saved task ID before later calls, body update rather than recreation, matching externally approved PTO, manual note requirement, and close behavior.

```python
def test_review_task_is_assigned_to_wendy_and_saved(monkeypatch):
    fake = ReviewFake(users=[{"id": 17, "login": "wendy@gruberpallets.com"}])
    wire_review(monkeypatch, fake, _needs_review())
    result = review.sync_review_task(41, NOW)
    assert result.task_id == 501
    assert fake.created[0]["assignee_uid"] == 17
    assert store.get_request(41).odoo_task_id == 501


def test_marking_odoo_task_done_does_not_claim_pto_was_paid(monkeypatch):
    wire_review(monkeypatch, ReviewFake(task_stage="Done"), _needs_review())
    review.reconcile_once(NOW)
    assert store.get_request(41).state == "needs_review"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_absence_pto_review.py tests/test_odoo_task_helpers.py -q
```

Expected: task lifecycle cases fail because the service is incomplete.

- [ ] **Step 3: Implement deterministic task content and delivery**

Build all content with `html.escape` and no absence reason:

```python
WENDY_LOGIN = "wendy@gruberpallets.com"


def task_name(row: AbsencePtoRequest) -> str:
    return f"[GPI-PM-PTO-{row.id}] Review {row.person_name} — {row.absence_day.isoformat()}"


def task_body(row: AbsencePtoRequest, app_url: str) -> str:
    return (
        "<p>Plant Manager could not safely finish a past PTO request.</p>"
        f"<p><strong>Worker:</strong> {html.escape(row.person_name)}<br>"
        f"<strong>Missed day:</strong> {row.absence_day.isoformat()}<br>"
        f"<strong>Requested PTO:</strong> {html.escape(row.leave_type_name)}<br>"
        f"<strong>Balance when requested:</strong> {row.balance_at_submit}<br>"
        f"<strong>Original Absence ID:</strong> {row.original_absence_leave_id or 'None'}<br>"
        f"<strong>Replacement PTO ID:</strong> {row.pto_leave_id or 'None'}<br>"
        f"<strong>Requested:</strong> {row.requested_at.date().isoformat()}<br>"
        f"<strong>Manager attempt:</strong> {html.escape(row.decided_by_name or 'None')}<br>"
        f"<strong>Stopped because:</strong> {html.escape(row.sync_error or 'Review required')}<br>"
        f"<strong>Last safe step:</strong> {html.escape(row.conversion_step)}<br>"
        f"<strong>Review:</strong> <a href=\"{html.escape(app_url, quote=True)}\">Open Plant Manager</a></p>"
    )
```

Resolve exactly one Wendy user. Search exact task title before create and again after ambiguous network errors. Save the task ID immediately. Set the deadline to the next configured plant business day. Retry recoverable errors with bounded backoff in `task_attempts/task_next_at`; multiple exact tasks set a permanent delivery error for manager review.

- [ ] **Step 4: Implement external and manual resolution**

`resolve_external_pto` requires exactly one matching `validate` PTO snapshot, finalizes local mirrors through the same Task 5 finalizer, posts a resolved message, and archives/closes the task. `resolve_manually` requires a nonblank note, records actor/time/namespaced audit, sets `resolved_manually`, posts the escaped note, and closes the task without creating a PTO mirror.

- [ ] **Step 5: Verify task delivery and reconciliation**

Run:

```bash
.venv/bin/python -m pytest tests/test_absence_pto_review.py tests/test_feedback_odoo.py tests/test_odoo_task_helpers.py -q
.venv/bin/ruff check src/zira_dashboard/absence_pto_review.py src/zira_dashboard/_odoo_feedback.py src/zira_dashboard/odoo_client.py
```

Expected: all tests pass; task retries produce at most one active exact task.

- [ ] **Step 6: Add the patch note, commit, and push**

Use:

```markdown
### Wendy gets unclear past PTO cases

- **When a pay period closes or a past PTO change is unclear, Plant Manager now makes one Odoo task for Wendy.** The task explains what needs review without changing payroll again.
```

Commit and push the scoped files.

```bash
git add CHANGELOG.md src/zira_dashboard/absence_pto_review.py src/zira_dashboard/absence_pto_store.py src/zira_dashboard/_odoo_feedback.py src/zira_dashboard/odoo_client.py tests/test_absence_pto_review.py tests/test_odoo_task_helpers.py
git commit -m "feat: send absence PTO reviews to Wendy"
git push origin main
```

### Task 8: Connect manager approvals, denials, and manual resolution

**Files:**
- Create: `src/zira_dashboard/routes/absence_pto_admin.py`
- Create: `tests/test_absence_pto_admin_routes.py`
- Modify: `src/zira_dashboard/routes/time_off_approvals.py`
- Modify: `src/zira_dashboard/routes/time_off.py`
- Modify: `src/zira_dashboard/templates/_time_off_approvals_panel.html`
- Modify: `src/zira_dashboard/static/time_off_approvals.js`
- Modify: `src/zira_dashboard/app.py`
- Modify: `tests/test_time_off_approvals.py`
- Modify: `tests/test_exceptions_odoo_error.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `absence_pto_conversion.approve`, `absence_pto.deny`, `absence_pto_review.resolve_manually`.
- Manager endpoints: `POST /api/exceptions/absence-pto/{id}/approve`, `/deny`, and `/handled`.
- Queue rows gain `request_kind="absence_pto"` and `action_base="/api/exceptions/absence-pto/{id}"`; ordinary rows retain existing URLs.

- [ ] **Step 1: Write failing merged-queue and route tests**

Assert linked rows show Past absence, Absent · unpaid, one PTO day, current balance, open/closed state, and distinct URLs. Route tests must cover actor capture, approval success/busy/pending/review responses, required denial reason, denial's zero Odoo calls, handled-note requirement, and cache refresh.

```python
def test_linked_pending_payload_has_distinct_action_contract(monkeypatch):
    monkeypatch.setattr(page.absence_pto_store, "list_pending", lambda: [_linked()])
    rows = page._pending_payload(date(2026, 8, 28))
    linked = next(row for row in rows if row["request_kind"] == "absence_pto")
    assert linked["action_base"] == "/api/exceptions/absence-pto/41"
    assert linked["leave_type"] == "Paid Time Off"
    assert linked["date_label"] == "2026-08-20"
    assert linked["past_absence"] is True
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_absence_pto_admin_routes.py tests/test_time_off_approvals.py -q
```

Expected: collection or assertions fail because the admin router and merged payload are absent.

- [ ] **Step 3: Merge rows without branching URLs in JavaScript**

Render exact metadata:

```html
<tr class="exception-row"
    data-request-id="{{ r.id }}"
    data-request-kind="{{ r.request_kind }}"
    data-action-base="{{ r.action_base }}">
```

In JavaScript, replace the hard-coded ordinary URL with:

```javascript
var base = row.dataset.actionBase || ('/api/exceptions/time-off/' + id);
postJson(base + (row.dataset.requestKind === 'absence_pto' ? '/deny' : '/refuse'), payload);
```

Approval always uses `base + '/approve'`. Show `resp.warning`/`resp.error` exactly and leave `needs_review` rows visible with a **Mark handled** note/action instead of Approve.

- [ ] **Step 4: Implement thin authenticated manager routes**

Each async route parses JSON, derives the actor using `inbox_log.actor_from(request)`, calls one sync domain function through `asyncio.to_thread`, and returns its status contract. Do not place conversion logic in the route module.

- [ ] **Step 5: Verify manager and ordinary approval regressions**

Run:

```bash
.venv/bin/python -m pytest tests/test_absence_pto_admin_routes.py tests/test_time_off_approvals.py tests/test_exceptions_odoo_error.py tests/test_time_off_decisions.py -q
.venv/bin/ruff check src/zira_dashboard/routes/absence_pto_admin.py src/zira_dashboard/routes/time_off_approvals.py
```

Expected: all tests pass; ordinary approve/deny UI remains unchanged.

- [ ] **Step 6: Add the patch note, commit, and push**

Use:

```markdown
### Managers can review past PTO

- **Managers can now approve or deny a request to pay a missed day with PTO.** If the pay period closed, they can see that Wendy needs to review it.
```

Commit and push the scoped files.

```bash
git add CHANGELOG.md src/zira_dashboard/routes/absence_pto_admin.py src/zira_dashboard/routes/time_off_approvals.py src/zira_dashboard/routes/time_off.py src/zira_dashboard/templates/_time_off_approvals_panel.html src/zira_dashboard/static/time_off_approvals.js src/zira_dashboard/app.py tests/test_absence_pto_admin_routes.py tests/test_time_off_approvals.py tests/test_exceptions_odoo_error.py
git commit -m "feat: review absence PTO requests"
git push origin main
```

### Task 9: Ship employee entry points, Staffing labels, and the reconciler

**Files:**
- Modify: `src/zira_dashboard/templates/timeclock_time_off_landing.html`
- Modify: `src/zira_dashboard/routes/timeclock_time_off.py`
- Modify: `src/zira_dashboard/templates/timeclock_time_off_mine.html`
- Modify: `src/zira_dashboard/scheduler_time_off.py`
- Modify: `src/zira_dashboard/templates/staffing.html`
- Modify: `src/zira_dashboard/app.py`
- Modify: `src/zira_dashboard/routes/README.md`
- Modify: `tests/test_timeclock_time_off_static.py`
- Modify: `tests/test_time_off_routes.py`
- Modify: `tests/test_scheduler_time_off.py`
- Modify: `tests/test_staffing_static.py`
- Modify: `tests/test_page_warmer.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes all prior task APIs.
- Produces visible employee action, combined My Requests rows with `detail_url`, Staffing absence pay labels, and `_tick_absence_pto_reconcile` registered every 60 seconds.

- [ ] **Step 1: Write failing employee, Staffing, and warmer tests**

Pin the visible action/count, generic My Requests detail URL, every projection label, preserved `manual_absent=True`, no absence-count removal, and warmer registration:

```python
@pytest.mark.parametrize(("state", "label"), [
    (None, "Absent"),
    ("pending", "Absent · PTO pending"),
    ("converting", "Absent · PTO pending"),
    ("approved", "Absent · PTO"),
    ("needs_review", "Absent · PTO review"),
    ("resolved_manually", "Absent · handled"),
])
def test_manual_absence_keeps_attendance_and_adds_pay_suffix(monkeypatch, state, label):
    wire_absence(monkeypatch, linked_state=state)
    row = scheduler_time_off.time_off_entries_for_day(DAY)[0]
    assert row["manual_absent"] is True
    assert row["pay_type"] == label
    assert row["timing_label"] == label


def test_absence_pto_reconciler_runs_every_minute():
    entry = next(w for w in app_module._WARMERS if w[0] == "absence PTO reconcile")
    assert entry[1] is app_module._tick_absence_pto_reconcile
    assert entry[2] == 60
```

- [ ] **Step 2: Run the focused tests and verify failures**

Run:

```bash
.venv/bin/python -m pytest tests/test_timeclock_time_off_static.py tests/test_time_off_routes.py tests/test_scheduler_time_off.py tests/test_staffing_static.py tests/test_page_warmer.py -q
```

Expected: new entry point, labels, generic URLs, and warmer assertions fail.

- [ ] **Step 3: Add the employee entry point and combined My Requests rows**

Add a large card linking to `/timeclock/time-off/past-absence/{{ token }}` with the copy **Use PTO for a Past Absence** and an eligible/pending count. Normalize both ordinary and linked rows before rendering:

```python
{
    "request_kind": "absence_pto",
    "id": row.id,
    "type_name": "Past absence · Paid Time Off",
    "date_from": row.absence_day,
    "date_to": row.absence_day,
    "bucket": absence_pto.employee_state_label(row.state),
    "detail_url": f"/timeclock/time-off/past-absence/{token}/requests/{row.id}",
}
```

Change the template anchor to `href="{{ r.detail_url }}"`; ordinary rows receive their existing URL from the route.

- [ ] **Step 4: Project the linked state onto absences**

Fetch linked states for all absent employee IDs in one query, not one query per name. When adding each manual absence row, set `pay_type` and `timing_label` from this exact map:

```python
_ABSENCE_PAY_LABEL = {
    "pending": "Absent · PTO pending",
    "converting": "Absent · PTO pending",
    "approved": "Absent · PTO",
    "needs_review": "Absent · PTO review",
    "resolved_manually": "Absent · handled",
}
```

Keep `hours=None`, `manual_absent=True`, `pending=False`, and existing light-red styling. Do not add an editable ordinary-time-off button to absence rows.

- [ ] **Step 5: Register the reconciler and route inventory**

Add:

```python
async def _tick_absence_pto_reconcile():
    from . import absence_pto_review
    await asyncio.to_thread(absence_pto_review.reconcile_once)
```

Register `("absence PTO reconcile", _tick_absence_pto_reconcile, 60)` once. Update the route README for both new routers.

- [ ] **Step 6: Run focused tests, then the full suite and lint**

Run:

```bash
.venv/bin/python -m pytest tests/test_absence_pto_store.py tests/test_absence_pto.py tests/test_timeclock_absence_pto_routes.py tests/test_absence_pto_conversion.py tests/test_absence_pto_review.py tests/test_absence_pto_admin_routes.py tests/test_time_off_approvals.py tests/test_time_off_routes.py tests/test_scheduler_time_off.py tests/test_staffing_static.py tests/test_page_warmer.py -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

Expected: focused and full suites pass with zero failures; Ruff exits 0. If a pre-existing unrelated failure appears, record the exact command/output and do not claim the feature fully verified until the scoped suite and the original failure are distinguished.

- [ ] **Step 7: Perform a rendered workflow smoke test**

With a test employee/absence in a safe local database and Odoo calls replaced by the stateful fake:

1. Render the Time Off landing and open **Use PTO for a Past Absence**.
2. Confirm another employee's absence and a prior-period absence do not appear.
3. Submit one eligible day and confirm the Odoo fake records no calls.
4. Render manager approvals and deny once with a required reason.
5. Resubmit, approve, and confirm exactly one PTO create/approve sequence.
6. Render historical Staffing and verify `Absent · PTO` remains light red and counted absent.
7. Move a pending fixture past rollover and verify one Wendy task is adopted/created.

Save no production credentials or personal absence reasons in fixtures or logs.

- [ ] **Step 8: Add the final patch note, commit, and push**

Use:

```markdown
### Workers can use PTO for a missed day

#### Features

- **Workers can now ask to use one PTO day for a missed day in the current pay period.** The day still counts as an absence, a manager must approve it, and unclear closed-pay cases go to Wendy for review.
```

Then stage only the Task 9 files, commit, and push:

```bash
git add CHANGELOG.md src/zira_dashboard/templates/timeclock_time_off_landing.html src/zira_dashboard/routes/timeclock_time_off.py src/zira_dashboard/templates/timeclock_time_off_mine.html src/zira_dashboard/scheduler_time_off.py src/zira_dashboard/templates/staffing.html src/zira_dashboard/app.py src/zira_dashboard/routes/README.md tests/test_timeclock_time_off_static.py tests/test_time_off_routes.py tests/test_scheduler_time_off.py tests/test_staffing_static.py tests/test_page_warmer.py
git commit -m "feat: request PTO for recorded absences"
git push origin main
```

After pushing, verify `git rev-parse HEAD` matches `git ls-remote origin refs/heads/main` and report the exact full-suite counts.
