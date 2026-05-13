# Widget Workshop: Seed + Duplicate + Edit-Warning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Seed the workshop with 10 starter widget entries that mirror `/recycling` and `/wc/{slug}` widgets, add a Duplicate button per row, show an edit-warning modal when editing a widget that's placed on N dashboards, and convert the TVs settings URL Copy button to a clickable hyperlink.

**Architecture:** Four additive pieces, no schema changes. (1) New `seed_defaults_if_empty()` in `widget_definitions_store`, called from `lifespan`. (2) New `duplicate(id)` store function + `POST /api/widget-defs/{id}/duplicate` endpoint + Duplicate button per row. (3) Pure client-side warning modal triggered from the existing Edit button when `usage_count > 0`. (4) `_settings_tvs.html` swap: span+button → `<a>` hyperlink.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-13-widget-workshop-seed-duplicate-warn-design.md`

---

## File Structure

**Modified files:**
- `src/zira_dashboard/widget_definitions_store.py` — `seed_defaults_if_empty` + `duplicate`
- `src/zira_dashboard/app.py` — call seed in `lifespan`
- `src/zira_dashboard/routes/widgets.py` — `POST /api/widget-defs/{id}/duplicate`
- `src/zira_dashboard/templates/widgets.html` — Duplicate button, `data-usage-count` attribute, edit-warning modal, JS handler updates
- `src/zira_dashboard/templates/_settings_tvs.html` — URL hyperlink swap (drop Copy button + handler)
- `tests/test_widget_definitions_store.py` — seed + duplicate tests
- `tests/test_widgets_routes.py` — duplicate endpoint tests
- `CHANGELOG.md`

No new files. No new tests files. No schema migrations.

---

## Conventions

- Python interpreter: `.venv/Scripts/python.exe`.
- Postgres-touching tests gate on `DATABASE_URL` via the existing module-level `pytestmark`.
- Commit messages: `feat(widgets):`, `test(widgets):`, `docs:`.

---

## Task 1: Store — `seed_defaults_if_empty` + `duplicate`

**Files:**
- Modify: `src/zira_dashboard/widget_definitions_store.py`
- Modify: `tests/test_widget_definitions_store.py`

- [ ] **Step 1: Append failing tests to `tests/test_widget_definitions_store.py`**

```python
def test_seed_defaults_if_empty_seeds_when_table_empty(monkeypatch):
    from zira_dashboard import widget_definitions_store, work_centers_store, staffing, db

    class _Loc:
        def __init__(self, name): self.name = name

    monkeypatch.setattr(
        work_centers_store, "all_group_names",
        lambda kind: ["Repair", "Dismantler"] if kind == "group" else [],
    )
    monkeypatch.setattr(staffing, "LOCATIONS", [_Loc("Repair 1")])

    db.execute("DELETE FROM widget_definitions")
    widget_definitions_store.seed_defaults_if_empty()
    rows = widget_definitions_store.list_definitions()
    names = {r["name"] for r in rows}
    # All 10 seeds present.
    assert "Pallets by WC — Dismantlers" in names
    assert "Pallets by WC — Repairs" in names
    assert "Total Pallets — Dismantlers" in names
    assert "Total Pallets — Repairs" in names
    assert "Pallets Banner — Repair 1" in names
    assert "Daily Progress — Repair 1" in names
    assert "Cumulative Progress — Repair 1" in names
    assert "Downtime Report — Repair 1" in names
    assert "GOAT Race — Repairs" in names
    assert "Monthly Ribbons — Repairs" in names
    # Re-running is a no-op on a non-empty table.
    widget_definitions_store.seed_defaults_if_empty()
    rows_again = widget_definitions_store.list_definitions()
    assert len(rows_again) == len(rows)
    db.execute("DELETE FROM widget_definitions WHERE name LIKE 'Pallets%' OR name LIKE 'Total%' OR name LIKE 'Daily%' OR name LIKE 'Cumulative%' OR name LIKE 'Downtime%' OR name LIKE 'GOAT%' OR name LIKE 'Monthly%'")


def test_seed_skips_missing_group(monkeypatch, caplog):
    """Seeds that reference a missing group are skipped with a warning."""
    from zira_dashboard import widget_definitions_store, work_centers_store, staffing, db
    import logging

    class _Loc:
        def __init__(self, name): self.name = name

    # Only "Repair" group exists, no "Dismantler".
    monkeypatch.setattr(
        work_centers_store, "all_group_names",
        lambda kind: ["Repair"] if kind == "group" else [],
    )
    monkeypatch.setattr(staffing, "LOCATIONS", [_Loc("Repair 1")])

    db.execute("DELETE FROM widget_definitions")
    with caplog.at_level(logging.WARNING):
        widget_definitions_store.seed_defaults_if_empty()
    rows = widget_definitions_store.list_definitions()
    names = {r["name"] for r in rows}
    # Repair entries present, Dismantler entries skipped.
    assert "Pallets by WC — Repairs" in names
    assert "Pallets by WC — Dismantlers" not in names
    assert "Total Pallets — Dismantlers" not in names
    db.execute("DELETE FROM widget_definitions WHERE name LIKE 'Pallets%' OR name LIKE 'Total%' OR name LIKE 'Daily%' OR name LIKE 'Cumulative%' OR name LIKE 'Downtime%' OR name LIKE 'GOAT%' OR name LIKE 'Monthly%'")


def test_seed_skips_missing_wc(monkeypatch):
    """Seeds referencing a missing WC are skipped."""
    from zira_dashboard import widget_definitions_store, work_centers_store, staffing, db

    monkeypatch.setattr(work_centers_store, "all_group_names", lambda kind: ["Repair", "Dismantler"])
    monkeypatch.setattr(staffing, "LOCATIONS", [])  # No WCs

    db.execute("DELETE FROM widget_definitions")
    widget_definitions_store.seed_defaults_if_empty()
    rows = widget_definitions_store.list_definitions()
    names = {r["name"] for r in rows}
    # Group-scoped entries present, WC-scoped entries skipped.
    assert "Pallets by WC — Repairs" in names
    assert "Pallets Banner — Repair 1" not in names
    assert "Daily Progress — Repair 1" not in names
    db.execute("DELETE FROM widget_definitions WHERE name LIKE 'Pallets%' OR name LIKE 'Total%' OR name LIKE 'Daily%' OR name LIKE 'Cumulative%' OR name LIKE 'Downtime%' OR name LIKE 'GOAT%' OR name LIKE 'Monthly%'")


def test_duplicate_creates_copy_with_unique_name():
    from zira_dashboard import widget_definitions_store, db
    original = widget_definitions_store.save(
        name="wt-dupe", type="ribbons", visual={"color": "#22c55e"},
        default_data={"group": "Repairs"},
    )
    dup = widget_definitions_store.duplicate(original["id"])
    assert dup["id"] != original["id"]
    assert dup["name"] == "wt-dupe (copy)"
    assert dup["type"] == "ribbons"
    assert dup["visual"] == {"color": "#22c55e"}
    assert dup["default_data"] == {"group": "Repairs"}
    # A second duplicate appends (copy 2).
    dup2 = widget_definitions_store.duplicate(original["id"])
    assert dup2["name"] == "wt-dupe (copy 2)"
    dup3 = widget_definitions_store.duplicate(original["id"])
    assert dup3["name"] == "wt-dupe (copy 3)"
    db.execute("DELETE FROM widget_definitions WHERE name LIKE 'wt-dupe%'")


def test_duplicate_missing_id_raises():
    from zira_dashboard import widget_definitions_store
    with pytest.raises(LookupError):
        widget_definitions_store.duplicate(999_999_999)
```

- [ ] **Step 2: Confirm tests fail/skip**

```
.venv/Scripts/python.exe -m pytest tests/test_widget_definitions_store.py -v 2>&1 | tail -15
```

Expected: SKIP without `DATABASE_URL`; with DB the new tests fail (functions not defined).

- [ ] **Step 3: Add `_SEED_LIST` + `seed_defaults_if_empty` + `duplicate` to `widget_definitions_store.py`**

Open `src/zira_dashboard/widget_definitions_store.py`. Add `import logging` at the top of the imports (after `from typing import Optional`), and a module-level `_log = logging.getLogger(__name__)`:

```python
from __future__ import annotations

import json
import logging
from typing import Optional

_log = logging.getLogger(__name__)
```

Append the seed list + new functions at the BOTTOM of the file (after the existing `_decode` helper):

```python
# Seed list — 10 starter widgets that mirror the hardcoded widgets on
# /recycling and /wc/{slug}. Inserted once on first boot when the table
# is empty. Group-scoped seeds skip if the group doesn't exist; WC-scoped
# seeds skip if the WC isn't in staffing.LOCATIONS.
_SEED_LIST = [
    # Group-scoped (mirrors /recycling)
    {"name": "Pallets by WC — Dismantlers", "type": "pallets_by_wc",
     "visual": {"color": "#22c55e", "sort": "desc"},
     "default_data": {"group": "Dismantler"},
     "needs_group": "Dismantler"},
    {"name": "Pallets by WC — Repairs", "type": "pallets_by_wc",
     "visual": {"color": "#22c55e", "sort": "desc"},
     "default_data": {"group": "Repair"},
     "needs_group": "Repair"},
    {"name": "Total Pallets — Dismantlers", "type": "kpi",
     "visual": {"color": "#22c55e"},
     "default_data": {"metric": "units_today_group", "group": "Dismantler"},
     "needs_group": "Dismantler"},
    {"name": "Total Pallets — Repairs", "type": "kpi",
     "visual": {"color": "#22c55e"},
     "default_data": {"metric": "units_today_group", "group": "Repair"},
     "needs_group": "Repair"},
    # WC-scoped (default to Repair 1; user duplicates + swaps WC for others)
    {"name": "Pallets Banner — Repair 1", "type": "pallets_banner",
     "visual": {"color": "#22c55e"},
     "default_data": {"wc_name": "Repair 1"},
     "needs_wc": "Repair 1"},
    {"name": "Daily Progress — Repair 1", "type": "daily_progress",
     "visual": {},
     "default_data": {"wc_name": "Repair 1"},
     "needs_wc": "Repair 1"},
    {"name": "Cumulative Progress — Repair 1", "type": "cumulative",
     "visual": {"color": "#22c55e", "show_target": "true"},
     "default_data": {"wc_name": "Repair 1"},
     "needs_wc": "Repair 1"},
    {"name": "Downtime Report — Repair 1", "type": "downtime",
     "visual": {},
     "default_data": {"wc_name": "Repair 1"},
     "needs_wc": "Repair 1"},
    # Mixed (group-scoped widgets that match /wc/{slug}'s defaults)
    {"name": "GOAT Race — Repairs", "type": "goat_race",
     "visual": {"color": "#22c55e"},
     "default_data": {"group": "Repair"},
     "needs_group": "Repair"},
    {"name": "Monthly Ribbons — Repairs", "type": "ribbons",
     "visual": {},
     "default_data": {"group": "Repair"},
     "needs_group": "Repair"},
]


def seed_defaults_if_empty() -> None:
    """Insert the 10-row seed list if `widget_definitions` is empty.

    Seeds whose referenced group isn't in `work_centers_store.all_group_names('group')`
    or whose WC isn't in `staffing.LOCATIONS` are skipped with a warning log so
    a partial plant config doesn't fail boot. Re-running on a non-empty table
    is a no-op — deleted seeds stay deleted across redeploys.
    """
    from . import db, staffing, work_centers_store
    existing = db.query("SELECT 1 FROM widget_definitions LIMIT 1")
    if existing:
        return
    valid_groups = set(work_centers_store.all_group_names("group"))
    valid_wcs = {loc.name for loc in staffing.LOCATIONS}
    inserted = 0
    for entry in _SEED_LIST:
        if "needs_group" in entry and entry["needs_group"] not in valid_groups:
            _log.warning(
                "widget_definitions seed skipping %s — group %r not in registered groups",
                entry["name"], entry["needs_group"],
            )
            continue
        if "needs_wc" in entry and entry["needs_wc"] not in valid_wcs:
            _log.warning(
                "widget_definitions seed skipping %s — WC %r not in staffing.LOCATIONS",
                entry["name"], entry["needs_wc"],
            )
            continue
        save(
            name=entry["name"], type=entry["type"],
            visual=entry["visual"], default_data=entry["default_data"],
        )
        inserted += 1
    _log.info("widget_definitions seeded %d starter rows", inserted)


def duplicate(id: int) -> dict:
    """Clone a definition, appending '(copy)' / '(copy 2)' / ... to the name.

    Raises LookupError if the source id doesn't exist.
    """
    from . import db
    source = get(id)
    if source is None:
        raise LookupError(f"no widget_definitions row with id={id}")
    base = source["name"]
    candidate = f"{base} (copy)"
    n = 2
    while True:
        rows = db.query(
            "SELECT id FROM widget_definitions WHERE name = %s",
            (candidate,),
        )
        if not rows:
            break
        candidate = f"{base} (copy {n})"
        n += 1
    return save(
        name=candidate, type=source["type"],
        visual=source["visual"], default_data=source["default_data"],
    )
```

- [ ] **Step 4: Run tests**

```
.venv/Scripts/python.exe -m pytest tests/test_widget_definitions_store.py -v 2>&1 | tail -15
.venv/Scripts/python.exe -c "from zira_dashboard import widget_definitions_store; print('OK')"
```

Expected: tests SKIP locally without `DATABASE_URL`. Module imports cleanly.

- [ ] **Step 5: Commit**

```
git add src/zira_dashboard/widget_definitions_store.py tests/test_widget_definitions_store.py
git commit -m "$(cat <<'EOF'
feat(widgets): seed_defaults_if_empty + duplicate on widget_definitions_store

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Call `seed_defaults_if_empty` from `lifespan`

**Files:**
- Modify: `src/zira_dashboard/app.py`

- [ ] **Step 1: Add seed call after `tv_displays_store.seed_defaults_if_empty()` in `lifespan`**

Open `src/zira_dashboard/app.py`. Find the lifespan body:

```python
    db.init_pool()
    db.bootstrap_schema()
    from . import tv_displays_store
    tv_displays_store.seed_defaults_if_empty()
    _prewarm_stratustime()
```

Replace with:

```python
    db.init_pool()
    db.bootstrap_schema()
    from . import tv_displays_store, widget_definitions_store
    tv_displays_store.seed_defaults_if_empty()
    widget_definitions_store.seed_defaults_if_empty()
    _prewarm_stratustime()
```

- [ ] **Step 2: Verify app boots**

```
.venv/Scripts/python.exe -c "from zira_dashboard.app import app; print('OK')"
.venv/Scripts/python.exe -m pytest 2>&1 | tail -3
```

Expected: `OK`. Full suite green.

- [ ] **Step 3: Commit**

```
git add src/zira_dashboard/app.py
git commit -m "$(cat <<'EOF'
feat(widgets): call widget_definitions_store.seed_defaults_if_empty in lifespan

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Route — `POST /api/widget-defs/{id}/duplicate`

**Files:**
- Modify: `src/zira_dashboard/routes/widgets.py`
- Modify: `tests/test_widgets_routes.py`

- [ ] **Step 1: Append failing tests to `tests/test_widgets_routes.py`**

```python
def test_post_duplicate_creates_copy():
    c = TestClient(app)
    orig = c.post("/api/widget-defs", json={
        "name": "wr-dup-source", "type": "ribbons",
        "visual": {}, "default_data": {"group": "Repairs"},
    }).json()
    r = c.post(f"/api/widget-defs/{orig['definition']['id']}/duplicate")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["definition"]["name"] == "wr-dup-source (copy)"
    assert body["definition"]["id"] != orig["definition"]["id"]


def test_post_duplicate_unknown_id_returns_404():
    c = TestClient(app)
    r = c.post("/api/widget-defs/999999999/duplicate")
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to confirm fail/skip**

```
.venv/Scripts/python.exe -m pytest tests/test_widgets_routes.py -v 2>&1 | tail -10
```

Expected: SKIP without DB; with DB the new tests 404 since endpoint doesn't exist.

- [ ] **Step 3: Add the endpoint to `routes/widgets.py`**

Open `src/zira_dashboard/routes/widgets.py`. Append at the end of the file:

```python
@router.post("/api/widget-defs/{def_id}/duplicate")
def duplicate_def(def_id: int):
    try:
        dup = widget_definitions_store.duplicate(def_id)
    except LookupError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    return JSONResponse({"ok": True, "definition": dup})
```

- [ ] **Step 4: Run tests + smoke check**

```
.venv/Scripts/python.exe -m pytest tests/test_widgets_routes.py -v 2>&1 | tail -10
.venv/Scripts/python.exe -c "from zira_dashboard.app import app; routes = sorted({r.path for r in app.routes if hasattr(r, 'path')}); [print(p) for p in routes if 'duplicate' in p]"
```

Expected: tests PASS with DB / SKIP without. Smoke output shows `/api/widget-defs/{def_id}/duplicate`.

- [ ] **Step 5: Commit**

```
git add src/zira_dashboard/routes/widgets.py tests/test_widgets_routes.py
git commit -m "$(cat <<'EOF'
feat(widgets): POST /api/widget-defs/{id}/duplicate

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Workshop UI — Duplicate button + edit-warning modal

**Files:**
- Modify: `src/zira_dashboard/templates/widgets.html`

Three edits: (a) add Duplicate button to each row + `data-usage-count` attribute, (b) widen the grid from 3 cols to 4, (c) add the warning modal + new JS handlers.

- [ ] **Step 1: Update the row markup**

Open `src/zira_dashboard/templates/widgets.html`. Find:

```jinja
        <div class="def-row" data-id="{{ d.id }}">
          <div>
            <span class="name">{{ d.name }}</span>
            <span class="type">{{ d.type }}</span>
            {% if d.usage_count and d.usage_count > 0 %}
              <span class="usage" title="Used on {{ d.usage_count }} dashboard{{ 's' if d.usage_count != 1 else '' }}">
                · in use by {{ d.usage_count }}
              </span>
            {% endif %}
          </div>
          <button type="button" class="edit-btn">Edit</button>
          <button type="button" class="danger delete-btn"
                  {% if d.usage_count and d.usage_count > 0 %}disabled title="Remove from {{ d.usage_count }} dashboard(s) first"{% endif %}>
            Delete
          </button>
        </div>
```

Replace with:

```jinja
        <div class="def-row" data-id="{{ d.id }}"
             data-name="{{ d.name }}"
             data-usage-count="{{ d.usage_count or 0 }}">
          <div>
            <span class="name">{{ d.name }}</span>
            <span class="type">{{ d.type }}</span>
            {% if d.usage_count and d.usage_count > 0 %}
              <span class="usage" title="Used on {{ d.usage_count }} dashboard{{ 's' if d.usage_count != 1 else '' }}">
                · in use by {{ d.usage_count }}
              </span>
            {% endif %}
          </div>
          <button type="button" class="edit-btn">Edit</button>
          <button type="button" class="dup-btn">Duplicate</button>
          <button type="button" class="danger delete-btn"
                  {% if d.usage_count and d.usage_count > 0 %}disabled title="Remove from {{ d.usage_count }} dashboard(s) first"{% endif %}>
            Delete
          </button>
        </div>
```

- [ ] **Step 2: Widen the row grid to 4 columns**

In the same file, find:

```css
  .def-row { display: grid; grid-template-columns: 1fr auto auto; gap: 0.5rem;
```

Replace with:

```css
  .def-row { display: grid; grid-template-columns: 1fr auto auto auto; gap: 0.5rem;
```

- [ ] **Step 3: Add the warning modal markup**

Just before the closing `</body>` tag, insert:

```jinja
<div id="edit-warning-modal" hidden>
  <div class="warn-card">
    <h3 id="warn-title"></h3>
    <p id="warn-body">Editing changes the widget everywhere it's placed. If you want to keep the original and customize a copy, choose "Duplicate and edit".</p>
    <div class="warn-actions">
      <button type="button" id="warn-cancel">Cancel</button>
      <button type="button" id="warn-edit">Edit anyway</button>
      <button type="button" id="warn-duplicate" class="primary">Duplicate and edit</button>
    </div>
  </div>
</div>
```

- [ ] **Step 4: Add modal CSS**

In the `<style>` block (just before `</style>`), append:

```css
  #edit-warning-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.4);
    display: grid; place-items: center; z-index: 1000; }
  #edit-warning-modal[hidden] { display: none; }
  #edit-warning-modal .warn-card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 1rem 1.2rem;
    min-width: 360px; max-width: 480px;
    box-shadow: 0 12px 32px rgba(0,0,0,0.25);
  }
  #edit-warning-modal h3 { margin: 0 0 0.5rem; font-size: 1rem; }
  #edit-warning-modal p { margin: 0 0 0.75rem; font-size: 0.88rem; color: var(--muted); }
  #edit-warning-modal .warn-actions { display: flex; gap: 0.5rem; justify-content: flex-end; }
  #edit-warning-modal button {
    background: var(--panel-2); color: var(--fg); border: 1px solid var(--border);
    border-radius: 6px; padding: 0.4rem 1rem; font-weight: 600; cursor: pointer;
  }
  #edit-warning-modal button.primary {
    background: var(--accent); color: white; border-color: var(--accent);
  }
```

- [ ] **Step 5: Update JS — refactor the existing edit-btn handler to use the warning modal, add the dup-btn handler**

In the same file, find the existing edit-btn handler block:

```javascript
  document.querySelectorAll('.edit-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const row = btn.closest('.def-row');
      const id = row.dataset.id;
      fetch('/api/widget-defs').then(r => r.json()).then(data => {
        const def = (data.definitions || []).find(d => d.id == id);
        if (!def) return;
        document.getElementById('def-id').value = def.id;
        document.getElementById('def-name').value = def.name;
        typeSel.value = def.type;
        rebuildFields(def.type, def.visual, def.default_data);
      });
    });
  });
```

Replace with:

```javascript
  function loadDefIntoForm(defId) {
    return fetch('/api/widget-defs').then(r => r.json()).then(data => {
      const def = (data.definitions || []).find(d => d.id == defId);
      if (!def) return null;
      document.getElementById('def-id').value = def.id;
      document.getElementById('def-name').value = def.name;
      typeSel.value = def.type;
      rebuildFields(def.type, def.visual, def.default_data);
      return def;
    });
  }

  function startEdit(row) {
    loadDefIntoForm(row.dataset.id);
  }

  function openWarning(row) {
    const name = row.dataset.name;
    const count = parseInt(row.dataset.usageCount, 10) || 0;
    document.getElementById('warn-title').textContent =
      `"${name}" is used on ${count} dashboard${count === 1 ? '' : 's'}`;
    const modal = document.getElementById('edit-warning-modal');
    modal.dataset.targetId = row.dataset.id;
    modal.hidden = false;
  }

  document.querySelectorAll('.edit-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const row = btn.closest('.def-row');
      const count = parseInt(row.dataset.usageCount, 10) || 0;
      if (count > 0) {
        openWarning(row);
      } else {
        startEdit(row);
      }
    });
  });

  document.querySelectorAll('.dup-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const row = btn.closest('.def-row');
      fetch('/api/widget-defs/' + row.dataset.id + '/duplicate', {method: 'POST'})
        .then(r => r.json()).then(data => {
          if (data.ok) location.reload();
          else alert('Duplicate failed: ' + (data.error || 'unknown'));
        });
    });
  });

  // Warning modal buttons.
  document.getElementById('warn-cancel').addEventListener('click', () => {
    document.getElementById('edit-warning-modal').hidden = true;
  });
  document.getElementById('warn-edit').addEventListener('click', () => {
    const modal = document.getElementById('edit-warning-modal');
    const id = modal.dataset.targetId;
    modal.hidden = true;
    loadDefIntoForm(id);
  });
  document.getElementById('warn-duplicate').addEventListener('click', () => {
    const modal = document.getElementById('edit-warning-modal');
    const id = modal.dataset.targetId;
    modal.hidden = true;
    fetch('/api/widget-defs/' + id + '/duplicate', {method: 'POST'})
      .then(r => r.json()).then(data => {
        if (data.ok) {
          // Reload so the new row appears and gets its data-* attributes;
          // then load it into the form via URL hash for follow-up edit.
          location.hash = '#edit=' + data.definition.id;
          location.reload();
        }
      });
  });
  // After a reload following Duplicate-and-edit, auto-open the editor on the new def.
  if (location.hash.startsWith('#edit=')) {
    const editId = location.hash.split('=')[1];
    loadDefIntoForm(editId);
    history.replaceState(null, '', location.pathname + location.search);
  }
```

- [ ] **Step 6: Verify**

```
.venv/Scripts/python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'), autoescape=True); env.parse(open('src/zira_dashboard/templates/widgets.html', encoding='utf-8').read()); print('parse OK')"
.venv/Scripts/python.exe -c "from zira_dashboard.app import app; print('app OK')"
.venv/Scripts/python.exe -m pytest 2>&1 | tail -3
```

Expected: parse OK, app OK, no test regressions.

- [ ] **Step 7: Commit**

```
git add src/zira_dashboard/templates/widgets.html
git commit -m "$(cat <<'EOF'
feat(widgets): Duplicate button + edit-warning modal in Workshop

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: TVs URL hyperlink + CHANGELOG + push

**Files:**
- Modify: `src/zira_dashboard/templates/_settings_tvs.html`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Replace URL cell — span+button → hyperlink**

Open `src/zira_dashboard/templates/_settings_tvs.html`. Find:

```jinja
          <td class="tv-url-cell">
            <span class="tv-url">/tv/d/{{ d.slug }}</span>
            <button type="button" class="tv-copy-btn">Copy</button>
          </td>
```

Replace with:

```jinja
          <td class="tv-url-cell">
            <a class="tv-url" href="/tv/d/{{ d.slug }}" target="_blank" rel="noopener">/tv/d/{{ d.slug }}</a>
          </td>
```

- [ ] **Step 2: Drop the Copy JS handler + update saveRow to re-sync the hyperlink**

In the same file, find:

```javascript
    tr.querySelector('.tv-copy-btn').addEventListener('click', () => {
      const slug = tr.dataset.slug;
      const url = window.location.origin + '/tv/d/' + slug;
      navigator.clipboard.writeText(url).then(() => {
        const btn = tr.querySelector('.tv-copy-btn');
        const orig = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(() => btn.textContent = orig, 1200);
      });
    });
```

Replace with: (nothing — delete the whole block including the `.addEventListener` lines)

Then find the saveRow success handler that updates the URL text:

```javascript
    }).then(data => {
      if (data.ok) {
        tr.dataset.slug = data.slug;
        const urlEl = tr.querySelector('.tv-url');
        if (urlEl) urlEl.textContent = '/tv/d/' + data.slug;
      }
      return data;
    });
```

Replace with:

```javascript
    }).then(data => {
      if (data.ok) {
        tr.dataset.slug = data.slug;
        const urlEl = tr.querySelector('.tv-url');
        if (urlEl) {
          urlEl.textContent = '/tv/d/' + data.slug;
          urlEl.setAttribute('href', '/tv/d/' + data.slug);
        }
      }
      return data;
    });
```

- [ ] **Step 3: Verify parse + app**

```
.venv/Scripts/python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'), autoescape=True); env.parse(open('src/zira_dashboard/templates/_settings_tvs.html', encoding='utf-8').read()); print('parse OK')"
.venv/Scripts/python.exe -c "from zira_dashboard.app import app; print('app OK')"
.venv/Scripts/python.exe -m pytest 2>&1 | tail -3
```

Expected: parse OK, app OK, suite green.

- [ ] **Step 4: Get current time**

```
powershell.exe -Command "Get-Date -Format 'h:mm tt'"
```

- [ ] **Step 5: Add CHANGELOG entry**

In `CHANGELOG.md`, insert a new `### <HH:MM TT>` block at the top of today's `## 2026-05-13` section:

```markdown
### <HH:MM TT>

- **Widget Workshop seeded with starters + Duplicate button + Edit-warning popup** — three workshop improvements plus one small fix. (1) **10 starter widgets** auto-seed on first boot mirroring the configs on `/recycling` (Pallets by WC + Total Pallets KPI, per group) and `/wc/{slug}` (Pallets Banner, Daily Progress, Cumulative, Downtime, GOAT Race, Monthly Ribbons — default to Repair 1, duplicate and swap WCs for others). Deleted seeds stay deleted across redeploys. (2) **Duplicate button** on every workshop row — POST `/api/widget-defs/{id}/duplicate` clones the row with name "<original> (copy)" / "(copy 2)" / etc., reloads the editor pre-filled on the new row. (3) **Edit-warning popup** fires when you click Edit on a widget that's placed on N dashboards — three buttons: Cancel / Edit anyway (changes affect all placements) / Duplicate and edit (clones the row, edits go to the copy). Rows with no placements skip the modal. (4) Settings → TVs panel: the per-row URL is now a clickable hyperlink that opens in a new tab; the separate Copy button is gone (right-click → copy on the link works).
```

- [ ] **Step 6: Commit + push**

```
git add src/zira_dashboard/templates/_settings_tvs.html CHANGELOG.md
git commit -m "$(cat <<'EOF'
feat: TVs URL hyperlink + widget workshop changelog

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin main
```

Railway picks up the push. After deploy:

1. Visit `/widgets` — should see 10 starter widgets (skips any whose group/WC isn't in your config).
2. Click **Duplicate** on any row → reload → see "<name> (copy)" appended.
3. Place a starter on a custom dashboard (`/dashboards/{slug}` → Add from palette).
4. Back on `/widgets`, click **Edit** on the now-placed widget — warning modal appears with three options.
5. Visit `/settings?section=tvs` — every row's URL is now a clickable hyperlink.

---

## Done

Workshop UX is rounded out: starter library, easy duplication, safe edits with a duplicate-and-edit escape hatch, hyperlinked TV URLs. No schema changes. Existing dashboards untouched.
