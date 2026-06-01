# Timeclock ↔ Odoo State Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the timeclock punch screen reflect Odoo's true attendance state (including punches added/closed/deleted/edited directly in Odoo) without any Odoo call on the tap path, make clock-in incapable of creating a duplicate open attendance, and hard-delete time-off rows when the leave is removed in Odoo.

**Architecture:** A ~30s background warmer mirrors every open `hr.attendance` into a single-row JSONB cache (`odoo_open_attendance_cache`). The punch screen's `_current_state()` reads that cache and reconciles it against the local `timeclock_punches_log`, trusting the local log only for punches the cache can't have reflected yet (the `synced_at`/`refreshed_at` race-guard). The clock-in sync path becomes self-correcting (adopts an already-open Odoo attendance instead of creating a duplicate). For time off, the existing 60s poller's "missing from Odoo" branch changes from soft-cancel to hard-delete, keeping the reverse cascade.

**Tech Stack:** Python 3.x, FastAPI, psycopg2 + Postgres (JSONB), Odoo XML-RPC, pytest. Follows the existing `live_cache` / `timeclock_sync` / `time_off_sync` patterns.

### Local verification protocol (READ FIRST)

This project's local interpreter is **Python 3.9 and cannot run the pytest suite**; production is **Railway auto-deploy on push to `main`**. So in each task:

- **Authoritative test command** (runs in CI / any env with deps): the `pytest ...` line shown.
- **Local verification** (Python 3.9): `python -m py_compile <changed files>` to catch syntax/parse errors, plus the targeted import/ast smoke shown in the final task. The pytest assertions are real and run in CI — write them properly; do not water them down.

All new modules/test files MUST start with `from __future__ import annotations` so `X | None` annotations parse under 3.9.

---

## File Structure

**Modify:**
- `src/zira_dashboard/db.py` — add the `odoo_open_attendance_cache` DDL to `_SCHEMA_DDL`.
- `src/zira_dashboard/odoo_client.py` — add `fetch_open_attendances()`, `_odoo_dt_to_iso()`, `set_attendance_wc()`.
- `src/zira_dashboard/live_cache.py` — add `read_open_attendance()`, `write_open_attendance()`, `refresh_odoo_open_attendance()`.
- `src/zira_dashboard/routes/timeclock.py` — rewrite `_current_state()` to reconcile; add `_latest_punch()`, `_trust_local()`, `_state_from_log()`; import `live_cache`.
- `src/zira_dashboard/timeclock_sync.py` — make `_retry_one()` clock-in self-correcting.
- `src/zira_dashboard/app.py` — add `_warm_odoo_attendance_loop()` and wire it into `lifespan`.
- `src/zira_dashboard/time_off_sync.py` — rename `_mark_missing_as_cancel()` → `_delete_missing_from_odoo()`, change to hard delete; update call site + docstrings.

**Create (tests):**
- `tests/test_odoo_open_attendance.py` — `fetch_open_attendances`, `refresh_odoo_open_attendance`, `set_attendance_wc`.
- `tests/test_timeclock_state_reconciliation.py` — `_current_state`, `_trust_local`, `_state_from_log`.
- `tests/test_timeclock_sync_dedup.py` — `_retry_one` self-correcting clock-in.

**Add tests to existing file:**
- `tests/test_time_off_sync.py` — hard-delete behavior of `_delete_missing_from_odoo`.

---

## Task 1: Schema — `odoo_open_attendance_cache` table

**Files:**
- Modify: `src/zira_dashboard/db.py` (inside the `_SCHEMA_DDL` string, after the `today_production_cache` table near line 563)

- [ ] **Step 1: Add the table DDL**

In `src/zira_dashboard/db.py`, find the end of the live-cache tables block in `_SCHEMA_DDL` (immediately after the `today_production_cache` `CREATE TABLE ... );`) and insert:

```sql
-- Odoo open-attendance snapshot (2026-06-01) ---------------------------
-- Single-row mirror of every currently-open hr.attendance (check_out IS
-- NULL), keyed by person_odoo_id inside the JSONB snapshot. The ~30s
-- warmer (_warm_odoo_attendance_loop in app.py) overwrites it; the
-- timeclock punch screen reconciles it against timeclock_punches_log so
-- punches added/closed/deleted directly in Odoo show up without an
-- XML-RPC call on the tap. Forced single row (id=1) so refreshed_at is a
-- GLOBAL freshness marker: "person absent from snapshot" only means
-- clocked-out when the snapshot is known-fresh.
CREATE TABLE IF NOT EXISTS odoo_open_attendance_cache (
  id           INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  snapshot     JSONB NOT NULL DEFAULT '{}'::jsonb,
  refreshed_at TIMESTAMPTZ
);
```

- [ ] **Step 2: Verify the DDL parses and the module imports**

Run: `python -m py_compile src/zira_dashboard/db.py`
Expected: exit 0, no output.

Run (only in an env with `DATABASE_URL` set — optional locally): `python -c "from zira_dashboard import db; db.init_pool(); db.bootstrap_schema(); print('ok')"`
Expected: prints `ok` (table created idempotently). If no `DATABASE_URL`, skip — Railway runs `bootstrap_schema()` on boot.

- [ ] **Step 3: Commit**

```bash
git add src/zira_dashboard/db.py
git commit -m "feat(timeclock): add odoo_open_attendance_cache table"
```

---

## Task 2: Odoo client — fetch open attendances + WC writer

**Files:**
- Modify: `src/zira_dashboard/odoo_client.py` (add functions after `get_current_attendance`, near line 348)
- Test: `tests/test_odoo_open_attendance.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_odoo_open_attendance.py`:

```python
"""Tests for the Odoo open-attendance fetch + WC writer (odoo_client) and
the live_cache snapshot refresh. All pure-logic: odoo_client.execute and the
cache writer are stubbed, so no Odoo and no Postgres are needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from zira_dashboard import odoo_client


def test_fetch_open_attendances_maps_rows(monkeypatch):
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", "x_kiosk_wc")
    fake = MagicMock(return_value=[
        {"id": 88, "employee_id": [5, "Bob"],
         "check_in": "2026-06-01 11:02:00", "x_kiosk_wc": "Bay 3 Nailer"},
        {"id": 90, "employee_id": [7, "Al"],
         "check_in": "2026-06-01 12:15:00", "x_kiosk_wc": False},
    ])
    monkeypatch.setattr(odoo_client, "execute", fake)

    out = odoo_client.fetch_open_attendances()

    # Domain filters to open rows; WC field requested because env is set.
    args, kwargs = fake.call_args
    assert args[0] == "hr.attendance" and args[1] == "search_read"
    assert ("check_out", "=", False) in args[2]
    assert "x_kiosk_wc" in kwargs["fields"]

    assert out == [
        {"att_id": 88, "employee_odoo_id": 5,
         "check_in": "2026-06-01T11:02:00+00:00", "wc_name": "Bay 3 Nailer"},
        {"att_id": 90, "employee_odoo_id": 7,
         "check_in": "2026-06-01T12:15:00+00:00", "wc_name": None},
    ]


def test_fetch_open_attendances_no_wc_field(monkeypatch):
    monkeypatch.delenv("ODOO_KIOSK_WC_FIELD", raising=False)
    fake = MagicMock(return_value=[
        {"id": 88, "employee_id": [5, "Bob"], "check_in": "2026-06-01 11:02:00"},
    ])
    monkeypatch.setattr(odoo_client, "execute", fake)

    out = odoo_client.fetch_open_attendances()

    _args, kwargs = fake.call_args
    assert kwargs["fields"] == ["id", "employee_id", "check_in"]
    assert out == [{"att_id": 88, "employee_odoo_id": 5,
                    "check_in": "2026-06-01T11:02:00+00:00", "wc_name": None}]


def test_odoo_dt_to_iso_parses_naive_utc():
    assert (odoo_client._odoo_dt_to_iso("2026-06-01 11:02:00")
            == datetime(2026, 6, 1, 11, 2, tzinfo=timezone.utc).isoformat())
    assert odoo_client._odoo_dt_to_iso(False) is None
    assert odoo_client._odoo_dt_to_iso(None) is None


def test_set_attendance_wc_writes_field(monkeypatch):
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", "x_kiosk_wc")
    monkeypatch.delenv("ODOO_KIOSK_DEPARTMENT_FIELD", raising=False)
    fake = MagicMock()
    monkeypatch.setattr(odoo_client, "execute", fake)

    odoo_client.set_attendance_wc(88, "Bay 3 Nailer")

    fake.assert_called_once_with(
        "hr.attendance", "write", [88], {"x_kiosk_wc": "Bay 3 Nailer"})


def test_set_attendance_wc_noop_without_field(monkeypatch):
    monkeypatch.delenv("ODOO_KIOSK_WC_FIELD", raising=False)
    fake = MagicMock()
    monkeypatch.setattr(odoo_client, "execute", fake)

    odoo_client.set_attendance_wc(88, "Bay 3 Nailer")
    odoo_client.set_attendance_wc(88, None)  # also no-op

    fake.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_odoo_open_attendance.py -v`
Expected: FAIL — `AttributeError: module 'zira_dashboard.odoo_client' has no attribute 'fetch_open_attendances'` (and `_odoo_dt_to_iso`, `set_attendance_wc`).
(Local Python 3.9: if pytest can't run, this step is satisfied by confirming the functions don't yet exist.)

- [ ] **Step 3: Implement the functions**

In `src/zira_dashboard/odoo_client.py`, add immediately after `get_current_attendance` (after line 348):

```python
def _odoo_dt_to_iso(value: Any) -> str | None:
    """Odoo returns datetimes as naive-UTC 'YYYY-MM-DD HH:MM:SS' strings
    (and False for empty). Return an ISO-8601 string with an explicit UTC
    offset, or None."""
    if not value:
        return None
    if isinstance(value, str):
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc)
        return dt.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return None


def fetch_open_attendances() -> list[dict]:
    """Every currently-open hr.attendance (check_out IS NULL), one entry
    per clocked-in employee. Returns
    [{att_id, employee_odoo_id, check_in, wc_name}, ...] where check_in is
    an ISO-8601 UTC string and wc_name is None when the kiosk WC field is
    unset or empty (e.g. a punch added by hand directly in Odoo)."""
    wc_field = _kiosk_wc_field()
    fields = ["id", "employee_id", "check_in"]
    if wc_field:
        fields.append(wc_field)
    rows = execute(
        "hr.attendance", "search_read",
        [("check_out", "=", False)],
        fields=fields,
    )
    out: list[dict] = []
    for r in rows:
        emp = r.get("employee_id")
        emp_id = emp[0] if isinstance(emp, list) else emp
        if not emp_id:
            continue
        out.append({
            "att_id": r["id"],
            "employee_odoo_id": emp_id,
            "check_in": _odoo_dt_to_iso(r.get("check_in")),
            "wc_name": (r.get(wc_field) or None) if wc_field else None,
        })
    return out


def set_attendance_wc(attendance_id: int, wc_name: str | None) -> None:
    """Write the kiosk WC (and resolved department) onto an existing
    hr.attendance. No-op when the WC field isn't configured or wc_name is
    empty. Used when the sync adopts a manually-created open attendance, so
    kiosk WC/department reports still attribute it."""
    wc_field = _kiosk_wc_field()
    if not wc_field or not wc_name:
        return
    payload: dict[str, Any] = {wc_field: wc_name}
    dept_field = _kiosk_department_field()
    if dept_field:
        dept_id = _department_id_for_wc(wc_name)
        if dept_id:
            payload[dept_field] = dept_id
    execute("hr.attendance", "write", [attendance_id], payload)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_odoo_open_attendance.py -v`
Expected: 5 passed.
Local Python 3.9: `python -m py_compile src/zira_dashboard/odoo_client.py tests/test_odoo_open_attendance.py` → exit 0.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/odoo_client.py tests/test_odoo_open_attendance.py
git commit -m "feat(timeclock): fetch open Odoo attendances + WC writer"
```

---

## Task 3: live_cache — open-attendance snapshot read/write/refresh

**Files:**
- Modify: `src/zira_dashboard/live_cache.py` (add functions after `read_production`, near line 70)
- Test: `tests/test_odoo_open_attendance.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_odoo_open_attendance.py`:

```python
def test_refresh_builds_keyed_snapshot(monkeypatch):
    from zira_dashboard import live_cache
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.fetch_open_attendances",
        lambda: [
            {"att_id": 88, "employee_odoo_id": 5,
             "check_in": "2026-06-01T11:02:00+00:00", "wc_name": "Bay 3"},
            {"att_id": 90, "employee_odoo_id": 7,
             "check_in": "2026-06-01T12:15:00+00:00", "wc_name": None},
        ],
    )
    written = {}
    monkeypatch.setattr(live_cache, "write_open_attendance",
                        lambda snap: written.update(snap))

    live_cache.refresh_odoo_open_attendance()

    assert written == {
        "5": {"att_id": 88, "check_in": "2026-06-01T11:02:00+00:00",
              "wc_name": "Bay 3"},
        "7": {"att_id": 90, "check_in": "2026-06-01T12:15:00+00:00",
              "wc_name": None},
    }


def test_refresh_swallows_errors(monkeypatch):
    from zira_dashboard import live_cache

    def boom():
        raise RuntimeError("odoo down")

    monkeypatch.setattr("zira_dashboard.odoo_client.fetch_open_attendances", boom)
    wrote = []
    monkeypatch.setattr(live_cache, "write_open_attendance",
                        lambda snap: wrote.append(snap))

    # Must not raise — the warmer relies on this.
    live_cache.refresh_odoo_open_attendance()
    assert wrote == []  # nothing written on failure
```

Note: `refresh_odoo_open_attendance` imports `odoo_client` *inside* the function (matching the existing `live_cache` pattern), so patch the source module by string path — `"zira_dashboard.odoo_client.fetch_open_attendances"` — exactly as the existing `test_refresh_attendance_*` tests do. `write_open_attendance` is a module-level function, so `monkeypatch.setattr(live_cache, "write_open_attendance", ...)` works directly.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_odoo_open_attendance.py -k refresh -v`
Expected: FAIL — `AttributeError: module 'zira_dashboard.live_cache' has no attribute 'refresh_odoo_open_attendance'`.

- [ ] **Step 3: Implement the cache functions**

In `src/zira_dashboard/live_cache.py`, add after `read_production` (after line 70):

```python
# ---- Odoo open-attendance snapshot (single-row, keyed by person id) ----


def write_open_attendance(snapshot: dict) -> None:
    """Overwrite the single-row Odoo open-attendance snapshot and stamp
    refreshed_at. `snapshot` is {str(person_odoo_id): {att_id, check_in,
    wc_name}}."""
    from . import db
    db.execute(
        """
        INSERT INTO odoo_open_attendance_cache (id, snapshot, refreshed_at)
        VALUES (1, %s::jsonb, now())
        ON CONFLICT (id) DO UPDATE SET
          snapshot = EXCLUDED.snapshot,
          refreshed_at = now()
        """,
        (json.dumps(snapshot, default=str),),
    )


def read_open_attendance() -> tuple[dict | None, datetime | None]:
    """Return (snapshot, refreshed_at). (None, None) if the warmer has
    never run. An empty dict snapshot means 'Odoo shows nobody clocked in'
    — distinct from None, which means 'no data yet, fall back to local'."""
    from . import db
    rows = db.query(
        "SELECT snapshot, refreshed_at FROM odoo_open_attendance_cache "
        "WHERE id = 1"
    )
    if not rows:
        return (None, None)
    return (rows[0]["snapshot"], rows[0]["refreshed_at"])


def refresh_odoo_open_attendance() -> None:
    """Pull every open hr.attendance from Odoo and overwrite the keyed
    snapshot. Errors are logged and swallowed — the previous good snapshot
    stays in place, then falls back to local once it crosses is_stale."""
    try:
        from . import odoo_client
        rows = odoo_client.fetch_open_attendances()
        snapshot = {
            str(r["employee_odoo_id"]): {
                "att_id": r["att_id"],
                "check_in": r["check_in"],
                "wc_name": r["wc_name"],
            }
            for r in rows
        }
        write_open_attendance(snapshot)
    except Exception as e:  # noqa: BLE001 — warmer must never die
        _log.warning("refresh_odoo_open_attendance failed: %s", e)
```

(`json`, `datetime`, and `_log` are already imported at the top of `live_cache.py`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_odoo_open_attendance.py -v`
Expected: 7 passed.
Local Python 3.9: `python -m py_compile src/zira_dashboard/live_cache.py tests/test_odoo_open_attendance.py` → exit 0.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/live_cache.py tests/test_odoo_open_attendance.py
git commit -m "feat(timeclock): live_cache read/write/refresh for Odoo open attendance"
```

---

## Task 4: Reconciled read — rewrite `_current_state`

**Files:**
- Modify: `src/zira_dashboard/routes/timeclock.py` (import line 56; replace `_current_state` at lines 166-203)
- Test: `tests/test_timeclock_state_reconciliation.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_timeclock_state_reconciliation.py`:

```python
"""Tests for the timeclock punch-screen state reconciliation.

_current_state blends two local sources: the Odoo open-attendance snapshot
(live_cache.read_open_attendance) and the latest timeclock_punches_log row
(via db.query). These tests stub both, so no Odoo and no Postgres needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from zira_dashboard.routes import timeclock


def _now():
    return datetime.now(timezone.utc)


def _set_cache(monkeypatch, snapshot, refreshed_at):
    monkeypatch.setattr(timeclock.live_cache, "read_open_attendance",
                        lambda: (snapshot, refreshed_at))


def _set_latest_punch(monkeypatch, row):
    # _latest_punch issues exactly one db.query; return [row] or [].
    monkeypatch.setattr(timeclock.db, "query",
                        lambda sql, params=None: ([row] if row else []))


# ---- _trust_local (pure predicate) -------------------------------------

def test_trust_local_none_punch_false():
    assert timeclock._trust_local(None, _now()) is False


def test_trust_local_unsynced_true():
    punch = {"synced_to_odoo": False, "synced_at": None}
    assert timeclock._trust_local(punch, _now()) is True


def test_trust_local_synced_but_no_synced_at_true():
    punch = {"synced_to_odoo": True, "synced_at": None}
    assert timeclock._trust_local(punch, _now()) is True


def test_trust_local_cache_predates_sync_true():
    synced = _now()
    refreshed = synced - timedelta(seconds=10)  # cache older than the sync
    punch = {"synced_to_odoo": True, "synced_at": synced}
    assert timeclock._trust_local(punch, refreshed) is True


def test_trust_local_cache_after_sync_false():
    synced = _now() - timedelta(seconds=20)
    refreshed = _now()  # cache refreshed after the punch synced
    punch = {"synced_to_odoo": True, "synced_at": synced}
    assert timeclock._trust_local(punch, refreshed) is False


# ---- _current_state (full decision) ------------------------------------

def test_forgot_to_punch_in_added_in_odoo_shows_clock_out(monkeypatch):
    """No local punch, fresh cache shows them open → clocked in (clock-out)."""
    _set_cache(monkeypatch, {"5": {
        "att_id": 88, "check_in": "2026-06-01T11:00:00+00:00",
        "wc_name": None}}, _now())
    _set_latest_punch(monkeypatch, None)

    st = timeclock._current_state(5)
    assert st["is_clocked_in"] is True
    assert st["open_odoo_attendance_id"] == 88
    assert st["current_wc"] is None  # manual Odoo punch has no WC
    assert st["check_in_ts"] == datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc)


def test_just_clocked_in_unsynced_stays_clocked_in(monkeypatch):
    """Race-guard: fresh kiosk punch not yet in the cache → trust local."""
    _set_cache(monkeypatch, {}, _now())  # cache doesn't show them yet
    _set_latest_punch(monkeypatch, {
        "action": "clock_in", "wc_name": "Bay 3",
        "occurred_at": _now(), "odoo_attendance_id": None,
        "synced_to_odoo": False, "synced_at": None})

    st = timeclock._current_state(5)
    assert st["is_clocked_in"] is True
    assert st["current_wc"] == "Bay 3"


def test_closed_in_odoo_shows_clock_in(monkeypatch):
    """Local says clocked in (synced long ago), fresh cache empty → clock-in."""
    _set_cache(monkeypatch, {}, _now())
    _set_latest_punch(monkeypatch, {
        "action": "clock_in", "wc_name": "Bay 3",
        "occurred_at": _now() - timedelta(hours=4),
        "odoo_attendance_id": 88, "synced_to_odoo": True,
        "synced_at": _now() - timedelta(hours=4)})

    st = timeclock._current_state(5)
    assert st["is_clocked_in"] is False


def test_stale_cache_falls_back_to_local(monkeypatch):
    """Cache older than is_stale threshold → use the local log."""
    _set_cache(monkeypatch, {}, _now() - timedelta(minutes=10))
    _set_latest_punch(monkeypatch, {
        "action": "clock_in", "wc_name": "Bay 7",
        "occurred_at": _now() - timedelta(hours=1),
        "odoo_attendance_id": 88, "synced_to_odoo": True,
        "synced_at": _now() - timedelta(hours=1)})

    st = timeclock._current_state(5)
    assert st["is_clocked_in"] is True       # from the local log, not the cache
    assert st["current_wc"] == "Bay 7"


def test_cold_cache_none_falls_back_to_local(monkeypatch):
    """Warmer never ran (snapshot None) → local log."""
    _set_cache(monkeypatch, None, None)
    _set_latest_punch(monkeypatch, None)
    st = timeclock._current_state(5)
    assert st["is_clocked_in"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_timeclock_state_reconciliation.py -v`
Expected: FAIL — `AttributeError` on `timeclock._trust_local` / `timeclock.live_cache` (live_cache not yet imported in the module).

- [ ] **Step 3: Add the `live_cache` import**

In `src/zira_dashboard/routes/timeclock.py`, change line 56 from:

```python
from .. import db, timeclock_sync, shift_config, staffing
```

to:

```python
from .. import db, timeclock_sync, shift_config, staffing, live_cache
```

- [ ] **Step 4: Replace `_current_state` with the reconciled version**

In `src/zira_dashboard/routes/timeclock.py`, replace the entire current `_current_state` function (lines 166-203) with:

```python
def _latest_punch(person_odoo_id: int) -> dict | None:
    """Most-recent local punch row for this person, or None. Carries the
    sync bookkeeping (synced_to_odoo, synced_at) the reconciliation rule
    needs to decide whether the cache could have seen it yet."""
    rows = db.query(
        "SELECT action, wc_name, "
        "COALESCE(rounded_at, occurred_at) AS occurred_at, "
        "odoo_attendance_id, synced_to_odoo, synced_at "
        "FROM timeclock_punches_log WHERE person_odoo_id = %s "
        "ORDER BY occurred_at DESC, id DESC LIMIT 1",
        (person_odoo_id,),
    )
    return rows[0] if rows else None


def _trust_local(latest: dict | None, refreshed_at) -> bool:
    """True when the local log holds a punch the Odoo cache can't have
    reflected yet — i.e. the latest punch is unsynced, or the cache was
    last refreshed before that punch finished syncing to Odoo. This is the
    race-guard that stops a lagging cache from flashing the wrong screen
    right after a kiosk punch."""
    if latest is None:
        return False
    if not latest.get("synced_to_odoo"):
        return True
    synced_at = latest.get("synced_at")
    if synced_at is None:
        return True
    return refreshed_at <= synced_at


def _state_from_log(latest: dict | None) -> dict:
    """The pre-reconciliation behavior: derive state purely from the most
    recent local punch. Used as the safe fallback (cold/stale cache) and
    whenever the local log wins."""
    if latest is None or latest["action"] in ("clock_out", "transfer_out"):
        return {
            "is_clocked_in": False, "current_wc": None,
            "check_in_ts": None, "open_odoo_attendance_id": None,
        }
    return {
        "is_clocked_in": True,
        "current_wc": latest["wc_name"],
        "check_in_ts": latest["occurred_at"],
        "open_odoo_attendance_id": latest["odoo_attendance_id"],
    }


def _current_state(person_odoo_id: int) -> dict:
    """The kiosk's view of an employee's current attendance state, reconciled
    against Odoo. Still a fast all-local read — no XML-RPC on the hot path.

    Sources: the Odoo open-attendance snapshot (live_cache, refreshed ~30s by
    the warmer) and the latest timeclock_punches_log row. Odoo is authoritative
    EXCEPT for very-recent local punches the snapshot can't have seen yet (see
    _trust_local). If the snapshot is missing or stale, we degrade to the local
    log so an Odoo/warmer outage never blanks everyone to 'clocked out'.

    See docs/superpowers/specs/2026-06-01-timeclock-odoo-state-reconciliation-design.md.
    """
    snapshot, refreshed_at = live_cache.read_open_attendance()
    latest = _latest_punch(person_odoo_id)

    if snapshot is None or live_cache.is_stale(refreshed_at):
        return _state_from_log(latest)
    if _trust_local(latest, refreshed_at):
        return _state_from_log(latest)

    entry = snapshot.get(str(person_odoo_id))
    if not entry:
        return {
            "is_clocked_in": False, "current_wc": None,
            "check_in_ts": None, "open_odoo_attendance_id": None,
        }
    check_in = entry.get("check_in")
    return {
        "is_clocked_in": True,
        "current_wc": entry.get("wc_name"),
        "check_in_ts": datetime.fromisoformat(check_in) if check_in else None,
        "open_odoo_attendance_id": entry.get("att_id"),
    }
```

(`datetime` is already imported at the top of the module: `from datetime import datetime, timezone`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_timeclock_state_reconciliation.py -v`
Expected: 10 passed.
Local Python 3.9: `python -m py_compile src/zira_dashboard/routes/timeclock.py tests/test_timeclock_state_reconciliation.py` → exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/timeclock.py tests/test_timeclock_state_reconciliation.py
git commit -m "feat(timeclock): reconcile punch screen against Odoo open-attendance cache"
```

---

## Task 5: Self-correcting clock-in in the sync path

**Files:**
- Modify: `src/zira_dashboard/timeclock_sync.py` (the `clock_in`/`transfer_in` branch of `_retry_one`, lines 72-75)
- Test: `tests/test_timeclock_sync_dedup.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_timeclock_sync_dedup.py`:

```python
"""Tests for the self-correcting clock-in in timeclock_sync._retry_one.

Stubs odoo_client + db so no Odoo / Postgres is touched. The key behavior:
a clock-in whose employee already has an open Odoo attendance must NOT create
a duplicate — it adopts the open row instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from zira_dashboard import timeclock_sync


@pytest.fixture
def fake_db(monkeypatch):
    captured: dict = {"executes": []}
    monkeypatch.setattr(timeclock_sync.db, "query",
                        lambda sql, params=None: [])
    monkeypatch.setattr(timeclock_sync.db, "execute",
                        lambda sql, params=None: captured["executes"].append(
                            (sql, params)))
    return captured


def _row(action="clock_in"):
    return {"id": 1, "person_odoo_id": 5, "action": action,
            "wc_name": "Bay 3", "occurred_at": datetime(2026, 6, 1, 11, 0,
                                                         tzinfo=timezone.utc)}


def test_clock_in_creates_when_nothing_open(monkeypatch, fake_db):
    monkeypatch.setattr(timeclock_sync.odoo_client, "get_current_attendance",
                        MagicMock(return_value=None))
    create = MagicMock(return_value=88)
    monkeypatch.setattr(timeclock_sync.odoo_client, "clock_in", create)

    timeclock_sync._retry_one(_row())

    create.assert_called_once_with(5, "Bay 3",
                                   datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc))
    # _mark_synced UPDATE carries the new attendance id.
    upd = [e for e in fake_db["executes"] if "synced_to_odoo = TRUE" in e[0]]
    assert upd and upd[0][1][0] == 88


def test_clock_in_adopts_existing_open_no_duplicate(monkeypatch, fake_db):
    monkeypatch.setattr(timeclock_sync.odoo_client, "get_current_attendance",
                        MagicMock(return_value={"id": 99, "check_in": "x"}))
    create = MagicMock()
    monkeypatch.setattr(timeclock_sync.odoo_client, "clock_in", create)
    set_wc = MagicMock()
    monkeypatch.setattr(timeclock_sync.odoo_client, "set_attendance_wc", set_wc)

    timeclock_sync._retry_one(_row())

    create.assert_not_called()                  # no duplicate open attendance
    set_wc.assert_called_once_with(99, "Bay 3")  # label the adopted row
    upd = [e for e in fake_db["executes"] if "synced_to_odoo = TRUE" in e[0]]
    assert upd and upd[0][1][0] == 99            # adopted the existing id


def test_transfer_in_also_self_corrects(monkeypatch, fake_db):
    monkeypatch.setattr(timeclock_sync.odoo_client, "get_current_attendance",
                        MagicMock(return_value={"id": 99}))
    create = MagicMock()
    monkeypatch.setattr(timeclock_sync.odoo_client, "clock_in", create)
    monkeypatch.setattr(timeclock_sync.odoo_client, "set_attendance_wc",
                        MagicMock())

    timeclock_sync._retry_one(_row(action="transfer_in"))
    create.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_timeclock_sync_dedup.py -v`
Expected: FAIL — `test_clock_in_adopts_existing_open_no_duplicate` fails because today's `_retry_one` calls `clock_in` unconditionally (creates a duplicate); `set_attendance_wc` is never called.

- [ ] **Step 3: Make clock-in self-correcting**

In `src/zira_dashboard/timeclock_sync.py`, replace the `clock_in`/`transfer_in` branch of `_retry_one` (lines 72-75):

```python
    if action in ("clock_in", "transfer_in"):
        att_id = odoo_client.clock_in(person_odoo_id, wc_name, ts)
        _mark_synced(r["id"], att_id)
        return
```

with:

```python
    if action in ("clock_in", "transfer_in"):
        # Self-correcting: if Odoo already shows an open attendance for this
        # person (a punch added by hand in Odoo, or a stale-window double-tap),
        # do NOT create a duplicate — adopt the open row so a later clock-out
        # closes the right one, and label its WC if the punch carries one.
        existing = odoo_client.get_current_attendance(person_odoo_id)
        if existing:
            odoo_client.set_attendance_wc(existing["id"], wc_name)
            _mark_synced(r["id"], existing["id"])
            return
        att_id = odoo_client.clock_in(person_odoo_id, wc_name, ts)
        _mark_synced(r["id"], att_id)
        return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_timeclock_sync_dedup.py -v`
Expected: 3 passed.
Local Python 3.9: `python -m py_compile src/zira_dashboard/timeclock_sync.py tests/test_timeclock_sync_dedup.py` → exit 0.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/timeclock_sync.py tests/test_timeclock_sync_dedup.py
git commit -m "feat(timeclock): self-correcting clock-in sync (no duplicate open attendance)"
```

---

## Task 6: Warmer loop + lifespan wiring

**Files:**
- Modify: `src/zira_dashboard/app.py` (add `_warm_odoo_attendance_loop` near the other warmers ~line 95; wire into `lifespan` at lines 268-273 and the cancel tuple at lines 277-287)
- Test: `tests/test_timeclock_sync_dedup.py` (append a tiny coroutine-shape check)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_timeclock_sync_dedup.py`:

```python
def test_odoo_attendance_warmer_is_coroutine():
    import inspect
    from zira_dashboard import app as app_module
    assert inspect.iscoroutinefunction(app_module._warm_odoo_attendance_loop)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_timeclock_sync_dedup.py::test_odoo_attendance_warmer_is_coroutine -v`
Expected: FAIL — `AttributeError: module 'zira_dashboard.app' has no attribute '_warm_odoo_attendance_loop'`.

- [ ] **Step 3: Add the warmer loop**

In `src/zira_dashboard/app.py`, add after `_warm_timeclock_sync_loop` (after line 106):

```python
async def _warm_odoo_attendance_loop():
    """Mirror Odoo's open hr.attendance into odoo_open_attendance_cache every
    ~30s so the timeclock punch screen reflects out-of-band Odoo edits
    (manual add/close/delete/time-edit) without an XML-RPC call on the tap.
    Errors are logged and swallowed so the warmer can't kill itself."""
    from . import live_cache
    while True:
        try:
            await asyncio.to_thread(live_cache.refresh_odoo_open_attendance)
        except Exception as e:  # noqa: BLE001 — never let the warmer die
            _log.warning("Odoo open-attendance warmer failed: %s", e)
        await asyncio.sleep(30)
```

- [ ] **Step 4: Wire it into the lifespan**

In `src/zira_dashboard/app.py`, in `lifespan`, add after the `timeclock_sync_task` line (line 268):

```python
    odoo_attendance_task = asyncio.create_task(_warm_odoo_attendance_loop())
```

Then add `odoo_attendance_task,` to the shutdown cancel tuple (the `for t in (...)` block at lines 277-287) — e.g. immediately after `timeclock_sync_task,`:

```python
            timeclock_sync_task,
            odoo_attendance_task,
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_timeclock_sync_dedup.py::test_odoo_attendance_warmer_is_coroutine -v`
Expected: 1 passed.
Local Python 3.9: `python -m py_compile src/zira_dashboard/app.py` → exit 0. Then manually confirm both edits: `grep -n "odoo_attendance_task" src/zira_dashboard/app.py` shows exactly 2 lines (create_task + cancel tuple).

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/app.py tests/test_timeclock_sync_dedup.py
git commit -m "feat(timeclock): ~30s warmer for Odoo open-attendance cache"
```

---

## Task 7: Time off — hard delete on Odoo deletion

**Files:**
- Modify: `src/zira_dashboard/time_off_sync.py` (rename `_mark_missing_as_cancel` → `_delete_missing_from_odoo` at line 381; update call site at line 296; update module docstring line 18 and `poll_odoo_leaves` docstring lines 241-243)
- Test: `tests/test_time_off_sync.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_time_off_sync.py`:

```python
def test_delete_missing_hard_deletes_and_reverse_cascades(monkeypatch, fake_db):
    """An approved leave gone from Odoo → reverse scheduler_moves still fire,
    balance invalidated, and the row is DELETEd (not soft-cancelled)."""
    fake_db["query_result"] = [{
        "id": 1, "state": "validate", "person_odoo_id": 5, "shape": "full_day",
        "holiday_status_id": 1, "date_from": date(2026, 6, 1),
        "date_to": date(2026, 6, 1), "hour_from": None, "hour_to": None,
        "working_hours_json": None, "odoo_leave_id": 555,
    }]
    invalidated = []
    monkeypatch.setattr(time_off_sync, "_invalidate_balance",
                        lambda pid: invalidated.append(pid))

    time_off_sync._delete_missing_from_odoo(set(), date(2026, 5, 1),
                                            date(2026, 12, 31))

    # Reverse audit row logged (validate → cancel).
    moves = [e for e in fake_db["executes"]
             if "INSERT INTO scheduler_moves" in e[0]]
    assert len(moves) == 1 and moves[0][1][4] == "time_off_canceled"
    # Row hard-deleted, not soft-cancelled.
    deletes = [e for e in fake_db["executes"]
               if "DELETE FROM time_off_requests" in e[0]]
    assert deletes and deletes[0][1] == (1,)
    assert not any("state = 'cancel'" in e[0] for e in fake_db["executes"])
    assert 5 in invalidated


def test_delete_missing_pending_row_deletes_no_scheduler_move(monkeypatch, fake_db):
    """A pending leave gone from Odoo → deleted + balance freed, but no
    scheduler_moves row (it was never approved)."""
    fake_db["query_result"] = [{
        "id": 2, "state": "confirm", "person_odoo_id": 7, "shape": "full_day",
        "holiday_status_id": 1, "date_from": date(2026, 6, 1),
        "date_to": date(2026, 6, 1), "hour_from": None, "hour_to": None,
        "working_hours_json": None, "odoo_leave_id": 777,
    }]
    invalidated = []
    monkeypatch.setattr(time_off_sync, "_invalidate_balance",
                        lambda pid: invalidated.append(pid))

    time_off_sync._delete_missing_from_odoo(set(), date(2026, 5, 1),
                                            date(2026, 12, 31))

    assert not any("INSERT INTO scheduler_moves" in e[0]
                   for e in fake_db["executes"])
    deletes = [e for e in fake_db["executes"]
               if "DELETE FROM time_off_requests" in e[0]]
    assert deletes and deletes[0][1] == (2,)
    assert invalidated == [7]


def test_delete_missing_skips_rows_still_in_odoo(monkeypatch, fake_db):
    """A leave still present in Odoo (its id is in seen_ids) is left alone."""
    fake_db["query_result"] = [{
        "id": 3, "state": "validate", "person_odoo_id": 5, "shape": "full_day",
        "holiday_status_id": 1, "date_from": date(2026, 6, 1),
        "date_to": date(2026, 6, 1), "hour_from": None, "hour_to": None,
        "working_hours_json": None, "odoo_leave_id": 555,
    }]
    monkeypatch.setattr(time_off_sync, "_invalidate_balance", lambda pid: None)

    time_off_sync._delete_missing_from_odoo({555}, date(2026, 5, 1),
                                            date(2026, 12, 31))

    assert not any("DELETE FROM time_off_requests" in e[0]
                   for e in fake_db["executes"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_time_off_sync.py -k delete_missing -v`
Expected: FAIL — `AttributeError: module 'zira_dashboard.time_off_sync' has no attribute '_delete_missing_from_odoo'`.

- [ ] **Step 3: Rename + convert to hard delete**

In `src/zira_dashboard/time_off_sync.py`, replace the whole `_mark_missing_as_cancel` function (lines 381-407) with:

```python
def _delete_missing_from_odoo(
    seen_ids: set[int], start_d: date, end_d: date,
) -> None:
    """Rows in ``[start_d..end_d]`` with an ``odoo_leave_id`` no longer
    returned by Odoo (not in ``seen_ids``) and not already terminal →
    HARD DELETE. Odoo is the source of truth: if the leave is gone there,
    the local mirror row is removed.

    Before deleting we fire ``cascade_on_state_change`` with a synthetic
    ``state='cancel'`` so an approved leave still logs its reverse
    ``scheduler_moves`` audit row (the breadcrumb survives the row's
    deletion) and invalidates the balance. We then invalidate the balance
    unconditionally so a deleted *pending* leave frees its in-flight
    allocation immediately rather than waiting for the 10-min balance sweep.

    Unsynced kiosk drafts (``odoo_leave_id IS NULL``) are never touched —
    the WHERE clause excludes them.
    """
    rows = db.query(
        "SELECT id, state, person_odoo_id, shape, holiday_status_id, "
        "date_from, date_to, hour_from, hour_to, working_hours_json, "
        "odoo_leave_id "
        "FROM time_off_requests "
        "WHERE odoo_leave_id IS NOT NULL "
        "AND state NOT IN ('cancel', 'refuse') "
        "AND date_to >= %s AND date_from <= %s",
        (start_d, end_d),
    )
    for r in rows:
        if r["odoo_leave_id"] in seen_ids:
            continue
        new_r = dict(r)
        new_r["state"] = "cancel"
        cascade_on_state_change(r, new_r)   # reverse audit + balance (if approved)
        _invalidate_balance(r["person_odoo_id"])  # also free pending allocations
        db.execute(
            "DELETE FROM time_off_requests WHERE id = %s",
            (r["id"],),
        )
```

- [ ] **Step 4: Update the call site**

In `src/zira_dashboard/time_off_sync.py`, in `poll_odoo_leaves` (line 296), change:

```python
    _mark_missing_as_cancel(seen_ids, start_d, end_d)
```

to:

```python
    _delete_missing_from_odoo(seen_ids, start_d, end_d)
```

- [ ] **Step 5: Update the stale docstrings**

In `src/zira_dashboard/time_off_sync.py`:

- Module docstring (line 18): change `local rows missing from Odoo are marked ``state='cancel'``.` to `local rows missing from Odoo are HARD DELETED (the reverse cascade still fires first).`
- `poll_odoo_leaves` docstring (lines 241-243): change `Local rows in non-terminal state whose ``odoo_leave_id`` is no longer returned by Odoo are marked ``state='cancel'`` (Odoo-side deletion).` to `Local rows in non-terminal state whose ``odoo_leave_id`` is no longer returned by Odoo are hard-deleted (Odoo-side deletion), after firing the reverse cascade.`

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_time_off_sync.py -v`
Expected: all existing tests still pass + 3 new `delete_missing` tests pass.
Local Python 3.9: `python -m py_compile src/zira_dashboard/time_off_sync.py tests/test_time_off_sync.py` → exit 0. Then `grep -rn "_mark_missing_as_cancel" src/ tests/` → no matches (rename complete).

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/time_off_sync.py tests/test_time_off_sync.py
git commit -m "feat(time-off): hard-delete local row when leave is removed in Odoo"
```

---

## Task 8: Full verification + spec status

**Files:**
- Modify: `docs/superpowers/specs/2026-06-01-timeclock-odoo-state-reconciliation-design.md` (tick Done criteria, update Status)

- [ ] **Step 1: Compile every changed source + test file**

Run:

```bash
python -m py_compile \
  src/zira_dashboard/db.py \
  src/zira_dashboard/odoo_client.py \
  src/zira_dashboard/live_cache.py \
  src/zira_dashboard/routes/timeclock.py \
  src/zira_dashboard/timeclock_sync.py \
  src/zira_dashboard/app.py \
  src/zira_dashboard/time_off_sync.py \
  tests/test_odoo_open_attendance.py \
  tests/test_timeclock_state_reconciliation.py \
  tests/test_timeclock_sync_dedup.py \
  tests/test_time_off_sync.py
```

Expected: exit 0, no output.

- [ ] **Step 2: Import/ast smoke (no DB, no Odoo)**

Run:

```bash
python -c "import ast,glob; [ast.parse(open(f).read(), f) for f in glob.glob('src/zira_dashboard/**/*.py', recursive=True)]; print('ast ok')"
python -c "from zira_dashboard import odoo_client, live_cache, timeclock_sync, time_off_sync; from zira_dashboard.routes import timeclock; print('import ok')"
```

Expected: prints `ast ok` then `import ok`. (If the second line errors on a missing optional dependency in the local 3.9 env, note it — CI/Railway carries the full deps.)

- [ ] **Step 3: Run the full suite where deps exist (CI / dev env)**

Run: `pytest tests/test_odoo_open_attendance.py tests/test_timeclock_state_reconciliation.py tests/test_timeclock_sync_dedup.py tests/test_time_off_sync.py -v`
Expected: all pass. (On local Python 3.9 this is the step that defers to CI per the verification protocol; do not skip writing it down.)

- [ ] **Step 4: Update the spec Done criteria + Status**

In `docs/superpowers/specs/2026-06-01-timeclock-odoo-state-reconciliation-design.md`, change every `- ☐` under "Done criteria" to `- ☑`, and change the `**Status:**` line to `Implemented (pending Railway deploy verification)`.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-06-01-timeclock-odoo-state-reconciliation-design.md
git commit -m "docs(timeclock): mark Odoo reconciliation spec implemented"
```

- [ ] **Step 6: Post-deploy manual verification (on Railway, after push to main)**

Not a code step — record results in the PR/commit notes:
1. In Odoo, manually add an open `hr.attendance` for a test employee with no kiosk punch today. Within ~30s, open that employee on the kiosk → **clock-out** screen shows; tap it → exactly one closed attendance in Odoo (no duplicate).
2. Clock in at the kiosk, then in Odoo close (set check_out) that attendance. Within ~30s the kiosk shows **clock-in** again.
3. In Odoo, delete a future approved leave for a test employee → within ~60s it disappears from the kiosk Who's-Out calendar and My Requests (row gone, not "Cancelled").

---

## Self-Review

**1. Spec coverage:**
- Goal 1 (punch screen reflects Odoo add/close/delete/time-edit, no XML-RPC on tap) → Tasks 1-4, 6. ✅
- Goal 2 (no duplicate open; clock-out closes manual row) → Task 5 (clock-in adopt); clock-out close is the existing `_retry_one` behavior, unchanged. ✅
- Goal 3 (no flicker right after a punch) → Task 4 `_trust_local` race-guard + its tests. ✅
- Goal 4 (leave deleted in Odoo → hard delete, reverse cascade + balance still fire) → Task 7. ✅
- Goal 5 (graceful degradation on outage) → Task 4 stale/None fallback + `test_stale_cache_falls_back_to_local`, `test_cold_cache_none_falls_back_to_local`. ✅
- Cache as single-row JSONB snapshot, ~30s warmer, WC fallback display → Tasks 1, 3, 6, and `_current_state` returning `current_wc=None` for manual rows (tested). ✅

**2. Placeholder scan:** No TBD/TODO/"add error handling"/"write tests for the above". Every code and test step shows complete code and exact commands. ✅

**3. Type/name consistency:**
- `fetch_open_attendances()` returns `{att_id, employee_odoo_id, check_in, wc_name}` (Task 2) — consumed with those exact keys by `refresh_odoo_open_attendance` (Task 3). ✅
- Snapshot entry shape `{att_id, check_in, wc_name}` written in Task 3, read with `entry.get("att_id"/"wc_name"/"check_in")` in Task 4. ✅
- `read_open_attendance() -> (snapshot, refreshed_at)` (Task 3) matches `_current_state` unpacking (Task 4). ✅
- `set_attendance_wc(attendance_id, wc_name)` defined Task 2, called Task 5 as `set_attendance_wc(existing["id"], wc_name)`. ✅
- `_delete_missing_from_odoo(seen_ids, start_d, end_d)` defined + called Task 7; old name fully removed (Step 6 grep). ✅
- `_trust_local(latest, refreshed_at)`, `_latest_punch(person_odoo_id)`, `_state_from_log(latest)` defined and used consistently in Task 4. ✅
