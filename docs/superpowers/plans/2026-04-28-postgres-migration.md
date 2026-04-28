# Postgres Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all app state off JSON files into Railway Postgres so saves persist across deploys, the dashboard reads sub-1ms locally, and the schema is "sync-ready" for future two-way Odoo writes via the outbox pattern.

**Architecture:** psycopg2 + raw SQL. Single `db.py` module owns the pool and helpers. Schema bootstraps on app startup via `CREATE TABLE IF NOT EXISTS`. Each existing store module gets a focused rewrite that swaps JSON I/O for SQL while keeping its public API intact. One-shot migration script populates Postgres from current JSON files, then JSON files become irrelevant.

**Tech Stack:** Python 3.12 / FastAPI / psycopg2-binary / pytest. Existing Odoo client unchanged.

**Dependencies:** Spec at `docs/superpowers/specs/2026-04-28-postgres-migration-design.md`. Railway Postgres add-on must be live with `DATABASE_URL` referenced into the web service's env. Verified at plan start.

---

## File Structure

- New: `src/zira_dashboard/db.py` — pool, cursor context, `query`, `execute`, `bootstrap_schema`
- New: `scripts/migrate_json_to_postgres.py` — one-shot data migration
- New: `tests/test_db.py` — pool, schema, transaction wrapper, error paths
- New: `tests/test_postgres_stores.py` — round-trip tests for each store against a live Postgres test schema (uses `DATABASE_URL` if set, else skipped)
- Modified: `src/zira_dashboard/app.py` — startup hook: `init_pool()` then `bootstrap_schema()`
- Modified: `src/zira_dashboard/staffing.py` — `load_roster` / `save_roster` / `load_schedule` / `save_schedule` use SQL
- Modified: `src/zira_dashboard/work_centers_store.py` — full SQL rewrite
- Modified: `src/zira_dashboard/settings_store.py` — `app_settings` rows
- Modified: `src/zira_dashboard/schedule_store.py` — `global_schedule` singleton
- Modified: `src/zira_dashboard/layout_store.py` — `widget_layouts` rows
- Modified: `src/zira_dashboard/widget_customizer.py` — `widget_customizations` rows
- Modified: `src/zira_dashboard/skill_filter_store.py` — `app_settings['skill_filter']`
- Modified: `src/zira_dashboard/odoo_sync.py` — writes to `people`, `skills`, `person_skills`, `app_settings['odoo_last_sync']`
- Modified: `requirements.txt` — add `psycopg2-binary`
- Modified: `docs/odoo-setup.md` — add Postgres add-on setup notes

---

### Task 1: DB foundation — pool, cursor, schema bootstrap

**Files:**
- Create: `src/zira_dashboard/db.py`
- Modify: `requirements.txt`
- Test: `tests/test_db.py`

- [ ] **Step 1: Add `psycopg2-binary` to `requirements.txt`**

Append the line:
```
psycopg2-binary>=2.9.9
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_db.py`:

```python
import os
import pytest

from zira_dashboard import db


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="No DATABASE_URL — Postgres tests need a live database",
)


@pytest.fixture(autouse=True)
def reset_pool():
    db.shutdown_pool()
    yield
    db.shutdown_pool()


def test_init_pool_idempotent():
    db.init_pool()
    db.init_pool()  # second call no-ops, doesn't error


def test_query_and_execute_round_trip():
    db.init_pool()
    db.execute("CREATE TEMP TABLE _t (id INT, name TEXT)")
    db.execute("INSERT INTO _t VALUES (1, 'alpha'), (2, 'beta')")
    rows = db.query("SELECT id, name FROM _t ORDER BY id")
    assert rows == [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}]


def test_cursor_rolls_back_on_exception():
    db.init_pool()
    db.execute("CREATE TEMP TABLE _t (id INT)")
    with pytest.raises(RuntimeError, match="boom"):
        with db.cursor() as cur:
            cur.execute("INSERT INTO _t VALUES (1)")
            raise RuntimeError("boom")
    rows = db.query("SELECT id FROM _t")
    assert rows == []


def test_bootstrap_schema_idempotent():
    db.init_pool()
    db.bootstrap_schema()
    db.bootstrap_schema()  # idempotent; no error
    rows = db.query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name IN "
        "('people', 'skills', 'person_skills', 'work_centers', "
        "'schedules', 'app_settings', 'sync_outbox')"
    )
    names = {r["table_name"] for r in rows}
    for expected in ("people", "skills", "person_skills", "work_centers",
                     "schedules", "app_settings", "sync_outbox"):
        assert expected in names, f"missing {expected}"
```

- [ ] **Step 3: Run, verify FAIL**

`.venv/Scripts/python.exe -m pytest tests/test_db.py -v` — expected ImportError (db module doesn't exist yet).

- [ ] **Step 4: Implement `db.py`**

Create `src/zira_dashboard/db.py`:

```python
"""Postgres connection pool + small SQL helpers + schema bootstrap.

Read DATABASE_URL from the environment (Railway auto-injects it for
services that reference the Postgres add-on). The pool is module-level
so handlers reuse connections across requests.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool


_pool: ThreadedConnectionPool | None = None


def init_pool(minconn: int = 1, maxconn: int = 8) -> None:
    """Create the global connection pool. Idempotent."""
    global _pool
    if _pool is not None:
        return
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Add the Postgres add-on on Railway "
            "and reference it into the web service's variables."
        )
    _pool = ThreadedConnectionPool(minconn, maxconn, dsn)


def shutdown_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


@contextmanager
def cursor() -> Iterator[psycopg2.extensions.cursor]:
    """Yield a cursor inside a transaction. Commits on clean exit, rolls
    back on exception, returns the connection to the pool either way."""
    if _pool is None:
        init_pool()
    assert _pool is not None
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        _pool.putconn(conn)


def query(sql: str, params: tuple | dict = ()) -> list[dict]:
    """Read helper. Returns rows as dicts (column name → value)."""
    with cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def execute(sql: str, params: tuple | dict = ()) -> None:
    """Write helper. Runs in its own short transaction."""
    with cursor() as cur:
        cur.execute(sql, params)


def execute_many(sql: str, rows: list[tuple]) -> None:
    if not rows:
        return
    with cursor() as cur:
        cur.executemany(sql, rows)


# --- Schema --------------------------------------------------------------

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS people (
  id            SERIAL PRIMARY KEY,
  odoo_id       INTEGER UNIQUE,
  name          TEXT NOT NULL UNIQUE,
  active        BOOLEAN NOT NULL DEFAULT TRUE,
  reserve       BOOLEAN NOT NULL DEFAULT FALSE,
  last_pulled_at  TIMESTAMPTZ,
  last_pushed_at  TIMESTAMPTZ,
  local_dirty   BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS people_active_idx ON people(active);

CREATE TABLE IF NOT EXISTS skills (
  id            SERIAL PRIMARY KEY,
  odoo_id       INTEGER UNIQUE,
  name          TEXT NOT NULL UNIQUE,
  skill_type    TEXT NOT NULL,
  sort_order    INTEGER NOT NULL DEFAULT 0,
  last_pulled_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS person_skills (
  person_id     INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  skill_id      INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  level         SMALLINT NOT NULL DEFAULT 0,
  last_pulled_at  TIMESTAMPTZ,
  last_pushed_at  TIMESTAMPTZ,
  local_dirty   BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY (person_id, skill_id)
);

CREATE TABLE IF NOT EXISTS work_centers (
  id            SERIAL PRIMARY KEY,
  odoo_id       INTEGER UNIQUE,
  name          TEXT NOT NULL UNIQUE,
  meter_id      TEXT,
  category      TEXT NOT NULL,
  cell          TEXT,
  value_stream  TEXT,
  min_ops       INTEGER NOT NULL DEFAULT 1,
  max_ops       INTEGER,
  goal_per_day_override INTEGER,
  group_name    TEXT,
  note          TEXT,
  last_pulled_at  TIMESTAMPTZ,
  last_pushed_at  TIMESTAMPTZ,
  local_dirty   BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS work_center_required_skills (
  wc_id     INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE CASCADE,
  skill_id  INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  PRIMARY KEY (wc_id, skill_id)
);

CREATE TABLE IF NOT EXISTS work_center_default_people (
  wc_id     INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  sort_order INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (wc_id, person_id)
);

CREATE TABLE IF NOT EXISTS groups (
  name      TEXT PRIMARY KEY,
  goal_per_day_override INTEGER
);

CREATE TABLE IF NOT EXISTS value_streams (
  name      TEXT PRIMARY KEY,
  goal_per_day_override INTEGER
);

CREATE TABLE IF NOT EXISTS schedules (
  day                 DATE PRIMARY KEY,
  published           BOOLEAN NOT NULL DEFAULT FALSE,
  testing_day         BOOLEAN NOT NULL DEFAULT FALSE,
  notes               TEXT NOT NULL DEFAULT '',
  custom_hours        JSONB,
  published_snapshot  JSONB,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schedule_assignments (
  day         DATE NOT NULL REFERENCES schedules(day) ON DELETE CASCADE,
  wc_id       INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE CASCADE,
  person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  sort_order  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, wc_id, person_id)
);
CREATE INDEX IF NOT EXISTS schedule_assignments_day_idx ON schedule_assignments(day);

CREATE TABLE IF NOT EXISTS schedule_time_off (
  day       DATE NOT NULL REFERENCES schedules(day) ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  PRIMARY KEY (day, person_id)
);

CREATE TABLE IF NOT EXISTS schedule_wc_notes (
  day       DATE NOT NULL REFERENCES schedules(day) ON DELETE CASCADE,
  wc_id     INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE CASCADE,
  note      TEXT NOT NULL,
  PRIMARY KEY (day, wc_id)
);

CREATE TABLE IF NOT EXISTS global_schedule (
  id              INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  shift_start     TIME NOT NULL,
  shift_end       TIME NOT NULL,
  work_weekdays   INTEGER[] NOT NULL,
  breaks          JSONB NOT NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS widget_layouts (
  page      TEXT PRIMARY KEY,
  layout    JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS widget_customizations (
  page      TEXT NOT NULL,
  widget_id TEXT NOT NULL,
  customizations JSONB NOT NULL,
  PRIMARY KEY (page, widget_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
  key       TEXT PRIMARY KEY,
  value     JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sync_outbox (
  id          BIGSERIAL PRIMARY KEY,
  kind        TEXT NOT NULL,
  entity_id   INTEGER,
  action      TEXT NOT NULL,
  payload     JSONB NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending',
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  pushed_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS sync_outbox_status_idx ON sync_outbox(status, created_at);
"""


def bootstrap_schema() -> None:
    """Create every table + index idempotently. Called once on app
    startup."""
    with cursor() as cur:
        cur.execute(_SCHEMA_DDL)
```

- [ ] **Step 5: Run tests, verify PASS** (requires `DATABASE_URL` set locally — see step 6)

If running locally without `DATABASE_URL`, tests skip. To verify against the live Railway Postgres, set `DATABASE_URL` from the Railway dashboard's Postgres service for one shell and run pytest.

- [ ] **Step 6: Local verification with the live Railway Postgres**

```powershell
$env:DATABASE_URL = "<paste from Railway Postgres -> Variables -> DATABASE_URL>"
.venv/Scripts/python.exe -m pytest tests/test_db.py -v
```

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/db.py tests/test_db.py requirements.txt
git commit -m "feat(db): postgres connection pool, helpers, and schema bootstrap"
```

---

### Task 2: Wire schema bootstrap into app startup

**Files:**
- Modify: `src/zira_dashboard/app.py`

- [ ] **Step 1: Add startup + shutdown hooks**

In `src/zira_dashboard/app.py`, find where `app = FastAPI(...)` is constructed. Add `lifespan` or startup/shutdown event hooks (whichever pattern the existing app uses):

```python
from contextlib import asynccontextmanager
from . import db


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_pool()
    db.bootstrap_schema()
    yield
    db.shutdown_pool()


app = FastAPI(lifespan=lifespan)
```

If the app already uses `app.add_event_handler("startup", ...)`, add equivalents there instead.

- [ ] **Step 2: Smoke-run locally**

```powershell
$env:DATABASE_URL = "<paste from Railway>"
.venv/Scripts/python.exe -m uvicorn zira_dashboard.app:app
```

Should boot without errors. Hit `http://localhost:8000/staffing/skills` — page should render (no data yet but no crash).

- [ ] **Step 3: Commit**

```bash
git add src/zira_dashboard/app.py
git commit -m "feat(db): bootstrap schema on app startup"
```

---

### Task 3: Migration script — JSON → Postgres

**Files:**
- Create: `scripts/migrate_json_to_postgres.py`

- [ ] **Step 1: Implement the script**

```python
#!/usr/bin/env python3
"""One-shot migration: read existing JSON state files, INSERT into
Postgres. Idempotent — safe to re-run; uses INSERT ... ON CONFLICT.

Run from the project root with DATABASE_URL set:
  python -m scripts.migrate_json_to_postgres
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from zira_dashboard import db  # noqa: E402


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARN: could not read {path}: {e}")
        return None


def migrate_skills(meta: list[dict] | None) -> None:
    if not meta:
        print("skills: no skill_columns_meta.json — skipped")
        return
    for i, m in enumerate(meta):
        db.execute(
            "INSERT INTO skills (name, skill_type, sort_order) VALUES (%s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET skill_type = EXCLUDED.skill_type, sort_order = EXCLUDED.sort_order",
            (m["name"], m.get("type", ""), i),
        )
    print(f"skills: {len(meta)} rows")


def migrate_people(roster: list[dict] | None) -> None:
    if not roster:
        print("people: no roster.json — skipped")
        return
    for p in roster:
        db.execute(
            "INSERT INTO people (odoo_id, name, active, reserve) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET odoo_id = EXCLUDED.odoo_id, active = EXCLUDED.active, reserve = EXCLUDED.reserve",
            (p.get("employee_id"), p["name"], p.get("active", True), p.get("reserve", False)),
        )
    print(f"people: {len(roster)} rows")
    # Person-skills: only if the person had non-zero levels.
    for p in roster:
        for skill_name, level in (p.get("skills") or {}).items():
            if not isinstance(level, (int, float)) or level <= 0:
                continue
            db.execute(
                "INSERT INTO person_skills (person_id, skill_id, level) "
                "SELECT pe.id, sk.id, %s FROM people pe, skills sk "
                "WHERE pe.name = %s AND sk.name = %s "
                "ON CONFLICT (person_id, skill_id) DO UPDATE SET level = EXCLUDED.level",
                (int(level), p["name"], skill_name),
            )


def migrate_work_centers(wc_data: dict | None) -> None:
    # Read from staffing.LOCATIONS too — the WC list is hardcoded there;
    # work_centers.json only has overrides keyed by meter_id or name.
    from zira_dashboard import staffing
    overrides = wc_data or {}
    for loc in staffing.LOCATIONS:
        key = loc.meter_id if loc.meter_id else f"name:{loc.name}"
        eff = overrides.get(key, {})
        db.execute(
            "INSERT INTO work_centers (name, meter_id, category, cell, value_stream, "
            "min_ops, max_ops, goal_per_day_override, group_name, note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET "
            "meter_id = EXCLUDED.meter_id, category = EXCLUDED.category, cell = EXCLUDED.cell, "
            "value_stream = EXCLUDED.value_stream, min_ops = EXCLUDED.min_ops, "
            "max_ops = EXCLUDED.max_ops, goal_per_day_override = EXCLUDED.goal_per_day_override, "
            "group_name = EXCLUDED.group_name, note = EXCLUDED.note",
            (
                loc.name, loc.meter_id, loc.category, loc.cell,
                eff.get("value_stream", "Recycled"),
                eff.get("min_ops", loc.min_ops),
                eff.get("max_ops", loc.max_ops),
                eff.get("goal_per_day"),
                eff.get("group"),
                eff.get("note", ""),
            ),
        )
    print(f"work_centers: {len(staffing.LOCATIONS)} rows")
    # Required skills + default people: pull from overrides too.
    for loc in staffing.LOCATIONS:
        key = loc.meter_id if loc.meter_id else f"name:{loc.name}"
        eff = overrides.get(key, {})
        for s in (eff.get("required_skills") or []):
            db.execute(
                "INSERT INTO work_center_required_skills (wc_id, skill_id) "
                "SELECT wc.id, sk.id FROM work_centers wc, skills sk "
                "WHERE wc.name = %s AND sk.name = %s "
                "ON CONFLICT DO NOTHING",
                (loc.name, s),
            )
        for i, person_name in enumerate(eff.get("default_people") or []):
            db.execute(
                "INSERT INTO work_center_default_people (wc_id, person_id, sort_order) "
                "SELECT wc.id, pe.id, %s FROM work_centers wc, people pe "
                "WHERE wc.name = %s AND pe.name = %s "
                "ON CONFLICT (wc_id, person_id) DO UPDATE SET sort_order = EXCLUDED.sort_order",
                (i, loc.name, person_name),
            )


def migrate_groups_and_value_streams(wc_data: dict | None) -> None:
    if not wc_data:
        return
    groups = (wc_data.get("groups") or {})
    for name, meta in groups.items():
        db.execute(
            "INSERT INTO groups (name, goal_per_day_override) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET goal_per_day_override = EXCLUDED.goal_per_day_override",
            (name, meta.get("goal_per_day") if isinstance(meta, dict) else None),
        )
    print(f"groups: {len(groups)} rows")


def migrate_schedules() -> None:
    sched_dir = ROOT / "schedules"
    if not sched_dir.exists():
        print("schedules: no schedules/ — skipped")
        return
    n = 0
    for path in sorted(sched_dir.glob("*.json")):
        d_iso = path.stem
        data = _load_json(path)
        if not isinstance(data, dict):
            continue
        db.execute(
            "INSERT INTO schedules (day, published, testing_day, notes, custom_hours, published_snapshot) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb) "
            "ON CONFLICT (day) DO UPDATE SET published = EXCLUDED.published, "
            "testing_day = EXCLUDED.testing_day, notes = EXCLUDED.notes, "
            "custom_hours = EXCLUDED.custom_hours, published_snapshot = EXCLUDED.published_snapshot",
            (
                d_iso,
                bool(data.get("published", False)),
                bool(data.get("testing_day", False)),
                str(data.get("notes") or ""),
                json.dumps(data.get("custom_hours")) if data.get("custom_hours") else None,
                json.dumps(data.get("published_snapshot")) if data.get("published_snapshot") else None,
            ),
        )
        # Assignments + time off + wc notes
        assignments = data.get("assignments") or {}
        time_off = assignments.get("__time_off") or []
        for person_name in time_off:
            db.execute(
                "INSERT INTO schedule_time_off (day, person_id) "
                "SELECT %s, pe.id FROM people pe WHERE pe.name = %s "
                "ON CONFLICT DO NOTHING",
                (d_iso, person_name),
            )
        for wc_name, names in assignments.items():
            if wc_name == "__time_off":
                continue
            for i, person_name in enumerate(names or []):
                db.execute(
                    "INSERT INTO schedule_assignments (day, wc_id, person_id, sort_order) "
                    "SELECT %s, wc.id, pe.id, %s FROM work_centers wc, people pe "
                    "WHERE wc.name = %s AND pe.name = %s "
                    "ON CONFLICT (day, wc_id, person_id) DO UPDATE SET sort_order = EXCLUDED.sort_order",
                    (d_iso, i, wc_name, person_name),
                )
        for wc_name, note in (data.get("wc_notes") or {}).items():
            if not note:
                continue
            db.execute(
                "INSERT INTO schedule_wc_notes (day, wc_id, note) "
                "SELECT %s, wc.id, %s FROM work_centers wc WHERE wc.name = %s "
                "ON CONFLICT (day, wc_id) DO UPDATE SET note = EXCLUDED.note",
                (d_iso, note, wc_name),
            )
        n += 1
    print(f"schedules: {n} files")


def migrate_global_schedule(data: dict | None) -> None:
    if not data:
        print("global_schedule: no schedule.json — skipped")
        return
    db.execute(
        "INSERT INTO global_schedule (id, shift_start, shift_end, work_weekdays, breaks) "
        "VALUES (1, %s, %s, %s, %s::jsonb) "
        "ON CONFLICT (id) DO UPDATE SET shift_start = EXCLUDED.shift_start, "
        "shift_end = EXCLUDED.shift_end, work_weekdays = EXCLUDED.work_weekdays, "
        "breaks = EXCLUDED.breaks",
        (
            data.get("shift_start", "07:00"),
            data.get("shift_end", "15:00"),
            list(data.get("work_weekdays", [0, 1, 2, 3, 4])),
            json.dumps(data.get("breaks") or []),
        ),
    )
    print("global_schedule: 1 row")


def migrate_simple_kv(label: str, key: str, path_name: str) -> None:
    data = _load_json(ROOT / path_name)
    if data is None:
        print(f"{label}: no {path_name} — skipped")
        return
    db.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s::jsonb, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, json.dumps(data)),
    )
    print(f"{label}: 1 row")


def main() -> int:
    print("Bootstrapping schema...")
    db.init_pool()
    db.bootstrap_schema()

    print("\n=== Migrating JSON files into Postgres ===")
    migrate_skills(_load_json(ROOT / "skill_columns_meta.json"))
    migrate_people(_load_json(ROOT / "roster.json"))
    wc_data = _load_json(ROOT / "work_centers.json")
    migrate_work_centers(wc_data)
    migrate_groups_and_value_streams(wc_data)
    migrate_schedules()
    migrate_global_schedule(_load_json(ROOT / "schedule.json"))
    migrate_simple_kv("settings", "settings", "settings.json")
    migrate_simple_kv("layouts", "layouts", "layouts.json")
    migrate_simple_kv("widget_customizations", "widget_customizations", "widget_customizations.json")
    migrate_simple_kv("skill_filter", "skill_filter", "skill_filter.json")

    last_sync_path = ROOT / ".odoo_last_sync"
    if last_sync_path.exists():
        ts = last_sync_path.read_text().strip()
        db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('odoo_last_sync', %s::jsonb, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (json.dumps(ts),),
        )
        print("odoo_last_sync: 1 row")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the migration locally against the live Railway Postgres**

```powershell
$env:DATABASE_URL = "<paste from Railway>"
python -m scripts.migrate_json_to_postgres
```

Output should list row counts per table. Errors (e.g., schedule referencing a person not yet in `people`) are surfaced and need investigating.

- [ ] **Step 3: Commit**

```bash
git add scripts/migrate_json_to_postgres.py
git commit -m "feat(db): one-shot JSON→Postgres migration script"
```

---

### Task 4: Refactor `staffing.py` (people + schedules)

**Files:**
- Modify: `src/zira_dashboard/staffing.py`
- Test: `tests/test_postgres_stores.py`

- [ ] **Step 1: Read the current staffing.py module to map the public API**

The public API to preserve (so handlers don't need changes):
- `Person` dataclass — keep as-is, just hydrate from SQL
- `Schedule` dataclass — same
- `load_roster() -> list[Person]`
- `save_roster(roster: list[Person]) -> None`
- `load_schedule(day: date) -> Schedule`
- `save_schedule(sched: Schedule) -> None`
- Constants: `LOCATIONS`, `SKILLS`, `TIME_OFF_KEY`, `SKILL_LABELS` — keep

The internals (`_seed_roster`, `_import_skill_matrix_csv`, ROSTER_PATH, SCHEDULES_DIR) become irrelevant or fall-back-only.

- [ ] **Step 2: Rewrite `load_roster()`**

```python
def load_roster() -> list[Person]:
    from . import db
    rows = db.query(
        "SELECT p.id, p.name, p.active, p.reserve, p.odoo_id, "
        "  COALESCE(json_object_agg(s.name, ps.level) FILTER (WHERE s.name IS NOT NULL), '{}'::json)::text AS skills_json "
        "FROM people p "
        "LEFT JOIN person_skills ps ON ps.person_id = p.id "
        "LEFT JOIN skills s ON s.id = ps.skill_id "
        "GROUP BY p.id "
        "ORDER BY (NOT p.active), lower(p.name)"
    )
    out: list[Person] = []
    import json as _json
    for r in rows:
        out.append(Person(
            name=r["name"],
            active=r["active"],
            reserve=r["reserve"],
            skills={k: int(v) for k, v in (_json.loads(r["skills_json"]) or {}).items()},
            employee_id=r["odoo_id"],
        ))
    return out
```

- [ ] **Step 3: Rewrite `save_roster()`**

```python
def save_roster(roster: list[Person]) -> None:
    from . import db
    with db.cursor() as cur:
        for p in roster:
            cur.execute(
                "INSERT INTO people (name, active, reserve, odoo_id) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (name) DO UPDATE SET active = EXCLUDED.active, "
                "reserve = EXCLUDED.reserve, odoo_id = COALESCE(EXCLUDED.odoo_id, people.odoo_id), "
                "local_dirty = TRUE",
                (p.name, p.active, p.reserve, p.employee_id),
            )
            # Skill levels: upsert non-zero, delete zero
            for skill_name, level in (p.skills or {}).items():
                if level > 0:
                    cur.execute(
                        "INSERT INTO person_skills (person_id, skill_id, level, local_dirty) "
                        "SELECT pe.id, sk.id, %s, TRUE FROM people pe, skills sk "
                        "WHERE pe.name = %s AND sk.name = %s "
                        "ON CONFLICT (person_id, skill_id) DO UPDATE SET level = EXCLUDED.level, local_dirty = TRUE",
                        (level, p.name, skill_name),
                    )
                else:
                    cur.execute(
                        "DELETE FROM person_skills WHERE "
                        "person_id = (SELECT id FROM people WHERE name = %s) AND "
                        "skill_id = (SELECT id FROM skills WHERE name = %s)",
                        (p.name, skill_name),
                    )
```

- [ ] **Step 4: Rewrite `load_schedule()` and `save_schedule()`**

```python
def load_schedule(day: date) -> Schedule:
    from . import db
    rows = db.query(
        "SELECT day, published, testing_day, notes, custom_hours, published_snapshot "
        "FROM schedules WHERE day = %s",
        (day,),
    )
    if not rows:
        return Schedule(day=day, published=False)
    r = rows[0]
    # Build assignments dict: {wc_name: [person_name, ...]} + __time_off
    asg_rows = db.query(
        "SELECT wc.name AS wc_name, pe.name AS person_name "
        "FROM schedule_assignments sa "
        "JOIN work_centers wc ON wc.id = sa.wc_id "
        "JOIN people pe ON pe.id = sa.person_id "
        "WHERE sa.day = %s ORDER BY sa.wc_id, sa.sort_order",
        (day,),
    )
    assignments: dict[str, list[str]] = {}
    for a in asg_rows:
        assignments.setdefault(a["wc_name"], []).append(a["person_name"])
    to_rows = db.query(
        "SELECT pe.name FROM schedule_time_off s JOIN people pe ON pe.id = s.person_id WHERE s.day = %s",
        (day,),
    )
    if to_rows:
        assignments[TIME_OFF_KEY] = [t["name"] for t in to_rows]
    notes_rows = db.query(
        "SELECT wc.name AS wc_name, sn.note "
        "FROM schedule_wc_notes sn JOIN work_centers wc ON wc.id = sn.wc_id "
        "WHERE sn.day = %s",
        (day,),
    )
    wc_notes = {n["wc_name"]: n["note"] for n in notes_rows}
    return Schedule(
        day=day,
        published=r["published"],
        assignments=assignments,
        notes=r["notes"] or "",
        wc_notes=wc_notes,
        testing_day=r["testing_day"],
        custom_hours=r["custom_hours"],
        published_snapshot=r["published_snapshot"],
    )


def save_schedule(sched: Schedule) -> None:
    import json as _json
    from . import db
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (day, published, testing_day, notes, custom_hours, published_snapshot, updated_at) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, now()) "
            "ON CONFLICT (day) DO UPDATE SET published = EXCLUDED.published, "
            "testing_day = EXCLUDED.testing_day, notes = EXCLUDED.notes, "
            "custom_hours = EXCLUDED.custom_hours, published_snapshot = EXCLUDED.published_snapshot, "
            "updated_at = now()",
            (
                sched.day, sched.published, sched.testing_day, sched.notes,
                _json.dumps(sched.custom_hours) if sched.custom_hours else None,
                _json.dumps(sched.published_snapshot) if sched.published_snapshot else None,
            ),
        )
        # Replace assignments + time off + wc_notes for this day (atomic).
        cur.execute("DELETE FROM schedule_assignments WHERE day = %s", (sched.day,))
        cur.execute("DELETE FROM schedule_time_off WHERE day = %s", (sched.day,))
        cur.execute("DELETE FROM schedule_wc_notes WHERE day = %s", (sched.day,))
        for wc_name, names in (sched.assignments or {}).items():
            if wc_name == TIME_OFF_KEY:
                for n in names:
                    cur.execute(
                        "INSERT INTO schedule_time_off (day, person_id) "
                        "SELECT %s, pe.id FROM people pe WHERE pe.name = %s",
                        (sched.day, n),
                    )
                continue
            for i, n in enumerate(names):
                cur.execute(
                    "INSERT INTO schedule_assignments (day, wc_id, person_id, sort_order) "
                    "SELECT %s, wc.id, pe.id, %s FROM work_centers wc, people pe "
                    "WHERE wc.name = %s AND pe.name = %s",
                    (sched.day, i, wc_name, n),
                )
        for wc_name, note in (sched.wc_notes or {}).items():
            if not note:
                continue
            cur.execute(
                "INSERT INTO schedule_wc_notes (day, wc_id, note) "
                "SELECT %s, wc.id, %s FROM work_centers wc WHERE wc.name = %s",
                (sched.day, note, wc_name),
            )
```

- [ ] **Step 5: Add round-trip test**

`tests/test_postgres_stores.py`:

```python
import os
from datetime import date
import pytest

from zira_dashboard import db, staffing

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")


@pytest.fixture(autouse=True)
def fresh_schema():
    db.shutdown_pool()
    db.init_pool()
    db.bootstrap_schema()
    # Wipe critical tables before each test
    with db.cursor() as cur:
        for tbl in ("schedule_assignments", "schedule_time_off", "schedule_wc_notes",
                    "schedules", "person_skills", "work_center_required_skills",
                    "work_center_default_people", "people", "skills", "work_centers"):
            cur.execute(f"DELETE FROM {tbl}")
    yield
    db.shutdown_pool()


def test_save_and_load_person_round_trip():
    db.execute("INSERT INTO skills (name, skill_type) VALUES ('Repair', 'Production Skills')")
    p = staffing.Person(name="Alice", active=True, reserve=False,
                        skills={"Repair": 3}, employee_id=42)
    staffing.save_roster([p])
    out = staffing.load_roster()
    assert len(out) == 1
    assert out[0].name == "Alice"
    assert out[0].active is True
    assert out[0].employee_id == 42
    assert out[0].skills == {"Repair": 3}
```

- [ ] **Step 6: Run, verify PASS**, **Step 7: Commit**

```bash
git add src/zira_dashboard/staffing.py tests/test_postgres_stores.py
git commit -m "feat(db): staffing.py reads/writes from Postgres"
```

---

### Task 5: Refactor `work_centers_store.py`

**Files:**
- Modify: `src/zira_dashboard/work_centers_store.py`

The module's existing public API (read from a peek):
- `effective(loc)` — returns dict with merged defaults + overrides
- `min_ops(loc)`, `max_ops(loc)`, `value_stream(loc)`, `required_skills(loc)`, `default_people(loc)`, `goal_per_day(loc)`
- `save_one(loc, updates)` — write per-WC fields
- `add_group(name)`, `delete_group(name)`, `rename_group(old, new)`, `registered_groups()`, `members(kind, name)`
- `group_goal(...)` etc.

Rewrite each to issue SQL queries against `work_centers`, `work_center_required_skills`, `work_center_default_people`, `groups`, `value_streams` tables. The transformation is mechanical — I'll spell out the exact SQL per function.

(Steps similar to Task 4 — read API, replace each function body with SQL, round-trip tests, commit.)

---

### Task 6: Refactor `settings_store.py`, `schedule_store.py`, `layout_store.py`, `widget_customizer.py`, `skill_filter_store.py`

**Files:** five small store modules.

Each module follows the same pattern:
- Read public API
- Replace JSON file I/O with `db.query` / `db.execute`
- Use `app_settings` table for simple key-value, or the dedicated table for structured data
- Add a round-trip test

Group these into one task because each is small (~30 lines per module).

---

### Task 7: Refactor `odoo_sync.py` to write to Postgres

**Files:**
- Modify: `src/zira_dashboard/odoo_sync.py`

Sync writes go to `people` (UPSERT by `odoo_id`), `skills` (UPSERT by `odoo_id` or `name`), `person_skills` (UPSERT). Last-sync timestamp goes to `app_settings['odoo_last_sync']`. Skill columns metadata goes to `skills` table directly (the `app_settings['skill_columns_meta']` becomes a derived view).

Critical rule: sync NEVER touches `local_dirty` rows on the local-only fields (currently just `reserve`). It DOES update server-mastered fields (`active`, skill levels) unconditionally.

- [ ] **Step 1**: Rewrite `sync()` body
- [ ] **Step 2**: Update tests in `tests/test_odoo_sync.py` to assert against Postgres rows instead of JSON files
- [ ] **Step 3**: Commit

---

### Task 8: Run the migration on Railway and verify

**Files:** none — manual + verification.

- [ ] **Step 1: Push everything so far to main**

After Tasks 1–7 are committed, push to origin. Railway redeploys; schema bootstraps automatically on app startup.

- [ ] **Step 2: Run the migration script via `railway run`**

```powershell
railway run python -m scripts.migrate_json_to_postgres
```

This executes the script in Railway's environment with `DATABASE_URL` already set. It reads JSON files from the deployed image (read-only) and inserts into Postgres.

Wait — JSON files are gitignored, so they're NOT in the deployed image. Migration source has to be the local files in your dev tree. Run it locally:

```powershell
$env:DATABASE_URL = "<paste from Railway Postgres>"
python -m scripts.migrate_json_to_postgres
```

- [ ] **Step 3: Verify the live app reads the migrated data**

Hit `gpiplantmanager.com/staffing/skills` — should show the synced employees + skills.
Hit `gpiplantmanager.com/staffing` — should show the migrated schedule for today (if there is one in `schedules/`).
Hit `gpiplantmanager.com/settings` — work center configs preserved.

- [ ] **Step 4: Test persistence**

Toggle reserve on a person. Push a no-op git commit (to force redeploy). Reload the page. Reserve flag still set ✓.

---

### Task 9: Performance + UX polish

**Files:**
- Modify: route handlers (latency improvements, batched queries)
- Modify: templates (loading states for slow operations)

- [ ] **Step 1**: Audit each route's queries; combine N+1 patterns into single queries where applicable.
- [ ] **Step 2**: Add a small loading spinner (or "Saving..." indicator) for autosave-triggering edits — gives a "feels alive" signal.
- [ ] **Step 3**: Ensure autosave failures surface a toast (currently silent in some places).
- [ ] **Step 4**: Commit polish changes.

---

### Task 10: Cleanup + docs

**Files:**
- Modify: `docs/odoo-setup.md`
- Modify: store modules (remove dead JSON read fallbacks if any)
- Modify: `.gitignore` (no functional change; keep the JSON file ignores for safety)

- [ ] **Step 1**: Update setup docs with Postgres add-on instructions.
- [ ] **Step 2**: Remove any dead code in stores that was left behind for transitional fallback.
- [ ] **Step 3**: Final test pass: `pytest tests/ -v` (with `DATABASE_URL` set) — full green.
- [ ] **Step 4**: Commit.

---

## Done criteria

- All 10 tasks committed and pushed.
- Railway Postgres has tables populated; live app reads/writes against Postgres.
- Persistence verified across a redeploy (toggle → push → reload → still set).
- Reserve flag, scheduler drafts, settings, work-center configs, skill filter all persist.
- Page render latency noticeably faster.
- Test suite green (with `DATABASE_URL` set).
- `sync_outbox` table exists and is empty (no Phase 2 writes yet, but the seam is in place).
