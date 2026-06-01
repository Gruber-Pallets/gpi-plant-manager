# StratusTime Decommission — Deploy 1: Odoo Attendance Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the four live StratusTime data functions with an Odoo-backed attendance source, repoint the late/absence report + dashboard man-hours math onto it, and fix the production write-path that has silently stopped writing `production_daily` rows since StratusTime was turned off — plus backfill the gap.

**Architecture:** A new `odoo_client.fetch_attendances_for_day(day)` reads `hr.attendance` for a day; a new focused module `attendance.py` turns those punches into the exact status/absence shapes the existing consumers already expect, keyed by `str(person_odoo_id)`. The late-report pure functions (`late_report.py`) are unchanged because they already string-coerce their ids — only the data sources move. `precompute.py` stops gating production rows on the dead StratusTime directory.

**Tech Stack:** FastAPI, Jinja2, psycopg2 (`db.query`/`db.execute`), Odoo XML-RPC via `odoo_client.execute`, pytest with monkeypatch.

---

## Staged rollout — where this fits

This is **Deploy 1 of 3** from [the spec](../specs/2026-06-01-stratustime-decommission-design.md). It is independently shippable and produces working software on its own (dashboards correct again, data loss stopped). Deploys 2 (re-key tables + history migration) and 3 (delete `stratustime_client.py`, warmers, endpoints, Settings panel) get their own plans **after Deploy 1 lands and the live numbers are confirmed** — their exact edits depend on Deploy 1's landed state.

**Deploy 1 does NOT delete `stratustime_client.py`.** Vestigial references (assignment time-range formatting `_fmt_time_range` at `routes/staffing.py:369`, the clear-partial branches ~414–460, the debug/`/api/stratustime/refresh` endpoints, the boot warmers in `app.py`) stay until Deploy 3. They are harmless: the file still exists, and the hot attendance/absence paths no longer call it after this deploy.

## Context the implementer needs

**StratusTime is fully OFF.** Every `stratustime_client.*` network call now fails or returns empty. The hot paths swallow the error and degrade to empty — which is why the Late/Absence report shows nobody and the dashboards under-count absences.

**The data-loss bug (fixed in Task 11):** `precompute.flatten_attribution` ([precompute.py:43](../../../src/zira_dashboard/precompute.py)) drops every production row whose person isn't in `stratustime_client.name_to_emp_id_map()`. With StratusTime off that map is empty, so `precompute_day` writes **zero** rows. `production_daily` reads are all **by name** (`sum_by_range`, `sum_by_name`, `daily_records_in_range`), so the `emp_id` column value is immaterial to reads — it only needs to be a stable, non-null per-person string for the PK `(day, emp_id, wc_name)`.

**Identity:** the live attendance flow keys on `str(person_odoo_id)`. `late_report.py`'s pure functions already do `{str(e) for e in scheduled_emp_ids}` and iterate `attendance.items()`, so feeding them `str(odoo_id)` keys needs **no change to `late_report.py`** in this deploy. Name↔id comes from the `people` table (`people.name`, `people.odoo_id`, `people.active`) — the same join `scheduler_time_off._rows_for_day` uses.

**Status dict shape** consumed by `late_report.late_people_for_day_v2` — each value: `{"status": "no_punch"|"late"|"on_time"|"clocked_out", "minutes_late": int, "clocked_in_at": str|None, "currently_open": bool}`. The report logic branches only on `status == "no_punch"` and `status == "late"`.

**Thresholds (preserve exactly):** `grace_minutes = 7` (on-time vs late), `LATE_THRESHOLD_MINUTES = 15` (already in `late_report.py`, unchanged), `ABSENT_BUFFER_MINUTES = 30` (no-show).

**Status rule (faithful to StratusTime's `attendance_for_day`):** judged on the person's **first** check-in of the day (actual arrival, not payroll-rounded). `no_punch` = no punch today. If punched but **not currently open** (clocked out now) → `clocked_out`. If currently open: `on_time` when first check-in ≤ `shift_start + grace`, else `late` with `minutes_late = max(0, minutes since shift_start)`.

**Odoo specifics:** `hr.attendance` stores `check_in`/`check_out` as **naive-UTC** `'YYYY-MM-DD HH:MM:SS'` strings (or `False`). `odoo_client._to_odoo_dt(dt)` formats aware→naive-UTC; `odoo_client._odoo_dt_to_iso(val)` parses naive-UTC→ISO-8601-with-offset (or None). `odoo_client.execute(model, method, *args, **kwargs)` runs the XML-RPC call. The site timezone is `shift_config.SITE_TZ`; `shift_config.shift_start_for(day)` returns a `time`.

**Call sites repointed in this deploy:**
- `live_cache.refresh_attendance` (warmer source) — Task 7. Also delete `refresh_timeoff` (no readers).
- `routes/staffing.py` `_safe_attendance`, `_attendance_with_fallback`, `_late_emp_ids` — Task 8.
- `routes/departments.py:122` and `:748`, `routes/admin.py:161` (`full_day_absent_names_for_day`) — Task 9.
- `staffing.py:472` (`partial_off_intervals_for_day`) — Task 10.
- `precompute.py:101` — Task 11.

**Testing:** the full suite can't run locally (Python 3.9, FastAPI not importable). Write the tests anyway — they run in CI/Railway — and keep each task's change small enough to eyeball on the live dashboards after deploy. Existing `odoo_client` tests monkeypatch `odoo_client.execute`; follow that pattern.

## File structure

- **Create** `src/zira_dashboard/attendance.py` — Odoo-era attendance/absence logic. Pure cores take injected punch dicts + a fixed clock; thin cache-backed wrappers call Odoo. Replaces `stratustime_client.attendance_for_day` / `full_day_absent_names_for_day` / `partial_off_intervals_for_day` / `derived_absences_for_day`.
- **Create** `tests/test_attendance.py` — unit tests for the pure cores.
- **Create** `scripts/backfill_production_daily.py` — one-off gap backfill.
- **Modify** `src/zira_dashboard/odoo_client.py` — add `fetch_attendances_for_day`.
- **Modify** `tests/test_odoo_client.py` (or new `tests/test_odoo_attendance_for_day.py`) — test the new fetch.
- **Modify** `src/zira_dashboard/live_cache.py` — Odoo attendance source; delete `refresh_timeoff`.
- **Modify** `src/zira_dashboard/routes/staffing.py` — repoint attendance helpers.
- **Modify** `src/zira_dashboard/routes/departments.py`, `src/zira_dashboard/routes/admin.py` — repoint `full_day_absent_names`.
- **Modify** `src/zira_dashboard/staffing.py` — repoint partial-off intervals.
- **Modify** `src/zira_dashboard/precompute.py` — Odoo/name-based key, never drop rows.

---

### Task 1: `odoo_client.fetch_attendances_for_day`

**Files:**
- Modify: `src/zira_dashboard/odoo_client.py` (add after `fetch_open_attendances`, ~line 461)
- Test: `tests/test_odoo_attendance_for_day.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_odoo_attendance_for_day.py
from datetime import date
from zira_dashboard import odoo_client


def test_fetch_attendances_for_day_reduces_to_earliest_and_open(monkeypatch):
    # Two punches for emp 7 (earlier one wins), one still-open punch for emp 9.
    fake_rows = [
        {"id": 1, "employee_id": [7, "A"], "check_in": "2026-06-01 13:10:00", "check_out": "2026-06-01 17:00:00"},
        {"id": 2, "employee_id": [7, "A"], "check_in": "2026-06-01 12:02:00", "check_out": "2026-06-01 12:30:00"},
        {"id": 3, "employee_id": [9, "B"], "check_in": "2026-06-01 12:05:00", "check_out": False},
    ]
    monkeypatch.setattr(odoo_client, "execute", lambda *a, **k: fake_rows)

    out = odoo_client.fetch_attendances_for_day(date(2026, 6, 1))
    by_id = {r["employee_odoo_id"]: r for r in out}

    assert by_id[7]["first_check_in"].startswith("2026-06-01T12:02:00")
    assert by_id[7]["currently_open"] is False
    assert by_id[9]["currently_open"] is True


def test_fetch_attendances_for_day_empty(monkeypatch):
    monkeypatch.setattr(odoo_client, "execute", lambda *a, **k: [])
    assert odoo_client.fetch_attendances_for_day(date(2026, 6, 1)) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_odoo_attendance_for_day.py -v`
Expected: FAIL — `AttributeError: module 'zira_dashboard.odoo_client' has no attribute 'fetch_attendances_for_day'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/zira_dashboard/odoo_client.py  (insert after fetch_open_attendances)
def fetch_attendances_for_day(day) -> list[dict]:
    """Every hr.attendance whose check_in falls on `day` (site-local day,
    open AND closed), reduced to one entry per employee — their EARLIEST
    check_in plus whether any of their punches is still open.

    Returns [{employee_odoo_id, first_check_in, currently_open}, ...] where
    first_check_in is an ISO-8601 UTC string. `day` bounds are the local
    day converted to UTC, since Odoo stores naive-UTC datetimes."""
    from datetime import datetime, time as _time, timedelta
    from . import shift_config
    start_local = datetime.combine(day, _time.min, tzinfo=shift_config.SITE_TZ)
    end_local = start_local + timedelta(days=1)
    rows = execute(
        "hr.attendance", "search_read",
        [
            ("check_in", ">=", _to_odoo_dt(start_local)),
            ("check_in", "<", _to_odoo_dt(end_local)),
        ],
        fields=["id", "employee_id", "check_in", "check_out"],
    )
    agg: dict[int, dict] = {}
    for r in rows:
        emp = r.get("employee_id")
        emp_id = emp[0] if isinstance(emp, list) else emp
        if not emp_id:
            continue
        ci = _odoo_dt_to_iso(r.get("check_in"))
        if ci is None:
            continue
        is_open = not r.get("check_out")
        cur = agg.get(emp_id)
        if cur is None:
            agg[emp_id] = {"employee_odoo_id": emp_id, "first_check_in": ci, "currently_open": is_open}
        else:
            if ci < cur["first_check_in"]:
                cur["first_check_in"] = ci
            if is_open:
                cur["currently_open"] = True
    return list(agg.values())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_odoo_attendance_for_day.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/odoo_client.py tests/test_odoo_attendance_for_day.py
git commit -m "feat(attendance): fetch_attendances_for_day from Odoo hr.attendance"
```

---

### Task 2: `attendance.compute_status` (pure core)

**Files:**
- Create: `src/zira_dashboard/attendance.py`
- Test: `tests/test_attendance.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_attendance.py
from datetime import datetime, date, time, timezone, timedelta
from zoneinfo import ZoneInfo
from zira_dashboard import attendance

TZ = ZoneInfo("America/New_York")  # match shift_config.SITE_TZ


def _shift_start():
    return datetime.combine(date(2026, 6, 1), time(8, 0), tzinfo=TZ)


def _utc_iso(local_dt):
    return local_dt.astimezone(timezone.utc).isoformat()


def test_compute_status_classifies_punches():
    shift_start = _shift_start()
    now = shift_start + timedelta(hours=1)
    punches = {
        "10": {"first_check_in": _utc_iso(shift_start + timedelta(minutes=3)), "currently_open": True},   # on_time (<=+7)
        "11": {"first_check_in": _utc_iso(shift_start + timedelta(minutes=25)), "currently_open": True},  # late
        "12": {"first_check_in": _utc_iso(shift_start - timedelta(minutes=5)), "currently_open": False},  # clocked_out
    }
    out = attendance.compute_status(punches, ["10", "11", "12", "13"], now, shift_start, grace_minutes=7)
    assert out["10"]["status"] == "on_time"
    assert out["11"]["status"] == "late" and out["11"]["minutes_late"] == 25
    assert out["12"]["status"] == "clocked_out"
    assert out["13"]["status"] == "no_punch"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_attendance.py::test_compute_status_classifies_punches -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'zira_dashboard.attendance'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/zira_dashboard/attendance.py
"""Odoo-era attendance + absence logic (replaces the StratusTime client's
attendance_for_day / full_day_absent_names_for_day / partial_off_intervals_for_day
/ derived_absences_for_day).

Pure cores take injected punch dicts + a fixed clock so they are testable
without mocking time or Odoo. Cache-backed wrappers call Odoo via live_cache.

Identity is `str(person_odoo_id)` throughout, matching the rest of the
Odoo-era stack and the string-keyed late_report helpers.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

GRACE_MINUTES = 7
ABSENT_BUFFER_MINUTES = 30


def compute_status(
    punches: dict,
    ids: Iterable[str],
    now_local: datetime,
    shift_start_local: datetime,
    grace_minutes: int = GRACE_MINUTES,
) -> dict:
    """Per-id attendance status. `punches` is {str_id: {first_check_in(iso UTC),
    currently_open(bool)}}. Returns {str_id: {status, minutes_late,
    clocked_in_at, currently_open}} for every id in `ids`.

    status: no_punch | on_time | late | clocked_out. Judged on the FIRST
    check-in of the day (actual arrival). A punched-but-currently-out person
    is clocked_out; a currently-open person is on_time/late vs shift_start+grace.
    """
    from . import shift_config
    cutoff = shift_start_local + timedelta(minutes=grace_minutes)
    out: dict = {}
    for raw in ids:
        sid = str(raw)
        p = punches.get(sid)
        entry = {"status": "no_punch", "minutes_late": 0, "clocked_in_at": None, "currently_open": False}
        ci = (p or {}).get("first_check_in")
        if p and ci:
            ci_local = datetime.fromisoformat(ci).astimezone(shift_config.SITE_TZ)
            entry["clocked_in_at"] = ci_local.strftime("%I:%M %p").lstrip("0")
            entry["currently_open"] = bool(p.get("currently_open"))
            if not entry["currently_open"]:
                entry["status"] = "clocked_out"
            elif ci_local <= cutoff:
                entry["status"] = "on_time"
            else:
                entry["status"] = "late"
                entry["minutes_late"] = max(0, int((ci_local - shift_start_local).total_seconds() // 60))
        out[sid] = entry
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_attendance.py::test_compute_status_classifies_punches -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/attendance.py tests/test_attendance.py
git commit -m "feat(attendance): compute_status pure core (no_punch/on_time/late/clocked_out)"
```

---

### Task 3: `attendance.punches_for_day` + `status_for_day` (cache-backed)

**Files:**
- Modify: `src/zira_dashboard/attendance.py`
- Test: `tests/test_attendance.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_attendance.py  (append)
def test_punches_for_day_keys_by_str_id(monkeypatch):
    from zira_dashboard import odoo_client
    monkeypatch.setattr(odoo_client, "fetch_attendances_for_day", lambda d: [
        {"employee_odoo_id": 7, "first_check_in": "2026-06-01T12:02:00+00:00", "currently_open": True},
    ])
    out = attendance.punches_for_day(date(2026, 6, 1))
    assert out == {"7": {"first_check_in": "2026-06-01T12:02:00+00:00", "currently_open": True}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_attendance.py::test_punches_for_day_keys_by_str_id -v`
Expected: FAIL — `AttributeError: module 'zira_dashboard.attendance' has no attribute 'punches_for_day'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/zira_dashboard/attendance.py  (append)
def punches_for_day(day) -> dict:
    """Pull today's Odoo punches and key them by str(person_odoo_id).
    {str_id: {first_check_in, currently_open}}. This is what the live_cache
    warmer stores in today_attendance_cache."""
    from . import odoo_client
    rows = odoo_client.fetch_attendances_for_day(day)
    return {
        str(r["employee_odoo_id"]): {
            "first_check_in": r["first_check_in"],
            "currently_open": r["currently_open"],
        }
        for r in rows
    }


def status_for_day(day, ids, now_local, shift_start_local) -> dict:
    """Cache-aware status for `ids` on `day`: read punches from live_cache
    (warmer-populated), fall back to a direct Odoo pull, then compute_status
    against the supplied clock so minutes_late stays fresh."""
    from . import live_cache
    payload, refreshed = live_cache.read_attendance(day)
    if payload is None:
        payload = punches_for_day(day)
    return compute_status(payload or {}, ids, now_local, shift_start_local)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_attendance.py::test_punches_for_day_keys_by_str_id -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/attendance.py tests/test_attendance.py
git commit -m "feat(attendance): cache-backed punches_for_day + status_for_day"
```

---

### Task 4: `attendance.name_to_person_id`

**Files:**
- Modify: `src/zira_dashboard/attendance.py`
- Test: `tests/test_attendance.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_attendance.py  (append)
def test_name_to_person_id_maps_active_people(monkeypatch):
    from zira_dashboard import db
    monkeypatch.setattr(db, "query", lambda *a, **k: [
        {"name": "Jose Luis", "odoo_id": 42},
        {"name": "Maria", "odoo_id": 7},
    ])
    assert attendance.name_to_person_id() == {"Jose Luis": "42", "Maria": "7"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_attendance.py::test_name_to_person_id_maps_active_people -v`
Expected: FAIL — no attribute `name_to_person_id`

- [ ] **Step 3: Write minimal implementation**

```python
# src/zira_dashboard/attendance.py  (append)
def name_to_person_id() -> dict:
    """{roster_name: str(person_odoo_id)} for active employees with an Odoo
    id. Replaces stratustime_client.name_to_emp_id_map. Names align with
    roster names (both from odoo_sync._short_name)."""
    from . import db
    rows = db.query(
        "SELECT name, odoo_id FROM people WHERE active = TRUE AND odoo_id IS NOT NULL"
    )
    return {r["name"]: str(r["odoo_id"]) for r in rows}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_attendance.py::test_name_to_person_id_maps_active_people -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/attendance.py tests/test_attendance.py
git commit -m "feat(attendance): name_to_person_id from the people table"
```

---

### Task 5: `attendance.derived_absent_names` + `full_day_absent_names`

**Files:**
- Modify: `src/zira_dashboard/attendance.py`
- Test: `tests/test_attendance.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_attendance.py  (append)
def test_full_day_absent_union(monkeypatch):
    from zira_dashboard import scheduler_time_off, late_report
    monkeypatch.setattr(scheduler_time_off, "full_day_off_names", lambda d: {"Ana"})
    monkeypatch.setattr(late_report, "absent_names_for_day", lambda d: {"Bob"})
    # No derived no-shows in this test (stub it to empty).
    monkeypatch.setattr(attendance, "derived_absent_names", lambda d: {"Carl"})
    assert attendance.full_day_absent_names(date(2026, 6, 1)) == {"Ana", "Bob", "Carl"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_attendance.py::test_full_day_absent_union -v`
Expected: FAIL — no attribute `full_day_absent_names`

- [ ] **Step 3: Write minimal implementation**

```python
# src/zira_dashboard/attendance.py  (append)
def derived_absent_names(day) -> set:
    """Active, non-reserve roster people with NO Odoo punch by
    shift_start + ABSENT_BUFFER_MINUTES who are not on approved/pending
    time off. Today only (matches the old derived_absences_for_day) —
    past/future days return an empty set."""
    from datetime import datetime, timezone
    from . import shift_config, staffing, scheduler_time_off
    today = datetime.now(timezone.utc).date()
    if day != today:
        return set()
    now_local = datetime.now(timezone.utc).astimezone(shift_config.SITE_TZ)
    shift_start_local = datetime.combine(day, shift_config.shift_start_for(day), tzinfo=shift_config.SITE_TZ)
    if now_local < shift_start_local + timedelta(minutes=ABSENT_BUFFER_MINUTES):
        return set()
    try:
        off = {e["name"] for e in scheduler_time_off.time_off_entries_for_day(day) if e.get("name")}
    except Exception:  # noqa: BLE001 — degrade to "nobody on leave"
        off = set()
    name_to_id = name_to_person_id()
    punches = punches_for_day(day)
    out: set = set()
    for p in staffing.load_roster():
        if not p.active or p.reserve or p.name in off:
            continue
        sid = name_to_id.get(p.name)
        if sid is None:
            continue  # can't check punches without an Odoo id
        if sid not in punches:
            out.add(p.name)
    return out


def full_day_absent_names(day) -> set:
    """Roster names out for the WHOLE day: full-day approved/pending leave
    ∪ manually-declared absences ∪ derived no-shows. Partial-day people are
    excluded (their time is subtracted via partial_off_intervals instead).
    Replaces stratustime_client.full_day_absent_names_for_day. Never raises."""
    from . import scheduler_time_off, late_report
    out: set = set()
    try:
        out |= set(scheduler_time_off.full_day_off_names(day))
    except Exception:  # noqa: BLE001
        pass
    try:
        out |= set(late_report.absent_names_for_day(day))
    except Exception:  # noqa: BLE001
        pass
    try:
        out |= derived_absent_names(day)
    except Exception:  # noqa: BLE001
        pass
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_attendance.py::test_full_day_absent_union -v`
Expected: PASS

- [ ] **Step 5: Add a derived-absent test, run, commit**

```python
# tests/test_attendance.py  (append)
def test_derived_absent_flags_unpunched_after_buffer(monkeypatch):
    from types import SimpleNamespace
    from zira_dashboard import staffing, scheduler_time_off, shift_config
    from datetime import datetime as _dt, timezone as _tz
    today = _dt.now(_tz.utc).date()
    monkeypatch.setattr(staffing, "load_roster", lambda: [
        SimpleNamespace(name="Ana", active=True, reserve=False),   # no punch -> absent
        SimpleNamespace(name="Bob", active=True, reserve=False),   # punched -> present
        SimpleNamespace(name="Cy", active=True, reserve=True),     # reserve -> ignored
    ])
    monkeypatch.setattr(scheduler_time_off, "time_off_entries_for_day", lambda d: [])
    monkeypatch.setattr(attendance, "name_to_person_id", lambda: {"Ana": "1", "Bob": "2", "Cy": "3"})
    monkeypatch.setattr(attendance, "punches_for_day", lambda d: {"2": {"first_check_in": "x", "currently_open": True}})
    # Force "well past shift start" by stubbing shift_start_for to early morning.
    monkeypatch.setattr(shift_config, "shift_start_for", lambda d: __import__("datetime").time(0, 0))
    assert attendance.derived_absent_names(today) == {"Ana"}
```

Run: `pytest tests/test_attendance.py -v`
Expected: PASS (all)

```bash
git add src/zira_dashboard/attendance.py tests/test_attendance.py
git commit -m "feat(attendance): derived_absent_names + full_day_absent_names from Odoo"
```

---

### Task 6: `attendance.partial_off_intervals`

**Files:**
- Modify: `src/zira_dashboard/attendance.py`
- Test: `tests/test_attendance.py`

Mirrors `stratustime_client.partial_off_intervals_for_day`: `{roster_name: [(start_utc, end_utc), ...]}` of timezone-aware UTC datetimes, consumed by `staffing.effective_minutes_worked`. Source is the Odoo mirror (`time_off_requests` partial shapes for `day`), via `scheduler_time_off`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_attendance.py  (append)
def test_partial_off_intervals_builds_utc_spans(monkeypatch):
    from zira_dashboard import scheduler_time_off
    # late_arrival: off from shift_start (6.0) until arrival (8.5) -> hour_from=6, hour_to=8.5
    monkeypatch.setattr(scheduler_time_off, "_rows_for_day", lambda d: [
        {"name": "Ana", "shape": "late_arrival", "hour_from": 6.0, "hour_to": 8.5,
         "state": "validate", "pay_type": "Custom Hours"},
        {"name": "Bob", "shape": "full_day", "hour_from": None, "hour_to": None,
         "state": "validate", "pay_type": "PTO"},  # full-day excluded
    ])
    out = attendance.partial_off_intervals(date(2026, 6, 1))
    assert "Bob" not in out
    assert len(out["Ana"]) == 1
    s, e = out["Ana"][0]
    assert s.tzinfo is not None and e > s
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_attendance.py::test_partial_off_intervals_builds_utc_spans -v`
Expected: FAIL — no attribute `partial_off_intervals`

- [ ] **Step 3: Write minimal implementation**

```python
# src/zira_dashboard/attendance.py  (append)
def partial_off_intervals(day) -> dict:
    """{roster_name: [(start_utc, end_utc), ...]} of partial-day off windows
    on `day`, as tz-aware UTC datetimes for overlap math in
    staffing.effective_minutes_worked. Full-day shapes are excluded. Source
    is the Odoo time_off_requests mirror via scheduler_time_off._rows_for_day.
    Replaces stratustime_client.partial_off_intervals_for_day. Never raises."""
    from datetime import datetime, timezone, time as _time
    from . import shift_config, scheduler_time_off
    out: dict = {}
    try:
        rows = scheduler_time_off._rows_for_day(day)
    except Exception:  # noqa: BLE001
        return out
    site_tz = shift_config.SITE_TZ
    for r in rows:
        if r.get("shape") == "full_day":
            continue
        hf = r.get("hour_from")
        ht = r.get("hour_to")
        if hf is None or ht is None:
            continue
        hf = float(hf)
        ht = float(ht)
        if ht <= hf:
            continue
        s_local = datetime.combine(day, _time(0, 0), tzinfo=site_tz) + timedelta(hours=hf)
        e_local = datetime.combine(day, _time(0, 0), tzinfo=site_tz) + timedelta(hours=ht)
        out.setdefault(r["name"], []).append(
            (s_local.astimezone(timezone.utc), e_local.astimezone(timezone.utc))
        )
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_attendance.py::test_partial_off_intervals_builds_utc_spans -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/attendance.py tests/test_attendance.py
git commit -m "feat(attendance): partial_off_intervals from the Odoo time-off mirror"
```

---

### Task 7: Repoint `live_cache` to Odoo; drop `refresh_timeoff`

**Files:**
- Modify: `src/zira_dashboard/live_cache.py:134-160`
- Modify: `src/zira_dashboard/app.py` (`_warm_live_cache_loop`, ~line 85-86)
- Test: `tests/test_live_cache.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_live_cache.py  (append)
def test_refresh_attendance_writes_odoo_punches(monkeypatch):
    from datetime import date
    from zira_dashboard import live_cache, attendance
    monkeypatch.setattr(attendance, "punches_for_day",
                        lambda d: {"7": {"first_check_in": "2026-06-01T12:00:00+00:00", "currently_open": True}})
    written = {}
    monkeypatch.setattr(live_cache, "write_attendance", lambda day, payload: written.update({"day": day, "payload": payload}))
    live_cache.refresh_attendance(date(2026, 6, 1))
    assert written["payload"] == {"7": {"first_check_in": "2026-06-01T12:00:00+00:00", "currently_open": True}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_live_cache.py::test_refresh_attendance_writes_odoo_punches -v`
Expected: FAIL — current `refresh_attendance` calls `stratustime_client` (AttributeError on the stub / wrong payload).

- [ ] **Step 3: Write minimal implementation**

Replace `refresh_attendance` and delete `refresh_timeoff` in `src/zira_dashboard/live_cache.py` (currently lines 134-160):

```python
def refresh_attendance(day: date) -> None:
    """Pull today's Odoo punches for every employee and write the keyed
    payload to cache. Routes read it and compute status against now.

    Errors are logged and swallowed — the previous good payload stays."""
    try:
        from . import attendance
        payload = attendance.punches_for_day(day)
        write_attendance(day, payload)
    except Exception as e:  # noqa: BLE001
        _log.warning("refresh_attendance(%s) failed: %s", day, e)
```

(Delete the entire `def refresh_timeoff(...)` function — `today_timeoff_cache` has no readers.)

In `src/zira_dashboard/app.py` `_warm_live_cache_loop`, delete the `refresh_timeoff` line (currently line 86):

```python
            today = datetime.now(timezone.utc).date()
            await asyncio.to_thread(live_cache.refresh_attendance, today)
            await asyncio.to_thread(
                live_cache.refresh_production, today, _zira_client()
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_live_cache.py -v`
Expected: PASS (and no test references `refresh_timeoff` — if one does, delete it)

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/live_cache.py src/zira_dashboard/app.py tests/test_live_cache.py
git commit -m "feat(attendance): live_cache attendance warmer reads Odoo; drop dead timeoff cache"
```

---

### Task 8: Repoint the staffing late/absence helpers

**Files:**
- Modify: `src/zira_dashboard/routes/staffing.py` — `_attendance_with_fallback` (76-92), `_safe_attendance` (112-185)
- Test: `tests/test_staffing_*` (add a focused test for `_safe_attendance` id-mapping)

The behavioral change: build `name_to_id` from `attendance.name_to_person_id()` (not StratusTime), fetch punches via the Odoo source, and compute the status dict with `attendance.compute_status`. `_late_emp_ids` (188-211) needs no change — it consumes `attendance_pkg["by_id"]` (now odoo-id-keyed) and `late_report.late_people_for_day` is unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_staffing_attendance_source.py
from datetime import datetime, date, time, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from zira_dashboard import attendance, staffing, shift_config
from zira_dashboard.routes import staffing as staffing_routes

TZ = ZoneInfo("America/New_York")


def test_safe_attendance_keys_by_odoo_id(monkeypatch):
    d = datetime.now(timezone.utc).date()
    monkeypatch.setattr(attendance, "name_to_person_id", lambda: {"Ana": "1", "Bob": "2"})
    monkeypatch.setattr(staffing_routes, "_timeoff_names_with_fallback", lambda day: set())
    monkeypatch.setattr(staffing, "load_roster", lambda: [
        SimpleNamespace(name="Ana", active=True, reserve=False),
        SimpleNamespace(name="Bob", active=True, reserve=False),
    ])
    # Ana scheduled; both punched (Ana on time, Bob no punch).
    monkeypatch.setattr(attendance, "status_for_day", lambda day, ids, now, ss: {
        sid: {"status": "on_time" if sid == "1" else "no_punch", "minutes_late": 0,
              "clocked_in_at": None, "currently_open": True} for sid in map(str, ids)
    })
    monkeypatch.setattr(shift_config, "shift_start_for", lambda day: time(0, 0))  # ensure past shift start
    sched = SimpleNamespace(assignments={"Baler": ["Ana"]})
    pkg = staffing_routes._safe_attendance(d, sched, d)
    assert pkg["name_to_id"] == {"Ana": "1", "Bob": "2"}
    assert "1" in pkg["by_id"] and pkg["by_name"]["Ana"]["status"] == "on_time"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_staffing_attendance_source.py -v`
Expected: FAIL — `_safe_attendance` still calls `stratustime_client.name_to_emp_id_map()` / `_attendance_with_fallback` (StratusTime), so the stubs don't take and keys/empties mismatch.

- [ ] **Step 3: Write minimal implementation**

Rewrite `_attendance_with_fallback` (76-92) to return Odoo punches via the existing `_live_or_fallback` helper (keeps the cache/staleness/refresh path; only the source changes). The cache now holds punches `{str_id: {first_check_in, currently_open}}` instead of the old status dict; `compute_status` (called in `_safe_attendance`) only reads the ids it's given, so an unfiltered fallback dict is harmless:

```python
def _attendance_with_fallback(day, ids):
    """Today's per-id punch dict. Reads the warmer-populated cache; falls
    back to a direct Odoo pull on a cold/stale cache. Keys are str(odoo_id)."""
    from .. import live_cache, attendance
    wanted = {str(i) for i in ids}
    return _live_or_fallback(
        day,
        read=live_cache.read_attendance,
        refresh=live_cache.refresh_attendance,
        fallback=lambda: attendance.punches_for_day(day),
        transform=lambda payload: {sid: info for sid, info in payload.items() if sid in wanted},
    )
```

In `_safe_attendance` (112-185), replace the StratusTime lookups. Change:

```python
        name_to_id = stratustime_client.name_to_emp_id_map()
```
to:
```python
        from .. import attendance
        name_to_id = attendance.name_to_person_id()
```

and replace the final attendance fetch + by_name build (currently the `attendance_by_id = _attendance_with_fallback(d, all_ids)` block) with a punches→status compute:

```python
        all_ids = list({*scheduled_ids, *unscheduled_ids})
        id_to_name = {v: k for k, v in name_to_id.items()}
        punches = _attendance_with_fallback(d, all_ids)
        attendance_by_id = attendance.compute_status(
            punches, all_ids, now_local, shift_start_local
        )
        by_name: dict[str, dict] = {}
        for emp_id, info in attendance_by_id.items():
            name = id_to_name.get(emp_id)
            if name:
                by_name[name] = info
        return {
            "by_name": by_name,
            "by_id": attendance_by_id,
            "name_to_id": name_to_id,
            "scheduled_ids": scheduled_ids,
            "unscheduled_ids": unscheduled_ids,
        }
```

(`now_local` and `shift_start_local` are already computed at the top of `_safe_attendance`. `scheduled_ids`/`unscheduled_ids` are built from `name_to_id` exactly as today — no change to that logic.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_staffing_attendance_source.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/routes/staffing.py tests/test_staffing_attendance_source.py
git commit -m "feat(attendance): staffing late/absence report reads Odoo punches"
```

---

### Task 9: Repoint dashboard "who's absent" (departments ×2, admin)

**Files:**
- Modify: `src/zira_dashboard/routes/departments.py:120-124` and `:746-750`
- Modify: `src/zira_dashboard/routes/admin.py:160-163`

Pure find-and-replace of the absence source. No new behavior; `attendance.full_day_absent_names` returns the same `set[str]` of roster names.

- [ ] **Step 1: Edit `routes/departments.py` (both sites)**

Site 1 (~120-124):
```python
    try:
        from .. import attendance
        _absent_today = attendance.full_day_absent_names(d)
    except Exception:
        _absent_today = set()
```
Site 2 (~746-750): identical replacement (the `from .. import stratustime_client` / `full_day_absent_names_for_day(d)` pair).

- [ ] **Step 2: Edit `routes/admin.py` (~160-163)**

```python
    try:
        from .. import attendance
        absent_today = sorted(attendance.full_day_absent_names(d))
    except Exception as e:
        absent_today = [f"<error: {e}>"]
```
Also drop `stratustime_client` from the `_pph_debug_impl` import line (141) if it's now unused there.

- [ ] **Step 3: Grep to confirm no remaining absence calls to StratusTime**

Run: `grep -rn "full_day_absent_names_for_day" src/`
Expected: only the definition in `stratustime_client.py` (removed in Deploy 3) — no route call sites.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/routes/departments.py src/zira_dashboard/routes/admin.py
git commit -m "feat(attendance): dashboards read absences from Odoo, not StratusTime"
```

---

### Task 10: Repoint partial-off intervals in man-hours math

**Files:**
- Modify: `src/zira_dashboard/staffing.py:448-449,470-472`
- Test: `tests/test_staffing_*` (reuse existing effective_minutes_worked tests if present; otherwise the Task 6 test covers the data source)

- [ ] **Step 1: Edit `staffing.effective_minutes_worked`**

Change the import (448-449):
```python
    from datetime import datetime, timezone
    from . import shift_config, attendance
```
Change the interval source (470-472):
```python
    # Subtract partial-day off intervals (Odoo time-off mirror).
    try:
        intervals_by_name = attendance.partial_off_intervals(day)
    except Exception:
        return max(0, base - break_minutes_in_window)
```

- [ ] **Step 2: Run the staffing man-hours tests**

Run: `pytest tests/ -k "effective_minutes or man_hours or staffing" -v`
Expected: PASS (interval shape is identical: `{name: [(start_utc, end_utc)]}`)

- [ ] **Step 3: Commit**

```bash
git add src/zira_dashboard/staffing.py
git commit -m "feat(attendance): partial-off man-hours subtraction reads Odoo mirror"
```

---

### Task 11: Fix the production write-path (stop dropping rows)

**Files:**
- Modify: `src/zira_dashboard/precompute.py:25-60` (`flatten_attribution`), `:94-104` (`precompute_day`)
- Test: `tests/test_precompute.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_precompute.py  (append)
from datetime import date
from zira_dashboard import precompute


def test_flatten_keeps_rows_without_odoo_id():
    attribution = {
        "Ana": {"Baler": {"units": 10, "downtime": 0, "hours": 8, "days_worked": 1}},
        "Ghost": {"Baler": {"units": 5, "downtime": 0, "hours": 8, "days_worked": 1}},
    }
    name_to_id = {"Ana": "42"}  # Ghost has no Odoo id
    rows = precompute.flatten_attribution(date(2026, 6, 1), attribution, name_to_id)
    by_name = {r["name"]: r for r in rows}
    assert by_name["Ana"]["emp_id"] == "42"
    assert by_name["Ghost"]["emp_id"] == "Ghost"  # falls back to name, NOT dropped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_precompute.py::test_flatten_keeps_rows_without_odoo_id -v`
Expected: FAIL — current code `continue`s on missing id, so "Ghost" is dropped (KeyError on `by_name["Ghost"]`).

- [ ] **Step 3: Write minimal implementation**

In `flatten_attribution` (25-60) replace the drop with a name fallback:

```python
    rows: list[dict] = []
    for person, wc_map in attribution.items():
        emp_id = name_to_emp_id.get(person) or person  # fall back to name; never drop
        for wc_name, totals in wc_map.items():
            units = float(totals.get("units") or 0)
            if units <= 0:
                continue
            rows.append({
                "day": day,
                "emp_id": str(emp_id),
                "name": person,
                "wc_name": wc_name,
                "units": units,
                "downtime": float(totals.get("downtime") or 0),
                "hours": float(totals.get("hours") or 0),
                "days_worked": float(totals.get("days_worked") or 0),
            })
    return rows
```

In `precompute_day` (94-104) source the map from Odoo, not StratusTime:

```python
def precompute_day(day: date, client) -> dict:
    """Compute attribution for one day and UPSERT into production_daily.
    Returns {"day": iso, "rows_written": int}. Idempotent; safe to re-run."""
    from . import production_history, attendance
    attribution = production_history.attribution_for(day, client)
    name_to_id = attendance.name_to_person_id()
    rows = flatten_attribution(day, attribution, name_to_id)
    written = upsert_production_daily(rows)
    return {"day": day.isoformat(), "rows_written": written}
```

Update the docstring/comment in `flatten_attribution` to drop the "StratusTime directory" reference (it now falls back to name).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_precompute.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/precompute.py tests/test_precompute.py
git commit -m "fix(precompute): never drop production rows for people missing from the directory"
```

---

### Task 12: Backfill the missing `production_daily` days

**Files:**
- Create: `scripts/backfill_production_daily.py`

Idempotent per day (delete-then-recompute) so re-running is safe and never double-counts. Window defaults wide (last 60 days) since the exact cutover date is unconfirmed; safe because each day is recomputed from Zira.

- [ ] **Step 1: Write the script**

```python
# scripts/backfill_production_daily.py
"""Rebuild production_daily for a date window (delete-then-recompute per day).

Use after the precompute fix to recover days that wrote zero rows while the
StratusTime directory was empty. Idempotent: each day is deleted then
recomputed from Zira, so re-runs and overlapping windows are safe.

Usage:
    python -m scripts.backfill_production_daily [--days N] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
Default window: the last 60 days ending today.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--start")
    ap.add_argument("--end")
    args = ap.parse_args()

    from zira_dashboard import db, precompute
    from zira_dashboard.deps import client

    db.init_pool()
    today = datetime.now(timezone.utc).date()
    end = date.fromisoformat(args.end) if args.end else today
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=args.days)

    d = start
    total = 0
    while d <= end:
        db.execute("DELETE FROM production_daily WHERE day = %s", (d,))
        res = precompute.precompute_day(d, client)
        total += res["rows_written"]
        print(f"{d}: {res['rows_written']} rows")
        d += timedelta(days=1)
    print(f"Backfill complete: {total} rows across {start}..{end}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Commit**

```bash
git add scripts/backfill_production_daily.py
git commit -m "feat(precompute): production_daily backfill script (delete-then-recompute)"
```

- [ ] **Step 3: Run the backfill (after deploy)**

On Railway (or wherever `DATABASE_URL` + Zira creds are set), after this deploy is live:

Run: `python -m scripts.backfill_production_daily --days 60`
Expected: per-day row counts printed; MTD/range totals, player-card stats, and trophy aggregates fill back in. Narrow `--days`/`--start` once Dale confirms the cutover date.

---

## Self-Review

**Spec coverage (Deploy 1 scope):**
- New Odoo attendance source (`fetch_attendances_for_day` + `attendance.py`) → Tasks 1-6. ✓
- Repoint late/absence report → Task 8. ✓
- Repoint dashboard absences + man-hours → Tasks 9, 10. ✓
- Production write-path fix + backfill → Tasks 11, 12. ✓
- Thresholds preserved (7/15/30) → Task 2 (grace 7), `late_report` unchanged (15), Task 5 (buffer 30). ✓
- Out of scope here (Deploys 2-3): re-key tables, JS/endpoint param rename, deletion of the client/warmers/endpoints/Settings panel. ✓ (explicitly deferred)

**Placeholder scan:** No TBD/TODO; every code step has complete code; commands have expected output. ✓

**Type consistency:** punch dict `{str_id: {first_check_in, currently_open}}` is produced by `punches_for_day` (Task 3) and consumed by `compute_status` (Task 2), `_attendance_with_fallback` (Task 8), `derived_absent_names` (Task 5). Status dict `{str_id: {status, minutes_late, clocked_in_at, currently_open}}` from `compute_status` is consumed by `late_report.late_people_for_day_v2` (unchanged) and `_safe_attendance`'s `by_id`/`by_name`. `name_to_person_id` returns `{name: str_id}` used as `name_to_id` in Task 8. `partial_off_intervals` returns `{name: [(utc, utc)]}` matching `effective_minutes_worked` (Task 10). Consistent. ✓

**Note on behavior parity:** status is judged on the FIRST check-in of the day (vs StratusTime's last-transaction). For the start-of-shift late/no-show purpose this is equivalent or more correct; it only differs on multi-punch days, which the report doesn't care about (it branches on `no_punch`/`late` only).
