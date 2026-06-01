# Occasional Saturday Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Saturdays a Settings-editable default schedule (6a–12p + two breaks) that auto-applies in the scheduler and drives punch rounding, while leaving weekdays and the published-gate untouched.

**Architecture:** A new singleton store (`saturday_schedule`) holds the Saturday default. `shift_config`'s per-day resolver gains one rung — Saturday default, between published per-day `custom_hours` and the weekday global schedule — exposed in two flavors: **gated** (`shift_start_for`, etc., for dashboards + the punch path; Saturday default applies only on a published/worked Saturday) and **configured** (`configured_shift_start_for`, etc., for the scheduler editor; the default shows even on a draft). The punch path is unchanged: `_shift_for_punch` already feeds `shift_start_for`/`shift_end_for` into `apply_rounding`.

**Tech Stack:** Python 3.11+ (FastAPI, psycopg/Postgres, Jinja2 templates), pytest. Source spec: [docs/superpowers/specs/2026-06-01-occasional-saturday-scheduling-design.md](../specs/2026-06-01-occasional-saturday-scheduling-design.md).

**Local verification note:** This repo's test suite targets a newer runtime than the local Python 3.9 and needs Postgres; locally, verify each `.py` with `python3 -m py_compile <file>`. The `pytest` "Run" steps below execute in CI / on Railway (where `DATABASE_URL` is set). Tests that fully stub their dependencies (Task 3, the `scheduler_hours_source` test) run anywhere.

---

### Task 1: `saturday_schedule` table

**Files:**
- Modify: `src/zira_dashboard/db.py` (the `_SCHEMA_DDL` string, right after the `global_schedule` block near line 400)
- Test: `tests/test_saturday_schedule_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_saturday_schedule_schema.py
"""The saturday_schedule singleton table is created by bootstrap_schema()."""
import os
import pytest
from zira_dashboard import db

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres"
)


def test_saturday_schedule_table_has_expected_columns():
    db.bootstrap_schema()
    rows = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'saturday_schedule'"
    )
    cols = {r["column_name"] for r in rows}
    assert {"id", "shift_start", "shift_end", "breaks", "updated_at"} <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_saturday_schedule_schema.py -v`
Expected: FAIL — table `saturday_schedule` does not exist (empty column set).

- [ ] **Step 3: Add the table to the schema DDL**

In `src/zira_dashboard/db.py`, find the `global_schedule` CREATE block inside `_SCHEMA_DDL` (ends with `);` near line 400) and insert immediately after it:

```sql
CREATE TABLE IF NOT EXISTS saturday_schedule (
  id              INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  shift_start     TIME NOT NULL,
  shift_end       TIME NOT NULL,
  breaks          JSONB NOT NULL DEFAULT '[]'::jsonb,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

(No seed `INSERT` — like `global_schedule`, the store returns its `DEFAULT` until the first save writes a row.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_saturday_schedule_schema.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/db.py tests/test_saturday_schedule_schema.py
git commit -m "feat(saturday): saturday_schedule singleton table"
```

---

### Task 2: `saturday_schedule_store`

**Files:**
- Create: `src/zira_dashboard/saturday_schedule_store.py`
- Test: `tests/test_saturday_schedule_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_saturday_schedule_store.py
"""saturday_schedule_store load/save/cache. Postgres-backed."""
import os
from datetime import time
import pytest
from zira_dashboard import db, saturday_schedule_store
from zira_dashboard.saturday_schedule_store import SaturdaySchedule, DEFAULT
from zira_dashboard.schedule_store import Break

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres"
)


@pytest.fixture(autouse=True)
def _reset():
    db.execute("DELETE FROM saturday_schedule WHERE id = 1")
    saturday_schedule_store.reload()
    yield
    db.execute("DELETE FROM saturday_schedule WHERE id = 1")
    saturday_schedule_store.reload()


def test_default_when_no_row():
    assert saturday_schedule_store.current() == DEFAULT
    assert DEFAULT.shift_start == time(6, 0)
    assert DEFAULT.shift_end == time(12, 0)
    assert DEFAULT.breaks == (
        Break(time(8, 0), time(8, 15), "Morning break"),
        Break(time(10, 0), time(10, 30), "Lunch"),
    )


def test_save_persists_and_invalidates_cache():
    s = SaturdaySchedule(time(6, 0), time(11, 0),
                         (Break(time(9, 0), time(9, 15), "Break"),))
    saturday_schedule_store.save(s)
    saturday_schedule_store.reload()
    assert saturday_schedule_store.current() == s


def test_save_reflected_in_current_without_reload():
    s = SaturdaySchedule(time(7, 0), time(13, 0), ())
    saturday_schedule_store.save(s)
    assert saturday_schedule_store.current() == s


def test_current_is_cached():
    saturday_schedule_store.current()  # prime cache as DEFAULT (no row)
    db.execute(
        "INSERT INTO saturday_schedule (id, shift_start, shift_end, breaks) "
        "VALUES (1, '05:00', '09:00', '[]'::jsonb) "
        "ON CONFLICT (id) DO UPDATE SET shift_start = EXCLUDED.shift_start"
    )
    assert saturday_schedule_store.current() == DEFAULT  # stale cache wins
    saturday_schedule_store.reload()
    assert saturday_schedule_store.current().shift_start == time(5, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_saturday_schedule_store.py -v`
Expected: FAIL — `ModuleNotFoundError: zira_dashboard.saturday_schedule_store`.

- [ ] **Step 3: Write the store**

```python
# src/zira_dashboard/saturday_schedule_store.py
"""Plant Saturday default schedule: shift hours + breaks for occasional
Saturdays. Persisted in the `saturday_schedule` table (singleton row id=1).

Mirrors schedule_store: an in-process cache of the singleton, invalidated
on save(), because shift_config's per-day resolver reads current() in hot
loops (per-sample, per-bucket) on Saturdays.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import time
from threading import RLock

from .schedule_store import Break, _parse_time, _format_time


@dataclass(frozen=True)
class SaturdaySchedule:
    shift_start: time
    shift_end: time
    breaks: tuple[Break, ...]


DEFAULT = SaturdaySchedule(
    shift_start=time(6, 0),
    shift_end=time(12, 0),
    breaks=(
        Break(time(8, 0), time(8, 15), "Morning break"),
        Break(time(10, 0), time(10, 30), "Lunch"),
    ),
)


def _row_to_schedule(row: dict) -> SaturdaySchedule:
    start = _parse_time(row.get("shift_start")) or DEFAULT.shift_start
    end = _parse_time(row.get("shift_end")) or DEFAULT.shift_end
    brks: list[Break] = []
    for b in (row.get("breaks") or []):
        if not isinstance(b, dict):
            continue
        bs = _parse_time(b.get("start"))
        be = _parse_time(b.get("end"))
        if not (bs and be) or be <= bs:
            continue
        name = str(b.get("name") or "Break")[:40]
        brks.append(Break(bs, be, name))
    brks.sort(key=lambda b: b.start)
    return SaturdaySchedule(start, end, tuple(brks))


_lock = RLock()
_cache: SaturdaySchedule | None = None


def _load_from_db() -> SaturdaySchedule:
    from . import db
    rows = db.query(
        "SELECT shift_start, shift_end, breaks FROM saturday_schedule WHERE id = 1"
    )
    if not rows:
        return DEFAULT
    return _row_to_schedule(rows[0])


def current() -> SaturdaySchedule:
    """Cached singleton; DEFAULT until the first save() writes a row."""
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load_from_db()
        return _cache


def save(sched: SaturdaySchedule) -> None:
    global _cache
    from . import db
    db.execute(
        "INSERT INTO saturday_schedule (id, shift_start, shift_end, breaks, updated_at) "
        "VALUES (1, %s, %s, %s::jsonb, now()) "
        "ON CONFLICT (id) DO UPDATE SET shift_start = EXCLUDED.shift_start, "
        "shift_end = EXCLUDED.shift_end, breaks = EXCLUDED.breaks, updated_at = now()",
        (
            sched.shift_start,
            sched.shift_end,
            json.dumps([
                {"start": _format_time(b.start), "end": _format_time(b.end), "name": b.name}
                for b in sched.breaks
            ]),
        ),
    )
    with _lock:
        _cache = sched


def reload() -> SaturdaySchedule:
    global _cache
    with _lock:
        _cache = _load_from_db()
        return _cache
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_saturday_schedule_store.py -v`
Expected: PASS (4 tests). Locally: `python3 -m py_compile src/zira_dashboard/saturday_schedule_store.py`.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/saturday_schedule_store.py tests/test_saturday_schedule_store.py
git commit -m "feat(saturday): saturday_schedule_store cached singleton"
```

---

### Task 3: `shift_config` Saturday-default resolver

**Files:**
- Modify: `src/zira_dashboard/shift_config.py` (replace `_published_custom_hours` at lines 63–75 and `shift_start_for`/`shift_end_for`/`breaks_for` at lines 103–151; leave `is_workday`, `productive_minutes_for`, `in_shift_on`, `shift_elapsed_minutes` unchanged — they call the gated `*_for` helpers and become Saturday-aware automatically)
- Test: `tests/test_shift_config_saturday.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shift_config_saturday.py
"""Saturday-default resolution in shift_config. Fully stubbed — no DB."""
from datetime import date, datetime, time
import pytest
from zira_dashboard import shift_config, staffing, schedule_store, saturday_schedule_store
from zira_dashboard.saturday_schedule_store import SaturdaySchedule
from zira_dashboard.schedule_store import Break
from zira_dashboard.shift_config import SITE_TZ

SAT = date(2026, 5, 16)   # Saturday (weekday 5)
TUE = date(2026, 5, 19)   # Tuesday (weekday 1)

SAT_DEFAULT = SaturdaySchedule(
    time(6, 0), time(12, 0),
    (Break(time(8, 0), time(8, 15), "Morning break"),
     Break(time(10, 0), time(10, 30), "Lunch")),
)
WEEKDAY = schedule_store.Schedule(
    time(7, 0), time(15, 30), frozenset({0, 1, 2, 3, 4}),
    (Break(time(9, 0), time(9, 15), "AM"), Break(time(11, 0), time(11, 30), "Lunch")),
)


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    # No DB: stub both stores and the work-week.
    monkeypatch.setattr(saturday_schedule_store, "current", lambda: SAT_DEFAULT)
    monkeypatch.setattr(schedule_store, "current", lambda: WEEKDAY)


def _load(published, custom=None):
    return lambda d: staffing.Schedule(
        day=d, published=published, assignments={}, custom_hours=custom
    )


def test_published_saturday_uses_saturday_default(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", _load(True))
    assert shift_config.shift_start_for(SAT) == time(6, 0)
    assert shift_config.shift_end_for(SAT) == time(12, 0)
    assert shift_config.breaks_for(SAT) == SAT_DEFAULT.breaks


def test_unpublished_saturday_gated_falls_back_to_weekday(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", _load(False))
    assert shift_config.shift_start_for(SAT) == time(7, 0)
    assert shift_config.shift_end_for(SAT) == time(15, 30)
    assert shift_config.breaks_for(SAT) == WEEKDAY.breaks


def test_configured_saturday_shows_default_even_on_draft(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", _load(False))
    assert shift_config.configured_shift_start_for(SAT) == time(6, 0)
    assert shift_config.configured_shift_end_for(SAT) == time(12, 0)
    assert shift_config.configured_breaks_for(SAT) == SAT_DEFAULT.breaks


def test_published_per_day_custom_overrides_saturday_default(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule",
        _load(True, {"start": "06:00", "end": "14:00", "breaks": []}))
    assert shift_config.shift_start_for(SAT) == time(6, 0)
    assert shift_config.shift_end_for(SAT) == time(14, 0)
    assert shift_config.breaks_for(SAT) == ()   # empty list = no breaks


def test_configured_draft_custom_wins_over_saturday_default(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule",
        _load(False, {"start": "06:00", "end": "13:00", "breaks": []}))
    assert shift_config.configured_shift_end_for(SAT) == time(13, 0)


def test_weekday_unchanged(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", _load(True))
    assert shift_config.shift_start_for(TUE) == time(7, 0)
    assert shift_config.shift_end_for(TUE) == time(15, 30)
    assert shift_config.breaks_for(TUE) == WEEKDAY.breaks


def test_productive_minutes_published_saturday(monkeypatch):
    # 06:00-12:00 = 360 min, minus 15 + 30 = 315.
    monkeypatch.setattr(staffing, "load_schedule", _load(True))
    assert shift_config.productive_minutes_for(SAT) == 315


def test_in_shift_on_published_saturday(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", _load(True))
    assert shift_config.in_shift_on(datetime(2026, 5, 16, 7, 0, tzinfo=SITE_TZ)) is True
    assert shift_config.in_shift_on(datetime(2026, 5, 16, 8, 5, tzinfo=SITE_TZ)) is False
    assert shift_config.in_shift_on(datetime(2026, 5, 16, 12, 30, tzinfo=SITE_TZ)) is False


def test_rounding_snaps_to_saturday_boundaries(monkeypatch):
    """The punch path feeds shift_start_for/shift_end_for into apply_rounding."""
    from zira_dashboard.rounding import apply_rounding, RoundingSettings
    monkeypatch.setattr(staffing, "load_schedule", _load(True))
    windows = RoundingSettings(15, 0, 0, 15)
    start, end = shift_config.shift_start_for(SAT), shift_config.shift_end_for(SAT)
    in_punch = datetime(2026, 5, 16, 5, 52, tzinfo=SITE_TZ)
    out_punch = datetime(2026, 5, 16, 12, 8, tzinfo=SITE_TZ)
    assert apply_rounding("clock_in", in_punch, start, end, windows).astimezone(SITE_TZ).time() == time(6, 0)
    assert apply_rounding("clock_out", out_punch, start, end, windows).astimezone(SITE_TZ).time() == time(12, 0)


def test_scheduler_hours_source():
    assert shift_config.scheduler_hours_source(SAT, False) == "saturday_default"
    assert shift_config.scheduler_hours_source(TUE, False) == "weekday_default"
    assert shift_config.scheduler_hours_source(SAT, True) == "custom"
    assert shift_config.scheduler_hours_source(TUE, True) == "custom"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_shift_config_saturday.py -v`
Expected: FAIL — `configured_shift_start_for` / `scheduler_hours_source` not defined; published Saturday returns weekday hours.

- [ ] **Step 3: Replace `_published_custom_hours` with the ungated `_custom_hours` + the Saturday-default helper**

In `src/zira_dashboard/shift_config.py`, replace the `_published_custom_hours` function (lines 63–75) with:

```python
SATURDAY = 5  # date.weekday(): Monday=0 .. Sunday=6


def _custom_hours(day: date, *, published_only: bool) -> dict | None:
    """The per-day custom_hours dict for `day`, or None.

    published_only=True (dashboards + punch path): only a PUBLISHED day's
    override applies — the long-standing rule that keeps drafts out of live
    metrics. published_only=False (the scheduler's own editor): the
    configured override applies whether or not it's published, so the Hours
    pill shows what will apply once published.

    Lazy import to avoid the shift_config -> staffing -> schedule_store cycle.
    """
    from . import staffing
    sched = staffing.load_schedule(day)
    if published_only and not getattr(sched, "published", False):
        return None
    ch = sched.custom_hours
    return ch if isinstance(ch, dict) else None


def _saturday_default():
    """The plant Saturday default schedule (cached singleton)."""
    from . import saturday_schedule_store
    return saturday_schedule_store.current()
```

- [ ] **Step 4: Replace the three `*_for` getters with the resolver + configured twins**

Replace `shift_start_for`, `shift_end_for`, and `breaks_for` (lines 103–151) with:

```python
def _use_saturday_default(day: date, *, published_only: bool) -> bool:
    """Whether `day` resolves from the Saturday default (vs the weekday
    global schedule), assuming no per-day override applies.

    Gated callers (dashboards, punch path) use it only when the Saturday is
    actually being worked — is_workday(day), which for a non-work weekday
    means a published schedule exists. So an unpublished Saturday behaves
    exactly as today (weekday global), staying inert. The scheduler's
    configured view always shows the Saturday default on a Saturday, so a
    fresh Saturday pre-fills 6a-12p before anything is published.
    """
    if day.weekday() != SATURDAY:
        return False
    if not published_only:
        return True
    return is_workday(day)


def _resolve_start(day: date, *, published_only: bool) -> time:
    ch = _custom_hours(day, published_only=published_only)
    if ch and isinstance(ch.get("start"), str):
        try:
            return time.fromisoformat(ch["start"])
        except ValueError:
            pass
    if _use_saturday_default(day, published_only=published_only):
        return _saturday_default().shift_start
    return shift_start()


def _resolve_end(day: date, *, published_only: bool) -> time:
    ch = _custom_hours(day, published_only=published_only)
    if ch and isinstance(ch.get("end"), str):
        try:
            return time.fromisoformat(ch["end"])
        except ValueError:
            pass
    if _use_saturday_default(day, published_only=published_only):
        return _saturday_default().shift_end
    return shift_end()


def _resolve_breaks(day: date, *, published_only: bool) -> tuple:
    from .schedule_store import Break
    ch = _custom_hours(day, published_only=published_only)
    if ch and isinstance(ch.get("breaks"), list):
        out = []
        for b in ch["breaks"]:
            if not isinstance(b, dict):
                continue
            try:
                bs = time.fromisoformat(b["start"])
                be = time.fromisoformat(b["end"])
            except (ValueError, KeyError, TypeError):
                continue
            name = str(b.get("name") or "Break")
            out.append(Break(bs, be, name))
        return tuple(out)
    if _use_saturday_default(day, published_only=published_only):
        return _saturday_default().breaks
    return breaks()


def shift_start_for(day: date) -> time:
    """Shift start for `day` (gated): published per-day custom_hours, else the
    Saturday default on a worked Saturday, else the weekday global schedule."""
    return _resolve_start(day, published_only=True)


def shift_end_for(day: date) -> time:
    return _resolve_end(day, published_only=True)


def breaks_for(day: date) -> tuple:
    """Breaks for `day` (gated). A per-day custom_hours `breaks` list — even
    empty (= 'no breaks today') — wins; otherwise the Saturday default on a
    worked Saturday, else the weekday global breaks."""
    return _resolve_breaks(day, published_only=True)


def configured_shift_start_for(day: date) -> time:
    """Ungated twin for the scheduler editor: a per-day override applies even
    on a draft; a Saturday with no override shows the Saturday default."""
    return _resolve_start(day, published_only=False)


def configured_shift_end_for(day: date) -> time:
    return _resolve_end(day, published_only=False)


def configured_breaks_for(day: date) -> tuple:
    return _resolve_breaks(day, published_only=False)


def scheduler_hours_source(day: date, has_per_day_override: bool) -> str:
    """Which hours the scheduler is showing for `day`: 'custom' (a per-day
    override exists), 'saturday_default' (a Saturday with no override), or
    'weekday_default'. Drives the Hours-pill styling + banner."""
    if has_per_day_override:
        return "custom"
    if day.weekday() == SATURDAY:
        return "saturday_default"
    return "weekday_default"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_shift_config_saturday.py -v`
Expected: PASS (10 tests). Locally: `python3 -m py_compile src/zira_dashboard/shift_config.py`.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/shift_config.py tests/test_shift_config_saturday.py
git commit -m "feat(saturday): shift_config Saturday-default resolver (gated + configured)"
```

---

### Task 4: Settings route — Saturday Default context + save

**Files:**
- Modify: `src/zira_dashboard/routes/settings.py` (add `saturday_schedule` to the GET context dict near line 360; add `POST /settings/saturday_schedule` after `settings_save_schedule`, near line 409)
- Test: `tests/test_settings_saturday_schedule.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_settings_saturday_schedule.py
"""Settings route for the Saturday default schedule. Postgres-backed."""
import os
from datetime import time
import pytest
from fastapi.testclient import TestClient
from zira_dashboard.app import app
from zira_dashboard import db, saturday_schedule_store

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres"
)
client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean():
    db.execute("DELETE FROM saturday_schedule WHERE id = 1")
    saturday_schedule_store.reload()
    yield
    db.execute("DELETE FROM saturday_schedule WHERE id = 1")
    saturday_schedule_store.reload()


def test_get_settings_renders_saturday_panel():
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Saturday Default" in r.text


def test_post_saves_saturday_schedule():
    r = client.post("/settings/saturday_schedule", data={
        "shift_start": "06:00", "shift_end": "12:00",
        "break_start_0": "08:00", "break_end_0": "08:15", "break_name_0": "Morning break",
        "break_start_1": "10:00", "break_end_1": "10:30", "break_name_1": "Lunch",
    }, headers={"accept": "application/json"})
    assert r.status_code == 200
    saturday_schedule_store.reload()
    s = saturday_schedule_store.current()
    assert s.shift_start == time(6, 0)
    assert s.shift_end == time(12, 0)
    assert len(s.breaks) == 2


def test_post_end_before_start_keeps_previous_end():
    client.post("/settings/saturday_schedule",
                data={"shift_start": "06:00", "shift_end": "12:00"},
                headers={"accept": "application/json"})
    saturday_schedule_store.reload()
    client.post("/settings/saturday_schedule",
                data={"shift_start": "09:00", "shift_end": "08:00"},
                headers={"accept": "application/json"})
    saturday_schedule_store.reload()
    s = saturday_schedule_store.current()
    assert s.shift_end > s.shift_start  # fell back, did not persist end<=start
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_settings_saturday_schedule.py -v`
Expected: FAIL — "Saturday Default" not in page; `POST /settings/saturday_schedule` 404.

- [ ] **Step 3: Add the GET context**

In `src/zira_dashboard/routes/settings.py`, just before the `return templates.TemplateResponse(...)` in the GET `/settings` handler (near line 333), add:

```python
    from .. import saturday_schedule_store
    _sat = saturday_schedule_store.current()
    saturday_schedule_ctx = {
        "shift_start": f"{_sat.shift_start.hour:02d}:{_sat.shift_start.minute:02d}",
        "shift_end":   f"{_sat.shift_end.hour:02d}:{_sat.shift_end.minute:02d}",
        "breaks": [
            {
                "start": f"{b.start.hour:02d}:{b.start.minute:02d}",
                "end":   f"{b.end.hour:02d}:{b.end.minute:02d}",
                "name": b.name,
            }
            for b in _sat.breaks
        ],
    }
```

Then add this entry to the context dict passed to `TemplateResponse` (alongside `"schedule": schedule_ctx,` near line 349):

```python
            "saturday_schedule": saturday_schedule_ctx,
```

- [ ] **Step 4: Add the POST handler**

After `settings_save_schedule` (which ends near line 409, before `@router.post("/settings/rounding")`), add:

```python
@router.post("/settings/saturday_schedule")
async def settings_save_saturday_schedule(request: Request):
    """Save the plant Saturday default (shift bookends + breaks). Mirrors
    settings_save_schedule: unparseable / end<=start values fall back to the
    current value rather than rejecting the submission."""
    from .. import saturday_schedule_store
    form = await request.form()
    current = saturday_schedule_store.current()
    shift_s = _parse_hhmm(form.get("shift_start")) or current.shift_start
    shift_e = _parse_hhmm(form.get("shift_end")) or current.shift_end
    if shift_e <= shift_s:
        shift_e = current.shift_end
    breaks_new: list[schedule_store.Break] = []
    idx = 0
    while idx <= 50:
        bs = _parse_hhmm(form.get(f"break_start_{idx}"))
        be = _parse_hhmm(form.get(f"break_end_{idx}"))
        bn = (form.get(f"break_name_{idx}") or "").strip() or "Break"
        if bs and be and be > bs:
            breaks_new.append(schedule_store.Break(bs, be, bn[:40]))
        idx += 1
    breaks_new.sort(key=lambda b: b.start)
    saturday_schedule_store.save(saturday_schedule_store.SaturdaySchedule(
        shift_start=shift_s,
        shift_end=shift_e,
        breaks=tuple(breaks_new),
    ))
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/settings?saved=1&section=timeclock", status_code=303)
```

(`schedule_store`, `JSONResponse`, and `RedirectResponse` are already imported in this module.)

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_settings_saturday_schedule.py -v`
Expected: PASS (3 tests). Locally: `python3 -m py_compile src/zira_dashboard/routes/settings.py`.

> Note: `test_get_settings_renders_saturday_panel` needs the template from Task 5 to pass. If running tasks strictly in order, expect this one test to fail until Task 5 lands; the two POST tests pass now. (Subagent-driven execution: note this cross-task dependency in the Task 4 handoff.)

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/settings.py tests/test_settings_saturday_schedule.py
git commit -m "feat(saturday): settings GET context + POST /settings/saturday_schedule"
```

---

### Task 5: Settings template — Saturday Default editor

**Files:**
- Modify: `src/zira_dashboard/templates/settings.html` (insert the form after the Company Schedule form, which closes with `</form>` at line 336; add a parallel add-break IIFE after the existing one near line 733; register the autosaver near line 891)

- [ ] **Step 1: Add the Saturday Default form**

Immediately after the Company Schedule form's closing `</form>` (line 336) and before the `<form ... action="/settings/rounding" ...>` (line 338), insert:

```html
    <form method="post" action="/settings/saturday_schedule" id="saturday-schedule-form"
          data-section="saturday_schedule">
      <h3 style="margin-top:1.6rem">Saturday Default</h3>
      <p class="note">Applied to Saturdays in the scheduler. Any individual
        Saturday can still be customized from its Hours pill. Punches on a
        published Saturday round to these hours.</p>
      <div class="sched-grid">
        <label for="sat_shift_start">Shift start</label>
        <input id="sat_shift_start" type="time" name="shift_start" value="{{ saturday_schedule.shift_start }}" step="300" required>

        <label for="sat_shift_end">Shift end</label>
        <input id="sat_shift_end" type="time" name="shift_end" value="{{ saturday_schedule.shift_end }}" step="300" required>

        <label>Breaks &amp; lunch</label>
        <div id="sat-breaks-list">
          {% for b in saturday_schedule.breaks %}
            <div class="break-row">
              <input type="time" name="break_start_{{ loop.index0 }}" value="{{ b.start }}" step="300" required>
              <input type="time" name="break_end_{{ loop.index0 }}" value="{{ b.end }}" step="300" required>
              <input type="text" name="break_name_{{ loop.index0 }}" value="{{ b.name }}" placeholder="name (e.g. Lunch)">
              <button type="button" class="remove-btn" title="Remove">×</button>
            </div>
          {% endfor %}
        </div>
        <button type="button" class="add-break-btn" id="sat-add-break">+ Add break</button>
      </div>
    </form>
```

- [ ] **Step 2: Add the add-break handler for the Saturday list**

After the existing break-row IIFE (the `(function () { ... })();` block scoped to `breaks-list`, ending at line 733), add a parallel block:

```html
  (function () {
    const list = document.getElementById('sat-breaks-list');
    if (!list) return;
    function reindex() {
      [...list.querySelectorAll('.break-row')].forEach((row, i) => {
        row.querySelectorAll('input').forEach(inp => {
          inp.name = inp.name.replace(/_\d+$/, '_' + i);
        });
      });
    }
    list.addEventListener('click', e => {
      if (e.target.classList.contains('remove-btn')) {
        e.target.closest('.break-row').remove();
        reindex();
      }
    });
    document.getElementById('sat-add-break').addEventListener('click', () => {
      const i = list.querySelectorAll('.break-row').length;
      const row = document.createElement('div');
      row.className = 'break-row';
      row.innerHTML =
        '<input type="time" name="break_start_' + i + '" step="300" required>'
      + '<input type="time" name="break_end_'   + i + '" step="300" required>'
      + '<input type="text" name="break_name_'  + i + '" placeholder="name (e.g. Lunch)">'
      + '<button type="button" class="remove-btn" title="Remove">×</button>';
      list.appendChild(row);
    });
  })();
```

- [ ] **Step 3: Register the autosaver**

After `attachAutosaver(document.getElementById('schedule-form'), '/settings/schedule');` (line 891), add:

```html
  attachAutosaver(document.getElementById('saturday-schedule-form'), '/settings/saturday_schedule');
```

- [ ] **Step 4: Verify**

Run: `pytest tests/test_settings_saturday_schedule.py -v`
Expected: PASS (all 3, including `test_get_settings_renders_saturday_panel`).
Manual: start the app, open Settings → the Company Schedule area shows a "Saturday Default" editor pre-filled 06:00–12:00 with two break rows; editing a field autosaves (top-center toast); reload shows the saved values.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/settings.html
git commit -m "feat(saturday): Saturday Default editor in Settings"
```

---

### Task 6: Scheduler — configured hours + Hours-pill Saturday state

**Files:**
- Modify: `src/zira_dashboard/routes/staffing.py` (the eff-hours block at lines 610–619; context dict near line 662)
- Modify: `src/zira_dashboard/templates/staffing.html` (lines 23, 135, 139, 315 — replace `has_custom_hours` with `hours_source`; add a `.hours-pill.saturday-default` style)

- [ ] **Step 1: Switch the route to the configured resolver + add `hours_source`**

In `src/zira_dashboard/routes/staffing.py`, replace the eff-hours block (lines 610–619):

```python
    eff_start = shift_config.shift_start_for(d)
    eff_end   = shift_config.shift_end_for(d)
    eff_breaks = [
        {"start": b.start.strftime("%H:%M"),
         "end":   b.end.strftime("%H:%M"),
         "name":  b.name}
        for b in shift_config.breaks_for(d)
    ]
    has_custom_hours = sched.custom_hours is not None
    eff_hours_label = f"{eff_start.strftime('%H:%M')}–{eff_end.strftime('%H:%M')}"
```

with:

```python
    eff_start = shift_config.configured_shift_start_for(d)
    eff_end   = shift_config.configured_shift_end_for(d)
    eff_breaks = [
        {"start": b.start.strftime("%H:%M"),
         "end":   b.end.strftime("%H:%M"),
         "name":  b.name}
        for b in shift_config.configured_breaks_for(d)
    ]
    hours_source = shift_config.scheduler_hours_source(d, sched.custom_hours is not None)
    eff_hours_label = f"{eff_start.strftime('%H:%M')}–{eff_end.strftime('%H:%M')}"
```

- [ ] **Step 2: Update the template context key**

In the same file, in the `TemplateResponse` context dict, replace the `"has_custom_hours": has_custom_hours,` entry (line 662) with:

```python
                "hours_source": hours_source,
```

- [ ] **Step 3: Update the four `has_custom_hours` template uses**

In `src/zira_dashboard/templates/staffing.html`:

Line 23 →
```html
  {% if hours_source != 'weekday_default' %}<div class="hours">{% if hours_source == 'saturday_default' %}Saturday Hours{% else %}Custom Hours{% endif %}: {{ eff_hours_label }}</div>{% endif %}
```

Line 135 (pill class) →
```html
      <button type="button" class="hours-pill {% if hours_source == 'custom' %}custom{% elif hours_source == 'saturday_default' %}saturday-default{% endif %}" id="hours-pill"
```

Line 139 (breaks count) →
```html
        {% if hours_source != 'weekday_default' %}<span>· {{ eff_breaks|length }} break{{ eff_breaks|length != 1 and 's' or '' }}</span>{% endif %}{% if hours_source == 'saturday_default' %}<span>· Saturday default</span>{% endif %}
```

Lines 315–322 (banner) →
```html
      {% if hours_source != 'weekday_default' %}
      <div class="custom-hours-banner">
        <b>{% if hours_source == 'saturday_default' %}Saturday hours:{% else %}Custom hours today:{% endif %}</b> {{ eff_hours_start }}–{{ eff_hours_end }}
        {% if eff_breaks %}· {{ eff_breaks|length }} break{{ eff_breaks|length != 1 and 's' or '' }}:
        {% for b in eff_breaks %}{{ b.name }} ({{ b.start }}–{{ b.end }}){% if not loop.last %}, {% endif %}{% endfor %}
        {% endif %}
      </div>
      {% endif %}
```

- [ ] **Step 4: Add the Saturday-default pill style**

Find the `.hours-pill.custom { ... }` rule in the `<style>` block of `staffing.html` (search: `grep -n "hours-pill.custom" src/zira_dashboard/templates/staffing.html`) and add immediately after it:

```css
    .hours-pill.saturday-default { border-color: #3b82f6; color: #3b82f6; }
```

- [ ] **Step 5: Verify**

Run: `pytest tests/test_shift_config_saturday.py::test_scheduler_hours_source -v`
Expected: PASS (already covered in Task 3). Locally: `python3 -m py_compile src/zira_dashboard/routes/staffing.py`.
Manual: in the app, open the scheduler on a Saturday with no edits → the Hours pill reads `Hours 06:00–12:00 · 2 breaks · Saturday default` (blue), and the editor pre-fills 6a–12p + the two breaks. Open a weekday → pill unchanged (plain, weekday hours). Set a custom Saturday via the pill → it reads `custom`.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/staffing.py src/zira_dashboard/templates/staffing.html
git commit -m "feat(saturday): scheduler shows Saturday-default hours + pill state"
```

---

## End-to-end verification (after all tasks)

1. **Settings:** Settings shows "Saturday Default" pre-filled 06:00–12:00 with the two breaks; edit + autosave + reload persists.
2. **Scheduler:** open an upcoming Saturday → Hours pill shows `06:00–12:00 · 2 breaks · Saturday default`; assign people; publish.
3. **Punch (published Saturday):** with plant rounding windows set, a `clock_in` just before 6:00 records 6:00 and a `clock_out` just after 12:00 records 12:00 (verify via `timeclock_punches_log.rounded_at`); a weekday punch is unchanged.
4. **Dashboards:** the published Saturday shows productive minutes = 6h − breaks (not zero, not a full weekday).
5. **Customized Saturday:** set the Hours pill to 6a–2p, publish → punches round to 6:00/14:00; dashboards use 6a–2p.
6. **Run the full suite in CI / on Railway:** `pytest -q`.

## Self-Review

**Spec coverage:**
- Settings-editable Saturday default → Tasks 1, 2, 4, 5. ✅
- Resolution layer (gated vs configured) → Task 3. ✅
- Saturday-only scope (`weekday == 5`) → Task 3 (`SATURDAY`), Task 3/6 (`scheduler_hours_source`). ✅
- Rounding reuses plant windows, snaps to Saturday boundaries → Task 3 `test_rounding_snaps_to_saturday_boundaries`; no punch-path code change (relies on existing `_shift_for_punch`). ✅
- Scheduler shows configured hours + third pill state → Task 6. ✅
- Unpublished Saturday inert for dashboards/punch; scheduler still shows default → Task 3 `test_unpublished_saturday_gated_falls_back_to_weekday` + `test_configured_saturday_shows_default_even_on_draft`. ✅
- Per-day override beats Saturday default → Task 3 `test_published_per_day_custom_overrides_saturday_default`. ✅
- Weekdays unchanged → Task 3 `test_weekday_unchanged`. ✅
- Override-employee precedence (assumption) → no code; unchanged `_shift_for_punch`. ✅

**Placeholder scan:** none — every step has concrete code/commands.

**Type consistency:** `SaturdaySchedule(shift_start, shift_end, breaks)` used identically in Task 2 (definition), Task 3 (`_saturday_default()` returns it), and Task 4 (`saturday_schedule_store.SaturdaySchedule(...)`). `scheduler_hours_source(day, has_per_day_override) -> str` defined in Task 3, called in Task 6 with the same signature; the three return values (`custom` / `saturday_default` / `weekday_default`) match the template branches in Task 6. `configured_shift_start_for` / `_end_for` / `_breaks_for` defined in Task 3, used in Task 6. Context key renamed `has_custom_hours` → `hours_source` in both the route (Task 6 Step 2) and all four template uses (Task 6 Step 3).

**Out-of-plan note:** `tests/test_shift_config_for.py` (lines 17–136) asserts pre-published-gate behavior and is skipped without `DATABASE_URL`; it is intentionally **not** modified here. Flag separately for cleanup.
