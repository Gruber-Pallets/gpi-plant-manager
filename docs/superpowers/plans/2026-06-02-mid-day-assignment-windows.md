# Mid-Day Assignment Windows & Goal Proration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a mid-day work-center assignment stay *open* (no end time at creation) and make the dashboard goal/expected accrue from the assignment's start time until the person clocks out, transfers, or is reassigned — so stations like Dismantler 4 show a real goal instead of a blank denominator.

**Architecture:** Introduce a pure `assignment_windows` module that merges three sources of "who worked where, when" into closed `[start, end]` **work segments**: (1) the published schedule, (2) kiosk punch windows from `timeclock_punches_log`, and (3) open-ended retro WC attributions. **Hybrid precedence: a person's kiosk punches win over their schedule and attributions.** Open windows are closed at the start of that person's *next* segment (transfer / reassignment) or at the shift cap (`min(now, shift_end)`). The recycling route computes per-WC `expected` by prorating each segment with the existing `staffing.effective_minutes_worked` primitive (which already subtracts breaks + partial time-off), replacing today's `scheduled_headcount × shift-wide elapsed_hours` formula. Attribution rows become open-ended (`end_utc` nullable).

**Tech Stack:** Python 3.11+, FastAPI, Postgres (psycopg via `db.query`/`db.execute`), Jinja2 templates, pytest. Pure-logic modules tested without DB/network (matches `progress.py` / `wc_attributions.py` conventions).

---

## Background — why Dismantler 4 had no goal (root cause)

The per-station bar goal is `b.expected`, hidden by `{% if b.expected %}` ([new_dept.html:90](../../../src/zira_dashboard/templates/new_dept.html), [recycling.html](../../../src/zira_dashboard/templates/recycling.html)). The recycling route computes:

```
per_wc_expected[wc] = station_target_per_hour(wc) × people_by_wc[wc] × elapsed_hours_d
```

at [departments.py:266](../../../src/zira_dashboard/routes/departments.py). `people_by_wc` ([departments.py:168](../../../src/zira_dashboard/routes/departments.py)) is built **only** from `sched.assignments` (the published schedule). Eulogio's mid-day assignment to Dismantler 4 was a retro WC attribution (`wc_time_attributions`), not a schedule entry — so:

- The **name** path (`_who_by_wc`, [departments.py:42](../../../src/zira_dashboard/routes/departments.py)) merges attributions → "Eulogio Mendez" shows.
- The **goal head-count** path (`people_by_wc`) ignores attributions → `expected = 0` → denominator hidden.

Two defects follow: (1) attributions are created with a frozen `end_utc` pinned to the last meter sample ([staffing.py:897](../../../src/zira_dashboard/routes/staffing.py)); (2) even if counted, proration uses shift-start→now, not assignment-start→now.

## File structure

- **Create** `src/zira_dashboard/assignment_windows.py` — pure segment resolver + `expected_by_wc` + `who_by_wc`. No DB/network.
- **Create** `src/zira_dashboard/timeclock_windows.py` — derive per-person punch windows from `timeclock_punches_log` (DB read + a pure `_segments_from_rows` transform).
- **Modify** `src/zira_dashboard/db.py` — make `wc_time_attributions.end_utc` nullable (idempotent `ALTER`).
- **Modify** `src/zira_dashboard/wc_attributions.py` — `add()` accepts open end (`end_utc=None`).
- **Modify** `src/zira_dashboard/routes/staffing.py` — `/api/staffing/attribute` accepts a missing/empty `end_utc` (open).
- **Modify** `src/zira_dashboard/templates/{new_dept,recycling,_footer}.html` — assign popover sends start only (open-ended).
- **Modify** `src/zira_dashboard/routes/departments.py` — `_recycling_day_data` derives `who_by_wc` + `per_wc_expected` from segments.
- **Create** `tests/test_assignment_windows.py`, `tests/test_timeclock_windows.py`, and extend `tests/test_staffing_attribute.py` (create if absent).

---

## Task 1: Schema — make attribution `end_utc` nullable

**Files:**
- Modify: `src/zira_dashboard/db.py:305-314` (the `wc_time_attributions` DDL block)

- [ ] **Step 1: Change the column DDL and add an idempotent migration**

In `db.py`, change the `CREATE TABLE` so `end_utc` is nullable, and add an `ALTER` immediately after the table+index block so existing databases (where the column is `NOT NULL`) are migrated on boot. Postgres `DROP NOT NULL` is a no-op if already nullable.

```sql
CREATE TABLE IF NOT EXISTS wc_time_attributions (
  id              BIGSERIAL PRIMARY KEY,
  day             DATE NOT NULL,
  wc_name         TEXT NOT NULL,
  person_name     TEXT NOT NULL,
  start_utc       TIMESTAMPTZ NOT NULL,
  end_utc         TIMESTAMPTZ,            -- NULL = open assignment (still running)
  source          TEXT NOT NULL DEFAULT 'manual',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS wc_time_attributions_day_idx ON wc_time_attributions(day);
CREATE INDEX IF NOT EXISTS wc_time_attributions_day_wc_idx ON wc_time_attributions(day, wc_name);
-- Migrate pre-existing deployments where end_utc was created NOT NULL.
ALTER TABLE wc_time_attributions ALTER COLUMN end_utc DROP NOT NULL;
```

- [ ] **Step 2: Verify the schema SQL is syntactically applied**

Run: `python -c "import zira_dashboard.db as d; print('end_utc' in d.SCHEMA_SQL if hasattr(d,'SCHEMA_SQL') else 'check init')"`
Expected: no import error. (If the schema is a module-level string under a different name, confirm the `ALTER` line is present in that string.)

- [ ] **Step 3: Commit**

```bash
git add src/zira_dashboard/db.py
git commit -m "feat(attributions): allow open-ended WC attributions (nullable end_utc)"
```

---

## Task 2: `wc_attributions.add` — accept an open end

**Files:**
- Modify: `src/zira_dashboard/wc_attributions.py:16-26`

- [ ] **Step 1: Make `end_utc` optional**

```python
def add(day: date, wc_name: str, person_name: str,
        start_utc: datetime, end_utc: datetime | None = None,
        source: str = "manual") -> int:
    """Insert one attribution row. `end_utc=None` means the assignment is
    OPEN — it stays running until the person clocks out, transfers, or is
    reassigned (resolved downstream by assignment_windows). Returns row id."""
    from . import db
    rows = db.query(
        "INSERT INTO wc_time_attributions "
        "(day, wc_name, person_name, start_utc, end_utc, source) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (day, wc_name, person_name, start_utc, end_utc, source),
    )
    return rows[0]["id"] if rows else 0
```

(`for_day` and `people_by_wc` already `SELECT end_utc` / ignore it — no change needed; `end_utc` will simply be `None` for open rows.)

- [ ] **Step 2: Commit**

```bash
git add src/zira_dashboard/wc_attributions.py
git commit -m "feat(attributions): add() supports open end_utc"
```

---

## Task 3: `/api/staffing/attribute` — accept open-ended assignments

**Files:**
- Modify: `src/zira_dashboard/routes/staffing.py:875-903`
- Test: `tests/test_staffing_attribute.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_staffing_attribute.py
import asyncio, json, types
from zira_dashboard.routes import staffing as staffing_routes


class _FakeReq:
    def __init__(self, body): self._body = body
    async def json(self): return self._body


def _call(body, monkeypatch):
    captured = {}
    def fake_add(day, wc, person, start_utc, end_utc=None, source="manual"):
        captured["end_utc"] = end_utc
        captured["start_utc"] = start_utc
        return 7
    monkeypatch.setattr("zira_dashboard.wc_attributions.add", fake_add)
    monkeypatch.setattr("zira_dashboard._http_cache.invalidate_today_cache", lambda: None)
    resp = asyncio.run(staffing_routes.staffing_attribute(_FakeReq(body)))
    return captured, resp


def test_attribute_open_ended_when_end_omitted(monkeypatch):
    body = {"day": "2026-06-02", "wc_name": "Dismantler 4",
            "person_name": "Eulogio Mendez", "start_utc": "2026-06-02T15:00:00+00:00"}
    captured, resp = _call(body, monkeypatch)
    assert resp.status_code == 200
    assert captured["end_utc"] is None  # open


def test_attribute_closed_when_end_provided(monkeypatch):
    body = {"day": "2026-06-02", "wc_name": "Dismantler 4",
            "person_name": "Eulogio Mendez",
            "start_utc": "2026-06-02T15:00:00+00:00",
            "end_utc": "2026-06-02T18:00:00+00:00"}
    captured, resp = _call(body, monkeypatch)
    assert resp.status_code == 200
    assert captured["end_utc"] is not None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_staffing_attribute.py -v`
Expected: FAIL — current handler requires `end_utc` (KeyError → 400) and always passes a value.

- [ ] **Step 3: Make `end_utc` optional in the handler**

Replace the body-parsing + validation block in `staffing_attribute`:

```python
    body = await request.json()
    try:
        day = _date.fromisoformat(body["day"])
        wc = str(body["wc_name"]).strip()
        person = str(body["person_name"]).strip()
        start_utc = _dt.fromisoformat(body["start_utc"])
        raw_end = body.get("end_utc")
        end_utc = _dt.fromisoformat(raw_end) if raw_end else None  # None/"" => open
    except (KeyError, TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": f"bad body: {e}"}, status_code=400)
    if not (wc and person):
        return JSONResponse({"ok": False, "error": "missing/invalid fields"}, status_code=400)
    if end_utc is not None and end_utc <= start_utc:
        return JSONResponse({"ok": False, "error": "end must be after start"}, status_code=400)
    new_id = wc_attributions.add(day, wc, person, start_utc, end_utc)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_staffing_attribute.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/routes/staffing.py tests/test_staffing_attribute.py
git commit -m "feat(staffing): attribute endpoint accepts open-ended assignment"
```

---

## Task 4: Assign popover sends start only (open-ended)

**Files:**
- Modify: `src/zira_dashboard/templates/new_dept.html:204-213`
- Modify: `src/zira_dashboard/templates/recycling.html:539-548`
- Modify: `src/zira_dashboard/templates/_footer.html:451-456`

- [ ] **Step 1: Drop `end_utc` from each POST body**

In all three templates, the `fetch('/api/staffing/attribute', …)` body currently includes `end_utc: btn.dataset.end` (or `li.dataset.end`). Remove that line so the assignment is created open. Example for `new_dept.html`:

```javascript
        fetch('/api/staffing/attribute', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            day: btn.dataset.day,
            wc_name: btn.dataset.wc,
            person_name: person,
            start_utc: btn.dataset.start,
          }),
        }).then(function (r) { return r.json(); })
```

For `_footer.html:455`, change `start_utc: li.dataset.start, end_utc: li.dataset.end,` to `start_utc: li.dataset.start,`.

- [ ] **Step 2: Verify no remaining `end_utc` send-sites**

Run: `grep -rn "end_utc: " src/zira_dashboard/templates`
Expected: no matches (the DELETE flows and `dataset.end` reads are unrelated; only the POST `end_utc:` keys should be gone).

- [ ] **Step 3: Commit**

```bash
git add src/zira_dashboard/templates/new_dept.html src/zira_dashboard/templates/recycling.html src/zira_dashboard/templates/_footer.html
git commit -m "feat(assign): create mid-day assignments open-ended (no end time)"
```

---

## Task 5: Pure `assignment_windows` module — resolve segments + expected

**Files:**
- Create: `src/zira_dashboard/assignment_windows.py`
- Test: `tests/test_assignment_windows.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_assignment_windows.py
from datetime import datetime, timezone
from zira_dashboard import assignment_windows as aw

UTC = timezone.utc
def t(h, m=0): return datetime(2026, 6, 2, h, m, tzinfo=UTC)

SHIFT_START = t(12)   # 07:00 America/Chicago == 12:00 UTC (CDT)
CAP = t(18)           # "now" = 13:00 CDT == 18:00 UTC


def _resolve(**kw):
    base = dict(assignments={}, attributions=[], punch_windows={},
                shift_start_utc=SHIFT_START, cap_utc=CAP, time_off_key="__time_off")
    base.update(kw)
    return aw.resolve_segments(**base)


def test_scheduled_person_spans_full_shift():
    segs = _resolve(assignments={"Dismantler 1": ["Jose Cabezas"]})
    assert len(segs) == 1
    s = segs[0]
    assert (s.wc_name, s.person_name, s.source) == ("Dismantler 1", "Jose Cabezas", "schedule")
    assert s.start_utc == SHIFT_START and s.end_utc == CAP


def test_time_off_key_is_skipped():
    assert _resolve(assignments={"__time_off": ["Whoever"]}) == []


def test_open_attribution_starts_midday_ends_at_cap():
    attrs = [{"wc_name": "Dismantler 4", "person_name": "Eulogio Mendez",
              "start_utc": t(15), "end_utc": None}]
    segs = _resolve(attributions=attrs)
    assert len(segs) == 1
    assert segs[0].start_utc == t(15) and segs[0].end_utc == CAP
    assert segs[0].source == "attribution"


def test_closed_attribution_keeps_its_end():
    attrs = [{"wc_name": "Dismantler 4", "person_name": "Eulogio Mendez",
              "start_utc": t(15), "end_utc": t(16, 30)}]
    segs = _resolve(attributions=attrs)
    assert segs[0].end_utc == t(16, 30)


def test_reassignment_closes_prior_open_segment_at_next_start():
    # Same person: open attribution at D4 from 13:00, then later open at D3 from 15:00.
    attrs = [
        {"wc_name": "Dismantler 4", "person_name": "Ana", "start_utc": t(13), "end_utc": None},
        {"wc_name": "Dismantler 3", "person_name": "Ana", "start_utc": t(15), "end_utc": None},
    ]
    segs = sorted(_resolve(attributions=attrs), key=lambda s: s.start_utc)
    assert (segs[0].wc_name, segs[0].start_utc, segs[0].end_utc) == ("Dismantler 4", t(13), t(15))
    assert (segs[1].wc_name, segs[1].start_utc, segs[1].end_utc) == ("Dismantler 3", t(15), CAP)


def test_punches_win_over_attribution_for_same_person():
    attrs = [{"wc_name": "Dismantler 4", "person_name": "Eulogio Mendez",
              "start_utc": t(13), "end_utc": None}]
    punches = {"Eulogio Mendez": [("Dismantler 2", t(14), None)]}
    segs = _resolve(attributions=attrs, punch_windows=punches)
    assert len(segs) == 1
    assert (segs[0].wc_name, segs[0].source) == ("Dismantler 2", "punch")
    assert segs[0].start_utc == t(14) and segs[0].end_utc == CAP


def test_punches_win_over_schedule_for_same_person():
    segs = _resolve(assignments={"Dismantler 1": ["Bob"]},
                    punch_windows={"Bob": [("Repair 1", t(13), t(16))]})
    assert len(segs) == 1
    assert (segs[0].wc_name, segs[0].source) == ("Repair 1", "punch")


def test_start_floored_and_end_capped_to_shift():
    attrs = [{"wc_name": "Dismantler 4", "person_name": "Ana",
              "start_utc": t(10), "end_utc": t(20)}]  # both outside [SHIFT_START, CAP]
    segs = _resolve(attributions=attrs)
    assert segs[0].start_utc == SHIFT_START and segs[0].end_utc == CAP


def test_zero_length_segment_dropped():
    attrs = [{"wc_name": "Dismantler 4", "person_name": "Ana",
              "start_utc": t(18), "end_utc": None}]  # starts at cap
    assert _resolve(attributions=attrs) == []


def test_expected_by_wc_prorates_per_segment():
    segs = [
        aw.WorkSegment("Dismantler 1", "Jose", SHIFT_START, CAP, "schedule"),
        aw.WorkSegment("Dismantler 4", "Eulogio", t(15), CAP, "attribution"),
    ]
    # 60 pallets/hr everywhere; productive_minutes returns raw window minutes.
    def prod(name, s, e): return (e - s).total_seconds() / 60.0
    out = aw.expected_by_wc(segs, {"Dismantler 1": 60.0, "Dismantler 4": 60.0}, prod)
    assert out["Dismantler 1"] == 360.0   # 6h × 60
    assert out["Dismantler 4"] == 180.0   # 3h × 60 (13:00→16:00 CDT)


def test_who_by_wc_dedupes_and_orders():
    segs = [
        aw.WorkSegment("Dismantler 4", "Eulogio", t(13), t(15), "attribution"),
        aw.WorkSegment("Dismantler 4", "Ana", t(15), CAP, "attribution"),
    ]
    assert aw.who_by_wc(segs) == {"Dismantler 4": "Eulogio + Ana"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_assignment_windows.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'zira_dashboard.assignment_windows'`.

- [ ] **Step 3: Implement the module**

```python
# src/zira_dashboard/assignment_windows.py
"""Resolve per-work-center work segments for a day by merging three sources
of "who worked where, when":

  1. The published schedule (full-shift assignments).
  2. Kiosk punch windows (clock_in/transfer_in -> transfer_out/clock_out).
  3. Open-ended retro WC attributions (end_utc may be None = still running).

Hybrid precedence: a person's KIOSK PUNCHES win over both their schedule
segment and any manual attribution for that day -- they were physically where
they punched. People with no punches fall back to schedule + attributions.

Every resolved segment carries a CLOSED [start_utc, end_utc] window. Open
inputs (attribution end_utc is None, or a trailing punch with no close yet)
are closed at the start of that person's NEXT segment that day (transfer /
reassignment) or at `cap_utc` = min(now, shift_end). Starts are floored to
shift start; ends capped to `cap_utc`; non-positive segments dropped.

Pure -- no DB, no network. The route supplies already-loaded inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable


@dataclass(frozen=True)
class WorkSegment:
    wc_name: str
    person_name: str
    start_utc: datetime
    end_utc: datetime
    source: str  # 'schedule' | 'punch' | 'attribution'


def resolve_segments(
    *,
    assignments: dict[str, list[str]],
    attributions: list[dict],
    punch_windows: dict[str, list[tuple]],
    shift_start_utc: datetime,
    cap_utc: datetime,
    time_off_key: str = "__time_off",
) -> list[WorkSegment]:
    """Merge schedule + punches + attributions into closed work segments.

    `attributions`: rows with keys wc_name, person_name, start_utc, end_utc(None ok).
    `punch_windows`: {person_name: [(wc_name, start_utc, end_utc|None), ...]}.
    """
    punched = set(punch_windows)
    # (person) -> list[ (wc, start, end_or_None, source) ]
    raw: dict[str, list[tuple]] = {}

    def _add(person, wc, start, end, source):
        raw.setdefault(person, []).append((wc, start, end, source))

    # 1. Schedule -- only for people WITHOUT punches (punches win).
    for wc, ops in (assignments or {}).items():
        if wc == time_off_key or not ops:
            continue
        for person in ops:
            if person in punched:
                continue
            _add(person, wc, shift_start_utc, None, "schedule")

    # 2. Punches -- authoritative for the people who have them.
    for person, windows in punch_windows.items():
        for (wc, start, end) in windows:
            if not wc:
                continue
            _add(person, wc, start, end, "punch")

    # 3. Attributions -- only for people WITHOUT punches.
    for a in (attributions or []):
        person = a["person_name"]
        if person in punched:
            continue
        _add(person, a["wc_name"], a["start_utc"], a.get("end_utc"), "attribution")

    out: list[WorkSegment] = []
    for person, items in raw.items():
        # Sort by start; an open segment closes at the next segment's start.
        items.sort(key=lambda x: x[1])
        for i, (wc, start, end, source) in enumerate(items):
            eff_start = max(start, shift_start_utc)
            eff_end = end if end is not None else cap_utc
            # Open (or overlapping) segment yields to the next one's start.
            if i + 1 < len(items):
                eff_end = min(eff_end, items[i + 1][1])
            eff_end = min(eff_end, cap_utc)
            if eff_end <= eff_start:
                continue
            out.append(WorkSegment(wc, person, eff_start, eff_end, source))
    return out


def expected_by_wc(
    segments: list[WorkSegment],
    target_per_hour: dict[str, float],
    productive_minutes: Callable[[str, datetime, datetime], float],
) -> dict[str, float]:
    """Sum prorated expected pallets per WC.

    `productive_minutes(person, start, end)` returns the working minutes in the
    window (the route passes staffing.effective_minutes_worked, which already
    subtracts breaks + partial time-off)."""
    out: dict[str, float] = {}
    for s in segments:
        thr = target_per_hour.get(s.wc_name, 0.0)
        if thr <= 0:
            continue
        mins = productive_minutes(s.person_name, s.start_utc, s.end_utc)
        if mins <= 0:
            continue
        out[s.wc_name] = out.get(s.wc_name, 0.0) + thr * mins / 60.0
    return out


def who_by_wc(segments: list[WorkSegment]) -> dict[str, str]:
    """{wc_name: 'A + B'} operator labels, deduped, ordered by segment start."""
    order: dict[str, list[str]] = {}
    for s in sorted(segments, key=lambda x: (x.wc_name, x.start_utc)):
        names = order.setdefault(s.wc_name, [])
        if s.person_name not in names:
            names.append(s.person_name)
    return {wc: " + ".join(ns) for wc, ns in order.items()}
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_assignment_windows.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/assignment_windows.py tests/test_assignment_windows.py
git commit -m "feat(windows): pure assignment-window resolver + per-WC expected proration"
```

---

## Task 6: `timeclock_windows` — punch windows from the kiosk log

**Files:**
- Create: `src/zira_dashboard/timeclock_windows.py`
- Test: `tests/test_timeclock_windows.py`

- [ ] **Step 1: Write the failing test (pure row transform)**

```python
# tests/test_timeclock_windows.py
from datetime import datetime, timezone
from zira_dashboard.timeclock_windows import _segments_from_rows

UTC = timezone.utc
def t(h, m=0): return datetime(2026, 6, 2, h, m, tzinfo=UTC)


def test_clock_in_then_out_one_window():
    rows = [
        {"action": "clock_in", "wc_name": "Dismantler 4", "at": t(13)},
        {"action": "clock_out", "wc_name": None, "at": t(17)},
    ]
    assert _segments_from_rows(rows) == [("Dismantler 4", t(13), t(17))]


def test_transfer_splits_into_two_windows():
    rows = [
        {"action": "clock_in", "wc_name": "Dismantler 4", "at": t(13)},
        {"action": "transfer_out", "wc_name": "Dismantler 4", "at": t(15)},
        {"action": "transfer_in", "wc_name": "Repair 1", "at": t(15)},
        {"action": "clock_out", "wc_name": None, "at": t(17)},
    ]
    assert _segments_from_rows(rows) == [
        ("Dismantler 4", t(13), t(15)),
        ("Repair 1", t(15), t(17)),
    ]


def test_still_clocked_in_trailing_window_is_open():
    rows = [{"action": "clock_in", "wc_name": "Dismantler 4", "at": t(13)}]
    assert _segments_from_rows(rows) == [("Dismantler 4", t(13), None)]


def test_window_without_wc_dropped():
    rows = [
        {"action": "clock_in", "wc_name": None, "at": t(13)},
        {"action": "clock_out", "wc_name": None, "at": t(17)},
    ]
    assert _segments_from_rows(rows) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_timeclock_windows.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the module**

```python
# src/zira_dashboard/timeclock_windows.py
"""Per-person work-center windows derived from the local kiosk punch log
(timeclock_punches_log). A clock_in/transfer_in opens a window at its
wc_name; a transfer_out/clock_out (or the next open) closes it. Trailing
open windows (still clocked in) get end=None and are closed downstream by
assignment_windows against the shift cap.

Kiosk is still a Phase-0 pilot, so most operators have no punches yet --
punch_windows_for_day returns {} for them and the resolver falls back to
schedule + manual attributions.
"""
from __future__ import annotations

from datetime import date, datetime


def _segments_from_rows(rows: list[dict]) -> list[tuple[str, datetime, datetime | None]]:
    """rows: ONE person's punch rows, ordered by time. Each {action, wc_name, at}.
    Returns [(wc_name, start_utc, end_utc|None)]. Pure + testable."""
    out: list[tuple[str, datetime, datetime | None]] = []
    open_wc: str | None = None
    open_start: datetime | None = None
    for r in rows:
        action = r["action"]
        at = r["at"]
        if action in ("clock_in", "transfer_in"):
            if open_wc is not None and open_start is not None and at > open_start:
                out.append((open_wc, open_start, at))
            open_wc = r.get("wc_name")
            open_start = at
        elif action in ("clock_out", "transfer_out"):
            if open_wc is not None and open_start is not None and at > open_start:
                out.append((open_wc, open_start, at))
            open_wc = None
            open_start = None
    if open_wc is not None and open_start is not None:
        out.append((open_wc, open_start, None))
    return [(wc, s, e) for (wc, s, e) in out if wc]


def punch_windows_for_day(day: date) -> dict[str, list[tuple[str, datetime, datetime | None]]]:
    """{roster_name: [(wc_name, start_utc, end_utc|None), ...]} from the punch
    log for `day` (site-local day bounds). Never raises -- returns {} on error."""
    try:
        from . import db, attendance, shift_config
        from datetime import datetime as _dt, time as _time, timezone as _tz, timedelta as _td
        site = shift_config.SITE_TZ
        start_local = _dt.combine(day, _time(0, 0), tzinfo=site)   # local midnight
        end_local = start_local + _td(days=1)                      # next local midnight
        start_utc = start_local.astimezone(_tz.utc)
        end_utc = end_local.astimezone(_tz.utc)
        id_to_name = {v: k for k, v in attendance.name_to_person_id().items()}
        rows = db.query(
            "SELECT person_odoo_id, action, wc_name, "
            "       COALESCE(rounded_at, occurred_at) AS at "
            "FROM timeclock_punches_log "
            "WHERE COALESCE(rounded_at, occurred_at) >= %s "
            "  AND COALESCE(rounded_at, occurred_at) < %s "
            "ORDER BY person_odoo_id, COALESCE(rounded_at, occurred_at), id",
            (start_utc, end_utc),
        )
    except Exception:
        return {}
    by_person: dict[str, list[dict]] = {}
    for r in rows:
        name = id_to_name.get(str(r["person_odoo_id"]))
        if not name:
            continue
        by_person.setdefault(name, []).append(r)
    return {name: _segments_from_rows(rs) for name, rs in by_person.items()}
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_timeclock_windows.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/timeclock_windows.py tests/test_timeclock_windows.py
git commit -m "feat(windows): derive per-person WC windows from kiosk punch log"
```

---

## Task 7: Wire segments into the recycling dashboard

**Files:**
- Modify: `src/zira_dashboard/routes/departments.py` — `_recycling_day_data` (`who_by_wc` at :90, `per_wc_expected` at :266)

- [ ] **Step 1: Build segments once, near the top of `_recycling_day_data`**

After `window_start_utc` / `window_end_utc` are computed ([departments.py:114-115](../../../src/zira_dashboard/routes/departments.py)) and after `sched = staffing.load_schedule(d)`, build the segment list and derive `who_by_wc` from it. Replace the `who_by_wc = _who_by_wc(sched.assignments, d)` line (:90) — but note it is used earlier (:90) than the window bounds (:114). Move the `who_by_wc` derivation down to just after :115, and compute `active_wc_names` from it there. Concretely:

Delete line 90 (`who_by_wc = _who_by_wc(sched.assignments, d)`) and lines 92-99 that depend on it, and reinsert this block right after `window_end_utc = ...` (:115):

```python
    from .. import assignment_windows, timeclock_windows, wc_attributions
    segments = assignment_windows.resolve_segments(
        assignments=sched.assignments,
        attributions=wc_attributions.for_day(d),
        punch_windows=timeclock_windows.punch_windows_for_day(d),
        shift_start_utc=window_start_utc,
        cap_utc=window_end_utc,
        time_off_key=staffing.TIME_OFF_KEY,
    )
    who_by_wc = assignment_windows.who_by_wc(segments)

    ACTIVE_UNITS_THRESHOLD = 5
    active_wc_names: set[str] = set(who_by_wc.keys())
    for r in results:
        if r.units > ACTIVE_UNITS_THRESHOLD:
            active_wc_names.add(r.station.name)
    active_results = [r for r in results if r.station.name in active_wc_names]
    active_stations = [s for s in stations if s.name in active_wc_names]
    total_units = sum(r.units for r in active_results)
    total_downtime = sum(r.downtime_minutes for r in active_results)
    elapsed = shift_elapsed_minutes(d, now)
    available = elapsed * len(active_stations)
    uptime_minutes = max(0, available - total_downtime)
```

(The original lines 92-105 computed `ACTIVE_UNITS_THRESHOLD`, `active_*`, `total_*`, `elapsed`, etc. before the window bounds; this block reorders them to run after the bounds. Remove the now-duplicated originals at 92-105. Keep the `shift_start_local/shift_end_local/now_local/window_*` block that originally sat at 107-115 above this block.)

- [ ] **Step 2: Replace the `per_wc_expected` formula (:266)**

Replace the dict-comprehension at [departments.py:266-273](../../../src/zira_dashboard/routes/departments.py) with a segment-based, window-aware proration:

```python
    # Per-WC expected pallets: prorate each work segment from its OWN start
    # (mid-day assignments included) to its end/transfer/now, using productive
    # minutes (breaks + partial time-off already netted out). Replaces the old
    # scheduled_headcount × shift-wide elapsed_hours, which ignored mid-day
    # attributions/punches and so showed no goal for unscheduled-but-worked WCs.
    target_per_hour = {
        r.station.name: settings_store.station_target(r.station) for r in active_results
    }
    active_segments = [s for s in segments if s.wc_name in active_wc_names]
    per_wc_expected = assignment_windows.expected_by_wc(
        active_segments,
        target_per_hour,
        lambda name, s_utc, e_utc: staffing.effective_minutes_worked(name, d, s_utc, e_utc),
    )
    # Ensure every active WC has an entry so downstream .get(...) is stable.
    for name in active_wc_names:
        per_wc_expected.setdefault(name, 0.0)
```

(`people_by_wc` at :168 stays — it still feeds the progress-chart `target_fn` grace logic, a separate widget. Only the per-station bar `expected` changes.)

- [ ] **Step 3: Run the existing recycling/dashboard test suite**

Run: `pytest tests/test_wc_dashboard_data.py tests/test_wc_attributions.py tests/test_progress.py -v`
Expected: PASS. (If a test asserted the old shift-wide `expected` for a *scheduled* WC, update it: a full-shift scheduled segment now prorates via `effective_minutes_worked(name, …, shift_start, cap)`, which equals the old value minus that person's breaks/partial-off — intentional refinement. Adjust the expected number to match the productive-minutes proration.)

- [ ] **Step 4: Run the full suite**

Run: `pytest -q`
Expected: PASS (or only the intentionally-updated assertions changed).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/routes/departments.py tests/
git commit -m "feat(recycling): goal prorates from mid-day assignment start (segments)"
```

---

## Task 8: Verify on the live dashboard

**Files:** none (verification only)

- [ ] **Step 1: Start the app and open the recycling TV view**

Use the `preview_*` tools: `preview_start`, then load `/tv/recycling`. (If a manual local run is needed, follow the project `run` skill.)

- [ ] **Step 2: Reproduce the original scenario**

With an open mid-day attribution for Dismantler 4 (e.g. via the `↪ assign` popover, which now writes start-only), confirm:
- Dismantler 4 now shows a **goal denominator** (e.g. `113 / NNN`) where `NNN` ≈ `station_target_per_hour × hours_from_assignment_start_to_now`.
- The goal is **smaller** than stations 1 & 2 proportional to the later start (it began mid-day), and **grows** on auto-refresh as the shift elapses.
- The card total (`941 / …`) increases its denominator by D4's prorated expected.

- [ ] **Step 3: Capture proof**

`preview_screenshot` the Dismantlers card; confirm D4's denominator is present and sensible. Check `preview_console_logs` for errors.

- [ ] **Step 4: Update CHANGELOG and commit**

Add a new `### HH:MM` entry under today's date in `CHANGELOG.md` (per the deploy convention), e.g.:

```markdown
### 14:05
- **Mid-day assignments now carry a live goal.** Assigning an operator to an unscheduled work center (e.g. Dismantler 4) mid-shift now stays open until they clock out / transfer / are reassigned, and the station's goal prorates from the assignment start instead of showing blank. Goal sources are hybrid: kiosk punches win, manual assignment is the fallback.
```

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): mid-day assignment goal proration"
```

---

## Self-review notes

- **Spec coverage:** "no end at assignment" → Tasks 1-4 (nullable + open `add` + endpoint + UI). "open until clock-out/transfer/reassign" → Task 5 (`resolve_segments` closes at next-segment start or cap) + Task 6 (punch transfer/clock-out windows). "goal from assignment start to end/transfer, and shows" → Task 5 `expected_by_wc` + Task 7 wiring + Task 8 template already renders `{% if b.expected %}`. "hybrid, punches first" → Task 5 `punched` precedence + Task 6.
- **Type consistency:** `WorkSegment(wc_name, person_name, start_utc, end_utc, source)` used identically in Tasks 5 & 7. `resolve_segments(... cap_utc ...)` keyword matches the route call. `punch_windows` shape `{name: [(wc, start, end|None)]}` matches Task 6 output and Task 5 consumer.
- **Edge cases covered by tests:** time-off key skipped, start floor / end cap, zero-length drop, reassignment close, punch-over-attribution, punch-over-schedule, still-open trailing punch, WC-less punch dropped.
- **Known intentional behavior change:** scheduled-WC `expected` now nets out per-person breaks/partial time-off (was a flat headcount × elapsed). Numbers shift slightly downward for people with partial-day leave; this is more accurate. Flag any test asserting the old value.
