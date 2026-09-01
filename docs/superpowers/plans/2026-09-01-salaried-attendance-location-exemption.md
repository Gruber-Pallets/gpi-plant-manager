# Salaried Attendance-Location Exemption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Exempt Odoo Fixed Wage (`monthly`) employees from no-work-center attendance alerts while preserving any real Odoo work-center span for production and staffing.

**Architecture:** Keep wage type as local Odoo employee truth in `people`. Enrich mirrored attendance rows with that value before projection, then make the shared timeline exempt only a `monthly` employee in its missing-work-center branch. Existing department-only callback users therefore inherit the rule without bespoke caller logic. Bind wage type into the shadow comparison’s local configuration digest so a salary-status change invalidates prior clean evidence.

**Tech Stack:** Python 3.12, PostgreSQL-local reads, pytest, Ruff.

## Global Constraints

- Odoo `wage_type == "monthly"` is the only salaried exemption; hourly, absent, and unknown values remain location-required.
- A mapped Odoo work center always remains a valid location span, regardless of wage type.
- Dashboard, readiness, Staffing, exception, and shadow projection paths remain local-only; no request-time Odoo I/O.
- Do not schedule or activate the live attendance rollout.
- Add a child-readable 2026-09-01 `CHANGELOG.md` note and preserve historical notes.

---

### Task 1: Add the employee-aware timeline rule

**Files:**
- Modify: `src/zira_dashboard/attendance_timeline.py:35-650`
- Modify: `tests/test_attendance_timeline.py:15-100`

**Interfaces:**
- Consumes: normalized attendance rows with optional `employee_wage_type` text supplied from local `people` records.
- Produces: `project_rows(...)` applies the salary exemption from each optional `employee_wage_type` source field.
- Preserves: `requires_work_center(department_name)` remains the department policy for hourly, absent, and unknown wage types.

- [ ] **Step 1: Write the failing test**

```python
def test_monthly_employee_without_location_is_exempt_even_in_required_department():
    source = row(work_center_id=None, work_center_name=None)
    source["employee_wage_type"] = "monthly"

    spans = project([source])

    assert [span.status for span in spans] == ["exempt_no_location"]


@pytest.mark.parametrize("wage_type", ["hourly", None, "unexpected"])
def test_non_monthly_employee_without_location_remains_required(wage_type):
    source = row(work_center_id=None, work_center_name=None)
    source["employee_wage_type"] = wage_type

    spans = project([source])

    assert [span.status for span in spans] == ["missing_required_location"]


def test_monthly_employee_mapped_location_remains_valid():
    source = row()
    source["employee_wage_type"] = "monthly"

    spans = project([source])

    assert [span.status for span in spans] == ["valid"]
```

- [ ] **Step 2: Run test to verify RED**

Run: `.venv/bin/python -m pytest tests/test_attendance_timeline.py -q`

Expected: FAIL because no wage value reaches the projection and a `monthly` row still produces the existing missing-location status.

- [ ] **Step 3: Write minimal implementation**

```python
@dataclass(frozen=True)
class _SourceRow:
    # existing fields ...
    wage_type: str | None


def _requires_work_center(
    source: _SourceRow,
    department_requirement: Callable[[str | None], bool],
) -> bool:
    if source.wage_type == "monthly":
        return False
    return _validated_requirement(department_requirement(source.department_name))
```

Use `_requires_work_center(...)` only in the no-work-center branch. Extend `_source_row()` to accept optional `employee_wage_type` text without adding it to `_ROW_FIELDS`, so existing callers remain compatible. Keep `project_rows()` and its department-policy callback API unchanged, making every existing consumer share the rule once its local rows are enriched.

- [ ] **Step 4: Run test to verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_attendance_timeline.py -q`

Expected: PASS; monthly missing-location spans are `exempt_no_location`, and real mapped spans stay `valid`.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/attendance_timeline.py tests/test_attendance_timeline.py
git commit -m "feat: exempt salaried attendance gaps"
```

### Task 2: Supply the local Odoo wage type consistently

**Files:**
- Modify: `src/zira_dashboard/attendance_timeline.py:569-650`
- Modify: `src/zira_dashboard/attendance_readiness.py:188-575,645-750`
- Modify: `tests/test_attendance_timeline.py`
- Modify: `tests/test_attendance_readiness.py`

**Interfaces:**
- Consumes: `people.odoo_id`, `people.department_name`, and `people.wage_type`, all already synchronized locally from Odoo.
- Produces: rows enriched with `employee_wage_type`; a shadow configuration snapshot/digest containing the employee wage type used by its projection.
- Preserves: absent people rows and absent/unknown wage types are location-required.

- [ ] **Step 1: Write the failing tests**

```python
def test_timeline_for_range_uses_local_monthly_wage_type_for_missing_location(monkeypatch):
    monkeypatch.setattr(attendance_mirror, "rows_overlapping", lambda *_args: (row_without_wc(),))
    monkeypatch.setattr(
        attendance_timeline.db,
        "query",
        lambda _sql, _params: [{"odoo_id": 41, "department_name": "Assembly", "wage_type": "monthly"}],
    )

    assert attendance_timeline.timeline_for_range(BASE, at(3), as_of_utc=at(3))[0].status == "exempt_no_location"


def test_shadow_wage_type_change_invalidates_saved_clean_origin(db_rows):
    before = attendance_readiness._shadow_config_snapshot_cur(db_rows, date(2026, 9, 1))
    db_rows.people[41]["wage_type"] = "monthly"
    after = attendance_readiness._shadow_config_snapshot_cur(db_rows, date(2026, 9, 1))

    assert before.digest != after.digest
```

- [ ] **Step 2: Run tests to verify RED**

Run: `.venv/bin/python -m pytest tests/test_attendance_timeline.py tests/test_attendance_readiness.py -q`

Expected: FAIL because employee fallback reads do not select `wage_type`, and the shadow digest omits wage type.

- [ ] **Step 3: Write minimal implementation**

```python
home_rows = db.query(
    "SELECT odoo_id, department_name, wage_type FROM people WHERE odoo_id = ANY(%s)",
    (employee_ids,),
)

```

Rename the fallback helper if necessary so it enriches every row with both the existing effective department and `employee_wage_type`. Use it from `timeline_for_range()` and existing local consumers. In readiness, select the same wage type, enrich its detached rows, and include `(odoo_id, normalized_department, normalized_wage_type)` in the canonical global shadow digest and snapshot mapping.

- [ ] **Step 4: Run tests to verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_attendance_timeline.py tests/test_attendance_readiness.py -q`

Expected: PASS; timeline/readiness agree, and a monthly↔hourly employee update invalidates shadow evidence.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/attendance_timeline.py src/zira_dashboard/attendance_readiness.py tests/test_attendance_timeline.py tests/test_attendance_readiness.py
git commit -m "fix: bind salaried status into attendance readiness"
```

### Task 3: Verify consumers, document, and ship

**Files:**
- Modify: `CHANGELOG.md:1-20`
- Test: `tests/test_attendance_location_end_to_end.py`
- Test: `tests/test_attendance_exceptions.py`

**Interfaces:**
- Consumes: shared projected `LocationSpan` values from Tasks 1–2.
- Produces: no salary-specific Inbox exception/readiness blocker for missing locations, without changing valid salaried plant work.

- [ ] **Step 1: Write the failing consumer regression test**

```python
def test_monthly_gap_is_not_an_attendance_exception_but_hourly_gap_is():
    spans = project_real_local_rows(
        monthly_without_work_center=True,
        hourly_without_work_center=True,
    )

    assert "missing_required_location" not in statuses_for_employee(spans, MONTHLY_ID)
    assert "missing_required_location" in statuses_for_employee(spans, HOURLY_ID)
```

- [ ] **Step 2: Run focused consumers to verify RED or inherited GREEN**

Run: `.venv/bin/python -m pytest tests/test_attendance_location_end_to_end.py tests/test_attendance_exceptions.py -q`

Expected: RED before Tasks 1–2, then GREEN after their shared projection behavior is complete. If the focused suite is already green because it exercises the real shared projection, record that it proves the consumer behavior rather than adding duplicate production logic.

- [ ] **Step 3: Add a child-readable What’s New note**

```markdown
### Salaried managers do not need a work center

- Salaried people from Odoo can clock in without a work center when they are not doing plant work. When Luke records a work center, that work still counts normally.
```

- [ ] **Step 4: Run verification**

Run:

```bash
.venv/bin/python -m pytest tests/test_attendance_timeline.py tests/test_attendance_readiness.py tests/test_attendance_location_end_to_end.py tests/test_attendance_exceptions.py -q
.venv/bin/python -m ruff check src tests
git diff --check
```

Expected: all selected tests pass, Ruff reports no errors, and `git diff --check` is silent.

- [ ] **Step 5: Commit and push**

```bash
git add CHANGELOG.md src/zira_dashboard/attendance_timeline.py src/zira_dashboard/attendance_readiness.py tests/test_attendance_timeline.py tests/test_attendance_readiness.py tests/test_attendance_location_end_to_end.py tests/test_attendance_exceptions.py
git commit -m "feat: exempt salaried attendance gaps"
git push origin HEAD:main
```

- [ ] **Step 6: Verify deployment without activating Live mode**

Run the local readiness CLI and production health check after the deployment. Confirm the salaried missing-location count no longer blocks readiness, while the rollout mode remains unchanged.

## Self-review

- Spec coverage: Task 1 implements the employee-level missing-location rule and preserves mapped spans. Task 2 uses the local Odoo wage type in both normal and shadow/readiness projections and invalidates stale evidence. Task 3 proves shared consumers, documents the user-facing change, and verifies deployment without activating rollout.
- Placeholder scan: no implementation placeholders remain; every code task gives the intended API, test behavior, command, and expected outcome.
- Type consistency: optional `employee_wage_type` is carried by normalized source rows; `project_rows` retains its existing department-policy callback signature, so all existing callers remain valid.
