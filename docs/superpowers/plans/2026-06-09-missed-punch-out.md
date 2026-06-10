# Missed Punch-Out Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-close any attendance still open from a prior day at that day's midnight, flag it as a "Missed Punch Out" on every screen, and let a manager type the real punch-out time to rewrite the record.

**Architecture:** A background warmer tick reads open Odoo attendances, closes any whose check-in was on a prior day at the midnight ending that day, and records each in a new `missed_punch_out` table. A `footer.js` nav badge + modal (cloned from the Missing-Work-Center alert) polls a cheap local endpoint; entering a time rewrites the Odoo `check_out` via the existing `clock_out` and resolves the flag. All Odoo math reuses existing `odoo_client` functions — no new Odoo calls.

**Tech Stack:** Python 3.12, FastAPI, psycopg2 + Postgres, Odoo XML-RPC (`odoo_client`), vanilla JS (`footer.js`), pytest.

---

## Context for the implementer

Read the design spec first: `docs/superpowers/specs/2026-06-09-missed-punch-out-design.md`.

Key existing pieces you will reuse (do **not** reimplement):

- `odoo_client.fetch_open_attendances()` → `[{att_id, employee_odoo_id, check_in (ISO-8601 UTC str), wc_name}]` for every currently-open record. (`src/zira_dashboard/odoo_client.py:536`)
- `odoo_client.clock_out(attendance_id, ts)` → sets `check_out` on a record; **safe on an already-closed record (it overwrites)**; accepts a tz-aware `datetime`. (`src/zira_dashboard/odoo_client.py:694`)
- `shift_config.SITE_TZ` = `ZoneInfo("America/Chicago")`. All "today"/midnight math is site-local.
- DB helpers: `db.query(sql, params) -> list[dict]`, `db.execute(sql, params)`. Schema DDL is a single string `SCHEMA_DDL` in `src/zira_dashboard/_schema.py`, run idempotently by `db.bootstrap_schema()`.
- The module/route/footer patterns to mirror: `src/zira_dashboard/missing_wc.py`, `src/zira_dashboard/routes/missing_wc.py`, and the "Missing Work Center" block in `src/zira_dashboard/static/footer.js` (lines ~673–850).

**Running tests:** `ZIRA_API_KEY=test .venv/bin/python -m pytest <path> -v`
Postgres-backed tests skip locally (no `DATABASE_URL`) and run in CI. Pure-logic tests run everywhere. `ZIRA_API_KEY=test` is required or route tests error at import/collection.

**This repo's `main` is rebased by a concurrent process.** Commit per task as instructed; do not push unless asked. A docs/CI workflow file may appear in `main` that your token can't push (no `workflow` scope) — that's expected and not your concern.

## File Structure

- **Create** `src/zira_dashboard/missed_punch_out.py` — pure detection (`overdue_closures`), label/shaping helpers, the close orchestration (`run_close`), and the DB layer (`record_close`, `current_rows`, `get_unresolved`, `correct`). Mirrors `missing_wc.py`.
- **Create** `src/zira_dashboard/routes/missed_punch_out.py` — `GET /api/missed-punch-out`, `POST /missed-punch-out/correct`. Mirrors `routes/missing_wc.py`.
- **Modify** `src/zira_dashboard/_schema.py` — add the `missed_punch_out` table to `SCHEMA_DDL`.
- **Modify** `src/zira_dashboard/app.py` — add `_tick_missed_punch_out` to `_WARMERS`; import + register `missed_punch_out.router`.
- **Modify** `src/zira_dashboard/static/footer.js` — add the `.mpo-*` badge + modal block.
- **Modify** `src/zira_dashboard/static/footer.css` — minimal `.mpo-*` rule for the inline time input (reuse `.late-*` otherwise).
- **Create** `tests/test_missed_punch_out.py` — pure-logic + run_close tests (no DB).
- **Create** `tests/test_missed_punch_out_db.py` — DB layer tests (Postgres-gated).
- **Create** `tests/test_missed_punch_out_routes.py` — route tests (Postgres-gated, mocked `clock_out`).
- **Modify** `CHANGELOG.md` — add the "What's New" entry.

---

### Task 1: Schema — `missed_punch_out` table

**Files:**
- Modify: `src/zira_dashboard/_schema.py` (end of `SCHEMA_DDL`, after the `missing_wc_resolved` block ~line 844)
- Test: `tests/test_missed_punch_out_db.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_missed_punch_out_db.py`:

```python
"""Schema + DB layer for the missed-punch-out alert. Postgres-backed."""

import os
from datetime import datetime, timezone, timedelta

import pytest

from zira_dashboard import db
from zira_dashboard.shift_config import SITE_TZ

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)

ATT = 999500


@pytest.fixture(autouse=True)
def _clean():
    db.bootstrap_schema()
    db.execute("DELETE FROM missed_punch_out WHERE attendance_id = %s", (ATT,))
    yield
    db.execute("DELETE FROM missed_punch_out WHERE attendance_id = %s", (ATT,))


def test_table_round_trips():
    ci = datetime(2026, 6, 8, 14, 0, tzinfo=timezone.utc)
    midnight = datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    db.execute(
        "INSERT INTO missed_punch_out "
        "(attendance_id, employee_odoo_id, name, check_in, auto_closed_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (ATT, 42, "Jesus Moreno", ci, midnight),
    )
    rows = db.query(
        "SELECT employee_odoo_id, name, resolved_at FROM missed_punch_out "
        "WHERE attendance_id = %s", (ATT,))
    assert rows and rows[0]["employee_odoo_id"] == 42
    assert rows[0]["name"] == "Jesus Moreno"
    assert rows[0]["resolved_at"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test DATABASE_URL=$DATABASE_URL .venv/bin/python -m pytest tests/test_missed_punch_out_db.py::test_table_round_trips -v`
Expected: FAIL (`relation "missed_punch_out" does not exist`) — or SKIP if no local `DATABASE_URL` (then CI is the gate; proceed).

- [ ] **Step 3: Add the table to the schema**

In `src/zira_dashboard/_schema.py`, immediately before the closing `"""` that ends `SCHEMA_DDL` (right after the `missing_wc_resolved` table block), add:

```sql

CREATE TABLE IF NOT EXISTS missed_punch_out (
  attendance_id    BIGINT PRIMARY KEY,
  employee_odoo_id BIGINT NOT NULL,
  name             TEXT,
  check_in         TIMESTAMPTZ NOT NULL,
  auto_closed_at   TIMESTAMPTZ NOT NULL,
  corrected_at     TIMESTAMPTZ,
  resolved_at      TIMESTAMPTZ,
  flagged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test DATABASE_URL=$DATABASE_URL .venv/bin/python -m pytest tests/test_missed_punch_out_db.py::test_table_round_trips -v`
Expected: PASS (or SKIP locally).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/_schema.py tests/test_missed_punch_out_db.py
git commit -m "feat(missed-punch): add missed_punch_out table"
```

---

### Task 2: Pure detection + label helpers in `missed_punch_out.py`

**Files:**
- Create: `src/zira_dashboard/missed_punch_out.py`
- Test: `tests/test_missed_punch_out.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_missed_punch_out.py`:

```python
"""Pure logic for the missed-punch-out alert (no DB/Odoo)."""

from datetime import date, datetime, timezone

from zira_dashboard import missed_punch_out as mpo
from zira_dashboard.shift_config import SITE_TZ


def _iso(y, m, d, hh, mm):
    """A UTC ISO string the way odoo_client.fetch_open_attendances emits."""
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc).isoformat()


def test_overdue_closures_flags_only_prior_day():
    today = date(2026, 6, 9)
    # 18:00 UTC on 6/8 == 13:00 site-local on 6/8 (prior day) -> overdue.
    rows = [
        {"att_id": 1, "employee_odoo_id": 10, "check_in": _iso(2026, 6, 8, 18, 0)},
        # 15:00 UTC on 6/9 == 10:00 site-local on 6/9 (today) -> NOT overdue.
        {"att_id": 2, "employee_odoo_id": 20, "check_in": _iso(2026, 6, 9, 15, 0)},
    ]
    out = mpo.overdue_closures(rows, today)
    assert [c["att_id"] for c in out] == [1]
    c = out[0]
    assert c["employee_odoo_id"] == 10
    # midnight ending the check-in day (6/8) == 6/9 00:00 site-local.
    assert c["midnight"] == datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    assert c["check_in"] == rows[0]["check_in"]


def test_overdue_closures_uses_site_local_date():
    # 04:00 UTC on 6/9 == 23:00 site-local on 6/8 (prior day) -> overdue,
    # even though the UTC date is already 6/9.
    today = date(2026, 6, 9)
    rows = [{"att_id": 5, "employee_odoo_id": 30, "check_in": _iso(2026, 6, 9, 4, 0)}]
    out = mpo.overdue_closures(rows, today)
    assert len(out) == 1
    assert out[0]["midnight"] == datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)


def test_overdue_closures_skips_bad_or_missing_check_in():
    today = date(2026, 6, 9)
    rows = [
        {"att_id": 7, "employee_odoo_id": 40, "check_in": None},
        {"att_id": 8, "employee_odoo_id": 41, "check_in": "not-a-date"},
    ]
    assert mpo.overdue_closures(rows, today) == []


def test_check_in_label_includes_date_in_site_local():
    label = mpo._check_in_label(_iso(2026, 6, 8, 18, 0))  # 13:00 local, Monday
    assert label == "1:00 PM Mon Jun 8"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_missed_punch_out.py -v`
Expected: FAIL (`No module named 'zira_dashboard.missed_punch_out'`).

- [ ] **Step 3: Create the module with the pure helpers**

Create `src/zira_dashboard/missed_punch_out.py`:

```python
"""Missed-punch-out alert: detect attendances left open past their day,
shape the badge/modal rows, and record/resolve flags.

The warmer (app._tick_missed_punch_out -> run_close) closes overdue Odoo
attendances at the midnight ending their check-in day and records each here;
the badge endpoint then does local reads only — no Odoo on the hot path.
Mirrors missing_wc.py.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, time as _time, timedelta, timezone

from .shift_config import SITE_TZ

_log = logging.getLogger(__name__)


def _parse_check_in(value):
    """ISO-8601 string (or datetime) -> tz-aware datetime, or None on bad input."""
    if not value:
        return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
    else:
        dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def overdue_closures(open_rows: list[dict], today) -> list[dict]:
    """Pure: open attendance rows + today's site-local date -> the ones whose
    check-in (site-local) was on a day BEFORE today, each with the midnight
    ending its check-in day. Rows checked in today (normal in-progress shifts)
    and rows with bad/missing check-in are skipped."""
    out: list[dict] = []
    for r in open_rows:
        dt = _parse_check_in(r.get("check_in"))
        if dt is None:
            continue
        local_date = dt.astimezone(SITE_TZ).date()
        if local_date >= today:
            continue
        midnight = datetime.combine(local_date + timedelta(days=1), _time.min,
                                    tzinfo=SITE_TZ)
        out.append({
            "att_id": r.get("att_id"),
            "employee_odoo_id": r.get("employee_odoo_id"),
            "check_in": r.get("check_in"),
            "midnight": midnight,
        })
    return out


def _check_in_label(value) -> str:
    """ISO string or datetime -> 'H:MM AM/PM Ddd Mon D' in site-local, '' on bad input."""
    dt = _parse_check_in(value)
    if dt is None:
        return ""
    local = dt.astimezone(SITE_TZ)
    fmt = "%#I:%M %p %a %b %#d" if os.name == "nt" else "%-I:%M %p %a %b %-d"
    return local.strftime(fmt)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_missed_punch_out.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/missed_punch_out.py tests/test_missed_punch_out.py
git commit -m "feat(missed-punch): pure overdue_closures + check_in label"
```

---

### Task 3: DB layer — `record_close`, `current_rows`, `get_unresolved`, `correct`

**Files:**
- Modify: `src/zira_dashboard/missed_punch_out.py`
- Test: `tests/test_missed_punch_out_db.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_missed_punch_out_db.py`:

```python
from zira_dashboard import missed_punch_out as mpo


def test_record_close_is_idempotent():
    ci = datetime(2026, 6, 8, 14, 0, tzinfo=timezone.utc)
    midnight = datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    mpo.record_close(ATT, 42, ci.isoformat(), midnight)
    mpo.record_close(ATT, 42, ci.isoformat(), midnight)  # ON CONFLICT DO NOTHING
    rows = db.query(
        "SELECT count(*) AS n FROM missed_punch_out WHERE attendance_id = %s", (ATT,))
    assert rows[0]["n"] == 1


def test_current_rows_shapes_unresolved_only():
    ci = datetime(2026, 6, 8, 18, 0, tzinfo=timezone.utc)  # 13:00 local
    midnight = datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    mpo.record_close(ATT, 42, ci.isoformat(), midnight)
    rows = [r for r in mpo.current_rows() if r["attendance_id"] == ATT]
    assert len(rows) == 1
    assert rows[0]["check_in_label"] == "1:00 PM Mon Jun 8"
    assert rows[0]["check_in_date"] == "2026-06-08"
    # After correction it drops out.
    mpo.correct(ATT, datetime(2026, 6, 8, 16, 30, tzinfo=SITE_TZ))
    assert not [r for r in mpo.current_rows() if r["attendance_id"] == ATT]


def test_get_unresolved_then_correct():
    ci = datetime(2026, 6, 8, 14, 0, tzinfo=timezone.utc)
    midnight = datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    mpo.record_close(ATT, 42, ci.isoformat(), midnight)
    row = mpo.get_unresolved(ATT)
    assert row and row["employee_odoo_id"] == 42
    mpo.correct(ATT, datetime(2026, 6, 8, 16, 30, tzinfo=SITE_TZ))
    assert mpo.get_unresolved(ATT) is None  # resolved -> not returned


def test_record_close_falls_back_to_id_name_when_unknown():
    ci = datetime(2026, 6, 8, 14, 0, tzinfo=timezone.utc)
    midnight = datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    mpo.record_close(ATT, 987654, ci.isoformat(), midnight)  # not in people
    rows = db.query("SELECT name FROM missed_punch_out WHERE attendance_id = %s", (ATT,))
    assert rows[0]["name"] == "#987654"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test DATABASE_URL=$DATABASE_URL .venv/bin/python -m pytest tests/test_missed_punch_out_db.py -v`
Expected: FAIL (`module 'zira_dashboard.missed_punch_out' has no attribute 'record_close'`) — or SKIP locally.

- [ ] **Step 3: Add the DB layer**

Append to `src/zira_dashboard/missed_punch_out.py`:

```python
def _name_for(employee_odoo_id) -> str:
    """Person's name from `people`, or '#<odoo_id>' when not mapped."""
    from . import db
    rows = db.query(
        "SELECT name FROM people WHERE odoo_id = %s", (int(employee_odoo_id),))
    if rows and rows[0].get("name"):
        return rows[0]["name"]
    return f"#{employee_odoo_id}"


def record_close(attendance_id, employee_odoo_id, check_in, auto_closed_at) -> None:
    """Flag an attendance auto-closed at midnight. Idempotent (PK conflict ->
    no-op), so re-running the warmer never duplicates a row. `check_in` may be
    an ISO string or datetime; `auto_closed_at` is the midnight datetime."""
    from . import db
    db.execute(
        "INSERT INTO missed_punch_out "
        "(attendance_id, employee_odoo_id, name, check_in, auto_closed_at) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (attendance_id) DO NOTHING",
        (int(attendance_id), int(employee_odoo_id),
         _name_for(employee_odoo_id), check_in, auto_closed_at),
    )


def _shape_row(r: dict) -> dict:
    ci = r.get("check_in")
    check_in_date = None
    if hasattr(ci, "astimezone"):
        check_in_date = ci.astimezone(SITE_TZ).date().isoformat()
    return {
        "attendance_id": r.get("attendance_id"),
        "employee_odoo_id": r.get("employee_odoo_id"),
        "name": r.get("name") or f"#{r.get('employee_odoo_id')}",
        "check_in_label": _check_in_label(ci),
        "check_in_date": check_in_date,
    }


def current_rows() -> list[dict]:
    """Badge/modal payload: unresolved flags, newest first. All local reads."""
    from . import db
    rows = db.query(
        "SELECT attendance_id, employee_odoo_id, name, check_in, auto_closed_at "
        "FROM missed_punch_out WHERE resolved_at IS NULL "
        "ORDER BY check_in DESC")
    return [_shape_row(r) for r in rows]


def get_unresolved(attendance_id) -> dict | None:
    """The unresolved flag row (carries check_in + auto_closed_at for the route's
    bounds check), or None if unknown or already resolved."""
    from . import db
    rows = db.query(
        "SELECT attendance_id, employee_odoo_id, name, check_in, auto_closed_at "
        "FROM missed_punch_out WHERE attendance_id = %s AND resolved_at IS NULL",
        (int(attendance_id),))
    return rows[0] if rows else None


def correct(attendance_id, corrected_ts) -> None:
    """Mark a flag resolved with the manager-entered punch-out time."""
    from . import db
    db.execute(
        "UPDATE missed_punch_out SET corrected_at = %s, resolved_at = now() "
        "WHERE attendance_id = %s",
        (corrected_ts, int(attendance_id)),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test DATABASE_URL=$DATABASE_URL .venv/bin/python -m pytest tests/test_missed_punch_out_db.py -v`
Expected: PASS (5 tests) — or SKIP locally (CI is the gate).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/missed_punch_out.py tests/test_missed_punch_out_db.py
git commit -m "feat(missed-punch): record/current/get/correct DB layer"
```

---

### Task 4: Close orchestration — `run_close`

**Files:**
- Modify: `src/zira_dashboard/missed_punch_out.py`
- Test: `tests/test_missed_punch_out.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_missed_punch_out.py`:

```python
from zira_dashboard import odoo_client


def test_run_close_closes_only_prior_day_and_records(monkeypatch):
    today = date(2026, 6, 9)
    monkeypatch.setattr(odoo_client, "fetch_open_attendances", lambda: [
        {"att_id": 1, "employee_odoo_id": 10, "check_in": _iso(2026, 6, 8, 18, 0)},
        {"att_id": 2, "employee_odoo_id": 20, "check_in": _iso(2026, 6, 9, 15, 0)},
    ])
    closed, recorded = [], []
    monkeypatch.setattr(odoo_client, "clock_out",
                        lambda att, ts: closed.append((att, ts)))
    monkeypatch.setattr(mpo, "record_close",
                        lambda att, emp, ci, mid: recorded.append((att, emp, mid)))

    n = mpo.run_close(today)

    assert n == 1
    assert closed == [(1, datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ))]
    assert recorded == [(1, 10, datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ))]


def test_run_close_noop_when_all_today(monkeypatch):
    today = date(2026, 6, 9)
    monkeypatch.setattr(odoo_client, "fetch_open_attendances", lambda: [
        {"att_id": 2, "employee_odoo_id": 20, "check_in": _iso(2026, 6, 9, 15, 0)},
    ])
    monkeypatch.setattr(odoo_client, "clock_out",
                        lambda att, ts: (_ for _ in ()).throw(AssertionError("should not close")))
    monkeypatch.setattr(mpo, "record_close", lambda *a: None)
    assert mpo.run_close(today) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_missed_punch_out.py -k run_close -v`
Expected: FAIL (`module 'zira_dashboard.missed_punch_out' has no attribute 'run_close'`).

- [ ] **Step 3: Add `run_close`**

Append to `src/zira_dashboard/missed_punch_out.py`:

```python
def run_close(today) -> int:
    """One sweep: close every open attendance whose check-in was on a prior
    day at that day's midnight, and flag each. `today` is the site-local date.
    Returns how many were closed. Owns the Odoo read + writes (off the hot
    path; called by the warmer). One bad record never kills the sweep."""
    from . import odoo_client
    open_rows = odoo_client.fetch_open_attendances()
    closures = overdue_closures(open_rows, today)
    n = 0
    for c in closures:
        try:
            odoo_client.clock_out(c["att_id"], c["midnight"])
            record_close(c["att_id"], c["employee_odoo_id"],
                         c["check_in"], c["midnight"])
            n += 1
        except Exception as e:  # noqa: BLE001 — one record never kills the sweep
            _log.warning("missed-punch close failed for att %s: %s",
                         c.get("att_id"), e)
    return n
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_missed_punch_out.py -v`
Expected: PASS (6 tests total).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/missed_punch_out.py tests/test_missed_punch_out.py
git commit -m "feat(missed-punch): run_close sweep (close overdue + flag)"
```

---

### Task 5: Routes — `GET /api/missed-punch-out`, `POST /missed-punch-out/correct`

**Files:**
- Create: `src/zira_dashboard/routes/missed_punch_out.py`
- Modify: `src/zira_dashboard/app.py:23-46` (import) and `app.py:343` (register)
- Test: `tests/test_missed_punch_out_routes.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_missed_punch_out_routes.py`:

```python
"""Missed-punch-out routes: GET shape, correct (mocked Odoo) + validation."""

import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard import db, missed_punch_out as mpo, odoo_client
from zira_dashboard.shift_config import SITE_TZ

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)

client = TestClient(app)
ATT = 999600


@pytest.fixture(autouse=True)
def _seed():
    db.bootstrap_schema()
    db.execute("DELETE FROM missed_punch_out WHERE attendance_id = %s", (ATT,))
    # check-in 13:00 local on 6/8; auto-closed at midnight 6/9.
    ci = datetime(2026, 6, 8, 18, 0, tzinfo=timezone.utc)
    midnight = datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    mpo.record_close(ATT, 42, ci.isoformat(), midnight)
    yield
    db.execute("DELETE FROM missed_punch_out WHERE attendance_id = %s", (ATT,))


def test_get_returns_count_and_rows():
    r = client.get("/api/missed-punch-out")
    assert r.status_code == 200
    body = r.json()
    assert set(["count", "rows"]) <= set(body.keys())
    row = next(x for x in body["rows"] if x["attendance_id"] == ATT)
    assert row["check_in_label"] == "1:00 PM Mon Jun 8"
    assert row["check_in_date"] == "2026-06-08"


def test_correct_rewrites_check_out_and_resolves(monkeypatch):
    calls = {}
    monkeypatch.setattr(odoo_client, "clock_out",
                        lambda att, ts: calls.update(att=att, ts=ts))
    r = client.post("/missed-punch-out/correct",
                    json={"attendance_id": ATT, "time": "16:30"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls["att"] == ATT
    assert calls["ts"] == datetime(2026, 6, 8, 16, 30, tzinfo=SITE_TZ)
    assert mpo.get_unresolved(ATT) is None


def test_correct_rejects_time_before_check_in(monkeypatch):
    monkeypatch.setattr(odoo_client, "clock_out",
                        lambda att, ts: (_ for _ in ()).throw(AssertionError("no write")))
    r = client.post("/missed-punch-out/correct",
                    json={"attendance_id": ATT, "time": "06:00"})  # before 13:00 check-in
    assert r.status_code == 400
    assert mpo.get_unresolved(ATT) is not None  # still flagged


def test_correct_rejects_bad_time(monkeypatch):
    r = client.post("/missed-punch-out/correct",
                    json={"attendance_id": ATT, "time": "nope"})
    assert r.status_code == 400


def test_correct_unknown_id_404():
    r = client.post("/missed-punch-out/correct",
                    json={"attendance_id": 123123, "time": "16:30"})
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test DATABASE_URL=$DATABASE_URL .venv/bin/python -m pytest tests/test_missed_punch_out_routes.py -v`
Expected: FAIL (404 on `/api/missed-punch-out` — route not registered) — or SKIP locally.

- [ ] **Step 3: Create the route module**

Create `src/zira_dashboard/routes/missed_punch_out.py`:

```python
"""Missed-punch-out alert endpoints: badge/modal read + time correction.

Mirrors routes/missing_wc.py. The READ is a cheap local DB read. The correct
endpoint validates the entered time is after clock-in and on the check-in day,
rewrites the Odoo hr.attendance check_out (exactly, no rounding) via
odoo_client.clock_out, then resolves the flag.
"""
from __future__ import annotations

from datetime import datetime, time as _time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/api/missed-punch-out")
def missed_punch_out_json():
    """Badge/modal snapshot: {count, rows}. All local reads."""
    from .. import missed_punch_out
    try:
        rows = missed_punch_out.current_rows()
    except Exception:
        rows = []
    return JSONResponse({"count": len(rows), "rows": rows})


@router.post("/missed-punch-out/correct")
async def missed_punch_out_correct(request: Request):
    """Rewrite a flagged attendance's check_out to the entered time.

    Body (JSON): {attendance_id, time}  where time is "HH:MM" (24-hour).
    """
    from .. import missed_punch_out, odoo_client
    from ..shift_config import SITE_TZ
    body = await request.json()
    try:
        att_id = int(body.get("attendance_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad attendance_id"}, status_code=400)
    raw = str(body.get("time") or "").strip()
    try:
        hh, mm = raw.split(":")
        parsed = _time(int(hh), int(mm))
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "bad time"}, status_code=400)

    row = missed_punch_out.get_unresolved(att_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

    check_in = row["check_in"].astimezone(SITE_TZ)
    midnight = row["auto_closed_at"].astimezone(SITE_TZ)
    corrected = datetime.combine(check_in.date(), parsed, tzinfo=SITE_TZ)
    if not (check_in < corrected <= midnight):
        return JSONResponse(
            {"ok": False, "error": "time must be after clock-in and on the clock-in day"},
            status_code=400)

    try:
        odoo_client.clock_out(att_id, corrected)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    missed_punch_out.correct(att_id, corrected)
    return JSONResponse({"ok": True})
```

- [ ] **Step 4: Register the router in `app.py`**

In `src/zira_dashboard/app.py`, add `missed_punch_out` to the `from .routes import (...)` block (after `missing_wc,` on line 33):

```python
    missing_wc,
    missed_punch_out,
```

Then after `app.include_router(missing_wc.router)` (line 343), add:

```python
app.include_router(missed_punch_out.router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test DATABASE_URL=$DATABASE_URL .venv/bin/python -m pytest tests/test_missed_punch_out_routes.py -v`
Expected: PASS (5 tests) — or SKIP locally.

Also sanity-check the app still imports (catches the import/registration edit):
Run: `ZIRA_API_KEY=test .venv/bin/python -c "import zira_dashboard.app"`
Expected: no error.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/missed_punch_out.py src/zira_dashboard/app.py tests/test_missed_punch_out_routes.py
git commit -m "feat(missed-punch): GET + correct routes; register router"
```

---

### Task 6: Warmer tick — `_tick_missed_punch_out`

**Files:**
- Modify: `src/zira_dashboard/app.py` (new tick near `_tick_missing_wc` ~line 145; add to `_WARMERS` ~line 170)
- Test: `tests/test_missed_punch_out.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_missed_punch_out.py`:

```python
import asyncio

from zira_dashboard import app as app_module


def test_tick_calls_run_close_with_site_local_today(monkeypatch):
    seen = {}
    monkeypatch.setattr(app_module.missed_punch_out, "run_close",
                        lambda today: seen.setdefault("today", today) or 0)
    asyncio.run(app_module._tick_missed_punch_out())
    assert seen["today"] == datetime.now(SITE_TZ).date()
```

Add the needed import at the top of `tests/test_missed_punch_out.py` if not present:

```python
from datetime import datetime  # already imported alongside date, timezone — extend the existing line
```

(The file's first import line is `from datetime import date, datetime, timezone` — `datetime` is already there; no change needed.)

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_missed_punch_out.py::test_tick_calls_run_close_with_site_local_today -v`
Expected: FAIL (`module 'zira_dashboard.app' has no attribute '_tick_missed_punch_out'`).

- [ ] **Step 3: Add the tick + register it**

In `src/zira_dashboard/app.py`, after the `_tick_missing_wc` function (ends ~line 153), add:

```python
async def _tick_missed_punch_out():
    """Close any attendance still open from a prior day at that day's midnight
    and flag it for the Missed-Punch-Out alert. Cadence doesn't affect the close
    time (it's computed from the check-in day, not 'now')."""
    from . import missed_punch_out, shift_config
    today = datetime.now(shift_config.SITE_TZ).date()
    await asyncio.to_thread(missed_punch_out.run_close, today)
```

Add the module to the top-level `from . import ...` so the test's `app_module.missed_punch_out` resolves. Find the existing top-of-file imports; there is a `from . import db` (line 22). Add directly below it:

```python
from . import missed_punch_out
```

Then add to the `_WARMERS` list (after the `("missing WC", _tick_missing_wc, 180),` line ~170):

```python
    ("missed punch-out", _tick_missed_punch_out, 60),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_missed_punch_out.py -v`
Expected: PASS (7 tests total).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/app.py tests/test_missed_punch_out.py
git commit -m "feat(missed-punch): warmer tick closing overdue at midnight"
```

---

### Task 7: Badge + modal in `footer.js` / `footer.css`

**Files:**
- Modify: `src/zira_dashboard/static/footer.js` (append a new self-invoking block after the Missing-WC block, ~line 850)
- Modify: `src/zira_dashboard/static/footer.css` (append `.mpo-*` rule)

There is no JS unit harness here; verify by `py_compile` of the app (static files ship as-is) and a manual smoke note. **First, guard against a stale contract test:**

- [ ] **Step 1: Check for tests asserting footer behavior**

Run: `grep -rn "footer.js\|late-nav-badge\|mwc-nav-badge\|No Work Center" tests/`
Expected: note any test that asserts on `footer.js` contents. (At time of writing there is none; if one exists, update it in this task to also tolerate the new block.)

- [ ] **Step 2: Append the badge + modal block to `footer.js`**

At the end of `src/zira_dashboard/static/footer.js`, after the Missing-Work-Center IIFE, add:

```javascript

// Global "Missed Punch Out" badge + modal — present on every page.
// Mirrors the Missing-Work-Center badge/modal and reuses its .late-* styling.
// Each row takes a time the manager enters; saving rewrites that attendance's
// check_out (from midnight to the entered time) and clears the row.
(function () {
  var navBadge = null;
  var modal = null;
  var data = null;
  var ENDPOINT = '/api/missed-punch-out';

  function settingsLink() {
    return document.querySelector('header nav a[href="/settings"]')
        || document.querySelector('header.app nav a[href="/settings"]');
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }

  function refreshCount() {
    fetch(ENDPOINT).then(function (r) { return r.json(); }).then(function (d) {
      data = d;
      injectOrUpdateBadge();
    }).catch(function () {});
  }

  function injectOrUpdateBadge() {
    if (!data || !data.count) {
      if (navBadge) { navBadge.remove(); navBadge = null; }
      return;
    }
    var anchor = settingsLink();
    if (!anchor) return;
    if (!navBadge) {
      navBadge = document.createElement('a');
      navBadge.href = '#';
      navBadge.className = 'late-nav-badge mpo-nav-badge';
      navBadge.title = 'Employees auto-clocked-out at midnight — click to set the real time';
      navBadge.addEventListener('click', function (e) { e.preventDefault(); openModal(); });
      anchor.parentNode.insertBefore(navBadge, anchor.nextSibling);
    }
    navBadge.innerHTML = '⏰ <span class="cnt">' + data.count + '</span> Missed Punch Out';
    navBadge.style.display = '';
  }

  function openModal() {
    closeModal();
    modal = document.createElement('div');
    modal.className = 'late-modal mpo-modal';
    modal.innerHTML = ''
      + '<div class="late-backdrop"></div>'
      + '<div class="late-card" role="dialog" aria-modal="true" aria-label="Missed punch out">'
      + '  <div class="late-head"><h3>Missed Punch Out</h3>'
      + '    <button type="button" class="late-close" aria-label="Close">×</button></div>'
      + '  <div class="late-body">Loading…</div>'
      + '</div>';
    document.body.appendChild(modal);
    document.documentElement.style.overflow = 'hidden';
    modal.querySelector('.late-backdrop').addEventListener('click', closeModal);
    modal.querySelector('.late-close').addEventListener('click', closeModal);
    document.addEventListener('keydown', escClose);
    fetch(ENDPOINT).then(function (r) { return r.json(); }).then(renderModal);
  }

  function closeModal() {
    if (modal) { modal.remove(); modal = null; }
    document.documentElement.style.overflow = '';
    document.removeEventListener('keydown', escClose);
  }

  function escClose(e) { if (e.key === 'Escape') closeModal(); }

  function postJson(url, payload) {
    return fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    }).then(function (r) { return r.json(); });
  }

  function finishRow(li, label, ok) {
    var status = li.querySelector('.late-status');
    status.textContent = label;
    status.hidden = false;
    if (ok) {
      li.querySelectorAll('button, input').forEach(function (el) { el.disabled = true; });
      li.style.opacity = '0.6';
      refreshCount();
    }
  }

  function wireActions(body) {
    body.querySelectorAll('.mpo-save-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var li = btn.closest('.late-item');
        var input = li.querySelector('.mpo-time-input');
        if (!input.value) { input.focus(); return; }
        btn.disabled = true;
        postJson('/missed-punch-out/correct', {
          attendance_id: parseInt(li.getAttribute('data-att'), 10),
          time: input.value,
        }).then(function (res) {
          if (res && res.ok) {
            finishRow(li, 'Corrected ✓', true);
          } else {
            finishRow(li, (res && res.error) || 'Error', false);
            btn.disabled = false;
          }
        }).catch(function () { finishRow(li, 'Error', false); btn.disabled = false; });
      });
    });
  }

  function renderModal(d) {
    data = d;
    if (!modal) return;
    var body = modal.querySelector('.late-body');
    var rows = (d && d.rows) || [];
    if (!rows.length) {
      body.innerHTML = '<p class="late-help">No missed punch-outs. Anyone left clocked in '
        + 'overnight is auto-clocked-out at midnight and appears here so you can set the '
        + 'time they actually left.</p>';
      return;
    }
    var html = '<ul class="late-list">';
    rows.forEach(function (item) {
      html += '<li class="late-item" data-att="' + item.attendance_id + '">'
        + '<span class="late-item-name">' + escapeHtml(item.name) + '</span>'
        + '<span class="late-item-mins">clocked in ' + escapeHtml(item.check_in_label)
        + ' · auto-closed at midnight</span>'
        + '<div class="late-reason-row">'
        + '  <label>Actually left at '
        + '    <input type="time" class="mpo-time-input" />'
        + '  </label>'
        + '  <button type="button" class="mpo-save-btn">Save</button>'
        + '</div>'
        + '<span class="late-status" hidden></span>'
        + '</li>';
    });
    html += '</ul>';
    body.innerHTML = html;
    wireActions(body);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', refreshCount);
  } else {
    refreshCount();
  }
  setInterval(refreshCount, 60000);
})();
```

- [ ] **Step 3: Append the time-input styling to `footer.css`**

At the end of `src/zira_dashboard/static/footer.css`, add:

```css
  .mpo-time-input {
    margin-left: 0.4rem;
    padding: 0.2rem 0.4rem;
    border: 1px solid var(--border, #d1d5db);
    border-radius: 4px;
    font: inherit;
  }
  .mpo-save-btn {
    margin-left: 0.5rem;
    padding: 0.25rem 0.6rem;
    border: 1px solid #b91c1c;
    border-radius: 4px;
    background: #b91c1c;
    color: white;
    cursor: pointer;
  }
  .mpo-save-btn:hover { background: #991b1b; }
  .mpo-save-btn:disabled { opacity: 0.5; cursor: not-allowed; }
```

- [ ] **Step 4: Verify the app still imports and static files are well-formed**

Run: `ZIRA_API_KEY=test .venv/bin/python -c "import zira_dashboard.app"`
Expected: no error.
Run: `node --check src/zira_dashboard/static/footer.js 2>/dev/null && echo "JS OK" || echo "node not available — skip"`
Expected: `JS OK` (or the skip note if `node` isn't installed; the syntax mirrors the verified mwc block).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/static/footer.js src/zira_dashboard/static/footer.css
git commit -m "feat(missed-punch): nav badge + modal with time correction"
```

---

### Task 8: Changelog + full-suite verification

**Files:**
- Modify: `CHANGELOG.md` (top, under `# What's New`)

- [ ] **Step 1: Run the full test suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass (DB-gated missed-punch tests SKIP locally; everything else green). Note the pass/skip counts.

- [ ] **Step 2: Lint the new/changed Python**

Run: `.venv/bin/python -m ruff check src/zira_dashboard/missed_punch_out.py src/zira_dashboard/routes/missed_punch_out.py src/zira_dashboard/app.py`
Expected: no errors. Fix any reported issues.

- [ ] **Step 3: Add the changelog entry**

In `CHANGELOG.md`, add a new dated section at the top (under the intro line, above the current top date). Use today's date and the current deploy time. Example:

```markdown
## 2026-06-09

### <deploy time, e.g. 9:00 AM>

- **New "⏰ Missed Punch Out" alert + automatic midnight clock-out.** If anyone is still clocked in when the day ends, they're now **automatically clocked out at midnight** (the end of that day) instead of accruing hours forever — this also cleaned up a stale open punch (Jesus Moreno). Each auto-clock-out shows up as a nav badge + modal on **every page** (same look as the Late and No-Work-Center alerts): click it, type the time the employee **actually** left, and it rewrites that day's attendance record so it ends at your time instead of midnight (stored exactly, no rounding). The badge stays until every missed punch is corrected — there's no dismiss. Covers **everyone** left clocked in across midnight (not just hourly), since an open record corrupts hours and production for any employee. A background job (~every minute) does the close off the hot path; the badge polls a cheap local endpoint with no Odoo call on page load. Spec → plan → reviewed subagent tasks; design at `docs/superpowers/specs/2026-06-09-missed-punch-out-design.md`.
```

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): missed punch-out alert + midnight auto-clock-out"
```

---

## Self-Review

**1. Spec coverage** (each spec section → task):
- Detection + auto-close warmer tick → Tasks 4 (`run_close`) + 6 (`_tick_missed_punch_out`).
- "Anyone covered, no wage filter" → `current_rows`/`overdue_closures` apply no wage_type filter (Tasks 2–3); covered by tests (no people-wage join).
- Close at the midnight ending the **check-in** day (incl. Jesus backlog) → `overdue_closures` midnight math + `test_overdue_closures_uses_site_local_date` / `test_run_close_closes_only_prior_day_and_records` (Tasks 2, 4).
- `missed_punch_out` table → Task 1.
- Time-only, stored-exactly correction with bounds `check_in < t ≤ midnight` → Task 5 route + `test_correct_*`.
- No dismiss path → no dismiss route/state exists (Tasks 3, 5).
- `GET /api/missed-punch-out` does no Odoo I/O → reads DB only (Task 5); the tick owns Odoo (Tasks 4, 6).
- Badge + modal on every page, mirroring Missing-WC → Task 7.
- Idempotent close → `record_close` ON CONFLICT + `test_record_close_is_idempotent` (Tasks 3).
- Acceptance criteria (today untouched, prior-day closed, rewrite verified, bad time rejected) → covered across Tasks 2/4/5 tests.

**2. Placeholder scan:** No "TBD"/"add error handling"/etc.; every code step shows complete code. The only deploy-time blank is the changelog timestamp (intentional — set at ship time).

**3. Type consistency:** `overdue_closures` emits dicts with keys `att_id, employee_odoo_id, check_in, midnight`; `run_close` consumes exactly those. `record_close(attendance_id, employee_odoo_id, check_in, auto_closed_at)` matches its callers in `run_close` and the tests. `current_rows()` rows expose `attendance_id, employee_odoo_id, name, check_in_label, check_in_date`; the route returns them as-is and `footer.js` reads `attendance_id`, `name`, `check_in_label`. `get_unresolved` returns `check_in` + `auto_closed_at` (tz-aware), which the route converts via `.astimezone(SITE_TZ)`. `correct(attendance_id, corrected_ts)` matches the route call. Consistent.
