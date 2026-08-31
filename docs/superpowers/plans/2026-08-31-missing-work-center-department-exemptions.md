# Missing Work Center Department Exemptions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop Maintenance, Transportation, and Supervisor employees from receiving Missing Work Center exceptions, using the employee's Odoo home department only when the attendance department is blank.

**Architecture:** Mirror `hr.employee.department_id` into `people.department_name`, resolve the attendance department before the employee fallback through one pure policy helper, and feed that effective department into both the legacy missing-WC shaper and the mirrored attendance timeline. Keep unknown departments conservative and preserve explicit administrator department choices.

**Tech Stack:** Python 3.11+, FastAPI service modules, Odoo XML-RPC, PostgreSQL schema bootstrap, pytest, Ruff.

## Global Constraints

- Maintenance, Transportation, and Supervisor do not require a work center by default.
- The attendance department wins whenever it is present; the employee's current Odoo home department is fallback-only.
- An explicit Settings choice remains authoritative and must not be overwritten by bootstrap or Odoo sync.
- A blank or unknown effective department continues to require a work center.
- Inbox request handling remains local-only and must not add an Odoo call.
- Existing resolved, dismissed, monitoring-boundary, and locally-unmapped behavior remains unchanged.
- New What's New text uses short sentences and common words and explains how the fix helps the person using the app.
- Preserve unrelated working-tree changes and push each completed implementation commit to `origin/main`.

---

### Task 1: Centralize department resolution and exemption defaults

**Files:**
- Modify: `src/zira_dashboard/attendance_location_policy.py:288-312`
- Modify: `src/zira_dashboard/_schema.py:78-92,184-196`
- Modify: `tests/test_attendance_location_policy.py:324-348`
- Modify: `tests/test_attendance_location_schema.py:1-18`
- Modify: `tests/test_attendance_location_department_sync.py:1-55`

**Interfaces:**
- Produces: `attendance_location_policy.effective_department_name(attendance_department_name: str | None, employee_department_name: str | None) -> str | None`.
- Produces: `people.department_name TEXT NULL` through idempotent schema bootstrap.
- Produces: Maintenance, Transportation, and Supervisor as the non-explicit default exemption set used by later tasks.

- [x] **Step 1: Write failing policy and schema tests**

Add these assertions to the policy test beside the existing department requirement test:

```python
def test_effective_department_prefers_attendance_then_employee_fallback():
    assert policy.effective_department_name(" 00 Maintenance ", "Transportation") == "Maintenance"
    assert policy.effective_department_name(None, " 06 Transportation ") == "Transportation"
    assert policy.effective_department_name("", "Supervisor") == "Supervisor"
    assert policy.effective_department_name(None, None) is None


def test_transportation_is_exempt_by_default():
    assert policy.default_department_requires_work_center("Transportation") is False
    assert policy.default_department_requires_work_center(" 06 Transportation ") is False
    assert policy.default_department_requires_work_center("Recycled") is True
    assert policy.default_department_requires_work_center(None) is True
```

Extend the static schema test:

```python
assert "ADD COLUMN IF NOT EXISTS department_name TEXT" in ddl
assert "IN ('maintenance', 'transportation', 'supervisor')" in " ".join(ddl.split())
```

Extend `test_department_sync_defaults_fresh_exempt_rows_without_overwriting_admin_choice` to pass `"Transportation"` and expect a non-explicit `False` row for it.

- [x] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_attendance_location_policy.py::test_effective_department_prefers_attendance_then_employee_fallback \
  tests/test_attendance_location_policy.py::test_transportation_is_exempt_by_default \
  tests/test_attendance_location_schema.py \
  tests/test_attendance_location_department_sync.py::test_department_sync_defaults_fresh_exempt_rows_without_overwriting_admin_choice
```

Expected: FAIL because `effective_department_name` and `people.department_name` do not exist and Transportation currently defaults to required.

- [x] **Step 3: Implement the pure resolver and idempotent defaults**

In `attendance_location_policy.py`, separate display-name cleaning from lowercase default matching:

```python
def _clean_department_name(department_name: str | None) -> str | None:
    if not department_name:
        return None
    cleaned = _NUMBERED_DEPARTMENT_PREFIX.sub("", department_name).strip()
    return cleaned or None


def _normalized_department_name(department_name: str | None) -> str:
    return (_clean_department_name(department_name) or "").lower()


def effective_department_name(
    attendance_department_name: str | None,
    employee_department_name: str | None,
) -> str | None:
    """Attendance department wins; employee department is fallback-only."""
    return _clean_department_name(attendance_department_name) or _clean_department_name(
        employee_department_name
    )
```

Add `"transportation"` to `default_department_requires_work_center`'s exempt set.

In `_schema.py`, add:

```sql
ALTER TABLE people ADD COLUMN IF NOT EXISTS department_name TEXT;
```

Change the non-explicit department migration predicate to:

```sql
IN ('maintenance', 'transportation', 'supervisor');
```

- [x] **Step 4: Run Task 1 tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_attendance_location_policy.py \
  tests/test_attendance_location_schema.py \
  tests/test_attendance_location_department_sync.py
```

Expected: all selected tests PASS; PostgreSQL-gated tests may report skipped when `DATABASE_URL` is unset.

- [x] **Step 5: Commit and push Task 1**

```bash
git add \
  src/zira_dashboard/attendance_location_policy.py \
  src/zira_dashboard/_schema.py \
  tests/test_attendance_location_policy.py \
  tests/test_attendance_location_schema.py \
  tests/test_attendance_location_department_sync.py
git commit -m "fix: exempt transportation from work centers"
git push origin main
```

---

### Task 2: Mirror the employee home department

**Files:**
- Modify: `src/zira_dashboard/odoo_client.py:276-290`
- Modify: `src/zira_dashboard/odoo_sync.py:65-71,493-520`
- Modify: `tests/test_odoo_client.py:516-531`
- Modify: `tests/test_odoo_sync.py:190-225`

**Interfaces:**
- Consumes: `attendance_location_policy.effective_department_name` from Task 1.
- Consumes: `people.department_name` from Task 1.
- Produces: active employee snapshots containing Odoo `department_id`.
- Produces: a clean nullable `people.department_name` updated on every successful roster sync.

- [x] **Step 1: Write failing Odoo fetch and sync tests**

In `test_fetch_employees_returns_active_only_with_required_fields`, capture the call keyword arguments and assert:

```python
_model, _method, _args, kwargs = calls[0]
assert "department_id" in kwargs["fields"]
```

Add this PostgreSQL-gated sync test after the full-name sync test:

```python
def test_sync_persists_clean_employee_home_department(monkeypatch):
    from zira_dashboard import db

    _stub_client(
        monkeypatch,
        employees=[{
            "id": 99007,
            "name": "Test Driver",
            "active": True,
            "work_email": False,
            "department_id": [6, "06 Transportation"],
        }],
        skills_for={},
        columns_meta=[],
        buckets={},
    )

    assert odoo_sync.sync(force=True).ok is True
    assert db.query(
        "SELECT department_name FROM people WHERE odoo_id = 99007"
    ) == [{"department_name": "Transportation"}]
```

- [x] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_odoo_client.py::test_fetch_employees_returns_active_only_with_required_fields \
  tests/test_odoo_sync.py::test_sync_persists_clean_employee_home_department
```

Expected: the client test FAILS because `department_id` is absent; the sync test either FAILS because `department_name` is not written or is skipped without PostgreSQL.

- [x] **Step 3: Fetch and persist the home department**

Add `"department_id"` to the `fetch_employees()` field list.

In `odoo_sync.py`, add a display-name helper beside `_m2o_id`:

```python
def _m2o_name(val) -> str | None:
    """Return a many2one display name, or None when Odoo returns False."""
    if isinstance(val, (list, tuple)) and len(val) > 1:
        return str(val[1]) or None
    return None
```

Before the employee loop, import `attendance_location_policy`. For each employee compute:

```python
department_name = attendance_location_policy.effective_department_name(
    None,
    _m2o_name(emp.get("department_id")),
)
```

Add `department_name` to the employee insert columns and values, and add:

```sql
department_name = EXCLUDED.department_name,
```

to the `ON CONFLICT (odoo_id) DO UPDATE` clause.

- [x] **Step 4: Run Task 2 tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_odoo_client.py tests/test_odoo_sync.py
```

Expected: all non-PostgreSQL tests PASS; PostgreSQL sync coverage passes in CI or reports skipped locally when `DATABASE_URL` is unset.

- [x] **Step 5: Commit and push Task 2**

```bash
git add \
  src/zira_dashboard/odoo_client.py \
  src/zira_dashboard/odoo_sync.py \
  tests/test_odoo_client.py \
  tests/test_odoo_sync.py
git commit -m "feat: mirror employee departments"
git push origin main
```

---

### Task 3: Apply the department rule to the legacy Missing Work Center inbox

**Files:**
- Modify: `src/zira_dashboard/_odoo_attendance.py:619-656`
- Modify: `src/zira_dashboard/odoo_client.py:491-495`
- Modify: `src/zira_dashboard/missing_wc.py:183-249`
- Modify: `tests/test_fetch_missing_wc.py:1-52`
- Modify: `tests/test_missing_wc.py:10-84`

**Interfaces:**
- Consumes: `effective_department_name` and the saved `department_requires_work_center` policy.
- Consumes: cached attendance `department_name` and local `people.department_name`.
- Produces: missing-WC cache rows containing nullable `department_name`.
- Produces: legacy inbox rows only for departments whose policy requires a work center.

- [x] **Step 1: Write the failing attendance-fetch test**

Update the configured-field test to monkeypatch both configured fields:

```python
monkeypatch.setattr(odoo_client, "_kiosk_wc_field", lambda: "x_kiosk_wc")
monkeypatch.setattr(
    odoo_client,
    "_kiosk_department_field",
    lambda: "x_kiosk_department",
)
```

Return `"x_kiosk_department": [8, "00 Maintenance"]`, capture `kwargs["fields"]`, and expect the shaped row to include:

```python
"department_name": "00 Maintenance",
```

Also assert `"x_kiosk_department" in captured["fields"]`.

- [x] **Step 2: Write failing legacy-shaper regression tests**

Add:

```python
def test_shape_suppresses_exempt_attendance_and_employee_departments():
    people = {
        7: {
            "name": "Trent",
            "wage_type": "hourly",
            "active": True,
            "excluded": False,
            "department_name": "Supervisor",
        },
        8: {
            "name": "Gerald",
            "wage_type": "hourly",
            "active": True,
            "excluded": False,
            "department_name": "Transportation",
        },
        9: {
            "name": "Producer",
            "wage_type": "hourly",
            "active": True,
            "excluded": False,
            "department_name": "Recycled",
        },
    }
    cached = [
        {"att_id": 1, "employee_odoo_id": 7, "department_name": "Maintenance"},
        {"att_id": 2, "employee_odoo_id": 7, "department_name": "Supervisor"},
        {"att_id": 3, "employee_odoo_id": 8, "department_name": None},
        {"att_id": 4, "employee_odoo_id": 9, "department_name": None},
    ]

    rows = missing_wc.shape_rows(
        cached,
        people,
        resolved=set(),
        requires_work_center=(
            attendance_location_policy.default_department_requires_work_center
        ),
    )

    assert [row["attendance_id"] for row in rows] == [4]
```

Import `attendance_location_policy` in `tests/test_missing_wc.py`. This one test covers attendance-first precedence, Supervisor and Maintenance exemptions, Gerald's employee fallback, and a required production control.

- [x] **Step 3: Run both focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_fetch_missing_wc.py::test_returns_shaped_rows_when_wc_field_configured \
  tests/test_missing_wc.py::test_shape_suppresses_exempt_attendance_and_employee_departments
```

Expected: FAIL because the fetch does not carry a department and `shape_rows` does not accept or apply `requires_work_center`.

- [x] **Step 4: Include attendance department in the cache**

Change `_odoo_attendance.fetch_attendances_missing_wc` to accept `department_field: str | None`. Build the search fields as:

```python
fields = ["id", "employee_id", "check_in", "check_out"]
if department_field:
    fields.append(department_field)
```

Shape each row with:

```python
department = row.get(department_field) if department_field else None
"department_name": (
    department[1]
    if isinstance(department, (list, tuple)) and len(department) > 1
    else None
),
```

Update the public wrapper to pass `_kiosk_department_field()`.

- [x] **Step 5: Filter the legacy rows through the shared policy**

Import `Callable`. Add this keyword argument to `shape_rows` so existing pure tests retain their current conservative behavior:

```python
requires_work_center: Callable[[str | None], bool] = lambda _department: True,
```

After the active-hourly checks, resolve and filter:

```python
effective_department = attendance_location_policy.effective_department_name(
    r.get("department_name"),
    p.get("department_name"),
)
if not requires_work_center(effective_department):
    continue
```

In `current_rows`, select `department_name` from `people` and pass:

```python
requires_work_center=attendance_location_policy.department_requires_work_center,
```

- [x] **Step 6: Run Task 3 tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_fetch_missing_wc.py \
  tests/test_missing_wc.py \
  tests/test_missing_wc_routes.py \
  tests/test_exception_inbox.py \
  tests/test_exception_inbox_attendance.py
```

Expected: all selected tests PASS; PostgreSQL-gated tests may report skipped.

- [x] **Step 7: Commit and push Task 3**

```bash
git add \
  src/zira_dashboard/_odoo_attendance.py \
  src/zira_dashboard/odoo_client.py \
  src/zira_dashboard/missing_wc.py \
  tests/test_fetch_missing_wc.py \
  tests/test_missing_wc.py
git commit -m "fix: filter missing work centers by department"
git push origin main
```

---

### Task 4: Apply employee fallback to mirrored attendance timelines

**Files:**
- Modify: `src/zira_dashboard/attendance_timeline.py:1-20,568-611`
- Modify: `tests/test_attendance_timeline.py:922-952`

**Interfaces:**
- Consumes: `people.department_name` and `effective_department_name`.
- Produces: `_rows_with_employee_department_fallback(rows: Sequence[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]` for local-only timeline input enrichment.
- Preserves: any nonblank `odoo_department_name` already stored on the attendance mirror row.

- [x] **Step 1: Write the failing Gerald timeline regression test**

Add beside the numbered-department timeline test:

```python
def test_timeline_uses_employee_department_when_attendance_department_is_blank(
    monkeypatch,
):
    source = row(
        check_out=at(minutes=10),
        work_center_id=None,
        work_center_name=None,
        department_id=None,
        department_name=None,
    )
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "health_snapshot",
        lambda: attendance_mirror.MirrorHealth(at(minutes=10), at(), at(), None, None),
    )
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "rows_overlapping",
        lambda _start, _end: (source,),
    )
    monkeypatch.setattr(
        attendance_timeline.db,
        "query",
        lambda _sql, _params: [{
            "odoo_id": 41,
            "department_name": "Transportation",
        }],
    )

    spans = attendance_timeline.timeline_for_range(
        at(), at(minutes=10), as_of_utc=at(minutes=10)
    )

    assert spans == (expected_span(at(), at(minutes=10), "exempt_no_location"),)
```

Extend the existing numbered Maintenance test with a `db.query` monkeypatch that raises `pytest.fail` if called. This proves a present attendance department wins without consulting the employee fallback.

- [x] **Step 2: Run the focused timeline tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_attendance_timeline.py::test_timeline_uses_employee_department_when_attendance_department_is_blank \
  tests/test_attendance_timeline.py::test_timeline_for_range_normalizes_numbered_odoo_department_for_saved_policy
```

Expected: the Gerald test FAILS with `missing_required_location` because timeline rows do not yet use `people.department_name`.

- [x] **Step 3: Enrich only blank timeline departments locally**

Add `db` to the module imports. Add:

```python
def _rows_with_employee_department_fallback(
    rows: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    missing_ids = sorted({
        int(row["employee_odoo_id"])
        for row in rows
        if not str(row.get("odoo_department_name") or "").strip()
    })
    if not missing_ids:
        return tuple(rows)
    home_rows = db.query(
        "SELECT odoo_id, department_name FROM people WHERE odoo_id = ANY(%s)",
        (missing_ids,),
    )
    home_by_id = {
        int(row["odoo_id"]): row.get("department_name")
        for row in home_rows
    }
    enriched = []
    for row in rows:
        if str(row.get("odoo_department_name") or "").strip():
            enriched.append(row)
            continue
        employee_department = home_by_id.get(int(row["employee_odoo_id"]))
        effective = attendance_location_policy.effective_department_name(
            None,
            employee_department,
        )
        enriched.append({**row, "odoo_department_name": effective})
    return tuple(enriched)
```

In `timeline_for_range`, call this helper after the mirror read and before `project_rows`.

- [x] **Step 4: Run Task 4 tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_attendance_timeline.py \
  tests/test_exception_inbox_attendance.py \
  tests/test_production_history_odoo_strict.py
```

Expected: all selected tests PASS.

- [x] **Step 5: Commit and push Task 4**

```bash
git add \
  src/zira_dashboard/attendance_timeline.py \
  tests/test_attendance_timeline.py
git commit -m "fix: use employee department for blank attendance"
git push origin main
```

---

### Task 5: Patch notes and complete verification

**Files:**
- Modify: `CHANGELOG.md:13-20`

**Interfaces:**
- Consumes: all behavior from Tasks 1-4.
- Produces: plain-language What's New coverage and final validation evidence.

- [x] **Step 1: Add the user-facing patch note**

Add this newest entry under `## 2026-08-31`:

```markdown
### Stop false work center alerts

- **Maintenance, Transportation, and Supervisor workers no longer get a work center warning when they do not need one.** If a time record loses its department, Plant Manager now checks the worker's saved Odoo department before showing a warning.
```

- [x] **Step 2: Run focused regression coverage**

Run:

```bash
.venv/bin/python -m pytest -q \
  tests/test_attendance_location_policy.py \
  tests/test_attendance_location_schema.py \
  tests/test_attendance_location_department_sync.py \
  tests/test_odoo_client.py \
  tests/test_odoo_sync.py \
  tests/test_fetch_missing_wc.py \
  tests/test_missing_wc.py \
  tests/test_missing_wc_routes.py \
  tests/test_attendance_timeline.py \
  tests/test_exception_inbox.py \
  tests/test_exception_inbox_attendance.py \
  tests/test_production_history_odoo_strict.py
```

Expected: all selected tests PASS; PostgreSQL-gated tests may report skipped when no test database is configured.

- [x] **Step 3: Run the complete suite and lint**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts
git diff --check
```

Expected: the complete suite PASSes, Ruff reports no errors, and `git diff --check` is silent.

- [x] **Step 4: Review the final diff for scope and secret safety**

Run:

```bash
git status --short
git diff --stat
git diff -- \
  CHANGELOG.md \
  src/zira_dashboard/_schema.py \
  src/zira_dashboard/attendance_location_policy.py \
  src/zira_dashboard/odoo_client.py \
  src/zira_dashboard/odoo_sync.py \
  src/zira_dashboard/_odoo_attendance.py \
  src/zira_dashboard/missing_wc.py \
  src/zira_dashboard/attendance_timeline.py \
  tests/test_attendance_location_policy.py \
  tests/test_attendance_location_schema.py \
  tests/test_attendance_location_department_sync.py \
  tests/test_odoo_client.py \
  tests/test_odoo_sync.py \
  tests/test_fetch_missing_wc.py \
  tests/test_missing_wc.py \
  tests/test_attendance_timeline.py
```

Expected: only the scoped fix, tests, plan tracking, and patch note appear; `.env` and credential values are absent.

- [x] **Step 5: Commit and push the patch note and final plan state**

```bash
git add \
  CHANGELOG.md \
  docs/superpowers/plans/2026-08-31-missing-work-center-department-exemptions.md
git commit -m "docs: explain work center alert exemptions"
git push origin main
```

- [x] **Step 6: Verify the pushed branch**

Run:

```bash
git fetch origin main
git status -sb
git log -1 --oneline origin/main
```

Expected: local `main` has no unpushed implementation commits; unrelated pre-existing working-tree files may remain visible.
