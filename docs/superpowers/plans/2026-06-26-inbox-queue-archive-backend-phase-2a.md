# Combined Queue + Archive Backend (Phase 2a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce the backend the unified-queue UI will consume — a canonical item-key scheme, a flat urgency-sorted/empty-hidden `queue` list in the inbox snapshot, and an archive read endpoint over `inbox_events` — with no user-facing change yet.

**Architecture:** A new `inbox_keys` module is the single source of truth for an inbox item's identity, used by both the snapshot rows and the Phase 1 resolve handlers so logged events correlate to open items (resolves the Phase 1 review carry-forward). `build_snapshot()` gains a pure `queue` list (flatten sections → sort by urgency tier → keep section order within tier; empty categories contribute nothing). `inbox_log.archive()` + `GET /api/exceptions/archive` return day-grouped, actor/hide-auto-filterable history with a `resolved_at` cursor for "show earlier".

**Tech Stack:** Python 3.12, FastAPI, psycopg2 + Postgres, pytest. Builds on Phase 1 (`inbox_events` table, `inbox_log`).

**Phase 2a scope (and non-scope):**
- ✅ `inbox_keys` canonical keys; `item_key` added to every snapshot row; handlers refactored to use `inbox_keys`.
- ✅ Flat `queue` list in `build_snapshot` (additive — existing `sections` stays, existing UI untouched).
- ✅ `inbox_log.archive()` + `GET /api/exceptions/archive` (day-grouped, filters, cursor).
- ❌ No template/JS/CSS change — the page renders exactly as today. The visible unified queue + archive UI is **Phase 2b**.
- ❌ Assignment-credit logging (`/api/staffing/attribute`) → **Phase 2b** (it shares an endpoint with the staffing page and needs that file's cache-helper context).

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `src/zira_dashboard/inbox_keys.py` | Canonical inbox item-key derivation (one definition per kind) | **Create** |
| `src/zira_dashboard/exception_inbox.py` | Snapshot builder | **Modify** — add `item_key` to every row; add pure `_queue_from_sections` + a `queue` key in `build_snapshot` |
| `src/zira_dashboard/routes/exceptions.py` | Inbox routes | **Modify** — refactor time-off `item_key` to `inbox_keys`; add `_group_by_day` + `GET /api/exceptions/archive` |
| `src/zira_dashboard/routes/missing_wc.py` | Missing-WC handlers | **Modify** — `item_key` via `inbox_keys` |
| `src/zira_dashboard/routes/missed_punch_out.py` | Missed-punch handler | **Modify** — `item_key` via `inbox_keys` |
| `src/zira_dashboard/routes/late_report.py` | Late handlers | **Modify** — `item_key` via `inbox_keys` |
| `src/zira_dashboard/inbox_log.py` | Activity-log reader/writer | **Modify** — add `archive(...)` |
| `tests/test_inbox_keys.py` | Key derivation | **Create** |
| `tests/test_inbox_queue.py` | `_queue_from_sections` ordering/hide-empty | **Create** |
| `tests/test_inbox_archive.py` | `archive()` + endpoint (DB-gated) | **Create** |

---

### Task 1: `inbox_keys` canonical keys

**Files:** Create `src/zira_dashboard/inbox_keys.py`; create `tests/test_inbox_keys.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_inbox_keys.py`:

```python
"""Canonical Exception Inbox item keys."""
from zira_dashboard import inbox_keys


def test_canonical_keys():
    assert inbox_keys.time_off(55) == "time_off:55"
    assert inbox_keys.missing_wc(48213) == "missing_wc:48213"
    assert inbox_keys.missed_punch_out(48213) == "missed_punch_out:48213"
    assert inbox_keys.late("42", "2026-06-26") == "late:42:2026-06-26"
    assert inbox_keys.assignment("Saw 1", "2026-06-26T13:00:00") == "assignment:Saw 1:2026-06-26T13:00:00"
    assert inbox_keys.plant_schedule("2026-06-29") == "plant_schedule:2026-06-29"
```

- [ ] **Step 2: Run to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_keys.py -v`
Expected: FAIL — `ModuleNotFoundError`/`ImportError` (no `inbox_keys`).

- [ ] **Step 3: Create the module**

Create `src/zira_dashboard/inbox_keys.py`:

```python
"""Canonical Exception Inbox item keys.

One identity per inbox item, shared by the snapshot rows (exception_inbox) and
the resolve handlers (routes/*) so a logged inbox_events row correlates to the
open item it resolved. Keep these stable: the Phase 4 reconciler joins the open
set to the event log on this key, and the Phase 2b client diffs queue rows
against archived events by it.
"""
from __future__ import annotations


def time_off(request_id) -> str:
    return f"time_off:{request_id}"


def missing_wc(attendance_id) -> str:
    return f"missing_wc:{attendance_id}"


def missed_punch_out(attendance_id) -> str:
    return f"missed_punch_out:{attendance_id}"


def late(emp_id, day) -> str:
    """`day` is an ISO date string (the plant day)."""
    return f"late:{emp_id}:{day}"


def assignment(wc_name, start_iso) -> str:
    return f"assignment:{wc_name}:{start_iso}"


def plant_schedule(day) -> str:
    """`day` is an ISO date string."""
    return f"plant_schedule:{day}"
```

- [ ] **Step 4: Run to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_keys.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/inbox_keys.py tests/test_inbox_keys.py
git commit -m "feat(inbox): add canonical inbox_keys module

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Refactor handlers to use `inbox_keys` (align with the snapshot)

The Phase 1 handlers already write the canonical strings inline; this swaps the inline f-strings for `inbox_keys` calls so there is one definition. The existing wiring tests (`tests/test_inbox_event_wiring.py`) assert the exact key strings and are the regression guard — they must stay green.

**Files:** Modify `routes/exceptions.py`, `routes/missing_wc.py`, `routes/missed_punch_out.py`, `routes/late_report.py`.

- [ ] **Step 1: Run the existing wiring tests (baseline green)**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_event_wiring.py -v`
Expected: PASS (8 tests). These pin the key strings; keep them passing after the refactor.

- [ ] **Step 2: `routes/exceptions.py`**

Change the import line 12 from `from .. import exception_inbox, inbox_log, plant_day, time_off_audit` to:

```python
from .. import exception_inbox, inbox_keys, inbox_log, plant_day, time_off_audit
```

In `_approve_time_off_sync` and `_refuse_time_off_sync`, replace the event write's `item_key=f"time_off:{row['id']}",` line with:

```python
        item_key=inbox_keys.time_off(row["id"]),
```

- [ ] **Step 3: `routes/missing_wc.py`**

In both `_assign_sync` and `_dismiss_sync`, the function-local import is `from .. import inbox_log, missing_wc, ...` — add `inbox_keys`:

```python
    from .. import inbox_keys, inbox_log, missing_wc, odoo_client, staffing
```
(for `_dismiss_sync`: `from .. import inbox_keys, inbox_log, missing_wc`)

Replace each `item_key=f"missing_wc:{att_id}",` with:

```python
        item_key=inbox_keys.missing_wc(att_id),
```

- [ ] **Step 4: `routes/missed_punch_out.py`**

In `_correct_sync`, change `from .. import inbox_log, missed_punch_out, odoo_client` to:

```python
    from .. import inbox_keys, inbox_log, missed_punch_out, odoo_client
```

Replace `item_key=f"missed_punch_out:{att_id}",` with:

```python
        item_key=inbox_keys.missed_punch_out(att_id),
```

- [ ] **Step 5: `routes/late_report.py`**

Change the import line 22 from `from .. import absence_sync, db, inbox_log, late_report` to:

```python
from .. import absence_sync, db, inbox_keys, inbox_log, late_report
```

In `_declare_absent_sync` and `_save_late_arrival_sync`, replace each `item_key=f"late:{emp_id}:{today.isoformat()}",` with:

```python
        item_key=inbox_keys.late(emp_id, today.isoformat()),
```

- [ ] **Step 6: Run the wiring tests (still green)**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_event_wiring.py -v`
Expected: PASS (8 tests) — the produced key strings are unchanged, now sourced from `inbox_keys`.

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/routes/exceptions.py src/zira_dashboard/routes/missing_wc.py src/zira_dashboard/routes/missed_punch_out.py src/zira_dashboard/routes/late_report.py
git commit -m "refactor(inbox): source handler item_key from inbox_keys

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `item_key` on every snapshot row + flat `queue` list

**Files:** Modify `src/zira_dashboard/exception_inbox.py`; create `tests/test_inbox_queue.py`.

- [ ] **Step 1: Write the failing test (pure helper)**

Create `tests/test_inbox_queue.py`:

```python
"""build_snapshot's flat queue: urgency-sorted, empty categories dropped."""
from zira_dashboard import exception_inbox


SECTIONS = [
    {"id": "assignments", "title": "Assignments To Do", "tone": "warn",
     "rows": [{"name": "Saw 1", "priority": "warn", "item_key": "assignment:Saw 1:x"}]},
    {"id": "late", "title": "Late / Absence", "tone": "bad",
     "rows": [
         {"name": "Maria", "priority": "urgent", "item_key": "late:1:d"},
         {"name": "Snoozed Sam", "priority": "muted", "item_key": "late:2:d"},
     ]},
    {"id": "missing_wc", "title": "Missing Work Center", "tone": "bad", "rows": []},
    {"id": "time_off", "title": "Pending Time Off", "tone": "info",
     "rows": [{"name": "Caleb", "priority": "info", "item_key": "time_off:9"}]},
]


def test_queue_orders_by_tier_then_section_order():
    q = exception_inbox._queue_from_sections(SECTIONS)
    # urgent -> warn -> info -> muted; within a tier, section order is preserved.
    assert [r["item_key"] for r in q] == [
        "late:1:d",            # urgent
        "assignment:Saw 1:x",  # warn
        "time_off:9",          # info
        "late:2:d",            # muted (follow-up sinks to the bottom)
    ]


def test_queue_drops_empty_categories_and_tags_rows():
    q = exception_inbox._queue_from_sections(SECTIONS)
    assert all(r["section_id"] != "missing_wc" for r in q)  # empty -> absent
    first = q[0]
    assert first["category_label"] == "Late / Absence"
    assert first["tone"] == "bad"
    assert first["section_id"] == "late"
```

- [ ] **Step 2: Run to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_queue.py -v`
Expected: FAIL — `AttributeError: module 'zira_dashboard.exception_inbox' has no attribute '_queue_from_sections'`.

- [ ] **Step 3: Add the import + the pure queue helper**

In `src/zira_dashboard/exception_inbox.py`, add to the imports near the top (after `from . import plant_day, schedule_store, staffing`):

```python
from . import inbox_keys
```

Then add this module-level helper (place it just above `def build_summary`):

```python
_TIER_RANK = {"urgent": 0, "warn": 1, "info": 2, "normal": 2, "muted": 3}


def _queue_from_sections(sections: list[dict]) -> list[dict]:
    """Flatten section rows into one queue: urgency tier first (urgent → warn →
    info → muted/follow-up), preserving each section's order within a tier.
    Empty sections contribute nothing. Each row is tagged with its category."""
    tagged = []
    for section_order, section in enumerate(sections):
        for row_index, row in enumerate(section.get("rows") or []):
            tagged.append((
                _TIER_RANK.get(row.get("priority", "normal"), 2),
                section_order,
                row_index,
                {
                    **row,
                    "section_id": section["id"],
                    "category_label": section["title"],
                    "tone": section["tone"],
                },
            ))
    tagged.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in tagged]
```

- [ ] **Step 4: Run to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_queue.py -v`
Expected: PASS.

- [ ] **Step 5: Add `item_key` to each row + emit `queue` from `build_snapshot`**

In `build_snapshot`, add an `item_key` to every row dict, sourced from `inbox_keys`:

- In `_pending_time_off` (the shaped dict, alongside `"row_key": ...`):
  ```python
            "item_key": inbox_keys.time_off(r["id"]),
  ```
- In the four `late_rows.append({...})` blocks, add (each uses the row's `emp_id`; `today` is in scope in `build_snapshot` and is passed to `_pending_time_off`, but the late loop runs inside `build_snapshot` where `today` exists):
  ```python
            "item_key": inbox_keys.late(item.get("emp_id"), today.isoformat()),
  ```
- In the `assignments` section rows (the list comprehension):
  ```python
                    "item_key": inbox_keys.assignment(item.get("wc_name"), item.get("first_iso")),
  ```
- In the `missing_wc` section rows:
  ```python
                    "item_key": inbox_keys.missing_wc(r.get("attendance_id")),
  ```
- In the `missed_punch_out` section rows:
  ```python
                    "item_key": inbox_keys.missed_punch_out(r.get("attendance_id")),
  ```
- In `_plant_schedule_reminder`'s returned row dict (alongside its `row_key`):
  ```python
        "item_key": inbox_keys.plant_schedule(target_day.isoformat()),
  ```

Then, in `build_snapshot`, after `sections = [ ... ]` is built and before the `return`, add the queue and include it in the returned dict:

```python
    queue = _queue_from_sections(sections)
```

and add `"queue": queue,` to the returned dict (next to `"sections": sections,`).

> `_pending_time_off` builds its rows in a separate function that receives `today`; it already has `today` as its first parameter, so `inbox_keys.time_off(r["id"])` (no `today` needed) is fine there.

- [ ] **Step 6: Add a DB-gated structural smoke test**

Append to `tests/test_inbox_queue.py`:

```python
import os
import pytest


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_build_snapshot_includes_queue_and_item_keys():
    from zira_dashboard import db
    db.bootstrap_schema()
    snap = exception_inbox.build_snapshot()
    assert "queue" in snap and isinstance(snap["queue"], list)
    for section in snap["sections"]:
        for row in section.get("rows") or []:
            assert row.get("item_key"), f"row missing item_key in {section['id']}"
    for row in snap["queue"]:
        assert row.get("item_key") and row.get("category_label") and row.get("section_id")
```

- [ ] **Step 7: Run the queue tests + full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_queue.py -v` (pure tests pass; the DB-gated one skips locally).
Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q` (no regressions; `test_exception_inbox.py` still passes — `queue` is additive, `sections` unchanged).

- [ ] **Step 8: Commit**

```bash
git add src/zira_dashboard/exception_inbox.py tests/test_inbox_queue.py
git commit -m "feat(inbox): add item_key to snapshot rows + flat sorted queue list

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `inbox_log.archive()`

**Files:** Modify `src/zira_dashboard/inbox_log.py`; create `tests/test_inbox_archive.py`.

- [ ] **Step 1: Write the failing tests (DB-gated)**

Create `tests/test_inbox_archive.py`:

```python
"""inbox_log.archive: filtered, newest-first history for the inbox archive."""
import os

import pytest

from zira_dashboard import db, inbox_log

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")

KP = "test:archive:"


@pytest.fixture(autouse=True)
def _clean():
    db.bootstrap_schema()
    db.execute("DELETE FROM inbox_events WHERE item_key LIKE %s", (KP + "%",))
    yield
    db.execute("DELETE FROM inbox_events WHERE item_key LIKE %s", (KP + "%",))


def _seed():
    inbox_log.record_event(item_kind="time_off", item_key=KP + "1", person_name="A",
                           category_label="Time off", action="approve",
                           actor_upn="dale@gruberpallets.com", actor_name="Dale")
    inbox_log.record_event(item_kind="late", item_key=KP + "2", person_name="B",
                           category_label="Late", action="auto_resolved",
                           actor_upn=None, actor_name=None, source="auto")
    inbox_log.record_event(item_kind="missing_wc", item_key=KP + "3", person_name="C",
                           category_label="Missing WC", action="assign",
                           actor_upn="maria@gruberpallets.com", actor_name="Maria")


def test_archive_hides_auto_by_default():
    _seed()
    rows = [r for r in inbox_log.archive(limit=500) if r["item_key"].startswith(KP)]
    keys = {r["item_key"] for r in rows}
    assert KP + "2" not in keys  # auto-resolved hidden by default
    assert {KP + "1", KP + "3"} <= keys


def test_archive_include_auto():
    _seed()
    rows = [r for r in inbox_log.archive(include_auto=True, limit=500) if r["item_key"].startswith(KP)]
    assert KP + "2" in {r["item_key"] for r in rows}


def test_archive_filters_by_actor():
    _seed()
    rows = [r for r in inbox_log.archive(actor_upn="maria@gruberpallets.com", limit=500)
            if r["item_key"].startswith(KP)]
    assert {r["item_key"] for r in rows} == {KP + "3"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `DATABASE_URL=$DATABASE_URL ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_archive.py -v`
Expected: skips locally (no DATABASE_URL). With a test Postgres: FAIL — `AttributeError: module 'zira_dashboard.inbox_log' has no attribute 'archive'`.

- [ ] **Step 3: Add `archive()` to `inbox_log.py`**

Add after `recent_events`:

```python
def archive(
    *,
    before=None,
    actor_upn: str | None = None,
    include_auto: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """History for the inbox archive, newest first.

    - ``before``: a ``resolved_at`` value; returns only rows strictly older
      (the "show earlier" cursor).
    - ``actor_upn``: restrict to one actor (also excludes auto-resolved, which
      have a NULL actor).
    - ``include_auto``: when False (default), hide auto-resolved rows.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if before is not None:
        clauses.append("resolved_at < %s")
        params.append(before)
    if actor_upn:
        clauses.append("actor_upn = %s")
        params.append(actor_upn)
    elif not include_auto:
        clauses.append("actor_upn IS NOT NULL")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    return db.query(
        "SELECT id, item_kind, item_key, person_name, category_label, action, "
        "outcome, before_value, after_value, reason, actor_upn, actor_name, "
        "source, reversible, undone_at, resolved_at "
        "FROM inbox_events" + where +
        " ORDER BY resolved_at DESC LIMIT %s",
        tuple(params),
    )
```

- [ ] **Step 4: Run to verify it passes**

Run (with test Postgres): `DATABASE_URL=$DATABASE_URL ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_archive.py -v`
Expected: PASS. (Skips without DATABASE_URL.)

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/inbox_log.py tests/test_inbox_archive.py
git commit -m "feat(inbox): add inbox_log.archive() filtered history reader

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `GET /api/exceptions/archive`

**Files:** Modify `src/zira_dashboard/routes/exceptions.py`; append to `tests/test_inbox_archive.py`.

- [ ] **Step 1: Write the failing test (DB-gated, via TestClient)**

Append to `tests/test_inbox_archive.py`:

```python
from fastapi.testclient import TestClient

from zira_dashboard.app import app

_client = TestClient(app)


def test_archive_endpoint_groups_by_day_and_hides_auto():
    _seed()
    r = _client.get("/api/exceptions/archive")
    assert r.status_code == 200
    body = r.json()
    assert "groups" in body and "next_before" in body
    events = [e for g in body["groups"] for e in g["events"]
              if str(e.get("category_label") or "")]
    seen = {(e["category_label"], e["action"]) for e in events}
    assert ("Time off", "approve") in seen
    assert ("Missing WC", "assign") in seen
    assert all(e["action"] != "auto_resolved" for e in events)  # hidden by default
    # Each group carries a human day label + each event a time label + actor.
    g0 = body["groups"][0]
    assert g0["label"] and g0["events"][0]["time_label"]


def test_archive_endpoint_include_auto_flag():
    _seed()
    r = _client.get("/api/exceptions/archive?include_auto=true")
    actions = {e["action"] for g in r.json()["groups"] for e in g["events"]}
    assert "auto_resolved" in actions
```

- [ ] **Step 2: Run to verify it fails**

Run (with test Postgres): `DATABASE_URL=$DATABASE_URL ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_archive.py -k endpoint -v`
Expected: FAIL — 404 (route not defined). (Skips without DATABASE_URL.)

- [ ] **Step 3: Add the day-grouping helper + the route**

In `src/zira_dashboard/routes/exceptions.py`, change the datetime import line 6 from `from datetime import datetime, timezone` to:

```python
from datetime import datetime, timedelta, timezone
```

Add this helper (place it after `_decision_time_label`, which already converts to `plant_day.SITE_TZ`):

```python
def _group_archive_by_day(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group newest-first events into plant-local day buckets for the archive."""
    today = plant_day.today()
    yesterday = today - timedelta(days=1)
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for r in rows:
        resolved = r["resolved_at"]
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=timezone.utc)
        local = resolved.astimezone(plant_day.SITE_TZ)
        day = local.date()
        if day == today:
            label = "Today"
        elif day == yesterday:
            label = "Yesterday"
        else:
            label = local.strftime("%A, %b %-d")
        if current is None or current["day"] != day.isoformat():
            current = {"day": day.isoformat(), "label": label, "events": []}
            groups.append(current)
        current["events"].append({
            "id": r["id"],
            "item_kind": r.get("item_kind"),
            "item_key": r.get("item_key"),
            "person_name": r.get("person_name"),
            "category_label": r.get("category_label"),
            "action": r.get("action"),
            "outcome": r.get("outcome"),
            "before_value": r.get("before_value"),
            "after_value": r.get("after_value"),
            "reason": r.get("reason"),
            "actor_name": r.get("actor_name"),
            "actor_upn": r.get("actor_upn"),
            "auto": r.get("actor_upn") is None,
            "time_label": local.strftime("%-I:%M %p"),
        })
    return groups


@router.get("/api/exceptions/archive")
def exceptions_archive_json(
    before: str | None = None,
    actor: str | None = None,
    include_auto: bool = False,
    limit: int = 200,
):
    before_dt = None
    if before:
        try:
            before_dt = datetime.fromisoformat(before)
        except ValueError:
            return _json_error("bad 'before' cursor", 400)
    limit = max(1, min(int(limit), 500))
    rows = inbox_log.archive(
        before=before_dt, actor_upn=actor, include_auto=include_auto, limit=limit
    )
    next_before = (
        rows[-1]["resolved_at"].isoformat() if len(rows) == limit and rows else None
    )
    return JSONResponse({
        "groups": _group_archive_by_day(rows),
        "next_before": next_before,
    })
```

- [ ] **Step 4: Run to verify it passes**

Run (with test Postgres): `DATABASE_URL=$DATABASE_URL ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_archive.py -v`
Expected: PASS. (Skips without DATABASE_URL.)

- [ ] **Step 5: Full suite + ruff**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q` (no regressions).
Run: `.venv/bin/python -m ruff check src/zira_dashboard/inbox_keys.py src/zira_dashboard/inbox_log.py src/zira_dashboard/exception_inbox.py src/zira_dashboard/routes/exceptions.py src/zira_dashboard/routes/missing_wc.py src/zira_dashboard/routes/missed_punch_out.py src/zira_dashboard/routes/late_report.py tests/test_inbox_keys.py tests/test_inbox_queue.py tests/test_inbox_archive.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/exceptions.py tests/test_inbox_archive.py
git commit -m "feat(inbox): add GET /api/exceptions/archive (day-grouped, filterable)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Done criteria

- `inbox_keys` is the single definition of each item's identity; the four handlers and the snapshot rows all use it, so a logged event's `item_key` matches the open item's `item_key`.
- `build_snapshot()` returns a `queue`: urgency-sorted, empty categories absent, each row tagged with `category_label`/`tone`/`item_key`. `sections` is unchanged (existing UI untouched).
- `GET /api/exceptions/archive` returns day-grouped history with actor + hide-auto filters and a `next_before` cursor; auto-resolved hidden by default.
- Full suite green; ruff clean; no UI change.

## What this phase intentionally does NOT do

- No template/JS/CSS change — Phase 2b renders the `queue` + archive.
- No assignment-credit logging — Phase 2b (shares `/api/staffing/attribute` with the staffing page).
- No live polling/auto-resolve — Phase 4.
