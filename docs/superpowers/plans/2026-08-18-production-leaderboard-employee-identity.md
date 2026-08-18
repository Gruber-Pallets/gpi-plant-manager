# Production Leaderboard Employee Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Calculate one production average row for each employee ID despite historical display-name changes.

**Architecture:** Carry `production_daily.emp_id` through normalized records, aggregate by it in shared production metrics, and retain names only as labels. Records without an employee ID use a name-scoped fallback identity. Staffing average percent calculations consume the same shared identity.

**Tech Stack:** Python 3.11, FastAPI, PostgreSQL, pytest, Ruff.

## Global Constraints

- `emp_id` is the only merge key when present; never merge merely matching names.
- Records with no `emp_id` retain their existing name-scoped identity.
- Keep templates on their existing `name`, average, and qualifying-day row contract.
- Do not rewrite historical data or alter awards, GOATs, ribbons, or player-card totals.
- Use TDD: watch each regression test fail, then pass.
- Add plain-language patch notes to `CHANGELOG.md` before pushing the implementation to `main`.

---

## File Structure

- `src/zira_dashboard/precompute.py`: normalized-record reader.
- `src/zira_dashboard/production_metrics.py`: shared identity and aggregation.
- `src/zira_dashboard/routes/leaderboards.py`: Staffing average percent aggregation.
- `tests/test_precompute_breakdown.py`: reader contract.
- `tests/test_production_metrics.py`: shared aggregation behavior.
- `tests/test_leaderboards_avg.py`: Staffing average behavior.
- `CHANGELOG.md`: user-facing patch note.

### Task 1: Expose employee ID on normalized records

**Files:**

- Modify: `src/zira_dashboard/precompute.py:219-254`
- Modify: `tests/test_precompute_breakdown.py:37-49`

**Interfaces:**

- Produces records shaped as `{day, emp_id, person, wc, units, downtime, hours, excluded_minutes}`.

- [ ] **Step 1: Write the failing reader-contract test**

```python
def test_normalized_daily_records_in_range_includes_employee_identity(monkeypatch):
    from zira_dashboard import db, precompute

    monkeypatch.setattr(db, "query", lambda sql, params: [{
        "day": date(2026, 7, 8), "emp_id": "501", "person": "Jesus G.",
        "wc": "Dismantler 2", "units": 0.0, "downtime": 10.0,
        "hours": 8.0, "excluded_minutes": 30.0,
    }])
    rows = precompute.normalized_daily_records_in_range(
        date(2026, 7, 8), date(2026, 7, 8)
    )
    assert rows[0]["emp_id"] == "501"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_precompute_breakdown.py::test_normalized_daily_records_in_range_includes_employee_identity -v`

Expected: FAIL with `KeyError: 'emp_id'` because the reader currently drops the database identity.

- [ ] **Step 3: Add the identity to the reader query and record**

```python
SELECT day, emp_id, name AS person, wc_name AS wc,
       units, downtime, hours, excluded_minutes
FROM production_daily
```

Add `"emp_id": str(r.get("emp_id") or "")` to the returned dictionary without changing the existing fields or absence filter.

- [ ] **Step 4: Verify the reader change**

Run: `pytest tests/test_precompute_breakdown.py tests/test_precompute.py -v`

Expected: PASS, including zero-unit and excluded-minute tests.

- [ ] **Step 5: Commit the reader contract**

```bash
git add src/zira_dashboard/precompute.py tests/test_precompute_breakdown.py
git commit -m "feat: expose production employee identity"
```

### Task 2: Aggregate shared leaderboard metrics by employee ID

**Files:**

- Modify: `src/zira_dashboard/production_metrics.py:8-112`
- Modify: `tests/test_production_metrics.py:8-88`

**Interfaces:**

- Produces `person_identity(record) -> tuple[str, str]`.
- Produces normalized score/average rows with the existing `name` plus `identity`.

- [ ] **Step 1: Write failing rename and namesake tests**

```python
def test_normalized_average_merges_renamed_employee_by_employee_id():
    rows = pm.normalized_average_by_person(
        [
            {**rec(date(2026, 1, 2), "Jesus Galindo", "Repair 1", 70, 7), "emp_id": "501"},
            {**rec(date(2026, 1, 3), "Jesus G.", "Repair 1", 140, 7), "emp_id": "501"},
        ], wc_names={"Repair 1"}, standard_full_day_hours=7,
    )
    assert len(rows) == 1
    assert rows[0]["identity"] == ("emp_id", "501")
    assert rows[0]["days"] == 2
    assert rows[0]["avg_units"] == 105.0


def test_normalized_average_keeps_same_name_different_employee_ids_separate():
    rows = pm.normalized_average_by_person(
        [
            {**rec(date(2026, 1, 2), "Jose Garcia", "Repair 1", 70, 7), "emp_id": "501"},
            {**rec(date(2026, 1, 3), "Jose Garcia", "Repair 1", 140, 7), "emp_id": "502"},
        ], wc_names={"Repair 1"}, standard_full_day_hours=7,
    )
    assert {row["identity"] for row in rows} == {("emp_id", "501"), ("emp_id", "502")}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_production_metrics.py -k employee_id -v`

Expected: FAIL because the current aggregation keys by the display name and exposes no identity.

- [ ] **Step 3: Add and apply the shared identity function**

```python
def person_identity(record: dict) -> tuple[str, str]:
    """Stable identity for leaderboard math; never infer identity from a label."""
    emp_id = str(record.get("emp_id") or "").strip()
    if emp_id:
        return ("emp_id", emp_id)
    return ("name", str(record["person"]))
```

In `normalized_daily_scores()`, replace its `(person, day)` bucket with `(person_identity(record), day)`, preserve the latest label for the bucket, and include `identity` on every score. In `normalized_average_by_person()`, replace name-keyed totals with identity-keyed totals and return that `identity` plus the label.

- [ ] **Step 4: Verify all shared metric tests**

Run: `pytest tests/test_production_metrics.py -v`

Expected: PASS. One ID with changed names becomes one row, same labels with different IDs remain separate, and existing name-only fixtures retain prior behavior.

- [ ] **Step 5: Commit the shared metrics change**

```bash
git add src/zira_dashboard/production_metrics.py tests/test_production_metrics.py
git commit -m "fix: group production averages by employee identity"
```

### Task 3: Use the shared identity in Staffing average rows

**Files:**

- Modify: `src/zira_dashboard/routes/leaderboards.py:25-210`
- Modify: `tests/test_leaderboards_avg.py:12-155`
- Modify: `CHANGELOG.md`

**Interfaces:**

- Consumes Task 2's `person_identity()` and normalized `identity` output.
- Produces the current Staffing average row shape, once per stable employee identity.

- [ ] **Step 1: Write the failing Staffing regression test**

```python
def test_averages_for_wc_merges_renamed_employee_by_employee_id():
    records = [
        {**_rec(date(2026, 4, 27), "Jesus Galindo", "WC1", 140), "emp_id": "501"},
        {**_rec(date(2026, 4, 28), "Jesus G.", "WC1", 280), "emp_id": "501"},
    ]
    rows = averages_for_wc(records, 30.0, _const_productive, "units")
    assert len(rows) == 1
    assert rows[0]["name"] == "Jesus G."
    assert rows[0]["name_count"] == 2
    assert rows[0]["avg_units"] == 210.0
```

- [ ] **Step 2: Run the regression test to verify it fails**

Run: `pytest tests/test_leaderboards_avg.py::test_averages_for_wc_merges_renamed_employee_by_employee_id -v`

Expected: FAIL because qualified-day, normalized-row, and percent dictionaries still use the display name.

- [ ] **Step 3: Replace Staffing name keys with the shared identity**

```python
qualified_days = {(row["identity"], row["day"]) for row in scores}
normalized_by_identity = {row["identity"]: row for row in normalized_rows}
by_person: dict[tuple[str, str], list[dict]] = {}

for record in scoped_rows:
    identity = production_metrics.person_identity(record)
    if identity in normalized_by_identity and (identity, record["day"]) in qualified_days:
        by_person.setdefault(identity, []).append(record)

for identity, recs in by_person.items():
    norm = normalized_by_identity[identity]
    # Preserve the existing percent and top-work-center calculations.
```

Apply the same pattern to `averages_for_wc()` and `averages_for_group()`. Use `norm["name"]` in the final template row. Add this top `CHANGELOG.md` entry:

```markdown
- **Each person now appears just once on production scoreboards.** If someone’s name was written a little differently in older records, their work is now put together on one line.
```

- [ ] **Step 4: Run focused and full verification**

Run: `pytest tests/test_leaderboards_avg.py tests/test_production_metrics.py tests/test_recycling_leaderboard_tv.py tests/test_new_leaderboard_static.py -v && ruff check src/zira_dashboard/production_metrics.py src/zira_dashboard/precompute.py src/zira_dashboard/routes/leaderboards.py tests/test_precompute_breakdown.py tests/test_production_metrics.py tests/test_leaderboards_avg.py && pytest -q`

Expected: all selected tests, Ruff, and the full suite pass; Postgres tests may skip only when `DATABASE_URL` is unavailable.

- [ ] **Step 5: Commit and push the completed fix**

```bash
git add CHANGELOG.md src/zira_dashboard/precompute.py src/zira_dashboard/production_metrics.py src/zira_dashboard/routes/leaderboards.py tests/test_precompute_breakdown.py tests/test_production_metrics.py tests/test_leaderboards_avg.py
git commit -m "fix: unify production leaderboard identities"
git push origin main
```
