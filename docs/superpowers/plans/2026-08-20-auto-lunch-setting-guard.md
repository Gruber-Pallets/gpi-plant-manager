# Auto-Lunch Setting Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a permanent Auto-Lunch settings history and one urgent app Inbox item whenever the persisted mode is not Live.

**Architecture:** Make Settings saves and their audit entries atomic in `auto_lunch_settings.py`. A focused `auto_lunch_guard.py` rereads the persisted singleton during the existing Inbox refresh, reconciles direct database changes into the audit history, refreshes the shared cache, and shapes the current state into an Inbox row. The Settings page renders the newest 20 events; the alert is derived from current state and therefore clears only when Live is restored.

**Tech Stack:** Python 3.13, FastAPI/Starlette, psycopg2/Postgres, Jinja2, pytest, Ruff, Railway.

## Global Constraints

- No Slack, email, or outside notification.
- The alert is urgent, cannot be dismissed, and clears automatically only when the persisted mode is Live.
- Direct database changes that remain persisted are detected on the existing roughly 20-second Inbox refresh.
- App-originated setting changes and audit entries are one transaction.
- External audit reconciliation is best-effort and cannot hide the active warning.
- Saving unchanged values creates no audit entry; flex-only changes are audited but do not alert while mode is Live.
- Keep the current production setting Live; production verification must not turn it Off or Observe-only.
- New What's New text must use short, plain sentences that a 10-year-old can understand.
- Preserve unrelated workspace changes and the untracked `.cursorignore`, `.python-version`, and `uv.lock` files.

---

## File Structure

- `src/zira_dashboard/_schema.py` — owns the idempotent audit-table DDL.
- `src/zira_dashboard/auto_lunch_settings.py` — owns persisted settings, atomic audited saves, external reconciliation, recent-history reads, and the shared cache.
- `src/zira_dashboard/auto_lunch_guard.py` — owns persisted observation, failure isolation, mode labels, and Inbox-row shaping.
- `src/zira_dashboard/inbox_keys.py` — owns the stable `auto_lunch:setting` item key.
- `src/zira_dashboard/exception_inbox.py` — composes the guard into summary counts, sections, and the urgent queue.
- `src/zira_dashboard/settings_context.py` — converts raw audit rows into site-local, plain-language view data.
- `src/zira_dashboard/routes/settings.py` — passes the authenticated actor into saves and recent history into the template.
- `src/zira_dashboard/templates/settings.html` — renders the newest audit entries below the Auto-Lunch form.
- `tests/test_auto_lunch_schema_static.py` — proves the audit schema exists without needing a database.
- `tests/test_auto_lunch_settings_audit.py` — proves audited persistence and external reconciliation with transaction fakes.
- `tests/test_auto_lunch_guard.py` — proves alert shaping and failure isolation.
- `tests/test_exception_inbox.py` — proves the alert is counted, urgent, linked, and auto-cleared.
- `tests/test_settings_context.py` and `tests/test_settings_auto_lunch.py` — prove history formatting, template rendering, and route actor propagation.
- `CHANGELOG.md` — describes the shipped safety guard in plain language.

---

### Task 1: Add append-only audit storage and atomic Settings saves

**Files:**
- Modify: `src/zira_dashboard/_schema.py:1313-1323`
- Modify: `src/zira_dashboard/auto_lunch_settings.py:1-69`
- Create: `tests/test_auto_lunch_schema_static.py`
- Create: `tests/test_auto_lunch_settings_audit.py`
- Modify: `tests/test_auto_lunch_settings.py:10-37`

**Interfaces:**
- Consumes: `db.cursor()`, `db.query()`, `CachedSingleton[Settings]`.
- Produces: `save(s: Settings, *, actor_upn: str | None = None, actor_name: str | None = None, source: str = "settings") -> bool`, `recent_events(limit: int = 20) -> list[dict]`, and private `_insert_event(cur, before, after, actor_upn, actor_name, source) -> None`.

- [ ] **Step 1: Write the failing schema test**

Create `tests/test_auto_lunch_schema_static.py`:

```python
from zira_dashboard._schema import SCHEMA_DDL


def test_auto_lunch_setting_event_schema_is_append_only_and_attributed():
    assert "CREATE TABLE IF NOT EXISTS auto_lunch_setting_events" in SCHEMA_DDL
    for column in (
        "before_enabled", "before_observe_only", "before_flex_after_hours",
        "before_flex_minutes", "after_enabled", "after_observe_only",
        "after_flex_after_hours", "after_flex_minutes", "actor_upn",
        "actor_name", "source", "changed_at",
    ):
        assert column in SCHEMA_DDL
    assert "CHECK (source IN ('settings','external','baseline'))" in SCHEMA_DDL
    assert "auto_lunch_setting_events_changed_at_idx" in SCHEMA_DDL
```

- [ ] **Step 2: Run the schema test and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_auto_lunch_schema_static.py -q
```

Expected: FAIL because `auto_lunch_setting_events` is absent from `SCHEMA_DDL`.

- [ ] **Step 3: Add the idempotent audit schema**

Insert after the `auto_lunch_settings` singleton seed in `_schema.py`:

```sql
-- Append-only Auto-Lunch setting history. A NULL before-value marks the first
-- observed baseline; settings/external rows snapshot both sides of a change.
CREATE TABLE IF NOT EXISTS auto_lunch_setting_events (
  id                         BIGSERIAL PRIMARY KEY,
  before_enabled             BOOLEAN,
  before_observe_only        BOOLEAN,
  before_flex_after_hours    NUMERIC,
  before_flex_minutes        INTEGER,
  after_enabled              BOOLEAN NOT NULL,
  after_observe_only         BOOLEAN NOT NULL,
  after_flex_after_hours     NUMERIC NOT NULL,
  after_flex_minutes         INTEGER NOT NULL,
  actor_upn                  TEXT,
  actor_name                 TEXT,
  source                     TEXT NOT NULL
                             CHECK (source IN ('settings','external','baseline')),
  changed_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS auto_lunch_setting_events_changed_at_idx
  ON auto_lunch_setting_events (changed_at DESC, id DESC);
```

- [ ] **Step 4: Run the schema test and verify GREEN**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_auto_lunch_schema_static.py -q
```

Expected: `1 passed`.

- [ ] **Step 5: Write failing audited-save tests**

Create `tests/test_auto_lunch_settings_audit.py` with a transaction fake that records SQL and controls `fetchone()`:

```python
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from zira_dashboard import auto_lunch_settings as settings, db


OFF = {
    "enabled": False, "observe_only": True,
    "flex_after_hours": 5.0, "flex_minutes": 30,
}


class RecordingCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


def cursor_context(cursor, *, fail_after_yield=False):
    @contextmanager
    def opened():
        yield cursor
        if fail_after_yield:
            raise RuntimeError("commit failed")
    return opened


def test_save_writes_setting_and_actor_audit_in_one_cursor(monkeypatch):
    cursor = RecordingCursor([OFF])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    cache_set = Mock()
    monkeypatch.setattr(settings._store, "set", cache_set)
    live = settings.Settings(True, False, 5.0, 30)

    assert settings.save(
        live, actor_upn="dale@gruberpallets.com", actor_name="Dale"
    ) is True

    assert len(cursor.executed) == 3
    assert "FOR UPDATE" in cursor.executed[0][0]
    assert "INSERT INTO auto_lunch_settings" in cursor.executed[1][0]
    assert "INSERT INTO auto_lunch_setting_events" in cursor.executed[2][0]
    assert cursor.executed[2][1][-3:] == (
        "dale@gruberpallets.com", "Dale", "settings"
    )
    cache_set.assert_called_once_with(live)


def test_save_of_identical_values_is_silent(monkeypatch):
    cursor = RecordingCursor([OFF])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))

    assert settings.save(settings.Settings()) is False

    assert len(cursor.executed) == 1


def test_cache_is_not_changed_when_transaction_commit_fails(monkeypatch):
    cursor = RecordingCursor([OFF])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor, fail_after_yield=True))
    cache_set = Mock()
    monkeypatch.setattr(settings._store, "set", cache_set)

    with pytest.raises(RuntimeError, match="commit failed"):
        settings.save(settings.Settings(True, False, 5.0, 30))

    cache_set.assert_not_called()
```

- [ ] **Step 6: Run the audited-save tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_auto_lunch_settings_audit.py -q
```

Expected: FAIL with `TypeError: save() got an unexpected keyword argument 'actor_upn'`.

- [ ] **Step 7: Implement atomic audited saves and recent-history reads**

Refactor `auto_lunch_settings.py` around these exact helpers and public signatures:

```python
_FIELDS = "enabled, observe_only, flex_after_hours, flex_minutes"


def _insert_event(cur, before: Settings | None, after: Settings,
                  actor_upn: str | None, actor_name: str | None,
                  source: str) -> None:
    before_values = (
        (before.enabled, before.observe_only, before.flex_after_hours,
         before.flex_minutes) if before is not None else (None, None, None, None)
    )
    cur.execute(
        "INSERT INTO auto_lunch_setting_events "
        "(before_enabled, before_observe_only, before_flex_after_hours, "
        " before_flex_minutes, after_enabled, after_observe_only, "
        " after_flex_after_hours, after_flex_minutes, actor_upn, actor_name, source) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (*before_values, after.enabled, after.observe_only,
         after.flex_after_hours, after.flex_minutes,
         actor_upn, actor_name, source),
    )


def save(s: Settings, *, actor_upn: str | None = None,
         actor_name: str | None = None, source: str = "settings") -> bool:
    from . import db
    if source not in {"settings", "external", "baseline"}:
        raise ValueError(f"invalid Auto-Lunch audit source: {source}")
    changed = False
    persisted = s
    with db.cursor() as cur:
        cur.execute(f"SELECT {_FIELDS} FROM auto_lunch_settings WHERE id = 1 FOR UPDATE")
        row = cur.fetchone()
        before = _row_to_settings(row) if row else None
        if before != s:
            cur.execute(
                "INSERT INTO auto_lunch_settings "
                "(id, enabled, observe_only, flex_after_hours, flex_minutes) "
                "VALUES (1, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET enabled = EXCLUDED.enabled, "
                "observe_only = EXCLUDED.observe_only, "
                "flex_after_hours = EXCLUDED.flex_after_hours, "
                "flex_minutes = EXCLUDED.flex_minutes",
                (s.enabled, s.observe_only, s.flex_after_hours, s.flex_minutes),
            )
            _insert_event(cur, before, s, actor_upn, actor_name, source)
            changed = True
        elif before is not None:
            persisted = before
    _store.set(persisted)
    return changed


def recent_events(limit: int = 20) -> list[dict]:
    from . import db
    return db.query(
        "SELECT id, before_enabled, before_observe_only, "
        "before_flex_after_hours, before_flex_minutes, after_enabled, "
        "after_observe_only, after_flex_after_hours, after_flex_minutes, "
        "actor_upn, actor_name, source, changed_at "
        "FROM auto_lunch_setting_events ORDER BY changed_at DESC, id DESC LIMIT %s",
        (max(1, min(int(limit), 100)),),
    )
```

Update `_load_from_db()` to use `_FIELDS`. Update the DB-backed fixture in
`tests/test_auto_lunch_settings.py` to delete `auto_lunch_setting_events`
before and after each test, and add a DB-backed assertion that one changed save
creates one event while an identical save creates none.

- [ ] **Step 8: Run Task 1 tests and verify GREEN**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_auto_lunch_schema_static.py tests/test_auto_lunch_settings_audit.py tests/test_auto_lunch_settings.py -q
```

Expected locally: pure tests pass and DB-backed tests skip. Expected in CI with
Postgres: all tests pass with no skips from these files.

- [ ] **Step 9: Commit Task 1**

```bash
git add src/zira_dashboard/_schema.py src/zira_dashboard/auto_lunch_settings.py tests/test_auto_lunch_schema_static.py tests/test_auto_lunch_settings_audit.py tests/test_auto_lunch_settings.py
git commit -m "feat(timeclock): audit auto-lunch setting changes"
```

---

### Task 2: Observe direct changes without hiding unsafe state

**Files:**
- Modify: `src/zira_dashboard/auto_lunch_settings.py`
- Create: `src/zira_dashboard/auto_lunch_guard.py`
- Modify: `tests/test_auto_lunch_settings_audit.py`
- Create: `tests/test_auto_lunch_guard.py`

**Interfaces:**
- Consumes: Task 1's `Settings`, `_insert_event`, `_row_to_settings`, `_FIELDS`, `reload()`, and cache.
- Produces: `reconcile_external_change() -> Settings`, `auto_lunch_guard.observe() -> Settings`, `mode_label(settings: Settings) -> str`, and `current_alert() -> dict | None`.

- [ ] **Step 1: Write failing guard behavior tests**

Create `tests/test_auto_lunch_guard.py`:

```python
from zira_dashboard import auto_lunch_guard as guard
from zira_dashboard.auto_lunch_settings import Settings


def test_live_mode_has_no_alert(monkeypatch):
    monkeypatch.setattr(guard, "observe", lambda: Settings(True, False, 5.0, 30))
    assert guard.current_alert() is None


def test_off_mode_returns_stable_urgent_inbox_row(monkeypatch):
    monkeypatch.setattr(guard, "observe", lambda: Settings(False, True, 5.0, 30))
    assert guard.current_alert() == {
        "name": "Auto-Lunch",
        "label": "Off",
        "detail": "Lunch deductions are not being written. Restore Live mode.",
        "priority": "urgent",
        "badge": "Timeclock",
        "href": "/settings?section=timeclock#auto-lunch-form",
        "row_key": "auto_lunch:setting",
        "item_key": "auto_lunch:setting",
    }


def test_observe_only_uses_plain_label(monkeypatch):
    monkeypatch.setattr(guard, "observe", lambda: Settings(True, True, 5.0, 30))
    assert guard.current_alert()["label"] == "Observe only"


def test_audit_failure_cannot_hide_off_alert(monkeypatch, caplog):
    off = Settings(False, True, 5.0, 30)
    monkeypatch.setattr(guard.auto_lunch_settings, "reload", lambda: off)
    monkeypatch.setattr(
        guard.auto_lunch_settings, "reconcile_external_change",
        lambda: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )

    assert guard.current_alert()["label"] == "Off"
    assert "external change audit failed" in caplog.text
```

- [ ] **Step 2: Run guard tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_auto_lunch_guard.py -q
```

Expected: collection FAIL because `zira_dashboard.auto_lunch_guard` does not exist.

- [ ] **Step 3: Implement guard observation and alert shaping**

Create `src/zira_dashboard/auto_lunch_guard.py`:

```python
"""Persisted Auto-Lunch observation and Exception Inbox alert shaping."""
from __future__ import annotations

import logging

from . import auto_lunch_settings
from .auto_lunch_settings import Settings

_log = logging.getLogger(__name__)
_DETAIL = "Lunch deductions are not being written. Restore Live mode."


def observe() -> Settings:
    persisted = auto_lunch_settings.reload()
    try:
        return auto_lunch_settings.reconcile_external_change()
    except Exception:
        _log.warning("Auto-Lunch external change audit failed", exc_info=True)
        return persisted


def mode_label(settings: Settings) -> str:
    if not settings.enabled:
        return "Off"
    if settings.observe_only:
        return "Observe only"
    return "Live"


def current_alert() -> dict | None:
    current = observe()
    label = mode_label(current)
    if label == "Live":
        return None
    return {
        "name": "Auto-Lunch",
        "label": label,
        "detail": _DETAIL,
        "priority": "urgent",
        "badge": "Timeclock",
        "href": "/settings?section=timeclock#auto-lunch-form",
        "row_key": "auto_lunch:setting",
        "item_key": "auto_lunch:setting",
    }
```

- [ ] **Step 4: Write failing external-reconciliation tests**

Append tests to `tests/test_auto_lunch_settings_audit.py` that use a
`RecordingCursor` with two `fetchone()` results: the locked persisted settings
row and the latest event row. Assert these behaviors:

```python
def test_reconcile_seeds_one_baseline_when_history_is_empty(monkeypatch):
    cursor = RecordingCursor([OFF, None])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))

    assert settings.reconcile_external_change() == settings.Settings()
    assert "FOR UPDATE" in cursor.executed[0][0]
    assert cursor.executed[-1][1][-1] == "baseline"


def test_reconcile_records_one_external_change(monkeypatch):
    latest = {
        "after_enabled": True, "after_observe_only": False,
        "after_flex_after_hours": 5.0, "after_flex_minutes": 30,
    }
    cursor = RecordingCursor([OFF, latest])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))

    settings.reconcile_external_change()

    assert cursor.executed[-1][1][-3:] == (None, None, "external")


def test_reconcile_does_not_duplicate_matching_signature(monkeypatch):
    latest = {
        "after_enabled": False, "after_observe_only": True,
        "after_flex_after_hours": 5.0, "after_flex_minutes": 30,
    }
    cursor = RecordingCursor([OFF, latest])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))

    settings.reconcile_external_change()

    assert len(cursor.executed) == 2
```

- [ ] **Step 5: Run reconciliation tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_auto_lunch_settings_audit.py -q
```

Expected: FAIL with `AttributeError` because `reconcile_external_change` is absent.

- [ ] **Step 6: Implement serialized external reconciliation**

Add to `auto_lunch_settings.py`:

```python
def _event_after_settings(row: dict) -> Settings:
    return Settings(
        enabled=bool(row["after_enabled"]),
        observe_only=bool(row["after_observe_only"]),
        flex_after_hours=float(row["after_flex_after_hours"]),
        flex_minutes=int(row["after_flex_minutes"]),
    )


def reconcile_external_change() -> Settings:
    from . import db
    with db.cursor() as cur:
        cur.execute(f"SELECT {_FIELDS} FROM auto_lunch_settings WHERE id = 1 FOR UPDATE")
        row = cur.fetchone()
        persisted = _row_to_settings(row) if row else DEFAULT
        cur.execute(
            "SELECT after_enabled, after_observe_only, after_flex_after_hours, "
            "after_flex_minutes FROM auto_lunch_setting_events "
            "ORDER BY changed_at DESC, id DESC LIMIT 1"
        )
        latest = cur.fetchone()
        if latest is None:
            _insert_event(cur, None, persisted, None, None, "baseline")
        else:
            audited = _event_after_settings(latest)
            if audited != persisted:
                _insert_event(cur, audited, persisted, None, None, "external")
    _store.set(persisted)
    return persisted
```

- [ ] **Step 7: Run Task 2 tests and verify GREEN**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_auto_lunch_guard.py tests/test_auto_lunch_settings_audit.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit Task 2**

```bash
git add src/zira_dashboard/auto_lunch_settings.py src/zira_dashboard/auto_lunch_guard.py tests/test_auto_lunch_settings_audit.py tests/test_auto_lunch_guard.py
git commit -m "feat(timeclock): detect unsafe auto-lunch modes"
```

---

### Task 3: Surface the non-Live mode in the Exception Inbox

**Files:**
- Modify: `src/zira_dashboard/inbox_keys.py:56-59`
- Modify: `src/zira_dashboard/exception_inbox.py:252-650`
- Modify: `tests/test_exception_inbox.py`

**Interfaces:**
- Consumes: `auto_lunch_guard.current_alert() -> dict | None` and Task 2's stable row fields.
- Produces: `inbox_keys.auto_lunch_setting() -> str`, summary section count `auto_lunch`, and snapshot section id `auto_lunch`.

- [ ] **Step 1: Isolate existing Inbox tests from persisted settings**

Add `auto_lunch_guard` to the imports in `tests/test_exception_inbox.py` and
add this autouse fixture before the first test:

```python
import pytest


@pytest.fixture(autouse=True)
def _live_auto_lunch(monkeypatch):
    monkeypatch.setattr(auto_lunch_guard, "current_alert", lambda: None)
```

Update the two exact section-count expectations to include
`"auto_lunch": 0`. Run the existing file and confirm it remains green before
adding new behavior.

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_exception_inbox.py -q
```

Expected: all existing Inbox tests pass.

- [ ] **Step 2: Write failing Inbox guard tests**

Append:

```python
def test_auto_lunch_off_is_one_urgent_inbox_item(monkeypatch):
    _empty_inbox_sources(monkeypatch)
    monkeypatch.setattr(exception_inbox, "_plant_schedule_reminder", lambda: (0, []))
    monkeypatch.setattr(machine_breakdown, "current_rows", lambda: [])
    monkeypatch.setattr(unexpected_worker, "open_events", lambda _day: [])
    row = {
        "name": "Auto-Lunch", "label": "Off",
        "detail": "Lunch deductions are not being written. Restore Live mode.",
        "priority": "urgent", "badge": "Timeclock",
        "href": "/settings?section=timeclock#auto-lunch-form",
        "row_key": "auto_lunch:setting", "item_key": "auto_lunch:setting",
    }
    monkeypatch.setattr(auto_lunch_guard, "current_alert", lambda: row)

    snapshot = exception_inbox.build_snapshot()

    section = next(s for s in snapshot["sections"] if s["id"] == "auto_lunch")
    assert section["count"] == 1
    assert section["rows"] == [row]
    assert snapshot["total"] == 1
    assert snapshot["urgent_total"] == 1
    assert snapshot["queue"][0]["item_key"] == "auto_lunch:setting"


def test_auto_lunch_alert_clears_when_guard_returns_none(monkeypatch):
    _empty_inbox_sources(monkeypatch)
    monkeypatch.setattr(exception_inbox, "_plant_schedule_reminder", lambda: (0, []))
    monkeypatch.setattr(machine_breakdown, "current_rows", lambda: [])
    monkeypatch.setattr(unexpected_worker, "open_events", lambda _day: [])
    monkeypatch.setattr(auto_lunch_guard, "current_alert", lambda: None)

    snapshot = exception_inbox.build_snapshot()

    section = next(s for s in snapshot["sections"] if s["id"] == "auto_lunch")
    assert section["count"] == 0
    assert section["rows"] == []
```

- [ ] **Step 3: Run the new Inbox tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_exception_inbox.py -q
```

Expected: FAIL because no `auto_lunch` section exists.

- [ ] **Step 4: Add the stable key and compose the guard into both Inbox views**

Add to `inbox_keys.py`:

```python
def auto_lunch_setting() -> str:
    """Identity for the singleton Auto-Lunch non-Live warning."""
    return "auto_lunch:setting"
```

In both `build_summary()` and `build_snapshot()`, import `auto_lunch_guard` and
capture the current row:

```python
auto_lunch_alert = _capture(
    source_errors, "Auto-Lunch", auto_lunch_guard.current_alert, None
)
auto_lunch_count = int(auto_lunch_alert is not None)
```

Add `auto_lunch_count` to summary `total`, `urgent_total`, and
`sections["auto_lunch"]`. Insert this section immediately after Timeclock
Roster in the snapshot:

```python
{
    "id": "auto_lunch",
    "title": "Auto-Lunch",
    "count": auto_lunch_count,
    "tone": "bad",
    "action_key": None,
    "action_label": None,
    "href": "/settings?section=timeclock#auto-lunch-form",
    "empty": "Live",
    "context": {},
    "rows": [auto_lunch_alert] if auto_lunch_alert else [],
},
```

Set the guard row's `row_key` and `item_key` from
`inbox_keys.auto_lunch_setting()` in `auto_lunch_guard.current_alert()` instead
of duplicating the literal.

- [ ] **Step 5: Run Inbox and guard tests and verify GREEN**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_exception_inbox.py tests/test_auto_lunch_guard.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/zira_dashboard/inbox_keys.py src/zira_dashboard/exception_inbox.py src/zira_dashboard/auto_lunch_guard.py tests/test_exception_inbox.py tests/test_auto_lunch_guard.py
git commit -m "feat(inbox): warn when auto-lunch is not live"
```

---

### Task 4: Capture the manager and render recent Settings history

**Files:**
- Modify: `src/zira_dashboard/settings_context.py:227-236`
- Modify: `src/zira_dashboard/routes/settings.py:424-430,723-754`
- Modify: `src/zira_dashboard/templates/settings.html:604-649`
- Modify: `tests/test_settings_context.py:305-323`
- Modify: `tests/test_settings_auto_lunch.py:48-94`

**Interfaces:**
- Consumes: `auto_lunch_settings.recent_events(20)`, `inbox_log.actor_from(request)`, and Task 1's actor-aware `save`.
- Produces: `settings_context.auto_lunch_history_context(events: list[dict]) -> list[dict]` and template variable `auto_lunch_history`.

- [ ] **Step 1: Write failing pure history-formatting tests**

Append to `tests/test_settings_context.py`:

```python
from datetime import UTC, datetime


def test_auto_lunch_history_context_uses_plain_labels_and_site_time():
    rows = [{
        "source": "settings",
        "actor_name": "Dale", "actor_upn": "dale@gruberpallets.com",
        "changed_at": datetime(2026, 8, 20, 21, 30, tzinfo=UTC),
        "before_enabled": False, "before_observe_only": True,
        "before_flex_after_hours": 5, "before_flex_minutes": 30,
        "after_enabled": True, "after_observe_only": False,
        "after_flex_after_hours": 5, "after_flex_minutes": 30,
    }]

    assert settings_context.auto_lunch_history_context(rows) == [{
        "time_label": "8/20/2026 4:30 PM",
        "before_label": "Off · 5 hours · 30 minutes",
        "after_label": "Live · 5 hours · 30 minutes",
        "actor_label": "Dale",
        "is_baseline": False,
    }]


def test_auto_lunch_history_context_labels_external_and_baseline():
    observed_at = datetime(2026, 8, 20, 21, 30, tzinfo=UTC)
    common = {
        "actor_name": None, "actor_upn": None, "changed_at": observed_at,
        "after_enabled": True, "after_observe_only": False,
        "after_flex_after_hours": 5, "after_flex_minutes": 30,
    }
    external = {
        **common, "source": "external",
        "before_enabled": False, "before_observe_only": True,
        "before_flex_after_hours": 5, "before_flex_minutes": 30,
    }
    baseline = {
        **common, "source": "baseline",
        "before_enabled": None, "before_observe_only": None,
        "before_flex_after_hours": None, "before_flex_minutes": None,
    }

    result = settings_context.auto_lunch_history_context([external, baseline])

    assert result[0]["actor_label"] == "Outside app / detected automatically"
    assert result[1]["actor_label"] == "Monitoring started"
    assert result[1]["is_baseline"] is True
```

- [ ] **Step 2: Run formatting tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_settings_context.py -q
```

Expected: FAIL with `AttributeError` because
`auto_lunch_history_context` is absent.

- [ ] **Step 3: Implement history view formatting**

Add to `settings_context.py`:

```python
def _auto_lunch_mode(enabled, observe_only) -> str:
    if not enabled:
        return "Off"
    return "Observe only" if observe_only else "Live"


def _auto_lunch_value_label(row: dict, prefix: str) -> str:
    mode = _auto_lunch_mode(row[f"{prefix}_enabled"], row[f"{prefix}_observe_only"])
    hours = float(row[f"{prefix}_flex_after_hours"])
    hours_label = f"{hours:g} hours"
    return f"{mode} · {hours_label} · {int(row[f'{prefix}_flex_minutes'])} minutes"


def auto_lunch_history_context(events: list[dict]) -> list[dict]:
    from . import shift_config
    shaped = []
    for row in events:
        source = row.get("source")
        baseline = source == "baseline"
        if baseline:
            actor_label = "Monitoring started"
        elif source == "external":
            actor_label = "Outside app / detected automatically"
        else:
            actor_label = row.get("actor_name") or row.get("actor_upn") or "Unknown manager"
        changed_at = row["changed_at"].astimezone(shift_config.SITE_TZ)
        shaped.append({
            "time_label": changed_at.strftime("%-m/%-d/%Y %-I:%M %p"),
            "before_label": None if baseline else _auto_lunch_value_label(row, "before"),
            "after_label": _auto_lunch_value_label(row, "after"),
            "actor_label": actor_label,
            "is_baseline": baseline,
        })
    return shaped
```

Leave `auto_lunch_context()` unchanged so its existing `off`, `observe`, and
`live` template contract stays intact.

- [ ] **Step 4: Write failing route and template tests**

Add these imports and tests to `tests/test_settings_auto_lunch.py`:

```python
from pathlib import Path


TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "src" / "zira_dashboard" / "templates" / "settings.html"
)


def test_auto_lunch_template_has_recent_history_block():
    html = TEMPLATE.read_text()
    assert 'id="auto-lunch-history"' in html
    assert "Recent Auto-Lunch changes" in html


@db_required
def test_post_passes_request_actor_to_audited_save(monkeypatch):
    from fastapi.testclient import TestClient
    from zira_dashboard import auto_lunch_settings, inbox_log
    from zira_dashboard.app import app

    captured = {}
    real_save = auto_lunch_settings.save

    def recording_save(updated, **kwargs):
        captured["kwargs"] = kwargs
        return real_save(updated, **kwargs)

    monkeypatch.setattr(
        inbox_log,
        "actor_from",
        lambda _request: ("manager@gruberpallets.com", "Plant Manager"),
    )
    monkeypatch.setattr(auto_lunch_settings, "save", recording_save)

    response = TestClient(app).post(
        "/settings/auto_lunch",
        data={"mode": "live", "flex_after_hours": "5", "flex_minutes": "30"},
        headers={"accept": "application/json"},
    )

    assert response.status_code == 200
    assert captured["kwargs"] == {
    "actor_upn": "manager@gruberpallets.com",
    "actor_name": "Plant Manager",
    }
```

Also seed one audit event in the DB-backed GET test and assert the response
contains `Recent Auto-Lunch changes`, `Live`, and the manager name.

- [ ] **Step 5: Run route/template tests and verify RED**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_settings_auto_lunch.py tests/test_settings_timeclock_layout.py -q
```

Expected with Postgres: FAIL because the route omits actor metadata and the
history block is absent. Locally, the DB-backed cases skip and the pure template
test fails because `auto-lunch-history` is absent.

- [ ] **Step 6: Pass actor metadata and history through the Settings route**

In the Settings GET handler, replace the existing Auto-Lunch context block with:

```python
from .. import auto_lunch_settings
_al = auto_lunch_settings.current()
auto_lunch_ctx = settings_context.auto_lunch_context(_al)
try:
    auto_lunch_history_ctx = settings_context.auto_lunch_history_context(
        auto_lunch_settings.recent_events(20)
    )
except Exception:
    logging.warning("Auto-Lunch history unavailable", exc_info=True)
    auto_lunch_history_ctx = []
```

Add `"auto_lunch_history": auto_lunch_history_ctx` to the template context.
In the POST handler, read the actor before `_work()` and pass it to `save`:

```python
from .. import inbox_log
actor_upn, actor_name = inbox_log.actor_from(request)

# inside _work(), after constructing `updated`
auto_lunch_settings.save(
    updated, actor_upn=actor_upn, actor_name=actor_name
)
```

- [ ] **Step 7: Render the newest history below the form**

Add immediately after `</form>` in `settings.html`:

```html
<div id="auto-lunch-history" style="margin-top:1rem">
  <h4 class="rounding-subhead">Recent Auto-Lunch changes</h4>
  {% if auto_lunch_history %}
    <ul style="font-size:.88rem;line-height:1.5;margin:.35rem 0 0 1.2rem">
      {% for event in auto_lunch_history %}
      <li>
        <strong>{{ event.time_label }}</strong> · {{ event.actor_label }}<br>
        {% if event.is_baseline %}
          {{ event.after_label }}
        {% else %}
          {{ event.before_label }} → {{ event.after_label }}
        {% endif %}
      </li>
      {% endfor %}
    </ul>
  {% else %}
    <p class="help">No changes recorded yet.</p>
  {% endif %}
</div>
```

- [ ] **Step 8: Run Task 4 tests and verify GREEN**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest tests/test_settings_context.py tests/test_settings_auto_lunch.py tests/test_settings_timeclock_layout.py -q
```

Expected locally: pure/static tests pass and DB-backed tests skip. Expected in
CI with Postgres: all tests pass.

- [ ] **Step 9: Commit Task 4**

```bash
git add src/zira_dashboard/settings_context.py src/zira_dashboard/routes/settings.py src/zira_dashboard/templates/settings.html tests/test_settings_context.py tests/test_settings_auto_lunch.py
git commit -m "feat(settings): show auto-lunch change history"
```

---

### Task 5: Release note, full verification, main integration, and production checks

**Files:**
- Modify: `CHANGELOG.md`
- Verify: all files changed by Tasks 1-4

**Interfaces:**
- Consumes: the complete guard, Inbox, and Settings-history behavior.
- Produces: a tested main-branch deployment with production still in Live mode.

- [ ] **Step 1: Replace the plan-only patch note with shipped behavior**

Under `### Auto-Lunch safety plan`, replace the plan-only bullet with:

```markdown
### Auto-Lunch stays visible

#### Features

- **The Inbox now warns when Auto-Lunch is not Live.** The warning stays until lunch deductions are turned back on.
- **Auto-Lunch now remembers setting changes.** The Settings page shows what changed, when it changed, and who changed it when the app knows their name.
```

- [ ] **Step 2: Run the complete focused test set**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest \
  tests/test_auto_lunch_schema_static.py \
  tests/test_auto_lunch_settings_audit.py \
  tests/test_auto_lunch_guard.py \
  tests/test_auto_lunch_settings.py \
  tests/test_exception_inbox.py \
  tests/test_settings_context.py \
  tests/test_settings_auto_lunch.py \
  tests/test_settings_timeclock_layout.py -q
```

Expected: all non-DB tests pass; DB-backed tests are skipped only because
`DATABASE_URL` is deliberately empty to protect production.

- [ ] **Step 3: Run Ruff on every changed Python file**

Run:

```bash
DATABASE_URL= .venv/bin/ruff check \
  src/zira_dashboard/_schema.py \
  src/zira_dashboard/auto_lunch_settings.py \
  src/zira_dashboard/auto_lunch_guard.py \
  src/zira_dashboard/inbox_keys.py \
  src/zira_dashboard/exception_inbox.py \
  src/zira_dashboard/settings_context.py \
  src/zira_dashboard/routes/settings.py \
  tests/test_auto_lunch_schema_static.py \
  tests/test_auto_lunch_settings_audit.py \
  tests/test_auto_lunch_guard.py \
  tests/test_exception_inbox.py \
  tests/test_settings_context.py \
  tests/test_settings_auto_lunch.py \
  tests/test_settings_timeclock_layout.py
```

Expected: `All checks passed!`.

- [ ] **Step 4: Run the full DB-disabled suite and classify unrelated failures**

Run:

```bash
DATABASE_URL= .venv/bin/python -m pytest -q
```

Expected: no failure in any Auto-Lunch, Inbox, Settings-context, or schema
test. If the known browser-sandbox or DB-required environment failures recur,
record their exact test names and confirm they also fail on `origin/main`
before treating them as pre-existing.

- [ ] **Step 5: Review the final diff against the design**

Run:

```bash
git diff --check
git diff --stat origin/main...HEAD
git diff origin/main...HEAD -- \
  src/zira_dashboard/_schema.py \
  src/zira_dashboard/auto_lunch_settings.py \
  src/zira_dashboard/auto_lunch_guard.py \
  src/zira_dashboard/inbox_keys.py \
  src/zira_dashboard/exception_inbox.py \
  src/zira_dashboard/settings_context.py \
  src/zira_dashboard/routes/settings.py \
  src/zira_dashboard/templates/settings.html \
  CHANGELOG.md
```

Expected: no whitespace errors; every design requirement maps to an
implementation and test; no unrelated user file appears.

- [ ] **Step 6: Commit the release note and any final test-only adjustment**

```bash
git add CHANGELOG.md
git commit -m "docs: announce auto-lunch inbox guard"
```

- [ ] **Step 7: Integrate the implementation onto `main` and push**

From the primary checkout, merge the isolated implementation branch without
discarding unrelated work, then run the focused command from Step 2 again.
Push only after the fresh focused run passes:

```bash
git push origin main
```

- [ ] **Step 8: Verify Railway without changing production mode**

Wait for the `web` service deployment to report `SUCCESS`, then confirm:

```text
GET /healthz -> 200
auto_lunch_settings.enabled = true
auto_lunch_settings.observe_only = false
auto_lunch_setting_events contains one baseline or later Live event
Exception Inbox Auto-Lunch section count = 0
```

Use read-only production queries and service logs. Do not switch the mode away
from Live as a deployment test.

---

## Plan Self-Review Checklist

- Every goal and non-goal in the approved design maps to a task above.
- Public signatures are consistent across producing and consuming tasks.
- Every behavior change begins with a failing test and an explicit RED run.
- The external audit failure path still returns the persisted unsafe state.
- Production verification keeps Auto-Lunch Live and uses only read-only checks.
- The final push includes plain-language What's New notes and no unrelated files.
