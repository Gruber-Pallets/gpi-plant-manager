# Timeclock Roster Single-Writer Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent stale local saves or incomplete Odoo responses from emptying the timeclock, while making any future zero-roster state visible and actionable.

**Architecture:** Odoo remains the only writer of employment status for existing people. The sync cross-checks its active-only employee read against a second read that explicitly includes archived records, updates only statuses that Odoo actually returned, and never infers archival from absence. The timeclock keeps using Postgres but renders a bilingual failure state and critical diagnostic log when no employee is available.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, PostgreSQL/psycopg2, Odoo XML-RPC, pytest, Ruff.

## Global Constraints

- Odoo sync is the sole owner of employment status for Odoo-backed people.
- Existing local roster saves may update Reserve and locally owned skills, but never `active`.
- A new local person may still be inserted as active.
- No employee may be deactivated merely because an Odoo response omitted them.
- Empty, malformed, duplicate-ID, or internally contradictory employee snapshots perform no roster or skill writes, do not advance `odoo_last_sync`, and retain an urgent alert.
- A valid sync clears the alert only after its roster transaction commits.
- A zero-person timeclock shows: “The employee list is unavailable. Please tell a manager.” and “La lista de empleados no está disponible. Avísale a un gerente.”
- Do not stage `.cursorignore`, `.python-version`, `uv.lock`, or any unrelated user changes.
- New `CHANGELOG.md` text uses short sentences and common words that a 10-year-old can understand.

---

## File Structure

- `src/zira_dashboard/staffing.py`: enforce the local-write ownership boundary.
- `src/zira_dashboard/odoo_client.py`: read explicit active statuses with archived records included.
- `src/zira_dashboard/odoo_sync.py`: validate the two employee snapshots, reject unsafe refreshes, and apply only explicit statuses.
- `src/zira_dashboard/exception_inbox.py`: describe all unsafe roster snapshots generically instead of only malformed active fields.
- `src/zira_dashboard/routes/timeclock.py`: pass an explicit empty-roster state and emit protected diagnostics.
- `src/zira_dashboard/templates/timeclock_home.html`: render the bilingual failure state without a dead search box.
- `tests/test_staffing_roster_status_ownership.py`: fast unit regression for stale local roster replay.
- `tests/test_odoo_client.py`: Odoo request-shape and unsafe-snapshot unit coverage.
- `tests/test_odoo_sync.py`: Postgres-backed explicit-status and omission behavior.
- `tests/test_exception_inbox.py`: manager-facing generic alert copy.
- `tests/test_timeclock_saturday_recruiting.py`: route behavior and critical-log coverage.
- `tests/test_timeclock_bilingual_render.py`: healthy and unavailable template rendering.
- `CHANGELOG.md`: plain-language user-facing patch note.

### Task 1: Stop Local Roster Saves from Replaying Employment Status

**Files:**

- Create: `tests/test_staffing_roster_status_ownership.py`
- Modify: `src/zira_dashboard/staffing.py:313-329`

**Interfaces:**

- Consumes: `staffing.Person` and `staffing.save_roster(people: list[Person]) -> None`.
- Produces: an upsert that uses `active` only on insert and never in the conflict-update clause.

- [ ] **Step 1: Write the failing stale-cache regression**

Create a fake context-managed cursor and assert against the exact ownership boundary:

```python
from contextlib import contextmanager

from zira_dashboard import db, staffing


def test_save_roster_never_updates_active_on_an_existing_person(monkeypatch):
    calls = []

    class Cursor:
        def execute(self, sql, params=None):
            calls.append((" ".join(sql.split()), params))

    @contextmanager
    def cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", cursor)

    staffing.save_roster([
        staffing.Person(
            name="Cached Worker",
            active=False,
            reserve=True,
            employee_id=41,
        )
    ])

    people_sql, params = calls[0]
    insert_clause, update_clause = people_sql.split("ON CONFLICT", 1)
    assert "active" in insert_clause
    assert "active =" not in update_clause
    assert params == ("Cached Worker", False, True, 41)
```

- [ ] **Step 2: Run the ownership test red**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_staffing_roster_status_ownership.py -q
```

Expected: FAIL because the conflict clause currently contains `active = EXCLUDED.active`.

- [ ] **Step 3: Remove `active` from the existing-row update**

Keep `active` in the `INSERT` values so a newly added local person remains active. Change only the conflict clause:

```python
cur.execute(
    "INSERT INTO people (name, active, reserve, odoo_id, local_dirty) "
    "VALUES (%s, %s, %s, %s, TRUE) "
    "ON CONFLICT (name) DO UPDATE SET reserve = EXCLUDED.reserve, "
    "odoo_id = COALESCE(EXCLUDED.odoo_id, people.odoo_id), "
    "local_dirty = TRUE",
    (p.name, p.active, p.reserve, p.employee_id),
)
```

Update the docstring to say explicitly that Odoo owns existing employment status.

- [ ] **Step 4: Run the ownership test green and commit**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_staffing_roster_status_ownership.py tests/test_object_api_models.py -q
.venv/bin/ruff check src/zira_dashboard/staffing.py tests/test_staffing_roster_status_ownership.py
git diff --check
```

Expected: all tests pass and lint/diff checks are clean.

Commit only these files:

```bash
git add src/zira_dashboard/staffing.py tests/test_staffing_roster_status_ownership.py
git commit -m "fix: keep roster status owned by Odoo"
git push origin main
```

### Task 2: Replace Absence-Based Deactivation with Explicit Odoo Status

**Files:**

- Modify: `src/zira_dashboard/odoo_client.py:371-383`
- Modify: `src/zira_dashboard/odoo_sync.py:237-438`
- Modify: `src/zira_dashboard/exception_inbox.py:444-476`
- Modify: `tests/test_odoo_client.py:17-128,426-442`
- Modify: `tests/test_odoo_sync.py:53-70,430-500`
- Modify: `tests/test_exception_inbox.py:500-522`

**Interfaces:**

- Produces: `odoo_client.fetch_employee_statuses() -> list[dict]` with records shaped as `{"id": int, "active": bool}`.
- Produces: `odoo_sync._employee_snapshot_error(active_rows, status_rows) -> tuple[str, int] | None`.
- Preserves: `odoo_sync.sync(force: bool = False) -> SyncResult`, where `employee_count` remains the active-employee count.

- [ ] **Step 1: Write the Odoo request-shape test**

Add this beside the existing `fetch_employees` test:

```python
def test_fetch_employee_statuses_includes_archived_records(monkeypatch):
    responses = {
        ("hr.employee", "search_read"): [
            {"id": 1, "active": True},
            {"id": 2, "active": False},
        ],
    }
    calls = _stub_execute(monkeypatch, responses)

    assert odoo_client.fetch_employee_statuses() == responses[("hr.employee", "search_read")]
    _model, _method, args, kwargs = calls[0]
    assert args[0] == []
    assert kwargs["fields"] == ["id", "active"]
    assert kwargs["context"] == {"active_test": False}
```

- [ ] **Step 2: Replace unsafe-payload tests with complete snapshot tests**

Keep the existing active-only contradiction test: a record returned from
`fetch_employees()` with `active is not True` must still be rejected. Add tests
that patch `fetch_employee_statuses` and prove these cases stop before
`fetch_skills_for` or `db.cursor`:

Update both existing malformed active-only fixtures to stub
`fetch_employee_statuses()` as well, because the sync obtains both snapshots
before validating them.

```python
@pytest.mark.parametrize(
    ("active_rows", "status_rows", "message"),
    [
        ([], [], "empty"),
        ([{"id": 1, "name": "A", "active": True}], [{"id": 1, "active": 1}], "malformed"),
        ([{"id": 1, "name": "A", "active": True}], [{"id": 1, "active": True}, {"id": 1, "active": True}], "duplicate"),
        ([{"id": 1, "name": "A", "active": True}], [{"id": 1, "active": False}], "contradict"),
    ],
)
def test_sync_rejects_unsafe_employee_snapshots_before_writes(
    monkeypatch, active_rows, status_rows, message
):
    from zira_dashboard import app_settings, db, odoo_sync

    saved = []
    monkeypatch.setattr(odoo_sync, "_read_last_sync", lambda: None)
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employees", lambda: active_rows)
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employee_statuses", lambda: status_rows)
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_skills_for",
        lambda _ids: pytest.fail("unsafe snapshot reached dependent reads"),
    )
    monkeypatch.setattr(db, "cursor", lambda: pytest.fail("unsafe snapshot reached writes"))
    monkeypatch.setattr(app_settings, "set_setting", lambda key, value: saved.append((key, value)))

    result = odoo_sync.sync(force=True)

    assert result.ok is False
    assert result.refreshed is False
    assert message in result.error.lower()
    assert saved[0][0] == odoo_sync.ROSTER_SYNC_ALERT_KEY
    assert saved[0][1]["error"] == result.error
    assert all(key != "odoo_last_sync" for key, _value in saved)
```

Update the successful-sync unit fixture to return
`[{"id": 1, "active": True}]` from `fetch_employee_statuses` and retain its
assertion that the prior alert clears only on success.

- [ ] **Step 3: Write Postgres regressions for omission and explicit archive**

Extend `_stub_client` with an optional `employee_statuses` argument. Its
default derives `id` and `active` from `employees`, while an explicit value is
returned unchanged from `fetch_employee_statuses`.

Replace the old absence-based deactivation test with one sequence:

```python
def test_sync_preserves_an_omitted_person_until_odoo_explicitly_archives_them(monkeypatch):
    from zira_dashboard import db

    employees = [
        {"id": 99100, "name": "Still Active", "active": True},
        {"id": 99101, "name": "Archived Later", "active": True},
    ]
    _stub_client(monkeypatch, employees, {}, [], {})
    assert odoo_sync.sync(force=True).ok is True

    _stub_client(
        monkeypatch,
        [employees[0]],
        {},
        [],
        {},
        employee_statuses=[{"id": 99100, "active": True}],
    )
    assert odoo_sync.sync(force=True).ok is True
    rows = db.query(
        "SELECT odoo_id, active FROM people WHERE odoo_id IN (99100, 99101) ORDER BY odoo_id"
    )
    assert rows == [{"odoo_id": 99100, "active": True}, {"odoo_id": 99101, "active": True}]

    _stub_client(
        monkeypatch,
        [employees[0]],
        {},
        [],
        {},
        employee_statuses=[
            {"id": 99100, "active": True},
            {"id": 99101, "active": False},
        ],
    )
    assert odoo_sync.sync(force=True).ok is True
    rows = db.query(
        "SELECT odoo_id, active FROM people WHERE odoo_id IN (99100, 99101) ORDER BY odoo_id"
    )
    assert rows == [{"odoo_id": 99100, "active": True}, {"odoo_id": 99101, "active": False}]
```

- [ ] **Step 4: Run the new sync tests red**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_odoo_client.py tests/test_exception_inbox.py -q
```

Expected: FAIL because `fetch_employee_statuses` and snapshot validation do
not exist and the Inbox copy is still specific to active-field corruption.
The Postgres regression is intentionally exercised by CI; locally it skips
unless a safe test database is configured.

- [ ] **Step 5: Add the archived-inclusive status read**

Add to `odoo_client.py` without changing the active-only `fetch_employees()`
contract:

```python
def fetch_employee_statuses() -> list[dict]:
    """All employee IDs and explicit active flags, including archived rows."""
    return execute(
        "hr.employee",
        "search_read",
        [],
        fields=["id", "active"],
        context={"active_test": False},
    )
```

- [ ] **Step 6: Validate both snapshots before dependent reads or writes**

Add a pure validator to `odoo_sync.py`. It must reject an empty active list,
non-dict rows, nonpositive or duplicate IDs, blank active-employee names,
non-Boolean `active`, a status list that omits an active ID, or a status that
contradicts the active-only read:

```python
def _employee_snapshot_error(
    active_rows: list[dict], status_rows: list[dict]
) -> tuple[str, int] | None:
    if not active_rows:
        return "Odoo active employee payload was empty; sync skipped.", 0

    active_by_id = {}
    invalid = 0
    for row in active_rows:
        employee_id = row.get("id") if isinstance(row, dict) else None
        name = (row.get("name") or "").strip() if isinstance(row, dict) else ""
        if (
            not isinstance(employee_id, int)
            or isinstance(employee_id, bool)
            or employee_id <= 0
            or employee_id in active_by_id
            or not name
            or row.get("active") is not True
        ):
            invalid += 1
        else:
            active_by_id[employee_id] = row

    status_by_id = {}
    for row in status_rows:
        employee_id = row.get("id") if isinstance(row, dict) else None
        active = row.get("active") if isinstance(row, dict) else None
        if (
            not isinstance(employee_id, int)
            or isinstance(employee_id, bool)
            or employee_id <= 0
            or employee_id in status_by_id
            or not isinstance(active, bool)
        ):
            invalid += 1
        else:
            status_by_id[employee_id] = active

    if invalid:
        return f"Odoo employee payload contained {invalid} malformed or duplicate record(s); sync skipped.", invalid
    if any(status_by_id.get(employee_id) is not True for employee_id in active_by_id):
        return "Odoo employee status payload contradicted or omitted active employees; sync skipped.", len(active_by_id)
    return None
```

Call `fetch_employee_statuses()` immediately after `fetch_employees()`, before
skills or any database cursor. On error, persist the existing roster alert
with `invalid_count`, `received_count`, `error`, and `detected_at`, then return
the failed `SyncResult` without changing the success timestamp.

- [ ] **Step 7: Apply only explicit statuses**

Keep the active upsert loop, but pass `True` explicitly and build skills only
for `active_rows`. Remove the `seen_employee_ids` set-difference update.
After active upserts, apply explicit archived statuses only to IDs present as
`False` in the validated status snapshot:

```python
inactive_ids = [int(row["id"]) for row in status_rows if row["active"] is False]
if inactive_ids:
    cur.execute(
        "UPDATE people SET active = FALSE, last_pulled_at = %s "
        "WHERE odoo_id = ANY(%s) AND active = TRUE",
        (pulled_at, inactive_ids),
    )
```

Do not insert never-seen archived employees and do not refresh/delete their
skills. Return `employee_count=len(active_rows)`.

Change the Inbox detail to generic copy:

```python
"Odoo sent an unsafe employee list. The timeclock is using the last good update."
```

- [ ] **Step 8: Run sync tests green and commit**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_odoo_client.py tests/test_odoo_sync_unit.py tests/test_exception_inbox.py tests/test_inbox_keys.py tests/test_inbox_reconcile.py -q
.venv/bin/ruff check src/zira_dashboard/odoo_client.py src/zira_dashboard/odoo_sync.py src/zira_dashboard/exception_inbox.py tests/test_odoo_client.py tests/test_odoo_sync.py tests/test_exception_inbox.py
git diff --check
```

Expected: pure tests pass; Postgres-only tests skip without `DATABASE_URL`.

Commit only the Task 2 files:

```bash
git add src/zira_dashboard/odoo_client.py src/zira_dashboard/odoo_sync.py src/zira_dashboard/exception_inbox.py tests/test_odoo_client.py tests/test_odoo_sync.py tests/test_exception_inbox.py
git commit -m "fix: require explicit Odoo roster status"
git push origin main
```

### Task 3: Replace the Silent Empty Timeclock with a Bilingual Failure State

**Files:**

- Modify: `src/zira_dashboard/routes/timeclock.py:516-533`
- Modify: `src/zira_dashboard/templates/timeclock_home.html:75-118`
- Modify: `tests/test_timeclock_saturday_recruiting.py:60-110`
- Modify: `tests/test_timeclock_bilingual_render.py:45-56`

**Interfaces:**

- Produces: template context `roster_unavailable: bool`.
- Preserves: the existing `/timeclock` route, search input, employee links, Saturday banner, and session-expired behavior when people exist.

- [ ] **Step 1: Write route and template regressions**

Add a route test that returns no people, disables the Saturday banner, and
patches the diagnostic reads:

```python
def test_home_turns_an_empty_roster_into_a_bilingual_manager_alert(monkeypatch, caplog):
    monkeypatch.setattr(timeclock.db, "query", lambda *_args: [])
    monkeypatch.setattr(timeclock, "_saturday_banner_context", lambda: None)
    monkeypatch.setattr(timeclock.odoo_sync, "_read_last_sync", lambda: "last-good")
    monkeypatch.setattr(
        timeclock.odoo_sync,
        "roster_sync_alert",
        lambda: {"error": "unsafe snapshot"},
    )

    with caplog.at_level("CRITICAL"):
        response = client.get("/timeclock")

    assert response.status_code == 200
    assert "The employee list is unavailable. Please tell a manager." in response.text
    assert "La lista de empleados no está disponible. Avísale a un gerente." in response.text
    assert 'id="filter"' not in response.text
    assert "timeclock roster is empty" in caplog.text
    assert "last-good" in caplog.text
```

Add a template render test with one person and `roster_unavailable=False` that
asserts the employee link and search input remain and the unavailable copy is
absent.

- [ ] **Step 2: Run the timeclock tests red**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_timeclock_saturday_recruiting.py tests/test_timeclock_bilingual_render.py -q
```

Expected: FAIL because the route does not expose `roster_unavailable`, does
not log the critical diagnostic, and still renders the search box.

- [ ] **Step 3: Add guarded diagnostics and context**

Import `odoo_sync` in `routes/timeclock.py`. After the people query:

```python
roster_unavailable = not rows
if roster_unavailable:
    try:
        last_sync_at = odoo_sync._read_last_sync()
    except Exception:  # noqa: BLE001 -- diagnostics must never replace the kiosk response
        last_sync_at = "unavailable"
    _log.critical(
        "timeclock roster is empty; last_sync_at=%r roster_alert=%r",
        last_sync_at,
        odoo_sync.roster_sync_alert(),
    )
```

Pass `roster_unavailable` to the template. `roster_sync_alert()` already
contains its own storage guard.

- [ ] **Step 4: Render one of two mutually exclusive home states**

In `timeclock_home.html`, keep the session banner first. When
`roster_unavailable` is true, render:

```html
<div class="k-warning" role="alert"
     style="max-width: 900px; text-align: center; margin: 2rem auto 0; font-size: 1.5rem;">
  <strong>The employee list is unavailable. Please tell a manager.</strong>
  <span class="k-es" style="display:block;margin-top:0.5rem;">
    La lista de empleados no está disponible. Avísale a un gerente.
  </span>
</div>
```

Put the existing filter, grid, and filter-event script in the `{% else %}`
branch so the unavailable page has no nonfunctional search control. Keep the
Saturday modal script outside this branch.

- [ ] **Step 5: Run the timeclock tests green and commit**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_timeclock_home_static.py tests/test_timeclock_bilingual_render.py tests/test_timeclock_saturday_recruiting.py -q
.venv/bin/ruff check src/zira_dashboard/routes/timeclock.py tests/test_timeclock_saturday_recruiting.py tests/test_timeclock_bilingual_render.py
git diff --check
```

Expected: all tests pass with no lint or whitespace errors.

Commit only the Task 3 files:

```bash
git add src/zira_dashboard/routes/timeclock.py src/zira_dashboard/templates/timeclock_home.html tests/test_timeclock_saturday_recruiting.py tests/test_timeclock_bilingual_render.py
git commit -m "fix: surface an unavailable timeclock roster"
git push origin main
```

### Task 4: Verify, Document, Push, and Check Production

**Files:**

- Modify: `CHANGELOG.md`
- Modify: `docs/superpowers/specs/2026-08-12-timeclock-roster-single-writer-safety-design.md` only to set `Status: Implemented` after all verification succeeds.

**Interfaces:**

- Consumes: all Task 1-3 behavior.
- Produces: verified commits on `origin/main` plus production count, refresh, Reserve-save, and timeclock-page evidence.

- [ ] **Step 1: Add the plain-language patch note**

At the top of the existing August 12 section, add:

```markdown
### Names stay on the timeclock

#### Fixes

- **The timeclock now protects the list of names during updates.** Changing a person's Reserve setting cannot hide everyone. If the list ever cannot load, the tablet tells workers to get a manager instead of showing a blank page.
```

- [ ] **Step 2: Run focused and full local verification**

Run these fresh commands:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_staffing_roster_status_ownership.py tests/test_object_api_models.py tests/test_odoo_client.py tests/test_odoo_sync_unit.py tests/test_exception_inbox.py tests/test_inbox_keys.py tests/test_inbox_reconcile.py tests/test_timeclock_home_static.py tests/test_timeclock_bilingual_render.py tests/test_timeclock_saturday_recruiting.py -q
DATABASE_URL= .venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
.venv/bin/python -m compileall -q src
git diff --check
```

Expected: all runnable tests pass, Postgres-gated tests skip without a local
test database, Ruff is clean, compilation exits zero, and the diff check emits
no output.

- [ ] **Step 3: Review scope, mark the spec implemented, and commit**

Change the design status only after Step 2 succeeds. Then run:

```bash
git status --short
git diff -- CHANGELOG.md docs/superpowers/specs/2026-08-12-timeclock-roster-single-writer-safety-design.md
git add CHANGELOG.md docs/superpowers/specs/2026-08-12-timeclock-roster-single-writer-safety-design.md
git commit -m "docs: explain timeclock roster protection"
git push origin main
```

Do not add `.cursorignore`, `.python-version`, or `uv.lock`.

- [ ] **Step 4: Wait for Railway and verify the deployed revision**

Use Railway deployment status/logs to wait for the implementation commit to
report healthy. Verify the deployed commit hash matches `origin/main` before
production mutation checks.

- [ ] **Step 5: Verify the production failure path and normal path**

Run read-only production queries to record total, active, inactive, excluded,
and `local_dirty` counts. Confirm the `/timeclock` query returns the Odoo-active
people.

Run one normal forced `odoo_sync.sync(force=True)` and verify:

- `ok=True`, `refreshed=True`, and the expected active employee count;
- the active kiosk count remains unchanged;
- `odoo_last_sync` advances;
- `odoo_roster_sync_alert` is cleared; and
- no absent local record is newly deactivated unless the explicit status read
  marked it inactive.

Exercise `staffing.save_roster()` against one current Odoo-backed employee
using that person's existing values except a reversible Reserve toggle. Read
the employee status immediately afterward and prove `active` is unchanged.
Restore the original Reserve value through the same path and recheck status.

Finally load the authenticated live `/timeclock` page through the browser and
confirm employee buttons are present and the unavailable message is absent.
Any failed assertion keeps the task active and blocks the completion claim.
