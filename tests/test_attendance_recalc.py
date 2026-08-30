"""Targeted attendance-driven production recalculation worker."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import pytest


DAY = date(2026, 8, 20)
NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


class FakeCursor:
    def __init__(self, row=None):
        self.row = row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.row


class FakeDatabase:
    def __init__(self, claim_rows):
        self.claim_rows = list(claim_rows)
        self.cursors = []

    @contextmanager
    def cursor(self):
        row = self.claim_rows.pop(0) if not self.cursors and self.claim_rows else None
        cursor = FakeCursor(row)
        self.cursors.append(cursor)
        yield cursor

    @property
    def statements(self):
        return [statement for cursor in self.cursors for statement in cursor.executed]


def queue_row(*, attempt_count=0):
    return {
        "day": DAY,
        "reason": "Odoo attendance changed",
        "requested_at": NOW - timedelta(hours=2),
        "attempt_count": attempt_count,
    }


def test_process_claims_oldest_first_with_skip_locked_and_completes(monkeypatch):
    from zira_dashboard import _http_cache, attendance_recalc, db, precompute

    fake_db = FakeDatabase([queue_row()])
    monkeypatch.setattr(db, "cursor", fake_db.cursor)
    calls = []
    client = object()
    monkeypatch.setattr(
        precompute,
        "precompute_day",
        lambda day, production_client: calls.append((day, production_client))
        or {"day": day.isoformat(), "rows_written": 2},
    )
    monkeypatch.setattr(
        _http_cache, "invalidate_all_cache", lambda: calls.append(("invalidate",))
    )

    result = attendance_recalc.process_next(
        production_client=client, now_utc=NOW
    )

    claim_sql = fake_db.statements[0][0]
    assert "ORDER BY requested_at, day" in claim_sql
    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    assert "LEAST(300" in claim_sql and "power(2" in claim_sql
    assert calls == [(DAY, client), ("invalidate",)]
    assert result == attendance_recalc.RecalcResult(
        day=DAY,
        status="completed",
        attempt_count=1,
        rows_written=2,
        error=None,
    )
    assert any(
        "completed_at = %s" in sql and "last_error = NULL" in sql
        for sql, _params in fake_db.statements
    )


def test_failed_job_records_error_and_remains_retryable(monkeypatch):
    from zira_dashboard import attendance_recalc, db, precompute

    fake_db = FakeDatabase([queue_row(attempt_count=2)])
    monkeypatch.setattr(db, "cursor", fake_db.cursor)
    monkeypatch.setattr(
        precompute,
        "precompute_day",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("Zira unavailable")),
    )

    result = attendance_recalc.process_next(
        production_client=object(), now_utc=NOW
    )

    assert result == attendance_recalc.RecalcResult(
        day=DAY,
        status="failed",
        attempt_count=3,
        rows_written=0,
        error="Zira unavailable",
    )
    failure_updates = [
        (sql, params)
        for sql, params in fake_db.statements
        if "last_error = %s" in sql
    ]
    assert failure_updates
    failure_sql, failure_params = failure_updates[-1]
    assert "completed_at = NULL" in failure_sql
    assert failure_params[0] == "Zira unavailable"


def test_cache_refresh_failure_is_recorded_for_retry(monkeypatch):
    from zira_dashboard import _http_cache, attendance_recalc, db, precompute

    fake_db = FakeDatabase([queue_row()])
    monkeypatch.setattr(db, "cursor", fake_db.cursor)
    monkeypatch.setattr(
        precompute,
        "precompute_day",
        lambda day, _client: {"day": day.isoformat(), "rows_written": 1},
    )
    monkeypatch.setattr(
        _http_cache,
        "invalidate_all_cache",
        lambda: (_ for _ in ()).throw(RuntimeError("cache refresh failed")),
    )

    result = attendance_recalc.process_next(
        production_client=object(), now_utc=NOW
    )

    assert result is not None and result.status == "failed"
    assert result.error == "cache refresh failed"


def test_processes_only_one_local_day_per_tick_and_keeps_strict_marker(monkeypatch):
    from zira_dashboard import _http_cache, attendance_recalc, db, precompute

    second_day = DAY + timedelta(days=1)
    fake_db = FakeDatabase([queue_row()])
    monkeypatch.setattr(db, "cursor", fake_db.cursor)
    processed = []
    monkeypatch.setattr(
        precompute,
        "precompute_day",
        lambda day, _client: processed.append(day)
        or {"day": day.isoformat(), "rows_written": 1},
    )
    monkeypatch.setattr(_http_cache, "invalidate_all_cache", lambda: None)

    first = attendance_recalc.process_next(
        production_client=object(), now_utc=NOW
    )
    second = attendance_recalc.process_next(
        production_client=object(), now_utc=NOW
    )

    assert first is not None and first.day == DAY
    assert second is None
    assert processed == [DAY]
    sql = " ".join(statement for statement, _params in fake_db.statements)
    assert "DELETE FROM attendance_strict_days" not in sql
    assert second_day not in processed


def test_default_client_is_lazy_zira_dependency(monkeypatch):
    from zira_dashboard import _http_cache, attendance_recalc, db, deps, precompute

    fake_db = FakeDatabase([queue_row()])
    monkeypatch.setattr(db, "cursor", fake_db.cursor)
    expected_client = object()
    monkeypatch.setattr(deps, "client", expected_client)
    seen = []
    monkeypatch.setattr(
        precompute,
        "precompute_day",
        lambda day, client: seen.append((day, client))
        or {"day": day.isoformat(), "rows_written": 0},
    )
    monkeypatch.setattr(_http_cache, "invalidate_all_cache", lambda: None)

    attendance_recalc.process_next(now_utc=NOW)

    assert seen == [(DAY, expected_client)]


def test_recalculation_warmer_is_registered_every_fifteen_seconds(monkeypatch):
    from zira_dashboard import app as app_module
    from zira_dashboard import attendance_recalc

    entry = next(
        warmer
        for warmer in app_module._WARMERS
        if warmer[1] is app_module._tick_attendance_recalc
    )
    assert entry == ("attendance production recalc", app_module._tick_attendance_recalc, 15)

    calls = []
    monkeypatch.setattr(attendance_recalc, "process_next", lambda: calls.append(True))
    asyncio.run(app_module._tick_attendance_recalc())
    assert calls == [True]


def test_process_next_requires_aware_utc_now():
    from zira_dashboard import attendance_recalc

    with pytest.raises(ValueError, match="timezone-aware"):
        attendance_recalc.process_next(
            production_client=object(), now_utc=datetime(2026, 8, 21, 12)
        )
