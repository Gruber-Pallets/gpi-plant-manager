"""Tests for the self-correcting clock-in in timeclock_sync._retry_one.

Stubs odoo_client + db so no Odoo / Postgres is touched. The key behavior:
a clock-in whose employee already has an open Odoo attendance must NOT create
a duplicate — it adopts the open row instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from zira_dashboard import timeclock_sync


@pytest.fixture
def fake_db(monkeypatch):
    captured: dict = {"executes": []}
    monkeypatch.setattr(timeclock_sync.db, "query",
                        lambda sql, params=None: [])
    monkeypatch.setattr(timeclock_sync.db, "execute",
                        lambda sql, params=None: captured["executes"].append(
                            (sql, params)))
    return captured


def _row(action="clock_in"):
    return {"id": 1, "person_odoo_id": 5, "action": action,
            "wc_name": "Bay 3", "occurred_at": datetime(2026, 6, 1, 11, 0,
                                                         tzinfo=timezone.utc)}


def test_clock_in_creates_when_nothing_open(monkeypatch, fake_db):
    monkeypatch.setattr(timeclock_sync.odoo_client, "get_current_attendance",
                        MagicMock(return_value=None))
    create = MagicMock(return_value=88)
    monkeypatch.setattr(timeclock_sync.odoo_client, "clock_in", create)

    timeclock_sync._retry_one(_row())

    create.assert_called_once_with(5, "Bay 3",
                                   datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc))
    # _mark_synced UPDATE carries the new attendance id.
    upd = [e for e in fake_db["executes"] if "synced_to_odoo = TRUE" in e[0]]
    assert upd and upd[0][1][0] == 88


def test_clock_in_adopts_existing_open_no_duplicate(monkeypatch, fake_db):
    monkeypatch.setattr(timeclock_sync.odoo_client, "get_current_attendance",
                        MagicMock(return_value={"id": 99, "check_in": "x"}))
    create = MagicMock()
    monkeypatch.setattr(timeclock_sync.odoo_client, "clock_in", create)
    set_wc = MagicMock()
    monkeypatch.setattr(timeclock_sync.odoo_client, "set_attendance_wc", set_wc)

    timeclock_sync._retry_one(_row())

    create.assert_not_called()                  # no duplicate open attendance
    set_wc.assert_called_once_with(99, "Bay 3")  # label the adopted row
    upd = [e for e in fake_db["executes"] if "synced_to_odoo = TRUE" in e[0]]
    assert upd and upd[0][1][0] == 99            # adopted the existing id


def test_transfer_in_also_self_corrects(monkeypatch, fake_db):
    monkeypatch.setattr(timeclock_sync.odoo_client, "get_current_attendance",
                        MagicMock(return_value={"id": 99}))
    create = MagicMock()
    monkeypatch.setattr(timeclock_sync.odoo_client, "clock_in", create)
    monkeypatch.setattr(timeclock_sync.odoo_client, "set_attendance_wc",
                        MagicMock())

    timeclock_sync._retry_one(_row(action="transfer_in"))
    create.assert_not_called()


def test_odoo_attendance_warmer_is_coroutine():
    import inspect
    from zira_dashboard import app as app_module
    assert inspect.iscoroutinefunction(app_module._warm_odoo_attendance_loop)
