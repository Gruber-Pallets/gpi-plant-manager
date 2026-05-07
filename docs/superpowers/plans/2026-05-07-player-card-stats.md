# Player Card Stats Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the player card's "Total units" bubble with one group-average pph tile per registered group (auto-hide when empty), add an Avg (pph) column to the per-WC table, and fix the table header alignment.

**Architecture:** Hours are already in the per-(person, WC) dict from `production_history.attribute_for_range` — the route just isn't reading them. Two route additions: an `avg_pph` field on each `rows` entry, and a `group_avgs` list built by iterating `work_centers_store.registered_groups()`. Template drops the Total units bubble, renders a second tile row when `group_avgs` is non-empty, and adds the new column to the per-WC table.

**Tech Stack:** Python 3.12 / FastAPI / Jinja2. Test runner: `.venv/Scripts/python.exe -m pytest`.

**Spec:** `docs/superpowers/specs/2026-05-07-player-card-stats-design.md`

---

## File map

| File | Change |
|---|---|
| `src/zira_dashboard/routes/people.py` | Compute `avg_pph` per row; build `group_avgs`; pass both to the template. |
| `src/zira_dashboard/templates/player_card.html` | Drop Total units bubble; add `.pc-group-avgs` row; add Avg (pph) column; fix `th.num` alignment. |
| `tests/test_player_card_stats.py` (new) | Unit tests for `avg_pph` and `group_avgs`. |
| `CHANGELOG.md` | Entry for the deploy. |

---

### Task 1: Route — compute `avg_pph` per row + `group_avgs`

**Files:**
- Create: `tests/test_player_card_stats.py`
- Modify: `src/zira_dashboard/routes/people.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_player_card_stats.py`:

```python
"""Unit tests for player card stats redesign — avg_pph + group_avgs.

These tests stub the route's data sources (production_history.attribution_range,
work_centers_store) so they don't need DATABASE_URL.
"""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient


def _make_client():
    from zira_dashboard.app import app
    return TestClient(app)


def _stub_route_dependencies(monkeypatch, *, person_data, registered, members_map, roster_names=None):
    """Patch the things the player-card route reaches for.

    person_data: {wc_name: {"units": float, "downtime": float, "hours": float, "days_worked": int}}
    registered:  list[str] — registered group names
    members_map: {group_name: [wc_name, ...]}
    """
    from zira_dashboard import staffing
    from zira_dashboard.routes import people as people_route
    from zira_dashboard import production_history, work_centers_store, late_report

    class _FakeLoc:
        def __init__(self, name): self.name = name

    monkeypatch.setattr(
        production_history,
        "attribution_range",
        lambda s, e, c: {"Test Person": person_data},
    )
    monkeypatch.setattr(
        production_history,
        "attribution_per_day",
        lambda s, e, c: [],
    )
    monkeypatch.setattr(
        work_centers_store, "registered_groups", lambda: list(registered),
    )
    monkeypatch.setattr(
        work_centers_store,
        "members",
        lambda kind, name: [_FakeLoc(n) for n in members_map.get(name, [])],
    )

    class _FakePerson:
        def __init__(self, name):
            self.name = name
            self.active = True
            self.skills = {}
            self.reserve = False

    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [_FakePerson("Test Person")],
    )
    monkeypatch.setattr(late_report, "absences_history_for_name", lambda *a, **k: [])
    monkeypatch.setattr(late_report, "late_arrivals_history_for_name", lambda *a, **k: [])


def test_avg_pph_per_wc_added_to_rows(monkeypatch):
    """Each per-WC row gains an `avg_pph` field equal to units/hours, rounded to 1dp."""
    _stub_route_dependencies(
        monkeypatch,
        person_data={
            "Repair 1": {"units": 100.0, "downtime": 5.0, "hours": 10.0, "days_worked": 2},
        },
        registered=[],
        members_map={},
    )
    captured = {}

    def _capture(request, template, ctx):
        captured["ctx"] = ctx
        from fastapi.responses import HTMLResponse
        return HTMLResponse("ok")

    from zira_dashboard.deps import templates
    monkeypatch.setattr(templates, "TemplateResponse", _capture)

    r = _make_client().get("/staffing/people/Test Person")
    assert r.status_code == 200
    rows = captured["ctx"]["rows"]
    assert len(rows) == 1
    assert rows[0]["wc"] == "Repair 1"
    assert rows[0]["avg_pph"] == 10.0


def test_avg_pph_zero_hours_returns_zero(monkeypatch):
    """Defensive: when hours == 0, avg_pph is 0 (not a divide-by-zero)."""
    _stub_route_dependencies(
        monkeypatch,
        person_data={
            "Repair 1": {"units": 50.0, "downtime": 0.0, "hours": 0.0, "days_worked": 1},
        },
        registered=[],
        members_map={},
    )
    captured = {}

    def _capture(request, template, ctx):
        captured["ctx"] = ctx
        from fastapi.responses import HTMLResponse
        return HTMLResponse("ok")

    from zira_dashboard.deps import templates
    monkeypatch.setattr(templates, "TemplateResponse", _capture)

    _make_client().get("/staffing/people/Test Person")
    assert captured["ctx"]["rows"][0]["avg_pph"] == 0


def test_group_avgs_hides_groups_with_no_hours(monkeypatch):
    """A registered group with no overlap with the person's WCs is omitted."""
    _stub_route_dependencies(
        monkeypatch,
        person_data={
            "Repair 1": {"units": 100.0, "downtime": 0.0, "hours": 10.0, "days_worked": 2},
        },
        registered=["Repairs", "Dismantlers"],
        members_map={
            "Repairs": ["Repair 1", "Repair 2"],
            "Dismantlers": ["Dismantle 1"],
        },
    )
    captured = {}

    def _capture(request, template, ctx):
        captured["ctx"] = ctx
        from fastapi.responses import HTMLResponse
        return HTMLResponse("ok")

    from zira_dashboard.deps import templates
    monkeypatch.setattr(templates, "TemplateResponse", _capture)

    _make_client().get("/staffing/people/Test Person")
    group_avgs = captured["ctx"]["group_avgs"]
    names = [g["name"] for g in group_avgs]
    assert names == ["Repairs"]
    assert group_avgs[0]["pph"] == 10.0


def test_group_avgs_hours_weighted_across_wcs(monkeypatch):
    """pph for a group is sum(units across the group's WCs) / sum(hours)."""
    _stub_route_dependencies(
        monkeypatch,
        person_data={
            "Repair 1": {"units": 50.0, "downtime": 0.0, "hours": 5.0, "days_worked": 1},
            "Repair 2": {"units": 100.0, "downtime": 0.0, "hours": 10.0, "days_worked": 2},
        },
        registered=["Repairs"],
        members_map={"Repairs": ["Repair 1", "Repair 2"]},
    )
    captured = {}

    def _capture(request, template, ctx):
        captured["ctx"] = ctx
        from fastapi.responses import HTMLResponse
        return HTMLResponse("ok")

    from zira_dashboard.deps import templates
    monkeypatch.setattr(templates, "TemplateResponse", _capture)

    _make_client().get("/staffing/people/Test Person")
    group_avgs = captured["ctx"]["group_avgs"]
    assert len(group_avgs) == 1
    # (50 + 100) / (5 + 10) = 10.0
    assert group_avgs[0]["pph"] == 10.0


def test_group_avgs_preserves_registry_order(monkeypatch):
    """`group_avgs` follows the order returned by `registered_groups()`."""
    _stub_route_dependencies(
        monkeypatch,
        person_data={
            "A": {"units": 10.0, "downtime": 0.0, "hours": 1.0, "days_worked": 1},
            "B": {"units": 20.0, "downtime": 0.0, "hours": 1.0, "days_worked": 1},
            "C": {"units": 30.0, "downtime": 0.0, "hours": 1.0, "days_worked": 1},
        },
        registered=["Juniors", "Repairs", "Dismantlers"],
        members_map={
            "Juniors": ["A"],
            "Repairs": ["B"],
            "Dismantlers": ["C"],
        },
    )
    captured = {}

    def _capture(request, template, ctx):
        captured["ctx"] = ctx
        from fastapi.responses import HTMLResponse
        return HTMLResponse("ok")

    from zira_dashboard.deps import templates
    monkeypatch.setattr(templates, "TemplateResponse", _capture)

    _make_client().get("/staffing/people/Test Person")
    names = [g["name"] for g in captured["ctx"]["group_avgs"]]
    assert names == ["Juniors", "Repairs", "Dismantlers"]
```

- [ ] **Step 2: Run the failing tests**

```
.venv/Scripts/python.exe -m pytest tests/test_player_card_stats.py -v
```

Expected: 5 FAIL — `rows[0]` has no `avg_pph` key, context dict has no `group_avgs` key.

- [ ] **Step 3: Update the route**

Open `src/zira_dashboard/routes/people.py`. After the `rows = sorted(...)` line (around line 52-55), inject the `avg_pph` field on each row. Then before the `roster = ...` line (around line 59), build `group_avgs`.

Replace the block from `rows = sorted(...)` through `total_days = sum(...)` (lines 52-58) with:

```python
    rows = sorted(
        ({"wc": wc, **t} for wc, t in person.items()),
        key=lambda r: -r["units"],
    )
    for r in rows:
        hrs = r.get("hours", 0.0)
        r["avg_pph"] = round(r["units"] / hrs, 1) if hrs > 0 else 0
    total_units    = sum(r["units"] for r in rows)
    total_downtime = sum(r["downtime"] for r in rows)
    total_days     = sum(r["days_worked"] for r in rows)

    # Group averages — one entry per registered group with hours > 0.
    # Hours-weighted pph across the group's WCs. Order follows
    # registered_groups() (which sorts by lower(name)).
    from .. import work_centers_store
    group_avgs: list[dict] = []
    for group_name in work_centers_store.registered_groups():
        wc_names = {loc.name for loc in work_centers_store.members("group", group_name)}
        if not wc_names:
            continue
        units_sum = 0.0
        hours_sum = 0.0
        for wc_name, totals in person.items():
            if wc_name in wc_names:
                units_sum += totals.get("units", 0.0)
                hours_sum += totals.get("hours", 0.0)
        if hours_sum > 0:
            group_avgs.append({
                "name": group_name,
                "pph": round(units_sum / hours_sum, 1),
            })
```

Then in the `templates.TemplateResponse` context dict (around line 102-118), add `"group_avgs": group_avgs,` right after `"rows": rows,`. The updated block:

```python
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
            "group_avgs": group_avgs,
            "total_units": round(total_units, 1),
            "total_downtime": round(total_downtime, 1),
            "total_days": total_days,
            "skills": skills,
            "day_rows": day_rows,
            "attendance_rows": attendance_rows,
            "total_absent_days": total_absent_days,
            "total_late_days": total_late_days,
            "roster_names": roster_names,
        },
    )
```

- [ ] **Step 4: Run the tests**

```
.venv/Scripts/python.exe -m pytest tests/test_player_card_stats.py -v
```

Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/routes/people.py tests/test_player_card_stats.py
git commit -m "$(cat <<'EOF'
feat(player_card): per-WC avg_pph + group_avgs context

Reads the existing `hours` field from attribution_range output and
exposes two new context keys: rows[i].avg_pph (units/hours per WC,
1dp) and group_avgs (one entry per registered group with hours > 0,
hours-weighted across the group's WCs, in registry order).

Template change comes in the next commit; the new fields are
unused until then.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Template — drop Total units, add group avgs row, add Avg column, fix th alignment

**Files:**
- Modify: `src/zira_dashboard/templates/player_card.html`

- [ ] **Step 1: Fix the `th.num` alignment in the styles block**

Find the styles block (around lines 26-30):

```css
table.pc { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
table.pc th, table.pc td { padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--border); text-align: left; }
table.pc th { color: var(--muted); font-size: 0.7rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
table.pc td.num { text-align: right; font-variant-numeric: tabular-nums; }
```

Add a rule for `th.num` and a styles block for the new `.pc-group-avgs` grid. Replace the existing `table.pc td.num` line with:

```css
  table.pc td.num, table.pc th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .pc-group-avgs { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 0.6rem; margin-bottom: 1rem; }
  .pc-group-avgs .stat { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 0.7rem 0.9rem; }
  .pc-group-avgs .stat .lab { color: var(--muted); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.5px; }
  .pc-group-avgs .stat .v { font-size: 1.8rem; font-weight: 700; font-variant-numeric: tabular-nums; margin-top: 0.2rem; color: var(--accent); }
```

(The `font-variant-numeric: tabular-nums` is now applied to both `th.num` and `td.num` so the column lines up.)

- [ ] **Step 2: Drop the Total units bubble + add the group avgs row**

Find the `.pc-totals` div (around lines 74-80):

```jinja
<div class="pc-totals">
  <div class="stat"><div class="lab">Days worked</div><div class="v">{{ total_days }}</div></div>
  <div class="stat"><div class="lab">Total units (split)</div><div class="v">{{ '{:,.0f}'.format(total_units) }}</div></div>
  <div class="stat"><div class="lab">Total downtime (min)</div><div class="v">{{ '{:,.0f}'.format(total_downtime) }}</div></div>
  <div class="stat"><div class="lab">Days Absent</div><div class="v">{{ total_absent_days }}</div></div>
  <div class="stat"><div class="lab">Days Late</div><div class="v">{{ total_late_days }}</div></div>
</div>
```

Replace with:

```jinja
<div class="pc-totals">
  <div class="stat"><div class="lab">Days worked</div><div class="v">{{ total_days }}</div></div>
  <div class="stat"><div class="lab">Total downtime (min)</div><div class="v">{{ '{:,.0f}'.format(total_downtime) }}</div></div>
  <div class="stat"><div class="lab">Days Absent</div><div class="v">{{ total_absent_days }}</div></div>
  <div class="stat"><div class="lab">Days Late</div><div class="v">{{ total_late_days }}</div></div>
</div>

{% if group_avgs %}
<div class="pc-group-avgs">
  {% for g in group_avgs %}
  <div class="stat">
    <div class="lab">{{ g.name }}</div>
    <div class="v">{{ '{:,.1f}'.format(g.pph) }} <span style="font-size:0.85rem;color:var(--muted);font-weight:500">pph</span></div>
  </div>
  {% endfor %}
</div>
{% endif %}
```

- [ ] **Step 3: Add the Avg (pph) column to the per-WC table**

Find the per-WC table block (around lines 82-97):

```jinja
{% if rows %}
<table class="pc">
  <thead>
    <tr><th>Work Center</th><th class="num">Days</th><th class="num">Units</th><th class="num">Downtime (min)</th></tr>
  </thead>
  <tbody>
    {% for r in rows %}
    <tr>
      <td>{{ r.wc }}</td>
      <td class="num">{{ r.days_worked }}</td>
      <td class="num">{{ '{:,.0f}'.format(r.units) }}</td>
      <td class="num">{{ '{:,.0f}'.format(r.downtime) }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
```

Replace with:

```jinja
{% if rows %}
<table class="pc">
  <thead>
    <tr><th>Work Center</th><th class="num">Days</th><th class="num">Units</th><th class="num">Avg (pph)</th><th class="num">Downtime (min)</th></tr>
  </thead>
  <tbody>
    {% for r in rows %}
    <tr>
      <td>{{ r.wc }}</td>
      <td class="num">{{ r.days_worked }}</td>
      <td class="num">{{ '{:,.0f}'.format(r.units) }}</td>
      <td class="num">{% if r.avg_pph %}{{ '{:,.1f}'.format(r.avg_pph) }}{% else %}—{% endif %}</td>
      <td class="num">{{ '{:,.0f}'.format(r.downtime) }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
```

- [ ] **Step 4: Smoke-test that the template parses**

```
.venv/Scripts/python.exe -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'))
env.get_template('player_card.html')
print('OK')
"
```

Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/player_card.html
git commit -m "$(cat <<'EOF'
feat(player_card): group avg pph tiles + Avg column + th alignment

Drops the Total units bubble. Renders a second tile row under the
existing stats, one tile per registered group with hours > 0
(hidden block when nothing matches). Adds Avg (pph) column to the
per-WC table between Units and Downtime. Fixes th.num alignment so
headers right-align with the numeric cells beneath them.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Final test pass + CHANGELOG + push

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run the full non-DB test suite**

```
.venv/Scripts/python.exe -m pytest tests/test_progress.py tests/test_deps_window_dates.py tests/test_share_route.py tests/test_results.py tests/test_zira_persist.py tests/test_slack_client.py tests/test_late_report.py tests/test_wc_attributions.py tests/test_leaderboards_avg.py tests/test_production_history.py tests/test_leaderboards_person_days.py tests/test_player_card.py tests/test_player_card_stats.py tests/test_roster_filter.py -q
```

Expected: all PASS (DB-bound tests skip).

- [ ] **Step 2: Get the time**

```
date "+%I:%M %p"
```

- [ ] **Step 3: Add the CHANGELOG entry**

In `CHANGELOG.md`, insert at the top of today's `## 2026-05-07` section (above the existing `### 10:08 AM` entry):

```markdown
### {time-from-step-2}

- **Player card stats redesign** — at the top of `/staffing/people/{name}`, the **Total units** bubble is gone, replaced by a row of **group-average pph** tiles (Repairs, Dismantlers, Juniors, etc.). One tile per registered group; tiles auto-hide when the operator has no hours in any of that group's WCs. The per-WC table below now has an **Avg (pph)** column alongside Units, and the table headers right-align with their numbers (the old `th.num` was left-aligned, putting headers offset from their numeric cells).
```

- [ ] **Step 4: Commit + push**

```bash
git add CHANGELOG.md
git commit -m "$(cat <<'EOF'
feat: player card stats redesign

Drops Total units bubble in favor of group-average pph tiles
(auto-hidden when empty), adds Avg (pph) column to the per-WC
table, fixes th.num alignment so headers right-align with their
numeric cells.

Spec: docs/superpowers/specs/2026-05-07-player-card-stats-design.md
Plan: docs/superpowers/plans/2026-05-07-player-card-stats.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin main
```

---

## Self-review checklist

- [x] Spec goal 1 (replace Total units bubble) — Task 2 step 2 ✓
- [x] Spec goal 2 (auto-hide empty groups) — Task 1 step 3 (`if hours_sum > 0`) + Task 2 step 2 (`{% if group_avgs %}`) ✓
- [x] Spec goal 3 (Avg column in per-WC table) — Task 1 step 3 (avg_pph on rows) + Task 2 step 3 ✓
- [x] Spec goal 4 (fix th.num alignment) — Task 2 step 1 ✓
- [x] No placeholders, no TBDs ✓
- [x] Type / property consistency: `avg_pph` (float, 1dp or 0), `group_avgs[i].name` (str), `group_avgs[i].pph` (float, 1dp). Used identically across route + tests + template ✓
- [x] Test design: stub `attribution_range`, `attribution_per_day`, `registered_groups()`, `members()`, `load_roster`, `late_report.*` so the tests don't need a DB. Capture context dict via monkey-patched `templates.TemplateResponse` ✓
