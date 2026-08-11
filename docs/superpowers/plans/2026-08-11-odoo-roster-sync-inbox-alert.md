# Odoo Roster Sync Inbox Alert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface rejected Odoo roster updates as an urgent, self-clearing Exception Inbox item.

**Architecture:** `odoo_sync` persists a small JSON alert only when the active-only Odoo employee response contains a value other than the Boolean `True`; it clears the alert only after a fresh complete sync succeeds. `exception_inbox` reads that durable state into one urgent row, and the existing reconciler archives it automatically after it clears.

**Tech Stack:** Python 3.11+, FastAPI, Postgres JSONB via `app_settings`, pytest.

## Global Constraints

- Unsafe Odoo roster payloads must never alter `people` rows.
- The alert must remain visible through process restarts and cache-only sync TTL hits.
- The alert must not write to Odoo; only a successful fresh Odoo roster sync clears it.
- User-facing text must say the timeclock is using the last good roster.

---

### Task 1: Persist and display the unsafe-roster alert

**Files:**

- Modify: `src/zira_dashboard/odoo_sync.py:53-91,210-250,406-413`
- Modify: `src/zira_dashboard/exception_inbox.py:252-315,610-670`
- Modify: `src/zira_dashboard/inbox_keys.py:39-42`
- Modify: `src/zira_dashboard/inbox_reconcile.py:26-45`
- Modify: `tests/test_odoo_client.py:14-63`
- Modify: `tests/test_exception_inbox.py:486-560`
- Modify: `tests/test_inbox_keys.py:1-11`

**Interfaces:**

- Produces `odoo_sync.roster_sync_alert() -> dict | None`, with `invalid_count`, `error`, and `detected_at` when an unsafe Odoo response is rejected.
- Produces `inbox_keys.odoo_roster_sync() -> str`, returning the stable key `odoo_roster_sync:active_status`.
- Consumes that alert in `exception_inbox.build_summary()` and `build_snapshot()` as the `odoo_roster_sync` section.

- [ ] **Step 1: Write the failing tests**

Add this to `tests/test_odoo_client.py` beside the malformed-active test:

```python
def test_sync_records_an_alert_for_a_non_boolean_active_value(monkeypatch):
    from zira_dashboard import app_settings, odoo_sync

    saved = []
    monkeypatch.setattr(odoo_sync, "_read_last_sync", lambda: None)
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employees",
        lambda: [{"id": 1, "name": "Malformed Active", "active": 0}],
    )
    monkeypatch.setattr(app_settings, "set_setting", lambda key, value: saved.append((key, value)))

    result = odoo_sync.sync(force=True)

    assert result.ok is False
    assert saved[0][0] == odoo_sync.ROSTER_SYNC_ALERT_KEY
    assert saved[0][1]["invalid_count"] == 1
    assert "active-only" in saved[0][1]["error"]
```

Add this Inbox test to `tests/test_exception_inbox.py`, using the file's
existing source stubs to make every unrelated section empty:

```python
def test_build_snapshot_surfaces_an_unsafe_roster_sync_as_urgent(monkeypatch):
    from zira_dashboard import odoo_sync

    _empty_inbox_sources(monkeypatch)
    monkeypatch.setattr(
        odoo_sync,
        "roster_sync_alert",
        lambda: {"invalid_count": 36, "detected_at": "2026-08-11T19:24:45+00:00"},
    )

    snapshot = exception_inbox.build_snapshot()

    section = next(s for s in snapshot["sections"] if s["id"] == "odoo_roster_sync")
    assert section["count"] == 1
    assert section["rows"][0]["priority"] == "urgent"
    assert section["rows"][0]["item_key"] == "odoo_roster_sync:active_status"
    assert "last good update" in section["rows"][0]["detail"]
    assert snapshot["total"] == 1
    assert snapshot["urgent_total"] == 1
```

Add this assertion to `tests/test_inbox_keys.py`:

```python
assert inbox_keys.odoo_roster_sync() == "odoo_roster_sync:active_status"
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```bash
.venv/bin/pytest \
  tests/test_odoo_client.py::test_sync_records_an_alert_for_a_non_boolean_active_value \
  tests/test_exception_inbox.py::test_build_snapshot_surfaces_an_unsafe_roster_sync_as_urgent \
  tests/test_inbox_keys.py -q
```

Expected: FAIL because the sync does not yet persist an alert and the Inbox has no `odoo_roster_sync` section or key helper.

- [ ] **Step 3: Add the minimal alert persistence API**

In `src/zira_dashboard/odoo_sync.py`, add the setting key and reader near
`SyncResult`:

```python
ROSTER_SYNC_ALERT_KEY = "odoo_roster_sync_alert"


def roster_sync_alert() -> dict | None:
    from . import app_settings

    value = app_settings.get_setting(ROSTER_SYNC_ALERT_KEY)
    return value if isinstance(value, dict) and value.get("error") else None
```

When `inactive_count` is nonzero, build the existing error string once, persist
it, and return the failed `SyncResult` without fetching skills or writing
people:

```python
error = (
    "Odoo employee payload contained "
    f"{inactive_count} inactive or malformed record(s) despite the active-only query; "
    "sync skipped."
)
from . import app_settings
app_settings.set_setting(
    ROSTER_SYNC_ALERT_KEY,
    {
        "invalid_count": inactive_count,
        "error": error,
        "detected_at": now.isoformat(),
    },
)
return SyncResult(
    ok=False, refreshed=False, employee_count=0, skill_column_count=0,
    last_sync_at=last, error=error,
)
```

Immediately after `_write_last_sync(pulled_at)` on the successful fresh-sync
path, clear the durable alert:

```python
from . import app_settings
app_settings.set_setting(ROSTER_SYNC_ALERT_KEY, None)
```

- [ ] **Step 4: Add the Inbox row and reconciliation identity**

Add the stable key in `src/zira_dashboard/inbox_keys.py`:

```python
def odoo_roster_sync() -> str:
    return "odoo_roster_sync:active_status"
```

In `exception_inbox`, load `odoo_sync.roster_sync_alert()` through `_capture`
in both builders. Add one `odoo_roster_sync` section before `assignments` in
`build_snapshot()` when the alert exists:

```python
{
    "id": "odoo_roster_sync",
    "title": "Timeclock Roster",
    "count": int(bool(roster_sync_alert)),
    "tone": "bad",
    "action_key": None,
    "action_label": None,
    "href": "/staffing/skills",
    "empty": "All clear",
    "context": {},
    "rows": [{
        "name": "Timeclock roster",
        "label": "Odoo roster update blocked",
        "detail": (
            "Odoo sent invalid active-status data for "
            f"{int(roster_sync_alert.get('invalid_count') or 0)} people. "
            "The timeclock is using the last good update."
        ),
        "priority": "urgent",
        "badge": "Roster",
        "href": "/staffing/skills",
        "row_key": _row_key("odoo_roster_sync", "active_status"),
        "item_key": inbox_keys.odoo_roster_sync(),
    }] if roster_sync_alert else [],
}
```

Include the same count in `build_summary()`'s `total`, `urgent_total`, and
`sections` dictionary. In `inbox_reconcile.py`, add the section and source
mapping so the existing 90-second self-clear archive behavior applies:

```python
"odoo_roster_sync": "odoo_roster_sync",
...
"odoo_roster_sync": "Timeclock Roster",
```

- [ ] **Step 5: Run focused tests to verify they pass**

Run:

```bash
.venv/bin/pytest \
  tests/test_odoo_client.py \
  tests/test_exception_inbox.py \
  tests/test_inbox_keys.py \
  tests/test_inbox_reconcile.py -q
```

Expected: PASS. The malformed payload has one durable alert, it produces one
urgent Inbox row, and the current Inbox lifecycle still accepts its new kind.

- [ ] **Step 6: Run static checks and commit**

Run:

```bash
.venv/bin/ruff check \
  src/zira_dashboard/odoo_sync.py \
  src/zira_dashboard/exception_inbox.py \
  src/zira_dashboard/inbox_keys.py \
  src/zira_dashboard/inbox_reconcile.py \
  tests/test_odoo_client.py \
  tests/test_exception_inbox.py \
  tests/test_inbox_keys.py
```

Then commit only the implementation files and tests:

```bash
git add \
  src/zira_dashboard/odoo_sync.py \
  src/zira_dashboard/exception_inbox.py \
  src/zira_dashboard/inbox_keys.py \
  src/zira_dashboard/inbox_reconcile.py \
  tests/test_odoo_client.py \
  tests/test_exception_inbox.py \
  tests/test_inbox_keys.py
git commit -m "feat(inbox): alert on rejected Odoo roster sync"
```
