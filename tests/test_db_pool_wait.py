"""Bounded wait-for-connection in the DB pool.

psycopg2's ``ThreadedConnectionPool.getconn()`` raises ``PoolError`` the
instant ``maxconn`` connections are checked out — it never waits. Under
transient concurrency spikes (fan-out renders holding 8-10 conns each, plus
the background page-warmer rendering those same pages) that brief
over-subscription turned into user-visible 500s, which Railway's edge renders
as its "upstream error" page.

``db._getconn_blocking`` waits a bounded amount of time for a connection to be
returned before giving up. These tests inject a fake pool, so no Postgres is
required — they run everywhere, including CI without ``DATABASE_URL``.
"""
from __future__ import annotations

import time

import pytest
from psycopg2.pool import PoolError

from zira_dashboard import db


class _PoolFreesAfter:
    """getconn() raises PoolError for the first ``fail_times`` calls (pool
    exhausted), then returns a sentinel connection (a slot freed up)."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0
        self.conn = object()

    def getconn(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise PoolError("connection pool exhausted")
        return self.conn


class _AlwaysExhausted:
    def getconn(self):
        raise PoolError("connection pool exhausted")


def test_getconn_blocking_waits_then_returns_when_pool_frees():
    pool = _PoolFreesAfter(fail_times=2)
    conn = db._getconn_blocking(pool, timeout=1.0, poll=0.005)
    assert conn is pool.conn
    assert pool.calls == 3  # two exhausted retries, then success


def test_getconn_blocking_reraises_after_timeout_when_never_free():
    start = time.monotonic()
    with pytest.raises(PoolError):
        db._getconn_blocking(_AlwaysExhausted(), timeout=0.05, poll=0.01)
    elapsed = time.monotonic() - start
    # It waited up to the deadline rather than failing on the first call.
    assert elapsed >= 0.05
