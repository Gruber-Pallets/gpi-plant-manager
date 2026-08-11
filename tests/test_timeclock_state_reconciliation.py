"""Tests for the timeclock punch-screen state reconciliation.

_current_state blends two local sources: the Odoo open-attendance snapshot
(live_cache.read_open_attendance) and the latest timeclock_punches_log row
(via db.query). These tests stub both, so no Odoo and no Postgres needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from zira_dashboard.routes import timeclock


def _now():
    return datetime.now(timezone.utc)


def _set_cache(monkeypatch, snapshot, refreshed_at):
    monkeypatch.setattr(timeclock.live_cache, "read_open_attendance",
                        lambda: (snapshot, refreshed_at))


def _set_latest_punch(monkeypatch, row):
    # _latest_punch issues exactly one db.query; return [row] or [].
    monkeypatch.setattr(timeclock.db, "query",
                        lambda sql, params=None: ([row] if row else []))


# ---- _trust_local (pure predicate) -------------------------------------

def test_trust_local_none_punch_false():
    assert timeclock._trust_local(None, _now()) is False


def test_trust_local_unsynced_true():
    punch = {"synced_to_odoo": False, "synced_at": None}
    assert timeclock._trust_local(punch, _now()) is True


def test_trust_local_synced_but_no_synced_at_true():
    punch = {"synced_to_odoo": True, "synced_at": None}
    assert timeclock._trust_local(punch, _now()) is True


def test_trust_local_cache_predates_sync_true():
    synced = _now()
    refreshed = synced - timedelta(seconds=10)  # cache older than the sync
    punch = {"synced_to_odoo": True, "synced_at": synced}
    assert timeclock._trust_local(punch, refreshed) is True


def test_trust_local_cache_after_sync_false():
    synced = _now() - timedelta(seconds=20)
    refreshed = _now()  # cache refreshed after the punch synced
    punch = {"synced_to_odoo": True, "synced_at": synced}
    assert timeclock._trust_local(punch, refreshed) is False


# ---- _current_state (full decision) ------------------------------------

def test_forgot_to_punch_in_added_in_odoo_shows_clock_out(monkeypatch):
    """No local punch, fresh cache shows them open → clocked in (clock-out)."""
    _set_cache(monkeypatch, {"5": {
        "att_id": 88, "check_in": "2026-06-01T11:00:00+00:00",
        "wc_name": None}}, _now())
    _set_latest_punch(monkeypatch, None)

    st = timeclock._current_state(5)
    assert st["is_clocked_in"] is True
    assert st["open_odoo_attendance_id"] == 88
    assert st["current_wc"] is None  # manual Odoo punch has no WC
    assert st["check_in_ts"] == datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc)


def test_auto_lunch_persists_reverse_mapped_app_work_center(monkeypatch):
    """The cache contains Task 3's app name, not Odoo's display label."""
    from types import SimpleNamespace

    from zira_dashboard import auto_lunch as al

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    now = datetime(2026, 6, 2, 11, 0, tzinfo=timezone.utc)
    persisted = []
    monkeypatch.setattr(al.live_cache, "is_stale", lambda _refreshed_at: False)
    monkeypatch.setattr(al.db, "cursor", lambda: Cursor())
    monkeypatch.setattr(al, "_write_auto_punch", lambda *_args, **_kwargs: 99)
    monkeypatch.setattr(
        al,
        "_upsert_run",
        lambda *_args, **kwargs: persisted.append(kwargs),
    )
    monkeypatch.setattr(al.timeclock_sync, "sync_one_by_id", lambda _punch_id: None)

    al._advance_person(
        5,
        now.date(),
        now,
        al.Window(now, now + timedelta(minutes=30)),
        set(),
        SimpleNamespace(observe_only=False),
        run=None,
        snapshot={"5": {
            "att_id": 88,
            "check_in": now.isoformat(),
            # Task 3 reverse-mapped Odoo's [41, "Repair #1"] to this app name.
            "wc_name": "Repair 1",
        }},
        refreshed_at=now,
        latest=None,
    )

    assert persisted[0]["wc_name"] == "Repair 1"
    assert persisted[0]["wc_name"] != "Repair #1"


def test_just_clocked_in_unsynced_stays_clocked_in(monkeypatch):
    """Race-guard: fresh kiosk punch not yet in the cache → trust local."""
    _set_cache(monkeypatch, {}, _now())  # cache doesn't show them yet
    _set_latest_punch(monkeypatch, {
        "action": "clock_in", "wc_name": "Bay 3",
        "occurred_at": _now(), "odoo_attendance_id": None,
        "synced_to_odoo": False, "synced_at": None})

    st = timeclock._current_state(5)
    assert st["is_clocked_in"] is True
    assert st["current_wc"] == "Bay 3"


def test_closed_in_odoo_shows_clock_in(monkeypatch):
    """Local says clocked in (synced long ago), fresh cache empty → clock-in."""
    _set_cache(monkeypatch, {}, _now())
    _set_latest_punch(monkeypatch, {
        "action": "clock_in", "wc_name": "Bay 3",
        "occurred_at": _now() - timedelta(hours=4),
        "odoo_attendance_id": 88, "synced_to_odoo": True,
        "synced_at": _now() - timedelta(hours=4)})

    st = timeclock._current_state(5)
    assert st["is_clocked_in"] is False


def test_stale_cache_falls_back_to_local(monkeypatch):
    """Cache older than is_stale threshold → use the local log."""
    _set_cache(monkeypatch, {}, _now() - timedelta(minutes=10))
    _set_latest_punch(monkeypatch, {
        "action": "clock_in", "wc_name": "Bay 7",
        "occurred_at": _now() - timedelta(hours=1),
        "odoo_attendance_id": 88, "synced_to_odoo": True,
        "synced_at": _now() - timedelta(hours=1)})

    st = timeclock._current_state(5)
    assert st["is_clocked_in"] is True       # from the local log, not the cache
    assert st["current_wc"] == "Bay 7"


def test_cold_cache_none_falls_back_to_local(monkeypatch):
    """Warmer never ran (snapshot None) → local log."""
    _set_cache(monkeypatch, None, None)
    _set_latest_punch(monkeypatch, None)
    st = timeclock._current_state(5)
    assert st["is_clocked_in"] is False
