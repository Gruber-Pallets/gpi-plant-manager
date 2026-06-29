# Weekly Odoo Calendar-Conflict Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A weekly in-app check that diffs the Odoo calendar-conflict set week-over-week and maintains one Odoo `project.task` describing the conflicts — silent when unchanged, archived when resolved.

**Architecture:** Detection logic moves out of the CLI script into a shared `calendar_conflicts` module. A new `calendar_conflict_monitor` module runs on the existing in-process warmer, gated to ≥7 days via a singleton Postgres state row, and syncs a single Odoo task through the existing feedback-task path. Best-effort: the warmer skeleton logs-and-swallows.

**Tech Stack:** Python 3.12, FastAPI app, psycopg2/Postgres, Odoo XML-RPC via `odoo_client`. Tests with pytest. Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest`.

---

## File Structure

- Create `src/zira_dashboard/calendar_conflicts.py` — detection (pure `classify_conflict`, `fmt_days`, `plant_weekdays`, `gather_rows`, `current_conflicts`), moved from the CLI script.
- Modify `scripts/diagnose_odoo_calendar_conflicts.py` — thin CLI importing detection from the module.
- Create `tests/test_calendar_conflicts.py` — detection tests (moved from `tests/test_odoo_calendar_conflict_diagnostic.py`).
- Delete `tests/test_odoo_calendar_conflict_diagnostic.py` — superseded by the above.
- Modify `src/zira_dashboard/odoo_client.py` — add `update_task`, `post_task_message`.
- Modify `src/zira_dashboard/_schema.py` — add `calendar_conflict_monitor` table to `SCHEMA_DDL`.
- Create `src/zira_dashboard/calendar_conflict_monitor.py` — `decide`, state load/save, `run_once`.
- Create `tests/test_calendar_conflict_monitor.py` — `decide` + `run_once` tests.
- Modify `src/zira_dashboard/app.py` — register `_tick_calendar_conflicts` warmer.

---

### Task 1: Extract the detection module

**Files:**
- Create: `src/zira_dashboard/calendar_conflicts.py`
- Modify: `scripts/diagnose_odoo_calendar_conflicts.py`
- Create: `tests/test_calendar_conflicts.py`
- Delete: `tests/test_odoo_calendar_conflict_diagnostic.py`

- [ ] **Step 1: Create the detection module**

Create `src/zira_dashboard/calendar_conflicts.py`:

```python
"""Detect Odoo work-schedule conflicts with the plant's workdays.

Read-only. Shared by the CLI diagnostic (scripts/diagnose_odoo_calendar_conflicts.py)
and the weekly monitor (calendar_conflict_monitor). Classifies each active
Odoo employee's resource.calendar against the plant's operating weekdays.
See docs/superpowers/specs/2026-06-29-calendar-conflict-monitor-design.md
"""

from __future__ import annotations

WD_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DEFAULT_WEEKDAYS = frozenset({0, 1, 2, 3, 4})  # Mon–Fri (0=Mon..6=Sun)


def classify_conflict(plant_weekdays, covered_weekdays, is_flexible=False, has_calendar=True) -> str:
    """Classify one employee's Odoo work schedule against the plant workdays.

    Returns "no_calendar" / "flexible" / "missing_days" / "ok". Odoo rejects a
    fixed-period leave for the first three; "ok" is fine.
    """
    if not has_calendar:
        return "no_calendar"
    if is_flexible or not covered_weekdays:
        return "flexible"
    if set(plant_weekdays) - set(covered_weekdays):
        return "missing_days"
    return "ok"


def fmt_days(days) -> str:
    return ", ".join(WD_ABBR[d] for d in sorted(days)) if days else "—"


def plant_weekdays():
    """(weekdays, note). Real plant work-week from Postgres; falls back to
    Mon–Fri (with a note) when the DB isn't reachable."""
    try:
        from . import schedule_store

        wd = schedule_store.current().work_weekdays
        if wd:
            return frozenset(wd), None
        return DEFAULT_WEEKDAYS, None
    except Exception as e:  # noqa: BLE001 -- DB optional; default the work-week
        return DEFAULT_WEEKDAYS, (
            f"Plant work-week unavailable ({type(e).__name__}); assuming Mon–Fri."
        )


def _load_roster_by_id():
    """({odoo_id: Person}, note). Optional Postgres enrichment to restrict to
    rostered people and drop reserves. (None, note) when the DB isn't
    reachable, so callers fall back to all active Odoo employees."""
    try:
        from . import staffing

        by_id = {
            int(p.employee_id): p
            for p in staffing.load_roster()
            if p.employee_id is not None
        }
        return by_id, None
    except Exception as e:  # noqa: BLE001 -- roster is optional enrichment
        return None, (
            f"Local roster unavailable ({type(e).__name__}); listing ALL active "
            "Odoo employees (reserves not filtered out)."
        )


def gather_rows(plant_set):
    """Classify active Odoo employees against `plant_set`. Returns (rows, notes).

    Odoo is the population; the local roster is optional enrichment. Each row:
    {name, odoo_id, cal_name, covered, missing, verdict}.
    """
    from . import odoo_client

    employees = odoo_client.fetch_employees()  # active only
    roster_by_id, roster_note = _load_roster_by_id()

    cal_meta = {
        int(s["id"]): (s.get("name") or "(unnamed)", bool(s.get("is_flexible")))
        for s in odoo_client.fetch_work_schedules()
    }

    emp_cal: dict[int, int | None] = {}
    for e in employees:
        cal_id = odoo_client.unwrap_m2o(e.get("resource_calendar_id"))
        valid = isinstance(cal_id, int) and not isinstance(cal_id, bool)
        emp_cal[int(e["id"])] = cal_id if valid else None

    cal_ids = {c for c in emp_cal.values() if c is not None}
    covered = {
        cid: {int(wd) for wd in days}
        for cid, days in odoo_client.fetch_calendar_hours(cal_ids).items()
    }

    plant = set(plant_set)
    rows = []
    for e in employees:
        eid = int(e["id"])
        if roster_by_id is not None:
            p = roster_by_id.get(eid)
            if p is None or p.reserve:
                continue  # not rostered, or a reserve — never declared absent
        cal_id = emp_cal.get(eid)
        has_cal = cal_id is not None
        if has_cal:
            cal_name, is_flex = cal_meta.get(cal_id, ("(unknown)", False))
            cov = covered.get(cal_id, set())
        else:
            cal_name, is_flex, cov = "(no Odoo work schedule)", False, set()
        rows.append({
            "name": e.get("name") or f"(id {eid})",
            "odoo_id": eid,
            "cal_name": cal_name,
            "covered": cov,
            "missing": plant - cov,
            "verdict": classify_conflict(plant, cov, is_flexible=is_flex, has_calendar=has_cal),
        })
    return rows, [n for n in (roster_note,) if n]


def current_conflicts():
    """Conflict rows only (verdict != 'ok'), for the monitor."""
    weekdays, _note = plant_weekdays()
    rows, _notes = gather_rows(weekdays)
    return [r for r in rows if r["verdict"] != "ok"]
```

- [ ] **Step 2: Rewrite the CLI script to import from the module**

Replace `scripts/diagnose_odoo_calendar_conflicts.py` entirely with:

```python
#!/usr/bin/env python3
"""List employees whose Odoo work schedule conflicts with the plant's workdays.

Read-only. Thin CLI over zira_dashboard.calendar_conflicts. Run from a laptop
via `railway run` (injects Odoo creds; Postgres is optional enrichment):

    railway run python scripts/diagnose_odoo_calendar_conflicts.py [--all]

See docs/superpowers/specs/2026-06-29-calendar-conflict-monitor-design.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from zira_dashboard.calendar_conflicts import (  # noqa: E402
    classify_conflict,  # re-exported for backwards-compat imports
    fmt_days,
    gather_rows,
    plant_weekdays,
)

_BUCKETS = [
    ("no_calendar", "No Odoo work schedule"),
    ("flexible", "Flexible / no fixed hours"),
    ("missing_days", "Calendar missing plant workday(s)"),
]

__all__ = ["classify_conflict", "main"]


def _print_report(rows, weekdays, show_all: bool, notes=()) -> None:
    for note in notes:
        print(f"NOTE: {note}")
    if notes:
        print()
    conflicts = [r for r in rows if r["verdict"] != "ok"]
    print(
        f"{len(conflicts)} of {len(rows)} employees have an Odoo work-schedule "
        f"conflict (plant runs {fmt_days(weekdays)})."
    )
    print()
    for key, title in _BUCKETS:
        group = sorted((r for r in rows if r["verdict"] == key), key=lambda r: r["name"].lower())
        if not group:
            continue
        print(f"{title} ({len(group)}):")
        for r in group:
            line = f"  • {r['name']} (id {r['odoo_id']}) · calendar {r['cal_name']!r}"
            if key == "missing_days":
                line += f" · covers {fmt_days(r['covered'])} · missing {fmt_days(r['missing'])}"
            print(line)
        print()
    if show_all:
        ok = sorted((r for r in rows if r["verdict"] == "ok"), key=lambda r: r["name"].lower())
        print(f"OK ({len(ok)}):")
        for r in ok:
            print(
                f"  • {r['name']} (id {r['odoo_id']}) · calendar {r['cal_name']!r} "
                f"· covers {fmt_days(r['covered'])}"
            )


def _parse_args(argv):
    ap = argparse.ArgumentParser(
        description="List employees whose Odoo work schedule conflicts with plant workdays."
    )
    ap.add_argument("--all", action="store_true", help="Also list employees whose calendar is fine.")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    weekdays, week_note = plant_weekdays()
    rows, notes = gather_rows(weekdays)
    _print_report(rows, weekdays, show_all=args.all, notes=([week_note] if week_note else []) + notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Create the moved detection tests**

Create `tests/test_calendar_conflicts.py`:

```python
"""Detection tests for zira_dashboard.calendar_conflicts (pure classifier +
the Odoo/roster gather with its graceful Postgres fallback)."""

from types import SimpleNamespace

import zira_dashboard.calendar_conflicts as cc
import zira_dashboard.odoo_client as oc
import zira_dashboard.staffing as staffing_mod

MON_FRI = frozenset({0, 1, 2, 3, 4})
_MON_FRI_HOURS = {str(d): ["6", "14"] for d in range(5)}
_MON_THU_HOURS = {str(d): ["6", "14"] for d in range(4)}


def _patch_odoo(monkeypatch, employees, schedules, cal_hours):
    monkeypatch.setattr(oc, "fetch_employees", lambda: employees)
    monkeypatch.setattr(oc, "fetch_work_schedules", lambda: schedules)
    monkeypatch.setattr(oc, "fetch_calendar_hours", lambda ids: cal_hours)


def test_covers_every_plant_weekday_is_ok():
    assert cc.classify_conflict(MON_FRI, {0, 1, 2, 3, 4}, is_flexible=False, has_calendar=True) == "ok"


def test_extra_weekend_coverage_still_ok():
    assert cc.classify_conflict(MON_FRI, {0, 1, 2, 3, 4, 5}, is_flexible=False, has_calendar=True) == "ok"


def test_missing_friday_is_missing_days():
    assert cc.classify_conflict(MON_FRI, {0, 1, 2, 3}, is_flexible=False, has_calendar=True) == "missing_days"


def test_flexible_flag_is_flexible():
    assert cc.classify_conflict(MON_FRI, {0, 1, 2, 3, 4}, is_flexible=True, has_calendar=True) == "flexible"


def test_calendar_with_no_covered_weekdays_is_flexible():
    assert cc.classify_conflict(MON_FRI, set(), is_flexible=False, has_calendar=True) == "flexible"


def test_no_calendar_is_no_calendar():
    assert cc.classify_conflict(MON_FRI, set(), is_flexible=False, has_calendar=False) == "no_calendar"


def test_no_calendar_takes_precedence_over_flexible():
    assert cc.classify_conflict(MON_FRI, set(), is_flexible=True, has_calendar=False) == "no_calendar"


def test_gather_excludes_reserves_and_non_roster(monkeypatch):
    employees = [
        {"id": 1, "name": "Ana Scheduled", "resource_calendar_id": [10, "M-F"]},
        {"id": 2, "name": "Bob Reserve", "resource_calendar_id": [10, "M-F"]},
        {"id": 3, "name": "Cara NotRostered", "resource_calendar_id": [10, "M-F"]},
        {"id": 4, "name": "Dan Missing Fri", "resource_calendar_id": [11, "M-Th"]},
    ]
    schedules = [
        {"id": 10, "name": "M-F", "is_flexible": False},
        {"id": 11, "name": "M-Th", "is_flexible": False},
    ]
    _patch_odoo(monkeypatch, employees, schedules, {10: _MON_FRI_HOURS, 11: _MON_THU_HOURS})
    roster = [
        SimpleNamespace(employee_id=1, reserve=False),
        SimpleNamespace(employee_id=2, reserve=True),
        SimpleNamespace(employee_id=4, reserve=False),
    ]
    monkeypatch.setattr(staffing_mod, "load_roster", lambda: roster)

    rows, notes = cc.gather_rows(MON_FRI)

    by_id = {r["odoo_id"]: r for r in rows}
    assert set(by_id) == {1, 4}
    assert by_id[1]["verdict"] == "ok"
    assert by_id[4]["verdict"] == "missing_days"
    assert by_id[4]["missing"] == {4}
    assert notes == []


def test_gather_falls_back_to_all_active_when_roster_unavailable(monkeypatch):
    employees = [
        {"id": 1, "name": "Ana", "resource_calendar_id": [10, "M-F"]},
        {"id": 2, "name": "Bob NoCal", "resource_calendar_id": False},
    ]
    schedules = [{"id": 10, "name": "M-F", "is_flexible": False}]
    _patch_odoo(monkeypatch, employees, schedules, {10: _MON_FRI_HOURS})

    def _boom():
        raise RuntimeError("postgres unreachable")

    monkeypatch.setattr(staffing_mod, "load_roster", _boom)

    rows, notes = cc.gather_rows(MON_FRI)
    by_id = {r["odoo_id"]: r for r in rows}
    assert set(by_id) == {1, 2}
    assert by_id[2]["verdict"] == "no_calendar"
    assert notes and "roster unavailable" in notes[0].lower()


def test_current_conflicts_returns_only_conflicts(monkeypatch):
    employees = [
        {"id": 1, "name": "Ana OK", "resource_calendar_id": [10, "M-F"]},
        {"id": 2, "name": "Dan Missing Fri", "resource_calendar_id": [11, "M-Th"]},
    ]
    schedules = [
        {"id": 10, "name": "M-F", "is_flexible": False},
        {"id": 11, "name": "M-Th", "is_flexible": False},
    ]
    _patch_odoo(monkeypatch, employees, schedules, {10: _MON_FRI_HOURS, 11: _MON_THU_HOURS})
    monkeypatch.setattr(cc, "plant_weekdays", lambda: (MON_FRI, None))

    # Make the roster lookup unavailable so all active Odoo employees are kept
    # (no reserve filter) — exercises current_conflicts() end to end.
    def _no_roster():
        raise RuntimeError("no db")

    monkeypatch.setattr(staffing_mod, "load_roster", _no_roster)

    conflicts = cc.current_conflicts()
    assert {c["odoo_id"] for c in conflicts} == {2}
```

- [ ] **Step 4: Delete the old script-coupled test**

```bash
git rm tests/test_odoo_calendar_conflict_diagnostic.py
```

- [ ] **Step 5: Run detection tests + the script smoke check**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_calendar_conflicts.py -v`
Expected: PASS (10 tests).

Run: `ZIRA_API_KEY=test .venv/bin/python scripts/diagnose_odoo_calendar_conflicts.py --help`
Expected: prints usage, exit 0.

Run: `.venv/bin/python -m ruff check src/zira_dashboard/calendar_conflicts.py scripts/diagnose_odoo_calendar_conflicts.py tests/test_calendar_conflicts.py`
Expected: All checks passed.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/calendar_conflicts.py scripts/diagnose_odoo_calendar_conflicts.py tests/test_calendar_conflicts.py
git rm tests/test_odoo_calendar_conflict_diagnostic.py
git commit -m "refactor(diagnostic): extract detection into calendar_conflicts module"
```

---

### Task 2: Odoo task helpers (`update_task`, `post_task_message`)

**Files:**
- Modify: `src/zira_dashboard/odoo_client.py` (add after `create_feedback_task`)
- Test: `tests/test_odoo_task_helpers.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_odoo_task_helpers.py`:

```python
from unittest.mock import MagicMock

import zira_dashboard.odoo_client as oc


def test_update_task_writes_fields(monkeypatch):
    execute = MagicMock(return_value=True)
    monkeypatch.setattr(oc, "execute", execute)
    oc.update_task(55, active=False, description="<p>x</p>")
    execute.assert_called_once_with("project.task", "write", [55], {"active": False, "description": "<p>x</p>"})


def test_post_task_message_posts_to_chatter(monkeypatch):
    execute = MagicMock(return_value=1)
    monkeypatch.setattr(oc, "execute", execute)
    oc.post_task_message(55, "hello")
    execute.assert_called_once_with("project.task", "message_post", [55], body="hello")
```

- [ ] **Step 2: Run to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_odoo_task_helpers.py -v`
Expected: FAIL — `AttributeError: module 'zira_dashboard.odoo_client' has no attribute 'update_task'`.

- [ ] **Step 3: Implement the helpers**

In `src/zira_dashboard/odoo_client.py`, immediately after the `create_feedback_task` function (before `add_task_attachment`), add:

```python
def update_task(task_id: int, **fields: Any) -> None:
    """Write fields on a project.task (e.g. description=..., active=False)."""
    execute("project.task", "write", [task_id], fields)


def post_task_message(task_id: int, body: str) -> None:
    """Post a message to a project.task's chatter (mirrors post_leave_message:
    `body` is forwarded as Odoo's keyword arg by `execute`)."""
    execute("project.task", "message_post", [task_id], body=body)
```

- [ ] **Step 4: Run to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_odoo_task_helpers.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/odoo_client.py tests/test_odoo_task_helpers.py
git commit -m "feat(odoo): add update_task + post_task_message helpers"
```

---

### Task 3: Add the `calendar_conflict_monitor` state table

**Files:**
- Modify: `src/zira_dashboard/_schema.py`

- [ ] **Step 1: Add the table to `SCHEMA_DDL`**

In `src/zira_dashboard/_schema.py`, append this block to the end of the `SCHEMA_DDL` string (just before the closing `"""`):

```sql
-- 2026-06-29: weekly Odoo calendar-conflict monitor state (single row).
-- reported_emp_ids is the conflict set last reported; last_run_at gates the
-- ~weekly cadence so frequent redeploys only re-check the gate.
CREATE TABLE IF NOT EXISTS calendar_conflict_monitor (
  id                INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  odoo_task_id      INTEGER,
  reported_emp_ids  INTEGER[] NOT NULL DEFAULT '{}',
  last_run_at       TIMESTAMPTZ
);
```

- [ ] **Step 2: Verify the DDL still applies (syntax / import)**

Run: `ZIRA_API_KEY=test .venv/bin/python -c "from zira_dashboard._schema import SCHEMA_DDL; assert 'calendar_conflict_monitor' in SCHEMA_DDL; print('ok')"`
Expected: prints `ok`.

(The table is created on the next `db.bootstrap_schema()` at app boot; no local Postgres needed for this step.)

- [ ] **Step 3: Commit**

```bash
git add src/zira_dashboard/_schema.py
git commit -m "feat(schema): add calendar_conflict_monitor state table"
```

---

### Task 4: The pure `decide()` diff

**Files:**
- Create: `src/zira_dashboard/calendar_conflict_monitor.py` (start the module)
- Test: `tests/test_calendar_conflict_monitor.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_calendar_conflict_monitor.py`:

```python
import zira_dashboard.calendar_conflict_monitor as mon


def test_decide_unchanged_set_is_not_changed():
    d = mon.decide({1, 2}, {1, 2})
    assert d == {"changed": False, "added": [], "removed": [], "now_empty": False}


def test_decide_new_employee_is_added():
    d = mon.decide({1, 2, 3}, {1, 2})
    assert d["changed"] is True
    assert d["added"] == [3]
    assert d["removed"] == []
    assert d["now_empty"] is False


def test_decide_resolved_employee_is_removed():
    d = mon.decide({1}, {1, 2})
    assert d["changed"] is True
    assert d["added"] == []
    assert d["removed"] == [2]
    assert d["now_empty"] is False


def test_decide_all_resolved_is_now_empty():
    d = mon.decide(set(), {1, 2})
    assert d["changed"] is True
    assert d["removed"] == [1, 2]
    assert d["now_empty"] is True


def test_decide_empty_to_empty_is_not_changed():
    d = mon.decide(set(), set())
    assert d["changed"] is False
    assert d["now_empty"] is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_calendar_conflict_monitor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'zira_dashboard.calendar_conflict_monitor'`.

- [ ] **Step 3: Create the module with `decide()`**

Create `src/zira_dashboard/calendar_conflict_monitor.py`:

```python
"""Weekly monitor: diff the Odoo calendar-conflict set and keep one Odoo task.

Runs on the in-process warmer (see app.py), gated to ≥7 days via the
calendar_conflict_monitor state row. Best-effort — the warmer logs/swallows.
See docs/superpowers/specs/2026-06-29-calendar-conflict-monitor-design.md
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import calendar_conflicts, db, odoo_client

_log = logging.getLogger(__name__)

THROTTLE = timedelta(days=7)
_TASK_NAME = "Odoo work-schedule conflicts"


def decide(current_ids, reported_ids) -> dict:
    """Pure diff of the conflict employee-id sets.

    Returns {changed, added (sorted ids), removed (sorted ids), now_empty}.
    """
    current = set(current_ids)
    reported = set(reported_ids)
    added = sorted(current - reported)
    removed = sorted(reported - current)
    return {
        "changed": bool(added or removed),
        "added": added,
        "removed": removed,
        "now_empty": len(current) == 0,
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_calendar_conflict_monitor.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/calendar_conflict_monitor.py tests/test_calendar_conflict_monitor.py
git commit -m "feat(monitor): pure decide() conflict-set diff"
```

---

### Task 5: State + `run_once()` orchestration

**Files:**
- Modify: `src/zira_dashboard/calendar_conflict_monitor.py`
- Test: `tests/test_calendar_conflict_monitor.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_calendar_conflict_monitor.py`:

```python
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_state(monkeypatch):
    state = {"odoo_task_id": None, "reported_emp_ids": [], "last_run_at": None}
    monkeypatch.setattr(mon, "_load_state", lambda: dict(state))
    saved = {}

    def _save(odoo_task_id, reported_emp_ids, last_run_at):
        saved.update(
            odoo_task_id=odoo_task_id,
            reported_emp_ids=sorted(reported_emp_ids),
            last_run_at=last_run_at,
        )

    monkeypatch.setattr(mon, "_save_state", _save)
    return state, saved


def _patch_conflicts(monkeypatch, rows):
    monkeypatch.setattr(mon.calendar_conflicts, "current_conflicts", lambda: rows)


def _conflict(odoo_id, name, missing):
    return {
        "name": name, "odoo_id": odoo_id, "cal_name": "M-Th",
        "covered": {0, 1, 2, 3}, "missing": missing, "verdict": "missing_days",
    }


def test_run_once_throttled_when_recent(fake_state, monkeypatch):
    state, saved = fake_state
    state["last_run_at"] = datetime.now(timezone.utc)
    current = MagicMock()
    monkeypatch.setattr(mon.calendar_conflicts, "current_conflicts", current)

    result = mon.run_once()

    assert result == {"skipped": "throttled"}
    current.assert_not_called()
    assert saved == {}


def test_run_once_first_run_creates_task(fake_state, monkeypatch):
    state, saved = fake_state  # last_run_at None -> due
    _patch_conflicts(monkeypatch, [_conflict(7, "Gerardo", {4})])
    monkeypatch.setattr(mon.odoo_client, "ensure_feedback_project", lambda: 3)
    monkeypatch.setattr(mon.odoo_client, "authenticate", lambda: 9)
    create = MagicMock(return_value=111)
    monkeypatch.setattr(mon.odoo_client, "create_feedback_task", create)
    comment = MagicMock()
    monkeypatch.setattr(mon.odoo_client, "post_task_message", comment)
    monkeypatch.setattr(mon.odoo_client, "update_task", MagicMock())

    result = mon.run_once()

    assert result["changed"] is True
    create.assert_called_once()
    comment.assert_called_once()
    assert saved["odoo_task_id"] == 111
    assert saved["reported_emp_ids"] == [7]
    assert saved["last_run_at"] is not None


def test_run_once_unchanged_is_silent(fake_state, monkeypatch):
    state, saved = fake_state
    state["last_run_at"] = datetime.now(timezone.utc) - timedelta(days=8)  # due
    state["reported_emp_ids"] = [7]
    state["odoo_task_id"] = 111
    _patch_conflicts(monkeypatch, [_conflict(7, "Gerardo", {4})])
    create = MagicMock()
    comment = MagicMock()
    monkeypatch.setattr(mon.odoo_client, "create_feedback_task", create)
    monkeypatch.setattr(mon.odoo_client, "post_task_message", comment)
    monkeypatch.setattr(mon.odoo_client, "update_task", MagicMock())

    result = mon.run_once()

    assert result["changed"] is False
    create.assert_not_called()
    comment.assert_not_called()
    assert saved["reported_emp_ids"] == [7]
    assert saved["odoo_task_id"] == 111
    assert saved["last_run_at"] is not None  # gate advanced


def test_run_once_all_resolved_archives_task(fake_state, monkeypatch):
    state, saved = fake_state
    state["last_run_at"] = datetime.now(timezone.utc) - timedelta(days=8)
    state["reported_emp_ids"] = [7]
    state["odoo_task_id"] = 111
    _patch_conflicts(monkeypatch, [])
    comment = MagicMock()
    update = MagicMock()
    monkeypatch.setattr(mon.odoo_client, "post_task_message", comment)
    monkeypatch.setattr(mon.odoo_client, "update_task", update)
    monkeypatch.setattr(mon.odoo_client, "create_feedback_task", MagicMock())

    result = mon.run_once()

    assert result["now_empty"] is True
    comment.assert_called_once()
    update.assert_called_once_with(111, active=False)
    assert saved["odoo_task_id"] is None
    assert saved["reported_emp_ids"] == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_calendar_conflict_monitor.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_load_state'` / `run_once`.

- [ ] **Step 3: Implement state + `run_once`**

Append to `src/zira_dashboard/calendar_conflict_monitor.py`:

```python
def _load_state() -> dict:
    rows = db.query(
        "SELECT odoo_task_id, reported_emp_ids, last_run_at "
        "FROM calendar_conflict_monitor WHERE id = 1"
    )
    if not rows:
        return {"odoo_task_id": None, "reported_emp_ids": [], "last_run_at": None}
    r = rows[0]
    return {
        "odoo_task_id": r["odoo_task_id"],
        "reported_emp_ids": list(r["reported_emp_ids"] or []),
        "last_run_at": r["last_run_at"],
    }


def _save_state(odoo_task_id, reported_emp_ids, last_run_at) -> None:
    db.execute(
        "INSERT INTO calendar_conflict_monitor (id, odoo_task_id, reported_emp_ids, last_run_at) "
        "VALUES (1, %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET "
        "  odoo_task_id = EXCLUDED.odoo_task_id, "
        "  reported_emp_ids = EXCLUDED.reported_emp_ids, "
        "  last_run_at = EXCLUDED.last_run_at",
        (odoo_task_id, sorted(reported_emp_ids), last_run_at),
    )


def _build_task_body(conflicts) -> str:
    rows = sorted(conflicts, key=lambda c: c["name"].lower())
    items = []
    for c in rows:
        if c["verdict"] == "missing_days":
            detail = f"calendar {c['cal_name']} missing {calendar_conflicts.fmt_days(c['missing'])}"
        elif c["verdict"] == "flexible":
            detail = f"calendar {c['cal_name']} is flexible / has no fixed hours"
        else:
            detail = "no Odoo work schedule"
        items.append(f"<li>{c['name']} (id {c['odoo_id']}) — {detail}</li>")
    return (
        "<p>These employees' Odoo work schedule has no working hours on a plant "
        "workday, so declaring them absent can't sync to Odoo Time Off. Fix each "
        "one's Working Schedule in Odoo.</p><ul>" + "".join(items) + "</ul>"
    )


def _summary_comment(decision, names_by_id) -> str:
    parts = []
    if decision["added"]:
        parts.append("New conflicts: " + ", ".join(names_by_id.get(i, f"id {i}") for i in decision["added"]))
    if decision["removed"]:
        parts.append("Resolved: " + ", ".join(f"id {i}" for i in decision["removed"]))
    return "; ".join(parts) or "Updated."


def run_once(force: bool = False) -> dict:
    """Weekly check. Best-effort; raises propagate to the warmer (logged/swallowed)."""
    state = _load_state()
    now = datetime.now(timezone.utc)
    if not force and state["last_run_at"] and (now - state["last_run_at"]) < THROTTLE:
        return {"skipped": "throttled"}

    conflicts = calendar_conflicts.current_conflicts()
    names_by_id = {int(c["odoo_id"]): c["name"] for c in conflicts if c.get("odoo_id") is not None}
    current_ids = set(names_by_id)
    reported = set(state["reported_emp_ids"])
    decision = decide(current_ids, reported)
    task_id = state["odoo_task_id"]

    if decision["changed"]:
        if decision["now_empty"]:
            if task_id:
                odoo_client.post_task_message(task_id, "✅ All Odoo work-schedule conflicts resolved.")
                odoo_client.update_task(task_id, active=False)
            task_id = None
        else:
            if not task_id:
                task_id = odoo_client.create_feedback_task(
                    project_id=odoo_client.ensure_feedback_project(),
                    name=_TASK_NAME,
                    description_html=_build_task_body(conflicts),
                    assignee_uid=odoo_client.authenticate(),
                    tag_id=None,
                    deadline=(now.date() + timedelta(days=7)).isoformat(),
                )
            else:
                odoo_client.update_task(task_id, description=_build_task_body(conflicts))
            odoo_client.post_task_message(task_id, _summary_comment(decision, names_by_id))

    _save_state(odoo_task_id=task_id, reported_emp_ids=current_ids, last_run_at=now)
    _log.info(
        "calendar-conflict monitor: %d conflict(s), changed=%s, task=%s",
        len(current_ids), decision["changed"], task_id,
    )
    return {"changed": decision["changed"], "now_empty": decision["now_empty"], "task_id": task_id, "count": len(current_ids)}
```

- [ ] **Step 4: Run to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_calendar_conflict_monitor.py -v`
Expected: PASS (10 tests).

Run: `.venv/bin/python -m ruff check src/zira_dashboard/calendar_conflict_monitor.py tests/test_calendar_conflict_monitor.py`
Expected: All checks passed.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/calendar_conflict_monitor.py tests/test_calendar_conflict_monitor.py
git commit -m "feat(monitor): state persistence + run_once() Odoo-task sync"
```

---

### Task 6: Register the warmer

**Files:**
- Modify: `src/zira_dashboard/app.py` (add tick coroutine near the other `_tick_*`; add registry entry in `_WARMERS`)

- [ ] **Step 1: Add the tick coroutine**

In `src/zira_dashboard/app.py`, after `_tick_inbox_reconcile` (around line 185), add:

```python
async def _tick_calendar_conflicts():
    """Weekly Odoo calendar-conflict check. Interval is short; run_once()
    self-throttles to ~weekly via its persisted last_run_at gate."""
    from . import calendar_conflict_monitor
    await asyncio.to_thread(calendar_conflict_monitor.run_once)
```

- [ ] **Step 2: Register it in `_WARMERS`**

In the `_WARMERS` list, add this entry after `("Inbox reconcile", _tick_inbox_reconcile, 60),`:

```python
    ("calendar conflicts", _tick_calendar_conflicts, 21600),
```

- [ ] **Step 3: Verify the app module imports**

Run: `ZIRA_API_KEY=test .venv/bin/python -c "import zira_dashboard.app as a; names=[w[0] for w in a._WARMERS]; assert 'calendar conflicts' in names, names; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Run the full suite + ruff**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: PASS (all green; only DB/Odoo-gated tests skip).

Run: `.venv/bin/python -m ruff check src tests scripts`
Expected: All checks passed.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/app.py
git commit -m "feat(monitor): register weekly calendar-conflict warmer"
```

---

## Notes for the implementer

- **Run path:** the monitor runs inside the deployed app (full Odoo + Postgres), so no `railway run` constraint applies to it — only to the CLI script.
- **First run:** on the first deploy after this lands, `last_run_at` is NULL, so `run_once` fires on boot; if there are conflicts it files the task. Subsequent boots within 7 days are throttled.
- **Live smoke test (manual, after deploy):** the Odoo-task path is mock-tested only. After merge, confirm in Odoo that the "Odoo work-schedule conflicts" task appeared in the Plant Manager project with the expected employee list. (No code; operational check.)
