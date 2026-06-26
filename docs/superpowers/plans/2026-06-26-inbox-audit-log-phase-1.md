# Exception Inbox Audit Log (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record a who/when audit row for every resolve action taken from the Exception Inbox, across the four inbox-native categories (time-off, missing-WC, missed-punch, late), with no user-facing UI change yet.

**Architecture:** A single append-only `inbox_events` table is the activity log (it will later back both the archive and the audit views — see the [design spec](../specs/2026-06-26-inbox-queue-archive-audit-design.md)). A thin `inbox_log` module writes rows; a best-effort `log_event_safe` wrapper guarantees a logging failure never breaks a completed action. Each of the four mutation handlers captures the acting manager from `request.state` and writes one event after its existing source-of-truth write. Snooze (temporary) writes nothing. Assignment-credit, plant-schedule, auto-resolved, and the queue/archive UI are explicitly out of scope for this phase.

**Tech Stack:** Python 3.12, FastAPI, psycopg2 + Postgres, pytest. Mirrors the existing `time_off_audit.py` / `time_off_decisions` audit pattern.

**Phase 1 scope (and non-scope):**
- ✅ `inbox_events` table + `inbox_log` module.
- ✅ Actor capture + event write in: time-off approve/deny, missing-WC assign/dismiss, missed-punch correct, late declare-absent / save-reason.
- ❌ Snooze writes no event (it returns to the queue later).
- ❌ Assignment-credit (`/api/staffing/attribute`) → Phase 2. Plant-schedule + auto-resolved → Phase 4. Undo → Phase 3. Combined queue + archive UI → Phase 2.

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `src/zira_dashboard/_schema.py` | Idempotent DDL string `SCHEMA_DDL` | **Modify** — append `inbox_events` table + indexes before the closing `"""` (currently line 907) |
| `src/zira_dashboard/inbox_log.py` | The activity-log writer/reader + `actor_from` helper | **Create** |
| `src/zira_dashboard/routes/exceptions.py` | Time-off approve/deny handlers | **Modify** — add `inbox_log` import + an event write in `_approve_time_off_sync` and `_refuse_time_off_sync` |
| `src/zira_dashboard/routes/missing_wc.py` | Missing-WC assign/dismiss | **Modify** — thread actor into `_assign_sync`/`_dismiss_sync`, write event |
| `src/zira_dashboard/routes/missed_punch_out.py` | Missed-punch correct | **Modify** — thread actor into `_correct_sync`, add `_clock_label` helper, write event |
| `src/zira_dashboard/routes/late_report.py` | Late absent/reason/snooze | **Modify** — thread actor into `_declare_absent_sync`/`_save_late_arrival_sync`, write events; snooze unchanged |
| `tests/test_inbox_events_schema.py` | Table round-trips | **Create** (DB-gated) |
| `tests/test_inbox_log.py` | `record_event` / `recent_events` / `log_event_safe` | **Create** |
| `tests/test_inbox_event_wiring.py` | Each handler emits the right event (collaborators mocked, no DB) | **Create** |

---

### Task 1: `inbox_events` table

**Files:**
- Test: `tests/test_inbox_events_schema.py` (create)
- Modify: `src/zira_dashboard/_schema.py` (append before the closing `"""` at line 907)

- [ ] **Step 1: Write the failing test**

Create `tests/test_inbox_events_schema.py`:

```python
"""inbox_events: the unified Exception Inbox activity log table (Postgres)."""
import os

import pytest

from zira_dashboard import db

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres"
)

KEY = "test:schema:inbox_events"


@pytest.fixture(autouse=True)
def _clean():
    db.bootstrap_schema()
    db.execute("DELETE FROM inbox_events WHERE item_key = %s", (KEY,))
    yield
    db.execute("DELETE FROM inbox_events WHERE item_key = %s", (KEY,))


def test_inbox_events_table_round_trips():
    db.execute(
        "INSERT INTO inbox_events "
        "(item_kind, item_key, person_name, category_label, action, actor_upn) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        ("time_off", KEY, "Maria", "Time off", "approve", "dale@gruberpallets.com"),
    )
    rows = db.query(
        "SELECT item_kind, action, actor_upn, reversible, undone_at, resolved_at "
        "FROM inbox_events WHERE item_key = %s",
        (KEY,),
    )
    assert rows and rows[0]["item_kind"] == "time_off"
    assert rows[0]["action"] == "approve"
    assert rows[0]["reversible"] is False  # column default
    assert rows[0]["undone_at"] is None
    assert rows[0]["resolved_at"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `DATABASE_URL=$DATABASE_URL ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_events_schema.py -v`
Expected: FAIL — `psycopg2.errors.UndefinedTable: relation "inbox_events" does not exist` (the table isn't created yet).

> If `DATABASE_URL` is unset the test SKIPS rather than fails — set it to a local/test Postgres to exercise this task.

- [ ] **Step 3: Add the table to the schema DDL**

In `src/zira_dashboard/_schema.py`, immediately after the `time_off_decisions` index (line 906, `... ON time_off_decisions (decided_at DESC);`) and before the closing `"""` on line 907, insert:

```sql

-- 2026-06-26: unified Exception Inbox activity log — the archive + audit trail.
-- One append-only row per resolution across every inbox category. Denormalized
-- (no FK) so history survives source-row deletion, like time_off_decisions.
-- actor_upn NULL => auto-resolved/system; otherwise the manager who acted.
CREATE TABLE IF NOT EXISTS inbox_events (
  id            SERIAL PRIMARY KEY,
  item_kind     TEXT NOT NULL,
  item_key      TEXT NOT NULL,
  person_name   TEXT,
  category_label TEXT,
  action        TEXT NOT NULL,
  outcome       TEXT,
  before_value  TEXT,
  after_value   TEXT,
  reason        TEXT,
  actor_upn     TEXT,
  actor_name    TEXT,
  source        TEXT,
  detail        JSONB,
  reversible    BOOLEAN NOT NULL DEFAULT FALSE,
  undone_at     TIMESTAMPTZ,
  undo_event_id INTEGER,
  resolved_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS inbox_events_resolved_at_idx ON inbox_events (resolved_at DESC);
CREATE INDEX IF NOT EXISTS inbox_events_actor_idx ON inbox_events (actor_upn);
CREATE INDEX IF NOT EXISTS inbox_events_item_idx ON inbox_events (item_kind, item_key);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `DATABASE_URL=$DATABASE_URL ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_events_schema.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/_schema.py tests/test_inbox_events_schema.py
git commit -m "feat(inbox): add inbox_events activity-log table

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `inbox_log` module

**Files:**
- Create: `src/zira_dashboard/inbox_log.py`
- Test: `tests/test_inbox_log.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_inbox_log.py`:

```python
"""inbox_log: write/read the Exception Inbox activity log + best-effort wrapper."""
import os

import pytest

from zira_dashboard import db, inbox_log

_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")

KEY = "test:inbox-log:1"


@pytest.fixture(autouse=True)
def _clean():
    if os.environ.get("DATABASE_URL"):
        db.bootstrap_schema()
        db.execute("DELETE FROM inbox_events WHERE item_key = %s", (KEY,))
    yield
    if os.environ.get("DATABASE_URL"):
        db.execute("DELETE FROM inbox_events WHERE item_key = %s", (KEY,))


@_db
def test_record_event_round_trips_with_actor():
    eid = inbox_log.record_event(
        item_kind="missing_wc",
        item_key=KEY,
        person_name="Maria",
        category_label="Missing WC",
        action="assign",
        outcome="Assigned to Saw 1",
        after_value="Saw 1",
        actor_upn="dale@gruberpallets.com",
        actor_name="Dale Gruber",
        source="inbox",
        reversible=True,
    )
    assert isinstance(eid, int)
    rows = [r for r in inbox_log.recent_events(days=1) if r["item_key"] == KEY]
    assert len(rows) == 1
    r = rows[0]
    assert r["action"] == "assign"
    assert r["after_value"] == "Saw 1"
    assert r["actor_upn"] == "dale@gruberpallets.com"
    assert r["reversible"] is True
    assert r["undone_at"] is None


@_db
def test_record_event_allows_null_actor_for_auto_resolved():
    inbox_log.record_event(
        item_kind="late",
        item_key=KEY,
        person_name="Tomas",
        category_label="Late",
        action="auto_resolved",
        outcome="Auto-resolved",
        actor_upn=None,
        actor_name=None,
        source="auto",
    )
    rows = [r for r in inbox_log.recent_events(days=1) if r["item_key"] == KEY]
    assert rows[0]["actor_upn"] is None


def test_log_event_safe_swallows_errors(monkeypatch):
    def boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(inbox_log, "record_event", boom)
    # Best-effort: a logging failure must never raise into the caller.
    result = inbox_log.log_event_safe(
        item_kind="missing_wc",
        item_key="x",
        person_name=None,
        category_label="Missing WC",
        action="assign",
    )
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_log.py -v`
Expected: FAIL — `ImportError`/`AttributeError: module 'zira_dashboard' has no attribute 'inbox_log'` (module doesn't exist). The swallow test runs even without `DATABASE_URL`.

- [ ] **Step 3: Create the module**

Create `src/zira_dashboard/inbox_log.py`:

```python
"""Append-only activity log for the Exception Inbox — the archive + audit trail.

One row per resolution across every inbox category. Denormalized on purpose
(snapshots person/category/outcome) so it stands alone after source rows are
deleted, mirroring time_off_audit.py. A NULL actor means the item resolved
itself (auto-resolved); a human action carries the manager's UPN + name.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import db

_log = logging.getLogger(__name__)


def actor_from(request) -> tuple[str | None, str | None]:
    """(user_upn, user_name) for the current request; both None for system/auto.

    The auth middleware sets these on request.state for every authenticated
    request (see auth.py). Returns (None, None) when unset (e.g. AUTH_DISABLED).
    """
    return (
        getattr(request.state, "user_upn", None),
        getattr(request.state, "user_name", None),
    )


def record_event(
    *,
    item_kind: str,
    item_key: str,
    person_name: str | None,
    category_label: str | None,
    action: str,
    outcome: str | None = None,
    before_value: str | None = None,
    after_value: str | None = None,
    reason: str | None = None,
    actor_upn: str | None = None,
    actor_name: str | None = None,
    source: str | None = "inbox",
    reversible: bool = False,
    detail: Any | None = None,
) -> int:
    """Insert one event row and return its id (for later undo correlation)."""
    rows = db.query(
        "INSERT INTO inbox_events "
        "(item_kind, item_key, person_name, category_label, action, outcome, "
        " before_value, after_value, reason, actor_upn, actor_name, source, "
        " reversible, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
        "RETURNING id",
        (item_kind, item_key, person_name, category_label, action, outcome,
         before_value, after_value, reason, actor_upn, actor_name, source,
         reversible, json.dumps(detail) if detail is not None else None),
    )
    return int(rows[0]["id"])


def log_event_safe(**kwargs) -> int | None:
    """record_event, but never raises. Returns the new id, or None on failure.

    The underlying action (Odoo write, suppression-table row) is the source of
    truth; a failed audit write is logged and swallowed so it can't turn a
    completed action into a 500. Mirrors the best-effort posture of the
    time-off chatter post.
    """
    try:
        return record_event(**kwargs)
    except Exception as e:  # noqa: BLE001 -- audit is best-effort
        _log.warning("inbox_log.record_event failed (%s): %s",
                     kwargs.get("item_key"), e, exc_info=True)
        return None


def recent_events(days: int = 30) -> list[dict[str, Any]]:
    """Events in the last ``days`` days, newest first (archive/audit feed)."""
    return db.query(
        "SELECT id, item_kind, item_key, person_name, category_label, action, "
        "outcome, before_value, after_value, reason, actor_upn, actor_name, "
        "source, reversible, undone_at, resolved_at "
        "FROM inbox_events "
        "WHERE resolved_at >= now() - make_interval(days => %s) "
        "ORDER BY resolved_at DESC",
        (days,),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_log.py -v`
Expected: PASS (the two `@_db` tests pass with `DATABASE_URL` set, skip without; the swallow test always passes).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/inbox_log.py tests/test_inbox_log.py
git commit -m "feat(inbox): add inbox_log activity-log writer + actor_from helper

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Time-off approve/deny → write an inbox event

**Files:**
- Modify: `src/zira_dashboard/routes/exceptions.py` (import line 12; `_approve_time_off_sync` ~line 242; `_refuse_time_off_sync` ~line 329)
- Test: `tests/test_inbox_event_wiring.py` (create; this task adds the first two tests)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_inbox_event_wiring.py`:

```python
"""Phase 1 wiring: each inbox-native resolve action records one inbox_events row.

Collaborators (Odoo, suppression-table writes, inbox_log itself) are
monkeypatched, so these need no Postgres. They assert each handler calls
inbox_log.log_event_safe with the right event shape + actor.
"""
from datetime import date, datetime, timezone

from zira_dashboard import inbox_log
from zira_dashboard.routes import exceptions as exceptions_route


def _capture_events(monkeypatch):
    events = []
    monkeypatch.setattr(inbox_log, "log_event_safe",
                        lambda **kw: events.append(kw) or 1)
    return events


def test_time_off_approve_records_inbox_event(monkeypatch):
    from zira_dashboard import odoo_client

    events = _capture_events(monkeypatch)
    row = {
        "id": 55, "person_odoo_id": 7, "person_name": "Maria Delgado",
        "leave_type": "PTO", "date_from": date(2026, 6, 22),
        "date_to": date(2026, 6, 22), "hour_from": 8.5, "hour_to": 12.25,
        "state": "confirm", "odoo_leave_id": 99,
    }
    monkeypatch.setattr(exceptions_route, "_load_time_off_request", lambda rid: row)
    monkeypatch.setattr(odoo_client, "approve_leave", lambda leave_id: "validate")
    monkeypatch.setattr(exceptions_route, "_set_time_off_state", lambda old, state: None)
    monkeypatch.setattr(exceptions_route.time_off_audit, "record_decision",
                        lambda **kw: None)

    resp = exceptions_route._approve_time_off_sync(
        55, actor_upn="dale@gruberpallets.com",
        actor_name="Dale Gruber", source="inbox")

    assert resp.status_code == 200
    assert len(events) == 1
    e = events[0]
    assert e["item_kind"] == "time_off"
    assert e["item_key"] == "time_off:55"
    assert e["action"] == "approve"
    assert e["actor_upn"] == "dale@gruberpallets.com"
    assert e["person_name"] == "Maria Delgado"


def test_time_off_deny_records_inbox_event(monkeypatch):
    events = _capture_events(monkeypatch)
    row = {
        "id": 56, "person_odoo_id": 8, "person_name": "Carlos Ortega",
        "leave_type": "Unpaid", "date_from": date(2026, 6, 22),
        "date_to": date(2026, 6, 22), "state": "draft", "odoo_leave_id": None,
    }
    monkeypatch.setattr(exceptions_route, "_load_time_off_request", lambda rid: row)
    monkeypatch.setattr(exceptions_route, "_set_time_off_state", lambda old, state: None)
    monkeypatch.setattr(exceptions_route.time_off_audit, "record_decision",
                        lambda **kw: None)

    resp = exceptions_route._refuse_time_off_sync(
        56, reason="No coverage", actor_upn="dale@gruberpallets.com",
        actor_name="Dale Gruber", source="inbox")

    assert resp.status_code == 200
    assert len(events) == 1
    e = events[0]
    assert e["item_kind"] == "time_off"
    assert e["item_key"] == "time_off:56"
    assert e["action"] == "deny"
    assert e["reason"] == "No coverage"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_event_wiring.py -v`
Expected: FAIL — `assert len(events) == 1` fails (currently `0`), because the handlers don't call `inbox_log` yet.

- [ ] **Step 3: Add the import**

In `src/zira_dashboard/routes/exceptions.py`, change line 12:

```python
from .. import exception_inbox, plant_day, time_off_audit
```

to:

```python
from .. import exception_inbox, inbox_log, plant_day, time_off_audit
```

- [ ] **Step 4: Write the approve event**

In `_approve_time_off_sync`, immediately after the `time_off_audit.record_decision(...)` call closes (just before `return JSONResponse({` near line 243), insert:

```python
    inbox_log.log_event_safe(
        item_kind="time_off",
        item_key=f"time_off:{row['id']}",
        person_name=row.get("person_name"),
        category_label="Time off",
        action="approve",
        outcome="Approved",
        after_value=final_state,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source=source,
        reversible=True,
    )
```

- [ ] **Step 5: Write the deny event**

In `_refuse_time_off_sync`, immediately after its `time_off_audit.record_decision(...)` call closes (just before `return JSONResponse({` near line 330), insert:

```python
    inbox_log.log_event_safe(
        item_kind="time_off",
        item_key=f"time_off:{row['id']}",
        person_name=row.get("person_name"),
        category_label="Time off",
        action="deny",
        outcome="Denied",
        reason=reason,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source=source,
        reversible=True,
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_event_wiring.py -v`
Expected: PASS (both time-off tests).

- [ ] **Step 7: Run the existing time-off tests to confirm no regression**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exception_inbox.py -v`
Expected: PASS (the existing approve/deny decision tests still pass — we only added a call).

- [ ] **Step 8: Commit**

```bash
git add src/zira_dashboard/routes/exceptions.py tests/test_inbox_event_wiring.py
git commit -m "feat(inbox): log time-off approve/deny to inbox_events

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Missing-WC assign/dismiss → write an inbox event

**Files:**
- Modify: `src/zira_dashboard/routes/missing_wc.py` (`_assign_sync` line 34, route line 54; `_dismiss_sync` line 64, route line 77)
- Test: `tests/test_inbox_event_wiring.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_inbox_event_wiring.py`:

```python
def test_missing_wc_assign_records_inbox_event(monkeypatch):
    from zira_dashboard import odoo_client, missing_wc, staffing
    from zira_dashboard.routes import missing_wc as missing_wc_route

    events = _capture_events(monkeypatch)
    monkeypatch.setattr(odoo_client, "set_attendance_wc", lambda att_id, wc: None)
    monkeypatch.setattr(missing_wc, "resolve", lambda *a, **k: None)
    wc_name = staffing.LOCATIONS[0].name

    resp = missing_wc_route._assign_sync(
        {"attendance_id": 999100, "wc_name": wc_name, "name": "Maria"},
        actor_upn="dale@gruberpallets.com", actor_name="Dale Gruber")

    assert resp.status_code == 200
    e = events[0]
    assert e["item_kind"] == "missing_wc"
    assert e["item_key"] == "missing_wc:999100"
    assert e["action"] == "assign"
    assert e["after_value"] == wc_name
    assert e["actor_name"] == "Dale Gruber"


def test_missing_wc_dismiss_records_inbox_event(monkeypatch):
    from zira_dashboard import missing_wc
    from zira_dashboard.routes import missing_wc as missing_wc_route

    events = _capture_events(monkeypatch)
    monkeypatch.setattr(missing_wc, "resolve", lambda *a, **k: None)

    resp = missing_wc_route._dismiss_sync(
        {"attendance_id": 999100, "name": "Maria"},
        actor_upn="dale@gruberpallets.com", actor_name="Dale Gruber")

    assert resp.status_code == 200
    e = events[0]
    assert e["item_kind"] == "missing_wc"
    assert e["item_key"] == "missing_wc:999100"
    assert e["action"] == "dismiss"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_event_wiring.py -k missing_wc -v`
Expected: FAIL — `TypeError: _assign_sync() got an unexpected keyword argument 'actor_upn'` (signature not updated yet).

- [ ] **Step 3: Update `_assign_sync`**

In `src/zira_dashboard/routes/missing_wc.py`, change the signature (line 34) and add the event write after the `missing_wc.resolve(...)` line (line 50). The function becomes:

```python
def _assign_sync(body: dict, actor_upn=None, actor_name=None) -> JSONResponse:
    """Blocking half of /missing-wc/assign (Odoo XML-RPC + Postgres write);
    runs in a worker thread via asyncio.to_thread."""
    from .. import inbox_log, missing_wc, odoo_client, staffing
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
    inbox_log.log_event_safe(
        item_kind="missing_wc",
        item_key=f"missing_wc:{att_id}",
        person_name=name,
        category_label="Missing WC",
        action="assign",
        outcome=f"Assigned to {wc_name}",
        after_value=wc_name,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        reversible=True,
    )
    return JSONResponse({"ok": True})
```

- [ ] **Step 4: Update the assign route to pass the actor**

Replace the `missing_wc_assign` route (lines 54-61):

```python
@router.post("/missing-wc/assign")
async def missing_wc_assign(request: Request):
    """Assign a work center to a flagged attendance record.

    Body (JSON): {attendance_id, wc_name, name?}
    """
    from .. import inbox_log
    body = await request.json()
    actor_upn, actor_name = inbox_log.actor_from(request)
    return await asyncio.to_thread(_assign_sync, body, actor_upn, actor_name)
```

- [ ] **Step 5: Update `_dismiss_sync`**

Change the signature (line 64) and add the event after `missing_wc.resolve(...)` (line 73):

```python
def _dismiss_sync(body: dict, actor_upn=None, actor_name=None) -> JSONResponse:
    """Blocking half of /missing-wc/dismiss (Postgres write); runs in a
    worker thread via asyncio.to_thread."""
    from .. import inbox_log, missing_wc
    try:
        att_id = int(body.get("attendance_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad attendance_id"}, status_code=400)
    name = (str(body.get("name") or "").strip() or None)
    missing_wc.resolve(att_id, "dismissed", name=name)
    inbox_log.log_event_safe(
        item_kind="missing_wc",
        item_key=f"missing_wc:{att_id}",
        person_name=name,
        category_label="Missing WC",
        action="dismiss",
        outcome="Dismissed",
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        reversible=True,
    )
    return JSONResponse({"ok": True})
```

- [ ] **Step 6: Update the dismiss route to pass the actor**

Replace the `missing_wc_dismiss` route (lines 77-84):

```python
@router.post("/missing-wc/dismiss")
async def missing_wc_dismiss(request: Request):
    """Dismiss a record that legitimately has no work center.

    Body (JSON): {attendance_id, name?}
    """
    from .. import inbox_log
    body = await request.json()
    actor_upn, actor_name = inbox_log.actor_from(request)
    return await asyncio.to_thread(_dismiss_sync, body, actor_upn, actor_name)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_event_wiring.py -k missing_wc -v`
Expected: PASS.

- [ ] **Step 8: Confirm no regression on the existing route tests**

Run: `DATABASE_URL=$DATABASE_URL ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_missing_wc_routes.py -v`
Expected: PASS (existing assign/dismiss tests still pass; they call via TestClient, where the route now also passes actor=None — unchanged behavior).

- [ ] **Step 9: Commit**

```bash
git add src/zira_dashboard/routes/missing_wc.py tests/test_inbox_event_wiring.py
git commit -m "feat(inbox): log missing-WC assign/dismiss to inbox_events

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Missed-punch correct → write an inbox event

**Files:**
- Modify: `src/zira_dashboard/routes/missed_punch_out.py` (add `_clock_label` helper; `_correct_sync` line 43; route line 29)
- Test: `tests/test_inbox_event_wiring.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_inbox_event_wiring.py`:

```python
def test_missed_punch_correct_records_inbox_event(monkeypatch):
    from zira_dashboard import odoo_client, missed_punch_out as mpo
    from zira_dashboard.routes import missed_punch_out as mpo_route
    from zira_dashboard.shift_config import SITE_TZ

    events = _capture_events(monkeypatch)
    ci = datetime(2026, 6, 8, 14, 0, tzinfo=timezone.utc)
    midnight = datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    monkeypatch.setattr(mpo, "get_unresolved", lambda att_id: {
        "attendance_id": att_id, "employee_odoo_id": 42, "name": "Devin Park",
        "check_in": ci, "auto_closed_at": midnight,
    })
    monkeypatch.setattr(odoo_client, "clock_out", lambda att_id, ts, mode=None: None)
    monkeypatch.setattr(mpo, "correct", lambda att_id, ts: None)

    resp = mpo_route._correct_sync(
        {"attendance_id": 999500, "time": "16:30"},
        actor_upn="dale@gruberpallets.com", actor_name="Dale Gruber")

    assert resp.status_code == 200
    e = events[0]
    assert e["item_kind"] == "missed_punch_out"
    assert e["item_key"] == "missed_punch_out:999500"
    assert e["action"] == "correct"
    assert e["after_value"] == "4:30 PM"
    assert e["person_name"] == "Devin Park"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_event_wiring.py -k missed_punch -v`
Expected: FAIL — `TypeError: _correct_sync() got an unexpected keyword argument 'actor_upn'`.

- [ ] **Step 3: Add the `_clock_label` helper**

In `src/zira_dashboard/routes/missed_punch_out.py`, after the `router = APIRouter()` line (line 15), add:

```python


def _clock_label(dt) -> str:
    """'4:30 PM' for an already site-local datetime — platform-safe (no %-I)."""
    return dt.strftime("%I:%M %p").lstrip("0")
```

- [ ] **Step 4: Update `_correct_sync`**

Change the signature (line 43) and add the event after the `missed_punch_out.correct(...)` call (line 73). The tail of the function becomes:

```python
def _correct_sync(body: dict, actor_upn=None, actor_name=None):
    from .. import inbox_log, missed_punch_out, odoo_client
    from ..shift_config import SITE_TZ
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
        odoo_client.clock_out(att_id, corrected, mode="manual")
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    missed_punch_out.correct(att_id, corrected)
    inbox_log.log_event_safe(
        item_kind="missed_punch_out",
        item_key=f"missed_punch_out:{att_id}",
        person_name=row.get("name"),
        category_label="Missed punch out",
        action="correct",
        outcome=f"Punch-out corrected to {_clock_label(corrected)}",
        before_value=_clock_label(midnight),
        after_value=_clock_label(corrected),
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        reversible=True,
    )
    return JSONResponse({"ok": True})
```

- [ ] **Step 5: Update the correct route to pass the actor**

Replace the `missed_punch_out_correct` route (lines 29-40):

```python
@router.post("/missed-punch-out/correct")
async def missed_punch_out_correct(request: Request):
    """Rewrite a flagged attendance's check_out to the entered time.

    Body (JSON): {attendance_id, time}  where time is "HH:MM" (24-hour).
    """
    import asyncio
    from .. import inbox_log
    body = await request.json()
    actor_upn, actor_name = inbox_log.actor_from(request)
    # The lookup + Odoo clock_out + resolve are all blocking (psycopg2 +
    # XML-RPC) — run them off the event loop so an Odoo round-trip can't
    # stall every in-flight request (same pattern as the other mutators).
    return await asyncio.to_thread(_correct_sync, body, actor_upn, actor_name)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_event_wiring.py -k missed_punch -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/routes/missed_punch_out.py tests/test_inbox_event_wiring.py
git commit -m "feat(inbox): log missed-punch corrections to inbox_events

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Late absent/reason → write an inbox event (snooze writes none)

**Files:**
- Modify: `src/zira_dashboard/routes/late_report.py` (import line 22; `_declare_absent_sync` line 36, route line 82; `_save_late_arrival_sync` line 96, route line 120)
- Test: `tests/test_inbox_event_wiring.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_inbox_event_wiring.py`:

```python
def test_late_declare_absent_records_inbox_event(monkeypatch):
    from zira_dashboard import absence_sync, db, late_report
    from zira_dashboard.routes import late_report as late_route

    events = _capture_events(monkeypatch)
    monkeypatch.setattr(absence_sync, "create_absence_for_day",
                        lambda **kw: {"leave_id": 123})
    monkeypatch.setattr(late_report, "declare_absent", lambda *a, **k: None)
    monkeypatch.setattr(db, "execute", lambda *a, **k: None)
    monkeypatch.setattr(late_route, "_bust_caches", lambda: None)

    resp = late_route._declare_absent_sync(
        {"emp_id": "42", "name": "Tomas Vela", "reason": "Sick"},
        actor_upn="dale@gruberpallets.com", actor_name="Dale Gruber")

    assert resp.status_code == 200
    e = events[0]
    assert e["item_kind"] == "late"
    assert e["action"] == "absent"
    assert e["reason"] == "Sick"
    assert e["person_name"] == "Tomas Vela"
    assert e["item_key"].startswith("late:42:")


def test_late_save_reason_records_inbox_event(monkeypatch):
    from zira_dashboard import late_report
    from zira_dashboard.routes import late_report as late_route

    events = _capture_events(monkeypatch)
    monkeypatch.setattr(late_report, "save_late_arrival", lambda *a, **k: None)
    monkeypatch.setattr(late_route, "_bust_caches", lambda: None)

    resp = late_route._save_late_arrival_sync(
        {"emp_id": "42", "name": "Tomas Vela", "reason": "Overslept"},
        actor_upn="dale@gruberpallets.com", actor_name="Dale Gruber")

    assert resp.status_code == 200
    e = events[0]
    assert e["item_kind"] == "late"
    assert e["action"] == "reason"
    assert e["reason"] == "Overslept"


def test_late_snooze_records_no_inbox_event(monkeypatch):
    from zira_dashboard import late_report
    from zira_dashboard.routes import late_report as late_route

    events = _capture_events(monkeypatch)
    monkeypatch.setattr(late_report, "snooze", lambda *a, **k: None)
    monkeypatch.setattr(late_route, "_bust_caches", lambda: None)

    resp = late_route._snooze_sync({"emp_id": "42", "name": "Tomas Vela", "minutes": 30})

    assert resp.status_code == 200
    assert events == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_event_wiring.py -k late -v`
Expected: FAIL — `TypeError: _declare_absent_sync() got an unexpected keyword argument 'actor_upn'` (absent + reason tests). The snooze test PASSES already (snooze is unchanged and emits nothing) — that's the intended end state for it.

- [ ] **Step 3: Add the import**

In `src/zira_dashboard/routes/late_report.py`, change line 22:

```python
from .. import absence_sync, db, late_report
```

to:

```python
from .. import absence_sync, db, inbox_log, late_report
```

- [ ] **Step 4: Update `_declare_absent_sync`**

Change the signature (line 36) and insert the event write after the `try/except` block succeeds — between the `except` block and `_bust_caches()` (line 78). The tail becomes:

```python
def _declare_absent_sync(body: dict, actor_upn=None, actor_name=None) -> JSONResponse:
    """Blocking half of /api/late-report/declare-absent (Postgres writes +
    cache busting); runs in a worker thread via asyncio.to_thread."""
    emp_id = str(body.get("emp_id") or "").strip()
    name = str(body.get("name") or "").strip()
    reason_raw = body.get("reason")
    reason = str(reason_raw).strip() if reason_raw is not None else ""
    if not emp_id or not name:
        return JSONResponse({"ok": False, "error": "emp_id and name required"}, status_code=400)
    if not reason:
        return JSONResponse(
            {"ok": False, "error": "reason required — no record posts until a reason is given"},
            status_code=400,
        )
    try:
        employee_odoo_id = int(emp_id)
    except ValueError:
        return JSONResponse(
            {"ok": False, "error": "emp_id must be an Odoo employee id"},
            status_code=400,
        )
    today = plant_today()
    try:
        absence = absence_sync.create_absence_for_day(
            employee_odoo_id=employee_odoo_id,
            employee_name=name,
            day=today,
            reason=reason,
        )
        late_report.declare_absent(
            today,
            emp_id,
            name,
            reason=reason,
            odoo_leave_id=absence["leave_id"],
        )
        db.execute(
            "DELETE FROM late_snoozes WHERE day = %s AND emp_id = %s",
            (today, emp_id),
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    inbox_log.log_event_safe(
        item_kind="late",
        item_key=f"late:{emp_id}:{today.isoformat()}",
        person_name=name,
        category_label="Late",
        action="absent",
        outcome="Marked absent",
        reason=reason,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        reversible=True,
    )
    _bust_caches()
    return JSONResponse({"ok": True})
```

- [ ] **Step 5: Update the declare-absent route to pass the actor**

Replace the `late_report_declare_absent` route (lines 82-93):

```python
@router.post("/api/late-report/declare-absent")
async def late_report_declare_absent(request: Request):
    """Mark a person as Absent for today.

    Body (JSON): {emp_id, name, reason}

    Reason is REQUIRED — no manual_absences row gets written until
    a non-empty reason is captured. Side effects: writes to
    manual_absences; clears any pending snooze; busts caches.
    """
    body = await request.json()
    actor_upn, actor_name = inbox_log.actor_from(request)
    return await asyncio.to_thread(_declare_absent_sync, body, actor_upn, actor_name)
```

- [ ] **Step 6: Update `_save_late_arrival_sync`**

Change the signature (line 96) and insert the event before `_bust_caches()` (line 116). The tail becomes:

```python
def _save_late_arrival_sync(body: dict, actor_upn=None, actor_name=None) -> JSONResponse:
    """Blocking half of /api/late-report/save-late-arrival (Postgres write +
    cache busting); runs in a worker thread via asyncio.to_thread."""
    from .. import late_report
    emp_id = str(body.get("emp_id") or "").strip()
    name = str(body.get("name") or "").strip()
    reason_raw = body.get("reason")
    reason = str(reason_raw).strip() if reason_raw is not None else ""
    if not emp_id or not name:
        return JSONResponse({"ok": False, "error": "emp_id and name required"}, status_code=400)
    if not reason:
        return JSONResponse(
            {"ok": False, "error": "reason required — no record posts until a reason is given"},
            status_code=400,
        )
    today = plant_today()
    try:
        late_report.save_late_arrival(today, emp_id, name, reason=reason)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    inbox_log.log_event_safe(
        item_kind="late",
        item_key=f"late:{emp_id}:{today.isoformat()}",
        person_name=name,
        category_label="Late",
        action="reason",
        outcome="Late reason recorded",
        reason=reason,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        reversible=False,
    )
    _bust_caches()
    return JSONResponse({"ok": True})
```

- [ ] **Step 7: Update the save-late-arrival route to pass the actor**

Replace the `late_report_save_late_arrival` route (lines 120-132):

```python
@router.post("/api/late-report/save-late-arrival")
async def late_report_save_late_arrival(request: Request):
    """Record a late-arrival event for today.

    Body (JSON): {emp_id, name, reason}

    Reason is REQUIRED — no late_arrivals row gets written until a
    non-empty reason is captured. Side effects: writes to
    late_arrivals; busts the report cache so the row drops out of
    needs_reason on the next poll.
    """
    body = await request.json()
    actor_upn, actor_name = inbox_log.actor_from(request)
    return await asyncio.to_thread(_save_late_arrival_sync, body, actor_upn, actor_name)
```

> Leave `_snooze_sync`, its route, and `_undo_absent_sync` unchanged — snooze emits no event (it returns to the queue later), and undo-absent reversal logging belongs to Phase 3.

- [ ] **Step 8: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_event_wiring.py -k late -v`
Expected: PASS (absent + reason now emit one event; snooze still emits none).

- [ ] **Step 9: Confirm no regression on existing late-report tests**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_late_report.py tests/test_late_report_absence_odoo.py -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add src/zira_dashboard/routes/late_report.py tests/test_inbox_event_wiring.py
git commit -m "feat(inbox): log late absent/reason to inbox_events (snooze excluded)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: PASS (DATABASE_URL-gated and Odoo-gated tests skip locally, as documented in the project memory; nothing newly fails). If a local/test `DATABASE_URL` is available, run with it set to exercise the schema + `inbox_log` round-trip tests too.

- [ ] **Step 2: Lint the changed files**

Run: `.venv/bin/python -m ruff check src/zira_dashboard/inbox_log.py src/zira_dashboard/routes/exceptions.py src/zira_dashboard/routes/missing_wc.py src/zira_dashboard/routes/missed_punch_out.py src/zira_dashboard/routes/late_report.py src/zira_dashboard/_schema.py`
Expected: no errors (matches the project's existing ruff config).

- [ ] **Step 3: Confirm the audit trail end to end (optional, needs DATABASE_URL)**

With a test Postgres, in a Python shell: call `inbox_log.record_event(...)` then `inbox_log.recent_events(days=1)` and confirm the row reads back with the actor. (This is what the Phase 2 archive UI will render.)

---

## Done criteria

- `inbox_events` exists and round-trips.
- Approving/denying time-off, assigning/dismissing a missing WC, correcting a missed punch, and declaring-absent / saving-a-late-reason each write exactly one `inbox_events` row carrying the acting manager (`actor_upn`/`actor_name`), the `item_kind`/`item_key`, and the relevant before/after + reason.
- Snooze writes nothing.
- A logging failure never breaks a completed action (`log_event_safe` swallows + logs).
- No existing test regresses.

## What this phase intentionally does NOT do

- No UI change — the queue still renders as today; the archive view comes in Phase 2.
- No assignment-credit logging (`/api/staffing/attribute`) — Phase 2.
- No plant-schedule or auto-resolved capture — Phase 4.
- No undo — Phase 3.
