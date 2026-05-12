# Best Averages Leaderboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Best Averages" column to the leaderboards page so each WC/group shows both the existing top-5 single-day records (left) and a per-person averages table (right) over the active date range.

**Architecture:** Two pure compute helpers (`averages_for_wc`, `averages_for_group`) added to `routes/leaderboards.py` produce per-person rows from already-fetched `daily_records`. The route handler emits parallel context keys for the averages side. Each WC/group section in the template is wrapped in a CSS-grid row with two cells (`.lb-side-days`, `.lb-side-avg`) and a shared header row, so drag/hide already keep working unchanged. Best Averages tables get a `▼ Show all (N)` toggle that flips a `lb-row-hidden` class on rows beyond the top 5.

**Tech Stack:** FastAPI, Jinja2, Python 3.11+, vanilla JS, CSS grid (`grid-template-areas`).

**Spec:** `docs/superpowers/specs/2026-04-30-best-averages-leaderboard-design.md`

---

## File Touch Map

- `src/zira_dashboard/routes/leaderboards.py` — add `averages_for_wc`, `averages_for_group` module-level helpers; populate four new context keys in the GET handler; pre-build `target_per_hour_by_wc` once.
- `src/zira_dashboard/templates/leaderboards.html` — restructure each WC/group block into `.lb-section.lb-section-row` containing a header row + `.lb-side-days` + `.lb-side-avg`. Add `toggleAll(btn)` JS.
- `src/zira_dashboard/static/leaderboards.css` — grid layout for `.lb-section-row`, switch `.lb-active-list` / `.lb-inactive-content` from card-grid to vertical stack, add `.lb-row-hidden`, `.lb-expand-btn`, responsive `<900px` collapse.
- `tests/test_leaderboards_avg.py` — new file. Pure unit tests for the two helpers (no DB).

No new endpoints. No DB migrations. No new dependencies.

---

## Task 1: Pure helper — `averages_for_wc`

**Files:**
- Modify: `src/zira_dashboard/routes/leaderboards.py` (add module-level helper)
- Test: `tests/test_leaderboards_avg.py` (new)

This is the core per-WC averaging function. Pure: takes records + a target + a per-day productive-minutes callable, returns sorted rows. The route handler later wires in the real callables.

- [ ] **Step 1: Write failing tests**

Create `tests/test_leaderboards_avg.py`:

```python
"""Unit tests for the pure averages helpers in routes/leaderboards.py.

These tests don't need Postgres — the helpers are dependency-injected
with a fake `productive_minutes_for` callable and explicit targets.
"""
from datetime import date

from zira_dashboard.routes.leaderboards import averages_for_wc


# A 7h productive day at every date — keeps math simple in tests.
def _const_productive(_day):
    return 7 * 60  # 420 min = 7h


def _rec(d, person, wc, units):
    return {"day": d, "person": person, "wc": wc, "units": units,
            "downtime": 0.0, "hours": 7.0}


def test_averages_single_person_multiple_days():
    target_per_hour = 30.0  # 7h * 30 = 210 expected per day
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1", 200),
        _rec(date(2026, 4, 28), "Alice", "WC1", 220),
        _rec(date(2026, 4, 29), "Alice", "WC1", 210),
    ]
    rows = averages_for_wc(records, target_per_hour, _const_productive, "units")
    assert len(rows) == 1
    r = rows[0]
    assert r["rank"] == 1
    assert r["name"] == "Alice"
    assert r["name_count"] == 3
    assert r["avg_units"] == 210.0
    # avg_pct = mean of (200/210, 220/210, 210/210)
    assert abs(r["avg_pct"] - (200/210 + 220/210 + 210/210) / 3) < 1e-9


def test_averages_sort_by_units_desc():
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1", 100),
        _rec(date(2026, 4, 27), "Bob",   "WC1", 300),
        _rec(date(2026, 4, 28), "Bob",   "WC1", 300),
    ]
    rows = averages_for_wc(records, 30.0, _const_productive, "units")
    assert [r["name"] for r in rows] == ["Bob", "Alice"]
    assert rows[0]["rank"] == 1
    assert rows[1]["rank"] == 2


def test_averages_sort_by_pct_desc():
    # Alice: avg 100 units/day, pct = 100/210 ≈ 0.476
    # Bob:   avg 200 units/day, pct = 200/210 ≈ 0.952
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1", 100),
        _rec(date(2026, 4, 27), "Bob",   "WC1", 200),
    ]
    rows = averages_for_wc(records, 30.0, _const_productive, "pct")
    assert [r["name"] for r in rows] == ["Bob", "Alice"]


def test_averages_tiebreak_more_days_ranks_higher():
    # Both average 200 units/day. Alice worked more days → ranks higher.
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1", 200),
        _rec(date(2026, 4, 28), "Alice", "WC1", 200),
        _rec(date(2026, 4, 29), "Alice", "WC1", 200),
        _rec(date(2026, 4, 27), "Bob",   "WC1", 200),
    ]
    rows = averages_for_wc(records, 30.0, _const_productive, "units")
    assert [r["name"] for r in rows] == ["Alice", "Bob"]


def test_averages_zero_unit_records_filtered():
    # Days where units=0 (e.g., time off) should NOT drag down the average.
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1", 200),
        _rec(date(2026, 4, 28), "Alice", "WC1", 0),
    ]
    rows = averages_for_wc(records, 30.0, _const_productive, "units")
    assert rows[0]["avg_units"] == 200.0
    assert rows[0]["name_count"] == 1


def test_averages_custom_hours_shrinks_expected():
    # Day 1 is a 4h day, day 2 is the standard 7h day.
    def productive_per_day(d):
        if d == date(2026, 4, 27):
            return 4 * 60
        return 7 * 60

    target_per_hour = 30.0
    # Alice did 120 on a 4h day → pct = 120 / (30*4) = 1.0
    # Alice did 210 on a 7h day → pct = 210 / (30*7) = 1.0
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1", 120),
        _rec(date(2026, 4, 28), "Alice", "WC1", 210),
    ]
    rows = averages_for_wc(records, target_per_hour, productive_per_day, "pct")
    assert abs(rows[0]["avg_pct"] - 1.0) < 1e-9


def test_averages_empty_records_returns_empty_list():
    assert averages_for_wc([], 30.0, _const_productive, "units") == []


def test_averages_zero_target_yields_zero_pct():
    records = [_rec(date(2026, 4, 27), "Alice", "WC1", 200)]
    rows = averages_for_wc(records, 0.0, _const_productive, "pct")
    assert rows[0]["avg_pct"] == 0.0
    assert rows[0]["avg_units"] == 200.0  # units math still works
```

- [ ] **Step 2: Run tests — confirm fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_leaderboards_avg.py -v`

Expected: ImportError or `cannot import name 'averages_for_wc'` — the function doesn't exist yet.

- [ ] **Step 3: Implement `averages_for_wc`**

In `src/zira_dashboard/routes/leaderboards.py`, ABOVE the `router = APIRouter()` line (so it's module-level and importable from tests), add:

```python
def averages_for_wc(
    records: list[dict],
    target_per_hour: float,
    productive_minutes_for,
    mode: str,
) -> list[dict]:
    """Per-person averages across the records (already filtered to one WC).

    `records` is a list of dicts with keys: day, person, wc, units,
    downtime, hours — same shape as production_history.daily_records().

    `target_per_hour` is the hourly target for this WC.

    `productive_minutes_for(day)` returns productive minutes for that day,
    honoring per-day custom_hours. Inject shift_config.productive_minutes_for.

    `mode` is 'units' or 'pct' — drives the sort.

    Returns rows sorted by the active metric desc, with rank assigned.
    Days where the operator earned zero units are excluded so they don't
    drag down the average. Tiebreak: more days_worked ranks higher.
    """
    rows = [r for r in records if r["units"] > 0]
    by_person: dict[str, list[dict]] = {}
    for r in rows:
        by_person.setdefault(r["person"], []).append(r)

    out: list[dict] = []
    for person, recs in by_person.items():
        days_worked = len(recs)
        total_units = sum(r["units"] for r in recs)
        avg_units = total_units / days_worked

        pct_per_day: list[float] = []
        for r in recs:
            prod_hr = productive_minutes_for(r["day"]) / 60.0
            expected = target_per_hour * prod_hr
            pct_per_day.append((r["units"] / expected) if expected > 0 else 0.0)
        avg_pct = sum(pct_per_day) / len(pct_per_day) if pct_per_day else 0.0

        out.append({
            "name": person,
            "name_count": days_worked,
            "avg_units": avg_units,
            "avg_pct": avg_pct,
        })

    if mode == "pct":
        out.sort(key=lambda r: (-r["avg_pct"], -r["name_count"], r["name"].lower()))
    else:
        out.sort(key=lambda r: (-r["avg_units"], -r["name_count"], r["name"].lower()))

    for i, row in enumerate(out, 1):
        row["rank"] = i
    return out
```

- [ ] **Step 4: Run tests — confirm pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_leaderboards_avg.py -v`

Expected: 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/routes/leaderboards.py tests/test_leaderboards_avg.py
git commit -m "Add averages_for_wc helper for Best Averages leaderboard"
```

---

## Task 2: Pure helper — `averages_for_group`

**Files:**
- Modify: `src/zira_dashboard/routes/leaderboards.py` (add module-level helper next to `averages_for_wc`)
- Test: `tests/test_leaderboards_avg.py` (extend)

Group averages span multiple WCs in a category. Each (person, day, wc) record is one sample, expected differs by WC. Output adds a `top_wc` column = the WC the operator most often worked in the range.

- [ ] **Step 1: Append failing tests**

Append to `tests/test_leaderboards_avg.py`:

```python
from zira_dashboard.routes.leaderboards import averages_for_group


def test_group_averages_basic_two_wcs():
    target_by_wc = {"Repair-1": 30.0, "Repair-2": 25.0}
    # Alice: 2 days at Repair-1 (210 each), 1 day at Repair-2 (175).
    # Repair-1 expected = 210, Repair-2 expected = 175.
    records = [
        _rec(date(2026, 4, 27), "Alice", "Repair-1", 210),
        _rec(date(2026, 4, 28), "Alice", "Repair-1", 210),
        _rec(date(2026, 4, 29), "Alice", "Repair-2", 175),
    ]
    rows = averages_for_group(records, target_by_wc, _const_productive, "units")
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "Alice"
    assert r["name_count"] == 3  # total person-days across the group
    assert r["avg_units"] == (210 + 210 + 175) / 3
    # All three days were exactly at goal → pct = 1.0
    assert abs(r["avg_pct"] - 1.0) < 1e-9
    assert r["top_wc"] == "Repair-1"  # 2 days vs 1


def test_group_averages_top_wc_alphabetical_tiebreak():
    # Alice worked Repair-1 once and Repair-2 once → tie, alphabetical first wins.
    target_by_wc = {"Repair-1": 30.0, "Repair-2": 30.0}
    records = [
        _rec(date(2026, 4, 27), "Alice", "Repair-2", 100),
        _rec(date(2026, 4, 28), "Alice", "Repair-1", 100),
    ]
    rows = averages_for_group(records, target_by_wc, _const_productive, "units")
    assert rows[0]["top_wc"] == "Repair-1"


def test_group_averages_sort_and_rank():
    target_by_wc = {"WC1": 30.0}
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1", 100),
        _rec(date(2026, 4, 27), "Bob",   "WC1", 300),
    ]
    rows = averages_for_group(records, target_by_wc, _const_productive, "units")
    assert [r["name"] for r in rows] == ["Bob", "Alice"]
    assert rows[0]["rank"] == 1


def test_group_averages_unknown_wc_target_yields_zero_pct_for_that_record():
    # If a record's WC isn't in target_by_wc, treat its expected as 0 and pct as 0
    # (don't crash). Units math is unaffected.
    target_by_wc = {"WC1": 30.0}
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1",      210),  # pct = 1.0
        _rec(date(2026, 4, 28), "Alice", "WC-Other", 100),  # pct = 0.0
    ]
    rows = averages_for_group(records, target_by_wc, _const_productive, "pct")
    # avg_pct = (1.0 + 0.0) / 2
    assert abs(rows[0]["avg_pct"] - 0.5) < 1e-9
    assert rows[0]["avg_units"] == 155.0


def test_group_averages_filters_zero_unit_records():
    target_by_wc = {"WC1": 30.0}
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1", 200),
        _rec(date(2026, 4, 28), "Alice", "WC1", 0),
    ]
    rows = averages_for_group(records, target_by_wc, _const_productive, "units")
    assert rows[0]["name_count"] == 1
    assert rows[0]["avg_units"] == 200.0
```

- [ ] **Step 2: Run tests — confirm new ones fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_leaderboards_avg.py -v`

Expected: 5 new tests fail with import error for `averages_for_group`.

- [ ] **Step 3: Implement `averages_for_group`**

In `src/zira_dashboard/routes/leaderboards.py`, immediately AFTER `averages_for_wc`:

```python
def averages_for_group(
    records: list[dict],
    target_per_hour_by_wc: dict[str, float],
    productive_minutes_for,
    mode: str,
) -> list[dict]:
    """Per-person averages across a group's WCs.

    Each (person, day, wc) record is one sample. `expected` for the
    pct math is computed per record using that record's WC target.
    `top_wc` = the WC the operator most often worked in the range
    (highest day count); ties broken by WC name alphabetical.

    Same filtering, sorting, and tiebreak rules as averages_for_wc.
    """
    rows = [r for r in records if r["units"] > 0]
    by_person: dict[str, list[dict]] = {}
    for r in rows:
        by_person.setdefault(r["person"], []).append(r)

    out: list[dict] = []
    for person, recs in by_person.items():
        days_worked = len(recs)
        total_units = sum(r["units"] for r in recs)
        avg_units = total_units / days_worked

        pct_per_day: list[float] = []
        wc_counts: dict[str, int] = {}
        for r in recs:
            wc_counts[r["wc"]] = wc_counts.get(r["wc"], 0) + 1
            prod_hr = productive_minutes_for(r["day"]) / 60.0
            target = target_per_hour_by_wc.get(r["wc"], 0.0)
            expected = target * prod_hr
            pct_per_day.append((r["units"] / expected) if expected > 0 else 0.0)
        avg_pct = sum(pct_per_day) / len(pct_per_day) if pct_per_day else 0.0

        # top_wc: highest count; tiebreak alphabetical by WC name.
        top_wc = min(wc_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]

        out.append({
            "name": person,
            "name_count": days_worked,
            "top_wc": top_wc,
            "avg_units": avg_units,
            "avg_pct": avg_pct,
        })

    if mode == "pct":
        out.sort(key=lambda r: (-r["avg_pct"], -r["name_count"], r["name"].lower()))
    else:
        out.sort(key=lambda r: (-r["avg_units"], -r["name_count"], r["name"].lower()))

    for i, row in enumerate(out, 1):
        row["rank"] = i
    return out
```

- [ ] **Step 4: Run tests — confirm pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_leaderboards_avg.py -v`

Expected: 13 tests pass total (8 from Task 1 + 5 from Task 2).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/routes/leaderboards.py tests/test_leaderboards_avg.py
git commit -m "Add averages_for_group helper with top_wc column"
```

---

## Task 3: Wire helpers into the GET handler

**Files:**
- Modify: `src/zira_dashboard/routes/leaderboards.py:46-194` (the GET handler)

After this task the route emits four new context keys (`active_avg_sections`, `inactive_avg_sections`, `active_avg_groups`, `inactive_avg_groups`) parallel to the existing top-5 keys, but the template doesn't consume them yet — so the page should render unchanged.

- [ ] **Step 1: Add imports + per-WC averages call**

Find this block in `routes/leaderboards.py` (currently around lines 53-103, the `# Per-WC top-5 computation.` loop). Right AFTER that loop ends and BEFORE the `# Stable secondary sort by LOCATIONS index` line, insert the per-WC averages computation.

The current loop body computes, for each `loc`, both `target_per_day` and a `wc_records` slice. We re-use those in the averages pass to avoid double-fetching. Restructure the loop to also build avg sections in the same pass — it's cleaner than a second loop.

Replace the existing `# Per-WC top-5 computation.` loop (lines 53-103, the `for loc in staffing.LOCATIONS:` block ending with `sections.append(...)`) with this version that ALSO builds avg_sections:

```python
    # Per-WC top-5 (best days) + per-WC averages computation.
    from .. import shift_config

    sections = []
    avg_sections = []
    for loc in staffing.LOCATIONS:
        station = Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)
        target_per_day = settings_store.station_target_per_day(station)
        target_per_hour = settings_store.station_target(station)
        wc_records = [r for r in records if r["wc"] == loc.name]

        # --- Best Days (existing top-5) ---
        def metric_value(r):
            if metric == "units":
                return r["units"]
            if target_per_day <= 0:
                return 0.0
            return r["units"] / target_per_day

        wc_records.sort(key=lambda r: (-metric_value(r), r["day"]))
        top = wc_records[:5]

        name_counts: dict[str, int] = {}
        for r in wc_records:
            name_counts[r["person"]] = name_counts.get(r["person"], 0) + 1

        rows = []
        for i, r in enumerate(top, start=1):
            day = r["day"]
            day_label = f"{day.strftime('%a')} {day.month}/{day.day}"
            expected = target_per_day
            pct = (r["units"] / expected) if expected > 0 else 0.0
            rows.append({
                "rank": i,
                "name": r["person"],
                "name_count": name_counts.get(r["person"], 0),
                "day": day.isoformat(),
                "day_label": day_label,
                "units": r["units"],
                "expected": expected,
                "pct": pct,
            })

        wc_settings = wc_settings_dict.get(loc.name, {"sort_order": 0, "is_inactive": False})
        # auto_inactive: empty when there's no production AT ALL for the WC in the
        # range. Both halves use the same flag — they share a row.
        auto_inactive = not wc_records
        sections.append({
            "loc_name": loc.name,
            "rows": rows,
            "is_inactive": wc_settings["is_inactive"] or auto_inactive,
            "is_manually_inactive": wc_settings["is_inactive"],
            "is_auto_empty": auto_inactive and not wc_settings["is_inactive"],
            "sort_order": wc_settings["sort_order"],
        })

        # --- Best Averages (new) ---
        avg_rows = averages_for_wc(
            wc_records, target_per_hour, shift_config.productive_minutes_for, metric,
        )
        avg_sections.append({
            "loc_name": loc.name,
            "rows": avg_rows,
            "is_inactive": wc_settings["is_inactive"] or auto_inactive,
            "is_manually_inactive": wc_settings["is_inactive"],
            "is_auto_empty": auto_inactive and not wc_settings["is_inactive"],
            "sort_order": wc_settings["sort_order"],
        })
```

- [ ] **Step 2: Sort and split avg_sections**

Find the existing `# Stable secondary sort by LOCATIONS index` block (currently lines 105-109):

```python
    # Stable secondary sort by LOCATIONS index (bay-organized natural order).
    loc_index = {loc.name: i for i, loc in enumerate(staffing.LOCATIONS)}
    sort_key = lambda s: (s["sort_order"], loc_index.get(s["loc_name"], 999))
    active_sections = sorted([s for s in sections if not s["is_inactive"]], key=sort_key)
    inactive_sections = sorted([s for s in sections if s["is_inactive"]], key=sort_key)
```

Add two more lines AFTER it:

```python
    active_avg_sections = sorted([s for s in avg_sections if not s["is_inactive"]], key=sort_key)
    inactive_avg_sections = sorted([s for s in avg_sections if s["is_inactive"]], key=sort_key)
```

- [ ] **Step 3: Add per-group averages computation**

Find the `# Per-group top-5 computation.` block (currently lines 111-163, the `for group_name in work_centers_store.registered_groups():` loop ending with `group_sections.append(...)`). Replace that loop with this version:

```python
    # Per-group top-5 (best days) + per-group averages computation.
    group_sections = []
    avg_group_sections = []
    for group_name in work_centers_store.registered_groups():
        member_locs = work_centers_store.members("group", group_name)
        member_names = {loc.name for loc in member_locs}
        if not member_names:
            continue
        g_records = [r for r in records if r["wc"] in member_names]
        target_by_wc = {
            loc.name: settings_store.station_target_per_day(
                Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)
            )
            for loc in member_locs
        }
        target_per_hour_by_wc = {
            loc.name: settings_store.station_target(
                Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)
            )
            for loc in member_locs
        }

        def metric_value_g(r, _target_by_wc=target_by_wc):
            if metric == "units":
                return r["units"]
            t = _target_by_wc.get(r["wc"], 0)
            return (r["units"] / t) if t > 0 else 0.0

        g_records.sort(key=lambda r: (-metric_value_g(r), r["day"]))
        top = g_records[:5]

        counts: dict[str, int] = {}
        for r in g_records:
            counts[r["person"]] = counts.get(r["person"], 0) + 1

        rows = []
        for i, r in enumerate(top, start=1):
            day = r["day"]
            target = target_by_wc.get(r["wc"], 0)
            rows.append({
                "rank": i,
                "name": r["person"],
                "name_count": counts.get(r["person"], 0),
                "day": day.isoformat(),
                "day_label": f"{day.strftime('%a')} {day.month}/{day.day}",
                "wc": r["wc"],
                "units": r["units"],
                "pct": (r["units"] / target) if target > 0 else 0.0,
            })

        g_set = group_settings_dict.get(group_name, {"sort_order": 0, "is_inactive": False})
        auto_inactive = not g_records
        group_sections.append({
            "loc_name": group_name,
            "rows": rows,
            "is_inactive": g_set["is_inactive"] or auto_inactive,
            "is_manually_inactive": g_set["is_inactive"],
            "is_auto_empty": auto_inactive and not g_set["is_inactive"],
            "sort_order": g_set["sort_order"],
        })

        # --- Best Averages for this group (new) ---
        avg_rows = averages_for_group(
            g_records, target_per_hour_by_wc, shift_config.productive_minutes_for, metric,
        )
        avg_group_sections.append({
            "loc_name": group_name,
            "rows": avg_rows,
            "is_inactive": g_set["is_inactive"] or auto_inactive,
            "is_manually_inactive": g_set["is_inactive"],
            "is_auto_empty": auto_inactive and not g_set["is_inactive"],
            "sort_order": g_set["sort_order"],
        })
```

- [ ] **Step 4: Sort and split avg_group_sections**

Find the existing block:

```python
    active_groups = sorted(
        [s for s in group_sections if not s["is_inactive"]],
        key=lambda s: s["sort_order"],
    )
    inactive_groups = sorted(
        [s for s in group_sections if s["is_inactive"]],
        key=lambda s: s["sort_order"],
    )
```

Add two more sorts AFTER it:

```python
    active_avg_groups = sorted(
        [s for s in avg_group_sections if not s["is_inactive"]],
        key=lambda s: s["sort_order"],
    )
    inactive_avg_groups = sorted(
        [s for s in avg_group_sections if s["is_inactive"]],
        key=lambda s: s["sort_order"],
    )
```

- [ ] **Step 5: Add the new keys + name-lookup dicts to the template context**

Find the `templates.TemplateResponse(...)` call. The current context dict starts with `"active": "leaderboards"` and ends with `"person_certs": person_certs`. Build name-keyed dicts (so the template can look up the matching averages section by `loc_name` without iterating), then add them plus the four list keys before `"window": window`:

```python
    avg_sections_by_name = {s["loc_name"]: s for s in (active_avg_sections + inactive_avg_sections)}
    avg_groups_by_name = {s["loc_name"]: s for s in (active_avg_groups + inactive_avg_groups)}
```

Then update the context dict:

```python
            "active_sections": active_sections,
            "inactive_sections": inactive_sections,
            "active_groups": active_groups,
            "inactive_groups": inactive_groups,
            "active_avg_sections": active_avg_sections,
            "inactive_avg_sections": inactive_avg_sections,
            "active_avg_groups": active_avg_groups,
            "inactive_avg_groups": inactive_avg_groups,
            "avg_sections_by_name": avg_sections_by_name,
            "avg_groups_by_name": avg_groups_by_name,
            "window": window,
```

- [ ] **Step 6: Smoke check — page still renders**

Run: `.venv\Scripts\python.exe -m uvicorn zira_dashboard.app:app --port 8001` (background).

Hit `http://localhost:8001/staffing/leaderboards` in a browser. Expected: identical to before — the new context keys exist but the template doesn't consume them yet, so visually nothing changed. Stop the server.

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/routes/leaderboards.py
git commit -m "Wire averages helpers into leaderboards GET handler"
```

---

## Task 4: Template — two-column row scaffold (Best Days inside `.lb-side-days`)

**Files:**
- Modify: `src/zira_dashboard/templates/leaderboards.html`

This task does NOT add the Best Averages tables yet. It just restructures each WC/group section so the existing Best Days table lives inside a `.lb-side-days` div, with an empty `.lb-side-avg` placeholder. The shared header row stays at the top. After this task the page looks essentially the same except each Best Days table is now wrapped in an extra div.

- [ ] **Step 1: Restructure the per-group active section**

In `templates/leaderboards.html`, find the active groups loop (currently the `{% for s in active_groups %}` block, ~lines 36-66). Replace its inner contents with:

```jinja
  {% for s in active_groups %}
    <div class="lb-section lb-section-row" data-kind="group" data-wc="{{ s.loc_name }}" draggable="true">
      <div class="lb-section-header">
        <span class="lb-drag-handle" title="Drag to reorder">&#9776;</span>
        <h3>{{ s.loc_name }} <span class="lb-section-tag">group</span></h3>
        <button type="button" class="lb-hide-btn" title="Mark inactive">&#10005;</button>
      </div>
      <div class="lb-side-days">
        {% if s.rows %}
          <table class="lb-table">
            <thead>
              <tr>
                <th>#</th><th>Operator</th><th>Date</th><th>WC</th>
                <th class="num">Units</th><th class="num">% of Goal</th>
              </tr>
            </thead>
            <tbody>
              {% for r in s.rows %}
                <tr>
                  <td class="rank">{{ r.rank }}</td>
                  <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                  <td>{{ r.day_label }}</td>
                  <td>{{ r.wc }}</td>
                  <td class="num">{{ r.units|round|int }}</td>
                  <td class="num pct">{{ '%.0f' % (r.pct * 100) }}%</td>
                </tr>
              {% endfor %}
            </tbody>
          </table>
        {% endif %}
      </div>
      <div class="lb-side-avg"></div>
    </div>
  {% endfor %}
```

- [ ] **Step 2: Restructure the per-WC active section**

Find the active WC sections loop (currently `{% for s in active_sections %}`, ~lines 67-96). Replace with:

```jinja
  {% for s in active_sections %}
    <div class="lb-section lb-section-row" data-kind="wc" data-wc="{{ s.loc_name }}" draggable="true">
      <div class="lb-section-header">
        <span class="lb-drag-handle" title="Drag to reorder">&#9776;</span>
        <h3>{{ s.loc_name }}</h3>
        <button type="button" class="lb-hide-btn" title="Mark inactive">&#10005;</button>
      </div>
      <div class="lb-side-days">
        {% if s.rows %}
          <table class="lb-table">
            <thead>
              <tr>
                <th>#</th><th>Operator</th><th>Date</th>
                <th class="num">Units</th><th class="num">% of Goal</th>
              </tr>
            </thead>
            <tbody>
              {% for r in s.rows %}
                <tr>
                  <td class="rank">{{ r.rank }}</td>
                  <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                  <td>{{ r.day_label }}</td>
                  <td class="num">{{ r.units|round|int }}</td>
                  <td class="num pct">{{ '%.0f' % (r.pct * 100) }}%</td>
                </tr>
              {% endfor %}
            </tbody>
          </table>
        {% endif %}
      </div>
      <div class="lb-side-avg"></div>
    </div>
  {% endfor %}
```

- [ ] **Step 3: Same restructure inside the `.lb-inactive-content` block**

Find the `{% for s in inactive_groups %}` loop (~lines 102-136) and replace inner block:

```jinja
    {% for s in inactive_groups %}
      <div class="lb-section lb-section-row lb-section-inactive" data-kind="group" data-wc="{{ s.loc_name }}" draggable="true">
        <div class="lb-section-header">
          <span class="lb-drag-handle" title="Drag to reorder">&#9776;</span>
          <h3>{{ s.loc_name }} <span class="lb-section-tag">group</span></h3>
          {% if s.is_manually_inactive %}
            <button type="button" class="lb-show-btn" title="Mark active">&#8634;</button>
          {% else %}
            <span class="lb-auto-empty" title="No data in this range — auto-hidden">auto-empty</span>
          {% endif %}
        </div>
        <div class="lb-side-days">
          {% if s.rows %}
            <table class="lb-table">
              <thead>
                <tr>
                  <th>#</th><th>Operator</th><th>Date</th><th>WC</th>
                  <th class="num">Units</th><th class="num">% of Goal</th>
                </tr>
              </thead>
              <tbody>
                {% for r in s.rows %}
                  <tr>
                    <td class="rank">{{ r.rank }}</td>
                    <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                    <td>{{ r.day_label }}</td>
                    <td>{{ r.wc }}</td>
                    <td class="num">{{ r.units|round|int }}</td>
                    <td class="num pct">{{ '%.0f' % (r.pct * 100) }}%</td>
                  </tr>
                {% endfor %}
              </tbody>
            </table>
          {% endif %}
        </div>
        <div class="lb-side-avg"></div>
      </div>
    {% endfor %}
```

Then find the `{% for s in inactive_sections %}` loop (~lines 137-170) and replace with:

```jinja
    {% for s in inactive_sections %}
      <div class="lb-section lb-section-row lb-section-inactive" data-kind="wc" data-wc="{{ s.loc_name }}" draggable="true">
        <div class="lb-section-header">
          <span class="lb-drag-handle" title="Drag to reorder">&#9776;</span>
          <h3>{{ s.loc_name }}</h3>
          {% if s.is_manually_inactive %}
            <button type="button" class="lb-show-btn" title="Mark active">&#8634;</button>
          {% else %}
            <span class="lb-auto-empty" title="No data in this range — auto-hidden">auto-empty</span>
          {% endif %}
        </div>
        <div class="lb-side-days">
          {% if s.rows %}
            <table class="lb-table">
              <thead>
                <tr>
                  <th>#</th><th>Operator</th><th>Date</th>
                  <th class="num">Units</th><th class="num">% of Goal</th>
                </tr>
              </thead>
              <tbody>
                {% for r in s.rows %}
                  <tr>
                    <td class="rank">{{ r.rank }}</td>
                    <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                    <td>{{ r.day_label }}</td>
                    <td class="num">{{ r.units|round|int }}</td>
                    <td class="num pct">{{ '%.0f' % (r.pct * 100) }}%</td>
                  </tr>
                {% endfor %}
              </tbody>
            </table>
          {% endif %}
        </div>
        <div class="lb-side-avg"></div>
      </div>
    {% endfor %}
```

- [ ] **Step 4: Smoke check — page still renders**

Run: `.venv\Scripts\python.exe -m uvicorn zira_dashboard.app:app --port 8001` (background).

Hit `http://localhost:8001/staffing/leaderboards`. Expected: looks the same as before. Right side of each card is empty (because `.lb-side-avg` is empty and CSS hasn't been added yet — visually it's just one column). Drag/hide still work. Stop the server.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/leaderboards.html
git commit -m "Wrap leaderboard sections in .lb-section-row scaffold"
```

---

## Task 5: Template — fill in Best Averages tables

**Files:**
- Modify: `src/zira_dashboard/templates/leaderboards.html`

Now add the actual averages tables and Show-all buttons. Each `.lb-side-avg` div gets a table sourced from the matching `*_avg_*` context list. Tables are paired by `loc_name` — find the matching avg section by name. The simplest way is to build a dict in Jinja (or pass it from Python). We'll do it in Jinja with a small `{% set ... %}` lookup.

Since the avg section ordering matches the days section ordering (same `sort_key`), we can also iterate them with a shared index. But a name-keyed lookup is more robust.

The lookup dicts `avg_sections_by_name` and `avg_groups_by_name` are passed in from the route handler (see Task 3 Step 5). The template looks up the matching averages section by `loc_name`.

- [ ] **Step 1: Add per-group averages table inside `.lb-side-avg`**

In each of the TWO group loops (`active_groups` and `inactive_groups`), replace `<div class="lb-side-avg"></div>` with:

```jinja
        <div class="lb-side-avg">
          {% set a = avg_groups_by_name.get(s.loc_name) %}
          {% if a and a.rows %}
            {% set top5 = a.rows[:5] %}
            {% set rest = a.rows[5:] %}
            <table class="lb-table">
              <thead>
                <tr>
                  <th>#</th><th>Operator</th><th>Top WC</th>
                  <th class="num">Avg/day{% if metric == 'units' %} <span class="lb-sort-caret">&#9662;</span>{% endif %}</th>
                  <th class="num">Avg %{% if metric == 'pct' %} <span class="lb-sort-caret">&#9662;</span>{% endif %}</th>
                </tr>
              </thead>
              <tbody>
                {% for r in top5 %}
                  <tr>
                    <td class="rank">{{ r.rank }}</td>
                    <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                    <td>{{ r.top_wc }}</td>
                    <td class="num">{{ r.avg_units|round|int }}</td>
                    <td class="num pct">{{ '%.0f' % (r.avg_pct * 100) }}%</td>
                  </tr>
                {% endfor %}
                {% for r in rest %}
                  <tr class="lb-row-hidden">
                    <td class="rank">{{ r.rank }}</td>
                    <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                    <td>{{ r.top_wc }}</td>
                    <td class="num">{{ r.avg_units|round|int }}</td>
                    <td class="num pct">{{ '%.0f' % (r.avg_pct * 100) }}%</td>
                  </tr>
                {% endfor %}
              </tbody>
            </table>
            {% if rest %}
              <button type="button" class="lb-expand-btn" onclick="toggleAll(this)" data-count="{{ rest|length }}">
                &#9662; Show all ({{ a.rows|length }})
              </button>
            {% endif %}
          {% endif %}
        </div>
```

- [ ] **Step 2: Add per-WC averages table inside `.lb-side-avg`**

In each of the TWO WC loops (`active_sections` and `inactive_sections`), replace `<div class="lb-side-avg"></div>` with:

```jinja
        <div class="lb-side-avg">
          {% set a = avg_sections_by_name.get(s.loc_name) %}
          {% if a and a.rows %}
            {% set top5 = a.rows[:5] %}
            {% set rest = a.rows[5:] %}
            <table class="lb-table">
              <thead>
                <tr>
                  <th>#</th><th>Operator</th>
                  <th class="num">Avg/day{% if metric == 'units' %} <span class="lb-sort-caret">&#9662;</span>{% endif %}</th>
                  <th class="num">Avg %{% if metric == 'pct' %} <span class="lb-sort-caret">&#9662;</span>{% endif %}</th>
                </tr>
              </thead>
              <tbody>
                {% for r in top5 %}
                  <tr>
                    <td class="rank">{{ r.rank }}</td>
                    <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                    <td class="num">{{ r.avg_units|round|int }}</td>
                    <td class="num pct">{{ '%.0f' % (r.avg_pct * 100) }}%</td>
                  </tr>
                {% endfor %}
                {% for r in rest %}
                  <tr class="lb-row-hidden">
                    <td class="rank">{{ r.rank }}</td>
                    <td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
                    <td class="num">{{ r.avg_units|round|int }}</td>
                    <td class="num pct">{{ '%.0f' % (r.avg_pct * 100) }}%</td>
                  </tr>
                {% endfor %}
              </tbody>
            </table>
            {% if rest %}
              <button type="button" class="lb-expand-btn" onclick="toggleAll(this)" data-count="{{ rest|length }}">
                &#9662; Show all ({{ a.rows|length }})
              </button>
            {% endif %}
          {% endif %}
        </div>
```

- [ ] **Step 3: Add `toggleAll` JS handler**

In the `{% block scripts %}` section of `templates/leaderboards.html`, INSIDE the existing `<script>` tag, ABOVE the `(function initLeaderboards() {` IIFE, add:

```html
<script>
function toggleAll(btn) {
  var card = btn.closest('.lb-side-avg');
  if (!card) return;
  var hidden = card.querySelectorAll('.lb-row-hidden, .lb-row-revealed');
  var nowVisible = !card.classList.contains('lb-expanded');
  card.classList.toggle('lb-expanded', nowVisible);
  hidden.forEach(function (tr) {
    tr.classList.toggle('lb-row-hidden', !nowVisible);
    tr.classList.toggle('lb-row-revealed', nowVisible);
  });
  if (nowVisible) {
    btn.innerHTML = '&#9650; Hide';
  } else {
    var card2 = btn.closest('.lb-side-avg');
    var totalRows = card2.querySelectorAll('tbody tr').length;
    btn.innerHTML = '&#9662; Show all (' + totalRows + ')';
  }
}
</script>
```

(The function is defined OUTSIDE the IIFE because it's referenced by inline `onclick`. Two `<script>` tags inside one `{% block scripts %}` is fine.)

- [ ] **Step 4: Smoke check — averages render but layout is broken**

Start the server. Hit the page. Expected: averages tables now appear visually below the Best Days tables (because grid CSS hasn't been added yet — they stack). Show all button shows when there are 6+ operators. Click toggles. Drag/hide still work.

If the server logs a Jinja error like "TemplateSyntaxError" or "UndefinedError: 'avg_sections_by_name'", recheck Task 3 Step 5 (the context keys).

Stop the server.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/leaderboards.html
git commit -m "Render Best Averages tables with Show-all toggle"
```

---

## Task 6: CSS — two-column grid + responsive + expansion styles

**Files:**
- Modify: `src/zira_dashboard/static/leaderboards.css`

After this task, Best Days and Best Averages sit side-by-side per row, the section header spans both, and below 900px wide they stack vertically.

- [ ] **Step 1: Add the new CSS rules**

In `src/zira_dashboard/static/leaderboards.css`, REPLACE the existing `.lb-active-list, .lb-inactive-content` block (currently around lines 22-28) with:

```css
  .lb-active-list,
  .lb-inactive-content {
    display: flex;
    flex-direction: column;
    gap: 0.7rem;
    align-items: stretch;
  }

  .lb-section-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    grid-template-areas:
      "header header"
      "days   avg";
    column-gap: 0.8rem;
    row-gap: 0.4rem;
  }
  .lb-section-row > .lb-section-header { grid-area: header; }
  .lb-section-row > .lb-side-days { grid-area: days; min-width: 0; }
  .lb-section-row > .lb-side-avg  { grid-area: avg;  min-width: 0; }
  .lb-section-row > .lb-side-days::before {
    content: "Best Days";
    display: block;
    font-size: 0.7rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 0.25rem;
  }
  .lb-section-row > .lb-side-avg::before {
    content: "Best Averages";
    display: block;
    font-size: 0.7rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 0.25rem;
  }

  .lb-row-hidden { display: none; }
  .lb-sort-caret { color: var(--accent); font-size: 0.65rem; vertical-align: 1px; }

  .lb-expand-btn {
    margin-top: 0.4rem;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
    border-radius: 4px;
    padding: 0.25rem 0.7rem;
    font: inherit; font-size: 0.8rem;
    cursor: pointer;
  }
  .lb-expand-btn:hover { color: var(--accent); border-color: var(--accent); }

  @media (max-width: 900px) {
    .lb-section-row {
      grid-template-columns: 1fr;
      grid-template-areas:
        "header"
        "days"
        "avg";
    }
  }
```

- [ ] **Step 2: Smoke check — full feature works**

Start the server. Hit `http://localhost:8001/staffing/leaderboards`.

Expected:
- Each WC/group renders as ONE row, with shared header at the top.
- Best Days table on the left, Best Averages table on the right, with small "BEST DAYS" / "BEST AVERAGES" labels above each.
- Active sort column has the `▼` caret (Avg/day caret when metric=units, Avg % caret when metric=pct).
- WCs/groups with 6+ operators show `▼ Show all (N)`. Click expands; click again collapses.
- Drag a section's drag handle — the WHOLE row moves (both halves together).
- Click ✕ on a section — the WHOLE row moves into the inactive collapsible.
- Resize window to <900px — Best Days appears above Best Averages per row.

Stop the server.

- [ ] **Step 3: Commit**

```bash
git add src/zira_dashboard/static/leaderboards.css
git commit -m "Add two-column grid + expansion styles for leaderboards"
```

---

## Task 7: End-to-end smoke

Manual verification before considering this done.

- [ ] **Step 1: Run unit tests one more time**

Run: `.venv\Scripts\python.exe -m pytest tests/test_leaderboards_avg.py -v`

Expected: 13/13 pass.

- [ ] **Step 2: Manual smoke checklist (live deploy or local server)**

Walk through every acceptance criterion from the spec:

1. Open `/staffing/leaderboards` — two columns visible per WC/group.
2. Toggle metric Units → % of Goal — sort caret moves between Avg/day and Avg % columns. Best Averages table re-sorts. Best Days table also reflects (existing behavior).
3. Pick a custom date range that contains a day where someone had custom hours (e.g., a 4h day on the schedule). Their `Avg %` should reflect the custom-hours-aware expected, not a static daily target. (Confirm with one operator's known math.)
4. Click `▼ Show all (N)` on a section with >5 operators — table expands, button label flips to `▲ Hide`. Click again — collapses.
5. Verify Best Days side does NOT have a Show all button anywhere on the page.
6. Drag-reorder a section — both halves move together. Reload — order persists.
7. Click ✕ on an active section — both halves move into the inactive `<details>` together.
8. Click ↶ on a manually-inactive section — both halves move back to active together.
9. WCs with no records in the range should appear in the inactive collapsible with empty tables on both sides.
10. Resize to <900px — Best Days appears above Best Averages per row.

- [ ] **Step 3: Tell Dale it's ready**

Report: "Best Averages leaderboard live. All 10 acceptance items checked. Want to push?"
