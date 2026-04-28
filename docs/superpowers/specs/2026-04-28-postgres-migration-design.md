# Postgres Migration — Design

**Date:** 2026-04-28
**Status:** Approved (brainstorming → implementation planning)

## Context

The dashboard currently persists everything to JSON files on the local
filesystem (`roster.json`, `schedules/*.json`, `work_centers.json`,
`settings.json`, `layouts.json`, `widget_customizations.json`,
`schedule.json`, `skill_filter.json`, `skill_columns_meta.json`,
`.odoo_last_sync`). All of these are gitignored.

Railway containers are ephemeral. Each `git push` triggers a redeploy
that wipes the container's filesystem. Every save we've built — reserve
toggles, scheduler drafts, custom hours, time off, settings — is lost
on the next deploy. With ~10 deploys today alone, the data was reset
~10 times.

The fix is a real database that persists across deploys. Architecture
chosen during brainstorming: **hybrid — Odoo masters HR-domain data
(people, skills, time off, work centers) and our local Postgres masters
app-specific data (per-day schedules, layouts, customizations)**. Reads
happen from Postgres for speed; HR-mastered entities are mirrored from
Odoo via the existing TTL sync. Two-way sync writes (push back to Odoo)
are scoped in *architecturally* but the actual write paths are deferred
to follow-up phases per entity.

## Goals

1. All app state persists across Railway deploys.
2. Page reads come from Postgres (sub-1ms locally), not JSON file I/O,
   so the dashboard feels snappy.
3. Normalized schema with surrogate IDs everywhere — every Odoo-mirrored
   entity has a stable `odoo_id` link, every write-able row tracks
   `last_pushed_at` / `local_dirty` so a future two-way sync push can
   queue cleanly via the outbox table.
4. One-shot migration script reads existing JSON files and bulk-inserts
   into Postgres, then JSON files become irrelevant.
5. Schema bootstrap on app startup (`CREATE TABLE IF NOT EXISTS`) — no
   alembic for now, we add it if migrations get complex.

## Non-goals

- Two-way sync writes back to Odoo. **Architecturally supported, not
  implemented in this spec.** The schema includes `odoo_id` columns,
  `last_pushed_at` timestamps, `local_dirty` flags, and a `sync_outbox`
  table for the future. Wiring up the actual `hr.employee.write`,
  `hr.employee.skill.create`, etc. calls is a follow-up phase per
  entity.
- Custom Odoo modules. Per-day schedules and layouts stay in our
  Postgres permanently; we don't try to model them in Odoo.
- Multi-tenant / multi-company. Single Odoo instance, single dashboard.
- Migration to async DB drivers. FastAPI handlers stay sync; psycopg2 +
  raw SQL is enough for our scale.
- ORM. We write SQL directly with small helper functions. No SQLAlchemy.

## Design

### Stack

- **Postgres** — Railway add-on (`railway.com/template/postgres`),
  auto-injects `DATABASE_URL` env var into the web service.
- **psycopg2-binary** — sync driver, well-known, no compilation issues.
- **Connection pool** — `psycopg2.pool.ThreadedConnectionPool` (size
  4–8) so concurrent requests don't open fresh connections each time.
- **Schema bootstrap** — at app startup, run `CREATE TABLE IF NOT EXISTS`
  + index DDL idempotently. No alembic; migrations later if needed.

### `db.py` module

Single module that owns the pool, schema bootstrap, and small SQL
helpers:

```python
from psycopg2.pool import ThreadedConnectionPool
from contextlib import contextmanager

_pool: ThreadedConnectionPool | None = None

def init_pool() -> None: ...
def shutdown_pool() -> None: ...

@contextmanager
def cursor():
    """Yields a cursor inside a transaction; commits on exit, rolls back
    on exception."""

def query(sql: str, params: tuple = ()) -> list[dict]:
    """Read helper — returns list of dicts (column name → value)."""

def execute(sql: str, params: tuple = ()) -> None:
    """Write helper — runs in its own transaction."""

def execute_many(sql: str, rows: list[tuple]) -> None: ...

def bootstrap_schema() -> None:
    """Idempotent CREATE TABLE / INDEX DDL. Called once on app startup."""
```

App startup (in `app.py`) calls `db.init_pool()` then
`db.bootstrap_schema()` before any route is served.

### Schema

```sql
-- HR-mastered entities (mirrored from Odoo via TTL sync) ----------------

CREATE TABLE people (
  id            SERIAL PRIMARY KEY,
  odoo_id       INTEGER UNIQUE,            -- hr.employee.id
  name          TEXT NOT NULL UNIQUE,
  active        BOOLEAN NOT NULL DEFAULT TRUE,
  reserve       BOOLEAN NOT NULL DEFAULT FALSE,
  last_pulled_at  TIMESTAMPTZ,
  last_pushed_at  TIMESTAMPTZ,
  local_dirty   BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX ON people(active);

CREATE TABLE skills (
  id            SERIAL PRIMARY KEY,
  odoo_id       INTEGER UNIQUE,            -- hr.skill.id
  name          TEXT NOT NULL UNIQUE,
  skill_type    TEXT NOT NULL,             -- "Production Skills" / "Supervisor Skills"
  sort_order    INTEGER NOT NULL DEFAULT 0,
  last_pulled_at  TIMESTAMPTZ
);

CREATE TABLE person_skills (
  person_id     INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  skill_id      INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  level         SMALLINT NOT NULL DEFAULT 0,  -- 0..3
  last_pulled_at  TIMESTAMPTZ,
  last_pushed_at  TIMESTAMPTZ,
  local_dirty   BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY (person_id, skill_id)
);

-- Work centers (eventually mirrored from Odoo's mrp.workcenter; for now
-- driven from our LOCATIONS constant) -----------------------------------

CREATE TABLE work_centers (
  id              SERIAL PRIMARY KEY,
  odoo_id         INTEGER UNIQUE,         -- mrp.workcenter.id (nullable)
  name            TEXT NOT NULL UNIQUE,   -- "Repair 1"
  meter_id        TEXT,                   -- Zira meter ID
  category        TEXT NOT NULL,          -- "Repair" | "Dismantler" | etc
  cell            TEXT,                   -- "Bay 1"
  value_stream    TEXT,                   -- "Recycled" | "New" | etc
  min_ops         INTEGER NOT NULL DEFAULT 1,
  max_ops         INTEGER,                -- nullable = unlimited
  goal_per_day_override INTEGER,          -- nullable = use auto
  group_name      TEXT,                   -- nullable
  note            TEXT,
  last_pulled_at  TIMESTAMPTZ,
  last_pushed_at  TIMESTAMPTZ,
  local_dirty     BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE work_center_required_skills (
  wc_id     INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE CASCADE,
  skill_id  INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  PRIMARY KEY (wc_id, skill_id)
);

CREATE TABLE work_center_default_people (
  wc_id     INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  sort_order INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (wc_id, person_id)
);

CREATE TABLE groups (
  name      TEXT PRIMARY KEY,
  goal_per_day_override INTEGER
);

CREATE TABLE value_streams (
  name      TEXT PRIMARY KEY,
  goal_per_day_override INTEGER
);

-- App-specific (not mirrored anywhere) ---------------------------------

CREATE TABLE schedules (
  day               DATE PRIMARY KEY,
  published         BOOLEAN NOT NULL DEFAULT FALSE,
  testing_day       BOOLEAN NOT NULL DEFAULT FALSE,
  notes             TEXT NOT NULL DEFAULT '',
  custom_hours      JSONB,                  -- {"start","end","breaks":[]}
  published_snapshot JSONB,                 -- snapshot at publish time
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Two surfaces for the day's assignments: regular WC slots + time off.
CREATE TABLE schedule_assignments (
  day         DATE NOT NULL REFERENCES schedules(day) ON DELETE CASCADE,
  wc_id       INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE CASCADE,
  person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  sort_order  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, wc_id, person_id)
);
CREATE INDEX ON schedule_assignments(day);

CREATE TABLE schedule_time_off (
  day       DATE NOT NULL REFERENCES schedules(day) ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  PRIMARY KEY (day, person_id)
);

CREATE TABLE schedule_wc_notes (
  day       DATE NOT NULL REFERENCES schedules(day) ON DELETE CASCADE,
  wc_id     INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE CASCADE,
  note      TEXT NOT NULL,
  PRIMARY KEY (day, wc_id)
);

CREATE TABLE global_schedule (
  id              INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- singleton
  shift_start     TIME NOT NULL,
  shift_end       TIME NOT NULL,
  work_weekdays   INTEGER[] NOT NULL,       -- 0=Mon..6=Sun
  breaks          JSONB NOT NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE widget_layouts (
  page      TEXT PRIMARY KEY,
  layout    JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE widget_customizations (
  page      TEXT NOT NULL,
  widget_id TEXT NOT NULL,
  customizations JSONB NOT NULL,
  PRIMARY KEY (page, widget_id)
);

CREATE TABLE app_settings (
  key       TEXT PRIMARY KEY,
  value     JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Used for: skill_filter (hidden list), .odoo_last_sync timestamp,
-- station_targets, group_targets per category, value_stream_targets.

-- Outbox for future two-way sync (not actively used in Phase 1) -------

CREATE TABLE sync_outbox (
  id          BIGSERIAL PRIMARY KEY,
  kind        TEXT NOT NULL,                -- "person", "skill", "person_skill", "time_off", "work_center"
  entity_id   INTEGER,                      -- local id of the row to push
  action      TEXT NOT NULL,                -- "create", "update", "delete"
  payload     JSONB NOT NULL,               -- exact field set to send to Odoo
  status      TEXT NOT NULL DEFAULT 'pending',  -- pending | pushed | failed | dead
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  pushed_at   TIMESTAMPTZ
);
CREATE INDEX ON sync_outbox(status, created_at);
```

### One-shot migration script

`scripts/migrate_json_to_postgres.py`:

1. Reads each existing JSON file (locally or via a `railway run` shell).
2. Inserts into the corresponding Postgres tables using
   `INSERT ... ON CONFLICT DO NOTHING` so the script is idempotent (safe
   to re-run).
3. Verifies row counts after migration.
4. Prints a summary.

The script is run once after the schema is bootstrapped on Railway. It
does NOT run automatically — Dale invokes it deliberately so the
migration window is observable.

### Store module refactors

Each existing store module gets rewritten to use `db.py`:

- `staffing.py` — `load_roster()` / `save_roster()` / `load_schedule()`
  / `save_schedule()` become Postgres reads/writes
- `work_centers_store.py` — full SQL rewrite
- `settings_store.py` — `app_settings` rows
- `schedule_store.py` — `global_schedule` table
- `layout_store.py` — `widget_layouts` table
- `widget_customizer.py` — `widget_customizations` table
- `skill_filter_store.py` — single `app_settings['skill_filter']` row
- `odoo_sync.py` — writes go to `people`, `skills`, `person_skills`
  tables; sync timestamp goes to `app_settings['odoo_last_sync']`

Module-level public APIs stay the same as much as possible (so route
handlers don't need to change). Internal storage swaps from JSON to SQL.

### Schedule data shape

Schedules are the most complex domain. The current JSON shape:

```json
{
  "day": "2026-04-28",
  "published": true,
  "testing_day": false,
  "notes": "...",
  "wc_notes": {"Repair 1": "..."},
  "assignments": {"Repair 1": ["Alice"], "__time_off": ["Bob"]},
  "custom_hours": {"start": "07:18", "end": "10:32", "breaks": []},
  "published_snapshot": {...}
}
```

In Postgres, this becomes one `schedules` row + N
`schedule_assignments` rows + M `schedule_time_off` rows + K
`schedule_wc_notes` rows. The `staffing.load_schedule(day)` API still
returns the same `Schedule` dataclass, just hydrated from SQL JOINs.

### Read-after-write consistency for two-way sync (architectural)

When local writes happen, the affected row(s) get `local_dirty = TRUE`
and an entry queued in `sync_outbox`. A future background worker drains
the outbox, pushes to Odoo, marks `pushed_at` and clears `local_dirty`.

In Phase 1 we only set the flags + queue rows; we don't drain. That
way the schema is "sync-ready" the moment Phase 2 ships per-entity.

### Performance characteristics

- Page render reads: 1–3 simple SQL queries per page. <5ms each on
  Railway internal network. Total page latency drops from ~200–500ms
  (current JSON-file + Odoo pull) to <50ms (Postgres + cached Odoo
  pull).
- Saves: 1 transaction with 1–N inserts/updates. <10ms.
- Odoo TTL sync remains a 1-hour background fetch + bulk upsert into
  Postgres. Stays the same speed — just writes to Postgres instead of
  JSON.

### Error handling

- DB connection failure on startup → app fails fast with a clear error;
  Railway restarts; eventually Postgres reconnects.
- DB connection failure on request → 500 with a clear message; route
  doesn't fall back to JSON.
- Constraint violation (e.g., duplicate name) → caught and surfaced
  in the response with a useful error.

## Acceptance criteria

- After deploy, Railway Postgres add-on is connected. App starts and
  bootstraps schema without error.
- One-shot migration script runs locally, populates Postgres from
  current JSON files, prints expected row counts, completes cleanly.
- All existing UI works against Postgres: People Matrix, Plant Scheduler
  (drafts persist!), Time Off, Settings (groups, WCs, value streams,
  schedule), all dashboards (Recycling VS, New VS, Work Centers).
- Reserve toggle on a person, then redeploy → reserve flag still set.
- Save a scheduler draft, then redeploy → draft still there.
- Edit a WC's required skills, then redeploy → still set.
- Odoo TTL sync writes to `people` / `skills` / `person_skills` tables
  on each refresh, preserving local `reserve` flags.
- Sync outbox table exists and accepts inserts; not actively drained
  (Phase 2 will).
- Page render latency noticeably faster than today.

## Risks

- **Migration window.** Between schema bootstrap and migration script
  running, the app sees an empty Postgres. Routes that read assume
  some data exists. Mitigation: run migration script as part of the
  deploy sequence, before opening the app to users. Or: have the route
  handlers tolerate empty tables gracefully (return empty lists +
  banner that says "Run migration").
- **Data shape drift.** Migrating ~50+ schedule files needs careful
  field-by-field mapping. The migration script is the place to catch
  shape mismatches; it should fail loudly on unknown keys.
- **Odoo write paths in Phase 2.** Each entity is its own design call
  (which fields are writable, how to handle conflicts, how often the
  outbox drains). Out of scope here, but the schema is built for it.
- **Postgres add-on cost.** Railway's hobby plan includes Postgres up
  to a few GB. Our data fits in tens of MB even with years of history.
  No expected cost issue.
- **Connection pool sizing.** Railway's free Postgres has a connection
  limit (~22). Pool size 4–8 leaves headroom. If we ever hit limits,
  we shrink the pool.

## File touch list

- New: `src/zira_dashboard/db.py`
- New: `scripts/migrate_json_to_postgres.py`
- New: `tests/test_db.py`
- Modified: `src/zira_dashboard/app.py` — `init_pool()` + `bootstrap_schema()` on startup
- Modified: `src/zira_dashboard/staffing.py` — full rewrite of load/save fns
- Modified: `src/zira_dashboard/work_centers_store.py` — full rewrite
- Modified: `src/zira_dashboard/settings_store.py` — full rewrite
- Modified: `src/zira_dashboard/schedule_store.py` — full rewrite
- Modified: `src/zira_dashboard/layout_store.py` — full rewrite
- Modified: `src/zira_dashboard/widget_customizer.py` — full rewrite
- Modified: `src/zira_dashboard/skill_filter_store.py` — full rewrite
- Modified: `src/zira_dashboard/odoo_sync.py` — writes go to SQL
- Modified: `requirements.txt` — add `psycopg2-binary`
- Modified: `tests/test_*.py` — adjust fixtures to use a test schema or
  in-memory SQLite (TBD per test, smallest change wins)
- Modified: `docs/odoo-setup.md` — add Postgres add-on setup
- Updated: `.gitignore` — JSON files no longer relevant but keep
  patterns for safety
