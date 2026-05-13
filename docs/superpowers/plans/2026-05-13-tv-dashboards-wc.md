# Per-WC Dashboard (Sub-Project 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-work-center dashboard at `/wc/{slug}` (editor — drag/resize widgets) and `/tv/wc/{slug}` (TV — read-only, no chrome). Six widgets: pallets banner, daily progress chart, GOAT race, monthly ribbons, 15-min increment bars, downtime report. Header shows WC name top-left + operators top-right (only people assigned to this WC).

**Architecture:** New `wc_dashboard_data.py` module owns the per-WC data prep (pure functions over Zira cached payload + awards). New `routes/wc_dashboard.py` carries the editor + TV routes — both call a shared `_render_wc_dashboard(request, *, slug, tv_mode, tv_theme)` helper, mirroring the cache-key + delegation pattern fixed in sub-project 1. Gridstack layout persists via the existing `layout_store` + `/api/layout/{page}` endpoints with `page = "wc:{slug}"`. Template is brand-new (`templates/wc_dashboard.html`); chrome-hide + TV theme handled by the existing `static/tv-mode.css` from sub-project 1.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, gridstack 10.3, psycopg2 + Postgres, pytest. No new dependencies, no new tables (reuses `widget_layouts`).

**Spec:** `docs/superpowers/specs/2026-05-13-tv-dashboards-design.md` — sub-project 2 of 4.

---

## File Structure

**New files:**
- `src/zira_dashboard/wc_dashboard_data.py` — pure data-prep helpers (slug derivation, per-WC pallets / daily progress / 15-min / downtime / GOAT race / monthly ribbons / assigned operators)
- `src/zira_dashboard/routes/wc_dashboard.py` — `/wc/{slug}` + `/tv/wc/{slug}` routes, both delegating to one shared renderer
- `src/zira_dashboard/templates/wc_dashboard.html` — single template with the 6 gridstack widgets + the `_tv_header` macro
- `tests/test_wc_dashboard_data.py` — unit tests for the data-prep functions
- `tests/test_wc_dashboard.py` — integration tests for the two routes

**Modified files:**
- `src/zira_dashboard/app.py` — register the new router
- `CHANGELOG.md` — one deploy entry

**Responsibility split:** `wc_dashboard_data.py` is pure-functional and has no FastAPI / template knowledge — every helper takes a WC name (or slug) plus a date and returns a widget-ready dict. The route module owns request/response, theme resolution, cache key, and template invocation. The template owns the gridstack layout + widget rendering. Tests follow the existing pytest patterns (`pytest.mark.skipif(not os.environ.get("DATABASE_URL"))` for anything that hits the schedule store or Zira cache).

---

## Conventions

- Python interpreter on Dale's Windows machine: `.venv/Scripts/python.exe`. Always use that.
- All new DB-touching tests are gated on `DATABASE_URL` at the module level, matching `tests/test_dashboards_polish.py`.
- Slug rule: lowercase, alphanumerics + hyphens; every other character → hyphen; collapse runs of hyphens; strip leading/trailing hyphens.
- Commit messages: `feat(wc-dashboard):` / `test(wc-dashboard):` / `docs:`.

---

## Task 1: Slug utility + tests

**Files:**
- Create: `src/zira_dashboard/wc_dashboard_data.py` (new module — only the slug function for now)
- Test: `tests/test_wc_dashboard_data.py` (new)

- [ ] **Step 1: Write failing tests**

Create `tests/test_wc_dashboard_data.py`:

```python
"""Unit tests for wc_dashboard_data helpers.

Pure functions only — these tests don't need a DB and run unconditionally.
"""
from __future__ import annotations


def test_slug_simple():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("Repair 1") == "repair-1"


def test_slug_lowercases():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("REPAIR 1") == "repair-1"


def test_slug_collapses_punctuation():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("Hand Build #1") == "hand-build-1"


def test_slug_strips_leading_trailing_hyphens():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("  Bay 4  ") == "bay-4"
    assert slug_for_wc("--repair-1--") == "repair-1"


def test_slug_collapses_runs_of_hyphens():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("Repair   1") == "repair-1"
    assert slug_for_wc("Hand // Build") == "hand-build"


def test_slug_keeps_digits():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("Trim Saw 12") == "trim-saw-12"


def test_slug_empty_input():
    from zira_dashboard.wc_dashboard_data import slug_for_wc
    assert slug_for_wc("") == ""
    assert slug_for_wc("   ") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_wc_dashboard_data.py -v`
Expected: 7 FAIL — module/function doesn't exist.

- [ ] **Step 3: Create the module + slug function**

Create `src/zira_dashboard/wc_dashboard_data.py`:

```python
"""Per-WC dashboard data-prep helpers.

Pure functions over the existing `cached_leaderboard`, `awards`, and
`work_centers_store` modules. Each helper takes a WC name (or slug) +
a date and returns a widget-ready dict the template can iterate.

The single-page dashboard at /wc/{slug} (editor) and /tv/wc/{slug}
(TV) compose these helpers into one render. No FastAPI / template
imports here — keep this module testable without standing up the app.
"""
from __future__ import annotations

import re


def slug_for_wc(name: str) -> str:
    """URL-safe slug derived from a work-center name.

    Lowercase, alphanumerics + hyphens; everything else collapses to
    a single hyphen. Used as the dashboard layout key (`wc:{slug}`)
    and in URLs (`/wc/{slug}`).

    Examples:
      'Repair 1'       -> 'repair-1'
      'Hand Build #1'  -> 'hand-build-1'
      'Trim Saw 12'    -> 'trim-saw-12'
    """
    s = (name or "").strip().lower()
    # Replace every run of non-alphanumeric chars with a single hyphen.
    s = re.sub(r"[^a-z0-9]+", "-", s)
    # Strip leading + trailing hyphens.
    return s.strip("-")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_wc_dashboard_data.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/wc_dashboard_data.py tests/test_wc_dashboard_data.py
git commit -m "feat(wc-dashboard): slug_for_wc utility + tests"
```

---

## Task 2: Data-prep module — pallets, operators, GOAT race, monthly ribbons

The four "easy" widgets (no Zira-payload parsing required — they read from existing helpers).

**Files:**
- Modify: `src/zira_dashboard/wc_dashboard_data.py` (append functions)
- Test: `tests/test_wc_dashboard_data.py` (append tests)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_wc_dashboard_data.py`:

```python
import os
from datetime import date as _date

import pytest


def test_wc_by_slug_resolves_known_slug(monkeypatch):
    from zira_dashboard import wc_dashboard_data, work_centers_store

    class _Loc:
        def __init__(self, name): self.name = name

    from zira_dashboard import staffing
    monkeypatch.setattr(
        staffing, "LOCATIONS",
        [_Loc("Repair 1"), _Loc("Hand Build #1")],
    )
    loc = wc_dashboard_data.wc_by_slug("repair-1")
    assert loc is not None and loc.name == "Repair 1"
    loc2 = wc_dashboard_data.wc_by_slug("hand-build-1")
    assert loc2 is not None and loc2.name == "Hand Build #1"


def test_wc_by_slug_unknown_returns_none(monkeypatch):
    from zira_dashboard import wc_dashboard_data, staffing
    monkeypatch.setattr(staffing, "LOCATIONS", [])
    assert wc_dashboard_data.wc_by_slug("ghost") is None


def test_assigned_operators_for_wc(monkeypatch):
    from zira_dashboard import wc_dashboard_data, staffing

    monkeypatch.setattr(staffing, "load_schedule", lambda d: staffing.Schedule(
        day=d, published=True,
        assignments={"Repair 1": ["Christian", "Jose L"], "Repair 2": ["Alice"]},
    ))
    out = wc_dashboard_data.assigned_operators_for_wc("Repair 1", _date(2026, 5, 13))
    assert out == ["Christian", "Jose L"]


def test_assigned_operators_unassigned_returns_empty(monkeypatch):
    from zira_dashboard import wc_dashboard_data, staffing
    monkeypatch.setattr(staffing, "load_schedule", lambda d: staffing.Schedule(
        day=d, published=True, assignments={},
    ))
    assert wc_dashboard_data.assigned_operators_for_wc("Repair 1", _date(2026, 5, 13)) == []


def test_pallets_banner_data(monkeypatch):
    """Pallets banner: today's units vs prorated target for THIS WC."""
    from zira_dashboard import wc_dashboard_data, work_centers_store
    # 200-unit/day WC; shift is half elapsed → prorated target 100.
    class _Loc:
        name = "Repair 1"
    monkeypatch.setattr(
        wc_dashboard_data, "_load_wc", lambda nm: _Loc() if nm == "Repair 1" else None
    )
    monkeypatch.setattr(work_centers_store, "goal_per_day", lambda loc: 200)
    monkeypatch.setattr(wc_dashboard_data, "_units_today_for_wc", lambda nm, d: 87)
    monkeypatch.setattr(wc_dashboard_data, "_shift_elapsed_fraction", lambda d: 0.5)

    out = wc_dashboard_data.pallets_banner("Repair 1", _date(2026, 5, 13))
    assert out["units_today"] == 87
    assert out["target_today"] == 100  # 200 * 0.5
    assert out["target_full_day"] == 200
    assert out["pct_of_target"] == pytest.approx(87.0)  # 87/100*100


def test_monthly_ribbons_uses_group(monkeypatch):
    """Monthly ribbons come from the WC's group, not the WC itself."""
    from zira_dashboard import wc_dashboard_data, work_centers_store, awards

    class _Loc:
        name = "Repair 1"
    monkeypatch.setattr(wc_dashboard_data, "_load_wc", lambda nm: _Loc() if nm == "Repair 1" else None)
    monkeypatch.setattr(work_centers_store, "groups", lambda loc: ["Repairs"])
    monkeypatch.setattr(
        awards, "monthly_badges",
        lambda group, year, month: [
            {"position": 1, "name": "Christian", "day": _date(2026, 5, 4), "units": 145, "pph": 16.1},
            {"position": 2, "name": "Lauro",     "day": _date(2026, 5, 9), "units": 132, "pph": 14.7},
        ] if group == "Repairs" else [],
    )
    out = wc_dashboard_data.monthly_ribbons("Repair 1", 2026, 5)
    assert out["group"] == "Repairs"
    assert len(out["entries"]) == 2
    assert out["entries"][0]["name"] == "Christian"


def test_goat_race_uses_group(monkeypatch):
    """GOAT race compares against the WC's group's all-time GOAT."""
    from zira_dashboard import wc_dashboard_data, work_centers_store, awards

    class _Loc:
        name = "Repair 1"
    monkeypatch.setattr(wc_dashboard_data, "_load_wc", lambda nm: _Loc() if nm == "Repair 1" else None)
    monkeypatch.setattr(work_centers_store, "groups", lambda loc: ["Repairs"])
    monkeypatch.setattr(
        awards, "goat",
        lambda group: {"name": "Christian", "day": _date(2026, 2, 15), "units": 145, "pph": 16.1}
            if group == "Repairs" else None,
    )
    # 87 units today vs GOAT's 145-units day, half elapsed → GOAT pace today = 145 * 0.5 = 72.5
    monkeypatch.setattr(wc_dashboard_data, "_units_today_for_wc", lambda nm, d: 87)
    monkeypatch.setattr(wc_dashboard_data, "_shift_elapsed_fraction", lambda d: 0.5)

    out = wc_dashboard_data.goat_race("Repair 1", _date(2026, 5, 13))
    assert out["group"] == "Repairs"
    assert out["units_today"] == 87
    assert out["goat"]["name"] == "Christian"
    assert out["goat"]["units"] == 145
    assert out["goat_pace_today"] == pytest.approx(72.5)
    assert out["status"] == "AHEAD"  # 87 > 72.5


def test_goat_race_status_on_pace_when_within_5pct(monkeypatch):
    from zira_dashboard import wc_dashboard_data, work_centers_store, awards

    class _Loc:
        name = "Repair 1"
    monkeypatch.setattr(wc_dashboard_data, "_load_wc", lambda nm: _Loc())
    monkeypatch.setattr(work_centers_store, "groups", lambda loc: ["Repairs"])
    monkeypatch.setattr(
        awards, "goat",
        lambda group: {"name": "Christian", "day": _date(2026, 2, 15), "units": 100, "pph": 12.5},
    )
    # 50 today, GOAT pace = 100 * 0.5 = 50 — exactly on pace.
    monkeypatch.setattr(wc_dashboard_data, "_units_today_for_wc", lambda nm, d: 50)
    monkeypatch.setattr(wc_dashboard_data, "_shift_elapsed_fraction", lambda d: 0.5)

    out = wc_dashboard_data.goat_race("Repair 1", _date(2026, 5, 13))
    assert out["status"] == "ON_PACE"


def test_goat_race_no_goat_yet(monkeypatch):
    from zira_dashboard import wc_dashboard_data, work_centers_store, awards

    class _Loc:
        name = "Repair 1"
    monkeypatch.setattr(wc_dashboard_data, "_load_wc", lambda nm: _Loc())
    monkeypatch.setattr(work_centers_store, "groups", lambda loc: ["Repairs"])
    monkeypatch.setattr(awards, "goat", lambda group: None)
    monkeypatch.setattr(wc_dashboard_data, "_units_today_for_wc", lambda nm, d: 30)
    monkeypatch.setattr(wc_dashboard_data, "_shift_elapsed_fraction", lambda d: 0.5)

    out = wc_dashboard_data.goat_race("Repair 1", _date(2026, 5, 13))
    assert out["goat"] is None
    assert out["status"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_wc_dashboard_data.py -v`
Expected: 9 new FAILs.

- [ ] **Step 3: Implement the helpers**

Append to `src/zira_dashboard/wc_dashboard_data.py`:

```python
from datetime import date, datetime, timezone


def _load_wc(name: str):
    """Return the Location for `name`, or None.

    Indirection so tests can monkeypatch this single function. Note
    that the canonical work-center list lives in `staffing.LOCATIONS`
    (work_centers_store re-uses it but doesn't expose `all_locations`).
    """
    from . import staffing
    for loc in staffing.LOCATIONS:
        if loc.name == name:
            return loc
    return None


def wc_by_slug(slug: str):
    """Reverse lookup: slug -> Location. Returns None if no match.

    Linear scan since the WC list is tens of items, not thousands.
    """
    from . import staffing
    target = (slug or "").strip().lower()
    if not target:
        return None
    for loc in staffing.LOCATIONS:
        if slug_for_wc(loc.name) == target:
            return loc
    return None


def assigned_operators_for_wc(wc_name: str, day: date) -> list[str]:
    """Return the names assigned to this specific WC in the published
    schedule for `day`. Empty list if unassigned. Only this WC — not
    the whole group.
    """
    from . import staffing
    try:
        sched = staffing.load_schedule(day)
    except Exception:
        return []
    return list(sched.assignments.get(wc_name, []) or [])


def _shift_elapsed_fraction(day: date) -> float:
    """Fraction of today's shift that has elapsed, 0.0..1.0.

    For days other than today, returns 1.0 (full shift counted). For
    today before shift-start, returns 0.0.
    """
    from . import shift_config
    today_utc = datetime.now(timezone.utc).date()
    if day < today_utc:
        return 1.0
    if day > today_utc:
        return 0.0
    elapsed = shift_config.shift_elapsed_minutes(day, datetime.now(timezone.utc))
    total = shift_config.productive_minutes_for(day) or 1
    return max(0.0, min(1.0, elapsed / total))


def _units_today_for_wc(wc_name: str, day: date) -> int:
    """Today's pallet count for one WC. Reads from the cached Zira
    leaderboard (shared with /recycling), so this is a fast lookup.
    Returns 0 if the WC has no meter or no data yet.
    """
    from .deps import client
    from .leaderboard import cached_leaderboard
    from .stations import Station
    loc = _load_wc(wc_name)
    if loc is None or not loc.meter_id:
        return 0
    stations = [Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)]
    try:
        results = cached_leaderboard(client, stations, day)
    except Exception:
        return 0
    for r in results:
        if r.station.name == wc_name:
            return int(r.units)
    return 0


def pallets_banner(wc_name: str, day: date) -> dict:
    """Pallets-banner widget data. Today's units for THIS WC against
    the prorated daily target.

    Returns: {units_today, target_today, target_full_day, pct_of_target}.
    """
    from . import work_centers_store
    loc = _load_wc(wc_name)
    if loc is None:
        return {"units_today": 0, "target_today": 0, "target_full_day": 0, "pct_of_target": None}
    full = int(work_centers_store.goal_per_day(loc) or 0)
    frac = _shift_elapsed_fraction(day)
    target_today = int(round(full * frac))
    units = _units_today_for_wc(wc_name, day)
    pct = (units / target_today * 100.0) if target_today > 0 else None
    return {
        "units_today": units,
        "target_today": target_today,
        "target_full_day": full,
        "pct_of_target": pct,
    }


def monthly_ribbons(wc_name: str, year: int, month: int) -> dict:
    """Top-3 person-days in this WC's group for the given month."""
    from . import awards, work_centers_store
    loc = _load_wc(wc_name)
    if loc is None:
        return {"group": None, "entries": []}
    grp_list = work_centers_store.groups(loc) or []
    if not grp_list:
        return {"group": None, "entries": []}
    group = grp_list[0]
    entries = awards.monthly_badges(group, year, month) or []
    return {"group": group, "entries": entries}


def goat_race(wc_name: str, day: date) -> dict:
    """GOAT-race widget. Compares today's pace at this WC against the
    WC's group's all-time GOAT day, prorated by elapsed shift fraction.

    status: 'AHEAD' / 'ON_PACE' / 'BEHIND' / None (if no GOAT yet).
    """
    from . import awards, work_centers_store
    loc = _load_wc(wc_name)
    if loc is None:
        return {"group": None, "goat": None, "units_today": 0, "goat_pace_today": 0, "status": None}
    grp_list = work_centers_store.groups(loc) or []
    group = grp_list[0] if grp_list else None
    goat = awards.goat(group) if group else None
    units = _units_today_for_wc(wc_name, day)
    if goat is None:
        return {"group": group, "goat": None, "units_today": units, "goat_pace_today": 0, "status": None}
    frac = _shift_elapsed_fraction(day)
    goat_pace_today = float(goat.get("units", 0)) * frac
    # Status thresholds — within ±5 % of pace is "ON_PACE", otherwise
    # AHEAD / BEHIND.
    if goat_pace_today <= 0:
        status = None
    else:
        delta_pct = (units - goat_pace_today) / goat_pace_today * 100.0
        if delta_pct > 5:
            status = "AHEAD"
        elif delta_pct < -5:
            status = "BEHIND"
        else:
            status = "ON_PACE"
    return {
        "group": group,
        "goat": goat,
        "units_today": units,
        "goat_pace_today": goat_pace_today,
        "status": status,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_wc_dashboard_data.py -v`
Expected: all PASS (7 from Task 1 + 9 from Task 2 = 16).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/wc_dashboard_data.py tests/test_wc_dashboard_data.py
git commit -m "feat(wc-dashboard): pallets banner + operators + GOAT race + monthly ribbons"
```

---

## Task 3: Data-prep — daily progress, 15-min increments, downtime

The three widgets that need to slice today's per-meter Zira readings.

**Files:**
- Modify: `src/zira_dashboard/wc_dashboard_data.py`
- Test: `tests/test_wc_dashboard_data.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_wc_dashboard_data.py`:

```python
def _fake_readings(per_minute):
    """Helper: build a fake Zira readings payload where `per_minute` is
    a list of (minute_offset_from_shift_start, units) tuples.
    """
    from datetime import datetime, timezone, timedelta
    from zira_dashboard.shift_config import shift_start_for, SITE_TZ
    today = datetime.now(timezone.utc).date()
    shift_start = datetime.combine(today, shift_start_for(today), tzinfo=SITE_TZ)
    return [
        {
            "ts_utc": shift_start.astimezone(timezone.utc) + timedelta(minutes=m),
            "units": u,
            "event": "pallet",
        }
        for m, u in per_minute
    ]


def test_daily_progress_cumulative(monkeypatch):
    """Daily progress: list of (minute, cumulative_units) at 15-min granularity."""
    from zira_dashboard import wc_dashboard_data

    # Three readings at minutes 5, 30, 80 with 10 / 20 / 5 units each.
    readings = _fake_readings([(5, 10), (30, 20), (80, 5)])
    monkeypatch.setattr(
        wc_dashboard_data, "_readings_for_wc_today",
        lambda nm, d: readings,
    )
    out = wc_dashboard_data.daily_progress("Repair 1", _date.today())
    # 8 hours × 4 buckets/hr = 32 buckets.
    assert len(out) >= 6
    # Reading at minute 5 → bucket 0 (0-14m); cumulative 10.
    assert out[0]["cumulative_units"] == 10
    # Reading at minute 30 → bucket 2 (30-44m); cumulative 30.
    assert out[2]["cumulative_units"] == 30
    # Reading at minute 80 → bucket 5 (75-89m); cumulative 35.
    assert out[5]["cumulative_units"] == 35


def test_fifteen_min_increments_color_coded(monkeypatch):
    """Each 15-min bucket: units in that interval + green/amber/red flag."""
    from zira_dashboard import wc_dashboard_data

    # Target = 8 units / bucket (= 32 / 4 buckets / 8 hrs * 15 min wait that math is wrong).
    # Easier: directly control the target via _wc_target_per_bucket.
    monkeypatch.setattr(wc_dashboard_data, "_wc_target_per_bucket", lambda nm, d: 8)
    # Readings: bucket 0 → 10 units (green), bucket 1 → 6 (amber, ≥ 75% of 8 = 6),
    # bucket 2 → 4 (red, < 75% = 6).
    readings = _fake_readings([(5, 10), (20, 6), (35, 4)])
    monkeypatch.setattr(
        wc_dashboard_data, "_readings_for_wc_today",
        lambda nm, d: readings,
    )
    out = wc_dashboard_data.fifteen_min_increments("Repair 1", _date.today())
    assert out[0]["units"] == 10 and out[0]["color"] == "green"
    assert out[1]["units"] == 6  and out[1]["color"] == "amber"
    assert out[2]["units"] == 4  and out[2]["color"] == "red"


def test_downtime_report(monkeypatch):
    """Downtime: list of {time, duration_minutes} events derived from
    gaps in active_intervals, plus an authoritative total from
    StationTotal.downtime_minutes."""
    from zira_dashboard import wc_dashboard_data

    class _Total:
        downtime_minutes = 11
    monkeypatch.setattr(wc_dashboard_data, "_station_total_for_wc",
                        lambda nm, d: _Total())
    monkeypatch.setattr(
        wc_dashboard_data, "_downtime_events_for_wc",
        lambda nm, d: [
            {"time": "9:42a",  "duration_minutes": 3},
            {"time": "11:15a", "duration_minutes": 8},
        ],
    )
    out = wc_dashboard_data.downtime_report("Repair 1", _date.today())
    assert out["total_minutes"] == 11
    assert len(out["events"]) == 2
    assert "reason" not in out["events"][0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_wc_dashboard_data.py -v`
Expected: 3 new FAILs.

- [ ] **Step 3: Implement the three helpers**

Append to `src/zira_dashboard/wc_dashboard_data.py`:

```python
def _station_total_for_wc(wc_name: str, day: date):
    """Return the StationTotal for one WC + day, or None.

    Reads via `cached_leaderboard` (in-process TODAY cache + Postgres
    past-day cache via `_zira_persist`). Both paths return the same
    StationTotal dataclass shape with `.samples`, `.active_intervals`,
    `.units`, `.downtime_minutes`, etc.
    """
    from .deps import client
    from .leaderboard import cached_leaderboard
    from .stations import Station
    loc = _load_wc(wc_name)
    if loc is None or not loc.meter_id:
        return None
    stations = [Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)]
    try:
        results = cached_leaderboard(client, stations, day)
    except Exception:
        return None
    for r in results:
        if r.station.name == wc_name:
            return r
    return None


def _readings_for_wc_today(wc_name: str, day: date) -> list[dict]:
    """Per-event readings for one WC + day, normalized to a list of
    `{ts_utc, units}` dicts. Extracted from StationTotal.samples which
    is a tuple of (datetime, int) pairs.

    Empty list if no meter / no data. Tests can monkeypatch this
    directly instead of stubbing the entire cached_leaderboard chain.
    """
    total = _station_total_for_wc(wc_name, day)
    if total is None:
        return []
    return [
        {"ts_utc": ts, "units": int(units)}
        for (ts, units) in (total.samples or [])
        if ts is not None
    ]


def _wc_target_per_bucket(wc_name: str, day: date) -> int:
    """Target units per 15-min bucket. daily_target / (shift_minutes/15)."""
    from . import work_centers_store, shift_config
    loc = _load_wc(wc_name)
    if loc is None:
        return 0
    full = int(work_centers_store.goal_per_day(loc) or 0)
    shift_minutes = shift_config.productive_minutes_for(day) or 1
    buckets = max(1, shift_minutes // 15)
    return max(0, int(round(full / buckets)))


def _bucket_index(reading_ts, shift_start_utc) -> int:
    """Map an event timestamp to its 15-min bucket from shift-start."""
    from datetime import timedelta
    if not reading_ts or not shift_start_utc:
        return 0
    delta = (reading_ts - shift_start_utc).total_seconds() / 60.0
    if delta < 0:
        return 0
    return int(delta // 15)


def _bucket_count_for_day(day: date) -> int:
    """Number of 15-min buckets in the shift on `day`."""
    from . import shift_config
    return max(1, (shift_config.productive_minutes_for(day) or 0) // 15)


def daily_progress(wc_name: str, day: date) -> list[dict]:
    """Cumulative units per 15-min bucket from shift-start to shift-end.

    Returns a list of {bucket_index, minute_offset, cumulative_units}
    one entry per bucket. Used by the daily-progress SVG chart.
    """
    from datetime import datetime, timezone
    from . import shift_config

    readings = _readings_for_wc_today(wc_name, day)
    n_buckets = _bucket_count_for_day(day)
    shift_start_local = datetime.combine(
        day, shift_config.shift_start_for(day), tzinfo=shift_config.SITE_TZ,
    )
    shift_start_utc = shift_start_local.astimezone(timezone.utc)

    per_bucket = [0] * n_buckets
    for r in readings:
        ts = r.get("ts_utc")
        if ts is None:
            continue
        idx = _bucket_index(ts, shift_start_utc)
        if 0 <= idx < n_buckets:
            per_bucket[idx] += int(r.get("units") or 0)

    cumulative = 0
    out = []
    for i, val in enumerate(per_bucket):
        cumulative += val
        out.append({
            "bucket_index": i,
            "minute_offset": i * 15,
            "cumulative_units": cumulative,
        })
    return out


def fifteen_min_increments(wc_name: str, day: date) -> list[dict]:
    """Per-bucket units + color flag (green ≥ target, amber ≥ 75%, red < 75%).

    Mirrors `daily_progress` but emits per-bucket (not cumulative) units
    and a color-coded status against the per-bucket target.
    """
    from datetime import datetime, timezone
    from . import shift_config

    readings = _readings_for_wc_today(wc_name, day)
    n_buckets = _bucket_count_for_day(day)
    target = _wc_target_per_bucket(wc_name, day)
    shift_start_local = datetime.combine(
        day, shift_config.shift_start_for(day), tzinfo=shift_config.SITE_TZ,
    )
    shift_start_utc = shift_start_local.astimezone(timezone.utc)

    per_bucket = [0] * n_buckets
    for r in readings:
        ts = r.get("ts_utc")
        if ts is None:
            continue
        idx = _bucket_index(ts, shift_start_utc)
        if 0 <= idx < n_buckets:
            per_bucket[idx] += int(r.get("units") or 0)

    def _color(units):
        if target <= 0:
            return "neutral"
        if units >= target:
            return "green"
        if units >= 0.75 * target:
            return "amber"
        return "red"

    return [
        {
            "bucket_index": i,
            "minute_offset": i * 15,
            "units": v,
            "color": _color(v),
            "target": target,
        }
        for i, v in enumerate(per_bucket)
    ]


def _downtime_events_for_wc(wc_name: str, day: date) -> list[dict]:
    """Downtime events derived from gaps in StationTotal.active_intervals.

    Each entry: `{time, duration_minutes}` where `time` is the local
    HH:MMa display of when the down period started. Reason data isn't
    captured by Zira so we don't include it. Intervals are sorted
    chronologically before gap detection.

    Indirection so tests can monkeypatch a fixed list.
    """
    from . import shift_config
    total = _station_total_for_wc(wc_name, day)
    if total is None:
        return []
    intervals = sorted(
        [(a, b) for (a, b) in (total.active_intervals or []) if a and b],
        key=lambda ab: ab[0],
    )
    if not intervals:
        return []
    events: list[dict] = []
    prev_end = intervals[0][1]
    for start, end in intervals[1:]:
        if start > prev_end:
            gap_minutes = int((start - prev_end).total_seconds() // 60)
            if gap_minutes >= 1:
                local = prev_end.astimezone(shift_config.SITE_TZ)
                events.append({
                    "time": local.strftime("%-I:%M%p").lower().replace(":00", "").replace("am", "a").replace("pm", "p"),
                    "duration_minutes": gap_minutes,
                })
        prev_end = max(prev_end, end)
    return events


def downtime_report(wc_name: str, day: date) -> dict:
    """Downtime widget data: {events: [...], total_minutes: int}.

    total_minutes pulls from StationTotal.downtime_minutes (Zira's
    own count); events are derived from active_intervals gaps. The
    two may differ slightly — the total is the authoritative number.
    """
    events = _downtime_events_for_wc(wc_name, day)
    total = _station_total_for_wc(wc_name, day)
    total_minutes = int(total.downtime_minutes) if total else 0
    return {"events": events, "total_minutes": total_minutes}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_wc_dashboard_data.py -v`
Expected: all PASS (16 + 3 = 19 total).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/wc_dashboard_data.py tests/test_wc_dashboard_data.py
git commit -m "feat(wc-dashboard): daily progress + 15-min increments + downtime"
```

---

## Task 4: `wc_dashboard.html` template

**Files:**
- Create: `src/zira_dashboard/templates/wc_dashboard.html`

- [ ] **Step 1: Create the template file**

Create `src/zira_dashboard/templates/wc_dashboard.html`:

```jinja
{# Per-WC dashboard. Two routes share this template:
   - /wc/{slug}        editor view (gridstack enabled, edit buttons visible)
   - /tv/wc/{slug}     TV view (read-only, no chrome, theme = data-tv-theme on <html>)
#}
{% from "_tv_header.html" import tv_header %}
{% from "_goat_badges.html" import goat_badges, goat_badges_css, hover_tip_clamp_script %}
<!doctype html>
<html lang="en"{% if tv_mode %} data-tv-theme="{{ tv_theme or 'dark' }}"{% endif %}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="/static/gpi-logo.png">
<title>{% if tv_mode %}TV · {% endif %}{{ wc_name }} — GPI Plant Manager</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/gridstack@10.3.1/dist/gridstack.min.css">
<link rel="stylesheet" href="/static/wc_dashboard.css?v={{ static_v('wc_dashboard.css') }}">
{% if tv_mode %}
<link rel="stylesheet" href="/static/tv-mode.css?v={{ static_v('tv-mode.css') }}">
<meta http-equiv="refresh" content="60">
{% endif %}
<style>{{ goat_badges_css() }}</style>
</head>
<body>
{{ tv_header(
    wc_name,
    crumb="WORK CENTER · " + (wc_group or "")|upper,
    right=operators_display or "(unassigned)",
) }}

<main>
<div class="grid-stack">

  {# Pallets banner — full width #}
  <div class="grid-stack-item" gs-id="wc-pallets-banner" gs-x="0" gs-y="0" gs-w="12" gs-h="2">
    <div class="grid-stack-item-content">
      <h3>Today · Pallets</h3>
      <div class="pallets-banner">
        <div class="pallets-numbers">
          <span class="units">{{ pallets.units_today }}</span>
          <span class="target">/ {{ pallets.target_today }} goal so far ({{ pallets.target_full_day }} full day)</span>
        </div>
        <div class="bar-track">
          <div class="bar-fill" style="width: {{ (pallets.pct_of_target|float)|round(0)|int if pallets.pct_of_target else 0 }}%"></div>
        </div>
      </div>
    </div>
  </div>

  {# Daily progress chart #}
  <div class="grid-stack-item" gs-id="wc-daily-progress" gs-x="0" gs-y="2" gs-w="6" gs-h="4">
    <div class="grid-stack-item-content">
      <h3>Daily Progress</h3>
      <svg class="daily-progress-chart" viewBox="0 0 200 80" preserveAspectRatio="none">
        {%- set max_y = pallets.target_full_day or 1 -%}
        {%- set n = daily_progress|length or 1 -%}
        {%- set pts = [] -%}
        {%- for p in daily_progress -%}
          {%- set _ = pts.append("%.1f,%.1f"|format(loop.index0 / (n-1 if n>1 else 1) * 200, 80 - (p.cumulative_units / max_y) * 80 if max_y else 80)) -%}
        {%- endfor -%}
        <polyline fill="none" stroke="var(--accent)" stroke-width="1.5" points="{{ pts|join(' ') }}"/>
        <line x1="0" y1="0" x2="200" y2="0" stroke="var(--muted)" stroke-dasharray="3 3" stroke-width="0.5"/>
        <text x="2" y="6" fill="var(--muted)" font-size="6">goal {{ max_y }}</text>
      </svg>
    </div>
  </div>

  {# GOAT race widget #}
  <div class="grid-stack-item" gs-id="wc-goat-race" gs-x="6" gs-y="2" gs-w="3" gs-h="4">
    <div class="grid-stack-item-content">
      <h3>Vs. GOAT Pace</h3>
      <div class="goat-race">
        {% if goat_race.status %}
          <div class="status-pill status-{{ goat_race.status|lower }}">{{ goat_race.status|replace('_', ' ') }}</div>
        {% else %}
          <div class="status-pill status-none">no record yet</div>
        {% endif %}
        <div class="race-stats">
          <div>Today: <b>{{ goat_race.units_today }}</b></div>
          <div>GOAT pace now: <b>{{ goat_race.goat_pace_today|round(0)|int }}</b></div>
          {% if goat_race.goat %}
            <div class="goat-meta">
              🐐 {{ goat_race.goat.name }} · {{ goat_race.goat.units }} on {{ goat_race.goat.day }}
            </div>
          {% endif %}
        </div>
      </div>
    </div>
  </div>

  {# Monthly ribbons #}
  <div class="grid-stack-item" gs-id="wc-monthly-ribbons" gs-x="9" gs-y="2" gs-w="3" gs-h="4">
    <div class="grid-stack-item-content">
      <h3>{{ month_name(month) }} {{ year }} · {{ ribbons.group or 'Ribbons' }}</h3>
      <ul class="ribbons-list">
        {% for r in ribbons.entries %}
          <li>
            <span class="medal">{% if r.position == 1 %}🥇{% elif r.position == 2 %}🥈{% else %}🥉{% endif %}</span>
            <span class="name"><a href="/staffing/people/{{ r.name|urlencode }}">{{ r.name }}</a></span>
            <span class="units">{{ r.units|round(0)|int }}</span>
          </li>
        {% else %}
          <li class="empty">no qualifying days yet</li>
        {% endfor %}
      </ul>
    </div>
  </div>

  {# 15-min increments #}
  <div class="grid-stack-item" gs-id="wc-15min-increments" gs-x="0" gs-y="6" gs-w="8" gs-h="3">
    <div class="grid-stack-item-content">
      <h3>15-min Increments</h3>
      <div class="fifteen-min-bars">
        {% for b in fifteen_min %}
          <div class="bar bar-{{ b.color }}"
               style="--units: {{ b.units }}; --target: {{ b.target }};"
               title="{{ b.units }} units · target {{ b.target }}"></div>
        {% endfor %}
      </div>
    </div>
  </div>

  {# Downtime report #}
  <div class="grid-stack-item" gs-id="wc-downtime-report" gs-x="8" gs-y="6" gs-w="4" gs-h="3">
    <div class="grid-stack-item-content">
      <h3>Downtime · {{ downtime.total_minutes }}m total</h3>
      <ul class="downtime-list">
        {% for e in downtime.events %}
          <li>
            <span class="time">{{ e.time }}</span>
            <span class="duration">{{ e.duration_minutes }}m</span>
          </li>
        {% else %}
          <li class="empty">no downtime</li>
        {% endfor %}
      </ul>
    </div>
  </div>

</div>{# /.grid-stack #}
</main>

<script src="https://cdn.jsdelivr.net/npm/gridstack@10.3.1/dist/gridstack-all.js"></script>
<script>
  const grid = GridStack.init({
    column: 12,
    cellHeight: 80,
    margin: 8,
    float: false,
  });
{% if tv_mode %}
  grid.disable();
{% else %}
  // Autosave layout on drag/resize end.
  function persistLayout() {
    const items = grid.save(false).map(it => ({
      id: it.id,
      x: it.x, y: it.y, w: it.w, h: it.h,
    })).filter(it => it.id);
    fetch('/api/layout/{{ layout_key }}', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(items),
    });
  }
  grid.on('change', persistLayout);
{% endif %}
</script>
{{ hover_tip_clamp_script() }}
</body>
</html>
```

- [ ] **Step 2: Create a tiny stylesheet for the per-WC widgets**

Create `src/zira_dashboard/static/wc_dashboard.css`:

```css
/* Per-WC dashboard layout (screen + TV variants share the same file). */
body { margin: 0; font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; background: var(--bg, #f1f4f7); color: var(--fg, #1f2937); }
main { padding: 12px 16px; }
.grid-stack-item-content {
  background: var(--panel, #fff);
  border: 1px solid var(--border, #d8dee5);
  border-radius: 8px;
  padding: 10px 12px;
  overflow: hidden;
}
.grid-stack-item-content h3 {
  margin: 0 0 8px 0;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 1.2px;
  color: var(--muted, #6b7280);
}
.pallets-banner .units { font-size: 2.4rem; font-weight: 800; color: var(--accent, #16a34a); }
.pallets-banner .target { font-size: 0.9rem; color: var(--muted); margin-left: 6px; }
.pallets-banner .bar-track { height: 14px; background: var(--panel-2, #e3e8ee); border-radius: 7px; overflow: hidden; margin-top: 6px; }
.pallets-banner .bar-fill { height: 100%; background: var(--accent, #16a34a); }
.daily-progress-chart { width: 100%; height: 100%; }
.goat-race .status-pill { display: inline-block; padding: 4px 10px; border-radius: 99px; font-size: 11px; font-weight: 800; letter-spacing: 1px; }
.goat-race .status-pill.status-ahead, .goat-race .status-pill.status-on_pace { background: var(--accent-dim, #dcfce7); color: var(--accent, #16a34a); }
.goat-race .status-pill.status-behind { background: var(--bad-dim, #fee2e2); color: var(--bad, #ef4444); }
.goat-race .status-pill.status-none { background: var(--panel-2, #e3e8ee); color: var(--muted); }
.goat-race .race-stats { margin-top: 8px; font-size: 0.9rem; line-height: 1.5; }
.goat-race .goat-meta { color: var(--muted); font-size: 0.78rem; margin-top: 4px; }
.ribbons-list { list-style: none; padding: 0; margin: 0; }
.ribbons-list li { display: flex; gap: 8px; align-items: center; padding: 4px 0; }
.ribbons-list li.empty { color: var(--muted); font-style: italic; }
.ribbons-list .medal { font-size: 1.1rem; }
.ribbons-list .name { flex: 1; font-weight: 600; }
.ribbons-list .name a { color: inherit; text-decoration: none; }
.ribbons-list .units { font-weight: 700; }
.fifteen-min-bars { display: grid; grid-template-columns: repeat(auto-fit, minmax(8px, 1fr)); gap: 2px; align-items: end; height: calc(100% - 30px); }
.fifteen-min-bars .bar { height: calc(var(--units) / max(var(--target), 1) * 100%); min-height: 4px; border-radius: 2px; }
.fifteen-min-bars .bar-green { background: var(--accent, #16a34a); }
.fifteen-min-bars .bar-amber { background: var(--warn, #f59e0b); }
.fifteen-min-bars .bar-red { background: var(--bad, #ef4444); }
.fifteen-min-bars .bar-neutral { background: var(--panel-3, #cbd5e1); }
.downtime-list { list-style: none; padding: 0; margin: 0; font-size: 0.85rem; }
.downtime-list li { display: flex; gap: 8px; padding: 3px 0; border-bottom: 1px solid var(--border, #e3e8ee); }
.downtime-list li:last-child { border-bottom: none; }
.downtime-list li.empty { color: var(--muted); font-style: italic; }
.downtime-list .time { font-variant-numeric: tabular-nums; color: var(--muted); }
.downtime-list .reason { flex: 1; }
.downtime-list .duration { font-weight: 700; color: var(--bad, #ef4444); }
```

- [ ] **Step 3: Verify the template parses**

Run:
```bash
.venv/Scripts/python.exe -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'), autoescape=True)
env.parse(open('src/zira_dashboard/templates/wc_dashboard.html', encoding='utf-8').read())
print('parse OK')
"
```
Expected: `parse OK`.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/templates/wc_dashboard.html src/zira_dashboard/static/wc_dashboard.css
git commit -m "feat(wc-dashboard): wc_dashboard.html template + wc_dashboard.css"
```

---

## Task 5: Routes — `/wc/{slug}` editor + `/tv/wc/{slug}` TV

**Files:**
- Create: `src/zira_dashboard/routes/wc_dashboard.py`
- Modify: `src/zira_dashboard/app.py` (register the router)

- [ ] **Step 1: Create the route module**

Create `src/zira_dashboard/routes/wc_dashboard.py`:

```python
"""Per-WC dashboard routes.

  /wc/{slug}        editor view (gridstack enabled, autosave on)
  /tv/wc/{slug}     TV view (read-only, no chrome, theme via ?theme=)

Both delegate to a single `_render_wc_dashboard` helper that composes
the data prep from `wc_dashboard_data`, looks up the saved widget
layout, and renders `wc_dashboard.html`. The helper owns the HTTP
response cache key (which includes tv_mode + tv_theme so screen and
TV variants stay separate) and the per-WC slug lookup.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import layout_store, wc_dashboard_data, work_centers_store
from ..deps import templates

router = APIRouter()


def _render_wc_dashboard(
    request: Request,
    *,
    slug: str,
    tv_mode: bool,
    tv_theme: str,
):
    """Shared implementation for the editor + TV routes."""
    loc = wc_dashboard_data.wc_by_slug(slug)
    if loc is None:
        return JSONResponse({"error": f"no work center matches slug {slug!r}"}, status_code=404)

    today = datetime.now(timezone.utc).date()
    wc_name = loc.name
    operators = wc_dashboard_data.assigned_operators_for_wc(wc_name, today)
    operators_display = " · ".join(operators)
    groups = work_centers_store.groups(loc) or []
    wc_group = groups[0] if groups else None

    pallets = wc_dashboard_data.pallets_banner(wc_name, today)
    daily_progress = wc_dashboard_data.daily_progress(wc_name, today)
    goat_race = wc_dashboard_data.goat_race(wc_name, today)
    ribbons = wc_dashboard_data.monthly_ribbons(wc_name, today.year, today.month)
    fifteen_min = wc_dashboard_data.fifteen_min_increments(wc_name, today)
    downtime = wc_dashboard_data.downtime_report(wc_name, today)

    layout_key = f"wc:{slug}"

    return templates.TemplateResponse(
        request,
        "wc_dashboard.html",
        {
            "slug": slug,
            "wc_name": wc_name,
            "wc_group": wc_group,
            "operators": operators,
            "operators_display": operators_display,
            "today": today.isoformat(),
            "year": today.year,
            "month": today.month,
            "pallets": pallets,
            "daily_progress": daily_progress,
            "goat_race": goat_race,
            "ribbons": ribbons,
            "fifteen_min": fifteen_min,
            "downtime": downtime,
            "layout": layout_store.layout_map(layout_key),
            "layout_key": layout_key,
            "tv_mode": tv_mode,
            "tv_theme": tv_theme,
        },
    )


@router.get("/wc/{slug}", response_class=HTMLResponse)
def wc_dashboard(request: Request, slug: str):
    """Per-WC dashboard editor view. Drag / resize widgets; layout
    autosaves to `widget_layouts.page = 'wc:{slug}'`."""
    return _render_wc_dashboard(
        request,
        slug=slug,
        tv_mode=False,
        tv_theme="dark",
    )


@router.get("/tv/wc/{slug}", response_class=HTMLResponse)
def tv_wc_dashboard(
    request: Request,
    slug: str,
    theme: str | None = Query(default=None),
):
    """Per-WC TV view. Same widgets, no chrome, no drag, auto-refresh.
    `?theme=light` overrides the default dark.
    """
    tv_theme = "light" if theme == "light" else "dark"
    return _render_wc_dashboard(
        request,
        slug=slug,
        tv_mode=True,
        tv_theme=tv_theme,
    )
```

- [ ] **Step 2: Register the router in app.py**

Open `src/zira_dashboard/app.py`. Find the existing router-include block (search for `app.include_router(`). Add the `wc_dashboard` import and the include line.

In the import block near the top (where `from .routes import (admin, api_layout, ...)` lives), add `wc_dashboard` to the alphabetical list:

```python
from .routes import (
    admin,
    api_layout,
    changelog,
    dashboard,
    late_report,
    leaderboards,
    past_schedules,
    people,
    settings,
    share,
    skills,
    staffing,
    time_off,
    trophies,
    value_streams,
    wc_dashboard,
)
```

Then add the include line near the bottom alongside the other includes (near `app.include_router(value_streams.router)`):

```python
app.include_router(wc_dashboard.router)
```

- [ ] **Step 3: Smoke test**

Run:
```bash
.venv/Scripts/python.exe -c "
from zira_dashboard.app import app
routes = sorted({r.path for r in app.routes if hasattr(r, 'path')})
assert '/wc/{slug}' in routes
assert '/tv/wc/{slug}' in routes
print('routes registered OK')
"
```
Expected: `routes registered OK`.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/routes/wc_dashboard.py src/zira_dashboard/app.py
git commit -m "feat(wc-dashboard): /wc/{slug} editor + /tv/wc/{slug} TV routes"
```

---

## Task 6: Integration tests + CHANGELOG + push

**Files:**
- Create: `tests/test_wc_dashboard.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write integration tests**

Create `tests/test_wc_dashboard.py`:

```python
"""Integration tests for the per-WC dashboard routes.

Mirrors the test_dashboards_polish.py pattern: TestClient + monkeypatch
of the data-source helpers so the test doesn't need live Zira / Odoo.
"""
from __future__ import annotations

import os
from datetime import date as _date
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; wc-dashboard tests need Postgres",
)


def _stub_wc(monkeypatch):
    """Make `wc_by_slug` return a fake Location for slug 'repair-1'."""
    from zira_dashboard import wc_dashboard_data, work_centers_store

    class _Loc:
        name = "Repair 1"
        meter_id = "meter-1"
        skill = "Repair"
        bay = "Bay 1"

    fake = _Loc()
    monkeypatch.setattr(wc_dashboard_data, "wc_by_slug", lambda s: fake if s == "repair-1" else None)
    monkeypatch.setattr(work_centers_store, "groups", lambda loc: ["Repairs"])
    monkeypatch.setattr(work_centers_store, "goal_per_day", lambda loc: 200)
    monkeypatch.setattr(wc_dashboard_data, "assigned_operators_for_wc",
                        lambda nm, d: ["Christian", "Jose L"])
    monkeypatch.setattr(wc_dashboard_data, "pallets_banner",
                        lambda nm, d: {"units_today": 87, "target_today": 100, "target_full_day": 200, "pct_of_target": 87.0})
    monkeypatch.setattr(wc_dashboard_data, "daily_progress", lambda nm, d: [])
    monkeypatch.setattr(wc_dashboard_data, "goat_race",
                        lambda nm, d: {"group": "Repairs", "goat": None, "units_today": 87, "goat_pace_today": 0, "status": None})
    monkeypatch.setattr(wc_dashboard_data, "monthly_ribbons",
                        lambda nm, y, m: {"group": "Repairs", "entries": []})
    monkeypatch.setattr(wc_dashboard_data, "fifteen_min_increments", lambda nm, d: [])
    monkeypatch.setattr(wc_dashboard_data, "downtime_report",
                        lambda nm, d: {"events": [], "total_minutes": 0})


def test_editor_route_renders_with_drag(monkeypatch):
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/wc/repair-1")
    assert r.status_code == 200
    # Editor: not in tv_mode, no data-tv-theme, no tv-mode.css link.
    assert "data-tv-theme" not in r.text
    assert "/static/tv-mode.css" not in r.text
    # Header renders the WC name + operator list.
    assert "Repair 1" in r.text
    assert "Christian · Jose L" in r.text
    # All 6 widget IDs present.
    for wid in ("wc-pallets-banner", "wc-daily-progress", "wc-goat-race",
                "wc-monthly-ribbons", "wc-15min-increments", "wc-downtime-report"):
        assert wid in r.text


def test_tv_route_renders_with_dark_theme_and_no_chrome(monkeypatch):
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/tv/wc/repair-1")
    assert r.status_code == 200
    assert 'data-tv-theme="dark"' in r.text
    assert "/static/tv-mode.css" in r.text
    assert 'http-equiv="refresh"' in r.text
    # Same widgets present.
    assert "wc-pallets-banner" in r.text


def test_tv_route_supports_light_theme_via_query(monkeypatch):
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/tv/wc/repair-1?theme=light")
    assert r.status_code == 200
    assert 'data-tv-theme="light"' in r.text


def test_unknown_slug_returns_404(monkeypatch):
    from zira_dashboard import wc_dashboard_data
    monkeypatch.setattr(wc_dashboard_data, "wc_by_slug", lambda s: None)
    c = TestClient(app)
    r = c.get("/wc/ghost")
    assert r.status_code == 404
    r2 = c.get("/tv/wc/ghost")
    assert r2.status_code == 404


def test_unassigned_wc_renders_with_placeholder(monkeypatch):
    _stub_wc(monkeypatch)
    from zira_dashboard import wc_dashboard_data
    monkeypatch.setattr(wc_dashboard_data, "assigned_operators_for_wc", lambda nm, d: [])
    c = TestClient(app)
    r = c.get("/tv/wc/repair-1")
    assert r.status_code == 200
    assert "(unassigned)" in r.text
```

- [ ] **Step 2: Run tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_wc_dashboard.py tests/test_wc_dashboard_data.py -v`
Expected: 5 integration tests SKIP without DATABASE_URL; 19 data-prep tests PASS.

Also run the full suite to confirm no regression:
```bash
.venv/Scripts/python.exe -m pytest 2>&1 | tail -3
```
Expected: same pass count as before plus the new tests.

- [ ] **Step 3: Get current time**

Run: `powershell.exe -Command "Get-Date -Format 'h:mm tt'"`
Note the result for the CHANGELOG header.

- [ ] **Step 4: Add CHANGELOG entry**

In `CHANGELOG.md`, insert a new `### <HH:MM TT>` block under today's `## 2026-05-13` heading (above the most recent entry):

```markdown
### <HH:MM TT>

- **Per-work-center TV dashboards** — every WC can now have its own URL on a TV mounted at the workstation. Editor at `/wc/{slug}` (drag/resize the six widgets, layout auto-saves); TV view at `/tv/wc/{slug}` (read-only, no chrome, `?theme=light` for a bright-area TV, 60 s auto-refresh). Widgets: pallets banner (today's count vs prorated goal for THIS WC), daily progress chart (cumulative 15-min buckets), GOAT race (status pill + WC group's all-time GOAT pace), monthly ribbons (group's top-3 person-days), 15-min increments (color-coded green/amber/red), downtime report (events + total minutes). Header shows WC name top-left + assigned operator names top-right (only the people scheduled for THIS WC, not the whole group). Sub-project 2 of 4 in the TV-dashboards spec; templates + the Settings panel still to follow.
```

- [ ] **Step 5: Commit + push**

```bash
git add tests/test_wc_dashboard.py CHANGELOG.md
git commit -m "test(wc-dashboard) + docs(changelog): per-WC TV dashboards"
git push origin main
```

Railway picks up the push and redeploys. After deploy, hit `https://gpiplantmanager.com/wc/repair-1` (editor) and `https://gpiplantmanager.com/tv/wc/repair-1` (TV) to verify.

---

## Done

`/wc/{slug}` (editor, drag/resize widgets, layout auto-saves) and `/tv/wc/{slug}` (TV, read-only, theme via `?theme=light`) ship. Layouts persist per-WC in `widget_layouts` keyed `wc:{slug}` so each WC can be arranged independently. The Settings → TV Displays panel (sub-project 4) will later add a UI to manage the list of TVs and persist theme per row; for now bookmark the URL directly.

If a future widget is needed (e.g., shift-elapsed indicator, daily best individual), follow the existing pattern:
- Add a helper to `wc_dashboard_data.py` that takes `(wc_name, day)` and returns a dict
- Add the data lookup + context key in `routes/wc_dashboard.py`'s `_render_wc_dashboard`
- Add a new `<div class="grid-stack-item">` to `wc_dashboard.html`
