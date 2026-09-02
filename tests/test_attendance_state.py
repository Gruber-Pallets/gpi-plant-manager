"""Pure-logic tests for the reconciliation helpers. No DB/Odoo — the two
sources (snapshot + latest punch) are passed in / monkeypatched."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from zira_dashboard import attendance_state as ast


def test_latest_punch_queries_include_raw_punch_time(monkeypatch):
    statements = []
    monkeypatch.setattr(
        ast.db,
        "query",
        lambda sql, params=None: statements.append((sql, params)) or [],
    )

    assert ast.latest_punch(5) is None
    assert ast.latest_punches_bulk([5]) == {}

    assert len(statements) == 2
    assert all("occurred_at AS raw_occurred_at" in sql for sql, _params in statements)


def test_state_from_log_clocked_out_when_none_or_clockout():
    assert ast.state_from_log(None)["is_clocked_in"] is False
    out = {"action": "clock_out", "wc_name": None, "occurred_at": None,
           "odoo_attendance_id": None}
    assert ast.state_from_log(out)["is_clocked_in"] is False


def test_state_from_log_clocked_in_carries_wc():
    row = {"action": "clock_in", "wc_name": "Bay 3",
           "occurred_at": datetime(2026, 6, 2, 7, tzinfo=timezone.utc),
           "odoo_attendance_id": 55}
    s = ast.state_from_log(row)
    assert s["is_clocked_in"] is True and s["current_wc"] == "Bay 3"
    assert s["open_odoo_attendance_id"] == 55


def test_trust_local_unsynced_punch_wins():
    assert ast.trust_local({"synced_to_odoo": False}, datetime.now(timezone.utc)) is True


def test_trust_local_synced_before_refresh_yields_to_cache():
    synced = datetime(2026, 6, 2, 11, 0, tzinfo=timezone.utc)
    refreshed = synced + timedelta(seconds=30)
    latest = {"synced_to_odoo": True, "synced_at": synced}
    assert ast.trust_local(latest, refreshed) is False


def test_current_state_unsynced_autoout_reads_clocked_out(monkeypatch):
    # The race-guard the worker depends on: a just-written, still-unsynced
    # auto clock_out makes current_state report clocked-out even though the
    # cache still shows the morning attendance open.
    monkeypatch.setattr(ast.live_cache, "read_open_attendance",
                        lambda: ({"5": {"att_id": 1, "check_in": None, "wc_name": "Bay 3"}},
                                 datetime.now(timezone.utc)))
    monkeypatch.setattr(ast.live_cache, "is_stale", lambda _r: False)
    monkeypatch.setattr(ast, "latest_punch",
                        lambda pid: {"action": "clock_out", "wc_name": None,
                                     "occurred_at": None, "odoo_attendance_id": None,
                                     "synced_to_odoo": False, "synced_at": None})
    assert ast.current_state(5)["is_clocked_in"] is False
