# Hand Build #1 Zira Meter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect Hand Build #1 to Zira meter `44484` with a 400-pallet full-crew daily goal across the existing New production pipeline.

**Architecture:** Follow the existing dual-registry meter pattern: `staffing.LOCATIONS` drives metered production history and work-center dashboards, while `stations.STATIONS` drives shared station and missing-work-center flows. Use guarded schema backfills to align the existing production database row, then rely on the already-dynamic New dashboard, Hand Build leaderboard family, attribution, and recognition paths without adding Hand-Build-specific runtime logic.

**Tech Stack:** Python 3.12, FastAPI application configuration, PostgreSQL bootstrap SQL, pytest, Ruff.

## Global Constraints

- Use Zira meter ID `44484` for Hand Build #1 in both registries.
- Use `400` as the full two-person crew's daily pallet goal.
- Preserve any later nonblank meter ID or nonzero goal saved in the database.
- Do not add size-specific goals until Zira populates product dimensions.
- Do not seed a dedicated physical TV display.
- Do not add a Hand-Build-specific fetch, cache, attribution, or scoring path.
- New changelog text must use short, common, user-facing sentences.

---

### Task 1: Map Hand Build #1 into the existing New production pipeline

**Files:**
- Create: `tests/test_hand_build_1_zira_mapping.py`
- Modify: `src/zira_dashboard/staffing.py:100-103`
- Modify: `src/zira_dashboard/stations.py:13-25`
- Modify: `src/zira_dashboard/_schema.py:133-143`
- Modify: `CHANGELOG.md:12`

**Interfaces:**
- Consumes: `staffing.LOCATIONS`, `stations.STATIONS`, `stations.recycling_stations()`, `routes.departments._new_stations()`, `production_history._metered_leaderboard(client, day)`, and `goat_categories.has_metered_source(category)`.
- Produces: a Hand Build #1 `Location` and `Station` sharing meter ID `"44484"`, plus idempotent database backfills for that meter and a 400-pallet goal.

- [ ] **Step 1: Write failing registry and downstream-discovery tests**

Create `tests/test_hand_build_1_zira_mapping.py`:

```python
from datetime import date

from zira_dashboard import goat_categories, leaderboard, production_history, staffing
from zira_dashboard._schema import SCHEMA_DDL
from zira_dashboard.routes import departments
from zira_dashboard.stations import STATIONS, recycling_stations


def _hand_build_location():
    return next(location for location in staffing.LOCATIONS if location.name == "Hand Build #1")


def _hand_build_station():
    return next(station for station in STATIONS if station.name == "Hand Build #1")


def test_hand_build_1_uses_its_zira_meter_in_both_registries():
    location = _hand_build_location()
    station = _hand_build_station()

    assert location.meter_id == "44484"
    assert station.meter_id == location.meter_id
    assert station.category == "Hand Build"
    assert station.cell == "New"
    assert station not in recycling_stations()


def test_hand_build_1_meter_and_goal_backfill_only_blank_defaults():
    assert "SET meter_id = '44484'" in SCHEMA_DDL
    assert "WHERE name = 'Hand Build #1'" in SCHEMA_DDL
    assert "COALESCE(meter_id, '') = ''" in SCHEMA_DDL
    assert "SET goal_per_day_override = 400" in SCHEMA_DDL
    assert "COALESCE(goal_per_day_override, 0) = 0" in SCHEMA_DDL


def test_hand_build_1_auto_activates_existing_new_paths(monkeypatch):
    monkeypatch.setattr(
        departments.work_centers_store,
        "department",
        lambda location: location.department,
    )
    new_stations = departments._new_stations()
    assert next(station for station in new_stations if station.name == "Hand Build #1").meter_id == "44484"

    captured = {}

    def fake_leaderboard(client, stations, day):
        captured["stations"] = stations
        return []

    monkeypatch.setattr(leaderboard, "cached_leaderboard", fake_leaderboard)
    production_history._metered_leaderboard(object(), date(2026, 8, 18))
    assert next(
        station for station in captured["stations"] if station.name == "Hand Build #1"
    ).meter_id == "44484"

    category = goat_categories.category_for_key("hand_build")
    assert goat_categories.has_metered_source(category) is True
```

- [ ] **Step 2: Run the new tests and verify the missing mappings fail**

Run:

```bash
DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest tests/test_hand_build_1_zira_mapping.py -q
```

Expected: FAIL because `STATIONS` has no station named `Hand Build #1` and the staffing location has no meter.

- [ ] **Step 3: Add the meter to both static registries**

In `src/zira_dashboard/staffing.py`, change Hand Build #1 to:

```python
Location("Hand Build #1", "Hand Build", "Bay 6", "New", "44484", min_ops=2, max_ops=2),
```

In `src/zira_dashboard/stations.py`, add:

```python
Station(meter_id="44484", name="Hand Build #1", category="Hand Build", cell="New"),
```

Do not add `Hand Build` to the legacy `CATEGORIES` tuple. The 400-pallet goal belongs to the work-center row, while `CATEGORIES` only feeds the retired per-group target form.

- [ ] **Step 4: Add guarded database backfills**

In `src/zira_dashboard/_schema.py`, after the existing Repair 4 meter backfill, add:

```sql
-- Hand Build #1 received its Zira source after the work-center row existed.
-- Preserve later nonblank meter and nonzero goal choices.
UPDATE work_centers
   SET meter_id = '44484'
 WHERE name = 'Hand Build #1'
   AND COALESCE(meter_id, '') = '';

UPDATE work_centers
   SET goal_per_day_override = 400
 WHERE name = 'Hand Build #1'
   AND COALESCE(goal_per_day_override, 0) = 0;
```

- [ ] **Step 5: Add the user-facing changelog note**

At the top of the `2026-08-18` section in `CHANGELOG.md`, add:

```markdown
### Hand Build production

#### Features

- **Hand Build #1 now shows its pallet count.** Its new Zira camera is connected to dashboards, reports, and leaderboards. Its daily goal starts at 400 pallets.
```

- [ ] **Step 6: Run the new tests and verify they pass**

Run:

```bash
DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest tests/test_hand_build_1_zira_mapping.py -q
```

Expected: `3 passed`.

- [ ] **Step 7: Run focused downstream validation**

Run:

```bash
DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest \
  tests/test_hand_build_1_zira_mapping.py \
  tests/test_new_dashboard_data.py \
  tests/test_production_history.py \
  tests/test_goat_categories.py \
  tests/test_wc_attributions_testing.py \
  tests/test_zira_persist.py -q
```

Expected: all selected tests pass with zero failures.

- [ ] **Step 8: Run Ruff and whitespace validation**

Run:

```bash
.venv/bin/python -m ruff check \
  src/zira_dashboard/_schema.py \
  src/zira_dashboard/staffing.py \
  src/zira_dashboard/stations.py \
  tests/test_hand_build_1_zira_mapping.py
git diff --check
```

Expected: `All checks passed!` and no whitespace errors.

- [ ] **Step 9: Run the broad safe regression suite**

The local repository has seven known baseline/environment failures: two tests
that need DB behavior despite running without a DB, one sandboxed Playwright
launch, and four route tests that reach DB-backed paths. Keep production DB
URLs disabled and deselect only those existing failures:

```bash
DATABASE_URL= DATABASE_PUBLIC_URL= ZIRA_API_KEY=test \
  .venv/bin/python -m pytest -q \
  --deselect tests/test_inbox_event_wiring.py::test_late_declare_absent_records_inbox_event \
  --deselect tests/test_odoo_facade_contract.py::test_facade_department_helper_is_resolved_at_call_time \
  --deselect tests/test_preview_new_leaderboard.py::test_preview_three_family_tv_ribbon_geometry_fits_target_viewports \
  --deselect tests/test_settings_api_keys.py::test_api_settings_page_route_renders \
  --deselect tests/test_settings_api_keys.py::test_super_admin_session_can_render_api_settings \
  --deselect tests/test_time_off_approvals.py::test_approvals_url_redirects_to_merged_time_off_page \
  --deselect tests/test_time_off_approvals.py::test_approvals_page_renders_pending_context_and_recent_decisions
```

Expected: exit 0 with every selected test passing; DB-gated tests skip safely.

- [ ] **Step 10: Commit and push the implementation**

```bash
git add \
  CHANGELOG.md \
  src/zira_dashboard/_schema.py \
  src/zira_dashboard/staffing.py \
  src/zira_dashboard/stations.py \
  tests/test_hand_build_1_zira_mapping.py
git commit -m "feat: connect Hand Build 1 Zira meter"
git push origin main
```

