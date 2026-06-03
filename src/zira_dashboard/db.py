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

from ._schema import SCHEMA_DDL


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
        cur.execute(SCHEMA_DDL)


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
