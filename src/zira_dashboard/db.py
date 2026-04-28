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

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
from psycopg2.pool import ThreadedConnectionPool


_pool: Optional[ThreadedConnectionPool] = None


def init_pool(minconn: int = 1, maxconn: int = 8) -> None:
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
    if _pool is None:
        raise RuntimeError(
            "Connection pool not initialized. Call db.init_pool() first."
        )
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
-- People (employees)
CREATE TABLE IF NOT EXISTS people (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    employee_number TEXT,
    hire_date       DATE,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_people_active ON people (active);

-- Skills catalog
CREATE TABLE IF NOT EXISTS skills (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Many-to-many: people <-> skills
CREATE TABLE IF NOT EXISTS person_skills (
    person_id  TEXT NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    skill_id   TEXT NOT NULL REFERENCES skills (id) ON DELETE CASCADE,
    level      INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (person_id, skill_id)
);

CREATE INDEX IF NOT EXISTS idx_person_skills_skill ON person_skills (skill_id);

-- Work centers (machines / stations / lines)
CREATE TABLE IF NOT EXISTS work_centers (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    odoo_id       INTEGER,
    value_stream  TEXT,
    group_name    TEXT,
    capacity      INTEGER,
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    config        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_work_centers_active ON work_centers (active);
CREATE INDEX IF NOT EXISTS idx_work_centers_value_stream ON work_centers (value_stream);
CREATE INDEX IF NOT EXISTS idx_work_centers_group ON work_centers (group_name);

-- Skills required to staff a work center
CREATE TABLE IF NOT EXISTS work_center_required_skills (
    work_center_id TEXT NOT NULL REFERENCES work_centers (id) ON DELETE CASCADE,
    skill_id       TEXT NOT NULL REFERENCES skills (id) ON DELETE CASCADE,
    PRIMARY KEY (work_center_id, skill_id)
);

-- Default people assigned to a work center
CREATE TABLE IF NOT EXISTS work_center_default_people (
    work_center_id TEXT NOT NULL REFERENCES work_centers (id) ON DELETE CASCADE,
    person_id      TEXT NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    role           TEXT,
    PRIMARY KEY (work_center_id, person_id)
);

-- Logical groupings of work centers
CREATE TABLE IF NOT EXISTS groups (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Value streams (e.g. "Sawmill", "Pallet Repair")
CREATE TABLE IF NOT EXISTS value_streams (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Daily schedule documents (one row per date)
CREATE TABLE IF NOT EXISTS schedules (
    schedule_date DATE PRIMARY KEY,
    notes         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Person-to-work-center assignments inside a schedule
CREATE TABLE IF NOT EXISTS schedule_assignments (
    id             BIGSERIAL PRIMARY KEY,
    schedule_date  DATE NOT NULL REFERENCES schedules (schedule_date) ON DELETE CASCADE,
    person_id      TEXT NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    work_center_id TEXT REFERENCES work_centers (id) ON DELETE SET NULL,
    role           TEXT,
    hours          NUMERIC(5, 2),
    notes          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_schedule_assignments_date ON schedule_assignments (schedule_date);
CREATE INDEX IF NOT EXISTS idx_schedule_assignments_person ON schedule_assignments (person_id);
CREATE INDEX IF NOT EXISTS idx_schedule_assignments_wc ON schedule_assignments (work_center_id);

-- Time-off / absence rows tied to a daily schedule
CREATE TABLE IF NOT EXISTS schedule_time_off (
    id            BIGSERIAL PRIMARY KEY,
    schedule_date DATE NOT NULL REFERENCES schedules (schedule_date) ON DELETE CASCADE,
    person_id     TEXT NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    reason        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_schedule_time_off_date ON schedule_time_off (schedule_date);
CREATE INDEX IF NOT EXISTS idx_schedule_time_off_person ON schedule_time_off (person_id);

-- Per-work-center notes for a given schedule date
CREATE TABLE IF NOT EXISTS schedule_wc_notes (
    schedule_date  DATE NOT NULL REFERENCES schedules (schedule_date) ON DELETE CASCADE,
    work_center_id TEXT NOT NULL REFERENCES work_centers (id) ON DELETE CASCADE,
    note           TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (schedule_date, work_center_id)
);

-- Plant-wide note for a given schedule date
CREATE TABLE IF NOT EXISTS global_schedule (
    schedule_date DATE PRIMARY KEY REFERENCES schedules (schedule_date) ON DELETE CASCADE,
    note          TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Saved widget layouts per dashboard
CREATE TABLE IF NOT EXISTS widget_layouts (
    id           TEXT PRIMARY KEY,
    dashboard_id TEXT NOT NULL,
    name         TEXT NOT NULL,
    layout       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_widget_layouts_dashboard ON widget_layouts (dashboard_id);

-- Per-widget customization overrides
CREATE TABLE IF NOT EXISTS widget_customizations (
    id            TEXT PRIMARY KEY,
    widget_key    TEXT NOT NULL,
    customization JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_widget_customizations_key ON widget_customizations (widget_key);

-- Misc app key/value settings
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Outbox for reliable downstream sync (Odoo, etc.)
CREATE TABLE IF NOT EXISTS sync_outbox (
    id           BIGSERIAL PRIMARY KEY,
    target       TEXT NOT NULL,
    operation    TEXT NOT NULL,
    payload      JSONB NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sync_outbox_status ON sync_outbox (status);
CREATE INDEX IF NOT EXISTS idx_sync_outbox_target ON sync_outbox (target);
CREATE INDEX IF NOT EXISTS idx_sync_outbox_created ON sync_outbox (created_at);
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
