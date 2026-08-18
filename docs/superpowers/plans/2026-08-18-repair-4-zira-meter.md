# Repair 4 Zira Meter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect Repair 4's Zira meter `44483` to every existing metered-repair data path.

**Architecture:** Keep the existing static configuration pattern used by Repairs 1–3. Add the meter to both the dashboard station registry and the staffing work-center registry; all downstream fetching, caching, attribution, dashboard, goal, and recognition code already consumes one of those registries.

**Tech Stack:** Python 3.12, pytest, Ruff, FastAPI application configuration.

## Global Constraints

- Preserve all existing Repair 4 staffing settings other than its meter ID.
- Use Zira meter ID `44483` for Repair 4 in both registries.
- Do not add a new fetch path or special case for Repair 4.
- New changelog text must use short, simple, user-facing sentences.

---

### Task 1: Map and verify the Repair 4 meter

**Files:**
- Create: `tests/test_repair_4_zira_mapping.py`
- Modify: `src/zira_dashboard/stations.py`
- Modify: `src/zira_dashboard/staffing.py`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `zira_dashboard.stations.STATIONS`, `recycling_stations()`, and `zira_dashboard.staffing.LOCATIONS`.
- Produces: a `Station` and `Location` for Repair 4 whose `meter_id` is `"44483"`.

- [ ] **Step 1: Write the failing registry invariant test**

```python
from zira_dashboard import staffing
from zira_dashboard.stations import STATIONS, recycling_stations


def test_repair_4_uses_its_zira_meter_in_both_registries():
    station = next(station for station in STATIONS if station.name == "Repair 4")
    location = next(location for location in staffing.LOCATIONS if location.name == "Repair 4")

    assert station.meter_id == "44483"
    assert location.meter_id == station.meter_id
    assert station in recycling_stations()
```

- [ ] **Step 2: Run the test and verify the missing station fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_repair_4_zira_mapping.py -q`

Expected: FAIL because `STATIONS` has no station named `Repair 4`.

- [ ] **Step 3: Add the meter to both registries**

In `src/zira_dashboard/stations.py`, add:

```python
Station(meter_id="44483", name="Repair 4", category="Repair", cell="Recycling"),
```

In `src/zira_dashboard/staffing.py`, change Repair 4's location to:

```python
Location("Repair 4", "Repair", "Bay 5", "Recycled", "44483"),
```

- [ ] **Step 4: Add the user-facing changelog note**

Add this bullet to the current top release entry in `CHANGELOG.md`:

```markdown
- **Repair 4 now shows its production.** Its new Zira meter is connected like Repairs 1–3, so its pallet counts can appear on dashboards, reports, and leaderboards.
```

- [ ] **Step 5: Run focused validation**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_repair_4_zira_mapping.py tests/test_wc_attributions_testing.py tests/test_wc_dashboard_data.py tests/test_goat_categories.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Run full validation**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`

Expected: zero failures.

Run: `.venv/bin/python -m ruff check src/zira_dashboard/stations.py src/zira_dashboard/staffing.py tests/test_repair_4_zira_mapping.py`

Expected: `All checks passed!`

- [ ] **Step 7: Commit and push**

```bash
git add CHANGELOG.md src/zira_dashboard/stations.py src/zira_dashboard/staffing.py tests/test_repair_4_zira_mapping.py
git commit -m "feat: connect Repair 4 Zira meter"
git push origin main
```
