# Machine Breakdown Inbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a machine-breakdown category to the Exception Inbox that auto-detects (or is manually reported) when a recycling machine stops producing, shows one card per broken machine with a row per idle operator, lets the manager Transfer each operator or Snooze the row 15 minutes, and automatically excludes the dead-machine time from every affected operator's productive-time averages (leaderboards and recycling dashboards) without touching historical numbers.

**Architecture:** A new `machine_breakdown.py` module owns detection (pure) and the incident/snooze store (I/O), following the existing `missing_wc.py` one-module-per-inbox-category pattern. Exclusion reuses the existing `wc_time_attributions` table with a new `source='breakdown'`, mirroring the existing `source='testing'` mechanism — but instead of zeroing units (what testing does), it zeroes *expected* minutes via a new `production_daily.excluded_minutes` column consumed by the leaderboard averages and the recycling per-WC expected calc. The inbox card renders as two new row types (`breakdown` header + `breakdown` operator rows) flowing through the existing single per-row template loop — no new template structure needed. Full life-cycle (open → cap on departure → auto-resolve → undo) reuses the existing inbox reconcile/undo machinery unchanged.

**Tech Stack:** FastAPI, Postgres (psycopg2 via the repo's `db` module), Jinja2, vanilla JS (no framework), pytest.

**File structure for this feature:**
- `src/zira_dashboard/machine_breakdown.py` (new) — pure detection math + incident/snooze store I/O + snapshot row shaping. This file is larger than most single-purpose modules in the repo (~350-400 lines) because it plays the same role `missing_wc.py` plays for its category, but this category has more state (incidents + snoozes + detection) — that's expected and matches the plan; don't split it further without checking with the user first.
- `src/zira_dashboard/wc_attributions.py` (modify) — add `BREAKDOWN_SOURCE` + breakdown-specific helpers, alongside the existing `TESTING_SOURCE` helpers.
- `src/zira_dashboard/production_history.py` (modify) — thread excluded minutes through `attribute_for_day`/`attribution_for`.
- `src/zira_dashboard/precompute.py` (modify) — carry `excluded_minutes` into `production_daily`.
- `src/zira_dashboard/routes/leaderboards.py` (modify) — averages subtract excluded minutes.
- `src/zira_dashboard/recycling_data.py` + `src/zira_dashboard/routes/departments.py` (modify) — per-WC expected on the recycling dashboard honors the same exclusion.
- `src/zira_dashboard/routes/exceptions.py` (modify) — new endpoints + undo wiring, following the existing time-off/missing-wc handler pattern.
- `src/zira_dashboard/exception_inbox.py`, `src/zira_dashboard/inbox_keys.py`, `src/zira_dashboard/inbox_reconcile.py` (modify) — wire the new category into the standard six-place inbox pattern.
- `src/zira_dashboard/templates/exceptions.html`, `src/zira_dashboard/static/exceptions.js`, `src/zira_dashboard/static/exceptions.css` (modify) — card rendering + interactions.
- `src/zira_dashboard/app.py` (modify) — register the detection warmer tick.

---

## Task 1: Schema — incident tables + excluded_minutes column

**Files:**
- Modify: `src/zira_dashboard/_schema.py` (append near the end of the `SCHEMA_SQL` string, right after the `page_views` block and before the closing `"""`)
- Test: `tests/test_machine_breakdown_schema.py`

- [ ] **Step 1: Write the failing test**

```python
"""machine_breakdowns / breakdown_snoozes tables + production_daily.excluded_minutes
(Postgres). Mirrors tests/test_inbox_open_items.py's fixture pattern."""
import os
from datetime import datetime, timezone

import pytest

from zira_dashboard import db

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")


@pytest.fixture(autouse=True)
def _clean():
    db.bootstrap_schema()
    db.execute("DELETE FROM breakdown_snoozes WHERE person_name = 'Test Person'")
    db.execute("DELETE FROM machine_breakdowns WHERE wc_name = 'Test WC'")
    db.execute("DELETE FROM production_daily WHERE wc_name = 'Test WC'")
    yield
    db.execute("DELETE FROM breakdown_snoozes WHERE person_name = 'Test Person'")
    db.execute("DELETE FROM machine_breakdowns WHERE wc_name = 'Test WC'")
    db.execute("DELETE FROM production_daily WHERE wc_name = 'Test WC'")


def test_machine_breakdowns_round_trips():
    now = datetime.now(timezone.utc)
    rows = db.query(
        "INSERT INTO machine_breakdowns (wc_name, day, detected_stop_utc, source) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        ("Test WC", now.date(), now, "auto"),
    )
    incident_id = rows[0]["id"]
    fetched = db.query(
        "SELECT wc_name, source, resolved_at, resolution, resume_utc "
        "FROM machine_breakdowns WHERE id = %s",
        (incident_id,),
    )
    assert fetched[0]["wc_name"] == "Test WC"
    assert fetched[0]["source"] == "auto"
    assert fetched[0]["resolved_at"] is None
    assert fetched[0]["resolution"] is None


def test_breakdown_snoozes_round_trips():
    now = datetime.now(timezone.utc)
    rows = db.query(
        "INSERT INTO machine_breakdowns (wc_name, day, detected_stop_utc, source) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        ("Test WC", now.date(), now, "auto"),
    )
    incident_id = rows[0]["id"]
    db.execute(
        "INSERT INTO breakdown_snoozes (breakdown_id, person_name, until_utc) "
        "VALUES (%s, %s, %s)",
        (incident_id, "Test Person", now),
    )
    fetched = db.query(
        "SELECT person_name FROM breakdown_snoozes WHERE breakdown_id = %s",
        (incident_id,),
    )
    assert fetched[0]["person_name"] == "Test Person"


def test_production_daily_has_excluded_minutes_column():
    db.execute(
        "INSERT INTO production_daily (day, emp_id, name, wc_name, units, downtime, "
        "hours, days_worked, excluded_minutes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (day, emp_id, wc_name) DO UPDATE SET excluded_minutes = EXCLUDED.excluded_minutes",
        (datetime.now(timezone.utc).date(), "test-emp", "Test Person", "Test WC",
         10.0, 0.0, 7.0, 1.0, 42.5),
    )
    fetched = db.query(
        "SELECT excluded_minutes FROM production_daily WHERE wc_name = 'Test WC'"
    )
    assert float(fetched[0]["excluded_minutes"]) == 42.5


def test_wc_time_attributions_has_breakdown_id_column():
    db.execute("DELETE FROM wc_time_attributions WHERE wc_name = 'Test WC'")
    db.execute(
        "INSERT INTO wc_time_attributions (day, wc_name, person_name, start_utc, "
        "source, breakdown_id) VALUES (%s, %s, %s, %s, %s, %s)",
        (datetime.now(timezone.utc).date(), "Test WC", "Test Person",
         datetime.now(timezone.utc), "breakdown", 999),
    )
    fetched = db.query(
        "SELECT breakdown_id FROM wc_time_attributions WHERE wc_name = 'Test WC' "
        "AND person_name = 'Test Person'"
    )
    assert fetched[0]["breakdown_id"] == 999
    db.execute("DELETE FROM wc_time_attributions WHERE wc_name = 'Test WC'")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `DATABASE_URL=postgresql://localhost/test .venv/bin/python -m pytest tests/test_machine_breakdown_schema.py -v`
Expected: FAIL — `relation "machine_breakdowns" does not exist` (or similar) for every test.

(If you don't have a `DATABASE_URL` set up for local Postgres, use the repo's embedded-Postgres path: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_machine_breakdown_schema.py -v` — check `tests/conftest.py` for how `DATABASE_URL` gets set for the embedded `pgserver` fixture; the memory note confirms this repo already runs DB-gated tests locally via `pgserver`.)

- [ ] **Step 3: Add the schema**

Open `src/zira_dashboard/_schema.py` and find the `page_views` block (ends with `CREATE INDEX IF NOT EXISTS page_views_day ON page_views (day);` followed by the closing `"""` of `SCHEMA_SQL`). Insert the following immediately before that closing `"""`:

```sql

-- 2026-07-08: machine breakdown incidents (Exception Inbox). One open
-- incident per (wc_name, day) at a time — a card stays open until it's
-- resolved (recovered / handled / dismissed) before a new one for the same
-- machine can open. No FK to keep this denormalized like the rest of the
-- inbox tables (a resolved incident's row must survive independently).
CREATE TABLE IF NOT EXISTS machine_breakdowns (
  id                BIGSERIAL PRIMARY KEY,
  wc_name           TEXT NOT NULL,
  day               DATE NOT NULL,
  detected_stop_utc TIMESTAMPTZ NOT NULL,  -- when output was last seen before the breakdown
  source            TEXT NOT NULL DEFAULT 'auto' CHECK (source IN ('auto', 'manual')),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at       TIMESTAMPTZ,
  resolution        TEXT CHECK (resolution IN ('recovered', 'handled', 'dismissed')),  -- NULL while open
  resume_utc        TIMESTAMPTZ  -- when the machine started producing again; may precede resolved_at if a manager still has to act
);
-- UNIQUE (not just an index): hard dedupe backstop against auto-detect and a
-- manual report racing each other for the same machine/day, mirroring the
-- existing employee_notifications_dedupe pattern in this file.
CREATE UNIQUE INDEX IF NOT EXISTS machine_breakdowns_open_idx
  ON machine_breakdowns (wc_name, day) WHERE resolved_at IS NULL;

-- 2026-07-08: per-operator 15-minute deferral on a breakdown card row.
-- Mirrors late_snoozes.
CREATE TABLE IF NOT EXISTS breakdown_snoozes (
  breakdown_id  BIGINT NOT NULL,
  person_name   TEXT NOT NULL,
  until_utc     TIMESTAMPTZ NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (breakdown_id, person_name)
);

-- 2026-07-08: link wc_time_attributions rows back to the machine_breakdowns
-- incident that created them, so a dismiss ("Not a breakdown") can delete
-- exactly this incident's exclusion rows without touching a different,
-- already-resolved incident on the same machine/day.
ALTER TABLE wc_time_attributions ADD COLUMN IF NOT EXISTS breakdown_id BIGINT;
CREATE INDEX IF NOT EXISTS wc_time_attributions_breakdown_idx
  ON wc_time_attributions (breakdown_id) WHERE breakdown_id IS NOT NULL;

-- 2026-07-08: per-record minutes excluded from a person's expected due to a
-- machine breakdown (source='breakdown' wc_time_attributions windows). Written
-- by precompute alongside units/downtime/hours; read by the leaderboard
-- averages and the recycling per-WC expected calc to shrink the expected
-- denominator without touching units.
ALTER TABLE production_daily ADD COLUMN IF NOT EXISTS excluded_minutes NUMERIC NOT NULL DEFAULT 0;
"""
```

(Note the trailing `"""` you're inserting before is the ORIGINAL closing triple-quote of the `SCHEMA_SQL` string — don't duplicate it, just make sure your new SQL block ends right before it.)

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_machine_breakdown_schema.py -v`
Expected: 4 passed

- [ ] **Step 5: Run the full suite to confirm nothing broke**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass (same count as before plus 4 new)

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/_schema.py tests/test_machine_breakdown_schema.py
git commit -m "feat(breakdown): add machine_breakdowns, breakdown_snoozes tables and excluded_minutes columns"
```

---

## Task 2: wc_attributions breakdown helpers

**Files:**
- Modify: `src/zira_dashboard/wc_attributions.py`
- Test: `tests/test_wc_attributions_breakdown.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Pure-logic + DB tests for the breakdown exclusion extension to
wc_attributions.py. Mirrors tests/test_wc_attributions_testing.py's style."""
from datetime import date, datetime, timezone

from zira_dashboard import wc_attributions


def test_breakdown_source_excluded_from_people_by_wc():
    rows = [
        {"id": 1, "wc_name": "Dismantler 2", "person_name": "Juan",
         "start_utc": None, "end_utc": None, "source": "manual"},
        {"id": 2, "wc_name": "Dismantler 2", "person_name": "Benjamin",
         "start_utc": None, "end_utc": None, "source": wc_attributions.BREAKDOWN_SOURCE},
    ]
    out = wc_attributions.people_by_wc("2026-07-08", rows=rows)
    assert out["Dismantler 2"] == ["Juan"]


def test_breakdown_source_excluded_from_creditable_for_day(monkeypatch):
    rows = [
        {"id": 1, "wc_name": "Dismantler 2", "person_name": "Juan",
         "start_utc": None, "end_utc": None, "source": "manual"},
        {"id": 2, "wc_name": "Dismantler 2", "person_name": "Benjamin",
         "start_utc": None, "end_utc": None, "source": wc_attributions.BREAKDOWN_SOURCE},
    ]
    monkeypatch.setattr(wc_attributions, "for_day", lambda day: rows)
    out = wc_attributions.creditable_for_day("2026-07-08")
    assert [r["person_name"] for r in out] == ["Juan"]


def test_breakdown_windows_for_day_groups_by_person_and_wc():
    s1 = datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc)
    e1 = datetime(2026, 7, 8, 13, 30, tzinfo=timezone.utc)
    rows = [
        {"id": 1, "wc_name": "Dismantler 2", "person_name": "Juan",
         "start_utc": s1, "end_utc": e1, "source": wc_attributions.BREAKDOWN_SOURCE},
        {"id": 2, "wc_name": "Dismantler 2", "person_name": "Juan",
         "start_utc": s1, "end_utc": None, "source": "manual"},
    ]
    out = wc_attributions.breakdown_windows_for_day("2026-07-08", rows=rows)
    assert out == {("Juan", "Dismantler 2"): [(s1, e1)]}


def test_add_breakdown_and_cap_and_reopen(monkeypatch):
    from zira_dashboard import db
    calls = {}

    def fake_query(sql, params):
        calls["insert"] = (sql, params)
        return [{"id": 5}]

    monkeypatch.setattr(db, "query", fake_query)
    day = date(2026, 7, 8)
    start = datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc)
    row_id = wc_attributions.add_breakdown(day, "Dismantler 2", "Juan", start, breakdown_id=42)
    assert row_id == 5
    sql, params = calls["insert"]
    assert "source" in sql.lower()
    assert params == (day, "Dismantler 2", "Juan", start, None, wc_attributions.BREAKDOWN_SOURCE, 42)

    def fake_execute(sql, params):
        calls["cap"] = (sql, params)

    monkeypatch.setattr(db, "execute", fake_execute)
    end = datetime(2026, 7, 8, 13, 30, tzinfo=timezone.utc)
    wc_attributions.cap_breakdown(5, end)
    assert calls["cap"][1] == (end, 5, wc_attributions.BREAKDOWN_SOURCE)

    wc_attributions.reopen_breakdown(5)
    assert calls["cap"][1] == (5, wc_attributions.BREAKDOWN_SOURCE)  # last _execute call was reopen


def test_open_breakdown_row(monkeypatch):
    from zira_dashboard import db
    day = date(2026, 7, 8)
    start = datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(db, "query", lambda sql, params: [{"id": 7, "start_utc": start}])
    row = wc_attributions.open_breakdown_row(day, "Dismantler 2", "Juan")
    assert row == {"id": 7, "start_utc": start}

    monkeypatch.setattr(db, "query", lambda sql, params: [])
    assert wc_attributions.open_breakdown_row(day, "Dismantler 2", "Juan") is None


def test_delete_breakdown_rows_for_incident(monkeypatch):
    from zira_dashboard import db
    calls = {}
    monkeypatch.setattr(db, "execute", lambda sql, params: calls.setdefault("args", params))
    wc_attributions.delete_breakdown_rows_for_incident(42)
    assert calls["args"] == (42, wc_attributions.BREAKDOWN_SOURCE)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_wc_attributions_breakdown.py -v`
Expected: FAIL — `AttributeError: module 'zira_dashboard.wc_attributions' has no attribute 'BREAKDOWN_SOURCE'`

- [ ] **Step 3: Add the constant and extend `add()`**

In `src/zira_dashboard/wc_attributions.py`, add below the existing `TESTING_SOURCE` constant:

```python
BREAKDOWN_SOURCE = "breakdown"
"""``wc_time_attributions.source`` value marking a machine-breakdown exclusion
window for one operator. Like ``TESTING_SOURCE``, these rows are excluded from
crediting (``people_by_wc`` / ``creditable_for_day``) -- they exist only to
carry excluded-minutes math (``breakdown_windows_for_day``), the mirror of how
testing rows carry no-credit unit offsets."""
```

Change the `add()` signature to accept an optional `breakdown_id` (appended last so existing positional callers are unaffected):

```python
def add(day: date, wc_name: str, person_name: str,
        start_utc: datetime, end_utc: datetime | None = None,
        source: str = "manual", breakdown_id: int | None = None) -> int:
    """Insert one attribution row. `end_utc=None` means the assignment is
    OPEN -- it stays running until the person clocks out, transfers, or is
    reassigned (resolved downstream by assignment_windows). `breakdown_id`
    links a source=BREAKDOWN_SOURCE row back to the machine_breakdowns
    incident that created it. Returns row id."""
    from . import db
    rows = db.query(
        "INSERT INTO wc_time_attributions "
        "(day, wc_name, person_name, start_utc, end_utc, source, breakdown_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (day, wc_name, person_name, start_utc, end_utc, source, breakdown_id),
    )
    return rows[0]["id"] if rows else 0
```

- [ ] **Step 4: Exclude breakdown rows from crediting**

Change the `people_by_wc` filter:

```python
    for r in rows:
        if r.get("source") in (TESTING_SOURCE, BREAKDOWN_SOURCE):
            continue
        out.setdefault(r["wc_name"], []).append(r["person_name"])
```

Change the `creditable_for_day` filter:

```python
def creditable_for_day(day: date) -> list[dict]:
    """Attributions for ``day`` EXCLUDING no-credit testing rows and
    breakdown-exclusion rows (``source in (TESTING_SOURCE, BREAKDOWN_SOURCE)``).
    This is the set that should drive both credited operators and dashboard
    GOALS -- neither a testing window nor a breakdown-exclusion window should
    inflate a goal or appear as a credited operator. Mirrors the testing
    filter in ``people_by_wc``."""
    return [r for r in for_day(day)
            if r.get("source") not in (TESTING_SOURCE, BREAKDOWN_SOURCE)]
```

- [ ] **Step 5: Add `breakdown_windows_for_day`**

Add below `testing_windows_for_day`:

```python
def breakdown_windows_for_day(day: date, rows: list[dict] | None = None) -> dict[tuple, list[tuple]]:
    """``{(person_name, wc_name): [(start_utc, end_utc|None), ...]}`` for
    ``source=BREAKDOWN_SOURCE`` rows. Swallows DB errors like people_by_wc;
    same optional ``rows`` param to skip a re-query."""
    if rows is None:
        try:
            rows = for_day(day)
        except Exception:
            return {}
    out: dict[tuple, list[tuple]] = {}
    for r in rows:
        if r.get("source") != BREAKDOWN_SOURCE:
            continue
        key = (r["person_name"], r["wc_name"])
        out.setdefault(key, []).append((r["start_utc"], r.get("end_utc")))
    return out
```

- [ ] **Step 6: Add the breakdown row CRUD helpers**

Add below `delete`:

```python
def add_breakdown(day: date, wc_name: str, person_name: str,
                   start_utc: datetime, breakdown_id: int) -> int:
    """Open a new breakdown exclusion window for one operator. end_utc is
    left NULL (open) until the operator leaves the machine (see
    cap_breakdown)."""
    return add(day, wc_name, person_name, start_utc, end_utc=None,
               source=BREAKDOWN_SOURCE, breakdown_id=breakdown_id)


def cap_breakdown(attribution_id: int, end_utc: datetime) -> None:
    """Close an open breakdown row at the operator's departure/incident-
    resolution time. No-op if already closed (idempotent against a
    detection tick re-processing the same incident)."""
    from . import db
    db.execute(
        "UPDATE wc_time_attributions SET end_utc = %s "
        "WHERE id = %s AND source = %s",
        (end_utc, attribution_id, BREAKDOWN_SOURCE),
    )


def reopen_breakdown(attribution_id: int) -> None:
    """Undo a cap: clear end_utc so the window is open again (breakdown
    transfer-undo)."""
    from . import db
    db.execute(
        "UPDATE wc_time_attributions SET end_utc = NULL "
        "WHERE id = %s AND source = %s",
        (attribution_id, BREAKDOWN_SOURCE),
    )


def open_breakdown_row(day: date, wc_name: str, person_name: str) -> dict | None:
    """The operator's currently-OPEN breakdown row for (day, wc_name), if
    any. Returns {id, start_utc} or None. Used by the detection tick to find
    the row to cap when an operator leaves the machine."""
    from . import db
    rows = db.query(
        "SELECT id, start_utc FROM wc_time_attributions "
        "WHERE day = %s AND wc_name = %s AND person_name = %s "
        "AND source = %s AND end_utc IS NULL",
        (day, wc_name, person_name, BREAKDOWN_SOURCE),
    )
    return rows[0] if rows else None


def delete_breakdown_rows_for_incident(breakdown_id: int) -> None:
    """Delete every wc_time_attributions row tied to one breakdown incident
    -- the "Not a breakdown" dismiss, which restores normal averages by
    removing the exclusion entirely."""
    from . import db
    db.execute(
        "DELETE FROM wc_time_attributions WHERE breakdown_id = %s AND source = %s",
        (breakdown_id, BREAKDOWN_SOURCE),
    )
```

- [ ] **Step 7: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_wc_attributions_breakdown.py -v`
Expected: 6 passed

- [ ] **Step 8: Run the full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass — specifically re-run `tests/test_wc_attributions.py` and `tests/test_wc_attributions_testing.py` to confirm `add()`'s new trailing param didn't break any existing positional call.

- [ ] **Step 9: Commit**

```bash
git add src/zira_dashboard/wc_attributions.py tests/test_wc_attributions_breakdown.py
git commit -m "feat(breakdown): add source='breakdown' exclusion helpers to wc_attributions"
```

---

## Task 3: Pure exclusion-math helpers in a new `machine_breakdown.py`

**Files:**
- Create: `src/zira_dashboard/machine_breakdown.py`
- Test: `tests/test_machine_breakdown_math.py`

This task creates the module and its two pure math helpers only. Detection (Task 7) and the incident store (Task 8) extend this same file later.

- [ ] **Step 1: Write the failing tests**

```python
"""Pure-logic tests for machine_breakdown.py's exclusion-math helpers. No DB."""
from datetime import date, datetime, timezone

from zira_dashboard import machine_breakdown


def _pm(day, start, end):
    """Fake productive_minutes_in_window: 1 minute per elapsed minute, no breaks."""
    return (end - start).total_seconds() / 60.0


def test_excluded_minutes_for_windows_sums_closed_windows():
    day = date(2026, 7, 8)
    windows = [
        (datetime(2026, 7, 8, 13, 0, tzinfo=timezone.utc), datetime(2026, 7, 8, 13, 30, tzinfo=timezone.utc)),
        (datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc), datetime(2026, 7, 8, 14, 10, tzinfo=timezone.utc)),
    ]
    assert machine_breakdown.excluded_minutes_for_windows(windows, day, _pm) == 40.0


def test_excluded_minutes_for_windows_skips_open_and_zero_span():
    day = date(2026, 7, 8)
    s = datetime(2026, 7, 8, 13, 0, tzinfo=timezone.utc)
    windows = [(s, None), (s, s)]
    assert machine_breakdown.excluded_minutes_for_windows(windows, day, _pm) == 0.0


def test_excluded_minutes_overlapping_clips_to_segment_and_caps_open_at_now():
    day = date(2026, 7, 8)
    seg_start = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    seg_end = datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)
    now = datetime(2026, 7, 8, 13, 45, tzinfo=timezone.utc)
    # Breakdown window opens at 13:00, still open (None) -- caps at `now` (13:45),
    # clipped to the segment [12:00, 14:00) -- overlap is [13:00, 13:45) = 45 min.
    windows = [(datetime(2026, 7, 8, 13, 0, tzinfo=timezone.utc), None)]
    minutes = machine_breakdown.excluded_minutes_overlapping(
        windows, seg_start, seg_end, now, day, _pm
    )
    assert minutes == 45.0


def test_excluded_minutes_overlapping_no_overlap_returns_zero():
    day = date(2026, 7, 8)
    seg_start = datetime(2026, 7, 8, 6, 0, tzinfo=timezone.utc)
    seg_end = datetime(2026, 7, 8, 7, 0, tzinfo=timezone.utc)
    now = datetime(2026, 7, 8, 13, 45, tzinfo=timezone.utc)
    windows = [(datetime(2026, 7, 8, 13, 0, tzinfo=timezone.utc), None)]
    minutes = machine_breakdown.excluded_minutes_overlapping(
        windows, seg_start, seg_end, now, day, _pm
    )
    assert minutes == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_machine_breakdown_math.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'zira_dashboard.machine_breakdown'`

- [ ] **Step 3: Create the module with the two pure helpers**

```python
"""Machine breakdown detection and exclusion math for the Exception Inbox.

Mirrors missing_wc.py's role for its category, but with more state: a
breakdown incident persists (machine_breakdowns), tracks per-operator
snoozes (breakdown_snoozes), and drives a per-operator time exclusion
(wc_time_attributions source='breakdown') that mirrors the existing
source='testing' mechanism -- except testing zeroes UNITS (credited to no
one) while a breakdown zeroes EXPECTED minutes (units earned before the
breakdown are kept).
"""

from __future__ import annotations

from datetime import date, datetime


def excluded_minutes_for_windows(
    windows: list[tuple[datetime, datetime | None]],
    day: date,
    productive_minutes_in_window,
) -> float:
    """Pure. Sum of productive_minutes_in_window(day, start, end) over each
    CLOSED [start, end) window (end is not None and end > start); open or
    zero/negative-span windows are skipped. `productive_minutes_in_window`
    is injected (matches shift_config.productive_minutes_in_window's
    signature) so this is testable without shift config or timezones,
    mirroring routes/leaderboards.py's averages_for_wc DI style."""
    total = 0.0
    for start, end in windows:
        if end is None or end <= start:
            continue
        total += productive_minutes_in_window(day, start, end)
    return total


def excluded_minutes_overlapping(
    windows: list[tuple[datetime, datetime | None]],
    start_utc: datetime,
    end_utc: datetime,
    now_utc: datetime,
    day: date,
    productive_minutes_in_window,
) -> float:
    """Pure. Sum of productive_minutes_in_window(day, lo, hi) for the overlap
    of each breakdown window (open windows capped at now_utc) with
    [start_utc, end_utc). Used to shrink one work segment's productive
    minutes (recycling per-WC expected) to honor a breakdown exclusion,
    without needing a whole-day total."""
    clipped: list[tuple[datetime, datetime]] = []
    for w_start, w_end in windows:
        w_end = w_end if w_end is not None else now_utc
        lo = max(w_start, start_utc)
        hi = min(w_end, end_utc)
        if hi > lo:
            clipped.append((lo, hi))
    return excluded_minutes_for_windows(clipped, day, productive_minutes_in_window)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_machine_breakdown_math.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/machine_breakdown.py tests/test_machine_breakdown_math.py
git commit -m "feat(breakdown): pure exclusion-minutes math in new machine_breakdown module"
```

---

## Task 4: Thread excluded_minutes through attribution and precompute

**Files:**
- Modify: `src/zira_dashboard/production_history.py`
- Modify: `src/zira_dashboard/precompute.py`
- Test: `tests/test_production_history_breakdown.py`
- Test: `tests/test_precompute_breakdown.py`

- [ ] **Step 1: Write the failing test for `attribute_for_day`**

```python
"""Pure-logic tests for the excluded_minutes extension to attribute_for_day."""
from zira_dashboard.production_history import attribute_for_day


def test_attribute_for_day_carries_excluded_minutes():
    assignments = {"Dismantler 2": ["Juan", "Benjamin"]}
    wc_totals = {"Dismantler 2": (100, 20)}
    excluded = {"Juan": {"Dismantler 2": 30.0}}
    out = attribute_for_day(assignments, wc_totals, 480, excluded_minutes=excluded)
    assert out["Juan"]["Dismantler 2"]["excluded_minutes"] == 30.0
    assert out["Benjamin"]["Dismantler 2"]["excluded_minutes"] == 0.0
    # Units/downtime unaffected -- breakdown never touches units.
    assert out["Juan"]["Dismantler 2"]["units"] == 50.0


def test_attribute_for_day_no_excluded_minutes_argument_defaults_zero():
    assignments = {"Forklift": ["Lauro"]}
    wc_totals = {"Forklift": (8, 0)}
    out = attribute_for_day(assignments, wc_totals, 480)
    assert out["Lauro"]["Forklift"]["excluded_minutes"] == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_production_history_breakdown.py -v`
Expected: FAIL — `KeyError: 'excluded_minutes'`

- [ ] **Step 3: Extend `attribute_for_day`**

In `src/zira_dashboard/production_history.py`, change the signature and body:

```python
def attribute_for_day(
    assignments: dict[str, list[str]],
    wc_totals: dict[str, tuple[int, int]],
    elapsed_minutes: int,
    extra_assignments: dict[str, list[str]] | None = None,
    excluded_minutes: dict[str, dict[str, float]] | None = None,
) -> dict[str, dict[str, dict[str, float]]]:
    """Attribute one day's WC output to the operators on each WC.

    Args:
        assignments: {wc_name: [person_name, ...]} -- from the schedule's
            assignments dict, with the time-off pseudo-key already stripped.
        wc_totals: {wc_name: (units, downtime_minutes)} -- from a Zira
            leaderboard call. Missing entries (WC with no meter) are
            treated as zero output.
        elapsed_minutes: shift minutes available that day; same for everyone.
        extra_assignments: optional ``{wc_name: [person, ...]}`` for retro
            time-window attributions. Adds operators to UNSCHEDULED WCs only
            (a WC already present in ``assignments`` with people is left
            alone -- the published schedule wins). Used to flow retro
            attributions into leaderboards and dashboards.
        excluded_minutes: optional ``{person: {wc_name: minutes}}`` of
            machine-breakdown-excluded minutes (the mirror of testing's unit
            offset -- this zeroes EXPECTED minutes, not units). Missing
            entries default to 0.0.

    Returns:
        {person: {wc_name: {"units": float, "downtime": float, "hours": float,
                            "days_worked": int, "excluded_minutes": float}}}
    """
    from .staffing import TIME_OFF_KEY  # local import avoids circular at module load

    out: dict[str, dict[str, dict[str, float]]] = {}
    hours = elapsed_minutes / 60.0
    excluded_minutes = excluded_minutes or {}

    # Merge: scheduled wins; extras only fire when a WC has no scheduled people.
    merged: dict[str, list[str]] = {}
    for wc_name, operators in assignments.items():
        if wc_name == TIME_OFF_KEY or not operators:
            continue
        merged[wc_name] = list(operators)
    if extra_assignments:
        for wc_name, ppl in extra_assignments.items():
            if wc_name in merged:  # scheduled — skip
                continue
            if not ppl:
                continue
            merged[wc_name] = list(ppl)

    for wc_name, operators in merged.items():
        units, downtime = wc_totals.get(wc_name, (0, 0))
        n = len(operators)
        per_units = units / n
        per_downtime = downtime / n
        for person in operators:
            wc_map = out.setdefault(person, {})
            wc_map[wc_name] = {
                "units": per_units,
                "downtime": per_downtime,
                "hours": hours,
                "days_worked": 1,
                "excluded_minutes": excluded_minutes.get(person, {}).get(wc_name, 0.0),
            }
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_production_history_breakdown.py -v`
Expected: 2 passed. Also re-run `tests/test_wc_attributions.py` (it calls `attribute_for_day` directly) to confirm backward compatibility.

- [ ] **Step 5: Write the failing test for `attribution_for`'s excluded-minutes wiring**

```python
"""Tests for attribution_for's breakdown-exclusion wiring (excluded_minutes)."""
from datetime import date, datetime, timezone

from zira_dashboard import production_history


def test_excluded_minutes_by_person_wc_sums_closed_and_caps_open(monkeypatch):
    from zira_dashboard import wc_attributions
    day = date(2026, 7, 8)
    # Times are picked to fall strictly before the plant's default 9:00-9:15
    # America/Chicago "Morning break" (13:00/13:30 UTC = 8:00/8:30 CDT;
    # 12:30/12:50 UTC = 7:30/7:50 CDT) so `productive_minutes_in_window`'s
    # real break-subtraction logic doesn't eat into either window -- this
    # test is about the closed/open-window handling, not break math.
    s = datetime(2026, 7, 8, 13, 0, tzinfo=timezone.utc)
    e = datetime(2026, 7, 8, 13, 30, tzinfo=timezone.utc)
    open_s = datetime(2026, 7, 8, 12, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(
        wc_attributions, "breakdown_windows_for_day",
        lambda d: {("Juan", "Dismantler 2"): [(s, e)], ("Benjamin", "Dismantler 2"): [(open_s, None)]},
    )
    now = datetime(2026, 7, 8, 12, 50, tzinfo=timezone.utc)
    out = production_history._excluded_minutes_by_person_wc(day, now)
    assert out["Juan"]["Dismantler 2"] == 30.0
    assert out["Benjamin"]["Dismantler 2"] == 20.0  # open window capped at `now`


def test_effective_now_clamps_to_shift_end(monkeypatch):
    from zira_dashboard import shift_config
    day = date(2026, 7, 8)
    monkeypatch.setattr(shift_config, "shift_end_for", lambda d: __import__("datetime").time(15, 30))
    late_now = datetime(2026, 7, 9, 2, 0, tzinfo=timezone.utc)  # well past shift end
    effective = production_history._effective_now(day, late_now)
    assert effective < late_now
```

- [ ] **Step 6: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_production_history_breakdown.py -v`
Expected: FAIL — `AttributeError: module 'zira_dashboard.production_history' has no attribute '_excluded_minutes_by_person_wc'`

- [ ] **Step 7: Add `_effective_now`, `_excluded_minutes_by_person_wc`, and wire into `attribution_for`**

In `src/zira_dashboard/production_history.py`, first check the module-level import line near the top of the file — it currently reads `from datetime import date, UTC` (no `datetime` class). Change it to `from datetime import date, datetime, UTC` so the two new functions' signature annotations below resolve under `ruff`'s static analysis (pyflakes F821 — `from __future__ import annotations` defers evaluation at runtime, but ruff still statically checks that annotated names are resolvable, and a name only imported inside a function body doesn't count for that function's own `def` line). With `datetime` now at module scope, add these two helpers near `_apply_testing_offsets` (no per-function local `datetime` import needed):

```python
def _effective_now(day: date, now: datetime) -> datetime:
    """`now`, clamped to `day`'s shift end. Used to cap an OPEN breakdown
    exclusion window for a past day (or a today read taken after hours) so
    excluded-minutes math never runs past the shift that actually happened."""
    from .shift_config import shift_end_for, SITE_TZ
    shift_end_utc = datetime.combine(day, shift_end_for(day), tzinfo=SITE_TZ).astimezone(UTC)
    return min(now, shift_end_utc)


def _excluded_minutes_by_person_wc(day: date, now: datetime) -> dict[str, dict[str, float]]:
    """{person: {wc_name: minutes}} of machine-breakdown-excluded minutes for
    `day`. Open breakdown windows are capped at `now` (already clamped to
    shift end by the caller) so a live in-progress breakdown is reflected
    immediately, matching the design's "today's live averages are correct
    during the outage" requirement."""
    from . import wc_attributions, machine_breakdown
    from .shift_config import productive_minutes_in_window
    windows_by_key = wc_attributions.breakdown_windows_for_day(day)
    out: dict[str, dict[str, float]] = {}
    for (person, wc), windows in windows_by_key.items():
        closed = [(s, e if e is not None else now) for (s, e) in windows]
        minutes = machine_breakdown.excluded_minutes_for_windows(
            closed, day, productive_minutes_in_window
        )
        if minutes > 0:
            out.setdefault(person, {})[wc] = minutes
    return out
```

Then wire it into `attribution_for`:

```python
def attribution_for(d: date, client) -> dict[str, dict[str, dict[str, float]]]:
    """Attribute production on a single day.
    ...
    """
    from datetime import datetime, UTC
    from . import staffing, wc_attributions
    sched = staffing.load_schedule(d)
    today = datetime.now(UTC).date()
    if d >= today and not sched.published:
        return {}
    wc_totals = _fetch_wc_totals(client, d)
    elapsed = _elapsed_minutes_for(d)
    extra = wc_attributions.people_by_wc(d)
    testing = wc_attributions.testing_windows_for_day(d)
    if testing:
        samples_by_wc = _fetch_wc_samples(client, d)
        wc_totals = _apply_testing_offsets(wc_totals, samples_by_wc, testing)
    try:
        excluded = _excluded_minutes_by_person_wc(d, _effective_now(d, datetime.now(UTC)))
    except Exception:
        excluded = {}
    return attribute_for_day(
        sched.assignments, wc_totals, elapsed,
        extra_assignments=extra, excluded_minutes=excluded,
    )
```

(Leave the rest of the docstring as-is; just the body and signature call change shown above.) The `try/except` matches how `wc_attributions.people_by_wc`/`testing_windows_for_day` (called just above) already swallow their own DB errors internally — without it, a transient DB hiccup on the breakdown lookup would take down the WHOLE attribution call (units/downtime/hours too), not just degrade the one auxiliary signal. It's also required for the "Step 4/8" full-suite runs to pass without a live Postgres: `_excluded_minutes_by_person_wc` reaches `shift_config.productive_minutes_in_window` → `breaks_for` → `staffing.load_schedule` → DB, an unguarded dependency this function's neighbors don't have.

- [ ] **Step 8: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_production_history_breakdown.py -v`
Expected: 4 passed

- [ ] **Step 9: Write the failing test for precompute wiring**

```python
"""Tests for excluded_minutes flowing through flatten_attribution and
upsert_production_daily."""
from datetime import date

from zira_dashboard.precompute import flatten_attribution


def test_flatten_attribution_carries_excluded_minutes():
    day = date(2026, 7, 8)
    attribution = {
        "Juan": {"Dismantler 2": {"units": 50.0, "downtime": 10.0, "hours": 8.0,
                                   "days_worked": 1, "excluded_minutes": 30.0}},
    }
    rows = flatten_attribution(day, attribution, {"Juan": "emp-1"})
    assert rows[0]["excluded_minutes"] == 30.0


def test_flatten_attribution_defaults_excluded_minutes_when_absent():
    day = date(2026, 7, 8)
    attribution = {
        "Juan": {"Dismantler 2": {"units": 50.0, "downtime": 10.0, "hours": 8.0,
                                   "days_worked": 1}},
    }
    rows = flatten_attribution(day, attribution, {"Juan": "emp-1"})
    assert rows[0]["excluded_minutes"] == 0.0
```

- [ ] **Step 10: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_precompute_breakdown.py -v`
Expected: FAIL — `KeyError: 'excluded_minutes'`

- [ ] **Step 11: Wire `excluded_minutes` through `flatten_attribution` and `upsert_production_daily`**

In `src/zira_dashboard/precompute.py`:

```python
def flatten_attribution(
    day: date,
    attribution: dict[str, dict[str, dict[str, float]]],
    name_to_emp_id: dict[str, str],
) -> list[dict]:
    """Turn {person: {wc: {units, downtime, hours, days_worked,
    excluded_minutes}}} into a flat list of rows ready for UPSERT into
    production_daily.

    Rows where units == 0 are dropped (the attribution dict can carry
    zero-unit rows for multi-person WCs with no production; they add
    no value in the table).

    Operators not found in the name->id map fall back to using their name
    as the row key (the column is TEXT and every production_daily read is
    by name), so a production row is never silently dropped.
    """
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
                "excluded_minutes": float(totals.get("excluded_minutes") or 0),
            })
    return rows
```

```python
def upsert_production_daily(rows: Iterable[dict]) -> int:
    """UPSERT a batch of rows into production_daily. Returns count written.

    Idempotent: PK conflict triggers an UPDATE of every non-PK column
    and bumps `computed_at`. Re-running the same day overwrites cleanly.
    """
    from . import db
    rows = list(rows)
    if not rows:
        return 0
    sql = """
        INSERT INTO production_daily (
            day, emp_id, name, wc_name,
            units, downtime, hours, days_worked, excluded_minutes, computed_at
        ) VALUES %s
        ON CONFLICT (day, emp_id, wc_name) DO UPDATE SET
            name             = EXCLUDED.name,
            units            = EXCLUDED.units,
            downtime         = EXCLUDED.downtime,
            hours            = EXCLUDED.hours,
            days_worked      = EXCLUDED.days_worked,
            excluded_minutes = EXCLUDED.excluded_minutes,
            computed_at      = now()
    """
    with db.cursor() as cur:
        # execute_values folds every row into one statement — a single
        # round-trip instead of executemany's one per row (this runs
        # every 45s from the live warmer).
        db.execute_values(cur, sql, [
            (r["day"], r["emp_id"], r["name"], r["wc_name"],
             r["units"], r["downtime"], r["hours"], r["days_worked"],
             r["excluded_minutes"])
            for r in rows
        ], template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, now())")
    return len(rows)
```

Also update `daily_records_in_range` (same file) to select the new column, since this is what feeds the leaderboard `records` (Task 5 needs it):

```python
def daily_records_in_range(start: date, end: date) -> list[dict]:
    """One row per (day, person, wc) in [start, end], matching the shape
    of the existing `production_history.daily_records` so awards/trophy
    code can swap over with no behavior change.

    Each row: {day, person, wc, units, downtime, hours, excluded_minutes}.
    """
    from . import db
    rows = db.query(
        """
        SELECT day, name AS person, wc_name AS wc,
               units, downtime, hours, excluded_minutes
        FROM production_daily
        WHERE day BETWEEN %s AND %s AND units > 0
          AND NOT EXISTS (
            SELECT 1 FROM manual_absences ma
            WHERE ma.day = production_daily.day
              AND ma.name = production_daily.name
          )
        """,
        (start, end),
    )
    return [
        {
            "day": r["day"],
            "person": r["person"],
            "wc": r["wc"],
            "units": float(r["units"]),
            "downtime": float(r["downtime"]),
            "hours": float(r["hours"]),
            "excluded_minutes": float(r["excluded_minutes"]),
        }
        for r in rows
    ]
```

- [ ] **Step 12: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_precompute_breakdown.py -v`
Expected: 2 passed

Also add one more assertion test for `daily_records_in_range`'s new field (append to the same test file):

```python
def test_daily_records_in_range_returns_excluded_minutes(monkeypatch):
    from datetime import date
    from zira_dashboard import db, precompute
    monkeypatch.setattr(db, "query", lambda sql, params: [
        {"day": date(2026, 7, 8), "person": "Juan", "wc": "Dismantler 2",
         "units": 50.0, "downtime": 10.0, "hours": 8.0, "excluded_minutes": 30.0},
    ])
    rows = precompute.daily_records_in_range(date(2026, 7, 8), date(2026, 7, 8))
    assert rows[0]["excluded_minutes"] == 30.0
```

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_precompute_breakdown.py -v`
Expected: 3 passed

- [ ] **Step 13: Run the full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass. Pay particular attention to `tests/test_precompute.py` and any test asserting the exact `upsert_production_daily` SQL string or column count.

- [ ] **Step 14: Commit**

```bash
git add src/zira_dashboard/production_history.py src/zira_dashboard/precompute.py \
        tests/test_production_history_breakdown.py tests/test_precompute_breakdown.py
git commit -m "feat(breakdown): thread excluded_minutes through attribution and precompute"
```

---

## Task 5: Leaderboard averages subtract excluded_minutes

**Files:**
- Modify: `src/zira_dashboard/routes/leaderboards.py`
- Test: `tests/test_leaderboards_avg.py` (extend existing file)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_leaderboards_avg.py`:

```python
def _rec_excl(d, person, wc, units, excluded_minutes=0.0):
    return {"day": d, "person": person, "wc": wc, "units": units,
            "downtime": 0.0, "hours": 7.0, "excluded_minutes": excluded_minutes}


def test_averages_for_wc_shrinks_expected_by_excluded_minutes():
    # Expected without exclusion: 7h * 30/h = 210. With 60 excluded minutes,
    # productive hours drop to 6h -> expected 180. units 180 -> pct == 1.0.
    records = [_rec_excl(date(2026, 4, 27), "Alice", "WC1", 180, excluded_minutes=60.0)]
    rows = averages_for_wc(records, 30.0, _const_productive, "pct")
    assert abs(rows[0]["avg_pct"] - 1.0) < 1e-9


def test_averages_for_wc_zero_exclusion_matches_pre_existing_behavior():
    """Regression guard: with excluded_minutes == 0 for every record, the
    result is bit-for-bit identical to before this feature existed."""
    records = [
        _rec_excl(date(2026, 4, 27), "Alice", "WC1", 200, excluded_minutes=0.0),
        _rec_excl(date(2026, 4, 28), "Alice", "WC1", 220, excluded_minutes=0.0),
    ]
    rows = averages_for_wc(records, 30.0, _const_productive, "pct")
    assert rows[0]["avg_units"] == 210.0
    assert abs(rows[0]["avg_pct"] - (200/210 + 220/210) / 2) < 1e-9


def test_averages_for_wc_missing_excluded_minutes_key_defaults_zero():
    """Records without an excluded_minutes key (e.g. old cached data) behave
    exactly like excluded_minutes=0 -- .get() with a default, never a KeyError."""
    records = [_rec(date(2026, 4, 27), "Alice", "WC1", 200)]  # no excluded_minutes key
    rows = averages_for_wc(records, 30.0, _const_productive, "pct")
    assert abs(rows[0]["avg_pct"] - 200 / 210) < 1e-9
```

Also append the group-averages equivalent:

```python
from zira_dashboard.routes.leaderboards import averages_for_group


def test_averages_for_group_shrinks_expected_by_excluded_minutes():
    records = [_rec_excl(date(2026, 4, 27), "Alice", "WC1", 180, excluded_minutes=60.0)]
    rows = averages_for_group(records, {"WC1": 30.0}, _const_productive, "pct")
    assert abs(rows[0]["avg_pct"] - 1.0) < 1e-9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_leaderboards_avg.py -v`
Expected: FAIL — `test_averages_for_wc_shrinks_expected_by_excluded_minutes` and the group equivalent fail their `avg_pct` assertion (expected is still computed off the full 7h, not shrunk).

- [ ] **Step 3: Subtract excluded_minutes from the expected denominator**

In `src/zira_dashboard/routes/leaderboards.py`, change `averages_for_wc`'s pct loop:

```python
        # Days without a configured goal contribute no pct sample; a person
        # with no goal-days at all gets avg_pct=None (renders "—", not "0%").
        pct_per_day: list[float] = []
        for r in recs:
            prod_min = productive_minutes_for(r["day"]) - r.get("excluded_minutes", 0.0)
            expected = target_per_hour * max(0.0, prod_min) / 60.0
            if expected > 0:
                pct_per_day.append(r["units"] / expected)
        avg_pct = sum(pct_per_day) / len(pct_per_day) if pct_per_day else None
```

And `averages_for_group`'s pct loop:

```python
        # Same None-means-no-goal convention as averages_for_wc.
        pct_per_day: list[float] = []
        wc_counts: dict[str, int] = {}
        for r in recs:
            wc_counts[r["wc"]] = wc_counts.get(r["wc"], 0) + 1
            prod_min = productive_minutes_for(r["day"]) - r.get("excluded_minutes", 0.0)
            target = target_per_hour_by_wc.get(r["wc"], 0.0)
            expected = target * max(0.0, prod_min) / 60.0
            if expected > 0:
                pct_per_day.append(r["units"] / expected)
        avg_pct = sum(pct_per_day) / len(pct_per_day) if pct_per_day else None
```

Update both docstrings' `records` shape line to mention the new key:

```python
    `records` is a list of dicts with keys: day, person, wc, units,
    downtime, hours, excluded_minutes -- same shape as
    production_history.daily_records().
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_leaderboards_avg.py -v`
Expected: all pass (existing tests + 4 new)

- [ ] **Step 5: Run the full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/leaderboards.py tests/test_leaderboards_avg.py
git commit -m "feat(breakdown): leaderboard averages subtract breakdown-excluded minutes from expected"
```

---

## Task 6: Recycling per-WC expected honors breakdowns

**Files:**
- Modify: `src/zira_dashboard/assignment_windows.py`
- Modify: `src/zira_dashboard/recycling_data.py`
- Modify: `src/zira_dashboard/routes/departments.py`
- Test: `tests/test_assignment_windows_breakdown.py`

`expected_by_wc`'s `productive_minutes` callable currently receives `(person_name, start, end)` — no `wc_name` — but a breakdown exclusion is scoped per (person, wc), so the callable needs `wc_name` too. This is a signature change; confirmed via grep there is exactly ONE call site for `expected_by_wc` (inside `compute_per_wc_expected`) and exactly ONE call site for `compute_per_wc_expected` (in `routes/departments.py`), so this is safe to change directly.

- [ ] **Step 1: Write the failing test**

```python
"""expected_by_wc's productive_minutes callable now receives wc_name too."""
from datetime import datetime, timezone

from zira_dashboard.assignment_windows import WorkSegment, expected_by_wc


def test_expected_by_wc_passes_wc_name_to_productive_minutes():
    seg = WorkSegment("Dismantler 2", "Juan",
                       datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc),
                       datetime(2026, 7, 8, 13, 0, tzinfo=timezone.utc), "punch")
    calls = []

    def productive_minutes(person, wc_name, start, end):
        calls.append((person, wc_name, start, end))
        return 60.0

    out = expected_by_wc([seg], {"Dismantler 2": 30.0}, productive_minutes)
    assert calls == [("Juan", "Dismantler 2", seg.start_utc, seg.end_utc)]
    assert out["Dismantler 2"] == 30.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_assignment_windows_breakdown.py -v`
Expected: FAIL — `TypeError: productive_minutes() missing 1 required positional argument` (the old call site passes only 3 args)

- [ ] **Step 3: Update `expected_by_wc`'s call to `productive_minutes`**

In `src/zira_dashboard/assignment_windows.py`:

```python
def expected_by_wc(
    segments: list[WorkSegment],
    target_per_hour: dict[str, float],
    productive_minutes: Callable[[str, str, datetime, datetime], float],
) -> dict[str, float]:
    """Sum prorated expected pallets per WC.

    `productive_minutes(person, wc_name, start, end)` returns the working
    minutes in the window. Since the June 2026 pace-goal fix the route passes
    a closure over shift_config.productive_minutes_in_window (with the `day`
    bound), which subtracts breaks only -- deliberately NOT
    effective_minutes_worked, since netting out partial time-off would
    wrongly shrink the pace goal on partial-leave days. The July 2026
    breakdown feature added `wc_name` to this signature so the closure can
    also subtract a machine-breakdown exclusion window scoped to this WC."""
    out: dict[str, float] = {}
    for s in segments:
        thr = target_per_hour.get(s.wc_name, 0.0)
        if thr <= 0:
            continue
        mins = productive_minutes(s.person_name, s.wc_name, s.start_utc, s.end_utc)
        if mins <= 0:
            continue
        out[s.wc_name] = out.get(s.wc_name, 0.0) + thr * mins / 60.0
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_assignment_windows_breakdown.py -v`
Expected: 1 passed

- [ ] **Step 5: Update the only call site in `routes/departments.py`**

Find the existing call (around where `compute_per_wc_expected` is invoked, near the `per_wc_expected =` assignment). Change:

```python
    per_wc_expected = compute_per_wc_expected(
        segments=segments,
        active_wc_names=active_wc_names,
        target_per_hour=target_per_hour,
        productive_minutes=lambda name, s_utc, e_utc:
            shift_config.productive_minutes_in_window(d, s_utc, e_utc),
    )
```

to:

```python
    breakdown_windows = wc_attributions.breakdown_windows_for_day(d)
    per_wc_expected = compute_per_wc_expected(
        segments=segments,
        active_wc_names=active_wc_names,
        target_per_hour=target_per_hour,
        productive_minutes=lambda name, wc_name, s_utc, e_utc:
            machine_breakdown.excluded_minutes_overlapping(
                breakdown_windows.get((name, wc_name), []),
                s_utc, e_utc, now, d,
                shift_config.productive_minutes_in_window,
            ) * -1 + shift_config.productive_minutes_in_window(d, s_utc, e_utc),
    )
```

Wait — that inline arithmetic is confusing to read. Use a small named closure instead. Replace the whole block with:

```python
    breakdown_windows = wc_attributions.breakdown_windows_for_day(d)

    def _productive_minutes_less_breakdown(name, wc_name, s_utc, e_utc):
        raw = shift_config.productive_minutes_in_window(d, s_utc, e_utc)
        excluded = machine_breakdown.excluded_minutes_overlapping(
            breakdown_windows.get((name, wc_name), []),
            s_utc, e_utc, now, d,
            shift_config.productive_minutes_in_window,
        )
        return max(0.0, raw - excluded)

    per_wc_expected = compute_per_wc_expected(
        segments=segments,
        active_wc_names=active_wc_names,
        target_per_hour=target_per_hour,
        productive_minutes=_productive_minutes_less_breakdown,
    )
```

Add the two new imports at the top of `routes/departments.py` alongside the existing `from .. import ...` block:

```python
from .. import wc_attributions, machine_breakdown
```

(If `routes/departments.py` already imports `wc_attributions` or `machine_breakdown` under a different existing import line, add to that line instead of duplicating an import statement — check the top of the file before editing.)

- [ ] **Step 6: Run the full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass — specifically confirm any existing recycling-dashboard tests that assert on `per_wc_expected` values still pass (they will, since `breakdown_windows` is empty `{}` for any day with no breakdown rows, making `excluded` always 0 and the result identical to before).

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/assignment_windows.py src/zira_dashboard/routes/departments.py \
        tests/test_assignment_windows_breakdown.py
git commit -m "feat(breakdown): recycling per-WC expected honors breakdown exclusion windows"
```

---

## Task 7: Pure `detect()` and `departed_at()`

**Files:**
- Modify: `src/zira_dashboard/machine_breakdown.py`
- Test: `tests/test_machine_breakdown_detect.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Pure-logic tests for machine_breakdown.detect() and departed_at()."""
from datetime import datetime, timedelta, timezone

from zira_dashboard.machine_breakdown import StationSignal, BreakdownCandidate, detect, departed_at

SHIFT_START = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)   # 7:00 AM Central
SHIFT_END = datetime(2026, 7, 8, 20, 30, tzinfo=timezone.utc)    # 3:30 PM Central


def test_detect_flags_station_with_no_output_past_threshold():
    last_output = SHIFT_START + timedelta(hours=2)
    now = last_output + timedelta(minutes=16)
    signals = [StationSignal(wc_name="Dismantler 2", last_output_utc=last_output, has_operator=True)]
    out = detect(signals, now, SHIFT_START, SHIFT_END)
    assert out == [BreakdownCandidate(wc_name="Dismantler 2", stop_utc=last_output)]


def test_detect_ignores_station_under_threshold():
    last_output = SHIFT_START + timedelta(hours=2)
    now = last_output + timedelta(minutes=10)
    signals = [StationSignal(wc_name="Dismantler 2", last_output_utc=last_output, has_operator=True)]
    assert detect(signals, now, SHIFT_START, SHIFT_END) == []


def test_detect_ignores_station_with_no_operator():
    last_output = SHIFT_START + timedelta(hours=2)
    now = last_output + timedelta(minutes=30)
    signals = [StationSignal(wc_name="Dismantler 2", last_output_utc=last_output, has_operator=False)]
    assert detect(signals, now, SHIFT_START, SHIFT_END) == []


def test_detect_treats_never_produced_as_stopped_since_shift_start():
    now = SHIFT_START + timedelta(minutes=20)
    signals = [StationSignal(wc_name="Dismantler 2", last_output_utc=None, has_operator=True)]
    out = detect(signals, now, SHIFT_START, SHIFT_END)
    assert out == [BreakdownCandidate(wc_name="Dismantler 2", stop_utc=SHIFT_START)]


def test_detect_returns_nothing_outside_shift_hours():
    last_output = SHIFT_START + timedelta(hours=2)
    now = SHIFT_END + timedelta(minutes=30)
    signals = [StationSignal(wc_name="Dismantler 2", last_output_utc=last_output, has_operator=True)]
    assert detect(signals, now, SHIFT_START, SHIFT_END) == []


def test_detect_respects_custom_threshold():
    last_output = SHIFT_START + timedelta(hours=2)
    now = last_output + timedelta(minutes=6)
    signals = [StationSignal(wc_name="Dismantler 2", last_output_utc=last_output, has_operator=True)]
    assert detect(signals, now, SHIFT_START, SHIFT_END, no_output_minutes=5) == [
        BreakdownCandidate(wc_name="Dismantler 2", stop_utc=last_output)
    ]


def test_departed_at_returns_none_when_open_punch_exists():
    stop = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
    punch_windows = {"Juan": [("Dismantler 2", SHIFT_START, None)]}
    assert departed_at("Juan", "Dismantler 2", punch_windows, stop) is None


def test_departed_at_returns_close_time_when_all_relevant_windows_closed():
    stop = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 8, 18, 5, tzinfo=timezone.utc)
    punch_windows = {"Juan": [("Dismantler 2", SHIFT_START, end)]}
    assert departed_at("Juan", "Dismantler 2", punch_windows, stop) == end


def test_departed_at_ignores_windows_that_closed_before_stop():
    stop = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
    old_end = datetime(2026, 7, 8, 10, 0, tzinfo=timezone.utc)  # closed long before the breakdown
    punch_windows = {"Juan": [("Dismantler 2", SHIFT_START, old_end)]}
    assert departed_at("Juan", "Dismantler 2", punch_windows, stop) is None


def test_departed_at_none_when_no_windows_for_wc():
    stop = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
    punch_windows = {"Juan": [("Repair 1", SHIFT_START, None)]}
    assert departed_at("Juan", "Dismantler 2", punch_windows, stop) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_machine_breakdown_detect.py -v`
Expected: FAIL — `ImportError: cannot import name 'StationSignal'`

- [ ] **Step 3: Add the dataclasses and both pure functions**

Add to `src/zira_dashboard/machine_breakdown.py`, above the existing `excluded_minutes_for_windows`:

```python
from dataclasses import dataclass
from datetime import timedelta

BREAKDOWN_NO_OUTPUT_MINUTES = 15
"""Default minutes of no output (while an operator is clocked in) before a
station is flagged as broken down."""


@dataclass(frozen=True)
class StationSignal:
    wc_name: str
    last_output_utc: datetime | None  # None = no output yet today
    has_operator: bool  # at least one operator currently clocked in on this WC


@dataclass(frozen=True)
class BreakdownCandidate:
    wc_name: str
    stop_utc: datetime


def detect(
    signals: list[StationSignal],
    now: datetime,
    shift_start_utc: datetime,
    shift_end_utc: datetime,
    no_output_minutes: int = BREAKDOWN_NO_OUTPUT_MINUTES,
) -> list[BreakdownCandidate]:
    """Pure. Which stations should open a NEW breakdown incident this tick.

    A station is a candidate when it has an operator clocked in AND has
    produced nothing for >= no_output_minutes (measured from its last output,
    or from shift start if it has never produced today) AND `now` is within
    shift hours. The caller is responsible for excluding stations that
    already have an open incident, an active testing window, or were
    recently dismissed without new output since -- this function only
    applies the no-output-while-staffed rule."""
    if now < shift_start_utc or now > shift_end_utc:
        return []
    threshold = timedelta(minutes=no_output_minutes)
    out: list[BreakdownCandidate] = []
    for sig in signals:
        if not sig.has_operator:
            continue
        stop = sig.last_output_utc or shift_start_utc
        if now - stop < threshold:
            continue
        out.append(BreakdownCandidate(wc_name=sig.wc_name, stop_utc=stop))
    return out


def departed_at(
    person_name: str,
    wc_name: str,
    punch_windows: dict[str, list[tuple]],
    stop_utc: datetime,
) -> datetime | None:
    """Pure. None if the person still has an open (or not-yet-closed-since-
    the-breakdown) punch on wc_name; otherwise the UTC time of their last
    closed punch window on wc_name at/after `stop_utc` -- i.e. when they left
    the broken machine (by transfer or clock-out). `punch_windows` matches
    assignment_windows.resolve_segments's punch_windows param shape:
    {person_name: [(wc_name, start_utc, end_utc|None), ...]}."""
    windows = [w for w in punch_windows.get(person_name, []) if w[0] == wc_name]
    relevant = [(s, e) for (_wc, s, e) in windows if e is None or e > stop_utc]
    if not relevant:
        return None
    if any(e is None for _, e in relevant):
        return None
    return max(e for _, e in relevant)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_machine_breakdown_detect.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/machine_breakdown.py tests/test_machine_breakdown_detect.py
git commit -m "feat(breakdown): pure detect() and departed_at() station-breakdown logic"
```

---

## Task 8: Incident store + snooze (I/O)

**Files:**
- Modify: `src/zira_dashboard/machine_breakdown.py`
- Test: `tests/test_machine_breakdown_store.py` (DB-backed, mirrors `tests/test_inbox_open_items.py`'s fixture)

- [ ] **Step 1: Write the failing tests**

```python
"""machine_breakdowns / breakdown_snoozes store (Postgres). Mirrors
tests/test_inbox_open_items.py's fixture pattern."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from zira_dashboard import db, machine_breakdown

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")

WC = "Test Dismantler"


@pytest.fixture(autouse=True)
def _clean():
    db.bootstrap_schema()
    db.execute("DELETE FROM machine_breakdowns WHERE wc_name = %s", (WC,))
    yield
    db.execute("DELETE FROM machine_breakdowns WHERE wc_name = %s", (WC,))


def test_open_incident_and_get_open_incident():
    now = datetime.now(timezone.utc)
    incident_id = machine_breakdown.open_incident(WC, now.date(), now, source="auto")
    row = machine_breakdown.get_open_incident(WC, now.date())
    assert row["id"] == incident_id
    assert row["source"] == "auto"
    assert row["resolved_at"] is None


def test_get_open_incident_none_when_resolved():
    now = datetime.now(timezone.utc)
    incident_id = machine_breakdown.open_incident(WC, now.date(), now, source="auto")
    machine_breakdown.resolve_incident(incident_id, "recovered", resume_utc=now)
    assert machine_breakdown.get_open_incident(WC, now.date()) is None


def test_get_incident_by_id():
    now = datetime.now(timezone.utc)
    incident_id = machine_breakdown.open_incident(WC, now.date(), now, source="manual")
    row = machine_breakdown.get_incident(incident_id)
    assert row["wc_name"] == WC
    assert row["source"] == "manual"
    assert machine_breakdown.get_incident(-1) is None


def test_resolve_and_reopen_incident():
    now = datetime.now(timezone.utc)
    incident_id = machine_breakdown.open_incident(WC, now.date(), now, source="auto")
    machine_breakdown.resolve_incident(incident_id, "dismissed")
    row = machine_breakdown.get_incident(incident_id)
    assert row["resolution"] == "dismissed"
    assert row["resolved_at"] is not None

    machine_breakdown.reopen_incident(incident_id)
    row = machine_breakdown.get_incident(incident_id)
    assert row["resolution"] is None
    assert row["resolved_at"] is None


def test_all_open_incidents():
    now = datetime.now(timezone.utc)
    id1 = machine_breakdown.open_incident(WC, now.date(), now, source="auto")
    row_ids = {r["id"] for r in machine_breakdown.all_open_incidents(now.date())}
    assert id1 in row_ids
    machine_breakdown.resolve_incident(id1, "recovered")
    row_ids = {r["id"] for r in machine_breakdown.all_open_incidents(now.date())}
    assert id1 not in row_ids


def test_snooze_operator_and_active_snooze_until():
    now = datetime.now(timezone.utc)
    incident_id = machine_breakdown.open_incident(WC, now.date(), now, source="auto")
    assert machine_breakdown.active_snooze_until(incident_id, "Juan") is None
    machine_breakdown.snooze_operator(incident_id, "Juan")
    until = machine_breakdown.active_snooze_until(incident_id, "Juan")
    assert until is not None
    assert until > now


def test_active_snooze_until_none_after_expiry(monkeypatch):
    now = datetime.now(timezone.utc)
    incident_id = machine_breakdown.open_incident(WC, now.date(), now, source="auto")
    db.execute(
        "INSERT INTO breakdown_snoozes (breakdown_id, person_name, until_utc) VALUES (%s, %s, %s)",
        (incident_id, "Juan", now - timedelta(minutes=1)),
    )
    assert machine_breakdown.active_snooze_until(incident_id, "Juan") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_machine_breakdown_store.py -v`
Expected: FAIL — `AttributeError: module 'zira_dashboard.machine_breakdown' has no attribute 'open_incident'`

- [ ] **Step 3: Add the incident store functions**

Add to `src/zira_dashboard/machine_breakdown.py`:

```python
BREAKDOWN_SNOOZE_MINUTES = 15


def open_incident(wc_name: str, day, stop_utc: datetime, source: str = "auto") -> int:
    """Open a new breakdown incident. Caller must ensure no incident is
    already open for (wc_name, day) -- see get_open_incident."""
    from . import db
    rows = db.query(
        "INSERT INTO machine_breakdowns (wc_name, day, detected_stop_utc, source) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (wc_name, day, stop_utc, source),
    )
    return rows[0]["id"]


def get_open_incident(wc_name: str, day) -> dict | None:
    """The currently-open incident for (wc_name, day), or None."""
    from . import db
    rows = db.query(
        "SELECT id, wc_name, day, detected_stop_utc, source, created_at, "
        "resolved_at, resolution, resume_utc FROM machine_breakdowns "
        "WHERE wc_name = %s AND day = %s AND resolved_at IS NULL",
        (wc_name, day),
    )
    return rows[0] if rows else None


def get_incident(incident_id: int) -> dict | None:
    """One incident by id, open or resolved."""
    from . import db
    rows = db.query(
        "SELECT id, wc_name, day, detected_stop_utc, source, created_at, "
        "resolved_at, resolution, resume_utc FROM machine_breakdowns WHERE id = %s",
        (incident_id,),
    )
    return rows[0] if rows else None


def all_open_incidents(day) -> list[dict]:
    """Every currently-open incident for `day`, oldest first."""
    from . import db
    return db.query(
        "SELECT id, wc_name, day, detected_stop_utc, source, created_at, "
        "resolved_at, resolution, resume_utc FROM machine_breakdowns "
        "WHERE day = %s AND resolved_at IS NULL ORDER BY detected_stop_utc",
        (day,),
    )


def resolve_incident(incident_id: int, resolution: str, resume_utc: datetime | None = None) -> None:
    """Mark an incident resolved (resolution in 'recovered'|'handled'|'dismissed')."""
    from . import db
    db.execute(
        "UPDATE machine_breakdowns SET resolved_at = now(), resolution = %s, resume_utc = %s "
        "WHERE id = %s",
        (resolution, resume_utc, incident_id),
    )


def reopen_incident(incident_id: int) -> None:
    """Undo a resolution -- clears resolved_at/resolution/resume_utc so the
    incident is open again (dismiss-undo)."""
    from . import db
    db.execute(
        "UPDATE machine_breakdowns SET resolved_at = NULL, resolution = NULL, resume_utc = NULL "
        "WHERE id = %s",
        (incident_id,),
    )


def snooze_operator(incident_id: int, person_name: str, minutes: int = BREAKDOWN_SNOOZE_MINUTES) -> None:
    """Silence one operator's row on this incident's card for `minutes`."""
    from . import db
    until = datetime.now(UTC) + timedelta(minutes=minutes)
    db.execute(
        "INSERT INTO breakdown_snoozes (breakdown_id, person_name, until_utc) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (breakdown_id, person_name) DO UPDATE SET "
        "until_utc = EXCLUDED.until_utc, created_at = now()",
        (incident_id, person_name, until),
    )


def active_snooze_until(incident_id: int, person_name: str) -> datetime | None:
    """The until_utc timestamp if this operator's snooze on this incident
    hasn't expired yet, else None."""
    from . import db
    rows = db.query(
        "SELECT until_utc FROM breakdown_snoozes "
        "WHERE breakdown_id = %s AND person_name = %s AND until_utc > now()",
        (incident_id, person_name),
    )
    return rows[0]["until_utc"] if rows else None
```

Add the missing `UTC` import at the top of the file (it currently only imports `date, datetime`):

```python
from datetime import date, datetime, timedelta, UTC
```

(Remove the separate `from datetime import timedelta` added in Task 7 if it's now redundant with this consolidated import line — keep exactly one `from datetime import ...` line at the top of the file.)

- [ ] **Step 4: Run test to verify it passes**

Run: `DATABASE_URL=<your test db url> ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_machine_breakdown_store.py -v`
Expected: 7 passed (skipped if `DATABASE_URL` isn't set — check `tests/conftest.py` for how the repo wires the embedded `pgserver` fixture to set `DATABASE_URL` automatically for the full suite run)

- [ ] **Step 5: Run the full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/machine_breakdown.py tests/test_machine_breakdown_store.py
git commit -m "feat(breakdown): incident store CRUD and per-operator snooze"
```

---

## Task 9: `current_rows()` + `run_detect_tick()` + `report_manual()`

**Files:**
- Modify: `src/zira_dashboard/machine_breakdown.py`
- Modify: `src/zira_dashboard/inbox_keys.py` (a forward dependency pulled in from Task 11 — see Step 2b)
- Test: `tests/test_machine_breakdown_rows.py`

This is the I/O glue: pulls Zira station data + live operator resolution, calls the pure `detect()`/`departed_at()`, opens/caps/resolves incidents, and shapes the snapshot rows the inbox will render.

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for current_rows()/run_detect_tick()/report_manual() -- the I/O glue.
Heavy monkeypatching of collaborators, following tests/test_inbox_reconcile.py's style."""
from datetime import date, datetime, timedelta, timezone

from zira_dashboard import machine_breakdown


def _now():
    return datetime(2026, 7, 8, 18, 22, tzinfo=timezone.utc)  # 1:22 PM Central


def test_run_detect_tick_opens_new_incident(monkeypatch):
    stop = _now() - timedelta(minutes=20)
    monkeypatch.setattr(machine_breakdown, "_station_signals", lambda day, now: [
        machine_breakdown.StationSignal("Dismantler 2", stop, True)
    ])
    monkeypatch.setattr(machine_breakdown, "_shift_bounds", lambda day: (
        _now() - timedelta(hours=6), _now() + timedelta(hours=2)
    ))
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda wc, day: None)
    opened = {}

    def _open_incident(wc, day, stop_utc, source):
        # NB: `opened.setdefault(...) or 1` looks tempting here but always
        # returns the (truthy) tuple, never 1 -- setdefault returns the
        # stored value, not a success flag.
        opened["args"] = (wc, day, stop_utc, source)
        return 1

    monkeypatch.setattr(machine_breakdown, "open_incident", _open_incident)
    monkeypatch.setattr(machine_breakdown, "_operators_on_wc", lambda wc, day: ["Juan"])
    from zira_dashboard import wc_attributions
    added = []
    monkeypatch.setattr(wc_attributions, "add_breakdown",
                        lambda day, wc, person, start, breakdown_id: added.append((day, wc, person, start, breakdown_id)) or 99)
    monkeypatch.setattr(machine_breakdown, "_cap_departed_operators", lambda incident, day, now: None)
    monkeypatch.setattr(machine_breakdown, "_maybe_auto_resolve", lambda incident, day, now: None)

    machine_breakdown.run_detect_tick(day=date(2026, 7, 8), now=_now())

    assert opened["args"] == ("Dismantler 2", date(2026, 7, 8), stop, "auto")
    assert added == [(date(2026, 7, 8), "Dismantler 2", "Juan", stop, 1)]


def test_run_detect_tick_skips_wc_with_open_incident(monkeypatch):
    stop = _now() - timedelta(minutes=20)
    monkeypatch.setattr(machine_breakdown, "_station_signals", lambda day, now: [
        machine_breakdown.StationSignal("Dismantler 2", stop, True)
    ])
    monkeypatch.setattr(machine_breakdown, "_shift_bounds", lambda day: (
        _now() - timedelta(hours=6), _now() + timedelta(hours=2)
    ))
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda wc, day: {"id": 5})
    called = []
    monkeypatch.setattr(machine_breakdown, "open_incident", lambda *a, **k: called.append(1))
    monkeypatch.setattr(machine_breakdown, "_cap_departed_operators", lambda incident, day, now: None)
    monkeypatch.setattr(machine_breakdown, "_maybe_auto_resolve", lambda incident, day, now: None)

    machine_breakdown.run_detect_tick(day=date(2026, 7, 8), now=_now())

    assert called == []


def test_cap_departed_operators_caps_and_leaves_still_present_untouched(monkeypatch):
    from zira_dashboard import wc_attributions
    incident = {"id": 1, "wc_name": "Dismantler 2", "day": date(2026, 7, 8),
                "detected_stop_utc": _now() - timedelta(minutes=30)}
    monkeypatch.setattr(machine_breakdown, "_operators_on_wc", lambda wc, day: ["Juan", "Benjamin"])
    dep_end = _now() - timedelta(minutes=5)
    monkeypatch.setattr(machine_breakdown, "_punch_windows_for_day", lambda day: {
        "Juan": [("Dismantler 2", _now() - timedelta(hours=6), dep_end)],
        "Benjamin": [("Dismantler 2", _now() - timedelta(hours=6), None)],
    })
    monkeypatch.setattr(wc_attributions, "open_breakdown_row",
                        lambda day, wc, person: {"id": 10, "start_utc": incident["detected_stop_utc"]} if person == "Juan" else {"id": 11, "start_utc": incident["detected_stop_utc"]})
    capped = []
    monkeypatch.setattr(wc_attributions, "cap_breakdown", lambda row_id, end: capped.append((row_id, end)))

    machine_breakdown._cap_departed_operators(incident, date(2026, 7, 8), _now())

    assert capped == [(10, dep_end)]  # only Juan (closed window); Benjamin still open


def test_maybe_auto_resolve_resolves_when_station_producing_again(monkeypatch):
    incident = {"id": 1, "wc_name": "Dismantler 2", "day": date(2026, 7, 8),
                "detected_stop_utc": _now() - timedelta(minutes=30)}
    resume = _now() - timedelta(minutes=2)
    monkeypatch.setattr(machine_breakdown, "_last_output_after", lambda wc, day, stop: resume)
    monkeypatch.setattr(machine_breakdown, "_operators_on_wc", lambda wc, day: ["Juan"])
    from zira_dashboard import wc_attributions
    monkeypatch.setattr(wc_attributions, "open_breakdown_row", lambda day, wc, person: {"id": 10})
    capped = []
    monkeypatch.setattr(wc_attributions, "cap_breakdown", lambda row_id, end: capped.append((row_id, end)))
    resolved = []
    monkeypatch.setattr(machine_breakdown, "resolve_incident",
                        lambda incident_id, resolution, resume_utc=None: resolved.append((incident_id, resolution, resume_utc)))

    machine_breakdown._maybe_auto_resolve(incident, date(2026, 7, 8), _now())

    assert resolved == [(1, "recovered", resume)]
    assert capped == [(10, resume)]  # any operator still open gets capped at resume


def test_maybe_auto_resolve_noop_when_still_down(monkeypatch):
    incident = {"id": 1, "wc_name": "Dismantler 2", "day": date(2026, 7, 8),
                "detected_stop_utc": _now() - timedelta(minutes=30)}
    monkeypatch.setattr(machine_breakdown, "_last_output_after", lambda wc, day, stop: None)
    resolved = []
    monkeypatch.setattr(machine_breakdown, "resolve_incident", lambda *a, **k: resolved.append(1))

    machine_breakdown._maybe_auto_resolve(incident, date(2026, 7, 8), _now())

    assert resolved == []


def test_current_rows_shapes_header_and_operator_rows(monkeypatch):
    incident = {"id": 1, "wc_name": "Dismantler 2", "day": date(2026, 7, 8),
                "detected_stop_utc": _now() - timedelta(minutes=25),
                "source": "auto", "resolved_at": None, "resolution": None}
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda day: [incident])
    monkeypatch.setattr(machine_breakdown, "_operators_on_wc", lambda wc, day: ["Juan", "Benjamin"])
    monkeypatch.setattr(machine_breakdown, "active_snooze_until",
                        lambda incident_id, person: (_now() + timedelta(minutes=10)) if person == "Benjamin" else None)
    from zira_dashboard import staffing
    monkeypatch.setattr(staffing, "LOCATIONS", [])

    rows = machine_breakdown.current_rows(day=date(2026, 7, 8), now=_now())

    # The header (machine) row is the only row carrying a dismiss_action --
    # both it and a snoozed operator row have action=None, so that alone
    # can't distinguish them.
    header = [r for r in rows if r.get("dismiss_action") is not None]
    assert len(header) == 1
    assert header[0]["name"] == "Dismantler 2"
    assert header[0]["priority"] == "urgent"

    juan_row = [r for r in rows if r.get("action") and r["action"].get("person_name") == "Juan"][0]
    assert juan_row["action"]["type"] == "breakdown"
    assert juan_row["priority"] == "urgent"

    benjamin_row = [r for r in rows if r["name"] == "Benjamin"][0]
    assert benjamin_row["priority"] == "muted"
    assert benjamin_row.get("action") is None  # snoozed -- no action buttons


def test_report_manual_opens_incident_with_operators(monkeypatch):
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda wc, day: None)
    monkeypatch.setattr(machine_breakdown, "_last_output_before", lambda wc, day, now: None)
    opened = {}

    def fake_open_incident(wc, day, stop_utc, source):
        opened["args"] = (wc, day, stop_utc, source)
        return 1

    monkeypatch.setattr(machine_breakdown, "open_incident", fake_open_incident)
    monkeypatch.setattr(machine_breakdown, "_operators_on_wc", lambda wc, day: ["Juan"])
    from zira_dashboard import wc_attributions
    monkeypatch.setattr(wc_attributions, "add_breakdown", lambda day, wc, person, start, breakdown_id: 5)
    resolved = []
    monkeypatch.setattr(machine_breakdown, "resolve_incident", lambda *a, **k: resolved.append(1))

    result = machine_breakdown.report_manual("Dismantler 2", day=date(2026, 7, 8), now=_now())

    assert opened["args"][0] == "Dismantler 2"
    assert opened["args"][3] == "manual"
    assert result["ok"] is True
    assert resolved == []  # has an operator -- stays open for the manager to act on


def test_report_manual_self_resolves_when_no_operators(monkeypatch):
    """Matches the design's "informational only, auto-resolves" rule for a
    manually-reported machine with no one currently on it -- nothing to act
    on, so don't leave a dead card sitting in the queue."""
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda wc, day: None)
    monkeypatch.setattr(machine_breakdown, "_last_output_before", lambda wc, day, now: None)
    monkeypatch.setattr(machine_breakdown, "open_incident", lambda wc, day, stop_utc, source: 1)
    monkeypatch.setattr(machine_breakdown, "_operators_on_wc", lambda wc, day: [])
    resolved = []
    monkeypatch.setattr(machine_breakdown, "resolve_incident",
                        lambda incident_id, resolution, resume_utc=None: resolved.append((incident_id, resolution)))

    result = machine_breakdown.report_manual("Dismantler 2", day=date(2026, 7, 8), now=_now())

    assert result == {"ok": True, "incident_id": 1}
    assert resolved == [(1, "handled")]


def test_report_manual_noop_when_already_open(monkeypatch):
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda wc, day: {"id": 5})
    called = []
    monkeypatch.setattr(machine_breakdown, "open_incident", lambda *a, **k: called.append(1))

    result = machine_breakdown.report_manual("Dismantler 2", day=date(2026, 7, 8), now=_now())

    assert called == []
    assert result == {"ok": True, "incident_id": 5, "already_open": True}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_machine_breakdown_rows.py -v`
Expected: FAIL — `AttributeError: module 'zira_dashboard.machine_breakdown' has no attribute '_station_signals'`

- [ ] **Step 2b: Add `inbox_keys.breakdown` — a forward dependency from Task 11**

`current_rows()` (added in Step 5 below) calls `inbox_keys.breakdown(...)`, but that function was originally scheduled for Task 11, which comes AFTER this task. Add it now so this task is actually testable; Task 11 will find it already present and skip re-adding it (see the note in Task 11's own Step 3).

Add to `src/zira_dashboard/inbox_keys.py`:

```python
def breakdown(wc_name, stop_iso, person_name=None) -> str:
    """The incident's own key when person_name is None (the card header /
    dismiss target); a distinct per-operator key otherwise (the Transfer /
    snooze / auto-resolve target for one operator's row)."""
    if person_name:
        return f"breakdown:{wc_name}:{stop_iso}:{person_name}"
    return f"breakdown:{wc_name}:{stop_iso}"
```

- [ ] **Step 3: Add the I/O collaborator functions**

Add to `src/zira_dashboard/machine_breakdown.py`:

```python
def _enabled() -> bool:
    import os
    return os.environ.get("MACHINE_BREAKDOWN_ENABLED", "true").strip().lower() not in ("0", "false", "no")


def _shift_bounds(day: date) -> tuple[datetime, datetime]:
    from .shift_config import shift_start_for, shift_end_for, SITE_TZ
    start = datetime.combine(day, shift_start_for(day), tzinfo=SITE_TZ).astimezone(UTC)
    end = datetime.combine(day, shift_end_for(day), tzinfo=SITE_TZ).astimezone(UTC)
    return start, end


def _operators_on_wc(wc_name: str, day: date) -> list[str]:
    """Names of people currently resolved onto wc_name and clocked in, via
    the same assignment_windows machinery the recycling dashboard uses."""
    from . import staffing, wc_attributions, assignment_windows, timeclock_windows, live_cache
    now = datetime.now(UTC)
    sched = staffing.load_schedule(day)
    punch_windows = timeclock_windows.attendance_windows_for_day(day)
    attributions = wc_attributions.creditable_for_day(day)
    shift_start, _ = _shift_bounds(day)
    segments = assignment_windows.resolve_segments(
        assignments=sched.assignments,
        attributions=attributions,
        punch_windows=punch_windows,
        shift_start_utc=shift_start,
        cap_utc=now,
    )
    return sorted({s.person_name for s in segments if s.wc_name == wc_name})


def _punch_windows_for_day(day: date) -> dict:
    from . import timeclock_windows
    return timeclock_windows.attendance_windows_for_day(day)


def _station_signals(day: date, now: datetime) -> list[StationSignal]:
    """One StationSignal per metered recycling station with an operator
    currently on it."""
    from . import staffing
    from .leaderboard import cached_leaderboard
    from .stations import recycling_stations
    from .deps import client  # local import: avoid a hard dep at module load
    totals = cached_leaderboard(client, recycling_stations(), day, now_utc=now)
    meter_to_loc_name = {loc.meter_id: loc.name for loc in staffing.LOCATIONS if loc.meter_id}
    out: list[StationSignal] = []
    for total in totals:
        wc_name = meter_to_loc_name.get(total.station.meter_id, total.station.name)
        # samples[-1][0] is the timestamp of the last REAL unit produced.
        # active_intervals[-1][1] is NOT that -- leaderboard._active_intervals
        # pads the tail to min(last_real_unit + TRANSFER_GAP(60min), now), so
        # using it here would silently inflate detection latency from the
        # spec's 15 minutes to ~75 minutes (60min padding + 15min threshold).
        last_output = total.samples[-1][0] if total.samples else None
        has_operator = bool(_operators_on_wc(wc_name, day))
        out.append(StationSignal(wc_name=wc_name, last_output_utc=last_output, has_operator=has_operator))
    return out


def _last_output_after(wc_name: str, day: date, stop_utc: datetime) -> datetime | None:
    """The most recent output time for wc_name strictly after `stop_utc`, or
    None if it's still silent -- used to detect recovery."""
    for sig in _station_signals(day, datetime.now(UTC)):
        if sig.wc_name == wc_name and sig.last_output_utc and sig.last_output_utc > stop_utc:
            return sig.last_output_utc
    return None


def _last_output_before(wc_name: str, day: date, now: datetime) -> datetime | None:
    """The station's last output time as of `now` (or None if it hasn't
    produced today) -- used by the manual report button."""
    for sig in _station_signals(day, now):
        if sig.wc_name == wc_name:
            return sig.last_output_utc
    return None
```

- [ ] **Step 4: Add `run_detect_tick`, `_cap_departed_operators`, `_maybe_auto_resolve`**

```python
def run_detect_tick(day: date | None = None, now: datetime | None = None) -> None:
    """One detection pass: open new incidents, cap operators who've left a
    broken machine, and auto-resolve incidents whose machine is producing
    again. Called from the warmer; best-effort per incident so one bad
    incident never blocks the others."""
    if not _enabled():
        return
    from . import wc_attributions
    from .plant_day import today as plant_today
    day = day or plant_today()
    now = now or datetime.now(UTC)
    shift_start, shift_end = _shift_bounds(day)

    try:
        open_incidents = all_open_incidents(day)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "machine breakdown: failed to load open incidents", exc_info=True)
        open_incidents = []

    for incident in open_incidents:
        try:
            _cap_departed_operators(incident, day, now)
            _maybe_auto_resolve(incident, day, now)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "machine breakdown tick failed for incident %s", incident["id"], exc_info=True)

    candidates = detect(_station_signals(day, now), now, shift_start, shift_end)
    for candidate in candidates:
        if get_open_incident(candidate.wc_name, day) is not None:
            continue
        try:
            incident_id = open_incident(candidate.wc_name, day, candidate.stop_utc, source="auto")
            for person in _operators_on_wc(candidate.wc_name, day):
                wc_attributions.add_breakdown(day, candidate.wc_name, person, candidate.stop_utc, incident_id)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "machine breakdown open failed for %s", candidate.wc_name, exc_info=True)


def _cap_departed_operators(incident: dict, day: date, now: datetime) -> None:
    """Cap any operator's open breakdown row the moment they leave the
    broken machine (transfer or self-punch-out) -- detected via their punch
    windows, not via the Transfer button (which caps immediately itself;
    this is the passive/punch-out path)."""
    from . import wc_attributions
    wc_name = incident["wc_name"]
    stop = incident["detected_stop_utc"]
    punch_windows = _punch_windows_for_day(day)
    for person in _operators_on_wc(wc_name, day) or list(punch_windows.keys()):
        dep = departed_at(person, wc_name, punch_windows, stop)
        if dep is None:
            continue
        row = wc_attributions.open_breakdown_row(day, wc_name, person)
        if row is not None:
            wc_attributions.cap_breakdown(row["id"], dep)


def _maybe_auto_resolve(incident: dict, day: date, now: datetime) -> None:
    """Resolve an incident as 'recovered' once its station has produced
    output again, capping any operator still open at the resume time."""
    from . import wc_attributions
    resume = _last_output_after(incident["wc_name"], day, incident["detected_stop_utc"])
    if resume is None:
        return
    for person in _operators_on_wc(incident["wc_name"], day):
        row = wc_attributions.open_breakdown_row(day, incident["wc_name"], person)
        if row is not None:
            wc_attributions.cap_breakdown(row["id"], resume)
    resolve_incident(incident["id"], "recovered", resume_utc=resume)
```

Note: `_cap_departed_operators` iterates `_operators_on_wc(...) or list(punch_windows.keys())` — once an operator has fully left (transferred away), they're no longer in `_operators_on_wc`'s live result, so the fallback to every person in `punch_windows` (a small, single-day set) ensures a departed operator with a still-open breakdown row is still checked and capped. In practice this only matters for a tick or two right after a transfer, since the Transfer endpoint (Task 13) caps immediately and doesn't wait for this tick.

- [ ] **Step 5: Add `current_rows` and `report_manual`**

```python
def current_rows(day: date | None = None, now: datetime | None = None) -> list[dict]:
    """Snapshot rows for every open incident today: one header row (machine
    info + dismiss) followed by one row per operator (Transfer/Snooze, or a
    muted no-action row while snoozed). Header and operator rows share the
    same item_kind ("breakdown") but differ by action/absence of action --
    see inbox_keys.breakdown and routes/exceptions.py's undo wiring."""
    from . import inbox_keys
    from .plant_day import today as plant_today
    day = day or plant_today()
    now = now or datetime.now(UTC)

    rows: list[dict] = []
    for incident in all_open_incidents(day):
        wc_name = incident["wc_name"]
        stop = incident["detected_stop_utc"]
        stop_iso = stop.isoformat()
        elapsed_min = int((now - stop).total_seconds() // 60)
        rows.append({
            "name": wc_name,
            "label": "Stopped producing",
            "detail": f"No output since {_local_time_label(stop)} ({elapsed_min} min)",
            "priority": "urgent",
            "badge": "AUTO-DETECTED" if incident["source"] == "auto" else "MANUAL",
            "row_key": f"breakdown_header:{wc_name}:{stop_iso}",
            "item_key": inbox_keys.breakdown(wc_name, stop_iso),
            "action": None,
            "dismiss_action": {
                "type": "breakdown_dismiss",
                "incident_id": incident["id"],
            },
        })
        for person in _operators_on_wc(wc_name, day):
            snoozed_until = active_snooze_until(incident["id"], person)
            item_key = inbox_keys.breakdown(wc_name, stop_iso, person)
            if snoozed_until is not None:
                mins_left = max(1, int((snoozed_until - now).total_seconds() // 60))
                rows.append({
                    "name": person,
                    "label": "Snoozed",
                    "detail": f"Re-checks in {mins_left} min",
                    "priority": "muted",
                    "badge": "Follow-up",
                    "row_key": f"breakdown_snoozed:{wc_name}:{stop_iso}:{person}",
                    "item_key": item_key,
                    "action": None,
                })
                continue
            rows.append({
                "name": person,
                "label": f"Idle — {wc_name} is down",
                "detail": "",
                "priority": "urgent",
                "badge": "Needs decision",
                "row_key": f"breakdown_op:{wc_name}:{stop_iso}:{person}",
                "item_key": item_key,
                "action": {
                    "type": "breakdown",
                    "incident_id": incident["id"],
                    "person_name": person,
                    "wc_name": wc_name,
                },
            })
    return rows


def _local_time_label(dt: datetime) -> str:
    import os
    from .shift_config import SITE_TZ
    local = dt.astimezone(SITE_TZ)
    fmt = "%#I:%M %p" if os.name == "nt" else "%-I:%M %p"
    return local.strftime(fmt)


def report_manual(wc_name: str, day: date | None = None, now: datetime | None = None) -> dict:
    """Open (or find) a breakdown incident for wc_name on demand -- the
    "+ Report a breakdown" button. Returns {ok, incident_id, already_open?}."""
    from . import wc_attributions
    from .plant_day import today as plant_today
    day = day or plant_today()
    now = now or datetime.now(UTC)

    existing = get_open_incident(wc_name, day)
    if existing is not None:
        return {"ok": True, "incident_id": existing["id"], "already_open": True}

    stop = _last_output_before(wc_name, day, now) or now
    incident_id = open_incident(wc_name, day, stop, source="manual")
    operators = _operators_on_wc(wc_name, day)
    for person in operators:
        wc_attributions.add_breakdown(day, wc_name, person, stop, incident_id)
    if not operators:
        # Nothing to act on -- resolve immediately rather than leaving an
        # empty, un-actionable card in the queue (mirrors the "informational
        # only, auto-resolves" edge case in the design spec).
        resolve_incident(incident_id, "handled")
    return {"ok": True, "incident_id": incident_id}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_machine_breakdown_rows.py -v`
Expected: 9 passed

- [ ] **Step 7: Run the full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add src/zira_dashboard/machine_breakdown.py tests/test_machine_breakdown_rows.py
git commit -m "feat(breakdown): current_rows/run_detect_tick/report_manual I/O glue"
```

---

## Task 10: Register the detection warmer

**Files:**
- Modify: `src/zira_dashboard/app.py`
- Test: `tests/test_machine_breakdown_warmer.py`

- [ ] **Step 1: Write the failing test**

```python
"""The machine-breakdown detection tick is registered in the warmer list."""
from zira_dashboard import app as app_module


def test_machine_breakdown_warmer_registered():
    names = [name for name, _tick, _interval in app_module._WARMERS]
    assert "machine breakdown" in names


async def test_tick_machine_breakdown_calls_run_detect_tick(monkeypatch):
    called = []
    from zira_dashboard import machine_breakdown
    monkeypatch.setattr(machine_breakdown, "run_detect_tick", lambda: called.append(1))
    await app_module._tick_machine_breakdown()
    assert called == [1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_machine_breakdown_warmer.py -v`
Expected: FAIL — `AssertionError` (name not in `_WARMERS`) and `AttributeError` for the missing tick function.

- [ ] **Step 3: Add the tick function and register it**

In `src/zira_dashboard/app.py`, add near `_tick_inbox_reconcile`:

```python
async def _tick_machine_breakdown():
    """Detect newly-broken recycling machines, cap operators who've left a
    broken machine, and auto-resolve incidents whose machine resumed. Runs
    off the same cadence as the production-data warmer since it reads the
    same Zira station data."""
    from . import machine_breakdown
    await asyncio.to_thread(machine_breakdown.run_detect_tick)
```

Add it to the `_WARMERS` list, right after `("live_cache", _tick_live_cache, 45)` (same 45s cadence, since it depends on the same Zira data that warmer refreshes):

```python
_WARMERS = [
    ("Zira cache", _tick_zira_cache, 30),
    ("live_cache", _tick_live_cache, 45),
    ("machine breakdown", _tick_machine_breakdown, 45),
    ("kiosk sync", _tick_timeclock_sync, 60),
    ("Odoo open-attendance", _tick_odoo_attendance, 30),
    ("auto-lunch", _tick_auto_lunch, 60),
    ("time-off sync", _tick_time_off_sync, 60),
    ("time-off poll", _tick_time_off_poll, 60),
    ("time-off balance", _tick_time_off_balance, 600),
    ("staffing pages", _tick_staffing_pages, 45),
    ("inbox warm", _tick_inbox, 20),
    ("staffing stable", _tick_staffing_stable, 300),
    ("missing WC", _tick_missing_wc, 180),
    ("missed punch-out", _tick_missed_punch_out, 60),
    ("forklift snapshot", _tick_forklift, 600),
    ("Inbox reconcile", _tick_inbox_reconcile, 60),
    ("calendar conflicts", _tick_calendar_conflicts, 21600),
    ("time-off local backfill", _tick_time_off_backfill, 3600),
    ("page-usage flush", _tick_page_usage, 60),
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_machine_breakdown_warmer.py -v`
Expected: 2 passed

- [ ] **Step 5: Run the full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/app.py tests/test_machine_breakdown_warmer.py
git commit -m "feat(breakdown): register the 45s machine-breakdown detection warmer"
```

---

## Task 11: `inbox_keys.breakdown` + snapshot section

**Files:**
- Modify: `src/zira_dashboard/inbox_keys.py` (already done in Task 9 — this task just adds its regression test, see Step 1)
- Modify: `src/zira_dashboard/exception_inbox.py`
- Test: `tests/test_inbox_keys_breakdown.py`
- Test: `tests/test_exception_inbox_breakdown.py`

- [ ] **Step 1: Write the test for the key builder — should already pass**

`inbox_keys.breakdown` was already implemented in Task 9 (Step 2b of that task), since `machine_breakdown.current_rows()` needed it as a forward dependency. Confirm it's there before writing this test: `grep -n "^def breakdown" src/zira_dashboard/inbox_keys.py` should find it. This step is now a regression-lock test, not new-feature TDD — write it anyway for permanent coverage, but don't expect it to fail first.

```python
from zira_dashboard import inbox_keys


def test_breakdown_key_without_person():
    assert inbox_keys.breakdown("Dismantler 2", "2026-07-08T18:02:00+00:00") == \
        "breakdown:Dismantler 2:2026-07-08T18:02:00+00:00"


def test_breakdown_key_with_person():
    assert inbox_keys.breakdown("Dismantler 2", "2026-07-08T18:02:00+00:00", "Juan") == \
        "breakdown:Dismantler 2:2026-07-08T18:02:00+00:00:Juan"
```

- [ ] **Step 2: Run test to verify it passes immediately**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_keys_breakdown.py -v`
Expected: 2 passed right away (the function already exists from Task 9). If instead you see `AttributeError: module 'zira_dashboard.inbox_keys' has no attribute 'breakdown'`, Task 9 wasn't completed as planned — stop and check Task 9's Step 2b was actually applied before proceeding.

- [ ] **Step 3: (Skip — already done in Task 9)** No implementation step needed here; `inbox_keys.breakdown` already exists byte-for-byte as specified. Continue to Step 4.

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_keys_breakdown.py -v`
Expected: 2 passed

- [ ] **Step 5: Write the failing test for the snapshot section**

```python
"""The breakdown section appears in build_summary/build_snapshot."""
from zira_dashboard import exception_inbox


def test_build_summary_includes_breakdown_count(monkeypatch):
    from zira_dashboard import machine_breakdown
    monkeypatch.setattr(machine_breakdown, "current_rows", lambda: [
        {"name": "Dismantler 2", "action": None},
        {"name": "Juan", "action": {"type": "breakdown"}},
    ])
    summary = exception_inbox.build_summary()
    assert summary["sections"]["breakdown"] == 2


def test_build_snapshot_includes_breakdown_section_and_rows(monkeypatch):
    from zira_dashboard import machine_breakdown
    row = {
        "name": "Dismantler 2", "label": "Stopped producing", "detail": "No output since 1:02 PM (23 min)",
        "priority": "urgent", "badge": "AUTO-DETECTED",
        "row_key": "breakdown_header:Dismantler 2:x", "item_key": "breakdown:Dismantler 2:x",
        "action": None, "dismiss_action": {"type": "breakdown_dismiss", "incident_id": 1},
    }
    monkeypatch.setattr(machine_breakdown, "current_rows", lambda: [row])
    snapshot = exception_inbox.build_snapshot()
    section = next(s for s in snapshot["sections"] if s["id"] == "breakdown")
    assert section["rows"] == [row]
    assert section["count"] == 1
    queue_item_keys = [r["item_key"] for r in snapshot["queue"]]
    assert "breakdown:Dismantler 2:x" in queue_item_keys
```

- [ ] **Step 6: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exception_inbox_breakdown.py -v`
Expected: FAIL — `KeyError: 'breakdown'`

- [ ] **Step 7: Wire the section into `build_summary` and `build_snapshot`**

In `src/zira_dashboard/exception_inbox.py`, `build_summary()`:

```python
def build_summary() -> dict:
    from . import missing_wc, missed_punch_out, machine_breakdown
    from .routes import staffing as staffing_routes

    today = plant_day.today()
    source_errors: list[dict] = []
    assignments = _capture(
        source_errors, "Assignments To Do", staffing_routes.assignments_todo_payload, {}
    )
    late = _capture(source_errors, "Late / Absence", staffing_routes.late_report_payload, {})
    missing_rows = _capture(source_errors, "Missing Work Center", missing_wc.current_rows, [])
    missed_rows = _capture(source_errors, "Missed Punch Out", missed_punch_out.current_rows, [])
    breakdown_rows = _capture(source_errors, "Machine Breakdown", machine_breakdown.current_rows, [])
    schedule_count = _capture(
        source_errors, "Plant Schedule", lambda: _plant_schedule_reminder()[0], 0
    )
    pending_count, pending_urgent_count = _capture(
        source_errors, "Pending Time Off", lambda: _pending_time_off_counts(today), (0, 0)
    )

    assignment_count = int(assignments.get("count") or 0)
    late_count = int(late.get("count") or 0)
    missing_count = len(missing_rows)
    missed_count = len(missed_rows)
    breakdown_count = len(breakdown_rows)
    urgent_total = (
        len(late.get("scheduled_late") or [])
        + len(late.get("unscheduled_late") or [])
        + missing_count
        + missed_count
        + pending_urgent_count
        + sum(1 for r in breakdown_rows if r.get("priority") == "urgent")
    )
    total = (
        assignment_count
        + schedule_count
        + late_count
        + missing_count
        + missed_count
        + pending_count
        + breakdown_count
    )
    return {
        "today": today.isoformat(),
        "generated_at": plant_day.now().strftime("%-I:%M %p"),
        "total": total,
        "urgent_total": urgent_total,
        "follow_up_total": len(late.get("snoozed") or []),
        "source_errors": source_errors,
        "sections": {
            "assignments": assignment_count,
            "plant_schedule": schedule_count,
            "late": late_count,
            "missing_wc": missing_count,
            "missed_punch_out": missed_count,
            "time_off": pending_count,
            "breakdown": breakdown_count,
        },
    }
```

In `build_snapshot()`, add the import and section source pull alongside the others:

```python
def build_snapshot() -> dict:
    from . import missing_wc, missed_punch_out, machine_breakdown
    from .routes import staffing as staffing_routes

    today = plant_day.today()
    source_errors: list[dict] = []
    assignments = _capture(
        source_errors, "Assignments To Do", staffing_routes.assignments_todo_payload, {}
    )
    late = _capture(source_errors, "Late / Absence", staffing_routes.late_report_payload, {})
    missing_rows = _capture(source_errors, "Missing Work Center", missing_wc.current_rows, [])
    missed_rows = _capture(source_errors, "Missed Punch Out", missed_punch_out.current_rows, [])
    breakdown_rows = _capture(source_errors, "Machine Breakdown", machine_breakdown.current_rows, [])
    schedule_count, schedule_rows = _capture(
        source_errors, "Plant Schedule", _plant_schedule_reminder, (0, [])
    )
    pending_count, pending_rows = _capture(
        source_errors, "Pending Time Off", lambda: _pending_time_off(today), (0, [])
    )
    work_centers = _capture(source_errors, "Work Center List", _work_center_names, [])
```

(Leave the existing `for _payload, _label in (...)` degraded-check loop and the `late_rows` construction untouched.)

Add the new section to the `sections` list, right after the `missed_punch_out` section and before `time_off`:

```python
        {
            "id": "breakdown",
            "title": "Machine Breakdown",
            "count": len(breakdown_rows),
            "tone": "bad",
            "action_key": "breakdown",
            "action_label": "Handle",
            "empty": "All clear",
            "context": {"work_centers": work_centers},
            "rows": breakdown_rows,
        },
```

Finally, update the `urgent_total`/`follow_up_total`/`total` computations at the bottom of `build_snapshot` — they already sum generically over `section["count"]` and iterate `row.get("priority")` across every section's rows, so **no change is needed there**: adding the `breakdown` section to `sections` automatically folds its rows into `total`, `urgent_total` (rows with `priority == "urgent"`), and `follow_up_total` (rows with `priority == "muted"`, i.e. snoozed operator rows) via the existing generic loops.

- [ ] **Step 8: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exception_inbox_breakdown.py tests/test_inbox_keys_breakdown.py -v`
Expected: 4 passed

- [ ] **Step 9: Run the full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass — pay attention to any existing `test_exception_inbox.py` test asserting an exact `sections` list length/order, since a new section was inserted.

- [ ] **Step 10: Commit**

```bash
git add src/zira_dashboard/inbox_keys.py src/zira_dashboard/exception_inbox.py \
        tests/test_inbox_keys_breakdown.py tests/test_exception_inbox_breakdown.py
git commit -m "feat(breakdown): wire machine breakdown into the inbox snapshot/summary"
```

---

## Task 12: Reconcile registration

**Files:**
- Modify: `src/zira_dashboard/inbox_reconcile.py`
- Test: `tests/test_inbox_reconcile.py` (extend existing file)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_inbox_reconcile.py`:

```python
def test_run_once_auto_resolves_departed_breakdown_row(monkeypatch):
    from zira_dashboard import exception_inbox, inbox_log

    def _snap():
        return {"queue": [], "source_errors": [],
                "sections": [{"id": "breakdown", "count": 0, "rows": []}]}

    monkeypatch.setattr(exception_inbox, "build_snapshot", _snap)
    monkeypatch.setattr(inbox_reconcile, "_read_mirror", lambda: {
        "breakdown:Dismantler 2:x": _mirror_row(
            item_key="breakdown:Dismantler 2:x", item_kind="breakdown",
            category_label="Machine Breakdown"),
    })
    deleted, logged = [], []
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda k, i: None)
    monkeypatch.setattr(inbox_reconcile, "_delete", lambda k: deleted.append(k))
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda k, s: False)
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: logged.append(kw) or 1)

    inbox_reconcile.run_once()

    assert deleted == ["breakdown:Dismantler 2:x"]
    assert logged[0]["item_kind"] == "breakdown"
    assert logged[0]["action"] == "auto_resolved"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_reconcile.py -v`
Expected: FAIL — the departure isn't recognized as "complete" so it's never auto-resolved (`deleted == []`), because `"breakdown"` isn't in `_SECTION_KIND`/`_KIND_SOURCE` yet.

- [ ] **Step 3: Register the new section**

In `src/zira_dashboard/inbox_reconcile.py`, add to both dicts:

```python
_SECTION_KIND = {
    "assignments": "assignment",
    "plant_schedule": "plant_schedule",
    "late": "late",
    "missing_wc": "missing_wc",
    "missed_punch_out": "missed_punch_out",
    "time_off": "time_off",
    "breakdown": "breakdown",
}

_KIND_SOURCE = {
    "assignment": "Assignments To Do",
    "plant_schedule": "Plant Schedule",
    "late": "Late / Absence",
    "missing_wc": "Missing Work Center",
    "missed_punch_out": "Missed Punch Out",
    "time_off": "Pending Time Off",
    "breakdown": "Machine Breakdown",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_reconcile.py -v`
Expected: all pass (existing + 1 new)

- [ ] **Step 5: Run the full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/inbox_reconcile.py tests/test_inbox_reconcile.py
git commit -m "feat(breakdown): register breakdown section with the inbox reconciler"
```

---

## Task 13: Transfer / Snooze / Dismiss / Report endpoints

**Files:**
- Modify: `src/zira_dashboard/routes/exceptions.py`
- Test: `tests/test_exceptions_breakdown_routes.py`

Follows the existing `_approve_time_off_sync`/`approve_time_off_request` pattern: a thin `async def` route that awaits the JSON body + pulls the actor, then `await asyncio.to_thread(_sync_fn, ...)` where the sync function does the blocking work and logs the inbox event.

- [ ] **Step 1: Write the failing tests**

```python
"""POST /api/exceptions/breakdown/{transfer,snooze,dismiss,report}."""
from datetime import datetime, timezone

from zira_dashboard.routes import exceptions as exceptions_route

_STOP = datetime(2026, 7, 8, 18, 2, tzinfo=timezone.utc)


def test_transfer_sync_caps_exclusion_and_calls_decide_and_apply(monkeypatch):
    from zira_dashboard import machine_breakdown, wc_attributions, staffing_transfer, inbox_log
    monkeypatch.setattr(machine_breakdown, "get_incident", lambda iid: {
        "id": 1, "wc_name": "Dismantler 2", "day": "2026-07-08", "detected_stop_utc": _STOP,
    })
    monkeypatch.setattr(wc_attributions, "open_breakdown_row",
                        lambda day, wc, person: {"id": 10})
    capped = []
    monkeypatch.setattr(wc_attributions, "cap_breakdown", lambda rid, end: capped.append((rid, end)))
    monkeypatch.setattr(staffing_transfer, "decide_and_apply",
                        lambda person, wc, ts: {"transfer": "moved", "person": person,
                                                "closed_id": 5, "new_id": 6, "to_dept": "Recycled"})
    logged = []
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: logged.append(kw) or 42)

    resp = exceptions_route._breakdown_transfer_sync(
        {"incident_id": 1, "person_name": "Juan", "to_wc": "Repair 3"},
        actor_upn="dale@gruberpallets.com", actor_name="Dale",
    )

    assert resp.status_code == 200
    assert capped and capped[0][0] == 10
    assert logged[0]["item_kind"] == "breakdown"
    assert logged[0]["action"] == "transfer"
    assert logged[0]["reversible"] is True
    assert logged[0]["detail"]["closed_id"] == 5
    assert logged[0]["detail"]["new_id"] == 6
    assert logged[0]["detail"]["attribution_id"] == 10


def test_transfer_sync_404_when_incident_missing(monkeypatch):
    from zira_dashboard import machine_breakdown
    monkeypatch.setattr(machine_breakdown, "get_incident", lambda iid: None)
    resp = exceptions_route._breakdown_transfer_sync(
        {"incident_id": 1, "person_name": "Juan", "to_wc": "Repair 3"}, None, None)
    assert resp.status_code == 404


def test_snooze_sync_calls_snooze_operator(monkeypatch):
    from zira_dashboard import machine_breakdown
    called = []
    monkeypatch.setattr(machine_breakdown, "snooze_operator",
                        lambda iid, person: called.append((iid, person)))
    resp = exceptions_route._breakdown_snooze_sync({"incident_id": 1, "person_name": "Juan"})
    assert resp.status_code == 200
    assert called == [(1, "Juan")]


def test_dismiss_sync_deletes_rows_and_resolves(monkeypatch):
    from zira_dashboard import machine_breakdown, wc_attributions, inbox_log
    monkeypatch.setattr(machine_breakdown, "get_incident", lambda iid: {
        "id": 1, "wc_name": "Dismantler 2", "day": "2026-07-08", "detected_stop_utc": _STOP,
    })
    snapshot_rows = [{"id": 10, "day": "2026-07-08", "wc_name": "Dismantler 2",
                      "person_name": "Juan", "start_utc": "2026-07-08T18:02:00+00:00",
                      "end_utc": None, "source": "breakdown"}]
    monkeypatch.setattr(wc_attributions, "for_day", lambda day: snapshot_rows)
    deleted = []
    monkeypatch.setattr(wc_attributions, "delete_breakdown_rows_for_incident",
                        lambda iid: deleted.append(iid))
    resolved = []
    monkeypatch.setattr(machine_breakdown, "resolve_incident",
                        lambda iid, resolution, resume_utc=None: resolved.append((iid, resolution)))
    logged = []
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: logged.append(kw) or 43)

    resp = exceptions_route._breakdown_dismiss_sync({"incident_id": 1}, "dale@gruberpallets.com", "Dale")

    assert resp.status_code == 200
    assert deleted == [1]
    assert resolved == [(1, "dismissed")]
    assert logged[0]["action"] == "dismiss"
    assert logged[0]["reversible"] is True
    assert logged[0]["detail"]["rows"] == snapshot_rows


def test_report_sync_calls_report_manual(monkeypatch):
    from zira_dashboard import machine_breakdown
    monkeypatch.setattr(machine_breakdown, "report_manual",
                        lambda wc: {"ok": True, "incident_id": 9})
    resp = exceptions_route._breakdown_report_sync({"wc_name": "Dismantler 2"})
    assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exceptions_breakdown_routes.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_breakdown_transfer_sync'`

- [ ] **Step 3: Add the four endpoint handlers**

Add to `src/zira_dashboard/routes/exceptions.py`, after the `_undo_sync`/`undo_inbox_event` block at the end of the file:

```python
def _breakdown_transfer_sync(body: dict, actor_upn=None, actor_name=None) -> JSONResponse:
    """Blocking half of /api/exceptions/breakdown/transfer: caps the
    operator's breakdown exclusion at transfer time (precise, doesn't wait
    for the next detection tick), then runs the normal transfer chokepoint."""
    from .. import inbox_keys, inbox_log, machine_breakdown, staffing_transfer, wc_attributions

    incident_id = body.get("incident_id")
    person_name = str(body.get("person_name") or "").strip()
    to_wc = str(body.get("to_wc") or "").strip()
    if not incident_id or not person_name or not to_wc:
        return _json_error("incident_id, person_name, and to_wc are required", 400)

    incident = machine_breakdown.get_incident(incident_id)
    if incident is None:
        return _json_error("incident not found", 404)

    now = plant_day.now()
    row = wc_attributions.open_breakdown_row(incident["day"], incident["wc_name"], person_name)
    if row is not None:
        wc_attributions.cap_breakdown(row["id"], now)

    result = staffing_transfer.decide_and_apply(person_name, to_wc, now)

    eid = inbox_log.log_event_safe(
        item_kind="breakdown",
        item_key=inbox_keys.breakdown(incident["wc_name"], incident["detected_stop_utc"].isoformat(), person_name),
        person_name=person_name,
        category_label="Machine Breakdown",
        action="transfer",
        outcome=f"Transferred to {to_wc}",
        after_value=to_wc,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        reversible=True,
        detail={
            "closed_id": result.get("closed_id"),
            "new_id": result.get("new_id"),
            "attribution_id": row["id"] if row is not None else None,
        },
    )
    return JSONResponse({"ok": True, "event_id": eid, "transfer": result.get("transfer")})


@router.post("/api/exceptions/breakdown/transfer")
async def breakdown_transfer(request: Request):
    """Transfer an operator off a broken machine.

    Body (JSON): {incident_id, person_name, to_wc}
    """
    from .. import inbox_log
    body = await request.json()
    actor_upn, actor_name = inbox_log.actor_from(request)
    return await asyncio.to_thread(_breakdown_transfer_sync, body, actor_upn, actor_name)


def _breakdown_snooze_sync(body: dict) -> JSONResponse:
    """Blocking half of /api/exceptions/breakdown/snooze."""
    from .. import machine_breakdown

    incident_id = body.get("incident_id")
    person_name = str(body.get("person_name") or "").strip()
    if not incident_id or not person_name:
        return _json_error("incident_id and person_name are required", 400)
    machine_breakdown.snooze_operator(incident_id, person_name)
    return JSONResponse({"ok": True})


@router.post("/api/exceptions/breakdown/snooze")
async def breakdown_snooze(request: Request):
    """Silence one operator's row on a breakdown card for 15 minutes.

    Body (JSON): {incident_id, person_name}
    """
    body = await request.json()
    return await asyncio.to_thread(_breakdown_snooze_sync, body)


def _breakdown_dismiss_sync(body: dict, actor_upn=None, actor_name=None) -> JSONResponse:
    """Blocking half of /api/exceptions/breakdown/dismiss ("Not a
    breakdown"): snapshots the incident's exclusion rows into the undo
    detail BEFORE deleting them, then resolves the incident."""
    from .. import inbox_keys, inbox_log, machine_breakdown, wc_attributions

    incident_id = body.get("incident_id")
    if not incident_id:
        return _json_error("incident_id is required", 400)
    incident = machine_breakdown.get_incident(incident_id)
    if incident is None:
        return _json_error("incident not found", 404)

    # for_day()'s SELECT does not include `day` (it's the WHERE filter, not a
    # returned column) -- stamp it back on before storing, since undo needs
    # the full row shape to re-insert via wc_attributions.add().
    snapshot_rows = [
        {**r, "day": incident["day"]}
        for r in wc_attributions.for_day(incident["day"])
        if r.get("wc_name") == incident["wc_name"] and r.get("source") == wc_attributions.BREAKDOWN_SOURCE
    ]
    wc_attributions.delete_breakdown_rows_for_incident(incident_id)
    machine_breakdown.resolve_incident(incident_id, "dismissed")

    eid = inbox_log.log_event_safe(
        item_kind="breakdown",
        item_key=inbox_keys.breakdown(incident["wc_name"], incident["detected_stop_utc"].isoformat()),
        person_name=None,
        category_label="Machine Breakdown",
        action="dismiss",
        outcome="Not a breakdown",
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        reversible=True,
        detail={"rows": snapshot_rows, "incident_id": incident_id},
    )
    return JSONResponse({"ok": True, "event_id": eid})


@router.post("/api/exceptions/breakdown/dismiss")
async def breakdown_dismiss(request: Request):
    """"Not a breakdown": resolve the incident and delete its exclusion rows.

    Body (JSON): {incident_id}
    """
    from .. import inbox_log
    body = await request.json()
    actor_upn, actor_name = inbox_log.actor_from(request)
    return await asyncio.to_thread(_breakdown_dismiss_sync, body, actor_upn, actor_name)


def _breakdown_report_sync(body: dict) -> JSONResponse:
    """Blocking half of /api/exceptions/breakdown/report (the manual
    "+ Report a breakdown" button)."""
    from .. import machine_breakdown, staffing

    wc_name = str(body.get("wc_name") or "").strip()
    if wc_name not in {loc.name for loc in staffing.LOCATIONS}:
        return _json_error("unknown work center", 400)
    result = machine_breakdown.report_manual(wc_name)
    return JSONResponse(result)


@router.post("/api/exceptions/breakdown/report")
async def breakdown_report(request: Request):
    """Manually report a machine as broken down.

    Body (JSON): {wc_name}
    """
    body = await request.json()
    return await asyncio.to_thread(_breakdown_report_sync, body)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exceptions_breakdown_routes.py -v`
Expected: 5 passed

- [ ] **Step 5: Run the full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/exceptions.py tests/test_exceptions_breakdown_routes.py
git commit -m "feat(breakdown): transfer/snooze/dismiss/report endpoints"
```

---

## Task 14: Undo wiring

**Files:**
- Modify: `src/zira_dashboard/inbox_log.py`
- Modify: `src/zira_dashboard/routes/exceptions.py`
- Test: `tests/test_inbox_undo_endpoint.py` (extend existing file)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_inbox_undo_endpoint.py`:

```python
def test_get_event_includes_detail(monkeypatch):
    """get_event's SELECT must include `detail` for breakdown undo to work."""
    from zira_dashboard import db, inbox_log
    captured = {}

    def fake_query(sql, params):
        captured["sql"] = sql
        return [_ev(id=7)]

    monkeypatch.setattr(db, "query", fake_query)
    inbox_log.get_event(7)
    assert "detail" in captured["sql"]


def test_undo_breakdown_transfer_reverses_and_reopens_exclusion(monkeypatch):
    from zira_dashboard import odoo_client, wc_attributions
    calls = {}
    monkeypatch.setattr(inbox_log, "get_event", lambda eid: _ev(
        id=eid, item_kind="breakdown", item_key="breakdown:Dismantler 2:x:Juan", action="transfer",
        detail={"closed_id": 5, "new_id": 6, "attribution_id": 10}))
    monkeypatch.setattr(odoo_client, "undo_transfer",
                        lambda closed_id, new_id: calls.setdefault("undo_transfer", (closed_id, new_id)))
    monkeypatch.setattr(wc_attributions, "reopen_breakdown",
                        lambda rid: calls.setdefault("reopen", rid))
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: 99)
    monkeypatch.setattr(inbox_log, "mark_undone", lambda e, u: None)
    monkeypatch.setattr(exceptions_route, "_refresh_time_off_surfaces", lambda: None)

    resp = exceptions_route._undo_sync(7, None, None)

    assert resp.status_code == 200
    assert calls["undo_transfer"] == (5, 6)
    assert calls["reopen"] == 10


def test_undo_breakdown_dismiss_reopens_incident_and_recreates_rows(monkeypatch):
    from zira_dashboard import machine_breakdown, wc_attributions
    snapshot_rows = [{"day": "2026-07-08", "wc_name": "Dismantler 2", "person_name": "Juan",
                      "start_utc": "2026-07-08T18:02:00+00:00", "end_utc": None}]
    monkeypatch.setattr(inbox_log, "get_event", lambda eid: _ev(
        id=eid, item_kind="breakdown", item_key="breakdown:Dismantler 2:x", action="dismiss",
        detail={"rows": snapshot_rows, "incident_id": 1}))
    calls = {}
    monkeypatch.setattr(machine_breakdown, "reopen_incident",
                        lambda iid: calls.setdefault("reopen_incident", iid))
    added = []
    monkeypatch.setattr(wc_attributions, "add", lambda **kw: added.append(kw) or 1)
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: 99)
    monkeypatch.setattr(inbox_log, "mark_undone", lambda e, u: None)
    monkeypatch.setattr(exceptions_route, "_refresh_time_off_surfaces", lambda: None)

    resp = exceptions_route._undo_sync(7, None, None)

    assert resp.status_code == 200
    assert calls["reopen_incident"] == 1
    assert len(added) == 1
    assert added[0]["person_name"] == "Juan"
    assert added[0]["breakdown_id"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_undo_endpoint.py -v`
Expected: FAIL — `get_event` doesn't select `detail`, and `_reverse_event` has no `breakdown` branch.

- [ ] **Step 3: Add `detail` to `get_event`'s SELECT**

In `src/zira_dashboard/inbox_log.py`:

```python
def get_event(event_id: int) -> dict[str, Any] | None:
    """One event row by id, or None."""
    rows = db.query(
        "SELECT id, item_kind, item_key, person_name, category_label, action, "
        "outcome, before_value, after_value, reason, actor_upn, actor_name, "
        "source, reversible, undone_at, undo_event_id, resolved_at, detail "
        "FROM inbox_events WHERE id = %s",
        (event_id,),
    )
    return rows[0] if rows else None
```

Note: `detail` is a `jsonb` column written via `json.dumps(...)::jsonb` in `record_event`. Depending on the psycopg2 connection's registered type adapters, `db.query` may return it already deserialized (dict/list) or as a JSON string. Handle both defensively in Step 4 below (mirrors `missing_wc._read_cache`'s existing `isinstance(..., list)` defensive check for the same reason).

- [ ] **Step 4: Add `_UNDOABLE` entries and `_reverse_event` branches**

In `src/zira_dashboard/routes/exceptions.py`, extend `_UNDOABLE`:

```python
_UNDOABLE = {
    ("missing_wc", "assign"),
    ("missing_wc", "dismiss"),
    ("late", "absent"),
    ("late", "reason"),
    ("breakdown", "transfer"),
    ("breakdown", "dismiss"),
}
```

Add a small JSON-decoding helper and two branches to `_reverse_event`:

```python
def _event_detail(ev: dict[str, Any]) -> dict:
    """ev['detail'] is written as jsonb; normalize to a dict regardless of
    whether the driver returned it already-parsed or as a raw JSON string."""
    import json
    detail = ev.get("detail")
    if isinstance(detail, dict):
        return detail
    if isinstance(detail, str) and detail:
        try:
            return json.loads(detail)
        except (TypeError, ValueError):
            return {}
    return {}


def _reverse_event(ev: dict[str, Any]) -> None:
    """Reverse a resolved inbox action. Assumes (item_kind, action) is undoable."""
    from .. import absence_sync, late_report, machine_breakdown, missing_wc, odoo_client, wc_attributions

    kind, action, key = ev["item_kind"], ev["action"], ev["item_key"]
    if kind == "missing_wc":
        att_id = int(key.split(":")[1])
        if action == "assign":
            odoo_client.clear_attendance_wc(att_id)
        missing_wc.unresolve(att_id)
    elif kind == "late":
        _, emp_id, day = key.split(":", 2)
        if action == "absent":
            absence_sync.refuse_absence_leave(
                late_report.odoo_leave_id_for_absence(day, emp_id)
            )
            late_report.undo_absent(day, emp_id)
        elif action == "reason":
            late_report.undo_late_arrival(day, emp_id)
    elif kind == "breakdown":
        detail = _event_detail(ev)
        if action == "transfer":
            closed_id, new_id = detail.get("closed_id"), detail.get("new_id")
            if new_id is not None:
                odoo_client.undo_transfer(closed_id, new_id)
            attribution_id = detail.get("attribution_id")
            if attribution_id is not None:
                wc_attributions.reopen_breakdown(attribution_id)
        elif action == "dismiss":
            incident_id = detail.get("incident_id")
            machine_breakdown.reopen_incident(incident_id)
            for row in detail.get("rows") or []:
                wc_attributions.add(
                    day=row["day"], wc_name=row["wc_name"], person_name=row["person_name"],
                    start_utc=row["start_utc"], end_utc=row.get("end_utc"),
                    source=wc_attributions.BREAKDOWN_SOURCE, breakdown_id=incident_id,
                )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_inbox_undo_endpoint.py -v`
Expected: all pass (existing + 3 new)

- [ ] **Step 6: Run the full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/inbox_log.py src/zira_dashboard/routes/exceptions.py \
        tests/test_inbox_undo_endpoint.py
git commit -m "feat(breakdown): undo wiring for breakdown transfer and dismiss"
```

---

## Task 15: Template — breakdown card

**Files:**
- Modify: `src/zira_dashboard/templates/exceptions.html`
- Test: `tests/test_exception_inbox_breakdown_template.py`

- [ ] **Step 1: Write the failing test**

```python
"""Template rendering for the breakdown header + operator rows."""
from zira_dashboard.routes import exceptions as exceptions_route
from starlette.testclient import TestClient
from zira_dashboard.app import app


def _snapshot():
    header = {
        "name": "Dismantler 2", "label": "Stopped producing",
        "detail": "No output since 1:02 PM (23 min)",
        "priority": "urgent", "badge": "AUTO-DETECTED",
        "row_key": "breakdown_header:Dismantler 2:x", "item_key": "breakdown:Dismantler 2:x",
        "action": None, "dismiss_action": {"type": "breakdown_dismiss", "incident_id": 1},
    }
    operator = {
        "name": "Juan", "label": "Idle — Dismantler 2 is down", "detail": "",
        "priority": "urgent", "badge": "Needs decision",
        "row_key": "breakdown_op:Dismantler 2:x:Juan", "item_key": "breakdown:Dismantler 2:x:Juan",
        "action": {"type": "breakdown", "incident_id": 1, "person_name": "Juan", "wc_name": "Dismantler 2"},
    }
    return {
        "today": "2026-07-08", "generated_at": "1:22 PM", "total": 2, "urgent_total": 2,
        "follow_up_total": 0, "source_errors": [], "work_centers": ["Repair 3", "Dismantler 2"],
        "people": [], "sections": [],
        "queue": [
            {**header, "section_id": "breakdown", "category_label": "Machine Breakdown", "tone": "bad"},
            {**operator, "section_id": "breakdown", "category_label": "Machine Breakdown", "tone": "bad"},
        ],
    }


def test_breakdown_header_row_renders_dismiss_button(monkeypatch):
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", _snapshot)
    client = TestClient(app)
    resp = client.get("/exceptions")
    assert resp.status_code == 200
    assert 'data-action-type="breakdown_header"' in resp.text
    assert 'data-incident-id="1"' in resp.text
    assert "js-breakdown-dismiss" in resp.text
    assert "Not a breakdown" in resp.text


def test_breakdown_operator_row_renders_transfer_and_snooze(monkeypatch):
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", _snapshot)
    client = TestClient(app)
    resp = client.get("/exceptions")
    assert resp.status_code == 200
    assert 'data-action-type="breakdown"' in resp.text
    assert 'data-person-name="Juan"' in resp.text
    assert "js-breakdown-transfer" in resp.text
    assert "js-breakdown-snooze" in resp.text
    assert '<option value="Repair 3">Repair 3</option>' in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exception_inbox_breakdown_template.py -v`
Expected: FAIL — none of the `data-action-type="breakdown..."` strings exist yet.

- [ ] **Step 3: Add the `data-*` attribute branches**

In `src/zira_dashboard/templates/exceptions.html`, the row rendering assigns `data-action-type="{{ action.type if action else '' }}"` — but the breakdown HEADER row has `action: None` (it uses a separate `dismiss_action` key instead, since a header row isn't a single-target action the way every other row is). Introduce a small Jinja variable at the top of the row loop to pick the effective "action type" for `data-action-type`, so a header row still gets a distinguishable value:

Find this line near the top of the `{% for row in queue %}` block:

```html
      {% set action = row.get('action') %}
```

Change it to:

```html
      {% set action = row.get('action') %}
      {% set dismiss_action = row.get('dismiss_action') %}
      {% set row_action_type = action.type if action else (dismiss_action.type ~ '_header' if dismiss_action else '') %}
```

Wait — `dismiss_action.type` is `"breakdown_dismiss"`, so `row_action_type` would become `"breakdown_dismiss_header"`, which is clunky. Simplify by giving the header its own explicit type instead of deriving it:

```html
      {% set action = row.get('action') %}
      {% set dismiss_action = row.get('dismiss_action') %}
      {% set row_action_type = action.type if action else ('breakdown_header' if dismiss_action else '') %}
```

Now find the `data-action-type` attribute on `.exception-row` and change it to use `row_action_type`:

```html
      <div class="exception-row priority-{{ row.get('priority', 'normal') }}"
           data-section-id="{{ row.section_id }}"
           data-action-type="{{ row_action_type }}"
           data-priority="{{ row.get('priority', 'normal') }}"
           data-row-key="{{ row.get('row_key', '') }}"
           data-item-key="{{ row.get('item_key', '') }}"
           data-person-name="{{ row.name }}"
           {% if action and action.type == 'assignment' %}
             data-day="{{ action.day or '' }}"
             data-wc-name="{{ action.wc_name or '' }}"
             data-start-utc="{{ action.start_utc or '' }}"
             data-end-utc="{{ action.end_utc or '' }}"
           {% elif action and action.type in ('late_absence', 'late_reason') %}
             data-emp-id="{{ action.emp_id or '' }}"
           {% elif action and action.type in ('missing_wc', 'missed_punch_out') %}
             data-attendance-id="{{ action.attendance_id or '' }}"
           {% elif action and action.type == 'time_off' %}
             data-request-id="{{ action.request_id or '' }}"
           {% elif action and action.type == 'breakdown' %}
             data-incident-id="{{ action.incident_id }}"
             data-wc-name="{{ action.wc_name }}"
           {% elif dismiss_action and dismiss_action.type == 'breakdown_dismiss' %}
             data-incident-id="{{ dismiss_action.incident_id }}"
           {% endif %}
      >
```

- [ ] **Step 4: Add the `.row-actions` branches**

Find the `.row-actions` block's `{% elif action and action.type == 'time_off' %}` branch and add two new branches after it, before the final `{% elif row.get('href') %}`:

```html
          {% elif action and action.type == 'breakdown' %}
            <select class="inline-select js-wc" aria-label="Work center to transfer to">
              <option value="">Transfer to...</option>
              {% for wc in work_centers %}
                <option value="{{ wc }}">{{ wc }}</option>
              {% endfor %}
            </select>
            <button type="button" class="row-btn primary js-breakdown-transfer">Transfer</button>
            <button type="button" class="row-btn js-breakdown-snooze">Snooze 15m</button>
          {% elif dismiss_action and dismiss_action.type == 'breakdown_dismiss' %}
            <button type="button" class="row-btn js-breakdown-dismiss">Not a breakdown</button>
          {% elif row.get('href') %}
```

(Note: the original `{% elif row.get('href') %}` line already exists in the file — you're inserting the two new branches immediately before it, not duplicating it.)

- [ ] **Step 5: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exception_inbox_breakdown_template.py -v`
Expected: 2 passed

- [ ] **Step 6: Run the full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass — specifically re-run `tests/test_exception_inbox.py`'s existing template tests (e.g. `test_exceptions_page_renders_inline_action_controls`) since `data-action-type` is now driven by the new `row_action_type` variable instead of `action.type` directly for every row (verify it still equals `action.type` for every non-breakdown row, since `row_action_type` falls back to `action.type` when `action` is truthy).

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/templates/exceptions.html tests/test_exception_inbox_breakdown_template.py
git commit -m "feat(breakdown): render breakdown header + operator rows in the inbox template"
```

---

## Task 16: JS — breakdown card handlers

**Files:**
- Modify: `src/zira_dashboard/static/exceptions.js`
- Test: `tests/test_exception_inbox_breakdown_js.py`

- [ ] **Step 1: Write the failing test**

```python
"""String-membership tests against the JS source, mirroring the existing
test_exceptions_js_refreshes_shared_badges_after_inline_resolution style."""
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "zira_dashboard" / "static"


def test_exceptions_js_has_breakdown_transfer_handler():
    js = (STATIC_DIR / "exceptions.js").read_text(encoding="utf-8")
    assert "js-breakdown-transfer" in js
    assert "/api/exceptions/breakdown/transfer" in js


def test_exceptions_js_has_breakdown_snooze_handler():
    js = (STATIC_DIR / "exceptions.js").read_text(encoding="utf-8")
    assert "js-breakdown-snooze" in js
    assert "/api/exceptions/breakdown/snooze" in js


def test_exceptions_js_has_breakdown_dismiss_handler():
    js = (STATIC_DIR / "exceptions.js").read_text(encoding="utf-8")
    assert "js-breakdown-dismiss" in js
    assert "/api/exceptions/breakdown/dismiss" in js


def test_exceptions_js_refreshes_breakdown_badge():
    js = (STATIC_DIR / "exceptions.js").read_text(encoding="utf-8")
    assert "'breakdown'" in js
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exception_inbox_breakdown_js.py -v`
Expected: FAIL — none of these strings exist in `exceptions.js` yet.

- [ ] **Step 3: Add the three handlers to the delegated click listener**

In `src/zira_dashboard/static/exceptions.js`, inside the delegated click listener (the same block containing `js-missing-wc-save`/`js-missing-wc-dismiss`), add the following blocks. Each row's `data-incident-id`, `data-wc-name`, and `data-person-name` (already rendered by Task 15's template attributes; `data-person-name` is already rendered for every row via the pre-existing `data-person-name="{{ row.name }}"` attribute) are read the same way `attendanceId`/`personName` are read for missing_wc:

```javascript
    var incidentId = row.dataset.incidentId;
    var breakdownWc = row.dataset.wcName;

    if (rowBtn.classList.contains('js-breakdown-transfer')) {
      var toWc = row.querySelector('.js-wc').value;
      if (!incidentId || !toWc) {
        failRow(row, toWc ? 'Missing incident id.' : 'Pick a work center.');
        return;
      }
      setBusy(row, true);
      rowStatus(row, 'Transferring...', false);
      postJson('/api/exceptions/breakdown/transfer', {
        incident_id: incidentId,
        person_name: personName,
        to_wc: toWc,
      }).then(function (resp) {
        if (resp && resp.ok) resolveRow(row, 'Transferred', resp.event_id);
        else failRow(row, (resp && resp.error) || 'Transfer failed.');
      }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }

    if (rowBtn.classList.contains('js-breakdown-snooze')) {
      if (!incidentId || !personName) {
        failRow(row, 'Missing incident id.');
        return;
      }
      setBusy(row, true);
      rowStatus(row, 'Snoozing...', false);
      postJson('/api/exceptions/breakdown/snooze', {
        incident_id: incidentId,
        person_name: personName,
      }).then(function (resp) {
        if (resp && resp.ok) resolveRow(row, 'Snoozed');
        else failRow(row, (resp && resp.error) || 'Snooze failed.');
      }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }

    if (rowBtn.classList.contains('js-breakdown-dismiss')) {
      if (!incidentId) {
        failRow(row, 'Missing incident id.');
        return;
      }
      setBusy(row, true);
      rowStatus(row, 'Dismissing...', false);
      postJson('/api/exceptions/breakdown/dismiss', {
        incident_id: incidentId,
      }).then(function (resp) {
        if (resp && resp.ok) resolveRow(row, 'Not a breakdown', resp.event_id);
        else failRow(row, (resp && resp.error) || 'Dismiss failed.');
      }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }
```

Place these three blocks in the same position as the existing `js-missing-wc-save`/`js-missing-wc-dismiss` pair (right after them is fine), reusing the existing local variables `row`, `rowBtn`, `personName` already established earlier in the listener (do not redeclare `personName` — check how it's already read from `row.dataset.personName` near the top of the listener and reuse that binding).

- [ ] **Step 4: Add breakdown to `refreshSharedBadge`**

```javascript
  function refreshSharedBadge(row) {
    var actionType = row && row.dataset.actionType;
    var badgeKey = null;
    if (actionType === 'assignment') badgeKey = 'assignments';
    else if (actionType === 'late_absence' || actionType === 'late_reason') badgeKey = 'late';
    else if (actionType === 'missing_wc') badgeKey = 'missing_wc';
    else if (actionType === 'missed_punch_out') badgeKey = 'missed_punch_out';
    else if (actionType === 'breakdown' || actionType === 'breakdown_header') badgeKey = 'breakdown';
    if (!badgeKey) return;
    var api = window.gpiAlertBadges && window.gpiAlertBadges[badgeKey];
    if (api && typeof api.refreshCount === 'function') api.refreshCount();
  }
```

- [ ] **Step 5: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exception_inbox_breakdown_js.py -v`
Expected: 4 passed

- [ ] **Step 6: Run the full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/static/exceptions.js tests/test_exception_inbox_breakdown_js.py
git commit -m "feat(breakdown): JS handlers for transfer/snooze/dismiss"
```

---

## Task 17: "+ Report a breakdown" button

**Files:**
- Modify: `src/zira_dashboard/templates/exceptions.html`
- Modify: `src/zira_dashboard/static/exceptions.js`
- Modify: `src/zira_dashboard/routes/exceptions.py`
- Test: `tests/test_exceptions_report_breakdown_button.py`

- [ ] **Step 1: Write the failing tests**

```python
""""+ Report a breakdown" button: renders in the header, posts to the report endpoint."""
from starlette.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard.routes import exceptions as exceptions_route


def _snapshot():
    return {
        "today": "2026-07-08", "generated_at": "1:22 PM", "total": 0, "urgent_total": 0,
        "follow_up_total": 0, "source_errors": [], "work_centers": ["Dismantler 2", "Repair 3"],
        "people": [], "sections": [], "queue": [],
    }


def test_report_breakdown_button_renders_with_work_center_options(monkeypatch):
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", _snapshot)
    client = TestClient(app)
    resp = client.get("/exceptions")
    assert resp.status_code == 200
    assert "js-report-breakdown" in resp.text
    assert 'js-report-breakdown-wc' in resp.text
    assert '<option value="Dismantler 2">Dismantler 2</option>' in resp.text


def test_exceptions_js_has_report_breakdown_handler():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "src" / "zira_dashboard" / "static" / "exceptions.js").read_text(encoding="utf-8")
    assert "js-report-breakdown" in js
    assert "/api/exceptions/breakdown/report" in js
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exceptions_report_breakdown_button.py -v`
Expected: FAIL — none of these exist yet.

- [ ] **Step 3: Pass `work_centers` to the template (already present) and add the button**

`work_centers` is already passed to the template context by the `/exceptions` route (`routes/exceptions.py:exceptions_page`, existing `"work_centers": snapshot.get("work_centers") or []`) — no route change needed for this part.

In `src/zira_dashboard/templates/exceptions.html`, find the `.inbox-title` block:

```html
  <div class="inbox-title">
    <div>
      <h2>Exception Inbox</h2>
      <p>
        {{ snapshot.generated_at }} · <span data-total-open>{{ snapshot.total }}</span> open
        <span class="urgent-inline" data-urgent-wrap {% if not snapshot.urgent_total %}hidden{% endif %}>
          · <span data-urgent-open>{{ snapshot.urgent_total }}</span> urgent
        </span>
      </p>
    </div>
    <a class="refresh-btn" href="/exceptions">Refresh</a>
  </div>
```

Add the report-breakdown control between the text block and the Refresh link:

```html
  <div class="inbox-title">
    <div>
      <h2>Exception Inbox</h2>
      <p>
        {{ snapshot.generated_at }} · <span data-total-open>{{ snapshot.total }}</span> open
        <span class="urgent-inline" data-urgent-wrap {% if not snapshot.urgent_total %}hidden{% endif %}>
          · <span data-urgent-open>{{ snapshot.urgent_total }}</span> urgent
        </span>
      </p>
    </div>
    <div class="inbox-title-actions">
      <select class="inline-select js-report-breakdown-wc" aria-label="Machine to report">
        <option value="">Report a breakdown...</option>
        {% for wc in work_centers %}
          <option value="{{ wc }}">{{ wc }}</option>
        {% endfor %}
      </select>
      <button type="button" class="row-btn js-report-breakdown">+ Report a breakdown</button>
      <a class="refresh-btn" href="/exceptions">Refresh</a>
    </div>
  </div>
```

- [ ] **Step 4: Run the template test**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exceptions_report_breakdown_button.py::test_report_breakdown_button_renders_with_work_center_options -v`
Expected: 1 passed

- [ ] **Step 5: Add the JS handler**

In `src/zira_dashboard/static/exceptions.js`, add a standalone click listener near the bottom of the file (outside the per-row delegated listener, since this button isn't inside a `.exception-row`) — find where the file wires up other page-level (non-row) controls (e.g. the archive toggle or refresh-now button) and add alongside them:

```javascript
  var reportBreakdownBtn = document.querySelector('.js-report-breakdown');
  if (reportBreakdownBtn) {
    reportBreakdownBtn.addEventListener('click', function () {
      var select = document.querySelector('.js-report-breakdown-wc');
      var wcName = select ? select.value : '';
      if (!wcName) {
        select.focus();
        return;
      }
      reportBreakdownBtn.disabled = true;
      postJson('/api/exceptions/breakdown/report', {wc_name: wcName})
        .then(function (resp) {
          reportBreakdownBtn.disabled = false;
          if (resp && resp.ok) {
            window.location.reload();
          }
        })
        .catch(function () {
          reportBreakdownBtn.disabled = false;
        });
    });
  }
```

(If `postJson` is defined inside an IIFE scope not reachable from this location in the file, add this block inside the same IIFE, near the other page-level event wiring, rather than at true top level — check the file's existing structure for where non-row buttons like the archive toggle are wired and place this block in the same scope.)

- [ ] **Step 6: Run test to verify it passes**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_exceptions_report_breakdown_button.py -v`
Expected: 2 passed

- [ ] **Step 7: Run the full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add src/zira_dashboard/templates/exceptions.html src/zira_dashboard/static/exceptions.js \
        tests/test_exceptions_report_breakdown_button.py
git commit -m "feat(breakdown): add the manual + Report a breakdown control"
```

---

## Task 18: CSS polish, full suite, and manual smoke test

**Files:**
- Modify: `src/zira_dashboard/static/exceptions.css`

- [ ] **Step 1: Add CSS for the header/operator row grouping and the title-actions bar**

Append to `src/zira_dashboard/static/exceptions.css`:

```css
.inbox-title-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.exception-row[data-action-type="breakdown_header"] {
  border-left: 4px solid var(--tone-bad, #e05a4d);
  background: rgba(224, 90, 77, 0.06);
  font-weight: 600;
}

.exception-row[data-action-type="breakdown"] {
  border-left: 4px solid var(--tone-bad, #e05a4d);
  border-top: none;
  margin-top: -1px;
}

.exception-row[data-action-type="breakdown"] + .exception-row[data-action-type="breakdown_header"],
.exception-row[data-action-type="breakdown_header"] + .exception-row[data-action-type="breakdown"] {
  margin-top: 0;
}
```

(Match the existing file's CSS custom-property names and tone-color variables exactly — check the top of `exceptions.css` for how `tone-bad`/similar tokens are already defined elsewhere in the file, e.g. on `.category-tag.tone-bad` or `.priority-pill`, and reuse the SAME variable name/value rather than introducing a new `--tone-bad` fallback if an existing one is already defined. Read the file first before hardcoding `#e05a4d` if a variable already exists for it.)

- [ ] **Step 2: Run the full test suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: all pass, full count = pre-feature count + all new tests added across Tasks 1-17.

- [ ] **Step 3: Manual smoke test via the dev server**

Start the app (use whatever the repo's existing dev-run command is, e.g. `uvicorn zira_dashboard.app:app --reload` from the project root with `.venv` activated, or the project's documented `run` skill/script) and in a browser:

1. Navigate to `/exceptions`.
2. Confirm the "+ Report a breakdown" control renders in the header with a work-center dropdown populated from `staffing.LOCATIONS`.
3. Pick a work center with at least one operator currently clocked in (check `/staffing` or `/recycling` for who's on shift) and click "+ Report a breakdown".
4. Confirm a breakdown card appears: a header row ("`<WC>` — Stopped producing", MANUAL badge, "Not a breakdown" button) followed by one row per operator on that WC (Transfer dropdown + Transfer button + Snooze 15m button).
5. Click "Snooze 15m" on one operator — confirm their row changes to a muted "Snoozed — Re-checks in ~15 min" row with no action buttons, and the card does NOT disappear.
6. Pick a destination work center for the other operator and click "Transfer" — confirm a 5-second Undo affordance appears, then either let it finalize or click Undo and confirm the row reverts.
7. Reload `/exceptions` and confirm the "Machine Breakdown" section's count in the nav badge / focus-strip reflects the remaining open row(s).
8. Click "Not a breakdown" on the header (if the card still has any rows) — confirm the whole card resolves and disappears, with an Undo affordance.
9. Check `/staffing/leaderboards` for the transferred operator's WC average on today — confirm it is not being computed against the full shift's expected for the machine they left (a manual DB check of `production_daily.excluded_minutes > 0` for that person/day/wc is the precise way to confirm; the UI-level check is that their percentage isn't unexpectedly low for a day they were mostly idle on a broken machine).

Report any visual or functional issues found and fix them before considering the feature complete. Do not skip this step — none of the automated tests exercise the real Zira/Odoo/Postgres stack end-to-end through a browser.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/static/exceptions.css
git commit -m "style(breakdown): visually group the breakdown header and operator rows"
```

---

## Post-implementation

After Task 18, use the `superpowers:finishing-a-development-branch` skill to decide how to integrate this work (merge, PR, or further cleanup) — do not merge or push without the user's explicit go-ahead, per this repo's standing practice of asking before any destructive or shared-state action.
