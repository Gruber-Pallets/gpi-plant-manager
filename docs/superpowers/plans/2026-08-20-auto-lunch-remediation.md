# Auto-Lunch Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore automatic lunch deductions and safely repair every eligible attendance interval missed while production Auto-Lunch was Off.

**Architecture:** Add a pure planner that turns historical Odoo intervals plus resolved lunch windows into idempotent repair actions. A command reads production data, prints those actions by default, and on `--apply` updates Odoo first, then writes matching local audit punches and terminal run records in one Postgres transaction.

**Tech Stack:** Python 3.12, pytest, psycopg2/Postgres, Odoo XML-RPC.

## Global Constraints

- Preserve the existing live Auto-Lunch worker and its fixed/flexible eligibility rules.
- Do not alter an interval that does not cover the scheduled lunch-out instant.
- `--apply` is the sole external-write mode; ordinary invocation is read-only.
- A saved `auto_lunch_runs` row is the idempotency guard.
- New user-facing patch notes use short plain language.

---

### Task 1: Model and plan historical lunch splits

**Files:**

- Create: `src/zira_dashboard/auto_lunch_backfill.py`
- Modify: `src/zira_dashboard/_odoo_attendance.py`
- Test: `tests/test_auto_lunch_backfill.py`
- Test: `tests/test_odoo_attendance_for_day.py`

**Interfaces:**

- Consumes: Odoo intervals shaped as `{id, employee_odoo_id, check_in, check_out, wc_name}`.
- Produces: `plan_repairs(intervals, windows_by_person, existing_run_people) -> list[Repair]`.

- [ ] **Step 1: Write the failing planner tests**

```python
def test_plan_repairs_splits_interval_covering_lunch():
    repair = plan_repairs([_interval(1, 7, "07:00", "15:30", "Repair 1")],
                          {7: _window("11:00", "11:30")}, set())
    assert repair == [Repair(1, 7, _dt("11:00"), _dt("11:30"), "Repair 1", True)]

def test_plan_repairs_skips_late_arrival_and_existing_run():
    assert plan_repairs([_interval(1, 7, "11:05", "15:30")], {7: _window("11:00", "11:30")}, set()) == []
    assert plan_repairs([_interval(1, 7, "07:00", "15:30")], {7: _window("11:00", "11:30")}, {7}) == []
```

- [ ] **Step 2: Run the new tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_auto_lunch_backfill.py -v`

Expected: collection fails because `auto_lunch_backfill` does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
@dataclass(frozen=True)
class Repair:
    attendance_id: int
    person_odoo_id: int
    out_at: datetime
    in_at: datetime
    wc_name: str | None
    create_return: bool

def plan_repairs(intervals, windows_by_person, existing_run_people):
    repairs = []
    for row in intervals:
        window = windows_by_person.get(int(row["employee_odoo_id"]))
        if not window or int(row["employee_odoo_id"]) in existing_run_people:
            continue
        start, end = _as_dt(row["check_in"]), _as_dt(row.get("check_out"))
        if start <= window.out_at < end:
            repairs.append(Repair(int(row["id"]), int(row["employee_odoo_id"]), window.out_at, window.in_at, row.get("wc_name"), end >= window.in_at))
    return repairs
```

Also retain an Odoo row ID in `fetch_attendance_intervals_for_day` with `"id": int(row["id"])`.

- [ ] **Step 4: Run planner and Odoo-read tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_auto_lunch_backfill.py tests/test_odoo_attendance_for_day.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the isolated model work**

Run: `git add src/zira_dashboard/auto_lunch_backfill.py src/zira_dashboard/_odoo_attendance.py tests/test_auto_lunch_backfill.py tests/test_odoo_attendance_for_day.py`

Run: `git commit -m "feat(attendance): plan safe auto-lunch backfills"`

### Task 2: Add the guarded production repair command

**Files:**

- Create: `scripts/backfill_auto_lunch.py`
- Modify: `CHANGELOG.md`
- Test: `tests/test_auto_lunch_backfill.py`

**Interfaces:**

- Consumes: `Repair` rows from `plan_repairs` and `--from-date`, `--through-date`, `--apply` CLI arguments.
- Produces: one Odoo split plus two synced local audit punches and one `done` run per applied repair.

- [ ] **Step 1: Write a failing execution-order test**

```python
def test_apply_repair_closes_then_returns_then_persists_audit():
    events = []
    apply_repair(_repair(), close=lambda *x: events.append(("close", x)), clock_in=lambda *x: events.append(("in", x)) or 9, persist=lambda *x: events.append(("persist", x)))
    assert [event[0] for event in events] == ["close", "in", "persist"]
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `.venv/bin/python -m pytest tests/test_auto_lunch_backfill.py::test_apply_repair_closes_then_returns_then_persists_audit -v`

Expected: FAIL because `apply_repair` is not defined.

- [ ] **Step 3: Write minimal execution and command guard**

```python
def apply_repair(repair, *, close, clock_in, persist):
    close(repair.attendance_id, repair.out_at)
    in_attendance_id = clock_in(repair.person_odoo_id, repair.wc_name, repair.in_at) if repair.create_return else None
    persist(repair, in_attendance_id)
```

The command adds `--apply`; without it it prints `Dry run — pass --apply only after reviewing these repairs.` and returns without writes. `persist` inserts source=`auto_lunch` clock-out and optional clock-in rows with `synced_to_odoo=TRUE`, then UPSERTs `auto_lunch_runs` as `done` using those IDs.

- [ ] **Step 4: Run focused tests and static checks**

Run: `.venv/bin/python -m pytest tests/test_auto_lunch_backfill.py tests/test_auto_lunch_worker.py tests/test_odoo_attendance_for_day.py -v`

Run: `.venv/bin/ruff check src/zira_dashboard/auto_lunch_backfill.py scripts/backfill_auto_lunch.py`

Expected: PASS.

- [ ] **Step 5: Add an understandable changelog note and commit**

Run: `git add scripts/backfill_auto_lunch.py src/zira_dashboard/auto_lunch_backfill.py tests/test_auto_lunch_backfill.py CHANGELOG.md`

Run: `git commit -m "fix(attendance): restore automatic lunch deductions"`

### Task 3: Production remediation and verification

**Files:**

- Use: `scripts/backfill_auto_lunch.py`
- Use: `scripts/diagnose_saturday_punches.py`

- [ ] **Step 1: Run a dry run for 2026-08-18 through 2026-08-20**

Run: `.venv/bin/python -m scripts.backfill_auto_lunch --from-date 2026-08-18 --through-date 2026-08-20`

Expected: a printed repair for each Odoo interval covering its resolved lunch-out time and no writes.

- [ ] **Step 2: Apply exactly the reviewed repairs**

Run: `.venv/bin/python -m scripts.backfill_auto_lunch --from-date 2026-08-18 --through-date 2026-08-20 --apply`

Expected: each action reports its Odoo close/create and matching local audit IDs.

- [ ] **Step 3: Restore production Live mode and independently verify**

Run: a scoped production settings update to `enabled=TRUE, observe_only=FALSE`, followed by the read-only diagnostic for the latest repaired day.

Expected: settings reports `LIVE (writes punches)`; each repaired person has a matching `done` run, auto-lunch audit punches, and split Odoo intervals.

- [ ] **Step 4: Push commits and verify the deployed revision**

Run: `git push origin main`

Run: `railway status`

Expected: `origin/main` contains both commits and Railway reports the web service online after deployment.
