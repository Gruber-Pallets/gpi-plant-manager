# Missing-Work-Center Alert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Late/Absence-report-style nav badge + modal (via the shared `footer.js`) that flags Odoo `hr.attendance` records from the last 14 days with no kiosk work-center tag (hourly employees), and lets a manager Assign a work center (writing it + the resolved department onto the attendance) or Dismiss the record.

**Architecture:** A background warmer caches the Odoo query off the hot path; a cheap cached endpoint feeds the badge. Pure shaping (hourly filter, suppression, label) lives in `missing_wc.py` (mirrors `late_report.py`); routes mirror `routes/late_report.py`; the badge/modal mirrors the late block in `footer.js` and reuses its `.late-*` CSS.

**Tech Stack:** Python 3.12, FastAPI, psycopg2 + Postgres, vanilla JS (footer.js), pytest. Odoo via XML-RPC (`odoo_client`).

**Source spec:** [docs/superpowers/specs/2026-06-04-missing-work-center-alert-design.md](../specs/2026-06-04-missing-work-center-alert-design.md)

---

## Running tests (read first)

Same split as the rest of this repo:
- **No-DB unit tests** run locally with `.\.venv\Scripts\python.exe -m pytest <file> -v` (the import chain needs no live DB; the standard CI env like `ZIRA_API_KEY` must be present, as for every test here).
- **Postgres-backed tests** are gated `pytest.mark.skipif(not os.environ.get("DATABASE_URL"))`; **`DATABASE_URL` is NOT set locally and must NOT be set** (it points at prod). They show as SKIPPED locally and run for real in CI (GitHub Actions Postgres). **CI is the red/green authority for DB-gated paths.**

Local toolchain: `.\.venv\Scripts\ruff.exe check <files>`, `.\.venv\Scripts\python.exe -m py_compile <files>`, `.\.venv\Scripts\python.exe -m pytest <file> -v`. Use PowerShell (Bash mangles `.\.venv\Scripts\` backslash paths). For `git commit`, prefer a simple single-line `-m "..."` (no inner double-quotes / angle brackets).

**Lesson carried over:** before editing `footer.js`, `git grep` the `tests/` tree for anything asserting footer / nav-badge behavior, and update it in the same task (DB-gated render tests only surface in CI).

---

### Task 1: Schema — `missing_wc_cache` + `missing_wc_resolved`

**Files:**
- Modify: `src/zira_dashboard/_schema.py` (append to `SCHEMA_DDL`, before the closing `"""`)
- Test: `tests/test_missing_wc_schema.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_missing_wc_schema.py`:

```python
"""Schema for the missing-work-center alert. Postgres-backed."""

import os

import pytest

from zira_dashboard import db

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)


def test_cache_table_round_trips():
    db.bootstrap_schema()
    db.execute(
        "INSERT INTO missing_wc_cache (id, snapshot, refreshed_at) "
        "VALUES (1, %s::jsonb, now()) "
        "ON CONFLICT (id) DO UPDATE SET snapshot = EXCLUDED.snapshot",
        ('[{"att_id": 1}]',),
    )
    rows = db.query("SELECT snapshot FROM missing_wc_cache WHERE id = 1")
    assert rows and rows[0]["snapshot"] == [{"att_id": 1}]


def test_resolved_table_upserts():
    db.bootstrap_schema()
    db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = %s", (999001,))
    db.execute(
        "INSERT INTO missing_wc_resolved (attendance_id, action, name) "
        "VALUES (%s, 'dismissed', 'X') "
        "ON CONFLICT (attendance_id) DO UPDATE SET action = EXCLUDED.action",
        (999001,),
    )
    rows = db.query("SELECT action FROM missing_wc_resolved WHERE attendance_id = %s", (999001,))
    assert rows and rows[0]["action"] == "dismissed"
    db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = %s", (999001,))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_missing_wc_schema.py -v`
Expected (no local DB): 2 SKIPPED. (In CI it would FAIL on the missing tables.) Confirm 0 collection errors.

- [ ] **Step 3: Append the tables to `SCHEMA_DDL`**

In `src/zira_dashboard/_schema.py`, at the END of the `SCHEMA_DDL` string (before the closing `"""`):

```sql

-- Missing-work-center alert (2026-06-04). Cache of Odoo hr.attendance rows
-- (last 14 days) lacking a kiosk work-center tag, refreshed by a warmer; plus
-- a suppression table for records a manager has assigned or dismissed.
CREATE TABLE IF NOT EXISTS missing_wc_cache (
  id           INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  snapshot     JSONB NOT NULL DEFAULT '[]'::jsonb,
  refreshed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS missing_wc_resolved (
  attendance_id BIGINT PRIMARY KEY,
  action        TEXT NOT NULL CHECK (action IN ('assigned','dismissed')),
  name          TEXT,
  wc_name       TEXT,
  resolved_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_missing_wc_schema.py -v` (SKIPPED locally; CI runs it green). Verify ruff + py_compile:
`.\.venv\Scripts\ruff.exe check tests/test_missing_wc_schema.py` and
`.\.venv\Scripts\python.exe -c "from zira_dashboard._schema import SCHEMA_DDL; print('missing_wc_cache' in SCHEMA_DDL, 'missing_wc_resolved' in SCHEMA_DDL)"` → `True True`.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/_schema.py tests/test_missing_wc_schema.py
git commit -m "feat(missing-wc): add missing_wc_cache + missing_wc_resolved tables"
```

---

### Task 2: `odoo_client.fetch_attendances_missing_wc(since)`

**Files:**
- Modify: `src/zira_dashboard/odoo_client.py` (add the function near `get_current_attendance` / `clock_in`)
- Test: `tests/test_fetch_missing_wc.py` (new, no DB — monkeypatched)

- [ ] **Step 1: Write the failing test**

Create `tests/test_fetch_missing_wc.py`:

```python
"""odoo_client.fetch_attendances_missing_wc — shaping + WC-field guard (mocked)."""

from datetime import datetime, timezone

from zira_dashboard import odoo_client


def test_returns_shaped_rows_when_wc_field_configured(monkeypatch):
    monkeypatch.setattr(odoo_client, "_kiosk_wc_field", lambda: "x_kiosk_wc")
    captured = {}

    def fake_execute(model, method, *args, **kwargs):
        captured["model"] = model
        captured["method"] = method
        captured["domain"] = args[0]
        return [{
            "id": 55, "employee_id": [7, "Maria Lopez"],
            "check_in": "2026-06-02 11:58:00", "check_out": False,
        }]

    monkeypatch.setattr(odoo_client, "execute", fake_execute)
    since = datetime(2026, 5, 21, tzinfo=timezone.utc)
    rows = odoo_client.fetch_attendances_missing_wc(since)
    assert captured["model"] == "hr.attendance"
    assert captured["method"] == "search_read"
    # Domain includes the WC-field-empty clause.
    assert ("x_kiosk_wc", "=", False) in captured["domain"]
    assert rows == [{
        "att_id": 55, "employee_odoo_id": 7, "employee_name": "Maria Lopez",
        "check_in": rows[0]["check_in"], "check_out": None,
    }]
    assert rows[0]["check_in"]  # ISO string, non-empty


def test_returns_empty_when_wc_field_not_configured(monkeypatch):
    monkeypatch.setattr(odoo_client, "_kiosk_wc_field", lambda: None)
    called = {"execute": False}
    monkeypatch.setattr(odoo_client, "execute",
                        lambda *a, **k: called.__setitem__("execute", True) or [])
    rows = odoo_client.fetch_attendances_missing_wc(datetime.now(timezone.utc))
    assert rows == []
    assert called["execute"] is False  # never hits Odoo
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_fetch_missing_wc.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'fetch_attendances_missing_wc'`.

- [ ] **Step 3: Implement the function**

In `src/zira_dashboard/odoo_client.py`, add (after `get_current_attendance`):

```python
def fetch_attendances_missing_wc(since) -> list[dict]:
    """hr.attendance from `since` (a tz-aware datetime) with NO kiosk
    work-center tag. Returns
    [{att_id, employee_odoo_id, employee_name, check_in (ISO), check_out (ISO|None)}].

    Returns [] (and logs once) when the kiosk WC field isn't configured — with
    no WC field we can't tell tagged from untagged, so the alert stays dark
    rather than flagging every record."""
    import logging
    wc_field = _kiosk_wc_field()
    if not wc_field:
        logging.getLogger(__name__).warning(
            "ODOO_KIOSK_WC_FIELD not configured; missing-work-center alert disabled"
        )
        return []
    rows = execute(
        "hr.attendance", "search_read",
        [("check_in", ">=", _to_odoo_dt(since)), (wc_field, "=", False)],
        fields=["id", "employee_id", "check_in", "check_out"],
        order="check_in desc",
        limit=500,
    )
    out: list[dict] = []
    for r in rows:
        emp = r.get("employee_id")
        out.append({
            "att_id": r["id"],
            "employee_odoo_id": unwrap_m2o(emp),
            "employee_name": emp[1] if isinstance(emp, list) and len(emp) > 1 else None,
            "check_in": _odoo_dt_to_iso(r.get("check_in")),
            "check_out": _odoo_dt_to_iso(r.get("check_out")),
        })
    return out
```

(`_kiosk_wc_field`, `_to_odoo_dt`, `unwrap_m2o`, `_odoo_dt_to_iso`, `execute` all already exist in this module.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_fetch_missing_wc.py -v` → 2 passed.
`.\.venv\Scripts\ruff.exe check src/zira_dashboard/odoo_client.py tests/test_fetch_missing_wc.py` → clean.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/odoo_client.py tests/test_fetch_missing_wc.py
git commit -m "feat(missing-wc): odoo_client.fetch_attendances_missing_wc"
```

---

### Task 3: `missing_wc.py` — cache, suppression, pure shaping

**Files:**
- Create: `src/zira_dashboard/missing_wc.py`
- Test: `tests/test_missing_wc.py` (new — pure shaping + Postgres cache/resolve)

- [ ] **Step 1: Write the failing test**

Create `tests/test_missing_wc.py`:

```python
"""missing_wc: pure shaping (no DB) + cache/resolve round-trips (Postgres)."""

import os

import pytest

from zira_dashboard import missing_wc


# ---- pure shaping (no DB) ----

def _people():
    return {
        7: {"name": "Maria", "wage_type": "hourly", "active": True, "excluded": False},
        8: {"name": "Boss", "wage_type": "monthly", "active": True, "excluded": False},
        9: {"name": "Gone", "wage_type": "hourly", "active": False, "excluded": False},
    }


def test_shape_keeps_only_active_hourly_unresolved():
    cached = [
        {"att_id": 1, "employee_odoo_id": 7, "employee_name": "Maria", "check_in": "2026-06-02T11:58:00+00:00"},
        {"att_id": 2, "employee_odoo_id": 8, "employee_name": "Boss", "check_in": "2026-06-02T08:00:00+00:00"},
        {"att_id": 3, "employee_odoo_id": 9, "employee_name": "Gone", "check_in": "2026-06-02T07:00:00+00:00"},
        {"att_id": 4, "employee_odoo_id": 7, "employee_name": "Maria", "check_in": "2026-06-01T06:00:00+00:00"},
    ]
    rows = missing_wc.shape_rows(cached, _people(), resolved={4})
    ids = [r["attendance_id"] for r in rows]
    assert ids == [1]  # salaried(2) + inactive(3) dropped; 4 resolved; only hourly-active-unresolved 1
    assert rows[0]["name"] == "Maria"
    assert rows[0]["check_in_label"]  # formatted, non-empty


def test_shape_sorts_newest_first():
    cached = [
        {"att_id": 1, "employee_odoo_id": 7, "employee_name": "M", "check_in": "2026-06-01T06:00:00+00:00"},
        {"att_id": 2, "employee_odoo_id": 7, "employee_name": "M", "check_in": "2026-06-03T06:00:00+00:00"},
    ]
    rows = missing_wc.shape_rows(cached, _people(), resolved=set())
    assert [r["attendance_id"] for r in rows] == [2, 1]


# ---- DB-backed cache/resolve ----

pg = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")


@pg
def test_cache_write_read_round_trip():
    from zira_dashboard import db
    missing_wc.write_cache([{"att_id": 1, "employee_odoo_id": 7}])
    assert missing_wc._read_cache() == [{"att_id": 1, "employee_odoo_id": 7}]
    db.execute("UPDATE missing_wc_cache SET snapshot = '[]'::jsonb WHERE id = 1")


@pg
def test_resolve_and_resolved_ids():
    from zira_dashboard import db
    db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = %s", (999002,))
    missing_wc.resolve(999002, "assigned", name="Maria", wc_name="Dismantler 1")
    assert 999002 in missing_wc.resolved_ids()
    db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = %s", (999002,))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_missing_wc.py -v`
Expected: the two pure `shape_rows` tests FAIL (module/func missing); the `@pg` ones SKIP locally.

- [ ] **Step 3: Implement `missing_wc.py`**

Create `src/zira_dashboard/missing_wc.py`:

```python
"""Missing-work-center alert: cached Odoo hr.attendance rows lacking a
work-center tag, plus suppression + row shaping for the badge/modal.

Mirrors late_report.py: the warmer owns the Odoo fetch (see
app._tick_missing_wc); this module does local reads + pure shaping, so the
badge endpoint never touches Odoo on the hot path.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from .shift_config import SITE_TZ

_log = logging.getLogger(__name__)


def write_cache(rows: list[dict]) -> None:
    """Overwrite the single-row snapshot with the latest fetch (warmer-owned)."""
    from . import db
    db.execute(
        "INSERT INTO missing_wc_cache (id, snapshot, refreshed_at) "
        "VALUES (1, %s::jsonb, now()) "
        "ON CONFLICT (id) DO UPDATE SET snapshot = EXCLUDED.snapshot, refreshed_at = now()",
        (json.dumps(rows or []),),
    )


def _read_cache() -> list[dict]:
    from . import db
    rows = db.query("SELECT snapshot FROM missing_wc_cache WHERE id = 1")
    if not rows:
        return []
    snap = rows[0]["snapshot"]
    if isinstance(snap, list):
        return snap
    try:
        return json.loads(snap) if snap else []
    except (TypeError, ValueError):
        return []


def resolve(attendance_id, action: str, name: str | None = None,
            wc_name: str | None = None) -> None:
    """Suppress an attendance row from the alert (action 'assigned'|'dismissed')."""
    from . import db
    db.execute(
        "INSERT INTO missing_wc_resolved (attendance_id, action, name, wc_name) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (attendance_id) DO UPDATE SET action = EXCLUDED.action, "
        "name = EXCLUDED.name, wc_name = EXCLUDED.wc_name, resolved_at = now()",
        (int(attendance_id), action, name, wc_name),
    )


def resolved_ids() -> set[int]:
    from . import db
    return {int(r["attendance_id"])
            for r in db.query("SELECT attendance_id FROM missing_wc_resolved")}


def _check_in_label(check_in_iso) -> str:
    """ISO UTC string -> 'H:MM AM/PM Ddd' in site-local time, '' on bad input."""
    if not check_in_iso:
        return ""
    try:
        dt = datetime.fromisoformat(check_in_iso)
    except (TypeError, ValueError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(SITE_TZ)
    fmt = "%#I:%M %p %a" if os.name == "nt" else "%-I:%M %p %a"
    return local.strftime(fmt)


def shape_rows(cached: list[dict], people_by_odoo_id: dict, resolved: set) -> list[dict]:
    """Pure: cached rows + {odoo_id: {name, wage_type, active, excluded}} +
    resolved att_id set -> modal rows for ACTIVE HOURLY people, newest first.
    One row per attendance record (each needs its own work center)."""
    out: list[dict] = []
    for r in cached:
        att_id = r.get("att_id")
        if att_id in resolved:
            continue
        p = people_by_odoo_id.get(r.get("employee_odoo_id"))
        if not p or p.get("wage_type") != "hourly":
            continue
        if not p.get("active") or p.get("excluded"):
            continue
        out.append({
            "attendance_id": att_id,
            "name": p.get("name") or r.get("employee_name") or "Unknown",
            "employee_odoo_id": r.get("employee_odoo_id"),
            "check_in": r.get("check_in"),
            "check_in_label": _check_in_label(r.get("check_in")),
        })
    out.sort(key=lambda x: x.get("check_in") or "", reverse=True)
    return out


def current_rows() -> list[dict]:
    """Badge/modal payload: cached snapshot filtered to active hourly people,
    minus suppressed records. All local reads — no Odoo I/O."""
    from . import db
    cached = _read_cache()
    prows = db.query(
        "SELECT odoo_id, name, wage_type, active, excluded FROM people "
        "WHERE odoo_id IS NOT NULL"
    )
    people_by_odoo_id = {int(r["odoo_id"]): r for r in prows}
    return shape_rows(cached, people_by_odoo_id, resolved_ids())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_missing_wc.py -v` → 2 passed (shape_rows), 2 skipped (`@pg`).
`.\.venv\Scripts\ruff.exe check src/zira_dashboard/missing_wc.py tests/test_missing_wc.py` → clean.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/missing_wc.py tests/test_missing_wc.py
git commit -m "feat(missing-wc): cache + suppression + pure row shaping"
```

---

### Task 4: Warmer — `_tick_missing_wc`

**Files:**
- Modify: `src/zira_dashboard/app.py` (add tick fn + register in `_WARMERS`)

No dedicated test: this is a thin async wrapper over `fetch_attendances_missing_wc` (Task 2) + `write_cache` (Task 3), both already tested. Verify by import + ruff.

- [ ] **Step 1: Add the tick function**

In `src/zira_dashboard/app.py`, after `_tick_staffing_stable` (the last `_tick_*` def, ~line 142):

```python
async def _tick_missing_wc():
    """Refresh the cache of Odoo hr.attendance lacking a work-center tag (last
    14 days) for the Missing-Work-Center alert. No-ops (logs once) if the Odoo
    kiosk WC field isn't configured."""
    from datetime import timedelta
    from . import missing_wc, odoo_client
    since = datetime.now(timezone.utc) - timedelta(days=14)
    rows = await asyncio.to_thread(odoo_client.fetch_attendances_missing_wc, since)
    await asyncio.to_thread(missing_wc.write_cache, rows)
```

- [ ] **Step 2: Register the warmer**

In the `_WARMERS` list, add (a 3-minute interval):

```python
    ("missing WC", _tick_missing_wc, 180),
```

- [ ] **Step 3: Verify import + lint**

Run:
```
.\.venv\Scripts\ruff.exe check src/zira_dashboard/app.py
.\.venv\Scripts\python.exe -c "import zira_dashboard.app as a; print(any(n == 'missing WC' for (n, _, _) in a._WARMERS))"
```
Expected: ruff clean; prints `True`.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/app.py
git commit -m "feat(missing-wc): background warmer refreshing the missing-WC cache"
```

---

### Task 5: Routes — `/api/missing-wc`, `/missing-wc/assign`, `/missing-wc/dismiss`

**Files:**
- Create: `src/zira_dashboard/routes/missing_wc.py`
- Modify: `src/zira_dashboard/app.py` (import + `include_router`)
- Test: `tests/test_missing_wc_routes.py` (new — Postgres-gated, mocked Odoo)

- [ ] **Step 1: Write the failing test**

Create `tests/test_missing_wc_routes.py`:

```python
"""Missing-WC routes: GET shape, assign (mocked Odoo) + dismiss record suppression."""

import os

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard import db, missing_wc, odoo_client

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)

client = TestClient(app)
ATT = 999100


@pytest.fixture(autouse=True)
def _clean():
    db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = %s", (ATT,))
    yield
    db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = %s", (ATT,))


def test_get_returns_count_rows_and_work_centers():
    r = client.get("/api/missing-wc")
    assert r.status_code == 200
    body = r.json()
    assert set(["count", "rows", "work_centers"]) <= set(body.keys())
    assert "Dismantler 1" in body["work_centers"]


def test_assign_writes_wc_and_records_resolved(monkeypatch):
    calls = {}
    monkeypatch.setattr(odoo_client, "set_attendance_wc",
                        lambda att_id, wc: calls.update(att_id=att_id, wc=wc))
    r = client.post("/missing-wc/assign",
                    json={"attendance_id": ATT, "wc_name": "Dismantler 1", "name": "Maria"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls == {"att_id": ATT, "wc": "Dismantler 1"}
    assert ATT in missing_wc.resolved_ids()


def test_assign_rejects_unknown_wc():
    r = client.post("/missing-wc/assign",
                    json={"attendance_id": ATT, "wc_name": "Not A WC"})
    assert r.status_code == 400
    assert ATT not in missing_wc.resolved_ids()


def test_assign_rejects_bad_id():
    r = client.post("/missing-wc/assign", json={"attendance_id": "x", "wc_name": "Dismantler 1"})
    assert r.status_code == 400


def test_dismiss_records_resolved():
    r = client.post("/missing-wc/dismiss", json={"attendance_id": ATT, "name": "Maria"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert ATT in missing_wc.resolved_ids()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_missing_wc_routes.py -v`
Expected (no local DB): 5 SKIPPED, 0 collection errors. (In CI they'd FAIL on the missing routes / 404.)

- [ ] **Step 3: Create the routes**

Create `src/zira_dashboard/routes/missing_wc.py`:

```python
"""Missing-Work-Center alert endpoints: badge/modal read + assign + dismiss.

Mirrors routes/late_report.py. The READ is a cheap local-cache read (the warmer
owns the Odoo query). Assign writes the work center + resolved department onto
the Odoo hr.attendance via odoo_client.set_attendance_wc, then suppresses the
row. Odoo-origin records have no local kiosk punch, so there's nothing to
re-round — the department tag is the fix.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/api/missing-wc")
def missing_wc_json():
    """Badge/modal snapshot: {count, rows, work_centers}. All local reads."""
    from .. import missing_wc, staffing
    try:
        rows = missing_wc.current_rows()
    except Exception:
        rows = []
    return JSONResponse({
        "count": len(rows),
        "rows": rows,
        "work_centers": [loc.name for loc in staffing.LOCATIONS],
    })


@router.post("/missing-wc/assign")
async def missing_wc_assign(request: Request):
    """Assign a work center to a flagged attendance record.

    Body (JSON): {attendance_id, wc_name, name?}
    """
    from .. import missing_wc, odoo_client, staffing
    body = await request.json()
    try:
        att_id = int(body.get("attendance_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad attendance_id"}, status_code=400)
    wc_name = str(body.get("wc_name") or "").strip()
    name = (str(body.get("name") or "").strip() or None)
    if wc_name not in {loc.name for loc in staffing.LOCATIONS}:
        return JSONResponse({"ok": False, "error": "unknown work center"}, status_code=400)
    try:
        odoo_client.set_attendance_wc(att_id, wc_name)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    missing_wc.resolve(att_id, "assigned", name=name, wc_name=wc_name)
    return JSONResponse({"ok": True})


@router.post("/missing-wc/dismiss")
async def missing_wc_dismiss(request: Request):
    """Dismiss a record that legitimately has no work center.

    Body (JSON): {attendance_id, name?}
    """
    from .. import missing_wc
    body = await request.json()
    try:
        att_id = int(body.get("attendance_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad attendance_id"}, status_code=400)
    name = (str(body.get("name") or "").strip() or None)
    missing_wc.resolve(att_id, "dismissed", name=name)
    return JSONResponse({"ok": True})
```

- [ ] **Step 4: Register the router in `app.py`**

In the `from .routes import (` block, add `missing_wc` to the imported names (alongside `late_report`). Then after `app.include_router(late_report.router)` (line ~329), add:

```python
app.include_router(missing_wc.router)
```

- [ ] **Step 5: Verify routes register + run tests**

Run:
```
.\.venv\Scripts\ruff.exe check src/zira_dashboard/routes/missing_wc.py tests/test_missing_wc_routes.py
.\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; p={r.path for r in app.routes}; print([x for x in ['/api/missing-wc','/missing-wc/assign','/missing-wc/dismiss'] if x not in p])"
```
Expected: ruff clean; prints `[]` (all registered).
Run: `.\.venv\Scripts\python.exe -m pytest tests/test_missing_wc_routes.py -v` → 5 SKIPPED locally (CI runs them).

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/missing_wc.py src/zira_dashboard/app.py tests/test_missing_wc_routes.py
git commit -m "feat(missing-wc): GET/assign/dismiss routes + register router"
```

---

### Task 6: Badge + modal in `footer.js`

**Files:**
- Modify: `src/zira_dashboard/static/footer.js` (append a new IIFE after the Late/Absence block)

Reuses the late report's `.late-*` CSS for identical look; adds `.mwc-*` hooks for behavior. No CSS file changes needed.

- [ ] **Step 1: Guard against breaking existing tests**

Run: `git grep -n -i "footer\|nav-badge\|late-nav-badge\|api/missing-wc" -- tests`
Expected: review any matches. If a test asserts footer/badge markup, update it in this task. (footer.js itself has no unit tests; it's verified via the routes in Task 5 + manual reasoning.)

- [ ] **Step 2: Append the badge + modal IIFE**

At the END of `src/zira_dashboard/static/footer.js`, append:

```javascript

// Global "Missing Work Center" badge + modal — present on every page.
// Mirrors the Late/Absence badge/modal above and reuses its .late-* styling.
(function () {
  var navBadge = null;
  var modal = null;
  var data = null;
  var ENDPOINT = '/api/missing-wc';

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
      navBadge.className = 'late-nav-badge mwc-nav-badge';
      navBadge.title = 'Attendance records with no work center — click to assign';
      navBadge.addEventListener('click', function (e) { e.preventDefault(); openModal(); });
      anchor.parentNode.insertBefore(navBadge, anchor.nextSibling);
    }
    navBadge.innerHTML = '📍 <span class="cnt">' + data.count + '</span> No Work Center';
    navBadge.style.display = '';
  }

  function openModal() {
    closeModal();
    modal = document.createElement('div');
    modal.className = 'late-modal mwc-modal';
    modal.innerHTML = ''
      + '<div class="late-backdrop"></div>'
      + '<div class="late-card" role="dialog" aria-modal="true" aria-label="Missing work center">'
      + '  <div class="late-head"><h3>Missing Work Center</h3>'
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

  function wcOptions(wcs) {
    var opts = '<option value="">Pick work center…</option>';
    (wcs || []).forEach(function (w) {
      opts += '<option value="' + escapeHtml(w) + '">' + escapeHtml(w) + '</option>';
    });
    return opts;
  }

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
      li.querySelectorAll('button, select').forEach(function (el) { el.disabled = true; });
      li.style.opacity = '0.6';
      refreshCount();
    }
  }

  function wireActions(body) {
    body.querySelectorAll('.mwc-assign-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        btn.closest('.late-item').querySelector('.mwc-assign-row').hidden = false;
      });
    });
    body.querySelectorAll('.mwc-wc-select').forEach(function (sel) {
      sel.addEventListener('change', function () {
        sel.parentElement.querySelector('.mwc-save-btn').disabled = !sel.value;
      });
    });
    body.querySelectorAll('.mwc-save-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var li = btn.closest('.late-item');
        var sel = li.querySelector('.mwc-wc-select');
        if (!sel.value) return;
        btn.disabled = true;
        postJson('/missing-wc/assign', {
          attendance_id: parseInt(li.getAttribute('data-att'), 10),
          wc_name: sel.value,
          name: li.querySelector('.late-item-name').textContent,
        }).then(function (res) {
          finishRow(li, res && res.ok ? 'Assigned ✓' : 'Error', !!(res && res.ok));
        }).catch(function () { finishRow(li, 'Error', false); btn.disabled = false; });
      });
    });
    body.querySelectorAll('.mwc-dismiss-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var li = btn.closest('.late-item');
        btn.disabled = true;
        postJson('/missing-wc/dismiss', {
          attendance_id: parseInt(li.getAttribute('data-att'), 10),
          name: li.querySelector('.late-item-name').textContent,
        }).then(function (res) {
          finishRow(li, res && res.ok ? 'Dismissed' : 'Error', !!(res && res.ok));
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
      body.innerHTML = '<p class="late-help">No attendance records are missing a work center. '
        + 'Any hourly employee with an attendance record in the last 14 days that has no '
        + 'work center will appear here.</p>';
      return;
    }
    var html = '<ul class="late-list">';
    rows.forEach(function (item) {
      html += '<li class="late-item" data-att="' + item.attendance_id + '">'
        + '<span class="late-item-name">' + escapeHtml(item.name) + '</span>'
        + '<span class="late-item-mins">clocked in ' + escapeHtml(item.check_in_label) + '</span>'
        + '<span class="late-item-actions">'
        + '  <button type="button" class="mwc-assign-btn">Assign</button>'
        + '  <button type="button" class="mwc-dismiss-btn">Dismiss</button>'
        + '</span>'
        + '<div class="late-reason-row mwc-assign-row" hidden>'
        + '  <select class="mwc-wc-select">' + wcOptions(d.work_centers) + '</select>'
        + '  <button type="button" class="mwc-save-btn" disabled>Save</button>'
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

- [ ] **Step 3: Sanity-check the JS parses**

Run (Node ships with the toolchain; if unavailable, skip — CI/browser will catch):
`node --check src/zira_dashboard/static/footer.js` → no output = OK. If `node` isn't present, eyeball balanced parens/braces; the block mirrors the late IIFE exactly.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/static/footer.js
git commit -m "feat(missing-wc): nav badge + modal mirroring the Late report"
```

---

### Task 7: Verify suite, lint, changelog

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Full affected test set + lint**

Run:
```
.\.venv\Scripts\python.exe -m pytest tests/test_fetch_missing_wc.py tests/test_missing_wc.py tests/test_missing_wc_schema.py tests/test_missing_wc_routes.py -v
.\.venv\Scripts\python.exe -m pytest tests/ -q -p no:cacheprovider
.\.venv\Scripts\ruff.exe check src/zira_dashboard/missing_wc.py src/zira_dashboard/routes/missing_wc.py src/zira_dashboard/odoo_client.py src/zira_dashboard/app.py src/zira_dashboard/_schema.py
```
Expected: pure tests pass; DB-gated ones skip locally; full suite shows no failures/errors; ruff clean.

- [ ] **Step 2: Add a CHANGELOG entry**

Add a new `### <HH:MM>` entry under today's date (`2026-06-04`) in `CHANGELOG.md` (newest first), e.g.:

```markdown
### <HH:MM>
- **New "📍 No Work Center" alert — same nav-badge + modal as the Late report.**
  Flags Odoo attendance records from the last 14 days (hourly employees) that
  have no work center, so they don't silently miss a department (production
  credit, and rounding where a kiosk punch exists). Click the badge → Assign a
  work center (writes it + the resolved department onto the Odoo attendance) or
  Dismiss. A background warmer keeps it off the hot path. Requires the Odoo
  kiosk WC field to be configured; safely shows nothing otherwise.
```

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): missing-work-center alert"
```

---

## Self-review notes (for the implementer)

- **Spec coverage:** detection via Odoo + WC-field guard (Task 2); 14-day window + cache (Tasks 2/4); hourly filter + suppression + shaping (Task 3); routes incl. assign-writes-department via `set_attendance_wc` (Task 5); late-report-style badge/modal (Task 6); changelog (Task 7).
- **Deliberate scope call:** the assign route does NOT re-round a local punch — flagged records are Odoo-origin with no local kiosk punch, and matching one by person+time is fragile; the department write is the fix. (Spec framed re-round as best-effort/partial.)
- **Type consistency:** cache rows use `att_id`/`employee_odoo_id`/`check_in`; shaped rows use `attendance_id`/`name`/`check_in_label`; `resolve(attendance_id, action, name, wc_name)` and `shape_rows(cached, people_by_odoo_id, resolved)` signatures match across tasks and the footer.js payload (`rows[].attendance_id`, `data.work_centers`).
- **DB-gated tests** (schema, missing_wc cache/resolve, routes) run in CI; pure tests (`fetch_*` mocked, `shape_rows`) run locally. CI is the authority.
