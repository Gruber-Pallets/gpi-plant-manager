# Widget Workshop — Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close out the widget-workshop master spec. Three polish items: (1) custom dashboards can be added to the **TVs settings** list and bookmarked at `/tv/d/{slug}`, (2) the dashboard editor gets a **per-placement edit popover** (⋮ button) so the data scope can be overridden for an individual widget after it's been dropped, (3) the workshop list shows an **"in use by N dashboards"** hint next to each widget so it's obvious why a delete might fail.

**Architecture:** Each piece is small and additive. (1) extends `tv_displays` with `custom_dashboard_id` + a new `kind='custom'` value, and routes `/tv/d/{slug}` for custom-kind rows to the existing custom-dashboard render helper. (2) reuses the type registry's `data_params_schema` (already exposed via `/api/widgets/types`) and the existing `PATCH /api/placements/{id}` endpoint. (3) is one new field in `list_definitions` + one template tweak.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, psycopg2 + Postgres, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-13-widget-workshop-and-custom-dashboards-design.md` (master) and `docs/superpowers/specs/2026-05-13-tv-displays-panel-design.md` (sub-project 4).

---

## File Structure

**Modified files:**
- `src/zira_dashboard/db.py` — add `custom_dashboard_id` column to `tv_displays` + extend kind CHECK
- `src/zira_dashboard/tv_displays_store.py` — handle `custom_dashboard_id` in save / by_slug / list
- `src/zira_dashboard/routes/tv_displays.py` — `kind='custom'` branch in `/tv/d/{slug}` + validation
- `src/zira_dashboard/templates/_settings_tvs.html` — "Custom dashboard" kind option + cascading slug picker
- `src/zira_dashboard/routes/settings.py` — pass `custom_dashboards_rows` to the template
- `src/zira_dashboard/templates/custom_dashboard.html` — per-placement ⋮ button + JS popover
- `src/zira_dashboard/widget_definitions_store.py` — `list_definitions` returns `usage_count`
- `src/zira_dashboard/templates/widgets.html` — show "in use by N" next to each preset, disable Delete when in use
- `tests/test_db.py` — assert new column + extended CHECK
- `tests/test_tv_displays_store.py` — kind='custom' save/load
- `tests/test_tv_displays_routes.py` — kind='custom' dispatches
- `tests/test_widget_definitions_store.py` — list_definitions returns usage_count
- `CHANGELOG.md` — one deploy entry

**Responsibility split:** Each piece touches independent surface area. No file gets restructured.

---

## Conventions

- Python interpreter: `.venv/Scripts/python.exe`.
- Postgres tests gate on `DATABASE_URL` via module-level `pytestmark`.
- Slug derivation reuses `wc_dashboard_data.slug_for_wc`.
- Commit messages: `feat(widgets-p3):` / `schema(tv-displays):` / `docs:`.

---

## Task 1: Schema migration — extend `tv_displays` for custom kind

**Files:**
- Modify: `src/zira_dashboard/db.py` — extend `tv_displays` CREATE TABLE; add ALTER TABLE for migration
- Test: `tests/test_db.py`

The existing `tv_displays` table has `kind CHECK IN ('vs_recycling', 'vs_new', 'wc')`. Phase 3 adds `'custom'`. We can't redefine a CHECK constraint via `CREATE TABLE IF NOT EXISTS` (no-op on existing tables), so we add explicit `ALTER TABLE` statements that are idempotent on both fresh and existing databases.

- [ ] **Step 1: Append failing tests to `tests/test_db.py`**

```python
def test_tv_displays_kind_allows_custom():
    db.init_pool()
    db.bootstrap_schema()
    # Cleanup any prior leftover.
    db.execute("DELETE FROM tv_displays WHERE slug = 'p3-kind-test'")
    # Should NOT raise on inserting kind='custom'.
    db.execute(
        "INSERT INTO tv_displays (name, slug, kind, custom_dashboard_id, theme) "
        "VALUES ('p3 kind test', 'p3-kind-test', 'custom', NULL, 'dark')"
    )
    rows = db.query("SELECT kind FROM tv_displays WHERE slug = 'p3-kind-test'")
    assert rows and rows[0]["kind"] == "custom"
    db.execute("DELETE FROM tv_displays WHERE slug = 'p3-kind-test'")


def test_tv_displays_has_custom_dashboard_id_column():
    db.init_pool()
    db.bootstrap_schema()
    cols = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'tv_displays'"
    )
    names = {r["column_name"] for r in cols}
    assert "custom_dashboard_id" in names


def test_tv_displays_custom_dashboard_id_fk_on_delete_set_null():
    """Deleting a custom dashboard should NULL the FK on any tv_displays row
    that references it, not cascade-delete the row."""
    db.init_pool()
    db.bootstrap_schema()
    db.execute("DELETE FROM tv_displays WHERE slug = 'p3-fk-test'")
    db.execute("DELETE FROM custom_dashboards WHERE slug = 'p3-fk-dash'")
    # Create a dashboard, then a tv_displays row pointing at it.
    rows = db.query(
        "INSERT INTO custom_dashboards (name, slug, scope_kind, scope_value) "
        "VALUES ('p3 fk dash', 'p3-fk-dash', 'wc', 'Repair 1') RETURNING id"
    )
    dash_id = rows[0]["id"]
    db.execute(
        "INSERT INTO tv_displays (name, slug, kind, custom_dashboard_id, theme) "
        "VALUES ('p3 fk test', 'p3-fk-test', 'custom', %s, 'dark')",
        (dash_id,),
    )
    # Deleting the dashboard should set the column to NULL.
    db.execute("DELETE FROM custom_dashboards WHERE id = %s", (dash_id,))
    rows = db.query("SELECT custom_dashboard_id FROM tv_displays WHERE slug = 'p3-fk-test'")
    assert rows and rows[0]["custom_dashboard_id"] is None
    db.execute("DELETE FROM tv_displays WHERE slug = 'p3-fk-test'")
```

- [ ] **Step 2: Run to confirm fail/skip**

Run: `.venv/Scripts/python.exe -m pytest tests/test_db.py -v 2>&1 | tail -15`
Expected: SKIP without `DATABASE_URL`; with DB: the new tests FAIL (column missing, kind='custom' rejected by CHECK).

- [ ] **Step 3: Modify `_SCHEMA_DDL` in `db.py`**

Open `src/zira_dashboard/db.py`. Two changes:

**3a — update the `tv_displays` CREATE TABLE block** to include the new column and the extended CHECK (so fresh databases get the right shape). Find:

```sql
CREATE TABLE IF NOT EXISTS tv_displays (
  id          SERIAL PRIMARY KEY,
  name        TEXT NOT NULL,
  slug        TEXT NOT NULL UNIQUE,
  kind        TEXT NOT NULL CHECK (kind IN ('vs_recycling', 'vs_new', 'wc')),
  wc_name     TEXT,
  theme       TEXT NOT NULL DEFAULT 'dark' CHECK (theme IN ('light', 'dark')),
  sort_order  INTEGER NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Replace with:

```sql
CREATE TABLE IF NOT EXISTS tv_displays (
  id                  SERIAL PRIMARY KEY,
  name                TEXT NOT NULL,
  slug                TEXT NOT NULL UNIQUE,
  kind                TEXT NOT NULL CHECK (kind IN ('vs_recycling', 'vs_new', 'wc', 'custom')),
  wc_name             TEXT,
  custom_dashboard_id INTEGER,
  theme               TEXT NOT NULL DEFAULT 'dark' CHECK (theme IN ('light', 'dark')),
  sort_order          INTEGER NOT NULL DEFAULT 0,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**3b — append idempotent ALTER TABLE statements** to handle existing databases. Append to `_SCHEMA_DDL` just before its closing `"""`:

```sql
-- Phase 3 migrations for tv_displays (idempotent on fresh DBs too) ------
-- Add the custom_dashboard_id column if missing.
ALTER TABLE tv_displays ADD COLUMN IF NOT EXISTS custom_dashboard_id INTEGER;
-- Add the FK separately so the IF NOT EXISTS protects column creation
-- without requiring custom_dashboards to exist first. DO block makes the
-- FK add idempotent across reboots.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'tv_displays_custom_dashboard_id_fkey'
  ) THEN
    ALTER TABLE tv_displays
      ADD CONSTRAINT tv_displays_custom_dashboard_id_fkey
      FOREIGN KEY (custom_dashboard_id) REFERENCES custom_dashboards(id)
      ON DELETE SET NULL;
  END IF;
END$$;
-- Extend kind CHECK to allow 'custom'.
ALTER TABLE tv_displays DROP CONSTRAINT IF EXISTS tv_displays_kind_check;
ALTER TABLE tv_displays ADD CONSTRAINT tv_displays_kind_check
  CHECK (kind IN ('vs_recycling', 'vs_new', 'wc', 'custom'));
```

- [ ] **Step 4: Run tests**

```
.venv/Scripts/python.exe -m pytest tests/test_db.py -v 2>&1 | tail -10
.venv/Scripts/python.exe -c "from zira_dashboard.app import app; print('OK')"
```

Expected: all PASS with `DATABASE_URL`, SKIP without. App OK.

- [ ] **Step 5: Commit**

```
git add src/zira_dashboard/db.py tests/test_db.py
git commit -m "$(cat <<'EOF'
schema(tv-displays): custom_dashboard_id + kind='custom'

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Store + routes — handle `kind='custom'`

**Files:**
- Modify: `src/zira_dashboard/tv_displays_store.py` — save / by_slug / list include `custom_dashboard_id`
- Modify: `src/zira_dashboard/routes/tv_displays.py` — `/tv/d/{slug}` branch + POST validation
- Test: `tests/test_tv_displays_store.py`
- Test: `tests/test_tv_displays_routes.py`

- [ ] **Step 1: Append failing tests to `tests/test_tv_displays_store.py`**

```python
def test_save_custom_kind_stores_dashboard_id():
    from zira_dashboard import tv_displays_store, custom_dashboards_store, db
    # Create a dashboard to reference.
    dash = custom_dashboards_store.save_dashboard(
        name="st-cust-dash", scope_kind="wc", scope_value="Repair 1", theme="dark",
    )
    row = tv_displays_store.save(
        name="st-cust-tv", kind="custom", wc_name=None,
        custom_dashboard_id=dash["id"], theme="dark",
    )
    assert row["kind"] == "custom"
    assert row["custom_dashboard_id"] == dash["id"]
    # by_slug returns it
    fetched = tv_displays_store.by_slug("st-cust-tv")
    assert fetched["custom_dashboard_id"] == dash["id"]
    # list_displays includes it
    rows = tv_displays_store.list_displays()
    target = next(r for r in rows if r["slug"] == "st-cust-tv")
    assert target["custom_dashboard_id"] == dash["id"]
    # cleanup
    db.execute("DELETE FROM tv_displays WHERE slug = 'st-cust-tv'")
    db.execute("DELETE FROM custom_dashboards WHERE slug = 'st-cust-dash'")
```

- [ ] **Step 2: Append failing tests to `tests/test_tv_displays_routes.py`**

```python
def test_post_add_custom_requires_dashboard_id():
    c = TestClient(app)
    r = c.post("/api/tv-displays", json={
        "name": "rt-cust-bad", "kind": "custom", "theme": "dark",
    })
    assert r.status_code == 400


def test_post_add_custom_returns_url():
    from zira_dashboard import custom_dashboards_store, db
    c = TestClient(app)
    dash = custom_dashboards_store.save_dashboard(
        name="rt-cust-dash", scope_kind="wc", scope_value="Repair 1", theme="dark",
    )
    r = c.post("/api/tv-displays", json={
        "name": "rt-cust-tv", "kind": "custom",
        "custom_dashboard_id": dash["id"], "theme": "dark",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["url"] == "/tv/d/rt-cust-tv"
    db.execute("DELETE FROM custom_dashboards WHERE slug = 'rt-cust-dash'")


def test_get_tv_d_custom_dispatches(monkeypatch):
    """/tv/d/{slug} where kind=custom renders the custom dashboard."""
    from zira_dashboard import custom_dashboards_store, db
    c = TestClient(app)
    dash = custom_dashboards_store.save_dashboard(
        name="rt-cust-render", scope_kind="wc", scope_value="Repair 1", theme="light",
    )
    c.post("/api/tv-displays", json={
        "name": "rt-cust-render-tv", "kind": "custom",
        "custom_dashboard_id": dash["id"], "theme": "light",
    })
    r = c.get("/tv/d/rt-cust-render-tv")
    assert r.status_code == 200
    assert 'data-tv-theme="light"' in r.text
    # The dashboard's name appears somewhere in the TV header / crumb
    assert "rt-cust-render" in r.text.lower() or "Repair 1" in r.text
    db.execute("DELETE FROM custom_dashboards WHERE slug = 'rt-cust-render'")


def test_get_tv_d_custom_returns_404_when_dashboard_deleted():
    """If custom_dashboard_id is NULL (dashboard was deleted), the route 404s."""
    from zira_dashboard import db, tv_displays_store
    c = TestClient(app)
    # Insert a row directly with NULL custom_dashboard_id (simulates the FK
    # ON DELETE SET NULL aftermath).
    db.execute(
        "INSERT INTO tv_displays (name, slug, kind, custom_dashboard_id, theme) "
        "VALUES ('rt-orphan', 'rt-orphan', 'custom', NULL, 'dark')"
    )
    r = c.get("/tv/d/rt-orphan")
    assert r.status_code == 404
    assert "dashboard" in r.text.lower() or "removed" in r.text.lower()
    db.execute("DELETE FROM tv_displays WHERE slug = 'rt-orphan'")
```

- [ ] **Step 3: Update `tv_displays_store.py`**

Open `src/zira_dashboard/tv_displays_store.py`. Three edits:

**3a — extend `save` signature + SQL:**

Find:

```python
def save(
    *,
    name: str,
    kind: str,
    wc_name: Optional[str],
    theme: str,
    id: Optional[int] = None,
) -> dict:
```

Replace with:

```python
def save(
    *,
    name: str,
    kind: str,
    wc_name: Optional[str],
    theme: str,
    custom_dashboard_id: Optional[int] = None,
    id: Optional[int] = None,
) -> dict:
```

Then in the same function, find:

```python
    if kind not in ("vs_recycling", "vs_new", "wc"):
        raise ValueError(f"invalid kind: {kind}")
```

Replace with:

```python
    if kind not in ("vs_recycling", "vs_new", "wc", "custom"):
        raise ValueError(f"invalid kind: {kind}")
```

Then replace the INSERT and UPDATE blocks. Find:

```python
    if id is None:
        rows = db.query(
            "INSERT INTO tv_displays (name, slug, kind, wc_name, theme) "
            "VALUES (%s, %s, %s, %s, %s) "
            "RETURNING id, name, slug, kind, wc_name, theme, sort_order",
            (name, slug, kind, wc_name, theme),
        )
    else:
        rows = db.query(
            "UPDATE tv_displays SET "
            "  name = %s, slug = %s, kind = %s, wc_name = %s, theme = %s, "
            "  updated_at = now() "
            "WHERE id = %s "
            "RETURNING id, name, slug, kind, wc_name, theme, sort_order",
            (name, slug, kind, wc_name, theme, id),
        )
    if not rows:
        raise LookupError(f"no tv_displays row with id={id}")
    r = rows[0]
    return {
        "id": int(r["id"]),
        "name": r["name"],
        "slug": r["slug"],
        "kind": r["kind"],
        "wc_name": r["wc_name"],
        "theme": r["theme"],
        "sort_order": int(r["sort_order"]),
    }
```

Replace with:

```python
    if id is None:
        rows = db.query(
            "INSERT INTO tv_displays (name, slug, kind, wc_name, custom_dashboard_id, theme) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "RETURNING id, name, slug, kind, wc_name, custom_dashboard_id, theme, sort_order",
            (name, slug, kind, wc_name, custom_dashboard_id, theme),
        )
    else:
        rows = db.query(
            "UPDATE tv_displays SET "
            "  name = %s, slug = %s, kind = %s, wc_name = %s, "
            "  custom_dashboard_id = %s, theme = %s, updated_at = now() "
            "WHERE id = %s "
            "RETURNING id, name, slug, kind, wc_name, custom_dashboard_id, theme, sort_order",
            (name, slug, kind, wc_name, custom_dashboard_id, theme, id),
        )
    if not rows:
        raise LookupError(f"no tv_displays row with id={id}")
    return _hydrate(rows[0])
```

**3b — add a `_hydrate` helper and replace both `by_slug` and `list_displays` to use it.** Add this helper at the end of the file:

```python
def _hydrate(row) -> dict:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "slug": row["slug"],
        "kind": row["kind"],
        "wc_name": row["wc_name"],
        "custom_dashboard_id": (
            int(row["custom_dashboard_id"]) if row.get("custom_dashboard_id") is not None else None
        ),
        "theme": row["theme"],
        "sort_order": int(row["sort_order"]),
    }
```

Then replace `by_slug` to use the helper:

Find:

```python
def by_slug(slug: str) -> Optional[dict]:
    from . import db
    rows = db.query(
        "SELECT id, name, slug, kind, wc_name, theme, sort_order "
        "FROM tv_displays WHERE slug = %s",
        (slug,),
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "id": int(r["id"]),
        "name": r["name"],
        "slug": r["slug"],
        "kind": r["kind"],
        "wc_name": r["wc_name"],
        "theme": r["theme"],
        "sort_order": int(r["sort_order"]),
    }
```

Replace with:

```python
def by_slug(slug: str) -> Optional[dict]:
    from . import db
    rows = db.query(
        "SELECT id, name, slug, kind, wc_name, custom_dashboard_id, theme, sort_order "
        "FROM tv_displays WHERE slug = %s",
        (slug,),
    )
    return _hydrate(rows[0]) if rows else None
```

Replace `list_displays`:

Find:

```python
def list_displays() -> list[dict]:
    """All rows ordered by (sort_order ASC, name ASC). Stable for UI."""
    from . import db
    rows = db.query(
        "SELECT id, name, slug, kind, wc_name, theme, sort_order "
        "FROM tv_displays ORDER BY sort_order ASC, lower(name) ASC"
    )
    return [
        {
            "id": int(r["id"]),
            "name": r["name"],
            "slug": r["slug"],
            "kind": r["kind"],
            "wc_name": r["wc_name"],
            "theme": r["theme"],
            "sort_order": int(r["sort_order"]),
        }
        for r in rows
    ]
```

Replace with:

```python
def list_displays() -> list[dict]:
    """All rows ordered by (sort_order ASC, name ASC). Stable for UI."""
    from . import db
    rows = db.query(
        "SELECT id, name, slug, kind, wc_name, custom_dashboard_id, theme, sort_order "
        "FROM tv_displays ORDER BY sort_order ASC, lower(name) ASC"
    )
    return [_hydrate(r) for r in rows]
```

- [ ] **Step 4: Update `routes/tv_displays.py`**

Open `src/zira_dashboard/routes/tv_displays.py`. Two edits.

**4a — add `kind='custom'` branch** in `tv_display` handler. Find:

```python
    if kind == "wc":
        from .. import staffing
        wc_name = row["wc_name"]
        valid = any(loc.name == wc_name for loc in staffing.LOCATIONS)
        if not valid:
            return HTMLResponse(
                _wc_removed_html(row["name"], wc_name),
                status_code=404,
            )
        from .wc_dashboard import _render_wc_dashboard
        return _render_wc_dashboard(
            request, slug=slug_for_wc(wc_name), tv_mode=True, tv_theme=tv_theme,
        )
    return JSONResponse(
        {"error": f"unknown kind: {kind}"}, status_code=500,
    )
```

Replace with:

```python
    if kind == "wc":
        from .. import staffing
        wc_name = row["wc_name"]
        valid = any(loc.name == wc_name for loc in staffing.LOCATIONS)
        if not valid:
            return HTMLResponse(
                _wc_removed_html(row["name"], wc_name),
                status_code=404,
            )
        from .wc_dashboard import _render_wc_dashboard
        return _render_wc_dashboard(
            request, slug=slug_for_wc(wc_name), tv_mode=True, tv_theme=tv_theme,
        )
    if kind == "custom":
        from .. import custom_dashboards_store
        dash_id = row.get("custom_dashboard_id")
        if dash_id is None:
            return HTMLResponse(
                _dashboard_removed_html(row["name"]),
                status_code=404,
            )
        dash = custom_dashboards_store.get_dashboard(int(dash_id))
        if dash is None:
            return HTMLResponse(
                _dashboard_removed_html(row["name"]),
                status_code=404,
            )
        from .custom_dashboards import _render_dashboard
        return _render_dashboard(
            request, slug=dash["slug"], tv_mode=True, tv_theme=tv_theme,
        )
    return JSONResponse(
        {"error": f"unknown kind: {kind}"}, status_code=500,
    )
```

**4b — add `_dashboard_removed_html`** below `_wc_removed_html`:

```python
def _dashboard_removed_html(display_name: str) -> str:
    return (
        f"<!doctype html><html><head><title>Dashboard removed</title>"
        f"<style>body{{font-family:system-ui;padding:3rem;text-align:center}}"
        f"a{{color:#16a34a}}</style></head><body>"
        f"<h1>Custom dashboard removed</h1>"
        f"<p>The display \"{display_name}\" was pointing at a custom dashboard that no longer exists.</p>"
        f"<p><a href=\"/settings?section=tvs\">Go to TVs settings</a></p>"
        f"</body></html>"
    )
```

**4c — extend `post_display` validation + save call.** Find:

```python
    if kind not in ("vs_recycling", "vs_new", "wc"):
        return JSONResponse({"ok": False, "error": "kind invalid"}, status_code=400)
    if kind == "wc":
        from .. import staffing
        if not isinstance(wc_name, str) or not wc_name.strip():
            return JSONResponse({"ok": False, "error": "wc_name required when kind=wc"}, status_code=400)
        if not any(loc.name == wc_name for loc in staffing.LOCATIONS):
            return JSONResponse({"ok": False, "error": f"unknown work center: {wc_name}"}, status_code=400)
    else:
        wc_name = None
    if theme not in ("light", "dark"):
        return JSONResponse({"ok": False, "error": "theme must be light or dark"}, status_code=400)
    saved = tv_displays_store.save(
        name=name.strip(), kind=kind, wc_name=wc_name, theme=theme,
        id=int(row_id) if row_id is not None else None,
    )
```

Replace with:

```python
    if kind not in ("vs_recycling", "vs_new", "wc", "custom"):
        return JSONResponse({"ok": False, "error": "kind invalid"}, status_code=400)
    custom_dashboard_id = None
    if kind == "wc":
        from .. import staffing
        if not isinstance(wc_name, str) or not wc_name.strip():
            return JSONResponse({"ok": False, "error": "wc_name required when kind=wc"}, status_code=400)
        if not any(loc.name == wc_name for loc in staffing.LOCATIONS):
            return JSONResponse({"ok": False, "error": f"unknown work center: {wc_name}"}, status_code=400)
    elif kind == "custom":
        from .. import custom_dashboards_store
        wc_name = None
        raw_id = body.get("custom_dashboard_id")
        if not isinstance(raw_id, int):
            return JSONResponse(
                {"ok": False, "error": "custom_dashboard_id required when kind=custom"},
                status_code=400,
            )
        if custom_dashboards_store.get_dashboard(raw_id) is None:
            return JSONResponse(
                {"ok": False, "error": f"unknown custom dashboard id: {raw_id}"},
                status_code=400,
            )
        custom_dashboard_id = raw_id
    else:
        wc_name = None
    if theme not in ("light", "dark"):
        return JSONResponse({"ok": False, "error": "theme must be light or dark"}, status_code=400)
    saved = tv_displays_store.save(
        name=name.strip(), kind=kind, wc_name=wc_name, theme=theme,
        custom_dashboard_id=custom_dashboard_id,
        id=int(row_id) if row_id is not None else None,
    )
```

- [ ] **Step 5: Run tests + verify**

```
.venv/Scripts/python.exe -m pytest tests/test_tv_displays_store.py tests/test_tv_displays_routes.py -v 2>&1 | tail -25
.venv/Scripts/python.exe -c "from zira_dashboard.app import app; print('app OK')"
.venv/Scripts/python.exe -m pytest 2>&1 | tail -3
```

Expected: SKIP without DB / PASS with DB. App OK. Full suite has 4 new tests, no new failures.

- [ ] **Step 6: Commit**

```
git add src/zira_dashboard/tv_displays_store.py src/zira_dashboard/routes/tv_displays.py tests/test_tv_displays_store.py tests/test_tv_displays_routes.py
git commit -m "$(cat <<'EOF'
feat(widgets-p3): tv_displays.kind='custom' — dispatch to custom dashboards

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: TVs settings UI — add custom kind option

**Files:**
- Modify: `src/zira_dashboard/routes/settings.py` — pass `custom_dashboards_rows` to template
- Modify: `src/zira_dashboard/templates/_settings_tvs.html` — add "Custom dashboard" kind option + cascading slug picker

- [ ] **Step 1: Update `routes/settings.py`**

Open `src/zira_dashboard/routes/settings.py`. Find the existing TVs section context block:

```python
    tv_displays_rows: list[dict] = []
    tv_templates_rows: list[dict] = []
    if section == "tvs":
        from .. import tv_displays_store, tv_templates_store
        tv_displays_rows = tv_displays_store.list_displays()
        tv_templates_rows = tv_templates_store.list_templates()
```

Replace with:

```python
    tv_displays_rows: list[dict] = []
    tv_templates_rows: list[dict] = []
    custom_dashboards_rows: list[dict] = []
    if section == "tvs":
        from .. import tv_displays_store, tv_templates_store, custom_dashboards_store
        tv_displays_rows = tv_displays_store.list_displays()
        tv_templates_rows = tv_templates_store.list_templates()
        custom_dashboards_rows = custom_dashboards_store.list_dashboards()
```

Then in the `return templates.TemplateResponse(...)` context dict, add `custom_dashboards_rows`:

Find:

```python
            "tv_displays_rows": tv_displays_rows,
            "tv_templates_rows": tv_templates_rows,
            "wc_locations_for_picker": [{"name": loc.name} for loc in staffing.LOCATIONS],
```

Replace with:

```python
            "tv_displays_rows": tv_displays_rows,
            "tv_templates_rows": tv_templates_rows,
            "custom_dashboards_rows": custom_dashboards_rows,
            "wc_locations_for_picker": [{"name": loc.name} for loc in staffing.LOCATIONS],
```

- [ ] **Step 2: Update `_settings_tvs.html`**

Open `src/zira_dashboard/templates/_settings_tvs.html`. Three edits.

**2a — add "Custom Dashboard" option to the table's per-row kind select:**

Find:

```jinja
            <select class="tv-kind-select">
              <option value="vs_recycling" {% if d.kind == 'vs_recycling' %}selected{% endif %}>Recycling VS</option>
              <option value="vs_new" {% if d.kind == 'vs_new' %}selected{% endif %}>New VS</option>
              <option value="wc" {% if d.kind == 'wc' %}selected{% endif %}>Work Center</option>
            </select>
            <select class="tv-wc-select" {% if d.kind != 'wc' %}style="display:none"{% endif %}>
              {% for loc in wc_locations_for_picker %}
                <option value="{{ loc.name }}" {% if d.wc_name == loc.name %}selected{% endif %}>{{ loc.name }}</option>
              {% endfor %}
            </select>
```

Replace with:

```jinja
            <select class="tv-kind-select">
              <option value="vs_recycling" {% if d.kind == 'vs_recycling' %}selected{% endif %}>Recycling VS</option>
              <option value="vs_new" {% if d.kind == 'vs_new' %}selected{% endif %}>New VS</option>
              <option value="wc" {% if d.kind == 'wc' %}selected{% endif %}>Work Center</option>
              <option value="custom" {% if d.kind == 'custom' %}selected{% endif %}>Custom Dashboard</option>
            </select>
            <select class="tv-wc-select" {% if d.kind != 'wc' %}style="display:none"{% endif %}>
              {% for loc in wc_locations_for_picker %}
                <option value="{{ loc.name }}" {% if d.wc_name == loc.name %}selected{% endif %}>{{ loc.name }}</option>
              {% endfor %}
            </select>
            <select class="tv-custom-select" {% if d.kind != 'custom' %}style="display:none"{% endif %}>
              {% for cd in custom_dashboards_rows %}
                <option value="{{ cd.id }}" {% if d.custom_dashboard_id == cd.id %}selected{% endif %}>{{ cd.name }}</option>
              {% endfor %}
            </select>
```

**2b — add the Custom Dashboard option to the "Add display" form:**

Find:

```jinja
  <div class="tv-add-form">
    <input type="text" id="tv-add-name" placeholder="Display name (e.g. Repair 1 — Wall TV)">
    <select id="tv-add-kind">
      <option value="vs_recycling">Recycling VS</option>
      <option value="vs_new">New VS</option>
      <option value="wc" selected>Work Center</option>
    </select>
    <select id="tv-add-wc">
      {% for loc in wc_locations_for_picker %}
        <option value="{{ loc.name }}">{{ loc.name }}</option>
      {% endfor %}
    </select>
    <select id="tv-add-theme">
```

Replace with:

```jinja
  <div class="tv-add-form">
    <input type="text" id="tv-add-name" placeholder="Display name (e.g. Repair 1 — Wall TV)">
    <select id="tv-add-kind">
      <option value="vs_recycling">Recycling VS</option>
      <option value="vs_new">New VS</option>
      <option value="wc" selected>Work Center</option>
      <option value="custom">Custom Dashboard</option>
    </select>
    <select id="tv-add-wc">
      {% for loc in wc_locations_for_picker %}
        <option value="{{ loc.name }}">{{ loc.name }}</option>
      {% endfor %}
    </select>
    <select id="tv-add-custom" style="display:none">
      {% for cd in custom_dashboards_rows %}
        <option value="{{ cd.id }}">{{ cd.name }}</option>
      {% endfor %}
    </select>
    <select id="tv-add-theme">
```

**2c — update the JS to handle the new select.** Find:

```javascript
  // --- Inline-row edits (name change, kind change, wc_name change) ---
  function saveRow(tr) {
    const id = tr.dataset.id;
    const name = tr.querySelector('.tv-name-input').value.trim();
    const kind = tr.querySelector('.tv-kind-select').value;
    const wcSel = tr.querySelector('.tv-wc-select');
    const wc_name = kind === 'wc' ? wcSel.value : null;
    const themeBtn = tr.querySelector('.tv-theme-btn.active');
    const theme = themeBtn ? themeBtn.dataset.theme : 'dark';
    return postJson('/api/tv-displays', {
      id: parseInt(id, 10), name, kind, wc_name, theme,
    }).then(data => {
```

Replace with:

```javascript
  // --- Inline-row edits (name change, kind change, wc_name change) ---
  function saveRow(tr) {
    const id = tr.dataset.id;
    const name = tr.querySelector('.tv-name-input').value.trim();
    const kind = tr.querySelector('.tv-kind-select').value;
    const wcSel = tr.querySelector('.tv-wc-select');
    const customSel = tr.querySelector('.tv-custom-select');
    const wc_name = kind === 'wc' ? wcSel.value : null;
    const custom_dashboard_id = kind === 'custom' ? parseInt(customSel.value, 10) : null;
    const themeBtn = tr.querySelector('.tv-theme-btn.active');
    const theme = themeBtn ? themeBtn.dataset.theme : 'dark';
    return postJson('/api/tv-displays', {
      id: parseInt(id, 10), name, kind, wc_name, custom_dashboard_id, theme,
    }).then(data => {
```

Find the per-row kind change handler:

```javascript
    tr.querySelector('.tv-kind-select').addEventListener('change', (e) => {
      const wcSel = tr.querySelector('.tv-wc-select');
      wcSel.style.display = e.target.value === 'wc' ? '' : 'none';
      saveRow(tr);
    });
    tr.querySelector('.tv-wc-select').addEventListener('change', () => saveRow(tr));
```

Replace with:

```javascript
    tr.querySelector('.tv-kind-select').addEventListener('change', (e) => {
      const wcSel = tr.querySelector('.tv-wc-select');
      const customSel = tr.querySelector('.tv-custom-select');
      wcSel.style.display = e.target.value === 'wc' ? '' : 'none';
      customSel.style.display = e.target.value === 'custom' ? '' : 'none';
      saveRow(tr);
    });
    tr.querySelector('.tv-wc-select').addEventListener('change', () => saveRow(tr));
    tr.querySelector('.tv-custom-select').addEventListener('change', () => saveRow(tr));
```

Find the add-form kind change handler:

```javascript
  const addKindSel = document.getElementById('tv-add-kind');
  const addWcSel = document.getElementById('tv-add-wc');
  if (addKindSel) {
    addKindSel.addEventListener('change', (e) => {
      addWcSel.style.display = e.target.value === 'wc' ? '' : 'none';
    });
  }
```

Replace with:

```javascript
  const addKindSel = document.getElementById('tv-add-kind');
  const addWcSel = document.getElementById('tv-add-wc');
  const addCustomSel = document.getElementById('tv-add-custom');
  if (addKindSel) {
    addKindSel.addEventListener('change', (e) => {
      addWcSel.style.display = e.target.value === 'wc' ? '' : 'none';
      addCustomSel.style.display = e.target.value === 'custom' ? '' : 'none';
    });
  }
```

And in the add-button click handler, find:

```javascript
      const name = document.getElementById('tv-add-name').value.trim();
      const kind = addKindSel.value;
      const wc_name = kind === 'wc' ? addWcSel.value : null;
      const theme = document.getElementById('tv-add-theme').value;
      if (!name) { showStatus('tv-add-status', 'name required'); return; }
      showStatus('tv-add-status', 'Adding…');
      postJson('/api/tv-displays', {name, kind, wc_name, theme}).then(data => {
```

Replace with:

```javascript
      const name = document.getElementById('tv-add-name').value.trim();
      const kind = addKindSel.value;
      const wc_name = kind === 'wc' ? addWcSel.value : null;
      const custom_dashboard_id = kind === 'custom' ? parseInt(addCustomSel.value, 10) : null;
      const theme = document.getElementById('tv-add-theme').value;
      if (!name) { showStatus('tv-add-status', 'name required'); return; }
      showStatus('tv-add-status', 'Adding…');
      postJson('/api/tv-displays', {name, kind, wc_name, custom_dashboard_id, theme}).then(data => {
```

- [ ] **Step 3: Verify parse + app**

```
.venv/Scripts/python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'), autoescape=True); env.parse(open('src/zira_dashboard/templates/_settings_tvs.html', encoding='utf-8').read()); env.parse(open('src/zira_dashboard/templates/settings.html', encoding='utf-8').read()); print('parse OK')"
.venv/Scripts/python.exe -c "from zira_dashboard.app import app; print('app OK')"
.venv/Scripts/python.exe -m pytest 2>&1 | tail -3
```

Expected: parse OK, app OK, suite green.

- [ ] **Step 4: Commit**

```
git add src/zira_dashboard/routes/settings.py src/zira_dashboard/templates/_settings_tvs.html
git commit -m "$(cat <<'EOF'
feat(widgets-p3): TVs settings panel — Custom Dashboard kind option

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Per-placement edit popover on the dashboard editor

**Files:**
- Modify: `src/zira_dashboard/templates/custom_dashboard.html`

A small "⋮" button on each widget in editor mode opens a popover with the type's `data_params_schema` rendered as form fields, pre-filled with the placement's `effective_data`. Save → PATCH `/api/placements/{id}` with new `data_overrides`. Cancel closes the popover.

Schema for fields comes from `/api/widgets/types` (already exposed in Phase 1). For dropdown options, `/api/widgets/options/{kind}` (also Phase 1).

- [ ] **Step 1: Add per-placement edit button overlay to the widget loop**

Open `src/zira_dashboard/templates/custom_dashboard.html`. Find the widget loop:

```jinja
<main>
<div class="grid-stack">
  {% for p in placements %}
    <div class="grid-stack-item"
         gs-id="{{ p.id }}" gs-x="{{ p.x }}" gs-y="{{ p.y }}" gs-w="{{ p.w }}" gs-h="{{ p.h }}">
      {% with placement = p, data = p.data %}
        {% include "_widget_render.html" %}
      {% endwith %}
    </div>
  {% endfor %}
</div>
</main>
```

Replace with:

```jinja
<main>
<div class="grid-stack">
  {% for p in placements %}
    <div class="grid-stack-item"
         gs-id="{{ p.id }}" gs-x="{{ p.x }}" gs-y="{{ p.y }}" gs-w="{{ p.w }}" gs-h="{{ p.h }}">
      <div class="placement-wrap" data-placement-id="{{ p.id }}"
           data-type="{{ p.type }}"
           data-effective='{{ p.effective_data | tojson }}'
           data-overrides='{{ p.data_overrides | tojson }}'>
        {% if not tv_mode %}
          <button type="button" class="placement-edit-btn" title="Edit data scope">⋮</button>
          <button type="button" class="placement-delete-btn" title="Remove from dashboard">×</button>
        {% endif %}
        {% with placement = p, data = p.data %}
          {% include "_widget_render.html" %}
        {% endwith %}
      </div>
    </div>
  {% endfor %}
</div>
</main>

{% if not tv_mode %}
<div id="placement-edit-popover" hidden>
  <div class="popover-card">
    <h3 id="ppop-title">Edit widget data</h3>
    <div id="ppop-fields"></div>
    <div class="popover-actions">
      <button type="button" id="ppop-cancel">Cancel</button>
      <button type="button" id="ppop-save" class="primary">Save</button>
    </div>
  </div>
</div>
{% endif %}
```

- [ ] **Step 2: Add CSS for the overlay + popover**

In the same file, find the existing `<style>` block (just before `</head>`). Append these rules inside the `<style>...</style>` block, right before the closing `</style>`:

```css
  .placement-wrap { position: relative; height: 100%; }
  .placement-edit-btn, .placement-delete-btn {
    position: absolute; top: 4px; z-index: 5;
    width: 20px; height: 20px; line-height: 16px;
    background: rgba(255,255,255,0.9); border: 1px solid var(--border, #d8dee5);
    border-radius: 999px; font-size: 13px; cursor: pointer; padding: 0;
    display: grid; place-items: center; color: var(--muted, #6b7280);
  }
  html[data-tv-theme="dark"] .placement-edit-btn,
  html[data-tv-theme="dark"] .placement-delete-btn {
    background: rgba(30,41,59,0.9); color: var(--fg, #e2e8f0);
  }
  .placement-edit-btn { right: 28px; }
  .placement-delete-btn { right: 4px; color: #ef4444; }
  .placement-edit-btn:hover, .placement-delete-btn:hover { background: white; }
  #placement-edit-popover {
    position: fixed; inset: 0; background: rgba(0,0,0,0.4);
    display: grid; place-items: center; z-index: 1000;
  }
  #placement-edit-popover[hidden] { display: none; }
  #placement-edit-popover .popover-card {
    background: var(--panel, white); border: 1px solid var(--border, #d8dee5);
    border-radius: 12px; padding: 1rem 1.2rem;
    min-width: 360px; max-width: 480px;
    box-shadow: 0 12px 32px rgba(0,0,0,0.25);
  }
  #placement-edit-popover h3 { margin: 0 0 0.6rem; font-size: 0.95rem; font-weight: 600; }
  #placement-edit-popover label { display: block; margin-bottom: 0.5rem; font-size: 0.85rem; color: var(--muted, #6b7280); }
  #placement-edit-popover input, #placement-edit-popover select {
    background: var(--panel-2, #f1f4f7); color: var(--fg, #1f2937);
    border: 1px solid var(--border, #d8dee5); border-radius: 6px;
    padding: 0.3rem 0.5rem; font: inherit; font-size: 0.9rem; width: 100%;
  }
  #placement-edit-popover .popover-actions { display: flex; gap: 0.5rem; justify-content: flex-end; margin-top: 0.75rem; }
  #placement-edit-popover button {
    background: var(--panel-2, #f1f4f7); color: var(--fg, #1f2937);
    border: 1px solid var(--border, #d8dee5); border-radius: 6px;
    padding: 0.4rem 1rem; font-weight: 600; cursor: pointer;
  }
  #placement-edit-popover button.primary { background: var(--accent, #16a34a); color: white; border-color: var(--accent, #16a34a); }
```

- [ ] **Step 3: Add JS handler at the bottom of the existing `<script>` block**

In the same file, find the `<script>` block. Inside the `{% if not tv_mode %}` ... `{% endif %}` block (after the existing `.pal-add-btn` handler), append:

```javascript
  // ---- Per-placement edit popover ----
  let TYPES_CACHE = null;
  function loadTypes() {
    if (TYPES_CACHE) return Promise.resolve(TYPES_CACHE);
    return fetch('/api/widgets/types').then(r => r.json()).then(d => {
      TYPES_CACHE = d.types || [];
      return TYPES_CACHE;
    });
  }

  function ppopFieldEl(field, currentValue) {
    const label = document.createElement('label');
    label.textContent = field.label;
    let input;
    if (field.input === 'select') {
      input = document.createElement('select');
      if (field.options) {
        const blank = document.createElement('option');
        blank.value = ''; blank.textContent = '— pick one —';
        input.appendChild(blank);
        for (const o of field.options) {
          const opt = document.createElement('option');
          opt.value = o.value; opt.textContent = o.label;
          if (String(currentValue) === String(o.value)) opt.selected = true;
          input.appendChild(opt);
        }
      }
      if (field.options_from) {
        const blank = document.createElement('option');
        blank.value = ''; blank.textContent = 'loading…';
        input.appendChild(blank);
        fetch('/api/widgets/options/' + field.options_from).then(r => r.json()).then(data => {
          input.innerHTML = '';
          const b = document.createElement('option');
          b.value = ''; b.textContent = '— pick one —';
          input.appendChild(b);
          for (const o of (data.options || [])) {
            const opt = document.createElement('option');
            opt.value = o.value; opt.textContent = o.label;
            if (String(currentValue) === String(o.value)) opt.selected = true;
            input.appendChild(opt);
          }
        });
      }
    } else if (field.input === 'color') {
      input = document.createElement('input');
      input.type = 'color';
      input.value = currentValue || field.default || '#22c55e';
    } else {
      input = document.createElement('input');
      input.type = 'text';
      if (currentValue !== undefined && currentValue !== null) input.value = currentValue;
    }
    input.dataset.key = field.key;
    label.appendChild(input);
    return label;
  }

  function openPlacementPopover(wrap) {
    const placementId = wrap.dataset.placementId;
    const type = wrap.dataset.type;
    const effective = JSON.parse(wrap.dataset.effective || '{}');
    loadTypes().then(types => {
      const def = types.find(t => t.type === type);
      const fieldsHost = document.getElementById('ppop-fields');
      fieldsHost.innerHTML = '';
      if (!def) {
        fieldsHost.textContent = 'Unknown widget type: ' + type;
      } else {
        for (const f of def.data_params_schema) {
          fieldsHost.appendChild(ppopFieldEl(f, effective[f.key]));
        }
      }
      document.getElementById('ppop-title').textContent =
        'Edit data scope · ' + (def ? def.label : type);
      const pop = document.getElementById('placement-edit-popover');
      pop.dataset.placementId = placementId;
      pop.hidden = false;
    });
  }

  document.querySelectorAll('.placement-edit-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      openPlacementPopover(btn.closest('.placement-wrap'));
    });
  });

  document.querySelectorAll('.placement-delete-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const wrap = btn.closest('.placement-wrap');
      if (!confirm('Remove this widget from the dashboard?')) return;
      fetch('/api/placements/' + wrap.dataset.placementId, {method: 'DELETE'})
        .then(r => r.json()).then(d => {
          if (d.ok) location.reload();
        });
    });
  });

  document.getElementById('ppop-cancel').addEventListener('click', () => {
    document.getElementById('placement-edit-popover').hidden = true;
  });

  document.getElementById('ppop-save').addEventListener('click', () => {
    const pop = document.getElementById('placement-edit-popover');
    const placementId = pop.dataset.placementId;
    const fieldsHost = document.getElementById('ppop-fields');
    const overrides = {};
    fieldsHost.querySelectorAll('input, select').forEach(el => {
      const v = el.value;
      if (v === '' || v === null || v === undefined) return;
      overrides[el.dataset.key] = v;
    });
    fetch('/api/placements/' + placementId, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({data_overrides: overrides}),
    }).then(r => r.json()).then(d => {
      if (d.ok) {
        pop.hidden = true;
        location.reload();
      }
    });
  });

  // Click outside the card closes the popover.
  document.getElementById('placement-edit-popover').addEventListener('click', (e) => {
    if (e.target.id === 'placement-edit-popover') {
      e.currentTarget.hidden = true;
    }
  });
```

- [ ] **Step 4: Verify parse + app**

```
.venv/Scripts/python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'), autoescape=True); env.parse(open('src/zira_dashboard/templates/custom_dashboard.html', encoding='utf-8').read()); print('parse OK')"
.venv/Scripts/python.exe -c "from zira_dashboard.app import app; print('app OK')"
.venv/Scripts/python.exe -m pytest 2>&1 | tail -3
```

Expected: parse OK, app OK, no test regressions.

- [ ] **Step 5: Commit**

```
git add src/zira_dashboard/templates/custom_dashboard.html
git commit -m "$(cat <<'EOF'
feat(widgets-p3): per-placement edit popover on dashboard editor

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Workshop "in use" badge + CHANGELOG + push

**Files:**
- Modify: `src/zira_dashboard/widget_definitions_store.py` — `list_definitions` returns `usage_count`
- Modify: `src/zira_dashboard/templates/widgets.html` — show "in use by N" next to each preset; disable Delete when > 0
- Modify: `tests/test_widget_definitions_store.py` — assert usage_count in list output
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Append failing test to `tests/test_widget_definitions_store.py`**

```python
def test_list_definitions_includes_usage_count():
    from zira_dashboard import widget_definitions_store, custom_dashboards_store, db
    wd = widget_definitions_store.save(
        name="wt-usagelist", type="ribbons", visual={}, default_data={"group": "Repairs"},
    )
    rows = [r for r in widget_definitions_store.list_definitions() if r["name"] == "wt-usagelist"]
    assert rows[0]["usage_count"] == 0
    dash = custom_dashboards_store.save_dashboard(
        name="wt-usagelist-dash", scope_kind="group", scope_value="Repairs", theme="dark",
    )
    custom_dashboards_store.add_placement(
        dashboard_id=dash["id"], widget_def_id=wd["id"], x=0, y=0, w=4, h=4, data_overrides={},
    )
    rows = [r for r in widget_definitions_store.list_definitions() if r["name"] == "wt-usagelist"]
    assert rows[0]["usage_count"] == 1
    db.execute("DELETE FROM custom_dashboards WHERE slug LIKE 'wt-%'")
```

- [ ] **Step 2: Update `list_definitions` to include usage_count**

Open `src/zira_dashboard/widget_definitions_store.py`. Find:

```python
def list_definitions() -> list[dict]:
    from . import db
    rows = db.query(
        "SELECT id, name, type, visual_json, default_data_json "
        "FROM widget_definitions ORDER BY type, lower(name)"
    )
    return [_hydrate(r) for r in rows]
```

Replace with:

```python
def list_definitions() -> list[dict]:
    """All definitions with `usage_count` precomputed via subquery."""
    from . import db
    rows = db.query(
        "SELECT wd.id, wd.name, wd.type, wd.visual_json, wd.default_data_json, "
        "  COALESCE(c.n, 0) AS usage_count "
        "FROM widget_definitions wd "
        "LEFT JOIN ("
        "  SELECT widget_def_id, COUNT(*) AS n "
        "  FROM dashboard_widgets GROUP BY widget_def_id"
        ") c ON c.widget_def_id = wd.id "
        "ORDER BY wd.type, lower(wd.name)"
    )
    out = []
    for r in rows:
        d = _hydrate(r)
        d["usage_count"] = int(r["usage_count"])
        out.append(d)
    return out
```

- [ ] **Step 3: Update `widgets.html` to surface usage**

Open `src/zira_dashboard/templates/widgets.html`. Find:

```jinja
      {% for d in definitions %}
        <div class="def-row" data-id="{{ d.id }}">
          <div><span class="name">{{ d.name }}</span> <span class="type">{{ d.type }}</span></div>
          <button type="button" class="edit-btn">Edit</button>
          <button type="button" class="danger delete-btn">Delete</button>
        </div>
      {% else %}
```

Replace with:

```jinja
      {% for d in definitions %}
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
      {% else %}
```

Then in the same file's `<style>` block, find:

```css
  .def-row .type { font-size: 0.75rem; color: var(--muted); font-variant: small-caps; }
```

Replace with:

```css
  .def-row .type { font-size: 0.75rem; color: var(--muted); font-variant: small-caps; }
  .def-row .usage { font-size: 0.75rem; color: var(--muted); margin-left: 0.4rem; }
  .def-row button[disabled] { opacity: 0.4; cursor: not-allowed; }
```

- [ ] **Step 4: Run full suite + verify parse**

```
.venv/Scripts/python.exe -m pytest tests/test_widget_definitions_store.py -v 2>&1 | tail -10
.venv/Scripts/python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'), autoescape=True); env.parse(open('src/zira_dashboard/templates/widgets.html', encoding='utf-8').read()); print('parse OK')"
.venv/Scripts/python.exe -m pytest 2>&1 | tail -3
```

Expected: tests SKIP without DB or PASS. Parse OK. Full suite no new failures.

- [ ] **Step 5: Get the current time**

```
powershell.exe -Command "Get-Date -Format 'h:mm tt'"
```

- [ ] **Step 6: Add CHANGELOG entry**

In `CHANGELOG.md`, insert at top of today's `## 2026-05-13` section:

```markdown
### <HH:MM TT>

- **Widget Workshop Phase 3 — closeout** — three polish items finish off the workshop spec. (1) **Custom dashboards can be added as TVs**: the Settings → TVs panel gains a "Custom Dashboard" kind with a cascading picker; the resulting `/tv/d/{slug}` URL renders the chosen custom dashboard with the row's saved theme. Deleting a custom dashboard nulls out any TV displays that referenced it (FK ON DELETE SET NULL) and shows a "dashboard removed" page when visited. (2) **Per-placement edit popover** on the dashboard editor — a small ⋮ button on each widget opens a schema-driven form to override that placement's data scope; ✕ deletes the widget from the dashboard without a reload of the workshop. (3) **"In use by N" badge** in the Widget Workshop list, with the Delete button disabled for widgets referenced by any dashboard so it's obvious why a delete would fail. The widget-workshop master spec (sub-project 5) is now fully shipped: Workshop + custom dashboards + 8 widget types + TVs integration + per-placement overrides.
```

- [ ] **Step 7: Commit + push**

```
git add src/zira_dashboard/widget_definitions_store.py src/zira_dashboard/templates/widgets.html tests/test_widget_definitions_store.py CHANGELOG.md
git commit -m "$(cat <<'EOF'
feat(widgets-p3): in-use badge on workshop + Phase 3 changelog

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin main
```

Railway picks up the push and redeploys. After deploy:
1. Visit `/dashboards`, create one and place a widget.
2. Visit `/settings?section=tvs`. Use the Add form: name = "Repair TV", kind = Custom Dashboard, pick your dashboard, theme = dark. Save → row appears.
3. Hit the copied URL — renders the custom dashboard in TV mode with the dashboard's scope name + operators in the header.
4. Back on `/dashboards/{slug}`, click ⋮ on a widget — popover lets you change its data scope without going through the workshop.
5. On `/widgets`, the in-use-by-N badge shows next to any widget that's placed somewhere; Delete is disabled until placements are removed.

---

## Done

Sub-project 5 (Widget Workshop & Custom Dashboards) is complete. The full master spec — workshop + custom dashboards + 8 widget types + TVs integration + per-placement overrides — has shipped across three plans / three deploys.

If a future enhancement is needed (more widget types, drag-from-palette, custom KPI metrics, etc.), each is a small additive change against the established pattern.
