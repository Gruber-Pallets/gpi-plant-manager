# Department-Driven Punch Rounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Select a timeclock punch's four rounding windows by the static department of the work center the employee works that day (scheduled WC → clock-in WC → plant default), while shift boundaries stay Odoo-sourced and `apply_rounding` is untouched.

**Architecture:** Two new Postgres tables — `rounding_systems` (named sets of four windows) and `department_rounding` (static department → system) — seeded idempotently in the schema DDL, with `rounding_settings` id=1 remaining the plant-default fallback. A new cached `rounding_system_store` serves the punch hot path. The punch resolver in `routes/timeclock.py` splits into `_hours_for_punch` (unchanged hours logic) and `_windows_for_day` (department-driven windows). The settings page gains a four-block rounding UI.

**Tech Stack:** Python 3.11+, FastAPI, psycopg2 + Postgres, Jinja2 templates, pytest.

**Source spec:** [docs/superpowers/specs/2026-06-04-department-driven-rounding-design.md](../specs/2026-06-04-department-driven-rounding-design.md)

---

## Running tests (read first)

Tests here are split into two kinds:

- **No-DB unit tests** (no live Postgres needed): `tests/test_staffing_department_for_wc.py`
  (imports only `staffing` — genuinely standalone) and
  `tests/test_rounding_windows_for_day.py` (monkeypatched — no DB calls, but it
  imports `routes.timeclock`, which pulls the normal app import chain, so the
  standard CI test env — e.g. `ZIRA_API_KEY` — must be present for collection,
  same as every other test in this repo).
- **Postgres-backed tests** (skipped unless `DATABASE_URL` is set): everything
  touching `db`. These run in CI (GitHub Actions Postgres service) and locally
  only when `DATABASE_URL` points at a test DB.

Per the local-environment constraint (only the Odoo-bundled Python is reliably
available), if you cannot run `python -m pytest` locally, treat **CI** as the
red/green authority and use `ruff check .` + `python -m py_compile <file>` for
fast local feedback after each task. The "Expected: FAIL/PASS" lines below
describe what CI (or a local run with `DATABASE_URL`) should show.

Run a single Postgres test, e.g.:
`python -m pytest tests/test_rounding_system_store.py -v`

---

## Implementation decisions locked in from the spec

- **Static department** = `staffing.Location.department` (Recycled / New /
  Supervisor / Maintenance / Transportation), NOT the user-editable
  `work_centers_store.department`.
- **Seeds:** Plant Operator = copy of `rounding_settings` id=1 (preserves
  current plant-floor behavior); Transportation = `20/0/0/0`; Supervisor =
  `0/0/0/0`. Map: Recycled/New/Maintenance → Plant Operator, Supervisor →
  Supervisor, Transportation → Transportation.
- **Schedule read:** `_windows_for_day` uses `staffing.load_schedule(d).assignments`
  whether or not the day is published — a draft assignment still reflects intent,
  and the clock-in-WC fallback covers truly-unscheduled people. (If you ever want
  published-only, gate on `sched.published`.)
- **Multi-department day:** the first scheduled WC (by work-center id, the
  assignments dict's insertion order) that resolves to a department wins.
- **`work_schedules` table:** kept for per-Odoo-schedule HOURS. Its rounding-window
  columns and the `/settings/work_schedule_rounding` save route stay in place but
  are no longer surfaced in the UI (vestigial; no migration to drop them).

---

### Task 1: Schema — `rounding_systems` + `department_rounding` tables and seed

**Files:**
- Modify: `src/zira_dashboard/_schema.py` (append to `SCHEMA_DDL`, before the closing `"""`)
- Test: `tests/test_rounding_systems_schema.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_rounding_systems_schema.py`:

```python
"""Seed + DDL for department-driven rounding. Postgres-backed."""

import os

import pytest

from zira_dashboard import db

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)


def test_seed_creates_three_systems():
    db.bootstrap_schema()
    names = {r["name"] for r in db.query("SELECT name FROM rounding_systems")}
    assert {"Plant Operator", "Supervisor", "Transportation"} <= names


def test_seed_maps_five_departments_to_named_systems():
    db.bootstrap_schema()
    rows = db.query(
        "SELECT dr.department, rs.name AS system_name "
        "FROM department_rounding dr JOIN rounding_systems rs ON rs.id = dr.system_id"
    )
    m = {r["department"]: r["system_name"] for r in rows}
    assert m["Recycled"] == "Plant Operator"
    assert m["New"] == "Plant Operator"
    assert m["Supervisor"] == "Supervisor"
    assert m["Transportation"] == "Transportation"
    assert m["Maintenance"] == "Plant Operator"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_rounding_systems_schema.py -v`
Expected: FAIL — `psycopg2.errors.UndefinedTable: relation "rounding_systems" does not exist`.

- [ ] **Step 3: Append the tables + seed to `SCHEMA_DDL`**

In `src/zira_dashboard/_schema.py`, add this block at the very END of the
`SCHEMA_DDL` string (after the `auto_lunch_settings` insert, before the closing
`"""`). It must come after the `rounding_settings` row exists earlier in the DDL
(it does), since Plant Operator copies from it:

```sql

-- Department-driven rounding (2026-06-04). Named rounding "systems" (each a set
-- of the four windows) are selected by the static department an employee works
-- that day (staffing.Location.department). rounding_settings id=1 remains the
-- plant-default fallback for any punch that doesn't resolve to a mapped dept.
CREATE TABLE IF NOT EXISTS rounding_systems (
  id              SERIAL PRIMARY KEY,
  name            TEXT NOT NULL UNIQUE,
  in_before_min   INT NOT NULL DEFAULT 0,
  in_after_min    INT NOT NULL DEFAULT 0,
  out_before_min  INT NOT NULL DEFAULT 0,
  out_after_min   INT NOT NULL DEFAULT 0,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS department_rounding (
  department  TEXT PRIMARY KEY,
  system_id   INTEGER REFERENCES rounding_systems(id) ON DELETE SET NULL
);

-- Seed the three systems (idempotent via UNIQUE(name)). Plant Operator inherits
-- the current plant-default windows so Recycled/New behavior is preserved on
-- migration; Transportation seeds to the known driver policy; Supervisor starts
-- at no-rounding for Dale to set.
INSERT INTO rounding_systems (name, in_before_min, in_after_min, out_before_min, out_after_min)
  SELECT 'Plant Operator', in_before_min, in_after_min, out_before_min, out_after_min
  FROM rounding_settings WHERE id = 1
  ON CONFLICT (name) DO NOTHING;
INSERT INTO rounding_systems (name, in_before_min, in_after_min, out_before_min, out_after_min)
  VALUES ('Transportation', 20, 0, 0, 0)
  ON CONFLICT (name) DO NOTHING;
INSERT INTO rounding_systems (name)
  VALUES ('Supervisor')
  ON CONFLICT (name) DO NOTHING;

-- Seed the department->system map (idempotent via PRIMARY KEY(department)).
INSERT INTO department_rounding (department, system_id)
  SELECT 'Recycled', id FROM rounding_systems WHERE name = 'Plant Operator'
  ON CONFLICT (department) DO NOTHING;
INSERT INTO department_rounding (department, system_id)
  SELECT 'New', id FROM rounding_systems WHERE name = 'Plant Operator'
  ON CONFLICT (department) DO NOTHING;
INSERT INTO department_rounding (department, system_id)
  SELECT 'Supervisor', id FROM rounding_systems WHERE name = 'Supervisor'
  ON CONFLICT (department) DO NOTHING;
INSERT INTO department_rounding (department, system_id)
  SELECT 'Transportation', id FROM rounding_systems WHERE name = 'Transportation'
  ON CONFLICT (department) DO NOTHING;
INSERT INTO department_rounding (department, system_id)
  SELECT 'Maintenance', id FROM rounding_systems WHERE name = 'Plant Operator'
  ON CONFLICT (department) DO NOTHING;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_rounding_systems_schema.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/_schema.py tests/test_rounding_systems_schema.py
git commit -m "feat(rounding): add rounding_systems + department_rounding tables and seed"
```

---

### Task 2: `staffing.department_for_wc` helper

**Files:**
- Modify: `src/zira_dashboard/staffing.py` (after the `LOCATIONS` tuple / `required_skills_for`)
- Test: `tests/test_staffing_department_for_wc.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_staffing_department_for_wc.py` (pure — no Postgres):

```python
"""staffing.department_for_wc maps a work-center name to its static department."""

from zira_dashboard import staffing


def test_known_work_centers_map_to_static_departments():
    assert staffing.department_for_wc("Dismantler 1") == "Recycled"
    assert staffing.department_for_wc("Tablets") == "Supervisor"
    assert staffing.department_for_wc("Truck Driver") == "Transportation"
    assert staffing.department_for_wc("Work Orders") == "Maintenance"


def test_unknown_or_blank_work_center_returns_none():
    assert staffing.department_for_wc("Nonexistent WC") is None
    assert staffing.department_for_wc("") is None
    assert staffing.department_for_wc(None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_staffing_department_for_wc.py -v`
Expected: FAIL — `AttributeError: module 'zira_dashboard.staffing' has no attribute 'department_for_wc'`.

- [ ] **Step 3: Add the helper**

In `src/zira_dashboard/staffing.py`, immediately after the `required_skills_for`
function (just before `DEPARTMENT_ORDER`), add:

```python
# Static work-center -> department map, built once from LOCATIONS. This is the
# `Location.department` classification (Recycled / New / Supervisor /
# Maintenance / Transportation) — NOT the user-editable
# work_centers_store.department. Drives department-based punch rounding.
_LOCATION_DEPARTMENT: dict[str, str] = {loc.name: loc.department for loc in LOCATIONS}


def department_for_wc(wc_name: str | None) -> str | None:
    """Static department for a work-center name, or None if unknown/blank."""
    if not wc_name:
        return None
    return _LOCATION_DEPARTMENT.get(wc_name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_staffing_department_for_wc.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/staffing.py tests/test_staffing_department_for_wc.py
git commit -m "feat(staffing): add department_for_wc static-department lookup"
```

---

### Task 3: `rounding_system_store.py` — cached store

**Files:**
- Create: `src/zira_dashboard/rounding_system_store.py`
- Test: `tests/test_rounding_system_store.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_rounding_system_store.py`:

```python
"""Tests for rounding_system_store: CRUD, department map, cache. Postgres-backed."""

import os

import pytest

from zira_dashboard import db, rounding_system_store
from zira_dashboard.rounding import RoundingSettings

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)

SYS_NAME = "ZZ Test System"
DEPT = "ZZ Test Dept"


@pytest.fixture(autouse=True)
def _clean():
    db.execute("DELETE FROM department_rounding WHERE department = %s", (DEPT,))
    db.execute("DELETE FROM rounding_systems WHERE name LIKE 'ZZ Test%'")
    rounding_system_store.reload()
    yield
    db.execute("DELETE FROM department_rounding WHERE department = %s", (DEPT,))
    db.execute("DELETE FROM rounding_systems WHERE name LIKE 'ZZ Test%'")
    rounding_system_store.reload()


def _sid(name=SYS_NAME):
    return next(s.id for s in rounding_system_store.all_systems() if s.name == name)


def test_add_then_save_windows():
    rounding_system_store.add_system(SYS_NAME)
    sid = _sid()
    rounding_system_store.save_system_windows(sid, RoundingSettings(20, 0, 0, 5))
    sysrec = next(s for s in rounding_system_store.all_systems() if s.id == sid)
    assert sysrec.rounding == RoundingSettings(20, 0, 0, 5)


def test_map_department_resolves_windows():
    rounding_system_store.add_system(SYS_NAME)
    sid = _sid()
    rounding_system_store.save_system_windows(sid, RoundingSettings(20, 0, 0, 0))
    rounding_system_store.set_department_system(DEPT, sid)
    assert rounding_system_store.windows_for_department(DEPT) == RoundingSettings(20, 0, 0, 0)


def test_unmapped_or_blank_department_returns_none():
    assert rounding_system_store.windows_for_department(DEPT) is None
    assert rounding_system_store.windows_for_department("") is None


def test_delete_system_unsets_mapping():
    rounding_system_store.add_system(SYS_NAME)
    sid = _sid()
    rounding_system_store.set_department_system(DEPT, sid)
    rounding_system_store.delete_system(sid)  # ON DELETE SET NULL -> no system
    assert rounding_system_store.windows_for_department(DEPT) is None


def test_rename_system():
    rounding_system_store.add_system(SYS_NAME)
    sid = _sid()
    rounding_system_store.rename_system(sid, "ZZ Test System Renamed")
    names = {s.name for s in rounding_system_store.all_systems()}
    assert "ZZ Test System Renamed" in names


def test_department_map_includes_unset_as_none():
    rounding_system_store.set_department_system(DEPT, None)
    assert rounding_system_store.department_map().get(DEPT) is None


def test_cache_invalidated_on_reload():
    rounding_system_store.add_system(SYS_NAME)
    sid = _sid()
    rounding_system_store.save_system_windows(sid, RoundingSettings(10, 0, 0, 0))
    rounding_system_store.set_department_system(DEPT, sid)
    assert rounding_system_store.windows_for_department(DEPT) == RoundingSettings(10, 0, 0, 0)
    db.execute("UPDATE rounding_systems SET in_before_min = 30 WHERE id = %s", (sid,))
    assert rounding_system_store.windows_for_department(DEPT).in_before_min == 10  # stale cache
    rounding_system_store.reload()
    assert rounding_system_store.windows_for_department(DEPT).in_before_min == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_rounding_system_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'zira_dashboard.rounding_system_store'`.

- [ ] **Step 3: Write the store**

Create `src/zira_dashboard/rounding_system_store.py`:

```python
"""Named rounding systems + the department->system map, cached in-process.

A "rounding system" is a named set of the four rounding windows (e.g. "Plant
Operator", "Supervisor", "Transportation"). Each static department
(staffing.Location.department) maps to at most one system; an employee's punches
use the system of the department they work that day. Resolution at punch time
reads the in-process cache, never the DB — same rationale as rounding_store /
work_schedule_store.

Anything that doesn't resolve to a mapped department + existing system falls
back to the plant default (rounding_settings id=1, via rounding_store).
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from .rounding import RoundingSettings


@dataclass(frozen=True)
class RoundingSystem:
    id: int
    name: str
    rounding: RoundingSettings


_lock = RLock()
# (systems_by_id, windows_by_department)
_cache: tuple[dict[int, RoundingSystem], dict[str, RoundingSettings]] | None = None


def _load_from_db() -> tuple[dict[int, RoundingSystem], dict[str, RoundingSettings]]:
    from . import db
    sys_rows = db.query(
        "SELECT id, name, in_before_min, in_after_min, out_before_min, out_after_min "
        "FROM rounding_systems"
    )
    systems: dict[int, RoundingSystem] = {}
    for r in sys_rows:
        systems[int(r["id"])] = RoundingSystem(
            id=int(r["id"]),
            name=str(r["name"]),
            rounding=RoundingSettings(
                in_before_min=int(r["in_before_min"]),
                in_after_min=int(r["in_after_min"]),
                out_before_min=int(r["out_before_min"]),
                out_after_min=int(r["out_after_min"]),
            ),
        )
    map_rows = db.query(
        "SELECT department, system_id FROM department_rounding WHERE system_id IS NOT NULL"
    )
    by_dept: dict[str, RoundingSettings] = {}
    for r in map_rows:
        sysrec = systems.get(int(r["system_id"]))
        if sysrec is not None:
            by_dept[str(r["department"])] = sysrec.rounding
    return systems, by_dept


def _cached() -> tuple[dict[int, RoundingSystem], dict[str, RoundingSettings]]:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load_from_db()
        return _cache


def windows_for_department(department: str | None) -> RoundingSettings | None:
    """Rounding windows for a static department, or None if the department is
    unmapped or its system was deleted. Cache read — safe on the punch path."""
    if not department:
        return None
    return _cached()[1].get(department)


def all_systems() -> list[RoundingSystem]:
    """All systems, sorted by name (for the settings UI)."""
    return sorted(_cached()[0].values(), key=lambda s: s.name.lower())


def department_map() -> dict[str, int | None]:
    """{department: system_id or None} for every department_rounding row.
    Settings-UI helper — reads the DB directly (infrequent, not the punch path)."""
    from . import db
    rows = db.query("SELECT department, system_id FROM department_rounding")
    return {
        str(r["department"]): (int(r["system_id"]) if r["system_id"] is not None else None)
        for r in rows
    }


def add_system(name: str) -> None:
    name = (name or "").strip()[:80]
    if not name:
        return
    from . import db
    db.execute(
        "INSERT INTO rounding_systems (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
        (name,),
    )
    reload()


def save_system_windows(system_id: int, r: RoundingSettings) -> None:
    from . import db
    db.execute(
        "UPDATE rounding_systems SET in_before_min = %s, in_after_min = %s, "
        "out_before_min = %s, out_after_min = %s, updated_at = now() WHERE id = %s",
        (r.in_before_min, r.in_after_min, r.out_before_min, r.out_after_min, int(system_id)),
    )
    reload()


def rename_system(system_id: int, new_name: str) -> None:
    new_name = (new_name or "").strip()[:80]
    if not new_name:
        return
    from . import db
    db.execute(
        "UPDATE rounding_systems SET name = %s, updated_at = now() WHERE id = %s",
        (new_name, int(system_id)),
    )
    reload()


def delete_system(system_id: int) -> None:
    from . import db
    db.execute("DELETE FROM rounding_systems WHERE id = %s", (int(system_id),))
    reload()


def set_department_system(department: str, system_id: int | None) -> None:
    department = (department or "").strip()
    if not department:
        return
    from . import db
    db.execute(
        "INSERT INTO department_rounding (department, system_id) VALUES (%s, %s) "
        "ON CONFLICT (department) DO UPDATE SET system_id = EXCLUDED.system_id",
        (department, int(system_id) if system_id is not None else None),
    )
    reload()


def reload() -> tuple[dict[int, RoundingSystem], dict[str, RoundingSettings]]:
    """Force a fresh read from Postgres, bypassing the cache."""
    global _cache
    with _lock:
        _cache = _load_from_db()
        return _cache
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_rounding_system_store.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/rounding_system_store.py tests/test_rounding_system_store.py
git commit -m "feat(rounding): add cached rounding_system_store"
```

---

### Task 4: Split the punch resolver in `routes/timeclock.py`

Splits `_shift_for_punch` into `_hours_for_punch` (unchanged hours logic) and
`_windows_for_day` (department-driven), adds `_effective_punch_wc`, and rewires
`_open_log_row`. Also migrates the now-obsolete `tests/test_shift_for_punch.py`.

**Files:**
- Modify: `src/zira_dashboard/routes/timeclock.py` (`_shift_for_punch` → split; `_open_log_row`)
- Delete: `tests/test_shift_for_punch.py`
- Create: `tests/test_hours_for_punch.py`
- Create: `tests/test_rounding_windows_for_day.py`

- [ ] **Step 1: Write the failing pure unit tests for `_windows_for_day` + `_effective_punch_wc`**

Create `tests/test_rounding_windows_for_day.py` (pure — no Postgres; monkeypatched):

```python
"""Department-driven window resolution + effective-WC selection (pure, monkeypatched)."""

from datetime import date

from zira_dashboard import staffing, rounding_store, rounding_system_store
from zira_dashboard.rounding import RoundingSettings
from zira_dashboard.routes import timeclock

MONDAY = date(2026, 6, 1)


def _sched(assignments):
    return staffing.Schedule(day=MONDAY, published=True, assignments=assignments)


# ---- _windows_for_day ----

def test_scheduled_dept_selects_system(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", lambda d: _sched({"Dismantler 1": ["Alice"]}))
    monkeypatch.setattr(rounding_system_store, "windows_for_department",
                        lambda dept: RoundingSettings(20, 0, 0, 0) if dept == "Recycled" else None)
    monkeypatch.setattr(rounding_store, "current", lambda: RoundingSettings(0, 0, 0, 0))
    assert timeclock._windows_for_day("Alice", MONDAY, None) == RoundingSettings(20, 0, 0, 0)


def test_tablets_resolves_supervisor_system(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", lambda d: _sched({"Tablets": ["Bob"]}))
    monkeypatch.setattr(rounding_system_store, "windows_for_department",
                        lambda dept: RoundingSettings(5, 5, 5, 5) if dept == "Supervisor" else None)
    monkeypatch.setattr(rounding_store, "current", lambda: RoundingSettings(0, 0, 0, 0))
    assert timeclock._windows_for_day("Bob", MONDAY, None) == RoundingSettings(5, 5, 5, 5)


def test_unscheduled_falls_back_to_clock_in_wc(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", lambda d: _sched({}))
    monkeypatch.setattr(rounding_system_store, "windows_for_department",
                        lambda dept: RoundingSettings(20, 0, 0, 0) if dept == "Transportation" else None)
    monkeypatch.setattr(rounding_store, "current", lambda: RoundingSettings(0, 0, 0, 0))
    assert timeclock._windows_for_day("Carlos", MONDAY, "Truck Driver") == RoundingSettings(20, 0, 0, 0)


def test_no_schedule_no_wc_uses_plant_default(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", lambda d: _sched({}))
    monkeypatch.setattr(rounding_system_store, "windows_for_department", lambda dept: None)
    monkeypatch.setattr(rounding_store, "current", lambda: RoundingSettings(7, 7, 7, 7))
    assert timeclock._windows_for_day("Dee", MONDAY, None) == RoundingSettings(7, 7, 7, 7)


def test_unmapped_department_uses_plant_default(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", lambda d: _sched({"Work Orders": ["Eve"]}))
    monkeypatch.setattr(rounding_system_store, "windows_for_department", lambda dept: None)
    monkeypatch.setattr(rounding_store, "current", lambda: RoundingSettings(1, 2, 3, 4))
    assert timeclock._windows_for_day("Eve", MONDAY, None) == RoundingSettings(1, 2, 3, 4)


def test_multi_dept_first_scheduled_wins(monkeypatch):
    # Scheduled in Tablets (Supervisor) first, then Dismantler 1 (Recycled).
    monkeypatch.setattr(staffing, "load_schedule",
                        lambda d: _sched({"Tablets": ["Frank"], "Dismantler 1": ["Frank"]}))
    monkeypatch.setattr(rounding_system_store, "windows_for_department",
                        lambda dept: {"Supervisor": RoundingSettings(9, 0, 0, 0),
                                      "Recycled": RoundingSettings(1, 0, 0, 0)}.get(dept))
    monkeypatch.setattr(rounding_store, "current", lambda: RoundingSettings(0, 0, 0, 0))
    assert timeclock._windows_for_day("Frank", MONDAY, None) == RoundingSettings(9, 0, 0, 0)


# ---- _effective_punch_wc ----

def test_effective_wc_clock_in_uses_form_wc():
    assert timeclock._effective_punch_wc("clock_in", "Dismantler 1", 123) == "Dismantler 1"


def test_effective_wc_clock_out_uses_current_wc(monkeypatch):
    monkeypatch.setattr(timeclock, "_current_state", lambda oid: {"current_wc": "Tablets"})
    assert timeclock._effective_punch_wc("clock_out", None, 123) == "Tablets"


def test_effective_wc_transfer_is_none():
    assert timeclock._effective_punch_wc("transfer_in", "X", 123) is None
    assert timeclock._effective_punch_wc("transfer_out", None, 123) is None


def test_effective_wc_clock_out_handles_lookup_error(monkeypatch):
    def _boom(oid):
        raise RuntimeError("db down")
    monkeypatch.setattr(timeclock, "_current_state", _boom)
    assert timeclock._effective_punch_wc("clock_out", None, 123) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_rounding_windows_for_day.py -v`
Expected: FAIL — `AttributeError: module 'zira_dashboard.routes.timeclock' has no attribute '_windows_for_day'`.

- [ ] **Step 3: Replace `_shift_for_punch` with `_hours_for_punch`, `_windows_for_day`, `_effective_punch_wc`**

In `src/zira_dashboard/routes/timeclock.py`, DELETE the entire `_shift_for_punch`
function and REPLACE it with these three functions:

```python
def _hours_for_punch(resource_calendar_id, local_date):
    """Resolve (shift_start, shift_end) for a punch — unchanged from the prior
    behavior. An employee on an Odoo work schedule with synced hours for this
    weekday gets those boundaries; everyone else (and any missing hours) falls
    back to the plant default. We never guess a boundary."""
    from .. import work_schedule_store
    if resource_calendar_id is not None:
        ws = work_schedule_store.get(resource_calendar_id)
        if ws is not None:
            hours = ws.work_hours.get(local_date.weekday())
            if hours is not None:
                return hours[0], hours[1]
            _log.warning(
                "Work schedule %s has no hours for weekday %s; using plant default hours",
                resource_calendar_id, local_date.weekday(),
            )
    return (
        shift_config.shift_start_for(local_date),
        shift_config.shift_end_for(local_date),
    )


def _effective_punch_wc(action, wc_name, person_odoo_id):
    """The work center that anchors the clock-in-WC fallback for rounding:
    the form WC on clock-in; the currently clocked-in WC on clock-out (which
    carries no WC); None for transfers (never rounded). Fails safe to None."""
    if action == "clock_in":
        return wc_name
    if action == "clock_out":
        try:
            return _current_state(person_odoo_id).get("current_wc")
        except Exception:
            _log.exception("current-WC lookup failed for person %s", person_odoo_id)
            return None
    return None


def _windows_for_day(person_name, local_date, effective_wc):
    """Resolve the four rounding windows by the static department the employee
    works `local_date`: their first scheduled WC's department, else the WC they
    clock into, else the plant default. Never raises a config error past the
    fallback."""
    from .. import rounding_store, rounding_system_store
    dept = None
    if person_name:
        sched = staffing.load_schedule(local_date)
        for wc_name, names in (sched.assignments or {}).items():
            if person_name in names:
                dept = staffing.department_for_wc(wc_name)
                if dept:
                    break
    if dept is None and effective_wc:
        dept = staffing.department_for_wc(effective_wc)
    if dept:
        win = rounding_system_store.windows_for_department(dept)
        if win is not None:
            return win
    return rounding_store.current()
```

- [ ] **Step 4: Rewire `_open_log_row` to use the split resolvers**

In `_open_log_row`, replace the body of the `try:` block (the part that computes
`local_date`, calls `_shift_for_punch`, and calls `apply_rounding`) with:

```python
        local_date = occurred_at.astimezone(shift_config.SITE_TZ).date()
        prow = db.query(
            "SELECT name, resource_calendar_id FROM people WHERE odoo_id = %s",
            (person_odoo_id,),
        )
        person_name = prow[0]["name"] if prow else None
        cal_id = prow[0]["resource_calendar_id"] if prow else None
        shift_start, shift_end = _hours_for_punch(cal_id, local_date)
        effective_wc = _effective_punch_wc(action, wc_name, person_odoo_id)
        windows = _windows_for_day(person_name, local_date, effective_wc)
        rounded = rounding.apply_rounding(
            action, occurred_at, shift_start, shift_end, windows,
        )
        db.execute(
            "UPDATE timeclock_punches_log SET rounded_at = %s WHERE id = %s",
            (rounded, log_id),
        )
        return log_id, rounded
```

(The surrounding `try/except` that preserves the raw punch on failure stays
unchanged.)

- [ ] **Step 5: Migrate the obsolete `_shift_for_punch` test to `_hours_for_punch`**

Delete the old test and create the hours-only replacement:

```bash
git rm tests/test_shift_for_punch.py
```

Create `tests/test_hours_for_punch.py`:

```python
"""Resolution of (shift_start, shift_end) per Odoo work schedule. Postgres-backed."""

import os
from datetime import date, time

import pytest

from zira_dashboard import db, work_schedule_store, shift_config
from zira_dashboard.routes.timeclock import _hours_for_punch

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)

CAL_ID = 990003
MONDAY = date(2026, 6, 1)  # weekday 0


@pytest.fixture(autouse=True)
def _clean():
    db.execute("DELETE FROM work_schedules WHERE resource_calendar_id = %s", (CAL_ID,))
    work_schedule_store.reload()
    yield
    db.execute("DELETE FROM work_schedules WHERE resource_calendar_id = %s", (CAL_ID,))
    work_schedule_store.reload()


def test_override_hours_for_weekday():
    work_schedule_store.create(CAL_ID, "Drivers")
    work_schedule_store.refresh_synced(CAL_ID, "Drivers", {"0": ["05:45", "14:30"]})
    start, end = _hours_for_punch(CAL_ID, MONDAY)
    assert start == time(5, 45)
    assert end == time(14, 30)


def test_weekday_without_hours_falls_back_to_plant_default():
    work_schedule_store.create(CAL_ID, "Drivers")
    work_schedule_store.refresh_synced(CAL_ID, "Drivers", {"0": ["05:45", "14:30"]})
    saturday = date(2026, 6, 6)  # weekday 5, not configured
    start, end = _hours_for_punch(CAL_ID, saturday)
    assert start == shift_config.shift_start_for(saturday)
    assert end == shift_config.shift_end_for(saturday)


def test_no_calendar_uses_plant_default():
    start, end = _hours_for_punch(None, MONDAY)
    assert start == shift_config.shift_start_for(MONDAY)
    assert end == shift_config.shift_end_for(MONDAY)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_rounding_windows_for_day.py tests/test_hours_for_punch.py -v`
Expected: PASS. (The pure window/effective-WC tests pass with no DB; the hours
tests pass with `DATABASE_URL` set, otherwise skip.)

Also confirm nothing else imports the deleted symbol:
Run: `git grep -n "_shift_for_punch"` — Expected: no results.

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/routes/timeclock.py tests/test_hours_for_punch.py tests/test_rounding_windows_for_day.py
git rm tests/test_shift_for_punch.py
git commit -m "feat(rounding): split punch resolver into hours + department-driven windows"
```

---

### Task 5: Settings route — context + new POST routes

**Files:**
- Modify: `src/zira_dashboard/routes/settings.py` (GET context near `rounding_ctx`; new POST routes after `settings_save_rounding`)
- Test: `tests/test_settings_rounding_systems.py`

Note: `JSONResponse`, `RedirectResponse`, and `staffing` are already imported in
`settings.py`.

- [ ] **Step 1: Write the failing route tests**

Create `tests/test_settings_rounding_systems.py`:

```python
"""Settings routes for rounding systems + department mapping. Postgres-backed."""

import os

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard import db, rounding_system_store
from zira_dashboard.rounding import RoundingSettings

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)

client = TestClient(app)
SYS_NAME = "ZZ Route System"
DEPT = "ZZ Route Dept"


@pytest.fixture(autouse=True)
def _clean():
    db.execute("DELETE FROM department_rounding WHERE department = %s", (DEPT,))
    db.execute("DELETE FROM rounding_systems WHERE name LIKE 'ZZ Route%'")
    rounding_system_store.reload()
    yield
    db.execute("DELETE FROM department_rounding WHERE department = %s", (DEPT,))
    db.execute("DELETE FROM rounding_systems WHERE name LIKE 'ZZ Route%'")
    rounding_system_store.reload()


def _sid():
    return next(s.id for s in rounding_system_store.all_systems() if s.name == SYS_NAME)


def test_add_then_save_windows_clamps():
    r = client.post("/settings/rounding_system/add", data={"name": SYS_NAME}, follow_redirects=False)
    assert r.status_code == 303
    rounding_system_store.reload()
    sid = _sid()
    r = client.post(
        "/settings/rounding_system",
        data={"system_id": str(sid), "in_before_min": "20", "in_after_min": "0",
              "out_before_min": "0", "out_after_min": "999"},  # clamps to 60
        headers={"accept": "application/json"},
    )
    assert r.status_code == 200
    rounding_system_store.reload()
    sysrec = next(s for s in rounding_system_store.all_systems() if s.id == sid)
    assert sysrec.rounding == RoundingSettings(20, 0, 0, 60)


def test_set_department_map():
    client.post("/settings/rounding_system/add", data={"name": SYS_NAME}, follow_redirects=False)
    rounding_system_store.reload()
    sid = _sid()
    rounding_system_store.save_system_windows(sid, RoundingSettings(20, 0, 0, 0))
    r = client.post(
        "/settings/department_rounding",
        data={"department": DEPT, "system_id": str(sid)},
        headers={"accept": "application/json"},
    )
    assert r.status_code == 200
    rounding_system_store.reload()
    assert rounding_system_store.windows_for_department(DEPT) == RoundingSettings(20, 0, 0, 0)


def test_set_department_map_to_plant_default():
    client.post("/settings/rounding_system/add", data={"name": SYS_NAME}, follow_redirects=False)
    rounding_system_store.reload()
    sid = _sid()
    rounding_system_store.set_department_system(DEPT, sid)
    r = client.post(
        "/settings/department_rounding",
        data={"department": DEPT, "system_id": "none"},
        headers={"accept": "application/json"},
    )
    assert r.status_code == 200
    rounding_system_store.reload()
    assert rounding_system_store.windows_for_department(DEPT) is None


def test_remove_system():
    client.post("/settings/rounding_system/add", data={"name": SYS_NAME}, follow_redirects=False)
    rounding_system_store.reload()
    sid = _sid()
    r = client.post("/settings/rounding_system/remove", data={"system_id": str(sid)}, follow_redirects=False)
    assert r.status_code == 303
    rounding_system_store.reload()
    assert all(s.id != sid for s in rounding_system_store.all_systems())


def test_save_bad_id_returns_400():
    r = client.post(
        "/settings/rounding_system",
        data={"system_id": "notanint", "in_before_min": "5"},
        headers={"accept": "application/json"},
    )
    assert r.status_code == 400


def test_settings_page_shows_systems_section():
    r = client.get("/settings?section=timeclock")
    assert r.status_code == 200
    assert "Rounding systems" in r.text
    assert "Department rounding" in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_settings_rounding_systems.py -v`
Expected: FAIL — add/save routes 404 (`assert 404 == 303/200`), and the page
test misses the "Rounding systems" string.

- [ ] **Step 3: Add GET context**

In `settings.py`, in the `settings_page` GET handler, right after the
`work_schedules_ctx = [...]` list comprehension (around line 318), add:

```python
    from .. import rounding_system_store
    _systems = rounding_system_store.all_systems()
    rounding_systems_ctx = [
        {
            "id": s.id,
            "name": s.name,
            "in_before_min": s.rounding.in_before_min,
            "in_after_min": s.rounding.in_after_min,
            "out_before_min": s.rounding.out_before_min,
            "out_after_min": s.rounding.out_after_min,
        }
        for s in _systems
    ]
    _dept_map = rounding_system_store.department_map()
    department_rounding_ctx = [
        {"department": d, "system_id": _dept_map.get(d)}
        for d in staffing.DEPARTMENT_ORDER
    ]
```

Then add these two keys to the `TemplateResponse(...)` context dict (alongside
`"rounding": rounding_ctx,`):

```python
            "rounding_systems": rounding_systems_ctx,
            "department_rounding": department_rounding_ctx,
```

- [ ] **Step 4: Add the POST routes**

In `settings.py`, immediately AFTER the `settings_save_rounding` function (ends
around line 491, before `_auto_lunch_mode_flags`), add:

```python
@router.post("/settings/rounding_system")
async def settings_save_rounding_system(request: Request):
    """Save the four windows for ONE rounding system (by id). Same 0..60 clamp
    as /settings/rounding."""
    from .. import rounding_system_store
    from ..rounding import RoundingSettings
    form = await request.form()

    def _clamp(raw) -> int:
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return 0
        return max(0, min(60, v))

    try:
        system_id = int(form.get("system_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    rounding_system_store.save_system_windows(system_id, RoundingSettings(
        in_before_min=_clamp(form.get("in_before_min")),
        in_after_min=_clamp(form.get("in_after_min")),
        out_before_min=_clamp(form.get("out_before_min")),
        out_after_min=_clamp(form.get("out_after_min")),
    ))
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/settings?saved=1&section=timeclock#rules", status_code=303)


@router.post("/settings/rounding_system/add")
async def settings_add_rounding_system(request: Request):
    """Create a new (all-zero) rounding system by name."""
    from .. import rounding_system_store
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "bad name"}, status_code=400)
    rounding_system_store.add_system(name)
    return RedirectResponse(url="/settings?saved=1&section=timeclock#rules", status_code=303)


@router.post("/settings/rounding_system/rename")
async def settings_rename_rounding_system(request: Request):
    """Rename one rounding system."""
    from .. import rounding_system_store
    form = await request.form()
    try:
        system_id = int(form.get("system_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    name = (form.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "bad name"}, status_code=400)
    rounding_system_store.rename_system(system_id, name)
    return RedirectResponse(url="/settings?saved=1&section=timeclock#rules", status_code=303)


@router.post("/settings/rounding_system/remove")
async def settings_remove_rounding_system(request: Request):
    """Delete a rounding system. Departments mapped to it revert to plant default."""
    from .. import rounding_system_store
    form = await request.form()
    try:
        system_id = int(form.get("system_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    rounding_system_store.delete_system(system_id)
    return RedirectResponse(url="/settings?saved=1&section=timeclock#rules", status_code=303)


@router.post("/settings/department_rounding")
async def settings_save_department_rounding(request: Request):
    """Map one static department to a rounding system, or to the plant default
    (system_id 'none'/blank)."""
    from .. import rounding_system_store
    form = await request.form()
    department = (form.get("department") or "").strip()
    if not department:
        return JSONResponse({"ok": False, "error": "bad department"}, status_code=400)
    raw = form.get("system_id")
    if raw in (None, "", "none", "0"):
        system_id = None
    else:
        try:
            system_id = int(raw)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    rounding_system_store.set_department_system(department, system_id)
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/settings?saved=1&section=timeclock#rules", status_code=303)
```

- [ ] **Step 5: Run test to verify route tests pass (page-section test still fails until Task 6)**

Run: `python -m pytest tests/test_settings_rounding_systems.py -v`
Expected: PASS for all route tests; `test_settings_page_shows_systems_section`
still FAILS (the template blocks come in Task 6). That's expected — leave it.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/settings.py tests/test_settings_rounding_systems.py
git commit -m "feat(settings): rounding-system + department-map context and routes"
```

---

### Task 6: Settings template — four-block rounding UI

**Files:**
- Modify: `src/zira_dashboard/templates/settings.html` (the rounding area in the `tc-tab-rules` panel)

- [ ] **Step 1: Replace the per-schedule-rounding block with three new blocks**

In `settings.html`, find the `<div class="per-schedule-rounding" ...>` block (it
starts just after the default rounding form's closing `</form>`, currently around
line 414, and ends at its matching `</div>` around line 475). REPLACE that entire
`<div class="per-schedule-rounding">...</div>` with the following three blocks:

```html
    <div class="rounding-systems" style="margin-top:1.6rem">
      <h4 class="rounding-subhead">Rounding systems</h4>
      <p class="help">
        Named sets of rounding windows. Each department below is assigned one
        system; editing a system changes rounding for every department using it.
      </p>

      {% for sys in rounding_systems %}
      <div class="rounding-card ws-rounding-card">
        <div class="ws-rounding-head">
          <strong>{{ sys.name }}</strong>
        </div>
        <form method="post" action="/settings/rounding_system" class="ws-rounding-fields">
          <input type="hidden" name="system_id" value="{{ sys.id }}">
          <div class="rounding-grid">
            <div class="rounding-col">
              <h4>IN</h4>
              <label>Up to
                <input type="number" name="in_before_min" min="0" max="60" value="{{ sys.in_before_min }}">
                minute(s) before the schedule clock-in time.</label>
              <label>Up to
                <input type="number" name="in_after_min" min="0" max="60" value="{{ sys.in_after_min }}">
                minute(s) after the schedule clock-in time.</label>
            </div>
            <div class="rounding-col">
              <h4>OUT</h4>
              <label>Up to
                <input type="number" name="out_before_min" min="0" max="60" value="{{ sys.out_before_min }}">
                minute(s) before the schedule clock-out time.</label>
              <label>Up to
                <input type="number" name="out_after_min" min="0" max="60" value="{{ sys.out_after_min }}">
                minute(s) after the schedule clock-out time.</label>
            </div>
          </div>
          <button type="submit">Save</button>
        </form>
        <form method="post" action="/settings/rounding_system/rename" style="margin-top:0.4rem">
          <input type="hidden" name="system_id" value="{{ sys.id }}">
          <label>Rename:
            <input type="text" name="name" value="{{ sys.name }}" maxlength="80">
          </label>
          <button type="submit">Rename</button>
        </form>
        <form method="post" action="/settings/rounding_system/remove" style="margin-top:0.4rem">
          <input type="hidden" name="system_id" value="{{ sys.id }}">
          <button type="submit"
                  onclick="return confirm('Remove this system? Departments using it revert to the plant default rounding.')">
            Remove
          </button>
        </form>
      </div>
      {% else %}
      <p class="note">No rounding systems yet.</p>
      {% endfor %}

      <form method="post" action="/settings/rounding_system/add" style="margin-top:0.8rem">
        <label>Add a system:
          <input type="text" name="name" placeholder="e.g. Plant Operator" maxlength="80">
        </label>
        <button type="submit">Add</button>
      </form>
    </div>

    <div class="department-rounding" style="margin-top:1.6rem">
      <h4 class="rounding-subhead">Department rounding</h4>
      <p class="help">
        Which rounding system each department uses. An employee's punches use the
        system of the department they work that day (from the scheduler; falling
        back to where they clock in). &ldquo;Plant default&rdquo; uses the default
        windows at the top.
      </p>
      {% for row in department_rounding %}
      <form method="post" action="/settings/department_rounding"
            style="display:flex; align-items:center; gap:.6rem; margin:.35rem 0">
        <input type="hidden" name="department" value="{{ row.department }}">
        <strong style="min-width:8rem">{{ row.department }}</strong>
        <select name="system_id" onchange="this.form.submit()">
          <option value="none" {% if not row.system_id %}selected{% endif %}>Plant default</option>
          {% for sys in rounding_systems %}
          <option value="{{ sys.id }}" {% if row.system_id == sys.id %}selected{% endif %}>{{ sys.name }}</option>
          {% endfor %}
        </select>
        <noscript><button type="submit">Save</button></noscript>
      </form>
      {% endfor %}
    </div>

    <div class="per-schedule-rounding" style="margin-top:1.6rem">
      <h4 class="rounding-subhead">Custom shift hours</h4>
      <p class="help">
        Odoo work schedules whose hours differ from the plant default (e.g.
        drivers at 5:45&ndash;2:30). The hours come from Odoo and set the boundary
        rounding pulls toward. Rounding windows are set by department above, not here.
      </p>

      {% for ws in work_schedules %}
      <div class="rounding-card ws-rounding-card">
        <div class="ws-rounding-head">
          <strong>{{ ws.name }}</strong>
          <span class="note">{{ ws.hours_display }} &middot; from Odoo</span>
        </div>
        <form method="post" action="/settings/work_schedule_rounding/remove" style="margin-top:0.4rem">
          <input type="hidden" name="resource_calendar_id" value="{{ ws.resource_calendar_id }}">
          <button type="submit"
                  onclick="return confirm('Remove this schedule? Its employees use the plant-default hours.')">
            Remove
          </button>
        </form>
      </div>
      {% else %}
      <p class="note">No custom shift hours yet.</p>
      {% endfor %}

      {% if available_schedules %}
      <form method="post" action="/settings/work_schedule_rounding/add" style="margin-top:0.8rem">
        <label>Add a schedule:
          <select name="resource_calendar_id">
            {% for s in available_schedules %}
            <option value="{{ s.id }}">{{ s.name }}</option>
            {% endfor %}
          </select>
        </label>
        <button type="submit">Add</button>
      </form>
      {% endif %}
    </div>
```

- [ ] **Step 2: Run the page-section test to verify it now passes**

Run: `python -m pytest tests/test_settings_rounding_systems.py::test_settings_page_shows_systems_section -v`
Expected: PASS (the page now contains "Rounding systems" and "Department rounding").

- [ ] **Step 3: Sanity-check the template renders (no Jinja errors)**

Run the full settings route test module:
Run: `python -m pytest tests/test_settings_rounding_systems.py tests/test_settings_work_schedule_rounding.py -v`
Expected: PASS (existing work-schedule tests still pass — their add/remove routes
are unchanged; the page renders without the removed rounding inputs).

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/templates/settings.html
git commit -m "feat(settings): four-block rounding UI (systems + dept map + custom hours)"
```

---

### Task 7: Verify whole suite, lint, and changelog

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run the full affected test set**

Run:
```bash
python -m pytest tests/test_rounding_systems_schema.py tests/test_staffing_department_for_wc.py tests/test_rounding_system_store.py tests/test_hours_for_punch.py tests/test_rounding_windows_for_day.py tests/test_settings_rounding_systems.py tests/test_rounding.py tests/test_rounding_store.py tests/test_work_schedule_store.py -v
```
Expected: PASS (Postgres-backed ones require `DATABASE_URL`; pure ones always run).

- [ ] **Step 2: Lint + compile**

Run:
```bash
ruff check src/zira_dashboard/rounding_system_store.py src/zira_dashboard/routes/timeclock.py src/zira_dashboard/routes/settings.py src/zira_dashboard/staffing.py src/zira_dashboard/_schema.py
python -m py_compile src/zira_dashboard/rounding_system_store.py src/zira_dashboard/routes/timeclock.py src/zira_dashboard/routes/settings.py src/zira_dashboard/staffing.py
```
Expected: no errors.

- [ ] **Step 3: Add a CHANGELOG entry**

Per the per-deploy changelog rule, add a new `### <TIME>` entry under today's
date (`2026-06-04`) in `CHANGELOG.md`, e.g.:

```markdown
### <HH:MM>
- **Department-driven punch rounding.** Timeclock rounding windows now follow
  the department of the work center an employee works that day (scheduled WC,
  falling back to where they clock in), via named rounding systems (Plant
  Operator / Supervisor / Transportation) mapped per department in Settings →
  Timeclock → Rules. Shift hours still come from Odoo; the plant default remains
  the fallback. No historical punches changed.
```

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): department-driven punch rounding"
```

---

## Self-review notes (for the implementer)

- **Spec coverage:** Tables + seed (Task 1); static department key (Task 2);
  cached store + fallback (Task 3); hours/windows split + scheduled→clock-in→default
  resolution + clock-out current-WC + multi-dept tiebreak (Task 4); settings
  context + routes (Task 5); four-block UI incl. reframed custom-hours (Task 6);
  changelog (Task 7). `apply_rounding` is never edited; `_hours_for_punch`
  preserves the prior hours behavior.
- **Vestigial-but-intentional:** `work_schedules` rounding columns and the
  `/settings/work_schedule_rounding` save route remain (hours-only UI), so the
  existing `test_settings_work_schedule_rounding.py` / `test_work_schedule_store.py`
  stay green.
- **Type consistency:** `RoundingSettings` everywhere for windows;
  `windows_for_department` returns `RoundingSettings | None`; resolvers return
  `(time, time)` for hours and `RoundingSettings` for windows.
- **If `tests/test_rounding.py` references `_shift_for_punch`** (it shouldn't — it
  tests pure `apply_rounding`), update it; the `git grep` in Task 4 Step 6 will
  catch any stragglers.
