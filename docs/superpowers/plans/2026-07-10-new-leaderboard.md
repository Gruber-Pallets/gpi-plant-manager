# New-Leaderboard Dashboard and TV Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build normal and TV New-Leaderboard surfaces that rank Juniors, Woodpecker, and Hand Build operators independently from Zira-backed production history and adapt from one active family to three.

**Architecture:** Extend the existing normalized-production and awards primitives with family-oriented pure helpers, then add one route module that renders normal and TV modes from the same payload. Family membership comes from `staffing.LOCATIONS` skills; the request performs one all-time `production_daily` read, and response caching prevents repeated work during TV refreshes.

**Tech Stack:** Python 3.11, FastAPI, Jinja2, server-rendered HTML, CSS, Postgres, pytest, Starlette TestClient.

**Approved spec:** `docs/superpowers/specs/2026-07-10-new-leaderboard-design.md`

## Global Constraints

- Exact display copy is `New-Leaderboard` everywhere.
- Exact routes are `/new-leaderboard` and `/tv/new-leaderboard`.
- TV registry kind is `vs_new_leaderboard`.
- Families are independent; never compare output across Junior, Woodpecker, and Hand Build.
- `Hand Build` includes Hand Build #1, Hand Build #2, and Big Build #1.
- A person-family-day qualifies at `hours >= 4.0` and uses the existing standard-full-day normalization.
- Families without current qualifying YTD or L30 rows are hidden.
- With one active family, show its table plus a right-side ribbon rail; with two or three, show equal family columns plus a full-width ribbon section below.
- Use one normalized-production read from `2024-01-01` through today per response-cache fill.
- Preserve existing Recycling-leaderboard behavior and payload shape.
- Do not add dependencies, change live `/new` KPIs, add meters, or redesign Trophy Case.

## File Structure

### New files

- `src/zira_dashboard/routes/new_leaderboard.py` — family mapping, payload assembly, normal/TV routes, error boundary, response caching.
- `src/zira_dashboard/templates/new_leaderboard_tv.html` — shared semantic markup for normal and TV modes.
- `src/zira_dashboard/static/new_leaderboard.css` — adaptive one/two/three-family layout and ribbon grid.
- `tests/test_new_leaderboard_routes.py` — family mapping, payload, route, theme, and error-state tests.
- `tests/test_new_leaderboard_static.py` — copy and structural CSS/template contracts.
- `scripts/preview_new_leaderboard.py` — deterministic Junior-only and three-family visual fixtures.

### Modified files

- `src/zira_dashboard/production_metrics.py` — generic family leaderboard builder; Recycling wrapper delegates to it.
- `tests/test_production_metrics.py` — family independence, active-family order, thresholds, and ribbons.
- `src/zira_dashboard/awards.py` — explicit work-center-set GOAT helper and public awards data floor.
- `tests/test_awards.py` — explicit scope, tie, and override tests.
- `src/zira_dashboard/app.py` — import and mount the new router.
- `src/zira_dashboard/templates/_dashboards_subnav.html` — New-Leaderboard tab.
- `src/zira_dashboard/tv_displays_store.py` — new kind, seed, and one-time backfill.
- `src/zira_dashboard/routes/tv_displays.py` — dispatcher and POST validation.
- `src/zira_dashboard/routes/settings.py` — TV picker option.
- `src/zira_dashboard/templates/_settings_tvs.html` — selected-option validation allowlist.
- `src/zira_dashboard/_schema.py` — new TV kind in create and migration constraints.
- `tests/test_tv_displays_store_unit.py` — idempotent backfill.
- `tests/test_tv_displays_store.py` — Postgres-gated save and seed count.
- `tests/test_tv_displays_routes.py` — registry dispatch.
- `tests/test_db.py` — Postgres constraint coverage.

---

### Task 1: Generalize Normalized Metrics to Arbitrary Families

**Files:**
- Modify: `src/zira_dashboard/production_metrics.py:118-245`
- Modify: `tests/test_production_metrics.py:89-147`

**Interfaces:**
- Consumes: existing `normalized_average_by_person()`, `_threshold()`, `_role_rows()`, `_best_ribbon()`, `_month_bounds()`, and `_add_months()`.
- Produces: `build_family_leaderboard(records: list[dict], *, today: date, standard_full_day_hours: float, family_wc_names: dict[str, set[str]]) -> dict`.
- Preserves: `build_recycling_leaderboard(...)->dict` with its existing `roles` and `repair`/`dismantler` ribbon keys.

- [ ] **Step 1: Write failing family-builder tests**

Append these tests to `tests/test_production_metrics.py`:

```python
def test_build_family_leaderboard_keeps_families_independent_and_ordered():
    records = [
        rec(date(2026, 7, 1), "Junior Operator", "Junior #2", 600, 7.0),
        rec(date(2026, 7, 1), "Wood Operator", "Woodpecker #1", 300, 7.0),
        rec(date(2026, 7, 1), "Builder", "Hand Build #1", 100, 3.0),
        rec(date(2026, 7, 1), "Builder", "Big Build #1", 80, 4.0),
    ]
    data = pm.build_family_leaderboard(
        records,
        today=date(2026, 7, 10),
        standard_full_day_hours=STD_HOURS,
        family_wc_names={
            "Juniors": {"Junior #1", "Junior #2", "Junior #3"},
            "Woodpecker": {"Woodpecker #1"},
            "Hand Build": {"Hand Build #1", "Hand Build #2", "Big Build #1"},
        },
    )
    assert data["active_families"] == ["Juniors", "Woodpecker", "Hand Build"]
    assert data["families"]["Juniors"]["rows"][0]["name"] == "Junior Operator"
    assert data["families"]["Woodpecker"]["rows"][0]["name"] == "Wood Operator"
    hand_build = data["families"]["Hand Build"]["rows"][0]
    assert hand_build["name"] == "Builder"
    assert hand_build["ytd"]["avg_units"] == 180.0


def test_build_family_leaderboard_hides_family_without_qualifying_rows():
    data = pm.build_family_leaderboard(
        [rec(date(2026, 7, 1), "Short Shift", "Woodpecker #1", 200, 3.99)],
        today=date(2026, 7, 10),
        standard_full_day_hours=STD_HOURS,
        family_wc_names={
            "Juniors": {"Junior #2"},
            "Woodpecker": {"Woodpecker #1"},
        },
    )
    assert data["active_families"] == []
    assert data["families"]["Woodpecker"]["rows"] == []


def test_build_family_leaderboard_first_day_threshold_and_ribbon():
    data = pm.build_family_leaderboard(
        [rec(date(2026, 7, 2), "Launch Operator", "Junior #2", 80, 4.0)],
        today=date(2026, 7, 10),
        standard_full_day_hours=STD_HOURS,
        family_wc_names={"Juniors": {"Junior #2"}},
    )
    assert data["families"]["Juniors"]["thresholds"] == {"ytd": 1, "l30": 1}
    assert data["ribbons"][0]["winners"]["Juniors"] == {
        "name": "Launch Operator",
        "day": date(2026, 7, 2),
        "amount": 140.0,
        "days": 1,
    }
```

- [ ] **Step 2: Run the tests and confirm the missing interface**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_production_metrics.py -v
```

Expected: the three new tests fail with `AttributeError: module 'zira_dashboard.production_metrics' has no attribute 'build_family_leaderboard'`; existing tests remain green.

- [ ] **Step 3: Implement the generic builder**

Add this function above `build_recycling_leaderboard()` in `src/zira_dashboard/production_metrics.py`:

```python
def build_family_leaderboard(
    records: list[dict],
    *,
    today: date,
    standard_full_day_hours: float,
    family_wc_names: dict[str, set[str]],
) -> dict:
    ytd_start = date(today.year, 1, 1)
    ytd_end = today
    l30_start = today - timedelta(days=29)
    l30_end = today
    ytd_records = [r for r in records if ytd_start <= r["day"] <= ytd_end]
    l30_records = [r for r in records if l30_start <= r["day"] <= l30_end]

    families: dict[str, dict] = {}
    active_families: list[str] = []
    for family, wc_names in family_wc_names.items():
        ytd_rows = normalized_average_by_person(
            ytd_records,
            wc_names=wc_names,
            standard_full_day_hours=standard_full_day_hours,
        )
        l30_rows = normalized_average_by_person(
            l30_records,
            wc_names=wc_names,
            standard_full_day_hours=standard_full_day_hours,
        )
        ytd_threshold = _threshold(ytd_rows)
        l30_threshold = _threshold(l30_rows)
        rows = _role_rows(
            ytd_rows=ytd_rows,
            l30_rows=l30_rows,
            ytd_threshold=ytd_threshold,
            l30_threshold=l30_threshold,
        )
        families[family] = {
            "rows": rows,
            "thresholds": {"ytd": ytd_threshold, "l30": l30_threshold},
        }
        if rows:
            active_families.append(family)

    ribbons: list[dict] = []
    current_month = date(today.year, today.month, 1)
    for offset in range(12):
        month_start = _add_months(current_month, -offset)
        start, end = _month_bounds(month_start.year, month_start.month, today)
        month_records = [r for r in records if start <= r["day"] <= end]
        ribbons.append(
            {
                "year": month_start.year,
                "month": month_start.month,
                "month_label": month_abbr[month_start.month],
                "winners": {
                    family: _best_ribbon(
                        month_records,
                        wc_names=wc_names,
                        standard_full_day_hours=standard_full_day_hours,
                    )
                    for family, wc_names in family_wc_names.items()
                },
            }
        )

    return {
        "ytd_start": ytd_start,
        "ytd_end": ytd_end,
        "l30_start": l30_start,
        "l30_end": l30_end,
        "families": families,
        "active_families": active_families,
        "ribbons": ribbons,
    }
```

Refactor `build_recycling_leaderboard()` so it delegates to the generic helper and translates only the stable legacy envelope:

```python
def build_recycling_leaderboard(
    records: list[dict],
    *,
    today: date,
    standard_full_day_hours: float,
    wc_role_by_name: dict[str, str],
) -> dict:
    family_wc_names = {
        "Repair": {wc for wc, role in wc_role_by_name.items() if role == "Repair"},
        "Dismantler": {
            wc for wc, role in wc_role_by_name.items() if role == "Dismantler"
        },
    }
    data = build_family_leaderboard(
        records,
        today=today,
        standard_full_day_hours=standard_full_day_hours,
        family_wc_names=family_wc_names,
    )
    return {
        "ytd_start": data["ytd_start"],
        "ytd_end": data["ytd_end"],
        "l30_start": data["l30_start"],
        "l30_end": data["l30_end"],
        "roles": data["families"],
        "ribbons": [
            {
                "year": row["year"],
                "month": row["month"],
                "month_label": row["month_label"],
                "repair": row["winners"]["Repair"],
                "dismantler": row["winners"]["Dismantler"],
            }
            for row in data["ribbons"]
        ],
    }
```

- [ ] **Step 4: Run metric regression tests**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_production_metrics.py tests/test_recycling_leaderboard_tv.py -v
```

Expected: all tests pass; Recycling-leaderboard route and payload compatibility stay intact.

- [ ] **Step 5: Commit the generic metrics layer**

```bash
git add src/zira_dashboard/production_metrics.py tests/test_production_metrics.py
git commit -m "feat: add family production leaderboard metrics"
```

---

### Task 2: Add Skill-Derived GOAT Computation Without Extra Reads

**Files:**
- Modify: `src/zira_dashboard/awards.py:12-147,198-273`
- Modify: `tests/test_awards.py:1-220,330-365`

**Interfaces:**
- Produces: `AWARDS_DATA_FLOOR = date(2024, 1, 1)`.
- Produces: `person_days_in_wc_names(wc_names: set[str], start: date, end: date, records: list[dict] | None = None) -> list[dict]`.
- Produces: `goat_for_wc_names(wc_names: set[str], *, group_name: str, records: list[dict], today: date, overrides: list[dict] | None = None) -> dict | None`.
- Produces: `load_overrides() -> list[dict]` as a public wrapper around the existing override query.
- Preserves: `goat(group_name)` cache and ranking behavior.

- [ ] **Step 1: Write failing explicit-WC GOAT tests**

Add to `tests/test_awards.py`:

```python
def test_goat_for_wc_names_uses_only_explicit_family_and_applies_override():
    from zira_dashboard import awards

    records = [
        {"day": date(2026, 7, 1), "person": "Junior Winner", "wc": "Junior #2",
         "units": 700.0, "hours": 7.0, "downtime": 0.0},
        {"day": date(2026, 7, 2), "person": "Other Family", "wc": "Repair 1",
         "units": 999.0, "hours": 7.0, "downtime": 0.0},
    ]
    overrides = [
        {"scope": "award_goat", "group_name": "Juniors", "wc_name": None,
         "year": None, "month": None, "position": 1,
         "action": "replace", "name": "Verified Junior"},
    ]
    winner = awards.goat_for_wc_names(
        {"Junior #1", "Junior #2", "Junior #3"},
        group_name="Juniors",
        records=records,
        today=date(2026, 7, 10),
        overrides=overrides,
    )
    assert winner["name"] == "Verified Junior"
    assert winner["units"] == 700.0


def test_goat_for_wc_names_first_day_wins_exact_tie():
    from zira_dashboard import awards

    records = [
        {"day": date(2026, 7, 2), "person": "Later", "wc": "Junior #2",
         "units": 700.0, "hours": 7.0, "downtime": 0.0},
        {"day": date(2026, 7, 1), "person": "First", "wc": "Junior #2",
         "units": 700.0, "hours": 7.0, "downtime": 0.0},
    ]
    winner = awards.goat_for_wc_names(
        {"Junior #2"},
        group_name="Juniors",
        records=records,
        today=date(2026, 7, 10),
        overrides=[],
    )
    assert winner["name"] == "First"
    assert winner["day"] == date(2026, 7, 1)


def test_goat_for_wc_names_returns_none_without_positive_units():
    from zira_dashboard import awards

    winner = awards.goat_for_wc_names(
        {"Junior #2"},
        group_name="Juniors",
        records=[
            {"day": date(2026, 7, 1), "person": "Zero", "wc": "Junior #2",
             "units": 0.0, "hours": 7.0, "downtime": 0.0},
        ],
        today=date(2026, 7, 10),
        overrides=[],
    )
    assert winner is None
```

- [ ] **Step 2: Run the focused awards tests and confirm failure**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_awards.py -k 'goat_for_wc_names' -v
```

Expected: all three tests fail because `goat_for_wc_names` does not exist.

- [ ] **Step 3: Extract the shared award primitives**

In `src/zira_dashboard/awards.py`, replace `_ALL_TIME_FLOOR` with the public constant and make group aggregation delegate to explicit work-center aggregation:

```python
AWARDS_DATA_FLOOR = date(2024, 1, 1)


def person_days_in_wc_names(
    wc_names: set[str],
    start: date,
    end: date,
    records: list[dict] | None = None,
) -> list[dict]:
    if not wc_names:
        return []
    raw = _records_for(start, end, records)
    agg: dict[tuple[str, date], dict] = defaultdict(
        lambda: {"units": 0.0, "hours": 0.0}
    )
    for row in raw:
        if row["wc"] not in wc_names:
            continue
        key = (row["person"], row["day"])
        agg[key]["units"] += row["units"]
        agg[key]["hours"] += row["hours"]
    return [
        {"name": person, "day": day, "units": values["units"], "hours": values["hours"]}
        for (person, day), values in agg.items()
        if values["units"] > 0
    ]


def person_days_in_group(
    group_name: str,
    start: date,
    end: date,
    records: list[dict] | None = None,
) -> list[dict]:
    return person_days_in_wc_names(
        _wc_names_for_group(group_name),
        start,
        end,
        records=records,
    )


def _goat_from_rows(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    top = sorted(rows, key=lambda row: (-row["units"], row["day"], row["name"]))[0]
    pph = round(top["units"] / top["hours"], 1) if top["hours"] > 0 else 0.0
    return {
        "name": top["name"],
        "day": top["day"],
        "units": top["units"],
        "pph": pph,
    }
```

Refactor `goat()` to call `_goat_from_rows()` and use `AWARDS_DATA_FLOOR`. Then add:

```python
def goat_for_wc_names(
    wc_names: set[str],
    *,
    group_name: str,
    records: list[dict],
    today: date,
    overrides: list[dict] | None = None,
) -> dict | None:
    rows = person_days_in_wc_names(
        wc_names,
        AWARDS_DATA_FLOOR,
        today,
        records=records,
    )
    live = _goat_from_rows(rows)
    return apply_overrides_single(
        live,
        scope="award_goat",
        group_name=group_name,
        overrides=overrides,
    )


def load_overrides() -> list[dict]:
    return _load_overrides()
```

- [ ] **Step 4: Run all awards tests**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_awards.py -v
```

Expected: all tests pass, including the pre-existing cached `goat()` tests.

- [ ] **Step 5: Commit the awards extension**

```bash
git add src/zira_dashboard/awards.py tests/test_awards.py
git commit -m "feat: compute goats for explicit work center families"
```

---

### Task 3: Add the New-Leaderboard Payload and Routes

**Files:**
- Create: `src/zira_dashboard/routes/new_leaderboard.py`
- Create: `src/zira_dashboard/templates/new_leaderboard_tv.html`
- Create: `tests/test_new_leaderboard_routes.py`
- Modify: `src/zira_dashboard/app.py:25-55,503-511`

**Interfaces:**
- Consumes: `production_metrics.build_family_leaderboard`, `awards.AWARDS_DATA_FLOOR`, `awards.goat_for_wc_names`, `awards.load_overrides`, `production_history.normalized_daily_records`, and `staffing.LOCATIONS`.
- Produces: `_family_wc_names(locations=None) -> dict[str, set[str]]`.
- Produces: `_leaderboard_payload(today: date) -> dict` with `families`, `active_families`, `ribbons`, `current_goats`, date ranges, and `error_message`.
- Produces: `render_new_leaderboard_tv(request: Request, *, tv_theme: str = "dark") -> HTMLResponse`.

- [ ] **Step 1: Write failing route and family-mapping tests**

Create `tests/test_new_leaderboard_routes.py`:

```python
from datetime import date

import pytest
from fastapi.testclient import TestClient

from zira_dashboard import _http_cache
from zira_dashboard.app import app


@pytest.fixture(autouse=True)
def _clear_response_cache():
    _http_cache.invalidate_today_cache()
    yield
    _http_cache.invalidate_today_cache()


def fake_payload():
    return {
        "ytd_start": date(2026, 1, 1),
        "ytd_end": date(2026, 7, 10),
        "l30_start": date(2026, 6, 11),
        "l30_end": date(2026, 7, 10),
        "active_families": ["Juniors"],
        "families": {
            "Juniors": {
                "thresholds": {"ytd": 1, "l30": 1},
                "rows": [{
                    "rank": 1,
                    "name": "Junior Operator",
                    "ytd": {"eligible": True, "avg_units": 640.0, "days": 10, "label": None},
                    "l30": {"eligible": True, "avg_units": 660.0, "days": 4, "label": None},
                }],
            },
            "Woodpecker": {"thresholds": {"ytd": 0, "l30": 0}, "rows": []},
            "Hand Build": {"thresholds": {"ytd": 0, "l30": 0}, "rows": []},
        },
        "ribbons": [{
            "year": 2026,
            "month": 7,
            "month_label": "Jul",
            "winners": {
                "Juniors": {
                    "name": "Junior Operator",
                    "day": date(2026, 7, 2),
                    "amount": 700.0,
                    "days": 1,
                },
            },
        }],
        "current_goats": [{
            "label": "Junior GOAT",
            "group": "Juniors",
            "name": "Junior Operator",
            "units": 700.0,
            "day": date(2026, 7, 2),
        }],
        "error_message": None,
    }


def test_family_wc_names_include_big_build_with_hand_build():
    from zira_dashboard.routes import new_leaderboard
    from zira_dashboard.staffing import Location

    locations = [
        Location("Junior #2", "Junior", "Bay 17", "New", "42345"),
        Location("Woodpecker #1", "Woodpecker", "Bay 16", "New", None),
        Location("Hand Build #1", "Hand Build", "Bay 6", "New", None),
        Location("Big Build #1", "Hand Build", "Bay 14", "New", None),
        Location("Repair 1", "Repair", "Bay 1", "Recycled", "40721"),
    ]
    assert new_leaderboard._family_wc_names(locations) == {
        "Juniors": {"Junior #2"},
        "Woodpecker": {"Woodpecker #1"},
        "Hand Build": {"Hand Build #1", "Big Build #1"},
    }


def test_dashboard_new_leaderboard_renders_junior_only(monkeypatch):
    monkeypatch.setattr(
        "zira_dashboard.routes.new_leaderboard._leaderboard_payload",
        lambda today: fake_payload(),
    )
    response = TestClient(app).get("/new-leaderboard")
    assert response.status_code == 200
    assert "New-Leaderboard" in response.text
    assert "Junior Operator" in response.text
    assert "Woodpecker #1" not in response.text
    assert 'href="/new-leaderboard"' in response.text
    assert "tv-refresh.js" not in response.text


def test_tv_new_leaderboard_renders_dark_and_refreshes(monkeypatch):
    monkeypatch.setattr(
        "zira_dashboard.routes.new_leaderboard._leaderboard_payload",
        lambda today: fake_payload(),
    )
    response = TestClient(app).get("/tv/new-leaderboard?theme=dark")
    assert response.status_code == 200
    assert 'data-tv-theme="dark"' in response.text
    assert "CURRENT GOATS" in response.text
    assert "tv-refresh.js" in response.text


def test_direct_tv_new_leaderboard_uses_saved_theme(monkeypatch):
    from zira_dashboard import tv_displays_store
    from zira_dashboard.routes import new_leaderboard

    monkeypatch.setattr(
        tv_displays_store,
        "by_slug",
        lambda slug: {"slug": slug, "theme": "light"},
    )

    def fake_render(request, *, tv_theme="dark"):
        from fastapi.responses import HTMLResponse
        return HTMLResponse(f'<html data-tv-theme="{tv_theme}">ok</html>')

    monkeypatch.setattr(
        new_leaderboard,
        "render_new_leaderboard_tv",
        fake_render,
    )
    response = TestClient(app).get("/tv/new-leaderboard")
    assert response.status_code == 200
    assert 'data-tv-theme="light"' in response.text


def test_new_leaderboard_no_data_state(monkeypatch):
    payload = fake_payload()
    payload["active_families"] = []
    payload["current_goats"] = []
    payload["ribbons"] = []
    for block in payload["families"].values():
        block["rows"] = []
    monkeypatch.setattr(
        "zira_dashboard.routes.new_leaderboard._leaderboard_payload",
        lambda today: payload,
    )
    response = TestClient(app).get("/new-leaderboard")
    assert response.status_code == 200
    assert "Waiting for qualifying Zira production." in response.text


def test_new_leaderboard_data_error_keeps_shell_and_refresh(monkeypatch):
    def fail(today):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "zira_dashboard.routes.new_leaderboard._leaderboard_payload",
        fail,
    )
    response = TestClient(app).get("/tv/new-leaderboard")
    assert response.status_code == 200
    assert "New-Leaderboard" in response.text
    assert "Production data is temporarily unavailable." in response.text
    assert "tv-refresh.js" in response.text


def test_payload_goat_failure_omits_chip_but_keeps_family_data(monkeypatch):
    from zira_dashboard.routes import new_leaderboard

    payload = fake_payload()
    payload.pop("current_goats")
    payload.pop("error_message")
    monkeypatch.setattr(
        new_leaderboard.production_history,
        "normalized_daily_records",
        lambda start, end: [],
    )
    monkeypatch.setattr(
        new_leaderboard.production_metrics,
        "build_family_leaderboard",
        lambda records, **kwargs: payload,
    )
    monkeypatch.setattr(new_leaderboard.awards, "load_overrides", lambda: [])
    monkeypatch.setattr(
        new_leaderboard.awards,
        "goat_for_wc_names",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("goat failed")),
    )
    data = new_leaderboard._leaderboard_payload(date(2026, 7, 10))
    assert data["active_families"] == ["Juniors"]
    assert data["current_goats"] == []


def test_new_leaderboard_response_cache_avoids_duplicate_payload(monkeypatch):
    calls = []

    def build(today):
        calls.append(today)
        return fake_payload()

    monkeypatch.setattr(
        "zira_dashboard.routes.new_leaderboard._leaderboard_payload",
        build,
    )
    client = TestClient(app)
    assert client.get("/new-leaderboard").status_code == 200
    assert client.get("/new-leaderboard").status_code == 200
    assert len(calls) == 1
```

- [ ] **Step 2: Run the new route tests and confirm import failure**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_new_leaderboard_routes.py -v
```

Expected: tests fail with an import error for `zira_dashboard.routes.new_leaderboard` and 404 responses for both new URLs.

- [ ] **Step 3: Implement family mapping, payload assembly, error boundary, and cache**

Create `src/zira_dashboard/routes/new_leaderboard.py` with these concrete elements:

```python
from __future__ import annotations

from datetime import date
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .. import awards, production_history, production_metrics, shift_config, staffing
from .._http_cache import get_cached_response, set_cache_headers, store_cached_response
from ..deps import templates
from ..plant_day import today as plant_today

router = APIRouter()
_log = logging.getLogger(__name__)

_FAMILY_SKILLS = (
    ("Juniors", "Junior", "Junior GOAT"),
    ("Woodpecker", "Woodpecker", "Woodpecker GOAT"),
    ("Hand Build", "Hand Build", "Hand Build GOAT"),
)


def _family_wc_names(locations=None) -> dict[str, set[str]]:
    source = staffing.LOCATIONS if locations is None else locations
    return {
        family: {loc.name for loc in source if loc.skill == skill}
        for family, skill, _goat_label in _FAMILY_SKILLS
    }


def _leaderboard_payload(today: date) -> dict:
    records = production_history.normalized_daily_records(
        awards.AWARDS_DATA_FLOOR,
        today,
    )
    family_wc_names = _family_wc_names()
    data = production_metrics.build_family_leaderboard(
        records,
        today=today,
        standard_full_day_hours=shift_config.productive_minutes_per_day() / 60.0,
        family_wc_names=family_wc_names,
    )
    try:
        overrides = awards.load_overrides()
    except Exception:
        _log.exception("New-Leaderboard award overrides failed")
        overrides = []
    goat_labels = {family: label for family, _skill, label in _FAMILY_SKILLS}
    goats: list[dict] = []
    for family in data["active_families"]:
        try:
            winner = awards.goat_for_wc_names(
                family_wc_names[family],
                group_name=family,
                records=records,
                today=today,
                overrides=overrides,
            )
        except Exception:
            _log.exception("New-Leaderboard GOAT lookup failed for %s", family)
            winner = None
        if winner is not None and winner.get("name"):
            goats.append({
                "label": goat_labels[family],
                "group": family,
                "name": winner["name"],
                "units": winner.get("units"),
                "day": winner.get("day"),
            })
    data["current_goats"] = goats
    data["error_message"] = None
    return data


def _empty_payload(today: date, message: str) -> dict:
    data = production_metrics.build_family_leaderboard(
        [],
        today=today,
        standard_full_day_hours=shift_config.productive_minutes_per_day() / 60.0,
        family_wc_names=_family_wc_names(),
    )
    data["current_goats"] = []
    data["error_message"] = message
    return data


def _render_new_leaderboard(
    request: Request,
    *,
    tv_mode: bool,
    tv_theme: str = "dark",
) -> HTMLResponse:
    today = plant_today()
    safe_theme = tv_theme if tv_theme in ("light", "dark") else "dark"
    cache_key = ("new_leaderboard", today.isoformat(), tv_mode, safe_theme)
    cached = get_cached_response(cache_key, includes_today=True)
    if cached is not None:
        return cached
    try:
        data = _leaderboard_payload(today)
    except Exception:
        _log.exception("New-Leaderboard payload failed")
        data = _empty_payload(today, "Production data is temporarily unavailable.")
    context = {
        "tv_mode": tv_mode,
        "tv_theme": safe_theme,
        "data": data,
        "active_dashboard_key": "vs_new_leaderboard",
    }
    response = templates.TemplateResponse(request, "new_leaderboard_tv.html", context)
    set_cache_headers(response, includes_today=True)
    store_cached_response(cache_key, includes_today=True, response=response)
    return response


def render_new_leaderboard_tv(
    request: Request,
    *,
    tv_theme: str = "dark",
) -> HTMLResponse:
    return _render_new_leaderboard(request, tv_mode=True, tv_theme=tv_theme)


@router.get("/new-leaderboard", response_class=HTMLResponse)
def new_leaderboard(request: Request):
    return _render_new_leaderboard(request, tv_mode=False)


@router.get("/tv/new-leaderboard", response_class=HTMLResponse)
def tv_new_leaderboard(request: Request, theme: str | None = Query(default=None)):
    from .. import tv_displays_store

    try:
        row = tv_displays_store.by_slug("new-leaderboard")
    except Exception:
        row = None
    stored_theme = row["theme"] if row is not None else "dark"
    tv_theme = "light" if theme == "light" else (
        "dark" if theme == "dark" else stored_theme
    )
    return render_new_leaderboard_tv(request, tv_theme=tv_theme)
```

- [ ] **Step 4: Add the semantic shared template**

Create `src/zira_dashboard/templates/new_leaderboard_tv.html` with the full semantic structure. Reuse the existing Recycling-leaderboard class names for table/panel typography; Task 4 adds only the adaptive layout rules:

```html
{% from "_tv_header.html" import tv_header %}
{% set is_tv = tv_mode | default(true) %}
<!doctype html>
<html lang="en"{% if is_tv %} data-tv-theme="{{ tv_theme or 'dark' }}"{% endif %}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="/static/gpi-logo.png">
{% if is_tv %}<title>New-Leaderboard</title>{% else %}<title>New-Leaderboard - GPI Plant Manager</title>{% endif %}
{% if is_tv %}
<link rel="stylesheet" href="/static/tv-mode.css?v={{ static_v('tv-mode.css') }}">
{% else %}
<link rel="stylesheet" href="/static/topnav.css?v={{ static_v('topnav.css') }}">
<link rel="stylesheet" href="/static/dashboards-subnav.css?v={{ static_v('dashboards-subnav.css') }}">
{% endif %}
<link rel="stylesheet" href="/static/recycling_leaderboard.css?v={{ static_v('recycling_leaderboard.css') }}">
<link rel="stylesheet" href="/static/new_leaderboard.css?v={{ static_v('new_leaderboard.css') }}">
{% if is_tv %}<script defer src="/static/tv-refresh.js?v={{ static_v('tv-refresh.js') }}"></script>{% endif %}
</head>
<body class="{% if is_tv %}new-leaderboard-tv{% else %}new-leaderboard-page{% endif %}">
  {% if is_tv %}
  {{ tv_header(
      "New-Leaderboard",
      crumb="NEW",
      right_label="CURRENT GOATS",
      right_items=data.current_goats,
      right_class="rlb-goat-banner",
  ) }}
  {% else %}
  <header>{% set active_nav = 'dashboards' %}{% include "_topnav.html" %}</header>
  {% include "_dashboards_subnav.html" %}
  {% endif %}
  <main class="rlb-main">
    <div class="rlb-range">
      <span>YTD: {{ data.ytd_start.strftime('%b %-d') }}-{{ data.ytd_end.strftime('%b %-d') }}</span>
      <span>L30: {{ data.l30_start.strftime('%b %-d') }}-{{ data.l30_end.strftime('%b %-d') }}</span>
    </div>
    {% if data.error_message %}
      <section class="rlb-panel nlb-state"><h2>{{ data.error_message }}</h2></section>
    {% elif not data.active_families %}
      <section class="rlb-panel nlb-state"><h2>Waiting for qualifying Zira production.</h2></section>
    {% else %}
    <section class="nlb-grid nlb-family-count-{{ data.active_families|length }}">
      <div class="nlb-family-panels">
      {% for family in data.active_families %}
        {% set block = data.families[family] %}
        <section class="rlb-panel nlb-family-panel">
          <div class="rlb-panel-head">
            <div><h2>{{ family }}</h2><p>Sorted by YTD full-day avg</p></div>
            <div class="rlb-thresholds"><div>YTD min {{ block.thresholds.ytd }} days</div><div>L30 min {{ block.thresholds.l30 }} days</div></div>
          </div>
          <table class="rlb-table">
            <colgroup><col class="rlb-rank-col"><col class="rlb-name-col"><col class="rlb-score-col"><col class="rlb-score-col"></colgroup>
            <thead><tr><th>#</th><th>Name</th><th class="num">YTD Avg</th><th class="num">L30 Avg</th></tr></thead>
            <tbody>
            {% for row in block.rows %}
              <tr>
                <td class="rank">{{ row.rank }}</td>
                <td class="name" aria-label="{{ row.name }}">{{ row.name }}</td>
                <td class="num">{% if row.ytd.eligible %}<span class="score">{{ "%.1f"|format(row.ytd.avg_units) }}</span><span class="days">{{ row.ytd.days }} days</span>{% else %}<span class="not-enough">not enough days</span>{% endif %}</td>
                <td class="num">{% if row.l30.eligible %}<span class="score l30">{{ "%.1f"|format(row.l30.avg_units) }}</span><span class="days">{{ row.l30.days }} days</span>{% else %}<span class="not-enough">not enough days</span>{% endif %}</td>
              </tr>
            {% endfor %}
            </tbody>
          </table>
        </section>
      {% endfor %}
      </div>
      <section class="rlb-panel nlb-ribbons">
        <div class="rlb-panel-head"><div><h2>Gold Ribbons</h2><p>Best normalized full day</p></div></div>
        <div class="nlb-ribbon-grid" style="--nlb-family-count: {{ data.active_families|length }}">
          <span></span>{% for family in data.active_families %}<strong>{{ family }}</strong>{% endfor %}
          {% for month in data.ribbons %}
            <strong class="nlb-month">{{ month.month_label }}</strong>
            {% for family in data.active_families %}
              {% set winner = month.winners[family] %}
              <span class="nlb-ribbon-cell">{% if winner %}<strong>{{ winner.name }}</strong><small>{{ winner.day.strftime('%b %-d') }} - {{ "%.0f"|format(winner.amount) }}</small>{% else %}<strong>-</strong>{% endif %}</span>
            {% endfor %}
          {% endfor %}
        </div>
      </section>
    </section>
    {% endif %}
  </main>
</body>
</html>
```

- [ ] **Step 5: Mount the router**

In `src/zira_dashboard/app.py`, add `new_leaderboard` to the `.routes` import tuple immediately after `goat_watch`, and add:

```python
app.include_router(new_leaderboard.router)
```

immediately after `app.include_router(recycling_leaderboard.router)`.

- [ ] **Step 6: Run route tests**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_new_leaderboard_routes.py -v
```

Expected: all route and error-state tests pass. CSS requests are not part of these TestClient assertions yet.

- [ ] **Step 7: Commit the functional routes and template**

```bash
git add src/zira_dashboard/routes/new_leaderboard.py src/zira_dashboard/templates/new_leaderboard_tv.html src/zira_dashboard/app.py tests/test_new_leaderboard_routes.py
git commit -m "feat: add new leaderboard dashboard routes"
```

---
### Task 4: Implement the Approved Adaptive Visual Layout

**Files:**
- Create: `src/zira_dashboard/static/new_leaderboard.css`
- Create: `tests/test_new_leaderboard_static.py`
- Modify: `src/zira_dashboard/templates/new_leaderboard_tv.html:18-74`
- Modify: `tests/test_new_leaderboard_routes.py`

**Interfaces:**
- Consumes: `data.active_families`, `data.families`, `data.ribbons`, and the stable `.rlb-*` styles from `recycling_leaderboard.css`.
- Produces: `.nlb-family-count-1`, `.nlb-family-count-2`, and `.nlb-family-count-3` layouts.
- Produces: a ribbon grid driven by CSS custom property `--nlb-family-count`.

- [ ] **Step 1: Write static visual-contract tests**

Create `tests/test_new_leaderboard_static.py`:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "src/zira_dashboard/templates/new_leaderboard_tv.html").read_text()
CSS = (ROOT / "src/zira_dashboard/static/new_leaderboard.css").read_text()
RECYCLING_CSS = (ROOT / "src/zira_dashboard/static/recycling_leaderboard.css").read_text()


def test_new_leaderboard_uses_recycling_visual_base_and_own_layout_css():
    assert "/static/recycling_leaderboard.css" in TEMPLATE
    assert "/static/new_leaderboard.css" in TEMPLATE
    assert "recycling-leaderboard-tv new-leaderboard-tv" in TEMPLATE
    assert "recycling-leaderboard-page new-leaderboard-page" in TEMPLATE


def test_new_leaderboard_layout_responds_to_active_family_count():
    assert "nlb-family-count-{{ data.active_families|length }}" in TEMPLATE
    assert ".nlb-family-count-1" in CSS
    assert ".nlb-family-count-2" in CSS
    assert ".nlb-family-count-3" in CSS
    assert "repeat(2, minmax(0, 1fr))" in CSS
    assert "repeat(3, minmax(0, 1fr))" in CSS


def test_new_leaderboard_ribbon_grid_is_family_driven():
    assert "--nlb-family-count: {{ data.active_families|length }}" in TEMPLATE
    assert "repeat(var(--nlb-family-count), minmax(0, 1fr))" in CSS
    assert "month.winners[family]" in TEMPLATE


def test_new_leaderboard_copy_and_empty_states_are_exact():
    assert "New-Leaderboard" in TEMPLATE
    assert "Waiting for qualifying Zira production." in TEMPLATE
    assert "Production data is temporarily unavailable." not in TEMPLATE
    assert "not enough days" in TEMPLATE


def test_new_leaderboard_has_mobile_stack_and_name_safety():
    assert "@media (max-width: 1100px)" in CSS
    name_start = RECYCLING_CSS.index(".rlb-table .name")
    name_end = RECYCLING_CSS.index(".rlb-table .num", name_start)
    assert "text-overflow: ellipsis" in RECYCLING_CSS[name_start:name_end]
    assert 'aria-label="{{ row.name }}"' in TEMPLATE
```

The temporary-unavailable text is supplied by route data, so the template must not hard-code a second copy.

Add a rendered-count contract to `tests/test_new_leaderboard_routes.py`:

```python
@pytest.mark.parametrize(
    ("active", "count"),
    [
        (["Juniors", "Woodpecker"], 2),
        (["Juniors", "Woodpecker", "Hand Build"], 3),
    ],
)
def test_new_leaderboard_renders_active_family_count(monkeypatch, active, count):
    payload = fake_payload()
    payload["active_families"] = active
    junior_block = payload["families"]["Juniors"]
    junior_winner = payload["ribbons"][0]["winners"]["Juniors"]
    for family in active[1:]:
        payload["families"][family] = junior_block
        payload["ribbons"][0]["winners"][family] = junior_winner
    monkeypatch.setattr(
        "zira_dashboard.routes.new_leaderboard._leaderboard_payload",
        lambda today: payload,
    )
    response = TestClient(app).get("/new-leaderboard")
    assert response.status_code == 200
    assert f"nlb-family-count-{count}" in response.text
    for family in active:
        assert family in response.text
```

- [ ] **Step 2: Run static tests and confirm missing CSS failure**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_new_leaderboard_static.py -v
```

Expected: tests fail because `new_leaderboard.css` is absent and the template body does not yet carry the Recycling-leaderboard base classes.

- [ ] **Step 3: Reuse the stable Recycling classes on the body**

Change the template body tag to:

```html
<body class="{% if is_tv %}recycling-leaderboard-tv new-leaderboard-tv{% else %}recycling-leaderboard-page new-leaderboard-page{% endif %}">
```

This activates the existing body, panel, table, range, GOAT-chip, and normal-dashboard typography without copying those rules.

- [ ] **Step 4: Add the adaptive CSS**

Create `src/zira_dashboard/static/new_leaderboard.css`:

```css
.nlb-grid {
  display: grid;
  gap: clamp(6px, 0.6vw, 12px);
  height: calc(100vh - 7.2rem);
  min-height: 0;
}

.nlb-family-panels {
  display: grid;
  gap: clamp(6px, 0.6vw, 12px);
  min-width: 0;
  min-height: 0;
}

.nlb-family-panel,
.nlb-ribbons {
  min-width: 0;
  min-height: 0;
}

.nlb-family-count-1 {
  grid-template-columns: minmax(0, 1.8fr) minmax(18rem, 1fr);
}

.nlb-family-count-1 .nlb-family-panels {
  grid-template-columns: minmax(0, 1fr);
}

.nlb-family-count-2,
.nlb-family-count-3 {
  grid-template-rows: minmax(0, 1fr) minmax(14rem, 0.42fr);
}

.nlb-family-count-2 .nlb-family-panels {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.nlb-family-count-3 .nlb-family-panels {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.nlb-ribbons {
  display: flex;
  flex-direction: column;
}

.nlb-ribbon-grid {
  flex: 1 1 auto;
  min-height: 0;
  display: grid;
  grid-template-columns: 2.6rem repeat(var(--nlb-family-count), minmax(0, 1fr));
  grid-template-rows: auto repeat(12, minmax(0, 1fr));
  gap: 0.18rem;
}

.nlb-ribbon-grid > strong,
.nlb-ribbon-grid > span {
  min-width: 0;
  display: flex;
  align-items: center;
  padding: 0.18rem 0.28rem;
}

.nlb-ribbon-grid > strong:not(.nlb-month) {
  color: #fbbf24;
  font-size: clamp(0.55rem, 0.75vw, 0.95rem);
  text-transform: uppercase;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.nlb-month {
  justify-content: center;
  color: #fbbf24;
}

.nlb-ribbon-cell {
  border: 1px solid rgba(251, 191, 36, 0.28);
  background: rgba(251, 191, 36, 0.08);
  border-radius: 6px;
  flex-direction: column;
  justify-content: center;
  align-items: flex-start !important;
}

.nlb-ribbon-cell strong,
.nlb-ribbon-cell small {
  display: block;
  max-width: 100%;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.nlb-ribbon-cell small {
  color: var(--muted);
  font-size: clamp(0.6rem, 0.75vw, 0.9rem);
}

.nlb-state {
  min-height: min(55vh, 32rem);
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
}

body.new-leaderboard-page .nlb-grid {
  height: auto;
}

body.new-leaderboard-page .nlb-family-count-2,
body.new-leaderboard-page .nlb-family-count-3 {
  grid-template-rows: auto auto;
}

body.new-leaderboard-page .nlb-ribbons {
  min-height: 22rem;
}

@media (max-width: 1100px) {
  .nlb-grid,
  .nlb-family-count-1,
  .nlb-family-count-2,
  .nlb-family-count-3 {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto auto;
    height: auto;
  }

  .nlb-family-count-2 .nlb-family-panels,
  .nlb-family-count-3 .nlb-family-panels {
    grid-template-columns: minmax(0, 1fr);
  }

  .nlb-ribbons {
    min-height: 22rem;
  }
}
```

The gold values intentionally match the already-approved Recycling-leaderboard palette. Do not introduce a separate New-department color system.

- [ ] **Step 5: Run static and route tests**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_new_leaderboard_static.py tests/test_new_leaderboard_routes.py tests/test_recycling_leaderboard_static.py -v
```

Expected: all tests pass and the Recycling-leaderboard contracts remain unchanged.

- [ ] **Step 6: Commit the adaptive presentation**

```bash
git add src/zira_dashboard/static/new_leaderboard.css src/zira_dashboard/templates/new_leaderboard_tv.html tests/test_new_leaderboard_static.py tests/test_new_leaderboard_routes.py
git commit -m "feat: style adaptive new leaderboard layout"
```

---

### Task 5: Register New-Leaderboard in Navigation, Settings, TV Dispatch, and Schema

**Files:**
- Modify: `src/zira_dashboard/tv_displays_store.py:1-40,135-198`
- Modify: `src/zira_dashboard/routes/tv_displays.py:35-116`
- Modify: `src/zira_dashboard/routes/settings.py:252-270`
- Modify: `src/zira_dashboard/templates/_settings_tvs.html:24-34`
- Modify: `src/zira_dashboard/templates/_dashboards_subnav.html:1-30`
- Modify: `src/zira_dashboard/_schema.py:470-529`
- Modify: `tests/test_tv_displays_store_unit.py:1-49`
- Modify: `tests/test_tv_displays_store.py:30-180`
- Modify: `tests/test_tv_displays_routes.py:95-132`
- Modify: `tests/test_db.py:140-163`

**Interfaces:**
- Produces: TV kind `vs_new_leaderboard` accepted consistently across Python and Postgres.
- Produces: default row `("New-Leaderboard", "vs_new_leaderboard", None)`.
- Produces: idempotent marker `tv_displays:seed_new_leaderboard_v1`.
- Consumes: `new_leaderboard.render_new_leaderboard_tv()` from Task 3.

- [ ] **Step 1: Write the new one-time backfill unit test**

In the existing recycling backfill test, replace the single marker dict and its two strict fake functions with:

```python
markers = {
    "tv_displays:seed_recycling_leaderboard_v1": None,
    "tv_displays:seed_new_leaderboard_v1": {"done": True},
}

def fake_get_setting(key):
    return markers[key]

def fake_set_setting(key, value):
    markers[key] = value
```

Change its final marker assertion to:

```python
assert markers["tv_displays:seed_recycling_leaderboard_v1"] == {"done": True}
```

Then append:

```python
def test_seed_defaults_backfills_new_leaderboard_when_rows_already_exist(monkeypatch):
    from zira_dashboard import app_settings, db, tv_displays_store

    calls: list[tuple[str, tuple | None]] = []
    markers = {
        "tv_displays:seed_recycling_leaderboard_v1": {"done": True},
        "tv_displays:seed_new_leaderboard_v1": None,
    }

    monkeypatch.setattr(app_settings, "get_setting", lambda key: markers[key])
    monkeypatch.setattr(
        app_settings,
        "set_setting",
        lambda key, value: markers.__setitem__(key, value),
    )

    def fake_query(sql, params=None):
        calls.append((sql, params))
        if "SELECT 1 FROM tv_displays LIMIT 1" in sql:
            return [{"exists": 1}]
        if "WHERE kind = %s" in sql:
            return []
        if "SELECT COALESCE(MAX(sort_order), -1)" in sql:
            return [{"sort_order": 10}]
        if "SELECT id FROM tv_displays WHERE slug = %s" in sql:
            return []
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(db, "query", fake_query)
    monkeypatch.setattr(db, "execute", lambda sql, params=None: calls.append((sql, params)))

    tv_displays_store.seed_defaults_if_empty()

    inserted = [
        params for sql, params in calls
        if "INSERT INTO tv_displays" in sql and params is not None
    ]
    assert inserted == [
        ("New-Leaderboard", "new-leaderboard", "vs_new_leaderboard", None, "dark", 11)
    ]
    assert markers["tv_displays:seed_new_leaderboard_v1"] == {"done": True}
```

- [ ] **Step 2: Write route, store, and DB-kind tests**

Add these focused cases:

```python
# tests/test_tv_displays_routes.py
def test_get_tv_new_leaderboard_dispatches(monkeypatch):
    from zira_dashboard.routes import new_leaderboard

    def fake_render(request, *, tv_theme="dark"):
        from fastapi.responses import HTMLResponse
        return HTMLResponse(
            f'<html data-tv-theme="{tv_theme}">New-Leaderboard</html>'
        )

    monkeypatch.setattr(new_leaderboard, "render_new_leaderboard_tv", fake_render)
    client = TestClient(app)
    client.post("/api/tv-displays", json={
        "name": "rt-new-leaderboard",
        "kind": "vs_new_leaderboard",
        "theme": "light",
    })
    response = client.get("/tv/rt-new-leaderboard")
    assert response.status_code == 200
    assert 'data-tv-theme="light"' in response.text
    assert "New-Leaderboard" in response.text


# tests/test_tv_displays_store.py
def test_save_new_leaderboard_kind():
    from zira_dashboard import tv_displays_store
    row = tv_displays_store.save(
        name="st-new-leaderboard",
        kind="vs_new_leaderboard",
        wc_name=None,
        theme="dark",
    )
    assert row["kind"] == "vs_new_leaderboard"
    assert row["wc_name"] is None


# tests/test_db.py
def test_tv_displays_kind_allows_new_leaderboard():
    db.init_pool()
    db.bootstrap_schema()
    db.execute("DELETE FROM tv_displays WHERE slug = 'db-new-leaderboard'")
    try:
        db.execute(
            "INSERT INTO tv_displays (name, slug, kind, wc_name, theme) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("db New-Leaderboard", "db-new-leaderboard", "vs_new_leaderboard", None, "dark"),
        )
        assert db.query(
            "SELECT kind FROM tv_displays WHERE slug = 'db-new-leaderboard'"
        ) == [{"kind": "vs_new_leaderboard"}]
    finally:
        db.execute("DELETE FROM tv_displays WHERE slug = 'db-new-leaderboard'")
```

- [ ] **Step 3: Run the focused tests and confirm rejection**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_tv_displays_store_unit.py tests/test_tv_displays_routes.py -k 'new_leaderboard' -v
```

Expected: unit coverage fails because the kind and seed are absent. Postgres-gated route/store cases skip when `DATABASE_URL` is not set.

- [ ] **Step 4: Add kind, seed, and generic backfill plumbing**

In `tv_displays_store.py`, make these constants exact:

```python
_VALID_KINDS = (
    "vs_recycling",
    "vs_new",
    "vs_recycling_leaderboard",
    "vs_new_leaderboard",
    "wc",
)
_RECYCLING_LEADERBOARD_SEED_MARKER = "tv_displays:seed_recycling_leaderboard_v1"
_RECYCLING_LEADERBOARD_SEED = (
    "Recycling-leaderboard", "vs_recycling_leaderboard", None,
)
_NEW_LEADERBOARD_SEED_MARKER = "tv_displays:seed_new_leaderboard_v1"
_NEW_LEADERBOARD_SEED = (
    "New-Leaderboard", "vs_new_leaderboard", None,
)
```

Insert `_NEW_LEADERBOARD_SEED` in `_SEED_LIST` immediately after `_RECYCLING_LEADERBOARD_SEED`. Replace the one-off implementation body with a shared helper while retaining named wrappers:

```python
def _backfill_dashboard_seed(marker: str, seed: tuple[str, str, str | None]) -> None:
    from . import app_settings, db

    if app_settings.get_setting(marker):
        return
    name, kind, wc_name = seed
    existing = db.query(
        "SELECT 1 FROM tv_displays WHERE kind = %s LIMIT 1",
        (kind,),
    )
    if not existing:
        sort_rows = db.query(
            "SELECT COALESCE(MAX(sort_order), -1) AS sort_order FROM tv_displays"
        )
        sort_order = int(sort_rows[0]["sort_order"]) + 1 if sort_rows else 0
        slug = _unique_slug(slug_for_wc(name))
        db.execute(
            "INSERT INTO tv_displays (name, slug, kind, wc_name, theme, sort_order) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (name, slug, kind, wc_name, "dark", sort_order),
        )
    app_settings.set_setting(marker, {"done": True})


def _backfill_recycling_leaderboard_seed() -> None:
    _backfill_dashboard_seed(
        _RECYCLING_LEADERBOARD_SEED_MARKER,
        _RECYCLING_LEADERBOARD_SEED,
    )


def _backfill_new_leaderboard_seed() -> None:
    _backfill_dashboard_seed(
        _NEW_LEADERBOARD_SEED_MARKER,
        _NEW_LEADERBOARD_SEED,
    )
```

When rows already exist, call both named backfills. After an initial full seed, mark both settings as done.

- [ ] **Step 5: Wire validation, dispatch, settings, navigation, and schema**

Make these exact additions:

```python
# routes/tv_displays.py, tv_display()
if kind == "vs_new_leaderboard":
    from . import new_leaderboard
    return new_leaderboard.render_new_leaderboard_tv(
        request,
        tv_theme=tv_theme,
    )
```

Add `"vs_new_leaderboard"` to the `post_display()` kind tuple.

```python
# routes/settings.py, all_dashboards_for_picker
{
    "kind": "vs_new_leaderboard",
    "ref": "",
    "name": "New-Leaderboard",
},
```

Add `vs_new_leaderboard` to the non-WC kind tuple in `_settings_tvs.html`.

Add this link immediately after the existing New link in `_dashboards_subnav.html`:

```html
<a href="/new-leaderboard"
   class="subnav-item {% if active_dashboard_key == 'vs_new_leaderboard' %}active{% endif %}">
  New-Leaderboard
</a>
```

In both the `CREATE TABLE tv_displays` check and the idempotent replacement constraint in `_schema.py`, use:

```sql
CHECK (kind IN (
  'vs_recycling',
  'vs_new',
  'vs_recycling_leaderboard',
  'vs_new_leaderboard',
  'wc'
))
```

Update the schema comment from 11 to 12 seeded rows. Update `tests/test_tv_displays_store.py` initial-seed expectations from 11 to 12 and the missing-WC seed expectation from 4 to 5, with `New-Leaderboard` present in both name lists.

- [ ] **Step 6: Run registry and navigation tests**

Run without Postgres:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_tv_displays_store_unit.py tests/test_new_leaderboard_routes.py tests/test_new_leaderboard_static.py -v
```

Expected: all tests pass.

When `DATABASE_URL` is configured, also run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_tv_displays_store.py tests/test_tv_displays_routes.py tests/test_db.py -v
```

Expected: all tests pass, including the new Postgres constraint test.

- [ ] **Step 7: Commit the complete registry integration**

```bash
git add src/zira_dashboard/tv_displays_store.py src/zira_dashboard/routes/tv_displays.py src/zira_dashboard/routes/settings.py src/zira_dashboard/templates/_settings_tvs.html src/zira_dashboard/templates/_dashboards_subnav.html src/zira_dashboard/_schema.py tests/test_tv_displays_store_unit.py tests/test_tv_displays_store.py tests/test_tv_displays_routes.py tests/test_db.py
git commit -m "feat: register new leaderboard tv display"
```

---

### Task 6: Add Deterministic Visual Fixtures and Run Full Verification

**Files:**
- Create: `scripts/preview_new_leaderboard.py`
- Verify: all files changed in Tasks 1-5

**Interfaces:**
- Consumes: `_leaderboard_payload` monkeypatch seam from Task 3.
- Produces: local preview pages for Junior-only dashboard/TV and three-family dark/light TV states.

- [ ] **Step 1: Create the preview renderer**

Create `scripts/preview_new_leaderboard.py`:

```python
from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import shutil
from unittest.mock import patch

os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("ZIRA_API_KEY", "preview-dummy")

from fastapi.testclient import TestClient  # noqa: E402

from zira_dashboard import _http_cache  # noqa: E402
from zira_dashboard.app import app  # noqa: E402


OUT = Path("scripts/_preview_out/new_leaderboard")
STATIC = Path("src/zira_dashboard/static")


def _row(rank: int, name: str, ytd: float, l30: float) -> dict:
    return {
        "rank": rank,
        "name": name,
        "ytd": {"eligible": True, "avg_units": ytd, "days": 24 - rank, "label": None},
        "l30": {"eligible": True, "avg_units": l30, "days": 8 - rank, "label": None},
    }


def _payload(active: list[str]) -> dict:
    names = {
        "Juniors": [("Alex M.", 642.0, 668.0), ("Jordan R.", 621.0, 650.0), ("Sam T.", 603.0, 611.0)],
        "Woodpecker": [("Taylor N.", 588.0, 610.0), ("Morgan P.", 571.0, 582.0), ("Riley C.", 548.0, 559.0)],
        "Hand Build": [("Jamie V.", 184.0, 192.0), ("Avery D.", 179.0, 186.0), ("Quinn S.", 171.0, 176.0)],
    }
    families = {
        family: {
            "thresholds": {"ytd": 2, "l30": 1},
            "rows": [
                _row(index, person, ytd, l30)
                for index, (person, ytd, l30) in enumerate(names[family], 1)
            ] if family in active else [],
        }
        for family in names
    }
    month_labels = ["Jul", "Jun", "May", "Apr", "Mar", "Feb", "Jan", "Dec", "Nov", "Oct", "Sep", "Aug"]
    ribbons = []
    for index, label in enumerate(month_labels):
        year = 2026 if index < 7 else 2025
        month = 7 - index if index < 7 else 19 - index
        ribbons.append({
            "year": year,
            "month": month,
            "month_label": label,
            "winners": {
                family: {
                    "name": names[family][0][0],
                    "day": date(year, month, 2),
                    "amount": names[family][0][1] + 40,
                    "days": 1,
                }
                for family in active
            },
        })
    return {
        "ytd_start": date(2026, 1, 1),
        "ytd_end": date(2026, 7, 10),
        "l30_start": date(2026, 6, 11),
        "l30_end": date(2026, 7, 10),
        "active_families": active,
        "families": families,
        "ribbons": ribbons,
        "current_goats": [
            {"label": f"{family.rstrip('s')} GOAT", "group": family,
             "name": names[family][0][0], "units": names[family][0][1] + 69, "day": date(2026, 7, 2)}
            for family in active
        ],
        "error_message": None,
    }


def _write(client: TestClient, filename: str, url: str, payload: dict) -> None:
    _http_cache.invalidate_today_cache()
    with patch("zira_dashboard.routes.new_leaderboard._leaderboard_payload", lambda today: payload):
        response = client.get(url)
    response.raise_for_status()
    html = response.text.replace('href="/static/', 'href="static/').replace('src="/static/', 'src="static/')
    (OUT / filename).write_text(html, encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copytree(STATIC, OUT / "static", dirs_exist_ok=True)
    client = TestClient(app)
    junior = _payload(["Juniors"])
    future = _payload(["Juniors", "Woodpecker", "Hand Build"])
    _write(client, "dashboard-junior-only.html", "/new-leaderboard", junior)
    _write(client, "tv-dark-junior-only.html", "/tv/new-leaderboard?theme=dark", junior)
    _write(client, "tv-dark-three-families.html", "/tv/new-leaderboard?theme=dark", future)
    _write(client, "tv-light-three-families.html", "/tv/new-leaderboard?theme=light", future)
    print(OUT.resolve())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Generate all four previews**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python scripts/preview_new_leaderboard.py
```

Expected: command prints the absolute `scripts/_preview_out/new_leaderboard` directory and creates four HTML files plus a copied `static/` directory. This path is already covered by the repository's preview-output ignore rule.

- [ ] **Step 3: Run focused and full automated verification**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_production_metrics.py tests/test_awards.py tests/test_new_leaderboard_routes.py tests/test_new_leaderboard_static.py tests/test_recycling_leaderboard_tv.py tests/test_recycling_leaderboard_static.py tests/test_tv_displays_store_unit.py -v
```

Expected: all non-Postgres tests pass.

Then run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest -q
```

Expected: the complete available suite passes; Postgres-gated tests skip only when `DATABASE_URL` is absent.

- [ ] **Step 4: Inspect the responsive previews in a browser**

Run:

```bash
python3 -m http.server 8765 --directory scripts/_preview_out/new_leaderboard
```

At a 1920×1080 viewport, inspect:

```text
http://127.0.0.1:8765/tv-dark-junior-only.html
http://127.0.0.1:8765/tv-dark-three-families.html
http://127.0.0.1:8765/tv-light-three-families.html
```

At desktop width and then below 1100px, inspect:

```text
http://127.0.0.1:8765/dashboard-junior-only.html
```

Acceptance checks:

- Junior-only TV has no blank future-family panels.
- Three-family TV keeps all names, YTD/L30 values, thresholds, and 12 ribbon rows visible without overlap.
- GOAT chips fit without obscuring the title.
- Light and dark themes retain readable names and ribbon metadata.
- Normal dashboard stacks cleanly below 1100px and exposes the active New-Leaderboard tab.
- Browser console has no errors.

If a check fails, make the smallest CSS/template correction, rerun `tests/test_new_leaderboard_static.py` and the preview script, and repeat this step before committing.

- [ ] **Step 5: Commit the preview tool and verified presentation**

```bash
git add scripts/preview_new_leaderboard.py src/zira_dashboard/static/new_leaderboard.css src/zira_dashboard/templates/new_leaderboard_tv.html
git commit -m "test: add new leaderboard visual fixtures"
```

- [ ] **Step 6: Final repository hygiene check**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only the user's pre-existing unrelated `.claude/` path may remain untracked.

---

## Final Verification Matrix

| Requirement | Evidence |
|---|---|
| Independent Junior/Woodpecker/Hand Build scoring | `tests/test_production_metrics.py` family-builder tests |
| Big Build #1 included with Hand Build | `tests/test_new_leaderboard_routes.py::test_family_wc_names_include_big_build_with_hand_build` |
| Existing Recycling behavior preserved | Recycling metric, route, and static regression tests |
| GOATs use explicit family WCs and overrides | `tests/test_awards.py` explicit-WC GOAT tests |
| Junior-only adaptive layout | route/static tests plus `tv-dark-junior-only.html` |
| Two/three-family expansion | count-class contracts plus `tv-dark-three-families.html` |
| Empty and temporary-failure states | `tests/test_new_leaderboard_routes.py` |
| Normal and TV routes/themes | route tests and dark/light previews |
| TV picker, dispatcher, seed, schema | TV store/route/DB tests |
| Full integration safety | complete pytest run and browser console check |
