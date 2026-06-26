# Exception Inbox Auto-Resolve (Phase 4a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record an `auto_resolved` audit/archive event for any inbox item that leaves the queue on its own (the person finally clocks in, the schedule gets published, another tool fixes it) — so the archive is a complete record — without a transient source failure ever mass-logging false resolutions.

**Architecture:** A new `inbox_open_items` mirror table tracks what's currently open. A `inbox_reconcile.run_once()` runs on the existing warmer tick: it diffs the live open set (from `build_snapshot()`'s `queue`) against the mirror; an item that departed **and** whose category's source did NOT error this tick **and** has no human `inbox_events` row since it was first seen is logged `auto_resolved`. The diff is a pure, unit-tested function; the DB + Odoo-snapshot wiring is a thin shell around it.

**Tech Stack:** Python 3.12, FastAPI, psycopg2 + Postgres, pytest. Builds on Phases 1–3.

**Scope:** Backend only — no UI change. Auto-resolved events surface in the existing archive on the next page load. The fully-live polling/diff client is **Phase 4b** (separate plan).

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `src/zira_dashboard/_schema.py` | DDL | **Modify** — add `inbox_open_items` table |
| `src/zira_dashboard/inbox_log.py` | Activity log | **Modify** — add `has_human_event_since(item_key, since)` |
| `src/zira_dashboard/inbox_reconcile.py` | Open-set mirror + auto-resolve reconcile | **Create** |
| `src/zira_dashboard/app.py` | Warmer ticks | **Modify** — add `_tick_inbox_reconcile` + register it |
| `tests/test_inbox_reconcile.py` | Pure diff + run_once orchestration | **Create** |
| `tests/test_inbox_open_items.py` | Mirror table + has_human_event_since (DB-gated) | **Create** |

---

### Task 1: `inbox_open_items` mirror table

**Files:** Modify `src/zira_dashboard/_schema.py`; create `tests/test_inbox_open_items.py`.

- [ ] **Step 1: Write the failing test (DB-gated)**

Create `tests/test_inbox_open_items.py`:

```python
"""inbox_open_items mirror table + has_human_event_since (Postgres)."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from zira_dashboard import db

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")

KEY = "test:openitems:1"


@pytest.fixture(autouse=True)
def _clean():
    db.bootstrap_schema()
    db.execute("DELETE FROM inbox_open_items WHERE item_key = %s", (KEY,))
    yield
    db.execute("DELETE FROM inbox_open_items WHERE item_key = %s", (KEY,))


def test_inbox_open_items_round_trips():
    db.execute(
        "INSERT INTO inbox_open_items (item_key, item_kind, person_name, category_label, priority) "
        "VALUES (%s, %s, %s, %s, %s)",
        (KEY, "missing_wc", "Maria", "Missing WC", "urgent"),
    )
    rows = db.query(
        "SELECT item_kind, person_name, first_seen, last_seen FROM inbox_open_items WHERE item_key = %s",
        (KEY,),
    )
    assert rows and rows[0]["item_kind"] == "missing_wc"
    assert rows[0]["first_seen"] is not None and rows[0]["last_seen"] is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `DATABASE_URL=${DATABASE_URL:-} ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_open_items.py -v`
Expected: skips locally (no DATABASE_URL); with a test Postgres FAILS (`relation "inbox_open_items" does not exist`).

- [ ] **Step 3: Add the table to `SCHEMA_DDL`**

In `src/zira_dashboard/_schema.py`, immediately after the `inbox_events` indexes (the `CREATE INDEX ... inbox_events_item_idx ...` line added in Phase 1) and before the closing `"""`, insert:

```sql

-- 2026-06-26: live "what's open right now" mirror for the Exception Inbox.
-- Bookkeeping for the reconcile tick (inbox_reconcile): diffed against the
-- freshly-computed open set to detect items that left without a human action
-- (logged as auto_resolved). Not a source of truth — rebuilt from the snapshot.
CREATE TABLE IF NOT EXISTS inbox_open_items (
  item_key       TEXT PRIMARY KEY,
  item_kind      TEXT NOT NULL,
  person_name    TEXT,
  category_label TEXT,
  priority       TEXT,
  first_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- [ ] **Step 4: Run to verify it passes**

Run (with test Postgres): `DATABASE_URL=$DATABASE_URL ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_open_items.py -v`
Expected: PASS (skips without DATABASE_URL).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/_schema.py tests/test_inbox_open_items.py
git commit -m "feat(inbox): add inbox_open_items mirror table

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `inbox_log.has_human_event_since`

**Files:** Modify `src/zira_dashboard/inbox_log.py`; append to `tests/test_inbox_open_items.py`.

- [ ] **Step 1: Write the failing test (DB-gated)**

Append to `tests/test_inbox_open_items.py`:

```python
def test_has_human_event_since():
    from zira_dashboard import inbox_log
    now = datetime.now(timezone.utc)
    earlier = now - timedelta(hours=1)
    db.execute("DELETE FROM inbox_events WHERE item_key = %s", (KEY,))
    # An auto_resolved row does NOT count as a human event.
    inbox_log.record_event(item_kind="missing_wc", item_key=KEY, person_name="M",
                           category_label="Missing WC", action="auto_resolved",
                           actor_upn=None, actor_name=None, source="auto")
    assert inbox_log.has_human_event_since(KEY, earlier) is False
    # A human dismiss DOES count.
    inbox_log.record_event(item_kind="missing_wc", item_key=KEY, person_name="M",
                           category_label="Missing WC", action="dismiss",
                           actor_upn="dale@gruberpallets.com", actor_name="Dale")
    assert inbox_log.has_human_event_since(KEY, earlier) is True
    # But not before its time.
    assert inbox_log.has_human_event_since(KEY, now + timedelta(hours=1)) is False
    db.execute("DELETE FROM inbox_events WHERE item_key = %s", (KEY,))
```

- [ ] **Step 2: Run to verify it fails**

Run (with test Postgres): `DATABASE_URL=$DATABASE_URL ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_open_items.py -k has_human -v`
Expected: FAIL (`has_human_event_since` missing). Skips without DATABASE_URL.

- [ ] **Step 3: Add the helper to `inbox_log.py`**

Add after `mark_undone`:

```python
def has_human_event_since(item_key: str, since) -> bool:
    """True if a human (non-auto, non-undo) event exists for this item at or
    after ``since`` — used by the reconciler to tell a human resolution from a
    self-clearing one."""
    rows = db.query(
        "SELECT 1 FROM inbox_events WHERE item_key = %s AND resolved_at >= %s "
        "AND action NOT IN ('auto_resolved', 'undo') LIMIT 1",
        (item_key, since),
    )
    return bool(rows)
```

- [ ] **Step 4: Run to verify it passes**

Run (with test Postgres): `DATABASE_URL=$DATABASE_URL ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_open_items.py -v`
Expected: PASS (skips without DATABASE_URL).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/inbox_log.py tests/test_inbox_open_items.py
git commit -m "feat(inbox): add inbox_log.has_human_event_since for reconcile

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `inbox_reconcile` module

**Files:** Create `src/zira_dashboard/inbox_reconcile.py`; create `tests/test_inbox_reconcile.py`.

- [ ] **Step 1: Write the failing tests (pure + mocked — NO DB)**

Create `tests/test_inbox_reconcile.py`:

```python
"""inbox_reconcile: pure diff + run_once orchestration (no DB/Odoo)."""
from datetime import datetime, timezone

from zira_dashboard import inbox_reconcile


def test_plan_reconcile_classifies_arrivals_departures_and_skips_errored():
    prev = {
        "missing_wc:1": {"item_kind": "missing_wc"},
        "time_off:9": {"item_kind": "time_off"},
        "late:5:2026-06-26": {"item_kind": "late"},
    }
    open_now = {
        "missing_wc:1": {"item_kind": "missing_wc"},       # still open
        "missed_punch_out:7": {"item_kind": "missed_punch_out"},  # new
    }
    errored = {"Pending Time Off"}  # the time_off source failed this tick

    actions = inbox_reconcile.plan_reconcile(open_now, prev, errored)

    assert set(actions["arrivals"]) == {"missed_punch_out:7"}
    assert actions["still_open"] == ["missing_wc:1"]
    assert "late:5:2026-06-26" in actions["departed"]   # left, source healthy
    assert "time_off:9" not in actions["departed"]       # source errored -> kept


def test_run_once_logs_auto_resolved_for_silent_departure(monkeypatch):
    from zira_dashboard import exception_inbox, inbox_log

    snap = {"queue": [], "source_errors": []}  # nothing open now
    monkeypatch.setattr(exception_inbox, "build_snapshot", lambda: snap)
    monkeypatch.setattr(inbox_reconcile, "_read_mirror", lambda: {
        "missing_wc:1": {
            "item_key": "missing_wc:1", "item_kind": "missing_wc",
            "person_name": "Maria", "category_label": "Missing WC",
            "first_seen": datetime(2026, 6, 26, tzinfo=timezone.utc),
        },
    })
    deleted, logged = [], []
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda k, i: None)
    monkeypatch.setattr(inbox_reconcile, "_delete", lambda k: deleted.append(k))
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda k, s: False)
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: logged.append(kw) or 1)

    inbox_reconcile.run_once()

    assert deleted == ["missing_wc:1"]
    assert len(logged) == 1
    assert logged[0]["action"] == "auto_resolved"
    assert logged[0]["item_key"] == "missing_wc:1"
    assert logged[0]["actor_upn"] is None


def test_run_once_skips_auto_when_human_resolved(monkeypatch):
    from zira_dashboard import exception_inbox, inbox_log

    monkeypatch.setattr(exception_inbox, "build_snapshot", lambda: {"queue": [], "source_errors": []})
    monkeypatch.setattr(inbox_reconcile, "_read_mirror", lambda: {
        "missing_wc:1": {
            "item_key": "missing_wc:1", "item_kind": "missing_wc",
            "person_name": "Maria", "category_label": "Missing WC",
            "first_seen": datetime(2026, 6, 26, tzinfo=timezone.utc),
        },
    })
    deleted, logged = [], []
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda k, i: None)
    monkeypatch.setattr(inbox_reconcile, "_delete", lambda k: deleted.append(k))
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda k, s: True)
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: logged.append(kw) or 1)

    inbox_reconcile.run_once()

    assert deleted == ["missing_wc:1"]  # mirror row cleared
    assert logged == []                 # but NOT logged auto_resolved (human did it)
```

- [ ] **Step 2: Run to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_reconcile.py -v`
Expected: FAIL — no `inbox_reconcile` module.

- [ ] **Step 3: Create the module**

Create `src/zira_dashboard/inbox_reconcile.py`:

```python
"""Reconcile the open-inbox mirror and log self-clearing items.

Runs on the warmer tick. Diffs the live open set (from build_snapshot's queue)
against the inbox_open_items mirror. An item that LEFT the queue, whose
category's source did NOT error this tick, and that has no human inbox_events
row since it was first seen, is logged 'auto_resolved'. A source error this
tick is treated as "unknown" (the item is kept), so a transient Odoo failure
can never mass-log false resolutions.
"""
from __future__ import annotations

import logging
from typing import Any

from . import db, exception_inbox, inbox_log

_log = logging.getLogger(__name__)

# Snapshot section id -> canonical item_kind.
_SECTION_KIND = {
    "assignments": "assignment",
    "plant_schedule": "plant_schedule",
    "late": "late",
    "missing_wc": "missing_wc",
    "missed_punch_out": "missed_punch_out",
    "time_off": "time_off",
}

# item_kind -> the build_snapshot source label (matches _capture(...) names),
# so a departed item can be checked against this tick's source_errors.
_KIND_SOURCE = {
    "assignment": "Assignments To Do",
    "plant_schedule": "Plant Schedule",
    "late": "Late / Absence",
    "missing_wc": "Missing Work Center",
    "missed_punch_out": "Missed Punch Out",
    "time_off": "Pending Time Off",
}


def plan_reconcile(open_now: dict, prev: dict, errored_sources: set) -> dict:
    """Pure diff. ``open_now``/``prev`` are {item_key: {item_kind, ...}}.

    Returns {arrivals: [keys], still_open: [keys], departed: [keys]}. A key that
    left but whose category source errored this tick is NOT reported as departed
    (we can't distinguish "resolved" from "source down")."""
    arrivals = [k for k in open_now if k not in prev]
    still_open = [k for k in open_now if k in prev]
    departed = []
    for key, row in prev.items():
        if key in open_now:
            continue
        if _KIND_SOURCE.get(row.get("item_kind")) in errored_sources:
            continue
        departed.append(key)
    return {"arrivals": arrivals, "still_open": still_open, "departed": departed}


def _open_now_from_snapshot(snapshot: dict) -> dict:
    out: dict[str, dict[str, Any]] = {}
    for row in snapshot.get("queue") or []:
        key = row.get("item_key")
        if not key:
            continue
        out[key] = {
            "item_kind": _SECTION_KIND.get(row.get("section_id"), row.get("section_id")),
            "person_name": row.get("name"),
            "category_label": row.get("category_label"),
            "priority": row.get("priority"),
        }
    return out


def _read_mirror() -> dict:
    rows = db.query(
        "SELECT item_key, item_kind, person_name, category_label, priority, first_seen "
        "FROM inbox_open_items"
    )
    return {r["item_key"]: r for r in rows}


def _upsert(key: str, info: dict) -> None:
    db.execute(
        "INSERT INTO inbox_open_items "
        "(item_key, item_kind, person_name, category_label, priority) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (item_key) DO UPDATE SET last_seen = now(), "
        "person_name = EXCLUDED.person_name, "
        "category_label = EXCLUDED.category_label, priority = EXCLUDED.priority",
        (key, info["item_kind"], info.get("person_name"),
         info.get("category_label"), info.get("priority")),
    )


def _delete(key: str) -> None:
    db.execute("DELETE FROM inbox_open_items WHERE item_key = %s", (key,))


def run_once() -> None:
    """One reconcile pass. Best-effort: one bad item never aborts the sweep."""
    snapshot = exception_inbox.build_snapshot()
    errored = {e.get("source") for e in (snapshot.get("source_errors") or [])}
    open_now = _open_now_from_snapshot(snapshot)
    prev = _read_mirror()
    actions = plan_reconcile(open_now, prev, errored)

    for key in actions["arrivals"]:
        _upsert(key, open_now[key])
    for key in actions["still_open"]:
        _upsert(key, open_now[key])

    for key in actions["departed"]:
        row = prev[key]
        try:
            if not inbox_log.has_human_event_since(key, row["first_seen"]):
                inbox_log.log_event_safe(
                    item_kind=row["item_kind"],
                    item_key=key,
                    person_name=row.get("person_name"),
                    category_label=row.get("category_label"),
                    action="auto_resolved",
                    outcome="Auto-resolved",
                    actor_upn=None,
                    actor_name=None,
                    source="auto",
                )
            _delete(key)
        except Exception as e:  # noqa: BLE001 -- one bad item never aborts the sweep
            _log.warning("inbox reconcile failed for %s: %s", key, e)
```

- [ ] **Step 4: Run to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_reconcile.py -v`
Expected: PASS (all 3 — pure + the 2 mocked run_once tests).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/inbox_reconcile.py tests/test_inbox_reconcile.py
git commit -m "feat(inbox): add inbox_reconcile (open-set diff + auto-resolve)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Wire the reconcile into the warmer tick

**Files:** Modify `src/zira_dashboard/app.py`; append to `tests/test_inbox_reconcile.py`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_inbox_reconcile.py`:

```python
def test_reconcile_tick_is_registered():
    from zira_dashboard import app
    names = [w[0] for w in app._WARMERS]
    assert "Inbox reconcile" in names
    entry = next(w for w in app._WARMERS if w[0] == "Inbox reconcile")
    assert entry[2] == 60  # seconds
```

- [ ] **Step 2: Run to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_reconcile.py::test_reconcile_tick_is_registered -v`
Expected: FAIL — "Inbox reconcile" not in `_WARMERS`.

- [ ] **Step 3: Add the tick + register it**

In `src/zira_dashboard/app.py`, add this coroutine immediately after `_tick_forklift` (the function ending at line 177):

```python
async def _tick_inbox_reconcile():
    """Log auto_resolved for inbox items that cleared themselves and refresh the
    open-items mirror. Skips categories whose source errored this tick, so a
    transient Odoo hiccup never mass-logs false resolutions."""
    from . import inbox_reconcile
    await asyncio.to_thread(inbox_reconcile.run_once)
```

Then add an entry to the `_WARMERS` list (the registry starting at the `("Zira cache", _tick_zira_cache, 30),` line):

```python
    ("Inbox reconcile", _tick_inbox_reconcile, 60),
```

- [ ] **Step 4: Run to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_reconcile.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/app.py tests/test_inbox_reconcile.py
git commit -m "feat(inbox): run the auto-resolve reconcile on the warmer tick (60s)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Full-suite verification

- [ ] **Step 1: Full suite + ruff**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q` (no regressions; DB-gated mirror/has-human tests skip locally, the pure + mocked reconcile tests + the tick-registration test run).
Run: `.venv/bin/python -m ruff check src/zira_dashboard/_schema.py src/zira_dashboard/inbox_log.py src/zira_dashboard/inbox_reconcile.py src/zira_dashboard/app.py tests/test_inbox_reconcile.py tests/test_inbox_open_items.py`
Expected: no errors.

---

## Done criteria

- An inbox item that leaves the queue without a human action is logged `auto_resolved` (NULL actor, `source='auto'`) and appears in the archive (hidden by default behind "hide auto-resolved").
- A human-resolved item is NOT double-logged (the reconciler sees the human event and skips it), and its mirror row is cleared.
- A category whose source errors on a tick has NONE of its items auto-resolved that tick (the mirror rows persist; a later healthy tick decides).
- The reconcile runs every 60s on the warmer; one bad item never aborts the sweep.
- Full suite green; ruff clean.

## Non-goals

- No UI change — auto-resolved items show on the next page load. The fully-live polling/diff client is **Phase 4b**.
- No retention/pruning of `inbox_open_items` beyond the natural delete-on-departure (the mirror only ever holds currently-open keys).
