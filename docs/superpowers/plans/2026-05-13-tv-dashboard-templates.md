# Layout Templates (Sub-Project 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Save the current per-WC widget arrangement as a named template, then apply it to one WC, every WC in a group, or every WC at once. Lets Dale arrange Repair 1 once and fan it out to Repair 2 / Repair 3 / Trim Saw 1 with a single click.

**Architecture:** New `tv_dashboard_templates` table holds named layout snapshots. New `tv_templates_store.py` module is the data layer (save / load / list / delete / apply). New `routes/tv_templates.py` exposes the CRUD endpoints plus the bulk-apply. WC editor view gets two inline buttons that drive the endpoints. Theme column is in the table per the spec but theme propagation to targets is deferred to sub-project 4 (which adds `tv_displays.theme`); for now the apply endpoint only writes layouts.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, psycopg2 + Postgres, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-13-tv-dashboards-design.md` — sub-project 3 of 4.

---

## File Structure

**New files:**
- `src/zira_dashboard/tv_templates_store.py` — data layer (save / load / list / delete / apply_to_targets)
- `src/zira_dashboard/routes/tv_templates.py` — `POST /api/tv-templates`, `GET /api/tv-templates`, `DELETE /api/tv-templates/{id}`, `POST /api/tv-templates/{id}/apply`
- `tests/test_tv_templates_store.py` — Postgres-gated tests for the store
- `tests/test_tv_templates_routes.py` — integration tests for the API

**Modified files:**
- `src/zira_dashboard/db.py` — append `tv_dashboard_templates` `CREATE TABLE` to `_SCHEMA_DDL`
- `src/zira_dashboard/app.py` — register the new router
- `src/zira_dashboard/templates/wc_dashboard.html` — add Save-as-template + Apply-template inline UI (editor mode only)
- `tests/test_db.py` — assert the new table is created
- `CHANGELOG.md` — one deploy entry

**Responsibility split:** `tv_templates_store.py` is the only module that talks to the `tv_dashboard_templates` table; it also calls into `layout_store.save` to write layouts onto target pages. The route module is thin — it parses requests, calls the store, returns JSON. The WC editor template gains two small inline `<details>` popovers; no new template file needed.

---

## Conventions

- Python interpreter on Dale's Windows box: `.venv/Scripts/python.exe`.
- Postgres-touching tests gate on `DATABASE_URL` via module-level `pytestmark = pytest.mark.skipif(...)`.
- Commit messages: `feat(tv-templates):` / `test(tv-templates):` / `schema(tv-templates):` / `docs:`.

---

## Task 1: Schema migration

**Files:**
- Modify: `src/zira_dashboard/db.py` — append to `_SCHEMA_DDL`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_db.py`:

```python
def test_bootstrap_creates_tv_dashboard_templates_table():
    db.init_pool()
    db.bootstrap_schema()
    rows = db.query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'tv_dashboard_templates'"
    )
    assert len(rows) == 1, "tv_dashboard_templates table missing"
    # Confirm the expected columns exist.
    cols = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'tv_dashboard_templates'"
    )
    names = {r["column_name"] for r in cols}
    expected = {"id", "name", "layout_json", "theme", "created_at", "updated_at"}
    assert expected.issubset(names), f"missing columns: {expected - names}"
```

- [ ] **Step 2: Run test to verify it fails (or skips without DATABASE_URL)**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py::test_bootstrap_creates_tv_dashboard_templates_table -v`
Expected: SKIP without `DATABASE_URL`, FAIL with one (table doesn't exist).

- [ ] **Step 3: Append DDL to `_SCHEMA_DDL` in `db.py`**

Open `src/zira_dashboard/db.py`. Find the end of `_SCHEMA_DDL` (just before the closing `"""` of the schema string). Append:

```sql
-- TV dashboard layout templates ----------------------------------------
-- Named snapshots of a widget-layout arrangement. The /wc/{slug}
-- editor saves the current layout as a template; the apply endpoint
-- fans it out to one WC, every WC in a group, or every WC at once.
-- Theme is stored per template per the spec, but theme propagation
-- to targets waits for sub-project 4 (tv_displays.theme).
CREATE TABLE IF NOT EXISTS tv_dashboard_templates (
  id          SERIAL PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  layout_json JSONB NOT NULL,
  theme       TEXT NOT NULL DEFAULT 'dark' CHECK (theme IN ('light', 'dark')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py -v`
Expected: PASS (or SKIP without DATABASE_URL).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/db.py tests/test_db.py
git commit -m "schema(tv-templates): tv_dashboard_templates table"
```

---

## Task 2: `tv_templates_store.py` — data layer

**Files:**
- Create: `src/zira_dashboard/tv_templates_store.py`
- Test: `tests/test_tv_templates_store.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_tv_templates_store.py`:

```python
"""Postgres-gated tests for tv_templates_store.

Each test resets the templates table for a 'test-' prefix so they
don't collide with real templates Dale has saved.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="tv_templates_store tests need Postgres",
)


@pytest.fixture(autouse=True)
def _clean_templates():
    """Drop every 'test-' prefixed template + layout before/after each test."""
    from zira_dashboard import db
    db.init_pool()
    db.bootstrap_schema()
    db.execute("DELETE FROM tv_dashboard_templates WHERE name LIKE 'test-%'")
    db.execute("DELETE FROM widget_layouts WHERE page LIKE 'test-wc:%'")
    yield
    db.execute("DELETE FROM tv_dashboard_templates WHERE name LIKE 'test-%'")
    db.execute("DELETE FROM widget_layouts WHERE page LIKE 'test-wc:%'")


def test_save_creates_template():
    from zira_dashboard import tv_templates_store
    tv_templates_store.save("test-repairs", [{"id": "wc-pallets-banner", "x": 0, "y": 0, "w": 12, "h": 2}], theme="dark")
    rows = tv_templates_store.list_templates()
    names = [r["name"] for r in rows]
    assert "test-repairs" in names


def test_save_upserts_by_name():
    """Saving twice with the same name updates the layout, not duplicates."""
    from zira_dashboard import tv_templates_store
    tv_templates_store.save("test-A", [{"id": "wc-pallets-banner", "x": 0, "y": 0, "w": 12, "h": 2}], theme="dark")
    tv_templates_store.save("test-A", [{"id": "wc-pallets-banner", "x": 1, "y": 1, "w": 6, "h": 4}], theme="light")
    rows = tv_templates_store.list_templates()
    matching = [r for r in rows if r["name"] == "test-A"]
    assert len(matching) == 1
    loaded = tv_templates_store.load(matching[0]["id"])
    assert loaded["layout_json"][0]["x"] == 1
    assert loaded["theme"] == "light"


def test_delete_removes_template():
    from zira_dashboard import tv_templates_store
    tv_templates_store.save("test-delete-me", [{"id": "wc-pallets-banner", "x": 0, "y": 0, "w": 12, "h": 2}])
    rows = tv_templates_store.list_templates()
    target = next(r for r in rows if r["name"] == "test-delete-me")
    tv_templates_store.delete(target["id"])
    rows_after = tv_templates_store.list_templates()
    assert all(r["name"] != "test-delete-me" for r in rows_after)


def test_apply_to_explicit_targets_writes_layout():
    """apply_to_targets with explicit page list writes each one."""
    from zira_dashboard import tv_templates_store, layout_store
    layout = [{"id": "wc-pallets-banner", "x": 0, "y": 0, "w": 12, "h": 2}]
    tv_templates_store.save("test-explicit", layout, theme="dark")
    rows = tv_templates_store.list_templates()
    tid = next(r for r in rows if r["name"] == "test-explicit")["id"]

    result = tv_templates_store.apply_to_targets(tid, ["test-wc:a", "test-wc:b"])
    assert sorted(result["applied_pages"]) == ["test-wc:a", "test-wc:b"]
    assert result["applied_count"] == 2
    assert layout_store.load("test-wc:a")[0]["id"] == "wc-pallets-banner"
    assert layout_store.load("test-wc:b")[0]["id"] == "wc-pallets-banner"


def test_apply_returns_zero_when_template_missing():
    from zira_dashboard import tv_templates_store
    result = tv_templates_store.apply_to_targets(999_999_999, ["test-wc:a"])
    assert result == {"applied_count": 0, "applied_pages": [], "error": "template not found"}


def test_resolve_targets_explicit_list_passes_through():
    """resolve_targets normalizes the various input shapes to a flat list of page keys."""
    from zira_dashboard import tv_templates_store
    out = tv_templates_store.resolve_targets(["wc:repair-1", "wc:repair-2"])
    assert sorted(out) == ["wc:repair-1", "wc:repair-2"]


def test_resolve_targets_group_expands(monkeypatch):
    """resolve_targets('group:Repairs') expands to every WC slug in that group."""
    from zira_dashboard import tv_templates_store, work_centers_store

    class _Loc:
        def __init__(self, name): self.name = name

    monkeypatch.setattr(
        work_centers_store, "members",
        lambda kind, name: [_Loc("Repair 1"), _Loc("Repair 2")] if (kind, name) == ("group", "Repairs") else [],
    )
    out = tv_templates_store.resolve_targets("group:Repairs")
    assert sorted(out) == ["wc:repair-1", "wc:repair-2"]


def test_resolve_targets_all_expands_to_every_wc(monkeypatch):
    """resolve_targets('all') expands to every Location.name in staffing.LOCATIONS."""
    from zira_dashboard import tv_templates_store, staffing

    class _Loc:
        def __init__(self, name): self.name = name

    monkeypatch.setattr(staffing, "LOCATIONS", [_Loc("Repair 1"), _Loc("Hand Build #1")])
    out = tv_templates_store.resolve_targets("all")
    assert sorted(out) == ["wc:hand-build-1", "wc:repair-1"]
```

- [ ] **Step 2: Run tests to verify they fail (or skip)**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tv_templates_store.py -v`
Expected: 8 SKIPS without DATABASE_URL; FAILs with one (module doesn't exist).

- [ ] **Step 3: Create the store module**

Create `src/zira_dashboard/tv_templates_store.py`:

```python
"""Persistence + apply layer for TV-dashboard layout templates.

A template is a named snapshot of a widget-layout JSON. The /wc/{slug}
editor can save its current arrangement as a template, then apply that
template to one WC, every WC in a group, or every WC at once.

Theme is stored per template (column on the table), but theme
propagation to target WCs waits for sub-project 4, which adds a
`tv_displays.theme` column. For now, apply_to_targets only writes
layouts.
"""
from __future__ import annotations

import json
from typing import Sequence

from . import layout_store


def save(name: str, layout: list[dict], theme: str = "dark") -> None:
    """UPSERT a named template by `name`.

    `layout` is the list of {id, x, y, w, h} gridstack items. The same
    normalization the layout API uses applies — items without an id
    are dropped, numbers are coerced to int.
    """
    from . import db
    items = [layout_store._normalize(i) for i in (layout or []) if isinstance(i, dict) and i.get("id")]
    if theme not in ("light", "dark"):
        theme = "dark"
    db.execute(
        "INSERT INTO tv_dashboard_templates (name, layout_json, theme, updated_at) "
        "VALUES (%s, %s::jsonb, %s, now()) "
        "ON CONFLICT (name) DO UPDATE SET "
        "  layout_json = EXCLUDED.layout_json, "
        "  theme = EXCLUDED.theme, "
        "  updated_at = now()",
        (name, json.dumps(items), theme),
    )


def list_templates() -> list[dict]:
    """All templates as {id, name, theme, updated_at} dicts. Newest first."""
    from . import db
    rows = db.query(
        "SELECT id, name, theme, updated_at FROM tv_dashboard_templates "
        "ORDER BY updated_at DESC"
    )
    return [
        {
            "id": int(r["id"]),
            "name": r["name"],
            "theme": r["theme"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def load(template_id: int) -> dict | None:
    """Full template by id: {id, name, layout_json, theme}.

    Returns None if no row with that id."""
    from . import db
    rows = db.query(
        "SELECT id, name, layout_json, theme FROM tv_dashboard_templates "
        "WHERE id = %s",
        (template_id,),
    )
    if not rows:
        return None
    r = rows[0]
    raw = r["layout_json"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    return {
        "id": int(r["id"]),
        "name": r["name"],
        "layout_json": raw or [],
        "theme": r["theme"],
    }


def delete(template_id: int) -> None:
    from . import db
    db.execute("DELETE FROM tv_dashboard_templates WHERE id = %s", (template_id,))


def resolve_targets(targets) -> list[str]:
    """Normalize the various target shapes into a flat list of page keys.

    Accepts:
      - list[str] of page keys: ["wc:repair-1", "wc:repair-2"] — passed through
      - "group:<group_name>": expand to every WC in that group
      - "all": every Location.name in staffing.LOCATIONS
    """
    from . import staffing, work_centers_store
    from .wc_dashboard_data import slug_for_wc

    if isinstance(targets, list):
        return [t for t in targets if isinstance(t, str) and t]

    if isinstance(targets, str):
        if targets == "all":
            return [f"wc:{slug_for_wc(loc.name)}" for loc in staffing.LOCATIONS]
        if targets.startswith("group:"):
            group_name = targets[len("group:"):]
            members = work_centers_store.members("group", group_name) or []
            return [f"wc:{slug_for_wc(loc.name)}" for loc in members]
    return []


def apply_to_targets(template_id: int, targets) -> dict:
    """Apply the template's layout to each target page.

    Targets can be a list of page keys, "group:<name>", or "all".
    Each target gets its widget_layouts row upserted via layout_store.
    Theme propagation deferred to sub-project 4.
    """
    tmpl = load(template_id)
    if tmpl is None:
        return {"applied_count": 0, "applied_pages": [], "error": "template not found"}
    pages = resolve_targets(targets)
    for page in pages:
        layout_store.save(page, tmpl["layout_json"])
    return {
        "applied_count": len(pages),
        "applied_pages": pages,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tv_templates_store.py -v`
Expected: 8 PASS with DATABASE_URL, 8 SKIP without.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/tv_templates_store.py tests/test_tv_templates_store.py
git commit -m "feat(tv-templates): tv_templates_store — save / list / load / delete / apply"
```

---

## Task 3: API endpoints

**Files:**
- Create: `src/zira_dashboard/routes/tv_templates.py`
- Modify: `src/zira_dashboard/app.py` — register the router
- Test: `tests/test_tv_templates_routes.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_tv_templates_routes.py`:

```python
"""Integration tests for the tv-templates API endpoints."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="tv-templates route tests need Postgres",
)


@pytest.fixture(autouse=True)
def _clean_templates():
    from zira_dashboard import db
    db.init_pool()
    db.bootstrap_schema()
    db.execute("DELETE FROM tv_dashboard_templates WHERE name LIKE 'rt-%'")
    db.execute("DELETE FROM widget_layouts WHERE page LIKE 'rt-wc:%'")
    yield
    db.execute("DELETE FROM tv_dashboard_templates WHERE name LIKE 'rt-%'")
    db.execute("DELETE FROM widget_layouts WHERE page LIKE 'rt-wc:%'")


def test_post_save_creates_template():
    c = TestClient(app)
    r = c.post("/api/tv-templates", json={
        "name": "rt-template",
        "layout": [{"id": "wc-pallets-banner", "x": 0, "y": 0, "w": 12, "h": 2}],
        "theme": "dark",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True


def test_post_save_rejects_missing_name():
    c = TestClient(app)
    r = c.post("/api/tv-templates", json={"layout": []})
    assert r.status_code == 400


def test_get_list_returns_saved_templates():
    c = TestClient(app)
    c.post("/api/tv-templates", json={
        "name": "rt-list-test",
        "layout": [{"id": "wc-pallets-banner", "x": 0, "y": 0, "w": 12, "h": 2}],
    })
    r = c.get("/api/tv-templates")
    assert r.status_code == 200
    names = [t["name"] for t in r.json()["templates"]]
    assert "rt-list-test" in names


def test_delete_template():
    c = TestClient(app)
    c.post("/api/tv-templates", json={
        "name": "rt-to-delete",
        "layout": [{"id": "wc-pallets-banner", "x": 0, "y": 0, "w": 12, "h": 2}],
    })
    list_r = c.get("/api/tv-templates")
    tid = next(t["id"] for t in list_r.json()["templates"] if t["name"] == "rt-to-delete")
    r = c.delete(f"/api/tv-templates/{tid}")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_post_apply_to_explicit_targets():
    from zira_dashboard import layout_store
    c = TestClient(app)
    c.post("/api/tv-templates", json={
        "name": "rt-apply",
        "layout": [{"id": "wc-pallets-banner", "x": 0, "y": 0, "w": 12, "h": 2}],
    })
    list_r = c.get("/api/tv-templates")
    tid = next(t["id"] for t in list_r.json()["templates"] if t["name"] == "rt-apply")

    r = c.post(f"/api/tv-templates/{tid}/apply", json={
        "targets": ["rt-wc:a", "rt-wc:b"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert sorted(body["applied_pages"]) == ["rt-wc:a", "rt-wc:b"]
    assert body["applied_count"] == 2
    # Verify the layouts were actually written.
    assert layout_store.load("rt-wc:a")[0]["id"] == "wc-pallets-banner"
    assert layout_store.load("rt-wc:b")[0]["id"] == "wc-pallets-banner"


def test_post_apply_unknown_id_returns_404():
    c = TestClient(app)
    r = c.post("/api/tv-templates/999999999/apply", json={"targets": ["wc:nowhere"]})
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail/skip**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tv_templates_routes.py -v`
Expected: SKIP without DATABASE_URL; FAILs with one (routes missing).

- [ ] **Step 3: Create the route module**

Create `src/zira_dashboard/routes/tv_templates.py`:

```python
"""HTTP API for TV-dashboard layout templates.

  POST   /api/tv-templates                     save (upsert by name)
  GET    /api/tv-templates                     list
  DELETE /api/tv-templates/{template_id}       delete
  POST   /api/tv-templates/{template_id}/apply apply to one/many WCs
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import tv_templates_store

router = APIRouter()


@router.post("/api/tv-templates")
async def save_template(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    name = (body or {}).get("name")
    layout = (body or {}).get("layout") or []
    theme = (body or {}).get("theme") or "dark"
    if not isinstance(name, str) or not name.strip():
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    if not isinstance(layout, list):
        return JSONResponse({"ok": False, "error": "layout must be a list"}, status_code=400)
    if theme not in ("light", "dark"):
        return JSONResponse({"ok": False, "error": "theme must be light or dark"}, status_code=400)
    tv_templates_store.save(name.strip(), layout, theme=theme)
    return JSONResponse({"ok": True, "name": name.strip()})


@router.get("/api/tv-templates")
def list_templates():
    rows = tv_templates_store.list_templates()
    return JSONResponse({
        "templates": [
            {
                "id": r["id"],
                "name": r["name"],
                "theme": r["theme"],
                "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
            }
            for r in rows
        ]
    })


@router.delete("/api/tv-templates/{template_id}")
def delete_template(template_id: int):
    tv_templates_store.delete(template_id)
    return JSONResponse({"ok": True})


@router.post("/api/tv-templates/{template_id}/apply")
async def apply_template(template_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    targets = (body or {}).get("targets")
    if not targets:
        return JSONResponse({"ok": False, "error": "targets required"}, status_code=400)
    result = tv_templates_store.apply_to_targets(template_id, targets)
    if result.get("error") == "template not found":
        return JSONResponse({"ok": False, "error": result["error"]}, status_code=404)
    return JSONResponse({
        "ok": True,
        "applied_count": result["applied_count"],
        "applied_pages": result["applied_pages"],
    })
```

- [ ] **Step 4: Register the router in `app.py`**

Open `src/zira_dashboard/app.py`. In the import block (the `from .routes import (...)` block), add `tv_templates` to the alphabetical list:

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
    tv_templates,
    value_streams,
    wc_dashboard,
)
```

Then add the include line near the bottom alongside the other includes. Insert just after the existing `app.include_router(wc_dashboard.router)` line:

```python
app.include_router(wc_dashboard.router)
app.include_router(tv_templates.router)
```

- [ ] **Step 5: Run tests + smoke check**

```bash
.venv/Scripts/python.exe -m pytest tests/test_tv_templates_routes.py -v
.venv/Scripts/python.exe -c "
from zira_dashboard.app import app
routes = sorted({r.path for r in app.routes if hasattr(r, 'path')})
for p in routes:
    if '/tv-templates' in p:
        print(p)
"
```
Expected pytest output: 6 PASS with DATABASE_URL, 6 SKIP without.
Expected smoke output: four template endpoints listed.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/tv_templates.py src/zira_dashboard/app.py tests/test_tv_templates_routes.py
git commit -m "feat(tv-templates): API endpoints — save / list / delete / apply"
```

---

## Task 4: WC editor UI — Save-as / Apply-template buttons

**Files:**
- Modify: `src/zira_dashboard/templates/wc_dashboard.html`

The editor view (`tv_mode == False`) gets two `<details>` popovers under the header: "Save as template…" and "Apply template…". Both hit the API endpoints from Task 3. The TV view stays untouched.

- [ ] **Step 1: Add inline editor controls to `wc_dashboard.html`**

Open `src/zira_dashboard/templates/wc_dashboard.html`. Find the `{{ tv_header(...) }}` call near the top of `<body>`. Right after that call (and BEFORE the `<main>` tag), insert this block:

```jinja
{% if not tv_mode %}
<div class="wc-template-bar">
  <details class="wc-template-popover">
    <summary class="wc-template-btn">Save as template…</summary>
    <div class="wc-template-form">
      <input type="text" id="wc-template-name" placeholder="Template name (e.g. Repairs)">
      <button type="button" id="wc-template-save">Save</button>
      <span class="wc-template-status" id="wc-template-save-status"></span>
    </div>
  </details>
  <details class="wc-template-popover">
    <summary class="wc-template-btn">Apply template…</summary>
    <div class="wc-template-form">
      <select id="wc-template-pick"><option value="">— pick a template —</option></select>
      <select id="wc-template-target">
        <option value="this">Apply to this WC only</option>
        <option value="group">Apply to every WC in this group</option>
        <option value="all">Apply to every WC</option>
      </select>
      <button type="button" id="wc-template-apply">Apply</button>
      <span class="wc-template-status" id="wc-template-apply-status"></span>
    </div>
  </details>
</div>
{% endif %}
```

- [ ] **Step 2: Add the supporting JS at the bottom of the existing `<script>` block**

Still in `wc_dashboard.html`, find the existing `<script>` block that initializes `grid`. Inside the `{% if tv_mode %}grid.disable();{% else %}...{% endif %}` block, AFTER the `grid.on('change', persistLayout);` line, add the template-UI handlers:

```jinja
  // ---- Template Save / Apply UI ----
  function templateLayout() {
    return grid.save(false).map(it => ({
      id: it.id, x: it.x, y: it.y, w: it.w, h: it.h,
    })).filter(it => it.id);
  }

  function refreshTemplateList() {
    fetch('/api/tv-templates').then(r => r.json()).then(data => {
      const sel = document.getElementById('wc-template-pick');
      const current = sel.value;
      sel.innerHTML = '<option value="">— pick a template —</option>'
        + (data.templates || []).map(t => `<option value="${t.id}">${t.name}</option>`).join('');
      if (current) sel.value = current;
    });
  }

  document.getElementById('wc-template-save').addEventListener('click', () => {
    const name = document.getElementById('wc-template-name').value.trim();
    const status = document.getElementById('wc-template-save-status');
    if (!name) { status.textContent = 'name required'; return; }
    status.textContent = 'Saving…';
    fetch('/api/tv-templates', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, layout: templateLayout()}),
    }).then(r => r.json()).then(data => {
      status.textContent = data.ok ? `Saved "${name}"` : ('Error: ' + (data.error || 'unknown'));
      if (data.ok) refreshTemplateList();
    });
  });

  document.getElementById('wc-template-apply').addEventListener('click', () => {
    const tid = document.getElementById('wc-template-pick').value;
    const scope = document.getElementById('wc-template-target').value;
    const status = document.getElementById('wc-template-apply-status');
    if (!tid) { status.textContent = 'pick a template first'; return; }
    let targets;
    if (scope === 'this') targets = ['{{ layout_key }}'];
    else if (scope === 'group') targets = '{{ ('group:' + wc_group) if wc_group else 'all' }}';
    else targets = 'all';
    status.textContent = 'Applying…';
    fetch(`/api/tv-templates/${tid}/apply`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({targets: targets}),
    }).then(r => r.json()).then(data => {
      if (data.ok) {
        status.textContent = `Applied to ${data.applied_count} WC${data.applied_count === 1 ? '' : 's'} · reloading…`;
        setTimeout(() => location.reload(), 700);
      } else {
        status.textContent = 'Error: ' + (data.error || 'unknown');
      }
    });
  });

  refreshTemplateList();
```

- [ ] **Step 3: Add styles to `wc_dashboard.css`**

Open `src/zira_dashboard/static/wc_dashboard.css`. Append:

```css
/* Template Save / Apply controls (editor view only). */
.wc-template-bar { display: flex; gap: 10px; padding: 8px 16px 0; }
.wc-template-popover { position: relative; }
.wc-template-btn {
  cursor: pointer;
  background: var(--panel, #fff);
  border: 1px solid var(--border, #d8dee5);
  border-radius: 6px;
  padding: 5px 12px;
  font-size: 13px;
  color: var(--fg, #1f2937);
  list-style: none;
}
.wc-template-btn::marker, .wc-template-btn::-webkit-details-marker { display: none; }
.wc-template-popover[open] .wc-template-btn { background: var(--panel-2, #e3e8ee); }
.wc-template-form {
  position: absolute; left: 0; top: calc(100% + 4px);
  background: var(--panel, #fff);
  border: 1px solid var(--border, #d8dee5);
  border-radius: 8px;
  padding: 10px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.1);
  z-index: 10;
  display: flex; gap: 8px; align-items: center;
  white-space: nowrap;
}
.wc-template-form input, .wc-template-form select {
  background: var(--panel-2, #f1f4f7);
  border: 1px solid var(--border, #d8dee5);
  border-radius: 5px;
  padding: 5px 8px;
  font: inherit;
  font-size: 13px;
  color: var(--fg, #1f2937);
}
.wc-template-form button {
  background: var(--accent-dim, #dcfce7);
  color: var(--accent, #16a34a);
  border: 1px solid var(--accent, #16a34a);
  border-radius: 5px;
  padding: 5px 12px;
  font: inherit;
  font-weight: 700;
  font-size: 13px;
  cursor: pointer;
}
.wc-template-status { color: var(--muted, #6b7280); font-size: 12px; min-width: 100px; }
```

- [ ] **Step 4: Verify the template still parses**

Run:
```bash
.venv/Scripts/python.exe -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'), autoescape=True)
env.parse(open('src/zira_dashboard/templates/wc_dashboard.html', encoding='utf-8').read())
print('parse OK')
"
.venv/Scripts/python.exe -c "from zira_dashboard import app; print('OK')"
```
Expected: `parse OK` then `OK`.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/wc_dashboard.html src/zira_dashboard/static/wc_dashboard.css
git commit -m "feat(tv-templates): WC editor Save-as / Apply-template buttons"
```

---

## Task 5: CHANGELOG + push

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run the full test suite**

Run: `.venv/Scripts/python.exe -m pytest 2>&1 | tail -3`
Expected: pass count grows by the new tests; no new failures.

- [ ] **Step 2: Get the current time**

Run: `powershell.exe -Command "Get-Date -Format 'h:mm tt'"`

- [ ] **Step 3: Add CHANGELOG entry**

In `CHANGELOG.md`, insert a new `### <HH:MM TT>` block at the top of today's `## 2026-05-13` section:

```markdown
### <HH:MM TT>

- **Per-WC dashboard layout templates** — arrange Repair 1's six widgets once, then fan that arrangement out to every other WC with a click. New `tv_dashboard_templates` table stores named layout snapshots. The `/wc/{slug}` editor now has **Save as template…** and **Apply template…** popovers above the widget grid: pick a template, choose "this WC only" / "every WC in this group" / "every WC", click Apply. Underlying API: `POST /api/tv-templates` (save), `GET /api/tv-templates` (list), `DELETE /api/tv-templates/{id}`, `POST /api/tv-templates/{id}/apply` with `targets` accepting an explicit page list, `group:<name>`, or `"all"`. Theme is stored per template per the spec but theme propagation to target WCs waits for sub-project 4 (Settings panel + tv_displays table).
```

- [ ] **Step 4: Commit + push**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): per-WC dashboard layout templates"
git push origin main
```

Railway picks up the push and redeploys. After deploy, visit `https://gpiplantmanager.com/wc/repair-1`, arrange the widgets, click **Save as template…**, give it a name. Then visit `https://gpiplantmanager.com/wc/repair-2`, click **Apply template…**, pick the template, choose "every WC in this group", click Apply. Both WCs now share Repair 1's layout.

---

## Done

Layout templates ship. Save once, fan out by WC / group / all. Theme storage column is present per the spec but propagation deferred to sub-project 4. The Settings panel UI for managing templates (delete, rename) arrives in that sub-project too; for now the API supports delete and the editor popovers cover save + apply.

If a future widget set adds new IDs that aren't in older templates, applying an older template simply leaves the new widgets in their default layout positions — no error, just partial coverage. That's the right behavior and falls out of `layout_store.save` only writing what's in the layout array.
