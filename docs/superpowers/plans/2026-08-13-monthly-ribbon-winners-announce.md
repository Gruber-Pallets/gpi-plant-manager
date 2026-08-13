# Monthly Ribbon Winners Announce Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On the first plant workday of each month, show a non-dismissible header banner celebrating the previous month’s gold/silver/bronze ribbon winners on operator TVs and the Recycling/New dashboards.

**Architecture:** A pure `ribbon_announce.py` module decides the announce day and builds a plant-wide podium payload from existing `awards.monthly_badges` + overrides. A shared Jinja partial + CSS render in the TV header center (stacked above GOAT Watch) and in the desktop Recycling/New banner strip. Routes pass context with a try/except omit-on-error wrapper, matching GOAT Watch.

**Tech Stack:** Python, FastAPI/Jinja2, existing `awards` / `shift_config` / `work_centers_store`, pytest

## Global Constraints

- Announce only on the first `shift_config.is_workday` day on or after the 1st of the month; one calendar day only.
- Month shown = previous calendar month.
- Surfaces: `/tv/wc/{slug}`; `/recycling` + `/tv/recycling`; `/new` + `/tv/new`. Not `/wc/{slug}` non-TV.
- Full podium (positions 1–3) per included group; identical plant-wide content.
- Omit groups with total production units ≤ 0 for that month, or with an empty podium after overrides.
- Placement: TV header center / desktop GOAT banner spot; never cover widgets.
- No dismiss. Stack above GOAT Watch when both are present.
- No new DB tables. No Slack/email/Inbox. No Forklift ribbons in v1.
- Add no dependencies.
- What's New copy must be kid-friendly per `AGENTS.md`.

## File map

| File | Responsibility |
|------|----------------|
| `src/zira_dashboard/ribbon_announce.py` | Announce-day gate + previous-month podium payload |
| `tests/test_ribbon_announce.py` | Unit tests for gate + payload filtering |
| `src/zira_dashboard/templates/_ribbon_winners_banner.html` | Shared banner markup |
| `src/zira_dashboard/static/ribbon_announce.css` | Banner styles (header-safe) |
| `src/zira_dashboard/static/tv-mode.css` | TV header-center tightening for the ribbon banner |
| `src/zira_dashboard/templates/wc_dashboard.html` | Include CSS + stack ribbon above GOAT on TV only |
| `src/zira_dashboard/templates/recycling.html` | Include CSS + stack ribbon above GOAT (TV + desktop) |
| `src/zira_dashboard/templates/new_dept.html` | Same as Recycling |
| `src/zira_dashboard/routes/wc_dashboard.py` | Pass `ribbon_announce` only when `tv_mode` |
| `src/zira_dashboard/routes/departments.py` | Pass `ribbon_announce` for Recycling + New |
| `tests/test_ribbon_announce_surfaces.py` | Static template + route-context wiring checks |
| `CHANGELOG.md` | Kid-friendly What's New under today |

---

### Task 1: Pure announce helpers

**Files:**
- Create: `src/zira_dashboard/ribbon_announce.py`
- Create: `tests/test_ribbon_announce.py`

**Interfaces:**
- Consumes: `shift_config.is_workday`, `work_centers_store.registered_groups`, `awards.person_days_in_group`, `awards.monthly_badges`, `awards.apply_overrides`, `production_history.daily_records` (via awards helpers), `calendar.month_name`
- Produces:
  - `is_ribbon_announce_day(today: date) -> bool`
  - `previous_month(today: date) -> tuple[int, int]`  # (year, month)
  - `ribbon_announce_payload(today: date) -> dict | None`
    - `None` when not announce day or no groups to show
    - Else `{"year": int, "month": int, "label": str, "groups": [{"group": str, "entries": list[dict]}, ...]}`
    - Each `entries` item matches `monthly_badges` / override shape: `position`, `name`, `day`, `units`, `pph`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ribbon_announce.py`:

```python
"""Unit tests for ribbon_announce — first-workday gate + podium payload."""
from __future__ import annotations

from datetime import date

import pytest


def test_previous_month_rolls_year():
    from zira_dashboard import ribbon_announce
    assert ribbon_announce.previous_month(date(2026, 1, 5)) == (2025, 12)
    assert ribbon_announce.previous_month(date(2026, 8, 3)) == (2026, 7)


def test_is_ribbon_announce_day_when_first_is_workday(monkeypatch):
    from zira_dashboard import ribbon_announce, shift_config
    monkeypatch.setattr(shift_config, "is_workday", lambda d: d.weekday() < 5)
    assert ribbon_announce.is_ribbon_announce_day(date(2026, 7, 1)) is True  # Wed
    assert ribbon_announce.is_ribbon_announce_day(date(2026, 7, 2)) is False


def test_is_ribbon_announce_day_skips_weekend_to_monday(monkeypatch):
    from zira_dashboard import ribbon_announce, shift_config
    monkeypatch.setattr(shift_config, "is_workday", lambda d: d.weekday() < 5)
    # Aug 2026: Sat 1, Sun 2, Mon 3
    assert ribbon_announce.is_ribbon_announce_day(date(2026, 8, 1)) is False
    assert ribbon_announce.is_ribbon_announce_day(date(2026, 8, 2)) is False
    assert ribbon_announce.is_ribbon_announce_day(date(2026, 8, 3)) is True
    assert ribbon_announce.is_ribbon_announce_day(date(2026, 8, 4)) is False


def test_is_ribbon_announce_day_mid_month_false(monkeypatch):
    from zira_dashboard import ribbon_announce, shift_config
    monkeypatch.setattr(shift_config, "is_workday", lambda d: True)
    assert ribbon_announce.is_ribbon_announce_day(date(2026, 8, 15)) is False


def test_payload_none_off_announce_day(monkeypatch):
    from zira_dashboard import ribbon_announce, shift_config
    monkeypatch.setattr(shift_config, "is_workday", lambda d: d.weekday() < 5)
    assert ribbon_announce.ribbon_announce_payload(date(2026, 8, 4)) is None


def _stub_groups_and_records(monkeypatch, *, groups, members_map, records):
    from zira_dashboard import production_history, work_centers_store, awards

    class _FakeLoc:
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(work_centers_store, "registered_groups", lambda: list(groups))
    monkeypatch.setattr(
        work_centers_store,
        "members",
        lambda kind, name: [_FakeLoc(n) for n in members_map.get(name, [])],
    )
    monkeypatch.setattr(
        production_history,
        "daily_records",
        lambda s, e, c=None: [r for r in records if s <= r["day"] <= e],
    )
    monkeypatch.setattr(awards, "apply_overrides", lambda slots, **kw: slots)
    awards._GOAT_CACHE.clear()


def test_payload_omits_zero_production_groups(monkeypatch):
    from zira_dashboard import ribbon_announce, shift_config

    monkeypatch.setattr(shift_config, "is_workday", lambda d: d.weekday() < 5)
    # Aug 3 2026 is first workday → July winners
    _stub_groups_and_records(
        monkeypatch,
        groups=["Repairs", "EmptyGroup"],
        members_map={
            "Repairs": ["Repair 1"],
            "EmptyGroup": ["Ghost 1"],
        },
        records=[
            {"day": date(2026, 7, 10), "person": "Alice", "wc": "Repair 1",
             "units": 100.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 7, 11), "person": "Bob", "wc": "Repair 1",
             "units": 90.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 7, 12), "person": "Cara", "wc": "Repair 1",
             "units": 80.0, "hours": 8.0, "downtime": 0.0},
            # EmptyGroup has only zero-unit rows → omit
            {"day": date(2026, 7, 10), "person": "Zed", "wc": "Ghost 1",
             "units": 0.0, "hours": 8.0, "downtime": 0.0},
        ],
    )
    payload = ribbon_announce.ribbon_announce_payload(date(2026, 8, 3))
    assert payload is not None
    assert payload["year"] == 2026
    assert payload["month"] == 7
    assert payload["label"] == "July 2026"
    assert [g["group"] for g in payload["groups"]] == ["Repairs"]
    assert [e["name"] for e in payload["groups"][0]["entries"]] == ["Alice", "Bob", "Cara"]
    assert [e["position"] for e in payload["groups"][0]["entries"]] == [1, 2, 3]


def test_payload_none_when_all_groups_empty(monkeypatch):
    from zira_dashboard import ribbon_announce, shift_config

    monkeypatch.setattr(shift_config, "is_workday", lambda d: True)
    _stub_groups_and_records(
        monkeypatch,
        groups=["EmptyGroup"],
        members_map={"EmptyGroup": ["Ghost 1"]},
        records=[],
    )
    assert ribbon_announce.ribbon_announce_payload(date(2026, 8, 1)) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_ribbon_announce.py -q`

Expected: FAIL (import / module not found)

- [ ] **Step 3: Implement `ribbon_announce.py`**

Create `src/zira_dashboard/ribbon_announce.py`:

```python
"""First-workday celebration of the previous month's ribbon podiums."""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from . import awards, production_history, shift_config, work_centers_store


def previous_month(today: date) -> tuple[int, int]:
    if today.month == 1:
        return (today.year - 1, 12)
    return (today.year, today.month - 1)


def is_ribbon_announce_day(today: date) -> bool:
    """True iff `today` is the first plant workday of its calendar month."""
    cursor = date(today.year, today.month, 1)
    while cursor <= today:
        try:
            if shift_config.is_workday(cursor):
                return cursor == today
        except Exception:
            pass
        cursor = cursor + timedelta(days=1)
    return False


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return (date(year, month, 1), date(year, month, last_day))


def ribbon_announce_payload(today: date) -> dict | None:
    if not is_ribbon_announce_day(today):
        return None
    year, month = previous_month(today)
    start, end = _month_bounds(year, month)
    records = production_history.daily_records(start, end)
    groups_out: list[dict] = []
    for group in work_centers_store.registered_groups():
        rows = awards.person_days_in_group(group, start, end, records=records)
        total_units = sum(float(r.get("units") or 0) for r in rows)
        if total_units <= 0:
            continue
        entries = awards.apply_overrides(
            awards.monthly_badges(group, year, month, records=records),
            scope="badge",
            group_name=group,
            year=year,
            month=month,
        )
        if not entries:
            continue
        groups_out.append({"group": group, "entries": entries})
    if not groups_out:
        return None
    return {
        "year": year,
        "month": month,
        "label": f"{calendar.month_name[month]} {year}",
        "groups": groups_out,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_ribbon_announce.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/ribbon_announce.py tests/test_ribbon_announce.py
git commit -m "$(cat <<'EOF'
feat: compute monthly ribbon announce payload

Add the first-workday gate and previous-month podium builder so
dashboards can celebrate ribbon winners without new persistence.
EOF
)"
```

---

### Task 2: Banner partial + CSS

**Files:**
- Create: `src/zira_dashboard/templates/_ribbon_winners_banner.html`
- Create: `src/zira_dashboard/static/ribbon_announce.css`
- Modify: `src/zira_dashboard/static/tv-mode.css` (after the existing `.tv-header-center .goat-watch-banner` rules ~192–216)
- Modify: `src/zira_dashboard/templates/wc_dashboard.html`
- Modify: `src/zira_dashboard/templates/recycling.html`
- Modify: `src/zira_dashboard/templates/new_dept.html`
- Create: `tests/test_ribbon_announce_surfaces.py` (static assertions for this task; route stubs added in Task 3)

**Interfaces:**
- Consumes: template context key `ribbon_announce` (`dict | None` from Task 1)
- Produces: visible banner markup when payload has groups; stacked above `_goat_watch_banner.html` in header/desktop spots

- [ ] **Step 1: Write failing static surface tests**

Create `tests/test_ribbon_announce_surfaces.py` with the static checks first:

```python
"""Ribbon winners banner — template wiring + route context."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src/zira_dashboard/templates"


def _read(name: str) -> str:
    return (TEMPLATES / name).read_text()


def test_templates_link_ribbon_css_and_stack_above_goat():
    for name in ("wc_dashboard.html", "recycling.html", "new_dept.html"):
        html = _read(name)
        assert "ribbon_announce.css" in html
        assert 'include "_ribbon_winners_banner.html"' in html
        # Ribbon include must appear before GOAT include in source order
        # so the stack is ribbon-on-top.
        ribbon_i = html.find('include "_ribbon_winners_banner.html"')
        goat_i = html.find('include "_goat_watch_banner.html"')
        assert ribbon_i != -1 and goat_i != -1
        assert ribbon_i < goat_i


def test_wc_dashboard_only_includes_ribbon_in_tv_header_call():
    html = _read("wc_dashboard.html")
    # Non-TV path must NOT include the ribbon partial (spec: TV only).
    assert html.count('include "_ribbon_winners_banner.html"') == 1
    tv_call = html.split("{% if tv_mode %}", 1)[1].split("{% else %}", 1)[0]
    assert 'include "_ribbon_winners_banner.html"' in tv_call


def test_recycling_and_new_include_ribbon_on_desktop_and_tv():
    for name in ("recycling.html", "new_dept.html"):
        html = _read(name)
        assert html.count('include "_ribbon_winners_banner.html"') >= 2
```

- [ ] **Step 2: Run static tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_ribbon_announce_surfaces.py -q`

Expected: FAIL (missing CSS link / include / order)

- [ ] **Step 3: Add partial + CSS + template includes**

Create `src/zira_dashboard/templates/_ribbon_winners_banner.html`:

```html
{# Monthly ribbon winners celebration banner.
   Shown on the first plant workday of the month for the previous month.
   Inputs: ribbon_announce — dict from ribbon_announce.ribbon_announce_payload
   or None/absent. #}
{% if ribbon_announce and ribbon_announce.groups %}
<div class="ribbon-announce-banner" role="region"
     aria-label="{{ ribbon_announce.label }} ribbon winners">
  <div class="ribbon-announce-head">
    <span class="ribbon-announce-medal" aria-hidden="true">🏅</span>
    <span class="ribbon-announce-title">{{ ribbon_announce.label }} Ribbon Winners</span>
  </div>
  <div class="ribbon-announce-groups">
    {% for g in ribbon_announce.groups %}
      <div class="ribbon-announce-group">
        <span class="ribbon-announce-group-name">{{ g.group }}</span>
        {% for e in g.entries %}
          <span class="ribbon-announce-entry">
            <span class="ribbon-announce-pos" aria-hidden="true">
              {% if e.position == 1 %}🥇{% elif e.position == 2 %}🥈{% else %}🥉{% endif %}
            </span>
            <span class="ribbon-announce-person">{{ e.name }}</span>
            {% if e.units is defined and e.units is not none %}
              <span class="ribbon-announce-meta">{{ "%.0f"|format(e.units) }}</span>
            {% endif %}
          </span>
        {% endfor %}
      </div>
    {% endfor %}
  </div>
</div>
{% endif %}
```

Create `src/zira_dashboard/static/ribbon_announce.css`:

```css
/* Monthly ribbon winners banner — Recycling / New / operator TV header. */

.ribbon-announce-banner {
  margin: 0.5rem 1rem 0;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  background: linear-gradient(135deg, #fef9c3 0%, #fde68a 100%);
  border: 1px solid #d97706;
  border-radius: 10px;
  padding: 0.4rem 0.75rem;
  color: #422006;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
  font-size: 0.9rem;
  line-height: 1.25;
  max-width: 100%;
}
html[data-tv-theme="dark"] .ribbon-announce-banner {
  background: linear-gradient(135deg, #422006 0%, #78350f 100%);
  color: #fef3c7;
  border-color: #f59e0b;
}

.ribbon-announce-head {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}
.ribbon-announce-title {
  font-weight: 800;
  letter-spacing: 0.4px;
}
.ribbon-announce-groups {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem 0.85rem;
}
.ribbon-announce-group {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.3rem 0.5rem;
}
.ribbon-announce-group-name {
  font-weight: 700;
  text-transform: uppercase;
  font-size: 0.78rem;
  opacity: 0.85;
}
.ribbon-announce-entry {
  display: inline-flex;
  align-items: baseline;
  gap: 0.2rem;
  white-space: nowrap;
}
.ribbon-announce-meta {
  opacity: 0.75;
  font-size: 0.78rem;
}
```

In `tv-mode.css`, immediately after the `.tv-header-center .goat-watch-banner` block, add:

```css
html[data-tv-theme] .tv-header .tv-header-center {
  flex-direction: column;
  align-items: center;
  gap: 0.35rem;
}
html[data-tv-theme] .tv-header .tv-header-center .ribbon-announce-banner {
  margin: 0;
  max-width: 100%;
  font-size: 0.82rem;
  padding: 0.3rem 0.6rem;
}
html[data-tv-theme] .tv-header-center .ribbon-announce-groups {
  gap: 0.25rem 0.6rem;
}
html[data-tv-theme] .tv-header-center .ribbon-announce-meta {
  /* Prefer ellipsizing meta before names when the center slot is tight. */
  max-width: 4.5rem;
  overflow: hidden;
  text-overflow: ellipsis;
}
```

Note: `.tv-header-center` already has `display: flex; justify-content: center;`. Adding `flex-direction: column` is required so ribbon + GOAT stack vertically inside the center slot without covering widgets.

Update templates:

**`wc_dashboard.html`** — in `{% block head %}`, after the goat_watch.css link:

```html
<link rel="stylesheet" href="/static/ribbon_announce.css?v={{ static_v('ribbon_announce.css') }}">
```

In the TV header call block, stack ribbon above GOAT:

```html
{% call tv_header(
    wc_name,
    crumb="OPERATOR · " + (wc_group or "")|upper,
    right=operators_display or "(unassigned)",
) %}{% include "_ribbon_winners_banner.html" %}{% include "_goat_watch_banner.html" %}{% endcall %}
```

Do **not** add the ribbon include to the non-TV branch.

**`recycling.html` and `new_dept.html`** — link `ribbon_announce.css` in `{% block head %}` next to `goat_watch.css`. In **both** the TV `{% call tv_header %}` center and the desktop `{% include "_goat_watch_banner.html" %}` spot, put `_ribbon_winners_banner.html` **immediately before** `_goat_watch_banner.html`.

- [ ] **Step 4: Run static tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_ribbon_announce_surfaces.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add \
  src/zira_dashboard/templates/_ribbon_winners_banner.html \
  src/zira_dashboard/static/ribbon_announce.css \
  src/zira_dashboard/static/tv-mode.css \
  src/zira_dashboard/templates/wc_dashboard.html \
  src/zira_dashboard/templates/recycling.html \
  src/zira_dashboard/templates/new_dept.html \
  tests/test_ribbon_announce_surfaces.py
git commit -m "$(cat <<'EOF'
feat: add ribbon winners banner chrome

Render the celebration strip in the TV header center and Recycling/New
banner spots, stacked above GOAT Watch so widgets stay clear.
EOF
)"
```

---

### Task 3: Route wiring + CHANGELOG

**Files:**
- Modify: `src/zira_dashboard/routes/wc_dashboard.py`
- Modify: `src/zira_dashboard/routes/departments.py`
- Modify: `tests/test_ribbon_announce_surfaces.py` (add route-context tests)
- Modify: `CHANGELOG.md` (under `## 2026-08-13`)

**Interfaces:**
- Consumes: `ribbon_announce.ribbon_announce_payload(today)`
- Produces: template context key `ribbon_announce` (`dict | None`); safe wrapper returns `None` on error

- [ ] **Step 1: Extend failing route-context tests**

Append to `tests/test_ribbon_announce_surfaces.py`:

```python
from datetime import date


def test_safe_helpers_exist_on_routes():
    from zira_dashboard.routes import wc_dashboard, departments
    assert callable(getattr(wc_dashboard, "_ribbon_announce", None))
    assert callable(getattr(departments, "_ribbon_announce", None))


def test_wc_and_departments_set_ribbon_announce_context():
    from zira_dashboard.routes import wc_dashboard, departments
    wc_src = Path(wc_dashboard.__file__).read_text()
    dept_src = Path(departments.__file__).read_text()
    assert '"ribbon_announce":' in wc_src
    assert "_ribbon_announce" in wc_src
    assert "tv_mode" in wc_src
    assert '"ribbon_announce":' in dept_src
    assert "_ribbon_announce" in dept_src
    # Both recycling and new render paths must set the key.
    assert dept_src.count('"ribbon_announce":') >= 2


def test_ribbon_announce_wrapper_returns_none_on_error(monkeypatch):
    from zira_dashboard.routes import departments

    def _boom(today):
        raise RuntimeError("nope")

    monkeypatch.setattr(
        "zira_dashboard.ribbon_announce.ribbon_announce_payload",
        _boom,
    )
    assert departments._ribbon_announce(date(2026, 8, 3)) is None


def test_ribbon_announce_wrapper_returns_payload(monkeypatch):
    from zira_dashboard.routes import departments
    sample = {
        "year": 2026,
        "month": 7,
        "label": "July 2026",
        "groups": [{"group": "Repairs", "entries": []}],
    }
    monkeypatch.setattr(
        "zira_dashboard.ribbon_announce.ribbon_announce_payload",
        lambda today: sample,
    )
    assert departments._ribbon_announce(date(2026, 8, 3)) == sample
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_ribbon_announce_surfaces.py -q`

Expected: FAIL (missing `_ribbon_announce` / context keys)

- [ ] **Step 3: Wire routes**

In `wc_dashboard.py`, add beside `_goat_watch_active_alerts`:

```python
def _ribbon_announce(today):
    try:
        from .. import ribbon_announce
        return ribbon_announce.ribbon_announce_payload(today)
    except Exception:
        return None
```

In `_render_wc_dashboard`’s template context dict, add:

```python
"ribbon_announce": _ribbon_announce(today) if tv_mode else None,
```

In `departments.py`, add the same `_ribbon_announce` helper next to `_goat_watch_active_alerts` (three-line try/except duplicate is fine and matches local GOAT style).

In `_render_recycling` and `_render_new_dept` context dicts, add:

```python
"ribbon_announce": _ribbon_announce(today),
```

Use the plant `today` already computed in those renders (same variable GOAT Watch uses), not the selected range’s end date.

- [ ] **Step 4: Add CHANGELOG entry**

Under `## 2026-08-13` in `CHANGELOG.md`, add a new subsection (kid-friendly):

```markdown
### Ribbon winners get a shout-out

#### Features

- **On the first work day of a new month, the boards celebrate last month’s ribbon winners.** Operator TVs and the Recycling and New screens show gold, silver, and bronze for every group that had real production, so the plant can cheer them on together.
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest \
  tests/test_ribbon_announce.py \
  tests/test_ribbon_announce_surfaces.py \
  -q
```

Expected: PASS

Also re-run existing New dashboard template surface tests:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest \
  tests/test_new_dashboard_template.py \
  -q
```

Expected: PASS (ribbon include is additive)

- [ ] **Step 6: Commit**

```bash
git add \
  src/zira_dashboard/routes/wc_dashboard.py \
  src/zira_dashboard/routes/departments.py \
  tests/test_ribbon_announce_surfaces.py \
  CHANGELOG.md
git commit -m "$(cat <<'EOF'
feat: show ribbon winners on announce-day dashboards

Wire the first-workday payload into operator TVs and Recycling/New so
last month's podiums appear in the header celebration strip.
EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| First workday gate via `is_workday` | Task 1 |
| Previous month podiums | Task 1 |
| Omit zero-production groups | Task 1 |
| Apply award overrides | Task 1 (`apply_overrides`) |
| Operator TV only (not editor) | Tasks 2–3 |
| Recycling + New desktop + TV | Tasks 2–3 |
| Header center / no widget cover | Task 2 |
| Stack above GOAT Watch | Task 2 |
| No dismiss / all day | Tasks 1–2 (no dismiss UI; day gate only) |
| Omit banner on helper error | Task 3 |
| Kid-friendly What's New | Task 3 |
| No new tables / no Slack | — satisfied by omission |
