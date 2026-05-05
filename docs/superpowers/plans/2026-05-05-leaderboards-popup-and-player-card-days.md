# Leaderboards Drill-Down Popup + Player Card Per-Day Rows — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a three-link drill-down chain on `/staffing/leaderboards` and `/staffing/people/{name}` — clickable operator names on every averages widget open a per-person modal with day-by-day rows; each date hyperlinks into `/recycling` for that day; an "Open full player card" button takes the user to the full per-day breakdown.

**Architecture:** Reuse the existing `production_history.attribution_for(d, client)` per-day attribution data. Add a sibling `attribution_per_day(start, end, client)` helper that preserves the day axis. Build a new JSON endpoint that filters that data by person + scope (single WC or category group) and aggregates per day, with TTL caching so repeated popup opens don't re-aggregate. Leaderboards page render adds zero work — only `data-*` attributes on operator names; the popup loads on click via AJAX.

**Tech Stack:** Python 3.12 / FastAPI / Jinja2 / vanilla JS / pytest. Test runner: `.venv/Scripts/python.exe -m pytest`.

**Spec:** `docs/superpowers/specs/2026-05-05-leaderboards-popup-and-player-card-days-design.md`

---

## File map

| File | Change |
|---|---|
| `src/zira_dashboard/production_history.py` | Add `attribution_per_day()` helper; no changes to existing functions. |
| `src/zira_dashboard/routes/leaderboards.py` | Add `person_days_json()` JSON endpoint + TTL cache. |
| `src/zira_dashboard/routes/people.py` | Extend `staffing_player_card` to compute and pass `day_rows`. |
| `src/zira_dashboard/templates/leaderboards.html` | Wrap operator-name cells in `<button class="lb-name-btn">`; inline modal HTML, CSS, and JS. |
| `src/zira_dashboard/templates/player_card.html` | Add per-day breakdown table below the existing per-WC summary. |
| `tests/test_production_history.py` (new or extend) | Unit tests for `attribution_per_day`. |
| `tests/test_leaderboards_person_days.py` (new) | Tests for the new JSON endpoint, mocking `attribution_per_day`. |
| `tests/test_player_card.py` (new) | Render test for the per-day breakdown table, mocking `attribution_per_day`. |
| `CHANGELOG.md` | Entry for the deploy. |

---

### Task 1: Add `attribution_per_day` helper

**Files:**
- Modify: `src/zira_dashboard/production_history.py`
- Create or extend: `tests/test_production_history.py`

- [ ] **Step 1: Write the failing test**

If `tests/test_production_history.py` doesn't exist yet, create it. Otherwise append to it.

```python
# tests/test_production_history.py (new file or appended)
from datetime import date
from unittest.mock import patch

from zira_dashboard import production_history


def test_attribution_per_day_returns_one_entry_per_day_in_order():
    """attribution_per_day returns a list of (day, attribution_dict) tuples
    in date-ascending order, one entry per day in the [start, end] range
    inclusive. Each attribution_dict matches what attribution_for() would
    return for that day individually."""
    start = date(2026, 4, 27)  # Monday
    end = date(2026, 4, 29)    # Wednesday

    def _fake_attribution_for(d, client):
        return {f"P{d.day}": {"WC1": {"units": float(d.day), "downtime": 0.0,
                                      "hours": 8.0, "days_worked": 1}}}

    with patch.object(production_history, "attribution_for", side_effect=_fake_attribution_for):
        out = production_history.attribution_per_day(start, end, client=None)

    assert [day for day, _ in out] == [date(2026, 4, 27), date(2026, 4, 28), date(2026, 4, 29)]
    assert out[0][1] == {"P27": {"units": 27.0, "downtime": 0.0, "hours": 8.0, "days_worked": 1}}
    assert out[1][1]["P28"]["units"] == 28.0
    assert out[2][1]["P29"]["units"] == 29.0


def test_attribution_per_day_keeps_empty_days_in_list():
    """Days where attribution_for returns {} still appear in the output
    list (with an empty dict value) so the date axis stays predictable
    for callers that need to know which days were checked."""
    def _fake(d, client):
        if d == date(2026, 4, 28):
            return {}
        return {"P": {"WC": {"units": 1.0, "downtime": 0.0, "hours": 8.0, "days_worked": 1}}}

    with patch.object(production_history, "attribution_for", side_effect=_fake):
        out = production_history.attribution_per_day(date(2026, 4, 27), date(2026, 4, 29), client=None)

    assert len(out) == 3
    assert out[0][1] != {}
    assert out[1][1] == {}
    assert out[2][1] != {}
```

- [ ] **Step 2: Run the tests to verify they fail**

```
.venv/Scripts/python.exe -m pytest tests/test_production_history.py -v -k attribution_per_day
```

Expected: FAIL with `AttributeError: module 'zira_dashboard.production_history' has no attribute 'attribution_per_day'`.

- [ ] **Step 3: Implement `attribution_per_day`**

Open `src/zira_dashboard/production_history.py`. Find the existing `attribution_range` function (around line 146). Add the new helper right above it. The implementation mirrors `attribution_range`'s per-day fan-out but yields tuples instead of summing:

```python
def attribution_per_day(
    start: date,
    end: date,
    client,
) -> list[tuple[date, dict[str, dict[str, dict[str, float]]]]]:
    """Per-day attribution across [start, end] inclusive.

    Returns a list of (day, attribution_dict) tuples in date-ascending
    order. Each attribution_dict has the same shape as
    `attribution_for(day, client)`. Days with no published schedule
    yield an empty dict (kept in the list so callers can distinguish
    "checked, found nothing" from "didn't check").

    Days are fetched concurrently via a thread pool — same pool sizing
    as `attribution_range` so multi-month ranges don't pay sequential
    per-day latency. The shared `cached_leaderboard` cache means
    repeated calls for the same range return instantly.
    """
    from datetime import timedelta
    from concurrent.futures import ThreadPoolExecutor

    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)

    if not days:
        return []

    with ThreadPoolExecutor(max_workers=min(8, len(days))) as pool:
        dailies = list(pool.map(lambda d: attribution_for(d, client), days))
    return list(zip(days, dailies))
```

- [ ] **Step 4: Run the tests to verify they pass**

```
.venv/Scripts/python.exe -m pytest tests/test_production_history.py -v -k attribution_per_day
```

Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/production_history.py tests/test_production_history.py
git commit -m "feat(production_history): add attribution_per_day helper

Sibling to attribution_range that preserves the day axis instead of
summing. Fans out per-day with the same ThreadPoolExecutor sizing.
Used by the upcoming leaderboards drill-down popup and the player
card per-day breakdown.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: New JSON endpoint `/api/staffing/leaderboards/person-days`

**Files:**
- Modify: `src/zira_dashboard/routes/leaderboards.py`
- Create: `tests/test_leaderboards_person_days.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_leaderboards_person_days.py`:

```python
from datetime import date
from unittest.mock import patch

from fastapi.testclient import TestClient

from zira_dashboard.app import app


def _attr(units: float, downtime: float = 0.0):
    return {"units": units, "downtime": downtime, "hours": 8.0, "days_worked": 1}


def test_person_days_400_when_neither_wc_nor_group():
    client = TestClient(app)
    r = client.get("/api/staffing/leaderboards/person-days?name=Carlos&start=2026-04-27&end=2026-04-29")
    assert r.status_code == 400


def test_person_days_400_when_both_wc_and_group():
    client = TestClient(app)
    r = client.get("/api/staffing/leaderboards/person-days?name=Carlos&wc=Repair-1&group=Repair&start=2026-04-27&end=2026-04-29")
    assert r.status_code == 400


def test_person_days_400_on_unparseable_dates():
    client = TestClient(app)
    r = client.get("/api/staffing/leaderboards/person-days?name=Carlos&wc=Repair-1&start=garbage&end=2026-04-29")
    assert r.status_code == 400


def test_person_days_filters_to_single_wc():
    """?wc=Repair-1 keeps only that WC; multi-WC days drop the others."""
    fake = [
        (date(2026, 4, 27), {"Carlos": {"Repair-1": _attr(95), "Repair-2": _attr(88)}}),
        (date(2026, 4, 28), {"Carlos": {"Repair-2": _attr(90)}}),
        (date(2026, 4, 29), {"Carlos": {"Repair-1": _attr(100)}}),
    ]
    with patch("zira_dashboard.routes.leaderboards.attribution_per_day", return_value=fake):
        client = TestClient(app)
        r = client.get("/api/staffing/leaderboards/person-days?name=Carlos&wc=Repair-1&start=2026-04-27&end=2026-04-29")
    assert r.status_code == 200
    rows = r.json()["rows"]
    # Newest first; only Repair-1 days for Carlos remain.
    assert [x["date"] for x in rows] == ["2026-04-29", "2026-04-27"]
    assert all(x["wcs"] == ["Repair-1"] for x in rows)
    assert rows[0]["units"] == 100
    assert rows[1]["units"] == 95


def test_person_days_aggregates_group_scope():
    """?group=Repair keeps any WC in the Repair category and aggregates per day."""
    fake = [
        (date(2026, 4, 27), {
            "Carlos": {"Repair-1": _attr(95, 5), "Repair-2": _attr(88, 7), "Dismantler-1": _attr(50)},
            "Other": {"Repair-1": _attr(10)},
        }),
        (date(2026, 4, 28), {
            "Carlos": {"Dismantler-1": _attr(60)},  # no Repair WC; should drop the day
        }),
    ]
    # The endpoint resolves WC categories via staffing.LOCATIONS (loc.skill).
    # Patch attribution_per_day; rely on the real LOCATIONS for category lookup.
    with patch("zira_dashboard.routes.leaderboards.attribution_per_day", return_value=fake):
        client = TestClient(app)
        r = client.get("/api/staffing/leaderboards/person-days?name=Carlos&group=Repair&start=2026-04-27&end=2026-04-28")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["date"] == "2026-04-27"
    assert row["wcs"] == ["Repair-1", "Repair-2"]  # alphabetical
    assert row["units"] == 95 + 88
    assert row["downtime"] == 5 + 7


def test_person_days_returns_empty_rows_when_no_match():
    """Person who has no production in the scope/range returns 200 with []."""
    fake = [
        (date(2026, 4, 27), {"Other": {"Repair-1": _attr(50)}}),
    ]
    with patch("zira_dashboard.routes.leaderboards.attribution_per_day", return_value=fake):
        client = TestClient(app)
        r = client.get("/api/staffing/leaderboards/person-days?name=Carlos&wc=Repair-1&start=2026-04-27&end=2026-04-27")
    assert r.status_code == 200
    assert r.json() == {"rows": []}
```

- [ ] **Step 2: Run the tests to verify they fail**

```
.venv/Scripts/python.exe -m pytest tests/test_leaderboards_person_days.py -v
```

Expected: all FAIL with 404 (route doesn't exist yet) or 405 / 500.

- [ ] **Step 3: Implement the endpoint**

Open `src/zira_dashboard/routes/leaderboards.py`. At the top of the file, add the import for `attribution_per_day`:

```python
# Add near the other top-level imports (around line 11):
from ..production_history import attribution_per_day
```

(Note: the existing `attribution_range` is currently imported lazily inside the route handler; we're using the imported-at-top form because `person_days_json` doesn't have the same heavy-import-at-startup concerns and tests need to patch it at the module level.)

Add the endpoint at the bottom of the file:

```python
@router.get("/api/staffing/leaderboards/person-days")
def person_days_json(
    name: str = Query(...),
    wc: str | None = Query(default=None),
    group: str | None = Query(default=None),
    start: str = Query(...),
    end: str = Query(...),
):
    """Per-day breakdown of a person's production within a scope (a single
    WC or a category group) over [start, end] inclusive. Used by the
    leaderboards averages popup. Returns rows sorted newest-first.
    """
    if (wc and group) or (not wc and not group):
        return JSONResponse({"error": "exactly one of wc / group must be set"}, status_code=400)
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except (ValueError, TypeError):
        return JSONResponse({"error": "start / end must be YYYY-MM-DD"}, status_code=400)
    if end_d < start_d:
        return JSONResponse({"error": "end must be on or after start"}, status_code=400)

    # Build the WC-name filter set.
    if wc:
        wc_filter = {wc}
    else:
        # group: gather every LOCATIONS entry whose .skill equals the group name.
        wc_filter = {loc.name for loc in staffing.LOCATIONS if loc.skill == group}
        if not wc_filter:
            return JSONResponse({"rows": []})

    # Fan out per day, filter, aggregate.
    rows: list[dict] = []
    for day, daily in attribution_per_day(start_d, end_d, client):
        person_data = daily.get(name, {})
        matching = {w: t for w, t in person_data.items() if w in wc_filter}
        if not matching:
            continue
        rows.append({
            "date": day.isoformat(),
            "wcs": sorted(matching.keys()),
            "units": sum(t["units"] for t in matching.values()),
            "downtime": sum(t["downtime"] for t in matching.values()),
        })
    rows.sort(key=lambda r: r["date"], reverse=True)
    return JSONResponse({"rows": rows})
```

The imports of `date`, `JSONResponse`, `staffing`, `client` are all already in this file from the existing `leaderboards()` route. Add `from datetime import date` to the top-level imports if it's not already there (the existing imports use `datetime, timezone` from `datetime` — add `date` to that list).

- [ ] **Step 4: Run the tests to verify they pass**

```
.venv/Scripts/python.exe -m pytest tests/test_leaderboards_person_days.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/routes/leaderboards.py tests/test_leaderboards_person_days.py
git commit -m "feat(leaderboards): /api/staffing/leaderboards/person-days endpoint

JSON endpoint for the upcoming averages-widget drill-down popup.
Filters per-day attribution by person + scope (single wc OR category
group), aggregates per day, sorts newest-first. Mutual-exclusivity
of wc/group enforced; bad dates return 400.

No caching yet — the next commit adds a TTL cache layer.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Add TTL cache to the endpoint

**Files:**
- Modify: `src/zira_dashboard/routes/leaderboards.py`

- [ ] **Step 1: Add the cache + cache lookup**

In `src/zira_dashboard/routes/leaderboards.py`, near the top of the file (after the imports, before `averages_for_wc`), add a module-level cache:

```python
from .._cache import TTLCache

# Cached responses by (name, scope-key, start, end). Past-only ranges
# get the longer TTL; ranges that include today get the shorter one
# so a fresh-published schedule shows up on the next click.
_PERSON_DAYS_CACHE_TODAY = TTLCache(ttl_seconds=60.0, max_entries=128)
_PERSON_DAYS_CACHE_PAST = TTLCache(ttl_seconds=3600.0, max_entries=512)
```

Modify `person_days_json` to consult the cache before computing:

```python
@router.get("/api/staffing/leaderboards/person-days")
def person_days_json(
    name: str = Query(...),
    wc: str | None = Query(default=None),
    group: str | None = Query(default=None),
    start: str = Query(...),
    end: str = Query(...),
):
    if (wc and group) or (not wc and not group):
        return JSONResponse({"error": "exactly one of wc / group must be set"}, status_code=400)
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except (ValueError, TypeError):
        return JSONResponse({"error": "start / end must be YYYY-MM-DD"}, status_code=400)
    if end_d < start_d:
        return JSONResponse({"error": "end must be on or after start"}, status_code=400)

    # Cache key: scope is "wc:NAME" or "group:NAME".
    scope_key = f"wc:{wc}" if wc else f"group:{group}"
    cache_key = (name, scope_key, start_d.isoformat(), end_d.isoformat())
    today = datetime.now(timezone.utc).date()
    includes_today = start_d <= today <= end_d
    cache = _PERSON_DAYS_CACHE_TODAY if includes_today else _PERSON_DAYS_CACHE_PAST
    cached = cache.get(cache_key)
    if cached is not None:
        return JSONResponse(cached)

    # Build the WC-name filter set.
    if wc:
        wc_filter = {wc}
    else:
        wc_filter = {loc.name for loc in staffing.LOCATIONS if loc.skill == group}
        if not wc_filter:
            payload = {"rows": []}
            cache.set(cache_key, payload)
            return JSONResponse(payload)

    rows: list[dict] = []
    for day, daily in attribution_per_day(start_d, end_d, client):
        person_data = daily.get(name, {})
        matching = {w: t for w, t in person_data.items() if w in wc_filter}
        if not matching:
            continue
        rows.append({
            "date": day.isoformat(),
            "wcs": sorted(matching.keys()),
            "units": sum(t["units"] for t in matching.values()),
            "downtime": sum(t["downtime"] for t in matching.values()),
        })
    rows.sort(key=lambda r: r["date"], reverse=True)
    payload = {"rows": rows}
    cache.set(cache_key, payload)
    return JSONResponse(payload)
```

(The existing `TTLCache` from `src/zira_dashboard/_cache.py` exposes `get(key)` and `set(key, value)`; if your local `TTLCache` uses different method names, adjust accordingly. As of 2026-05-05 it has `get(key)` returning the value or `None`, and `set(key, value)` storing it.)

- [ ] **Step 2: Verify with the existing tests**

The mock-based tests from Task 2 should still pass — caching shouldn't affect them.

```
.venv/Scripts/python.exe -m pytest tests/test_leaderboards_person_days.py -v
```

Expected: 5 PASSED.

- [ ] **Step 3: Add a cache-hit test**

Append to `tests/test_leaderboards_person_days.py`:

```python
def test_person_days_caches_response(monkeypatch):
    """Repeated calls for the same (name, scope, range) hit the cache and
    don't re-call attribution_per_day."""
    from zira_dashboard.routes import leaderboards as lb_mod

    # Clear caches so the test starts clean.
    lb_mod._PERSON_DAYS_CACHE_TODAY.invalidate() if hasattr(lb_mod._PERSON_DAYS_CACHE_TODAY, "invalidate") else None
    lb_mod._PERSON_DAYS_CACHE_PAST.invalidate() if hasattr(lb_mod._PERSON_DAYS_CACHE_PAST, "invalidate") else None

    fake = [(date(2026, 4, 27), {"Carlos": {"Repair-1": _attr(95)}})]
    call_count = {"n": 0}
    def _spy(*args, **kwargs):
        call_count["n"] += 1
        return fake

    monkeypatch.setattr(lb_mod, "attribution_per_day", _spy)
    client = TestClient(app)

    url = "/api/staffing/leaderboards/person-days?name=Carlos&wc=Repair-1&start=2026-04-27&end=2026-04-27"
    client.get(url)
    client.get(url)
    client.get(url)

    assert call_count["n"] == 1, "expected only the first call to hit attribution_per_day"
```

- [ ] **Step 4: Run the new test**

```
.venv/Scripts/python.exe -m pytest tests/test_leaderboards_person_days.py::test_person_days_caches_response -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/routes/leaderboards.py tests/test_leaderboards_person_days.py
git commit -m "feat(leaderboards): TTL cache on person-days endpoint

Cache responses keyed by (name, scope, start, end). Past-only ranges
get a 1h TTL; ranges including today get 60s. Repeated popup opens
on the same widget skip re-aggregation entirely.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Player card route — pass per-day rows

**Files:**
- Modify: `src/zira_dashboard/routes/people.py`
- Create: `tests/test_player_card.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_player_card.py`:

```python
from datetime import date
from unittest.mock import patch

from fastapi.testclient import TestClient

from zira_dashboard.app import app


def _attr(units: float, downtime: float = 0.0):
    return {"units": units, "downtime": downtime, "hours": 8.0, "days_worked": 1}


def test_player_card_renders_per_day_breakdown_table():
    """The player card surfaces a per-day-per-WC table below the per-WC
    summary, with each Date cell hyperlinked to the recycling dashboard
    for that day. Days are sorted newest-first."""
    fake = [
        (date(2026, 4, 27), {"Carlos": {"Repair-1": _attr(95)}}),
        (date(2026, 4, 28), {"Carlos": {"Repair-1": _attr(80), "Repair-2": _attr(70)}}),
        (date(2026, 4, 29), {"Other": {"Repair-1": _attr(50)}}),
    ]
    with patch("zira_dashboard.production_history.attribution_per_day", return_value=fake), \
         patch("zira_dashboard.production_history.attribution_range",
               return_value={"Carlos": {"Repair-1": {"units": 175.0, "downtime": 0.0,
                                                     "hours": 16.0, "days_worked": 2},
                                        "Repair-2": {"units": 70.0, "downtime": 0.0,
                                                     "hours": 8.0, "days_worked": 1}}}), \
         patch("zira_dashboard.staffing.load_roster", return_value=[]):
        client = TestClient(app)
        html = client.get("/staffing/people/Carlos?start=2026-04-27&end=2026-04-29").text

    # Per-day breakdown header is present.
    assert "Per-day breakdown" in html
    # Date hyperlinks point at the recycling dashboard for that day.
    assert 'href="/recycling?start=2026-04-28&end=2026-04-28"' in html
    assert 'href="/recycling?start=2026-04-27&end=2026-04-27"' in html
    # Newest first.
    assert html.index("2026-04-28") < html.index("2026-04-27")
    # Carlos's entries appear, "Other" does not.
    assert "Repair-1" in html and "Repair-2" in html
```

- [ ] **Step 2: Run the test to verify it fails**

```
.venv/Scripts/python.exe -m pytest tests/test_player_card.py -v
```

Expected: FAIL — the per-day breakdown table doesn't exist yet, so `"Per-day breakdown"` won't appear in the HTML.

- [ ] **Step 3: Update the route**

Open `src/zira_dashboard/routes/people.py`. The existing route looks like (slightly elided):

```python
@router.get("/staffing/people/{name}", response_class=HTMLResponse)
def staffing_player_card(
    request: Request,
    name: str,
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    from .. import production_history
    today = datetime.now(timezone.utc).date()
    end_d = date.fromisoformat(end) if end else today
    start_d = date.fromisoformat(start) if start else (end_d - timedelta(days=29))
    range_out = production_history.attribution_range(start_d, end_d, client)
    person = range_out.get(name, {})
    rows = sorted(
        ({"wc": wc, **t} for wc, t in person.items()),
        key=lambda r: -r["units"],
    )
    ...
```

Add a per-day-per-WC row build before `return templates.TemplateResponse(...)`. Replace the function body's inner section:

```python
@router.get("/staffing/people/{name}", response_class=HTMLResponse)
def staffing_player_card(
    request: Request,
    name: str,
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    from .. import production_history
    today = datetime.now(timezone.utc).date()
    end_d = date.fromisoformat(end) if end else today
    start_d = date.fromisoformat(start) if start else (end_d - timedelta(days=29))
    range_out = production_history.attribution_range(start_d, end_d, client)
    person = range_out.get(name, {})
    rows = sorted(
        ({"wc": wc, **t} for wc, t in person.items()),
        key=lambda r: -r["units"],
    )
    total_units    = sum(r["units"] for r in rows)
    total_downtime = sum(r["downtime"] for r in rows)
    total_days     = sum(r["days_worked"] for r in rows)
    roster = {p.name: p for p in staffing.load_roster()}
    p = roster.get(name)
    skills = []
    if p:
        skills = sorted(
            ((s, lvl) for s, lvl in p.skills.items() if lvl >= 1),
            key=lambda kv: -kv[1],
        )

    # Per-day-per-WC rows for the breakdown table. Newest first.
    day_rows: list[dict] = []
    for day, daily in production_history.attribution_per_day(start_d, end_d, client):
        person_data = daily.get(name, {})
        for wc_name, totals in person_data.items():
            day_rows.append({
                "date": day.isoformat(),
                "wc": wc_name,
                "units": totals["units"],
                "downtime": totals["downtime"],
            })
    day_rows.sort(key=lambda r: (r["date"], r["wc"]), reverse=True)

    return templates.TemplateResponse(
        request,
        "player_card.html",
        {
            "active": "people",
            "name": name,
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "today": today.isoformat(),
            "rows": rows,
            "day_rows": day_rows,
            "total_units": round(total_units, 1),
            "total_downtime": round(total_downtime, 1),
            "total_days": total_days,
            "skills": skills,
        },
    )
```

The added pieces: `from ..production_history import attribution_per_day`, the `day_rows` build, and `"day_rows": day_rows,` in the context dict. Everything else stays the same.

- [ ] **Step 4: Run the test (still expect failure — template not updated yet)**

```
.venv/Scripts/python.exe -m pytest tests/test_player_card.py -v
```

Expected: still FAIL — the route now passes `day_rows` but the template doesn't render it. We update the template in Task 5.

- [ ] **Step 5: Commit (route only)**

```bash
git add src/zira_dashboard/routes/people.py tests/test_player_card.py
git commit -m "feat(player_card): collect per-day rows in the route handler

Builds a list of per-(day, WC) dicts from attribution_per_day, sorted
newest-first, and passes it to the template as day_rows. Template
rendering of the breakdown table comes in the next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Player card template — render the per-day table

**Files:**
- Modify: `src/zira_dashboard/templates/player_card.html`

- [ ] **Step 1: Append the breakdown table**

Open `src/zira_dashboard/templates/player_card.html`. Find the end of the existing per-WC `{% if rows %} ... {% endif %}` block (around the `{% endblock %}` near the bottom). Insert the per-day breakdown after the per-WC table's `{% endif %}` line, *before* the `{% endblock %}`:

```jinja
{% if day_rows %}
<h3 style="margin-top:1rem">Per-day breakdown</h3>
<table class="pc">
  <thead>
    <tr><th>Date</th><th>Work Center</th><th class="num">Units</th><th class="num">Downtime (min)</th></tr>
  </thead>
  <tbody>
    {% for r in day_rows %}
    <tr>
      <td><a href="/recycling?start={{ r.date }}&end={{ r.date }}">{{ r.date }}</a></td>
      <td>{{ r.wc }}</td>
      <td class="num">{{ '{:,.0f}'.format(r.units) }}</td>
      <td class="num">{{ '{:,.0f}'.format(r.downtime) }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% endif %}
```

- [ ] **Step 2: Run the player-card render test**

```
.venv/Scripts/python.exe -m pytest tests/test_player_card.py -v
```

Expected: PASS — the template now contains "Per-day breakdown" and the date hyperlinks.

- [ ] **Step 3: Commit**

```bash
git add src/zira_dashboard/templates/player_card.html
git commit -m "feat(player_card): per-day breakdown table

One row per (day, WC) under the existing per-WC summary. Date cells
hyperlink to /recycling?start=DATE&end=DATE. Newest first. Hidden
when there's no data for the selected range.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Leaderboards template — clickable operator names

**Files:**
- Modify: `src/zira_dashboard/templates/leaderboards.html`

The averages widgets render operator names inside `<td class="op">`. There are 4 such cells we need to update — both `active_avg_groups` + `active_avg_sections`, plus their `inactive_*` mirrors, each rendered for `top5` and `rest` rows. The wrap is the same in all 8 spots.

- [ ] **Step 1: Add the `.lb-name-btn` CSS**

Inside the `<style>` block at the top of `leaderboards.html`, add a new rule. A natural place is right after the existing `.lb-table` rules. Find a `.lb-table td` rule and add below it:

```css
  .lb-name-btn {
    background: transparent;
    border: 0;
    padding: 0;
    margin: 0;
    color: inherit;
    font: inherit;
    cursor: pointer;
    text-align: left;
  }
  .lb-name-btn:hover { text-decoration: underline; text-decoration-color: var(--accent); }
```

- [ ] **Step 2: Wrap the 8 operator-name cells**

Use Jinja-friendly find-and-replace. Each of the four sections (`active_avg_groups`, `active_avg_sections`, `inactive_avg_groups`, `inactive_avg_sections`) renders the operator name in two places (top5 + rest), each looking like:

```jinja
<td class="op">{{ r.name }}{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
```

Replace **all 8 occurrences** of `{{ r.name }}{{ cert_badges` (the cert-badges-after-the-name pattern occurs only inside averages widgets — verify by grep) with the wrapped version. The new wrap is contextual: per-WC widgets pass `data-wc`, per-group widgets pass `data-group`. There are 4 wc-avg cells (2 active + 2 inactive top5/rest in `active_avg_sections` + `inactive_avg_sections`) and 4 group-avg cells (in `active_avg_groups` + `inactive_avg_groups`).

For each `data-kind="group-avg"` section's two `<td class="op">` cells:

```jinja
<td class="op"><button type="button" class="lb-name-btn"
   data-name="{{ r.name }}" data-group="{{ s.loc_name }}"
   data-start="{{ start }}" data-end="{{ end }}"
   onclick="openLbPopup(this)">{{ r.name }}</button>{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
```

For each `data-kind="wc-avg"` section's two `<td class="op">` cells:

```jinja
<td class="op"><button type="button" class="lb-name-btn"
   data-name="{{ r.name }}" data-wc="{{ s.loc_name }}"
   data-start="{{ start }}" data-end="{{ end }}"
   onclick="openLbPopup(this)">{{ r.name }}</button>{{ cert_badges(r.name, person_certs) }} <span class="lb-name-count">({{ r.name_count }})</span></td>
```

**Important:** the wrap goes around just `{{ r.name }}`. The `{{ cert_badges(...) }}` and `<span class="lb-name-count">` stay outside the button so cert badges and the name count remain visible (and not clickable).

- [ ] **Step 3: Smoke-test the template**

The template should render without Jinja syntax errors. Verify:

```
.venv/Scripts/python.exe -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'))
env.get_template('leaderboards.html')
print('OK')
"
```

Expected: `OK`.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/templates/leaderboards.html
git commit -m "feat(leaderboards): wrap averages-widget operator names in clickable buttons

Each operator name on every averages widget (per-WC and per-group,
active and inactive) becomes a transparent button carrying data-name
+ data-wc-or-group + data-start/end. Cert badges and the (count)
span stay outside the button so only the name is clickable.

Modal HTML and JS click handlers come in the next commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Leaderboards template — modal HTML, CSS, and JS

**Files:**
- Modify: `src/zira_dashboard/templates/leaderboards.html`

- [ ] **Step 1: Add modal CSS to the existing `<style>` block**

Append to the `<style>` block (a natural place is right after the `.lb-name-btn` rules from Task 6):

```css
  .popover-backdrop {
    position: fixed; inset: 0; background: rgba(0,0,0,0.55);
    display: none; align-items: center; justify-content: center; z-index: 1000;
  }
  .popover-backdrop.show { display: flex; }
  .popover {
    background: var(--panel); color: var(--fg);
    border: 1px solid var(--border); border-radius: 12px;
    padding: 1rem 1.25rem; max-width: 34rem; width: 90%;
    max-height: 80vh; overflow: auto;
    box-shadow: 0 12px 36px rgba(0,0,0,0.5);
  }
  .popover h4 { margin: 0 0 0.6rem; font-size: 0.95rem; font-weight: 700; }
  .popover .actions {
    display: flex; justify-content: flex-end; gap: 0.5rem;
    margin-top: 0.9rem; flex-wrap: wrap;
  }
  .popover .actions a, .popover .actions button {
    border: 1px solid var(--border); border-radius: 6px;
    padding: 0.4rem 0.85rem;
    font: inherit; font-size: 0.85rem; font-weight: 600;
    cursor: pointer; text-decoration: none;
    background: var(--panel-2); color: var(--fg);
  }
  .popover .actions a.primary,
  .popover .actions button.primary {
    background: var(--accent); color: white; border-color: var(--accent);
  }
  .popover .actions a:hover, .popover .actions button:hover { filter: brightness(1.08); }
  table.pc-popup { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
  table.pc-popup th, table.pc-popup td {
    padding: 0.4rem 0.55rem; border-bottom: 1px solid var(--border); text-align: left;
  }
  table.pc-popup th {
    color: var(--muted); font-size: 0.7rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  table.pc-popup td.num { text-align: right; font-variant-numeric: tabular-nums; }
  table.pc-popup a { color: var(--accent); }
```

- [ ] **Step 2: Add the modal HTML**

Find the closing `{% endblock %}` near the bottom of `leaderboards.html` (after the inactive sections block). Just before it, add:

```html
<div id="lb-popup-bd" class="popover-backdrop">
  <div class="popover" role="dialog" aria-modal="true" aria-labelledby="lb-popup-title">
    <h4 id="lb-popup-title">…</h4>
    <table class="pc-popup" id="lb-popup-table">
      <thead>
        <tr><th>Date</th><th>Work Centers</th><th class="num">Units</th><th class="num">Downtime</th></tr>
      </thead>
      <tbody></tbody>
    </table>
    <p id="lb-popup-empty" style="color:var(--muted);font-style:italic;margin-top:0.5rem;display:none">
      No production days for this person in the selected range.
    </p>
    <div class="actions">
      <a id="lb-popup-card-link" href="#" class="primary">Open full player card →</a>
      <button type="button" onclick="closeLbPopup()">Close</button>
    </div>
  </div>
</div>
```

- [ ] **Step 3: Add the JS**

Find the existing `<script>` block at the bottom of the page (search for `function toggleAll` — it's near other inline JS). Add the popup functions inside the same `<script>` block, or as a new `<script>` block right after:

```html
<script>
async function openLbPopup(btn) {
  const name = btn.dataset.name;
  const wc = btn.dataset.wc || '';
  const group = btn.dataset.group || '';
  const start = btn.dataset.start;
  const end = btn.dataset.end;

  const scopeLabel = wc ? wc : (group + ' group');
  document.getElementById('lb-popup-title').textContent =
    `${name} — ${scopeLabel} · ${start} → ${end}`;

  const cardUrl = `/staffing/people/${encodeURIComponent(name)}?start=${start}&end=${end}`;
  document.getElementById('lb-popup-card-link').href = cardUrl;

  const bd = document.getElementById('lb-popup-bd');
  bd.classList.add('show');
  const tbody = document.querySelector('#lb-popup-table tbody');
  tbody.innerHTML = '<tr><td colspan="4" style="color:var(--muted)">Loading…</td></tr>';
  document.getElementById('lb-popup-empty').style.display = 'none';

  const params = new URLSearchParams({ name, start, end });
  if (wc) params.set('wc', wc); else params.set('group', group);

  try {
    const r = await fetch('/api/staffing/leaderboards/person-days?' + params);
    if (!r.ok) {
      tbody.innerHTML = '<tr><td colspan="4" style="color:var(--bad)">Failed to load (HTTP ' + r.status + ').</td></tr>';
      return;
    }
    const data = await r.json();
    renderLbPopupRows(data.rows || []);
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="4" style="color:var(--bad)">Failed to load: ' + (e.message || 'network error') + '</td></tr>';
  }
}

function renderLbPopupRows(rows) {
  const tbody = document.querySelector('#lb-popup-table tbody');
  const empty = document.getElementById('lb-popup-empty');
  if (!rows.length) {
    tbody.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td><a href="/recycling?start=${r.date}&end=${r.date}">${r.date}</a></td>
      <td>${(r.wcs || []).join(', ')}</td>
      <td class="num">${Math.round(r.units || 0).toLocaleString()}</td>
      <td class="num">${Math.round(r.downtime || 0).toLocaleString()}</td>
    </tr>
  `).join('');
}

function closeLbPopup() {
  document.getElementById('lb-popup-bd').classList.remove('show');
}

// Backdrop click closes (but not clicks inside the popover content).
document.getElementById('lb-popup-bd').addEventListener('click', e => {
  if (e.target.id === 'lb-popup-bd') closeLbPopup();
});

// Esc closes.
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeLbPopup();
});
</script>
```

The `onclick="openLbPopup(this)"` on each `.lb-name-btn` (added in Task 6) calls into this function — no need for a separate `addEventListener` loop.

- [ ] **Step 4: Smoke-test the template parses**

```
.venv/Scripts/python.exe -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'))
env.get_template('leaderboards.html')
print('OK')
"
```

Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/leaderboards.html
git commit -m "feat(leaderboards): drill-down popup modal and click handlers

Modal HTML using the same .popover-backdrop / .popover style pattern
as past_schedules.html. JS opens on operator-name click, fetches the
new /api/staffing/leaderboards/person-days endpoint, renders rows
with date hyperlinks (drill into single-day dashboard) and a primary
"Open full player card" link carrying the page's selected timeframe.
Backdrop click and Esc key both close the modal.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Final test pass + CHANGELOG + push

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run the full non-DB test suite**

```
.venv/Scripts/python.exe -m pytest tests/test_progress.py tests/test_deps_window_dates.py tests/test_share_route.py tests/test_results.py tests/test_zira_persist.py tests/test_slack_client.py tests/test_late_report.py tests/test_views_store.py tests/test_wc_attributions.py tests/test_leaderboards_avg.py tests/test_production_history.py tests/test_leaderboards_person_days.py tests/test_player_card.py -q
```

Expected: all PASS (existing tests unchanged, new tests added in Tasks 1–5).

- [ ] **Step 2: Get the current local time**

```
date "+%I:%M %p"
```

Note the result for the CHANGELOG entry below.

- [ ] **Step 3: Add the CHANGELOG entry**

In `CHANGELOG.md`, find today's date heading (`## 2026-05-05`) and insert a new entry at the top of today's section (above the most recent existing entry):

```markdown
### {time-from-step-2}

- **Drill-down popups on the leaderboards averages widgets + per-day rows on the player card** — clicking any operator name on any averages widget on `/staffing/leaderboards` opens a modal showing that person's days contributing to the widget's average over the page's selected range. Each row's date hyperlinks to the `/recycling` dashboard for that single day, and a primary button on the modal opens the full player card (which now includes a per-day-per-WC breakdown table below the existing per-WC summary). Speed-first: the leaderboards page render adds zero work for this feature; popup data lazy-loads on click via a new JSON endpoint with TTL caching (1 h for past-only ranges, 60 s when today is included), so repeated opens skip re-aggregation entirely.
```

- [ ] **Step 4: Commit and push**

```bash
git add CHANGELOG.md
git commit -m "$(cat <<'EOF'
feat: leaderboards drill-down popup + player card per-day rows

Three-link drill-down chain on /staffing/leaderboards and
/staffing/people/{name}:

1. Player card adds a per-day-per-WC breakdown table below the
   existing per-WC summary. Date cells hyperlinked to the recycling
   dashboard for that day.
2. Operator names on every averages widget become clickable. Click
   opens a modal with that person's days contributing to the
   widget's average, scoped to the page's selected timeframe and
   the widget's WC or category.
3. Popup contains date hyperlinks (drill into a single day's
   dashboard) and an "Open full player card" button (full per-day
   breakdown with timeframe carried through).

Speed-first: leaderboards page render adds zero cost (data-attrs only).
Popup data lazy-loads via a new JSON endpoint with TTL caching
(1h past-only / 60s with today). Reuses production_history
attribution_for per-day data already produced today.

Spec: docs/superpowers/specs/2026-05-05-leaderboards-popup-and-player-card-days-design.md
Plan: docs/superpowers/plans/2026-05-05-leaderboards-popup-and-player-card-days.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin main
```

---

## Self-review checklist

- [ ] Spec section 1 (player card per-day) — covered by Tasks 1, 4, 5 ✓
- [ ] Spec section 2 (clickable averages names) — covered by Task 6 ✓
- [ ] Spec section 3 (popup → dashboard date links) — covered by Tasks 2, 7 ✓
- [ ] Spec section 4 (popup → player card link) — covered by Task 7 ✓
- [ ] Performance constraints (zero cost on leaderboards render, AJAX, TTL cache, no doubled fetch) — covered by Tasks 1, 2, 3, 6 ✓
- [ ] All 9 testing items in the spec — covered by Tasks 1, 2, 3, 4 (helper + endpoint + cache + render tests) ✓
- [ ] No placeholders / TODOs ✓
- [ ] All file paths exact, all code blocks complete ✓
- [ ] Type consistency: `attribution_per_day` signature matches between Tasks 1 (definition), 2/3 (endpoint), 4 (route) ✓
- [ ] `lb-name-btn` class consistent across CSS (Task 6), HTML wrap (Task 6), JS handler (Task 7) ✓
- [ ] Cache key format consistent: `(name, scope-key, start, end)` where scope-key is `wc:NAME` or `group:NAME` ✓
