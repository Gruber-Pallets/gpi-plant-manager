"""Postgres connection pool, helpers, and schema bootstrap.

Single point of access to the Railway-hosted Postgres database.

Usage:
    from zira_dashboard import db

    db.init_pool()             # call once at app startup
    db.bootstrap_schema()      # idempotent DDL — safe to call on every boot
    rows = db.query("SELECT * FROM people WHERE active = TRUE")
    db.execute("UPDATE people SET active = FALSE WHERE id = %s", (pid,))

    with db.cursor() as cur:
        cur.execute("INSERT INTO ...")
        # commits on clean exit, rolls back on exception, returns conn always

The module never auto-initializes — importing it has no side effects. The
caller (app startup, tests, scripts) is responsible for calling init_pool().
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterable, Optional, Sequence

from psycopg2.extras import RealDictCursor, execute_values
from psycopg2.pool import ThreadedConnectionPool


_pool: Optional[ThreadedConnectionPool] = None


def init_pool(minconn: int = 1, maxconn: int = 20) -> None:
    """Initialize the global connection pool. Idempotent — second call no-ops.

    Reads the connection string from the ``DATABASE_URL`` environment variable.
    Raises ``RuntimeError`` if it is not set.
    """
    global _pool
    if _pool is not None:
        return
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. Postgres connection cannot be initialized."
        )
    _pool = ThreadedConnectionPool(minconn, maxconn, dsn)


def shutdown_pool() -> None:
    """Close all pooled connections and reset the module state.

    Safe to call when no pool exists. After shutdown, ``init_pool()`` may be
    called again to start a fresh pool.
    """
    global _pool
    if _pool is None:
        return
    try:
        _pool.closeall()
    finally:
        _pool = None


def _get_pool() -> ThreadedConnectionPool:
    """Lazy-init: if no one has called init_pool() yet (CLI scripts, tests),
    do it now. App startup calls init_pool() explicitly via the lifespan
    hook for predictable pool sizing."""
    if _pool is None:
        init_pool()
    assert _pool is not None
    return _pool


@contextmanager
def cursor():
    """Yield a ``RealDictCursor`` inside a transaction.

    - Commits on clean exit.
    - Rolls back on any exception, then re-raises.
    - Always returns the connection to the pool.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    finally:
        pool.putconn(conn)


def query(sql: str, params: Optional[Sequence[Any]] = None) -> list[dict]:
    """Run a SELECT and return rows as a list of dicts."""
    with cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def execute(sql: str, params: Optional[Sequence[Any]] = None) -> None:
    """Run a single write statement in its own short transaction."""
    with cursor() as cur:
        cur.execute(sql, params)


def execute_many(sql: str, rows: Iterable[Sequence[Any]]) -> None:
    """Bulk-write helper. Uses psycopg2's executemany for now.

    For very large bulk inserts, callers may prefer to construct a single
    ``INSERT ... VALUES %s`` statement and use ``execute_values`` directly
    via the cursor() context manager.
    """
    rows = list(rows)
    if not rows:
        return
    with cursor() as cur:
        cur.executemany(sql, rows)


def bootstrap_schema() -> None:
    """Run the full schema DDL idempotently.

    Every CREATE statement uses IF NOT EXISTS, so this is safe to call on
    every application boot.
    """
    with cursor() as cur:
        cur.execute(_SCHEMA_DDL)


_SCHEMA_DDL = """
-- HR-mastered entities (mirrored from Odoo via TTL sync) ----------------

CREATE TABLE IF NOT EXISTS people (
  id              SERIAL PRIMARY KEY,
  odoo_id         INTEGER UNIQUE,
  name            TEXT NOT NULL UNIQUE,
  active          BOOLEAN NOT NULL DEFAULT TRUE,
  reserve         BOOLEAN NOT NULL DEFAULT FALSE,
  last_pulled_at  TIMESTAMPTZ,
  last_pushed_at  TIMESTAMPTZ,
  local_dirty     BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS people_active_idx ON people (active);

CREATE TABLE IF NOT EXISTS skills (
  id              SERIAL PRIMARY KEY,
  odoo_id         INTEGER UNIQUE,
  name            TEXT NOT NULL UNIQUE,
  skill_type      TEXT NOT NULL,
  sort_order      INTEGER NOT NULL DEFAULT 0,
  last_pulled_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS person_skills (
  person_id       INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  skill_id        INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  level           SMALLINT NOT NULL DEFAULT 0,
  last_pulled_at  TIMESTAMPTZ,
  last_pushed_at  TIMESTAMPTZ,
  local_dirty     BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY (person_id, skill_id)
);

-- Work centers ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS work_centers (
  id              SERIAL PRIMARY KEY,
  odoo_id         INTEGER UNIQUE,
  name            TEXT NOT NULL UNIQUE,
  meter_id        TEXT,
  category        TEXT NOT NULL,
  cell            TEXT,
  value_stream    TEXT,
  min_ops         INTEGER NOT NULL DEFAULT 1,
  max_ops         INTEGER,
  goal_per_day_override INTEGER,
  group_name      TEXT,
  note            TEXT,
  last_pulled_at  TIMESTAMPTZ,
  last_pushed_at  TIMESTAMPTZ,
  local_dirty     BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS work_center_required_skills (
  wc_id           INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE CASCADE,
  skill_id        INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
  PRIMARY KEY (wc_id, skill_id)
);

CREATE TABLE IF NOT EXISTS work_center_default_people (
  wc_id           INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE CASCADE,
  person_id       INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  sort_order      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (wc_id, person_id)
);

CREATE TABLE IF NOT EXISTS groups (
  name            TEXT PRIMARY KEY,
  goal_per_day_override INTEGER
);

CREATE TABLE IF NOT EXISTS value_streams (
  name            TEXT PRIMARY KEY,
  goal_per_day_override INTEGER
);

-- App-specific (not mirrored anywhere) ---------------------------------

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
  day             DATE NOT NULL REFERENCES schedules(day) ON DELETE CASCADE,
  wc_id           INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE CASCADE,
  person_id       INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  sort_order      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, wc_id, person_id)
);
CREATE INDEX IF NOT EXISTS schedule_assignments_day_idx ON schedule_assignments(day);

-- schedule_time_off: removed (sub-project #2 — time-off now sourced live
-- from StratusTime, not stored locally). Drop the orphan table on bootstrap.
DROP TABLE IF EXISTS schedule_time_off;

CREATE TABLE IF NOT EXISTS schedule_wc_notes (
  day             DATE NOT NULL REFERENCES schedules(day) ON DELETE CASCADE,
  wc_id           INTEGER NOT NULL REFERENCES work_centers(id) ON DELETE CASCADE,
  note            TEXT NOT NULL,
  PRIMARY KEY (day, wc_id)
);

-- Retro time-windowed WC attributions: when a metered WC produced units but
-- had no one scheduled there, the user can attribute the production to the
-- person who actually worked it. Used by attribute_for_day so leaderboards
-- and dashboards pick up the credit. No FK on day -- attribution can predate
-- the schedule entry.
CREATE TABLE IF NOT EXISTS wc_time_attributions (
  id              BIGSERIAL PRIMARY KEY,
  day             DATE NOT NULL,
  wc_name         TEXT NOT NULL,
  person_name     TEXT NOT NULL,
  start_utc       TIMESTAMPTZ NOT NULL,
  end_utc         TIMESTAMPTZ NOT NULL,
  source          TEXT NOT NULL DEFAULT 'manual',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS wc_time_attributions_day_idx ON wc_time_attributions(day);
CREATE INDEX IF NOT EXISTS wc_time_attributions_day_wc_idx ON wc_time_attributions(day, wc_name);

-- Late / absence overrides for the Late/Absence Report ----------------
-- manual_absences: marks a scheduled person as Absent for a single day
-- (manager-declared via the Late/Absence Report). Layered into the
-- StratusTime time-off list so they drop out of Unscheduled + picker.
-- cleared_time_off: per-day, per-request opt-out for StratusTime
-- partial-day off entries. When a StratusTime PTO/Early-Leave request
-- is filed but the person actually worked through it (Jose Luis case),
-- the user can clear that request for the day. Doesn't touch StratusTime.
CREATE TABLE IF NOT EXISTS cleared_time_off (
  day            DATE NOT NULL,
  request_id     BIGINT NOT NULL,
  declared_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, request_id)
);
CREATE INDEX IF NOT EXISTS cleared_time_off_day_idx ON cleared_time_off(day);

-- cleared_non_work_shifts: per-day, per-emp opt-out for StratusTime
-- non-work-shift entries (manager-entered Unpaid Time, etc.) that don't
-- have a request_id. Same idea as cleared_time_off but keyed by emp_id
-- because the V1 punch endpoint doesn't expose a stable id per entry.
CREATE TABLE IF NOT EXISTS cleared_non_work_shifts (
  day            DATE NOT NULL,
  emp_id         TEXT NOT NULL,
  declared_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, emp_id)
);
CREATE INDEX IF NOT EXISTS cleared_non_work_shifts_day_idx ON cleared_non_work_shifts(day);

-- cleared_partials_by_name: clear key on (day, name). Catch-all for any
-- partial entry the user wants to suppress, regardless of source —
-- works even when the underlying StratusTime entry has neither a
-- request_id nor a usable emp_id (which is why the previous (day,
-- request_id) and (day, emp_id) approaches missed Jose Luis's case).
-- Names align with the scheduler's roster names.
CREATE TABLE IF NOT EXISTS cleared_partials_by_name (
  day            DATE NOT NULL,
  name           TEXT NOT NULL,
  declared_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, name)
);
CREATE INDEX IF NOT EXISTS cleared_partials_by_name_day_idx ON cleared_partials_by_name(day);

CREATE TABLE IF NOT EXISTS manual_absences (
  day            DATE NOT NULL,
  emp_id         TEXT NOT NULL,
  name           TEXT NOT NULL,
  declared_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, emp_id)
);
CREATE INDEX IF NOT EXISTS manual_absences_day_idx ON manual_absences(day);

-- late_snoozes: silences a person from the Late/Absence Report until
-- `until_utc`. After expiry the report re-checks them automatically.
CREATE TABLE IF NOT EXISTS late_snoozes (
  day            DATE NOT NULL,
  emp_id         TEXT NOT NULL,
  name           TEXT NOT NULL,
  until_utc      TIMESTAMPTZ NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (day, emp_id)
);
CREATE INDEX IF NOT EXISTS late_snoozes_day_idx ON late_snoozes(day);

CREATE TABLE IF NOT EXISTS global_schedule (
  id              INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  shift_start     TIME NOT NULL,
  shift_end       TIME NOT NULL,
  work_weekdays   INTEGER[] NOT NULL,
  breaks          JSONB NOT NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS widget_layouts (
  page            TEXT PRIMARY KEY,
  layout          JSONB NOT NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS widget_customizations (
  page            TEXT NOT NULL,
  widget_id       TEXT NOT NULL,
  customizations  JSONB NOT NULL,
  PRIMARY KEY (page, widget_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
  key             TEXT PRIMARY KEY,
  value           JSONB NOT NULL,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Outbox for future two-way sync (not actively drained in Phase 1) ----

CREATE TABLE IF NOT EXISTS sync_outbox (
  id              BIGSERIAL PRIMARY KEY,
  kind            TEXT NOT NULL,
  entity_id       INTEGER,
  action          TEXT NOT NULL,
  payload         JSONB NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending',
  attempts        INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  pushed_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS sync_outbox_status_idx ON sync_outbox(status, created_at);

-- Saved Views for the People Matrix (filter bundles) ------------------

CREATE TABLE IF NOT EXISTS skill_matrix_views (
  id              SERIAL PRIMARY KEY,
  name            TEXT NOT NULL UNIQUE,
  is_default      BOOLEAN NOT NULL DEFAULT FALSE,
  hidden_skills   TEXT[]  NOT NULL DEFAULT '{}',
  visible_people  TEXT[],
  active_filter   TEXT NOT NULL DEFAULT 'active'
                  CHECK (active_filter IN ('active','inactive','all')),
  reserve_filter  TEXT NOT NULL DEFAULT 'all'
                  CHECK (reserve_filter IN ('include','exclude','only','all')),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS skill_matrix_views_default_idx
  ON skill_matrix_views (is_default) WHERE is_default = TRUE;

-- Per-WC display settings for the leaderboards page ------------------

CREATE TABLE IF NOT EXISTS leaderboard_wc_settings (
  kind         TEXT NOT NULL DEFAULT 'wc',
  wc_name      TEXT NOT NULL,
  sort_order   INTEGER NOT NULL DEFAULT 0,
  is_inactive  BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, wc_name)
);

-- Idempotent: add `kind` column to a pre-existing table that has the
-- legacy single-column PK on wc_name.
ALTER TABLE leaderboard_wc_settings ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'wc';

-- Persistent cache for past-day Zira leaderboard results -------------
-- Past-day production is immutable; survive Railway redeploys without
-- re-paying the Zira API cost. Today's data stays in-process only.

CREATE TABLE IF NOT EXISTS zira_daily_cache (
  meter_id    TEXT NOT NULL,
  day         DATE NOT NULL,
  payload     JSONB NOT NULL,
  computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (meter_id, day)
);
CREATE INDEX IF NOT EXISTS idx_zira_daily_cache_day ON zira_daily_cache(day);

-- Migrate single-column PK to composite (kind, wc_name) when needed.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'leaderboard_wc_settings_pkey'
      AND conrelid = 'leaderboard_wc_settings'::regclass
  ) AND NOT EXISTS (
    SELECT 1 FROM pg_index i
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
    WHERE i.indrelid = 'leaderboard_wc_settings'::regclass
      AND i.indisprimary
      AND a.attname = 'kind'
  ) THEN
    ALTER TABLE leaderboard_wc_settings DROP CONSTRAINT leaderboard_wc_settings_pkey;
    ALTER TABLE leaderboard_wc_settings ADD PRIMARY KEY (kind, wc_name);
  END IF;
END $$;
"""


# Re-exported helpers in case callers want to use them directly via this module.
__all__ = [
    "init_pool",
    "shutdown_pool",
    "cursor",
    "query",
    "execute",
    "execute_many",
    "bootstrap_schema",
    "RealDictCursor",
    "execute_values",
]
