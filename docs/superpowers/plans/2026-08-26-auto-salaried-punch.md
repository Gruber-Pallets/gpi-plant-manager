# Auto Salaried Punch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A background worker that writes four daily `hr.attendance` punches (6:00 in / 11:00 out / 11:30 in / 15:30 out, America/Chicago) for every active fixed-wage (`wage_type='monthly'`) employee, skipping weekends/holidays/approved leave, defaulting to the "Sustaining" department, returning to the pre-lunch department after lunch, and self-cleaning when approved leave arrives after punches exist.

**Architecture:** New module `auto_salaried.py` modeled on `auto_lunch.py` (pure decision core + `run_tick()` I/O wrapper, driven by a 60s warmer). Punches flow through the existing `timeclock_punches_log` → `timeclock_sync` → Odoo pipeline with `source='auto_salaried'`. A per-person/day scoreboard table (`auto_salaried_runs`) guarantees each punch slot fires at most once; a slower reconcile warmer deletes robot punches on late-approved-leave days or flags messy days into `auto_salaried_flags`, surfaced on an admin page.

**Tech Stack:** Python 3.11, FastAPI, psycopg2/Postgres (`db.query`/`db.execute`/`db.cursor`), Odoo XML-RPC via `odoo_client.execute`, Jinja2 templates, pytest.

**Spec:** `docs/superpowers/specs/2026-08-26-auto-salaried-punch-design.md`

## Global Constraints

- Punch times are exact plant-local times (`shift_config.SITE_TZ` = America/Chicago): 06:00 in, 11:00 out, 11:30 in, 15:30 out. `rounded_at = occurred_at` (no rounding).
- Feature is env-gated: `AUTO_SALARIED_DRY_RUN=1` → log-only simulation; `AUTO_SALARIED_ENABLED=1` → live; neither → completely off. Dry-run takes precedence.
- Punch log rows use `source='auto_salaried'`. The reconciler may only delete Odoo attendances whose ids came from rows with that source.
- Never touch hourly employees or `auto_lunch` behavior. Salaried = `people.wage_type = 'monthly'` AND `active = TRUE` AND `odoo_id IS NOT NULL`.
- Approved leave = `time_off_requests.state = 'validate'` (not `validate1`, not `confirm`).
- One person's failure never kills a tick (`try/except` per person, mirroring `auto_lunch.run_tick`).
- Tests requiring Postgres must carry `pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")` and clean up their rows (copy `tests/test_auto_lunch_worker.py` hygiene).
- Run tests with `pytest tests/<file> -v` from the repo root.

---

### Task 1: Scoreboard + flags schema

**Files:**
- Modify: `src/zira_dashboard/_schema.py` (append to `SCHEMA_DDL`, near the `auto_lunch_runs` definition — search for `CREATE TABLE IF NOT EXISTS auto_lunch_runs`)
- Test: `tests/test_auto_salaried_schema_static.py`

**Interfaces:**
- Produces: tables `auto_salaried_runs` (PK `(person_odoo_id, day)`; slot columns `morning_in_punch_id`, `lunch_out_punch_id`, `lunch_in_punch_id`, `day_out_punch_id`; `skipped`, `skip_reason`, `lunch_dept_id`, `lunch_dept_name`, `dept_patch_state`, `reverted`, `flagged`) and `auto_salaried_flags` (`id`, `person_odoo_id`, `day`, `reason`, `details`, `created_at`, `resolved_at`, unique on `(person_odoo_id, day, reason)`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auto_salaried_schema_static.py
from zira_dashboard._schema import SCHEMA_DDL


def test_auto_salaried_runs_schema():
    assert "CREATE TABLE IF NOT EXISTS auto_salaried_runs" in SCHEMA_DDL
    for column in (
        "morning_in_punch_id", "lunch_out_punch_id", "lunch_in_punch_id",
        "day_out_punch_id", "skipped", "skip_reason", "lunch_dept_id",
        "lunch_dept_name", "dept_patch_state", "reverted", "flagged",
    ):
        assert column in SCHEMA_DDL
    assert "CHECK (dept_patch_state IN ('none','pending','done','failed'))" in SCHEMA_DDL


def test_auto_salaried_flags_schema():
    assert "CREATE TABLE IF NOT EXISTS auto_salaried_flags" in SCHEMA_DDL
    assert "UNIQUE (person_odoo_id, day, reason)" in SCHEMA_DDL
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auto_salaried_schema_static.py -v`
Expected: FAIL (assertions on missing DDL)

- [ ] **Step 3: Append the DDL to `SCHEMA_DDL` in `_schema.py`**

Insert directly after the `auto_lunch_runs` table block (keep neighboring auto-punch machinery together):

```sql
-- Auto salaried punch scoreboard: one row per fixed-wage person per plant day.
-- Each *_punch_id column is the timeclock_punches_log id of that slot's punch
-- (0 = simulated punch written in dry-run mode; NULL = not yet punched).
-- See docs/superpowers/specs/2026-08-26-auto-salaried-punch-design.md.
CREATE TABLE IF NOT EXISTS auto_salaried_runs (
  person_odoo_id       INTEGER NOT NULL,
  day                  DATE NOT NULL,
  skipped              BOOLEAN NOT NULL DEFAULT FALSE,
  skip_reason          TEXT,
  morning_in_punch_id  BIGINT,
  lunch_out_punch_id   BIGINT,
  lunch_in_punch_id    BIGINT,
  day_out_punch_id     BIGINT,
  lunch_dept_id        INTEGER,
  lunch_dept_name      TEXT,
  dept_patch_state     TEXT NOT NULL DEFAULT 'none'
                       CHECK (dept_patch_state IN ('none','pending','done','failed')),
  reverted             BOOLEAN NOT NULL DEFAULT FALSE,
  flagged              BOOLEAN NOT NULL DEFAULT FALSE,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (person_odoo_id, day)
);
CREATE INDEX IF NOT EXISTS idx_auto_salaried_runs_day ON auto_salaried_runs (day);

-- Days the auto-salaried robot could not handle safely ("needs a human").
CREATE TABLE IF NOT EXISTS auto_salaried_flags (
  id              BIGSERIAL PRIMARY KEY,
  person_odoo_id  INTEGER NOT NULL,
  day             DATE NOT NULL,
  reason          TEXT NOT NULL,
  details         TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at     TIMESTAMPTZ,
  UNIQUE (person_odoo_id, day, reason)
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auto_salaried_schema_static.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/_schema.py tests/test_auto_salaried_schema_static.py
git commit -m "feat: auto_salaried_runs and auto_salaried_flags schema"
```

---

### Task 2: Odoo client additions (virtual Sustaining department, dept patch, unlink)

The punch pipeline derives an Odoo department from `wc_name` via `staffing.LOCATIONS`, which has no "Sustaining" entry. Add a virtual-WC fallback so `wc_name="Sustaining"` resolves to the Odoo department named like "Sustaining" without polluting the staffing UI. Also add the two write helpers the robot needs.

**Files:**
- Modify: `src/zira_dashboard/odoo_client.py` (`_department_id_for_wc` at ~line 381; new functions after `clear_attendance_wc` at ~line 504)
- Test: `tests/test_auto_salaried_odoo_client.py`

**Interfaces:**
- Produces:
  - `_VIRTUAL_WC_DEPARTMENTS: dict[str, str]` — `{"sustaining": "Sustaining"}`, keyed lowercase.
  - `_department_id_for_wc(wc_name)` now falls back to `_VIRTUAL_WC_DEPARTMENTS` when the WC isn't in `staffing.LOCATIONS`.
  - `set_attendance_department(attendance_id: int, dept_id: int) -> bool` — writes the configured `ODOO_KIOSK_DEPARTMENT_FIELD`; returns False when the field is unset or dept_id falsy.
  - `delete_attendances(attendance_ids: list[int]) -> None` — `hr.attendance` unlink; no-op on empty list.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_auto_salaried_odoo_client.py
"""Virtual Sustaining department + dept patch + unlink helpers.

Monkeypatch odoo_client.execute (the universal XML-RPC entry point) — the
same fake-Odoo seam every other odoo_client test uses.
"""
from zira_dashboard import odoo_client


def _fresh_cache(monkeypatch):
    monkeypatch.setattr(odoo_client, "_wc_dept_id_cache", {})


def test_department_id_for_virtual_sustaining_wc(monkeypatch):
    _fresh_cache(monkeypatch)
    calls = []

    def fake_execute(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        return [{"id": 77}]

    monkeypatch.setattr(odoo_client, "execute", fake_execute)
    assert odoo_client._department_id_for_wc("Sustaining") == 77
    model, method, args, kwargs = calls[0]
    assert model == "hr.department"
    assert args[0] == ("name", "ilike", "Sustaining")


def test_department_id_unknown_wc_still_none(monkeypatch):
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(
        odoo_client, "execute",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Odoo call expected")),
    )
    assert odoo_client._department_id_for_wc("Not A Real WC") is None


def test_set_attendance_department(monkeypatch):
    calls = []
    monkeypatch.setenv("ODOO_KIOSK_DEPARTMENT_FIELD", "x_kiosk_department_id")
    monkeypatch.setattr(
        odoo_client, "execute",
        lambda model, method, ids, payload: calls.append((model, method, ids, payload)) or True,
    )
    assert odoo_client.set_attendance_department(123, 77) is True
    assert calls == [("hr.attendance", "write", [123], {"x_kiosk_department_id": 77})]


def test_set_attendance_department_field_unset(monkeypatch):
    monkeypatch.delenv("ODOO_KIOSK_DEPARTMENT_FIELD", raising=False)
    monkeypatch.setattr(
        odoo_client, "execute",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Odoo call expected")),
    )
    assert odoo_client.set_attendance_department(123, 77) is False


def test_delete_attendances(monkeypatch):
    calls = []
    monkeypatch.setattr(
        odoo_client, "execute",
        lambda model, method, ids: calls.append((model, method, ids)) or True,
    )
    odoo_client.delete_attendances([5, 9])
    odoo_client.delete_attendances([])  # no-op, no extra call
    assert calls == [("hr.attendance", "unlink", [5, 9])]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_auto_salaried_odoo_client.py -v`
Expected: FAIL — `test_department_id_for_virtual_sustaining_wc` gets None; `set_attendance_department`/`delete_attendances` are AttributeError.

- [ ] **Step 3: Implement in `odoo_client.py`**

Above `_department_id_for_wc` (next to `_wc_dept_id_cache`):

```python
# Virtual work centers: names the auto-salaried punch worker uses that are
# NOT real staffing locations. They exist only to route a department to
# _department_id_for_wc (and through _attendance_create_payload) without
# adding fake rows to staffing.LOCATIONS (which drives the scheduler UI).
# Keys are lowercase wc_name; values are the Odoo hr.department name to
# ilike-match (so "Sustaining" matches e.g. "05 Sustaining").
_VIRTUAL_WC_DEPARTMENTS: dict[str, str] = {"sustaining": "Sustaining"}
```

Inside `_department_id_for_wc`, after the `staffing.LOCATIONS` loop and before the `if not dept_name:` early-return:

```python
    if not dept_name:
        dept_name = _VIRTUAL_WC_DEPARTMENTS.get(wc_name.strip().lower())
```

(Keep the existing `if not dept_name: _wc_dept_id_cache[wc_name] = None; return None` after it.)

After `clear_attendance_wc`:

```python
def set_attendance_department(attendance_id: int, dept_id: int) -> bool:
    """Write a department directly onto an hr.attendance (auto-salaried
    lunch-return patch: the pipeline derives departments from WC names, but
    the pre-lunch department read off Odoo has no app WC to route through).
    Returns False when the department field isn't configured."""
    dept_field = _kiosk_department_field()
    if not dept_field or not dept_id:
        return False
    return bool(execute("hr.attendance", "write", [attendance_id], {dept_field: dept_id}))


def delete_attendances(attendance_ids: list[int]) -> None:
    """Unlink hr.attendance records. Only the auto-salaried reconciler calls
    this, and only with ids of records the robot itself created."""
    if not attendance_ids:
        return
    execute("hr.attendance", "unlink", list(attendance_ids))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_auto_salaried_odoo_client.py -v`
Expected: PASS (5 tests). Also run `pytest tests/test_client.py -v` to confirm no regression in existing odoo_client tests.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/odoo_client.py tests/test_auto_salaried_odoo_client.py
git commit -m "feat: virtual Sustaining department + attendance dept-patch/unlink helpers"
```

---

### Task 3: Decision core (pure logic)

**Files:**
- Create: `src/zira_dashboard/auto_salaried.py`
- Test: `tests/test_auto_salaried_decide.py`

**Interfaces:**
- Produces (all pure, no I/O):
  - `SUSTAINING_WC = "Sustaining"`, `SLOT_ORDER: tuple[str, ...]`, `SLOT_ACTION: dict[str, str]`, `PUNCH_TIMES: dict[str, time]`, `SIMULATED_PUNCH_ID = 0`, `RECONCILE_LOOKBACK_DAYS = 7`
  - `mode() -> str` — `'dry_run' | 'live' | 'off'` from env (`AUTO_SALARIED_DRY_RUN` wins over `AUTO_SALARIED_ENABLED`).
  - `scheduled_at(day: date, slot: str) -> datetime` — plant-local (tz-aware) scheduled time.
  - `skip_reason(day: date, *, is_company_holiday: bool, has_approved_leave: bool) -> str | None` — `'weekend' | 'holiday' | 'approved_leave' | None`.
  - `due_slots(now: datetime, day: date, run: dict | None) -> list[str]` — slots whose time has arrived and whose `<slot>_punch_id` is None, in `SLOT_ORDER`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_auto_salaried_decide.py
"""Pure decision core: no DB, no Odoo, no env beyond mode()."""
from datetime import date, datetime, time

from zira_dashboard import auto_salaried as asal
from zira_dashboard import shift_config

TUE = date(2026, 9, 1)   # a Tuesday
SAT = date(2026, 9, 5)   # a Saturday


def _at(day, hh, mm):
    return datetime.combine(day, time(hh, mm), tzinfo=shift_config.SITE_TZ)


def test_punch_times_and_order():
    assert asal.SLOT_ORDER == ("morning_in", "lunch_out", "lunch_in", "day_out")
    assert asal.PUNCH_TIMES["morning_in"] == time(6, 0)
    assert asal.PUNCH_TIMES["lunch_out"] == time(11, 0)
    assert asal.PUNCH_TIMES["lunch_in"] == time(11, 30)
    assert asal.PUNCH_TIMES["day_out"] == time(15, 30)
    assert asal.SLOT_ACTION == {
        "morning_in": "clock_in", "lunch_out": "clock_out",
        "lunch_in": "clock_in", "day_out": "clock_out",
    }


def test_scheduled_at_is_plant_local():
    dt = asal.scheduled_at(TUE, "morning_in")
    assert dt == _at(TUE, 6, 0)
    assert dt.tzinfo is shift_config.SITE_TZ


def test_scheduled_at_survives_dst_days():
    # 2026-03-08 and 2026-11-01 are US DST transitions; punch stays 6:00 local.
    for day in (date(2026, 3, 9), date(2026, 11, 2)):  # Mondays after transitions
        assert asal.scheduled_at(day, "morning_in").time() == time(6, 0)


def test_skip_reasons():
    assert asal.skip_reason(SAT, is_company_holiday=False, has_approved_leave=False) == "weekend"
    assert asal.skip_reason(TUE, is_company_holiday=True, has_approved_leave=False) == "holiday"
    assert asal.skip_reason(TUE, is_company_holiday=False, has_approved_leave=True) == "approved_leave"
    assert asal.skip_reason(TUE, is_company_holiday=False, has_approved_leave=False) is None


def test_due_slots_progression():
    assert asal.due_slots(_at(TUE, 5, 59), TUE, None) == []
    assert asal.due_slots(_at(TUE, 6, 0), TUE, None) == ["morning_in"]
    run = {"morning_in_punch_id": 11}
    assert asal.due_slots(_at(TUE, 10, 59), TUE, run) == []
    assert asal.due_slots(_at(TUE, 11, 0), TUE, run) == ["lunch_out"]
    run = {"morning_in_punch_id": 11, "lunch_out_punch_id": 12, "lunch_in_punch_id": 13}
    assert asal.due_slots(_at(TUE, 15, 30), TUE, run) == ["day_out"]


def test_due_slots_catches_up_after_downtime():
    # App down 5:50-12:10: everything through lunch_in is due at once, in order.
    assert asal.due_slots(_at(TUE, 12, 10), TUE, None) == [
        "morning_in", "lunch_out", "lunch_in"]


def test_due_slots_simulated_id_counts_as_done():
    run = {"morning_in_punch_id": asal.SIMULATED_PUNCH_ID}
    assert asal.due_slots(_at(TUE, 6, 5), TUE, run) == []


def test_mode(monkeypatch):
    monkeypatch.delenv("AUTO_SALARIED_DRY_RUN", raising=False)
    monkeypatch.delenv("AUTO_SALARIED_ENABLED", raising=False)
    assert asal.mode() == "off"
    monkeypatch.setenv("AUTO_SALARIED_ENABLED", "1")
    assert asal.mode() == "live"
    monkeypatch.setenv("AUTO_SALARIED_DRY_RUN", "1")
    assert asal.mode() == "dry_run"  # dry-run wins
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_auto_salaried_decide.py -v`
Expected: FAIL with `ModuleNotFoundError` / import error for `auto_salaried`.

- [ ] **Step 3: Create `src/zira_dashboard/auto_salaried.py` with the pure core**

```python
"""Auto salaried punch worker: writes four hr.attendance punches per weekday
for every active fixed-wage (wage_type='monthly') employee, mimicking an
hourly employee's two-block day. Skips weekends, company holidays, and days
with approved leave; a reconciler cleans up when leave is approved after
punches exist.

The decision logic (due_slots / skip_reason / scheduled_at) is pure and
unit-testable. run_tick() / run_reconcile() wire the I/O around it.

See docs/superpowers/specs/2026-08-26-auto-salaried-punch-design.md.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta

from . import company_holidays, db, shift_config, timeclock_sync

_log = logging.getLogger(__name__)

SUSTAINING_WC = "Sustaining"

PUNCH_TIMES: dict[str, time] = {
    "morning_in": time(6, 0),
    "lunch_out": time(11, 0),
    "lunch_in": time(11, 30),
    "day_out": time(15, 30),
}
SLOT_ORDER: tuple[str, ...] = ("morning_in", "lunch_out", "lunch_in", "day_out")
SLOT_ACTION: dict[str, str] = {
    "morning_in": "clock_in",
    "lunch_out": "clock_out",
    "lunch_in": "clock_in",
    "day_out": "clock_out",
}

# Dry-run mode marks a slot done with this sentinel instead of a real
# timeclock_punches_log id, so simulation advances the scoreboard (no log
# spam every 60s) without writing punches.
SIMULATED_PUNCH_ID = 0

RECONCILE_LOOKBACK_DAYS = 7


def mode() -> str:
    """'dry_run' | 'live' | 'off'. Dry-run wins so a deploy with both vars
    set can never write real punches by accident."""
    if os.environ.get("AUTO_SALARIED_DRY_RUN") == "1":
        return "dry_run"
    if os.environ.get("AUTO_SALARIED_ENABLED") == "1":
        return "live"
    return "off"


def scheduled_at(day: date, slot: str) -> datetime:
    return datetime.combine(day, PUNCH_TIMES[slot], tzinfo=shift_config.SITE_TZ)


def skip_reason(day: date, *, is_company_holiday: bool,
                has_approved_leave: bool) -> str | None:
    if day.weekday() >= 5:
        return "weekend"
    if is_company_holiday:
        return "holiday"
    if has_approved_leave:
        return "approved_leave"
    return None


def due_slots(now: datetime, day: date, run: dict | None) -> list[str]:
    """Slots whose scheduled time has arrived and that haven't punched yet,
    in punch order. Catch-up after downtime falls out naturally: every
    overdue slot is returned at once, each backdated to its scheduled time
    by the caller."""
    done = run or {}
    return [
        s for s in SLOT_ORDER
        if done.get(f"{s}_punch_id") is None and now >= scheduled_at(day, s)
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_auto_salaried_decide.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/auto_salaried.py tests/test_auto_salaried_decide.py
git commit -m "feat: auto-salaried punch decision core"
```

---

### Task 4: Worker tick I/O (`run_tick`)

**Files:**
- Modify: `src/zira_dashboard/auto_salaried.py` (append below the pure core)
- Test: `tests/test_auto_salaried_worker.py`

**Interfaces:**
- Consumes: Task 3's pure core; `odoo_client.get_current_attendance(pid)` (returns dict with `department_id`/`department_name` when `ODOO_KIOSK_DEPARTMENT_FIELD` is set); `odoo_client.set_attendance_department(att_id, dept_id)`; `timeclock_sync.sync_one_by_id(log_id)`; `company_holidays.for_day(day)` / `company_holidays.has_synced()`.
- Produces: `run_tick(now: datetime | None = None) -> None` — safe every 60s, no-op when `mode() == 'off'`.

Behavior recap (from spec): enrollment happens at first touch of the day; if the person already has non-robot punches today (mid-day promotion, manual Odoo punch), skip the day with reason `other_punches` and flag it. Lunch-out captures the open record's department from Odoo (fallback: flag + patch none). Lunch-in punches with `wc_name='Sustaining'`; a later tick patches the department onto the synced Odoo record when the pre-lunch department wasn't Sustaining.

- [ ] **Step 1: Append the I/O section to `auto_salaried.py`**

```python
# ---------- I/O ----------

def _fixed_wage_ids() -> list[int]:
    rows = db.query(
        "SELECT odoo_id FROM people "
        "WHERE active = TRUE AND wage_type = 'monthly' AND odoo_id IS NOT NULL "
        "ORDER BY odoo_id"
    )
    return [int(r["odoo_id"]) for r in rows]


def _approved_leave_ids(day: date, person_ids: list[int]) -> set[int]:
    """People with approved (state='validate') leave overlapping `day`."""
    if not person_ids:
        return set()
    rows = db.query(
        "SELECT DISTINCT person_odoo_id FROM time_off_requests "
        "WHERE state = 'validate' AND date_from <= %s AND date_to >= %s "
        "AND person_odoo_id = ANY(%s)",
        (day, day, person_ids),
    )
    return {int(r["person_odoo_id"]) for r in rows}


def _get_runs_bulk(day: date, person_ids: list[int]) -> dict[int, dict]:
    if not person_ids:
        return {}
    rows = db.query(
        "SELECT * FROM auto_salaried_runs WHERE day = %s AND person_odoo_id = ANY(%s)",
        (day, person_ids),
    )
    return {int(r["person_odoo_id"]): r for r in rows}


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=shift_config.SITE_TZ)
    return start, start + timedelta(days=1)


def _foreign_punches_today(person_odoo_id: int, day: date) -> bool:
    """True when the person already has punches today NOT created by this
    worker — a manual Odoo/kiosk punch or a same-day promotion from hourly.
    Enrolling anyway would double their morning."""
    start, end = _day_bounds(day)
    rows = db.query(
        "SELECT 1 FROM timeclock_punches_log WHERE person_odoo_id = %s "
        "AND COALESCE(source, '') <> 'auto_salaried' "
        "AND COALESCE(rounded_at, occurred_at) >= %s "
        "AND COALESCE(rounded_at, occurred_at) < %s LIMIT 1",
        (person_odoo_id, start, end),
    )
    return bool(rows)


def _flag(person_odoo_id: int, day: date, reason: str, details: str, *, cur=None) -> None:
    sql = (
        "INSERT INTO auto_salaried_flags (person_odoo_id, day, reason, details) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (person_odoo_id, day, reason) DO NOTHING"
    )
    params = (person_odoo_id, day, reason, details[:500])
    if cur is not None:
        cur.execute(sql, params)
    else:
        db.execute(sql, params)


def _insert_skipped_run(person_odoo_id: int, day: date, reason: str) -> None:
    db.execute(
        "INSERT INTO auto_salaried_runs (person_odoo_id, day, skipped, skip_reason) "
        "VALUES (%s, %s, TRUE, %s) ON CONFLICT (person_odoo_id, day) DO NOTHING",
        (person_odoo_id, day, reason),
    )


def _ensure_run(person_odoo_id: int, day: date) -> None:
    db.execute(
        "INSERT INTO auto_salaried_runs (person_odoo_id, day) VALUES (%s, %s) "
        "ON CONFLICT (person_odoo_id, day) DO NOTHING",
        (person_odoo_id, day),
    )


def _record_punch(person_odoo_id: int, day: date, slot: str, punch_id: int, *, cur=None) -> None:
    if slot not in SLOT_ORDER:  # guards the f-string column name
        raise ValueError(f"unknown slot {slot!r}")
    sql = (
        f"UPDATE auto_salaried_runs SET {slot}_punch_id = %s, updated_at = now() "
        f"WHERE person_odoo_id = %s AND day = %s AND {slot}_punch_id IS NULL"
    )
    params = (punch_id, person_odoo_id, day)
    if cur is not None:
        cur.execute(sql, params)
    else:
        db.execute(sql, params)


def _write_auto_punch(person_odoo_id: int, action: str, wc_name: str | None,
                      occurred_at: datetime, *, cur) -> int:
    """Insert an auto-salaried punch stamped at the scheduled time. Caller's
    open cursor makes the punch + scoreboard update one transaction (same
    crash-safety contract as auto_lunch._write_auto_punch)."""
    cur.execute(
        "INSERT INTO timeclock_punches_log "
        "(person_odoo_id, action, wc_name, occurred_at, rounded_at, source) "
        "VALUES (%s, %s, %s, %s, %s, 'auto_salaried') RETURNING id",
        (person_odoo_id, action, wc_name, occurred_at, occurred_at),
    )
    return cur.fetchone()["id"]


def _capture_lunch_department(person_odoo_id: int, day: date, *, cur) -> None:
    """At lunch-out, read the department off the person's OPEN Odoo record —
    Odoo is the referee because outside apps transfer salaried people without
    telling this app's log. Unreadable → default Sustaining after lunch and
    flag the day (spec: missing 30 minutes is worse than a wrong department)."""
    dept_id = dept_name = None
    try:
        from . import odoo_client
        current = odoo_client.get_current_attendance(person_odoo_id)
        if current:
            dept_id = current.get("department_id")
            dept_name = current.get("department_name")
    except Exception as e:  # noqa: BLE001 — flag and carry on
        _log.warning("auto-salaried: dept read failed for person %s: %s",
                     person_odoo_id, e)
    if dept_id is None:
        _flag(person_odoo_id, day, "lunch_dept_unread",
              "Could not read pre-lunch department from Odoo; "
              "lunch return defaulted to Sustaining.", cur=cur)
        patch_state = "none"
    elif "sustaining" in (dept_name or "").strip().lower():
        patch_state = "none"  # pipeline already writes Sustaining
    else:
        patch_state = "pending"
    cur.execute(
        "UPDATE auto_salaried_runs SET lunch_dept_id = %s, lunch_dept_name = %s, "
        "dept_patch_state = %s, updated_at = now() "
        "WHERE person_odoo_id = %s AND day = %s",
        (dept_id, dept_name, patch_state, person_odoo_id, day),
    )


def _patch_departments() -> None:
    """Write the remembered pre-lunch department onto lunch-in attendances
    once the sync has landed them in Odoo (we only learn the Odoo id then)."""
    rows = db.query(
        "SELECT r.person_odoo_id, r.day, r.lunch_dept_id, l.odoo_attendance_id "
        "FROM auto_salaried_runs r "
        "JOIN timeclock_punches_log l ON l.id = r.lunch_in_punch_id "
        "WHERE r.dept_patch_state = 'pending' AND l.synced_to_odoo = TRUE "
        "AND l.odoo_attendance_id IS NOT NULL"
    )
    for r in rows:
        try:
            from . import odoo_client
            ok = odoo_client.set_attendance_department(
                int(r["odoo_attendance_id"]), int(r["lunch_dept_id"]))
        except Exception as e:  # noqa: BLE001 — retry next tick
            _log.warning("auto-salaried: dept patch failed for person %s %s: %s",
                         r["person_odoo_id"], r["day"], e)
            continue
        db.execute(
            "UPDATE auto_salaried_runs SET dept_patch_state = %s, updated_at = now() "
            "WHERE person_odoo_id = %s AND day = %s",
            ("done" if ok else "failed", r["person_odoo_id"], r["day"]),
        )


def _advance_person(person_odoo_id: int, day: date, now: datetime,
                    run: dict | None, is_holiday: bool, has_leave: bool,
                    worker_mode: str) -> None:
    if run is not None and (run.get("skipped") or run.get("reverted")):
        return
    if run is None:
        reason = skip_reason(day, is_company_holiday=is_holiday,
                             has_approved_leave=has_leave)
        if reason is None and _foreign_punches_today(person_odoo_id, day):
            reason = "other_punches"
            _flag(person_odoo_id, day, "other_punches",
                  "Person already had non-robot punches at enrollment time "
                  "(manual punch or same-day wage-type change); day skipped.")
        if reason:
            _insert_skipped_run(person_odoo_id, day, reason)
            return
        _ensure_run(person_odoo_id, day)
        run = {}
    for slot in due_slots(now, day, run):
        at = scheduled_at(day, slot)
        action = SLOT_ACTION[slot]
        wc_name = SUSTAINING_WC if action == "clock_in" else None
        if worker_mode == "dry_run":
            _log.info("auto-salaried DRY-RUN: person %s %s (%s) @ %s",
                      person_odoo_id, action, slot, at)
            _record_punch(person_odoo_id, day, slot, SIMULATED_PUNCH_ID)
            run = dict(run, **{f"{slot}_punch_id": SIMULATED_PUNCH_ID})
            continue
        _log.info("auto-salaried LIVE: person %s %s (%s) @ %s",
                  person_odoo_id, action, slot, at)
        with db.cursor() as cur:
            if slot == "lunch_out":
                _capture_lunch_department(person_odoo_id, day, cur=cur)
            punch_id = _write_auto_punch(person_odoo_id, action, wc_name, at, cur=cur)
            _record_punch(person_odoo_id, day, slot, punch_id, cur=cur)
        timeclock_sync.sync_one_by_id(punch_id)
        run = dict(run, **{f"{slot}_punch_id": punch_id})


def run_tick(now: datetime | None = None) -> None:
    """One worker sweep. Safe to call every ~60s."""
    worker_mode = mode()
    if worker_mode == "off":
        return
    now = (now or datetime.now(shift_config.SITE_TZ)).astimezone(shift_config.SITE_TZ)
    today = now.date()
    if today.weekday() >= 5:
        return
    if now.time() < PUNCH_TIMES["morning_in"]:
        return
    if not company_holidays.has_synced():
        _log.info("auto-salaried: holiday mirror never synced; skipping tick")
        return
    person_ids = _fixed_wage_ids()
    if not person_ids:
        return
    runs = _get_runs_bulk(today, person_ids)
    is_holiday = company_holidays.for_day(today) is not None
    leave_ids = _approved_leave_ids(today, person_ids)
    for pid in person_ids:
        try:
            _advance_person(pid, today, now, runs.get(pid), is_holiday,
                            pid in leave_ids, worker_mode)
        except Exception as e:  # noqa: BLE001 — one person never kills the tick
            _log.warning("auto-salaried: failed for person %s: %s", pid, e)
    if worker_mode == "live":
        _patch_departments()
```

- [ ] **Step 2: Write the worker tests**

```python
# tests/test_auto_salaried_worker.py
"""run_tick end-to-end against Postgres with Odoo + sync stubbed.
Mirrors tests/test_auto_lunch_worker.py hygiene. skipif Postgres."""
import os
from datetime import date, datetime, time

import pytest

from zira_dashboard import auto_salaried as asal
from zira_dashboard import company_holidays, db, shift_config

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres")

PID = 990888  # test person odoo_id unlikely to collide
TUE = date(2026, 9, 1)


def _at(hh, mm, day=TUE):
    return datetime.combine(day, time(hh, mm), tzinfo=shift_config.SITE_TZ)


def _cleanup():
    db.execute("DELETE FROM auto_salaried_runs WHERE person_odoo_id = %s", (PID,))
    db.execute("DELETE FROM auto_salaried_flags WHERE person_odoo_id = %s", (PID,))
    db.execute("DELETE FROM timeclock_punches_log WHERE person_odoo_id = %s", (PID,))
    db.execute("DELETE FROM time_off_requests WHERE person_odoo_id = %s", (PID,))
    db.execute("DELETE FROM people WHERE odoo_id = %s", (PID,))


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    db.bootstrap_schema()
    _cleanup()
    db.execute(
        "INSERT INTO people (odoo_id, name, active, wage_type) "
        "VALUES (%s, 'Test Salaried', TRUE, 'monthly')", (PID,))
    monkeypatch.setenv("AUTO_SALARIED_ENABLED", "1")
    monkeypatch.delenv("AUTO_SALARIED_DRY_RUN", raising=False)
    monkeypatch.setattr(company_holidays, "has_synced", lambda: True)
    monkeypatch.setattr(company_holidays, "for_day", lambda d: None)
    monkeypatch.setattr("zira_dashboard.timeclock_sync.sync_one_by_id", lambda _id: None)
    # No open Odoo attendance by default → lunch dept unread path.
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.get_current_attendance", lambda pid: None)
    yield
    _cleanup()


def _punches():
    return db.query(
        "SELECT action, wc_name, occurred_at, source FROM timeclock_punches_log "
        "WHERE person_odoo_id = %s ORDER BY occurred_at, id", (PID,))


def _run_row():
    rows = db.query(
        "SELECT * FROM auto_salaried_runs WHERE person_odoo_id = %s AND day = %s",
        (PID, TUE))
    return rows[0] if rows else None


def test_full_day_four_punches(monkeypatch):
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.get_current_attendance",
        lambda pid: {"id": 42, "department_id": 9, "department_name": "05 Sustaining"})
    for hh, mm in ((6, 0), (11, 0), (11, 30), (15, 30)):
        asal.run_tick(_at(hh, mm))
    rows = _punches()
    assert [(r["action"], r["wc_name"]) for r in rows] == [
        ("clock_in", "Sustaining"), ("clock_out", None),
        ("clock_in", "Sustaining"), ("clock_out", None)]
    assert all(r["source"] == "auto_salaried" for r in rows)
    run = _run_row()
    assert all(run[f"{s}_punch_id"] for s in asal.SLOT_ORDER)
    assert run["dept_patch_state"] == "none"  # already Sustaining, no patch


def test_no_double_punch_on_repeat_ticks():
    asal.run_tick(_at(6, 0))
    asal.run_tick(_at(6, 1))
    asal.run_tick(_at(6, 2))
    assert len(_punches()) == 1


def test_catch_up_backdates():
    asal.run_tick(_at(12, 10))  # app "down" until 12:10
    rows = _punches()
    assert [r["action"] for r in rows] == ["clock_in", "clock_out", "clock_in"]
    assert rows[0]["occurred_at"].astimezone(shift_config.SITE_TZ).time() == time(6, 0)
    assert rows[1]["occurred_at"].astimezone(shift_config.SITE_TZ).time() == time(11, 0)


def test_approved_leave_skips_day():
    db.execute(
        "INSERT INTO time_off_requests (person_odoo_id, shape, holiday_status_id, "
        "date_from, date_to, state) VALUES (%s, 'full_day', 1, %s, %s, 'validate')",
        (PID, TUE, TUE))
    asal.run_tick(_at(6, 0))
    assert _punches() == []
    assert _run_row()["skip_reason"] == "approved_leave"


def test_pending_leave_does_not_skip():
    db.execute(
        "INSERT INTO time_off_requests (person_odoo_id, shape, holiday_status_id, "
        "date_from, date_to, state) VALUES (%s, 'full_day', 1, %s, %s, 'confirm')",
        (PID, TUE, TUE))
    asal.run_tick(_at(6, 0))
    assert len(_punches()) == 1


def test_holiday_skips_day(monkeypatch):
    monkeypatch.setattr(company_holidays, "for_day", lambda d: object())
    asal.run_tick(_at(6, 0))
    assert _punches() == []
    assert _run_row()["skip_reason"] == "holiday"


def test_weekend_no_op():
    sat = date(2026, 9, 5)
    asal.run_tick(_at(6, 0, day=sat))
    assert _punches() == []
    assert _run_row() is None


def test_hourly_person_untouched():
    db.execute("UPDATE people SET wage_type = 'hourly' WHERE odoo_id = %s", (PID,))
    asal.run_tick(_at(6, 0))
    assert _punches() == []


def test_foreign_punches_skip_and_flag():
    db.execute(
        "INSERT INTO timeclock_punches_log (person_odoo_id, action, occurred_at, source) "
        "VALUES (%s, 'clock_in', %s, 'kiosk')", (PID, _at(6, 30)))
    asal.run_tick(_at(7, 0))
    own = [r for r in _punches() if r["source"] == "auto_salaried"]
    assert own == []
    assert _run_row()["skip_reason"] == "other_punches"
    flags = db.query(
        "SELECT reason FROM auto_salaried_flags WHERE person_odoo_id = %s", (PID,))
    assert [f["reason"] for f in flags] == ["other_punches"]


def test_dept_capture_pending_patch(monkeypatch):
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.get_current_attendance",
        lambda pid: {"id": 42, "department_id": 31, "department_name": "Maintenance"})
    patched = []
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.set_attendance_department",
        lambda att_id, dept_id: patched.append((att_id, dept_id)) or True)
    asal.run_tick(_at(6, 0))
    asal.run_tick(_at(11, 0))
    run = _run_row()
    assert run["lunch_dept_id"] == 31
    assert run["dept_patch_state"] == "pending"
    asal.run_tick(_at(11, 30))
    # Simulate the sync landing the lunch-in punch in Odoo.
    db.execute(
        "UPDATE timeclock_punches_log SET synced_to_odoo = TRUE, odoo_attendance_id = 777 "
        "WHERE id = %s", (_run_row()["lunch_in_punch_id"],))
    asal.run_tick(_at(11, 32))
    assert patched == [(777, 31)]
    assert _run_row()["dept_patch_state"] == "done"


def test_dry_run_writes_no_punches(monkeypatch):
    monkeypatch.setenv("AUTO_SALARIED_DRY_RUN", "1")
    for hh, mm in ((6, 0), (11, 0), (11, 30), (15, 30)):
        asal.run_tick(_at(hh, mm))
    assert _punches() == []
    run = _run_row()
    assert all(run[f"{s}_punch_id"] == asal.SIMULATED_PUNCH_ID for s in asal.SLOT_ORDER)


def test_off_mode_no_op(monkeypatch):
    monkeypatch.delenv("AUTO_SALARIED_ENABLED", raising=False)
    asal.run_tick(_at(6, 0))
    assert _punches() == []
```

- [ ] **Step 3: Run the tests**

Run: `pytest tests/test_auto_salaried_worker.py -v`
Expected: PASS (12 tests) with `DATABASE_URL` pointing at local Postgres; SKIP without it. Also run `pytest tests/test_auto_salaried_decide.py -v` (still PASS).

Note: if the `people` insert fails on NOT NULL columns not listed here, check `CREATE TABLE IF NOT EXISTS people` in `_schema.py` and extend the INSERT with required defaults — follow whatever `tests/test_auto_lunch_worker.py`-adjacent tests insert.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/auto_salaried.py tests/test_auto_salaried_worker.py
git commit -m "feat: auto-salaried punch worker tick"
```

---

### Task 5: Reconciler (`run_reconcile`)

**Files:**
- Modify: `src/zira_dashboard/auto_salaried.py` (append)
- Test: `tests/test_auto_salaried_reconcile.py`

**Interfaces:**
- Consumes: `odoo_client.fetch_employee_attendances_for_day(pid, day)` (list of dicts with `id`), `odoo_client.delete_attendances(ids)`.
- Produces: `run_reconcile(now: datetime | None = None) -> None` — safe every ~600s.

- [ ] **Step 1: Append the reconciler to `auto_salaried.py`**

```python
# ---------- Reconciler ----------

def _runs_with_late_leave(start: date, end: date) -> list[dict]:
    """Punched, unhandled runs in [start, end] whose person now has approved
    leave overlapping that day (leave arrived AFTER the 6:00 skip check)."""
    return db.query(
        "SELECT r.* FROM auto_salaried_runs r WHERE r.day BETWEEN %s AND %s "
        "AND r.skipped = FALSE AND r.reverted = FALSE AND r.flagged = FALSE "
        "AND r.morning_in_punch_id IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM time_off_requests t "
        "  WHERE t.person_odoo_id = r.person_odoo_id AND t.state = 'validate' "
        "  AND t.date_from <= r.day AND t.date_to >= r.day)",
        (start, end),
    )


def _mark_flagged(person_odoo_id: int, day: date) -> None:
    db.execute(
        "UPDATE auto_salaried_runs SET flagged = TRUE, updated_at = now() "
        "WHERE person_odoo_id = %s AND day = %s", (person_odoo_id, day))


def _revert_day(run: dict) -> None:
    """Delete the robot's punches for a leave-conflicted day — only when the
    day is clean (nothing but auto_salaried punches locally AND in Odoo)."""
    pid, day = int(run["person_odoo_id"]), run["day"]
    start, end = _day_bounds(day)
    log_rows = db.query(
        "SELECT id, source, synced_to_odoo, odoo_attendance_id "
        "FROM timeclock_punches_log WHERE person_odoo_id = %s "
        "AND COALESCE(rounded_at, occurred_at) >= %s "
        "AND COALESCE(rounded_at, occurred_at) < %s",
        (pid, start, end),
    )
    if any(r["source"] != "auto_salaried" for r in log_rows):
        _flag(pid, day, "leave_conflict",
              "Approved leave arrived after punches, but the day has "
              "non-robot punches (transfer or manual). Clean up in Odoo.")
        _mark_flagged(pid, day)
        return
    own_ids = sorted({int(r["odoo_attendance_id"]) for r in log_rows
                      if r["odoo_attendance_id"]})
    from . import odoo_client
    odoo_atts = odoo_client.fetch_employee_attendances_for_day(pid, day)
    strangers = [a for a in odoo_atts if int(a["id"]) not in own_ids]
    if strangers:
        _flag(pid, day, "leave_conflict",
              f"Approved leave arrived after punches, but Odoo has "
              f"{len(strangers)} attendance record(s) the robot didn't "
              f"create (outside-app transfer or manual entry). Clean up in Odoo.")
        _mark_flagged(pid, day)
        return
    odoo_client.delete_attendances(own_ids)
    log_ids = [int(r["id"]) for r in log_rows]
    if log_ids:
        # Also neutralize any not-yet-synced rows so the retry sweep can't
        # resurrect the deleted day.
        db.execute(
            "UPDATE timeclock_punches_log SET synced_to_odoo = TRUE, "
            "sync_error = 'reverted: approved leave', synced_at = now() "
            "WHERE id = ANY(%s)", (log_ids,))
    db.execute(
        "UPDATE auto_salaried_runs SET reverted = TRUE, updated_at = now() "
        "WHERE person_odoo_id = %s AND day = %s", (pid, day))
    _log.info("auto-salaried: reverted %s punches for person %s on %s "
              "(approved leave)", len(own_ids), pid, day)


def _flag_incomplete_days(start: date, end_exclusive: date) -> None:
    """Past days where the robot started but never finished all four slots."""
    rows = db.query(
        "SELECT person_odoo_id, day FROM auto_salaried_runs "
        "WHERE day >= %s AND day < %s AND skipped = FALSE AND reverted = FALSE "
        "AND flagged = FALSE AND (morning_in_punch_id IS NULL "
        "OR lunch_out_punch_id IS NULL OR lunch_in_punch_id IS NULL "
        "OR day_out_punch_id IS NULL)",
        (start, end_exclusive),
    )
    for r in rows:
        pid, day = int(r["person_odoo_id"]), r["day"]
        _flag(pid, day, "incomplete_day",
              "The day ended without all four auto punches (extended app "
              "downtime?). Check the person's attendance in Odoo.")
        _mark_flagged(pid, day)


def run_reconcile(now: datetime | None = None) -> None:
    """Slow sweep (~600s): late-approved-leave cleanup + incomplete-day flags.
    Live mode only — dry-run wrote no real punches, so there is nothing to
    revert, and 'incomplete' days are expected while simulating."""
    if mode() != "live":
        return
    now = (now or datetime.now(shift_config.SITE_TZ)).astimezone(shift_config.SITE_TZ)
    today = now.date()
    start = today - timedelta(days=RECONCILE_LOOKBACK_DAYS)
    for run in _runs_with_late_leave(start, today):
        try:
            _revert_day(run)
        except Exception as e:  # noqa: BLE001 — one day never kills the sweep
            _log.warning("auto-salaried reconcile: failed for person %s %s: %s",
                         run["person_odoo_id"], run["day"], e)
    _flag_incomplete_days(start, today)
```

- [ ] **Step 2: Write the reconciler tests**

```python
# tests/test_auto_salaried_reconcile.py
"""run_reconcile against Postgres with Odoo stubbed. skipif Postgres."""
import os
from datetime import date, datetime, time, timedelta

import pytest

from zira_dashboard import auto_salaried as asal
from zira_dashboard import db, shift_config

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres")

PID = 990889
TUE = date(2026, 9, 1)


def _cleanup():
    db.execute("DELETE FROM auto_salaried_runs WHERE person_odoo_id = %s", (PID,))
    db.execute("DELETE FROM auto_salaried_flags WHERE person_odoo_id = %s", (PID,))
    db.execute("DELETE FROM timeclock_punches_log WHERE person_odoo_id = %s", (PID,))
    db.execute("DELETE FROM time_off_requests WHERE person_odoo_id = %s", (PID,))


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    db.bootstrap_schema()
    _cleanup()
    monkeypatch.setenv("AUTO_SALARIED_ENABLED", "1")
    monkeypatch.delenv("AUTO_SALARIED_DRY_RUN", raising=False)
    yield
    _cleanup()


def _now():
    return datetime.combine(TUE, time(12, 0), tzinfo=shift_config.SITE_TZ)


def _seed_punched_morning(odoo_att_id=501):
    """A morning-in punch (synced) + its run row, as run_tick would leave them."""
    at = datetime.combine(TUE, time(6, 0), tzinfo=shift_config.SITE_TZ)
    row = db.query(
        "INSERT INTO timeclock_punches_log (person_odoo_id, action, wc_name, "
        "occurred_at, rounded_at, source, synced_to_odoo, odoo_attendance_id) "
        "VALUES (%s, 'clock_in', 'Sustaining', %s, %s, 'auto_salaried', TRUE, %s) "
        "RETURNING id", (PID, at, at, odoo_att_id))
    db.execute(
        "INSERT INTO auto_salaried_runs (person_odoo_id, day, morning_in_punch_id) "
        "VALUES (%s, %s, %s)", (PID, TUE, row[0]["id"]))


def _approve_leave():
    db.execute(
        "INSERT INTO time_off_requests (person_odoo_id, shape, holiday_status_id, "
        "date_from, date_to, state) VALUES (%s, 'full_day', 1, %s, %s, 'validate')",
        (PID, TUE, TUE))


def _run(day=TUE):
    rows = db.query(
        "SELECT * FROM auto_salaried_runs WHERE person_odoo_id = %s AND day = %s",
        (PID, day))
    return rows[0] if rows else None


def _flags():
    return [f["reason"] for f in db.query(
        "SELECT reason FROM auto_salaried_flags WHERE person_odoo_id = %s "
        "ORDER BY reason", (PID,))]


def test_clean_day_reverted(monkeypatch):
    _seed_punched_morning(odoo_att_id=501)
    _approve_leave()
    deleted = []
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.fetch_employee_attendances_for_day",
        lambda pid, day: [{"id": 501}])
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.delete_attendances",
        lambda ids: deleted.append(ids))
    asal.run_reconcile(_now())
    assert deleted == [[501]]
    assert _run()["reverted"] is True
    log = db.query(
        "SELECT sync_error FROM timeclock_punches_log WHERE person_odoo_id = %s", (PID,))
    assert log[0]["sync_error"] == "reverted: approved leave"


def test_stranger_odoo_record_flags_instead(monkeypatch):
    _seed_punched_morning(odoo_att_id=501)
    _approve_leave()
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.fetch_employee_attendances_for_day",
        lambda pid, day: [{"id": 501}, {"id": 999}])  # outside-app transfer
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.delete_attendances",
        lambda ids: pytest.fail("must not delete on a messy day"))
    asal.run_reconcile(_now())
    assert _run()["flagged"] is True
    assert _run()["reverted"] is False
    assert "leave_conflict" in _flags()


def test_foreign_local_punch_flags_instead(monkeypatch):
    _seed_punched_morning()
    _approve_leave()
    at = datetime.combine(TUE, time(8, 0), tzinfo=shift_config.SITE_TZ)
    db.execute(
        "INSERT INTO timeclock_punches_log (person_odoo_id, action, occurred_at, source) "
        "VALUES (%s, 'transfer_in', %s, 'kiosk')", (PID, at))
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.delete_attendances",
        lambda ids: pytest.fail("must not delete on a messy day"))
    asal.run_reconcile(_now())
    assert "leave_conflict" in _flags()


def test_no_leave_no_action(monkeypatch):
    _seed_punched_morning()
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.delete_attendances",
        lambda ids: pytest.fail("no leave, no delete"))
    asal.run_reconcile(_now())
    assert _run()["reverted"] is False
    assert _flags() == []


def test_incomplete_past_day_flagged():
    yesterday = TUE - timedelta(days=1)
    at = datetime.combine(yesterday, time(6, 0), tzinfo=shift_config.SITE_TZ)
    row = db.query(
        "INSERT INTO timeclock_punches_log (person_odoo_id, action, wc_name, "
        "occurred_at, rounded_at, source) VALUES "
        "(%s, 'clock_in', 'Sustaining', %s, %s, 'auto_salaried') RETURNING id",
        (PID, at, at))
    db.execute(
        "INSERT INTO auto_salaried_runs (person_odoo_id, day, morning_in_punch_id) "
        "VALUES (%s, %s, %s)", (PID, yesterday, row[0]["id"]))
    asal.run_reconcile(_now())
    assert "incomplete_day" in _flags()
    assert _run(yesterday)["flagged"] is True


def test_today_incomplete_not_flagged():
    _seed_punched_morning()  # today, mid-day: naturally incomplete
    asal.run_reconcile(_now())
    assert _flags() == []
```

- [ ] **Step 3: Run the tests**

Run: `pytest tests/test_auto_salaried_reconcile.py -v`
Expected: PASS (6 tests) with `DATABASE_URL`; SKIP without.

- [ ] **Step 4: Run the whole auto-salaried suite**

Run: `pytest tests/test_auto_salaried_decide.py tests/test_auto_salaried_worker.py tests/test_auto_salaried_reconcile.py tests/test_auto_salaried_odoo_client.py tests/test_auto_salaried_schema_static.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/auto_salaried.py tests/test_auto_salaried_reconcile.py
git commit -m "feat: auto-salaried reconciler (late-leave revert + flags)"
```

---

### Task 6: Warmer registration

**Files:**
- Modify: `src/zira_dashboard/app.py` (tick coroutines near `_tick_auto_lunch` at ~line 127; registry entries in `_WARMERS` at ~line 387, next to the `("auto-lunch", _tick_auto_lunch, 60)` entry)
- Test: `tests/test_auto_salaried_warmers.py`

**Interfaces:**
- Consumes: `auto_salaried.run_tick`, `auto_salaried.run_reconcile` (Tasks 4–5).
- Produces: `_WARMERS` entries `("auto-salaried punch", _tick_auto_salaried, 60)` and `("auto-salaried reconcile", _tick_auto_salaried_reconcile, 600)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auto_salaried_warmers.py
from zira_dashboard import app as app_module


def _warmer(name):
    return next((w for w in app_module._WARMERS if w[0] == name), None)


def test_auto_salaried_warmers_registered():
    tick = _warmer("auto-salaried punch")
    reconcile = _warmer("auto-salaried reconcile")
    assert tick is not None and tick[2] == 60
    assert reconcile is not None and reconcile[2] == 600
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auto_salaried_warmers.py -v`
Expected: FAIL (`tick is None`)

- [ ] **Step 3: Add coroutines and registry entries in `app.py`**

Next to `_tick_auto_lunch` (~line 127):

```python
async def _tick_auto_salaried():
    """Auto-salaried punch worker (see auto_salaried.py). Off unless
    AUTO_SALARIED_ENABLED=1 or AUTO_SALARIED_DRY_RUN=1."""
    from . import auto_salaried

    await asyncio.to_thread(auto_salaried.run_tick)


async def _tick_auto_salaried_reconcile():
    from . import auto_salaried

    await asyncio.to_thread(auto_salaried.run_reconcile)
```

In `_WARMERS`, directly after the `("auto-lunch", _tick_auto_lunch, 60)` entry:

```python
    ("auto-salaried punch", _tick_auto_salaried, 60),
    ("auto-salaried reconcile", _tick_auto_salaried_reconcile, 600),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auto_salaried_warmers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/app.py tests/test_auto_salaried_warmers.py
git commit -m "feat: register auto-salaried punch + reconcile warmers"
```

---

### Task 7: Flags admin page

**Files:**
- Create: `src/zira_dashboard/routes/auto_salaried_admin.py`
- Create: `src/zira_dashboard/templates/auto_salaried_flags.html`
- Modify: `src/zira_dashboard/app.py` (import + `include_router`, alongside the other `app.include_router(...)` calls at ~line 595)
- Test: `tests/test_auto_salaried_flags_page.py`

**Interfaces:**
- Produces: `GET /auto-salaried/flags` (HTML list of unresolved flags), `POST /auto-salaried/flags/{flag_id}/resolve` (marks resolved, 303 back to the list).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auto_salaried_flags_page.py
"""Flags page smoke tests. skipif Postgres (rows come from the flags table)."""
import os
from datetime import date

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres")

from zira_dashboard import db
from zira_dashboard.app import app

PID = 990890


@pytest.fixture(autouse=True)
def _setup():
    db.bootstrap_schema()
    db.execute("DELETE FROM auto_salaried_flags WHERE person_odoo_id = %s", (PID,))
    yield
    db.execute("DELETE FROM auto_salaried_flags WHERE person_odoo_id = %s", (PID,))


def test_flags_page_lists_unresolved():
    db.execute(
        "INSERT INTO auto_salaried_flags (person_odoo_id, day, reason, details) "
        "VALUES (%s, %s, 'leave_conflict', 'test detail xyz')",
        (PID, date(2026, 9, 1)))
    with TestClient(app) as client:
        r = client.get("/auto-salaried/flags")
    assert r.status_code == 200
    assert "leave_conflict" in r.text
    assert "test detail xyz" in r.text


def test_resolve_flag():
    db.execute(
        "INSERT INTO auto_salaried_flags (person_odoo_id, day, reason) "
        "VALUES (%s, %s, 'incomplete_day')", (PID, date(2026, 9, 1)))
    flag_id = db.query(
        "SELECT id FROM auto_salaried_flags WHERE person_odoo_id = %s", (PID,))[0]["id"]
    with TestClient(app) as client:
        r = client.post(f"/auto-salaried/flags/{flag_id}/resolve", follow_redirects=False)
        assert r.status_code == 303
        r2 = client.get("/auto-salaried/flags")
    assert "incomplete_day" not in r2.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auto_salaried_flags_page.py -v`
Expected: FAIL with 404s

- [ ] **Step 3: Create the route module**

```python
# src/zira_dashboard/routes/auto_salaried_admin.py
"""Auto-salaried punch 'needs a human' list: days the robot wouldn't touch
(late-leave conflicts, incomplete days, unreadable departments). Read the
reasons, fix the day in Odoo if needed, hit Resolve."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..deps import templates

router = APIRouter()


@router.get("/auto-salaried/flags", response_class=HTMLResponse)
def auto_salaried_flags(request: Request):
    from .. import db

    rows = db.query(
        "SELECT f.id, f.person_odoo_id, f.day, f.reason, f.details, f.created_at, "
        "COALESCE(p.name, '#' || f.person_odoo_id::text) AS person_name "
        "FROM auto_salaried_flags f "
        "LEFT JOIN people p ON p.odoo_id = f.person_odoo_id "
        "WHERE f.resolved_at IS NULL "
        "ORDER BY f.day DESC, person_name",
        (),
    )
    return templates.TemplateResponse(
        request, "auto_salaried_flags.html", {"flags": rows})


@router.post("/auto-salaried/flags/{flag_id}/resolve")
def resolve_flag(flag_id: int):
    from .. import db

    db.execute(
        "UPDATE auto_salaried_flags SET resolved_at = now() "
        "WHERE id = %s AND resolved_at IS NULL", (flag_id,))
    return RedirectResponse("/auto-salaried/flags", status_code=303)
```

- [ ] **Step 4: Create the template**

```html
{# src/zira_dashboard/templates/auto_salaried_flags.html #}
{% extends "_base_app.html" %}
{% block title %}Auto-Salaried Flags{% endblock %}
{% block content %}
<div style="max-width: 960px; margin: 0 auto; padding: 1rem;">
  <h1>Auto-Salaried Punch — Needs a Human</h1>
  <p>Days the auto-punch robot could not handle safely. Fix the day in Odoo
     if needed, then resolve the flag.</p>
  {% if not flags %}
    <p><em>No open flags. All clear.</em></p>
  {% else %}
  <table>
    <thead>
      <tr><th>Day</th><th>Person</th><th>Reason</th><th>Details</th><th></th></tr>
    </thead>
    <tbody>
      {% for f in flags %}
      <tr>
        <td>{{ f.day }}</td>
        <td>{{ f.person_name }}</td>
        <td><code>{{ f.reason }}</code></td>
        <td>{{ f.details or "" }}</td>
        <td>
          <form method="post" action="/auto-salaried/flags/{{ f.id }}/resolve">
            <button type="submit">Resolve</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}
</div>
{% endblock %}
```

Note: open `src/zira_dashboard/templates/_base_app.html` and confirm the main block is named `content`; if it uses a different block name (e.g. `body`), match it.

- [ ] **Step 5: Register the router in `app.py`**

With the other route imports:

```python
from .routes import auto_salaried_admin
```

With the other `include_router` calls (~line 595):

```python
app.include_router(auto_salaried_admin.router)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_auto_salaried_flags_page.py -v`
Expected: PASS (2 tests)

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/routes/auto_salaried_admin.py \
        src/zira_dashboard/templates/auto_salaried_flags.html \
        src/zira_dashboard/app.py tests/test_auto_salaried_flags_page.py
git commit -m "feat: auto-salaried flags admin page"
```

---

### Task 8: Docs, full test run, and deploy checklist

**Files:**
- Modify: `README.md` (env var docs, in the Setup section)
- Test: full suite

- [ ] **Step 1: Document the env vars in `README.md`**

Add to the Setup section, after the `.env` paragraph:

```markdown
### Auto salaried punch

Salaried (Fixed Wage) employees get automatic attendance punches
(6:00–11:00 and 11:30–15:30, department "Sustaining") so their sustaining/
maintenance hours are trackable — see
`docs/superpowers/specs/2026-08-26-auto-salaried-punch-design.md`.

- `AUTO_SALARIED_DRY_RUN=1` — simulate: log intended punches, write nothing.
- `AUTO_SALARIED_ENABLED=1` — live. Dry-run wins if both are set.
- Requires `ODOO_KIOSK_DEPARTMENT_FIELD` for department tagging and an Odoo
  `hr.department` whose name contains "Sustaining".
- "Needs a human" flags: `/auto-salaried/flags`.
```

- [ ] **Step 2: Run the full test suite**

Run: `pytest -v`
Expected: everything passes (or only pre-existing failures/skips — compare against a `git stash`-clean baseline run if unsure).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: auto salaried punch setup notes"
```

- [ ] **Step 4: Manual deploy checklist (needs production credentials — do WITH Luke, not in CI)**

These cannot be automated from this machine (no `.env` locally at plan time):

1. Pull env values from Railway; verify `ODOO_KIOSK_DEPARTMENT_FIELD` is set in the deployed environment. If unset, punches will be department-less — set it before go-live.
2. Probe Odoo for the Sustaining department: `execute("hr.department", "search_read", [("name", "ilike", "Sustaining")], fields=["id", "name"])`. If missing, create it in Odoo (or adjust `_VIRTUAL_WC_DEPARTMENTS` to the real name).
3. Verify Odoo `wage_type` values: fetch a few known salaried employees and confirm `wage_type == 'monthly'`.
4. Deploy with `AUTO_SALARIED_DRY_RUN=1`. Watch logs for 2–3 weekdays: right people, right times, right skips (weekend/holiday/PTO).
5. Flip to `AUTO_SALARIED_ENABLED=1` (remove the dry-run var). Spot-check the first live day in Odoo Attendance, including one PTO-skip and one transfer round-trip from the maintenance app.
```
