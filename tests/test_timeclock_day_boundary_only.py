"""Active-live Timeclock owns only the workday boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from zira_dashboard import timeclock_sync
from zira_dashboard.app import app
from zira_dashboard.routes import timeclock


client = TestClient(app)
PERSON = {
    "id": 1,
    "name": "Maria Garcia",
    "odoo_id": 5,
    "wage_type": "hourly",
    "spanish_level": 3,
}
PUNCH_AT = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _wire_person(monkeypatch, *, active: bool) -> None:
    monkeypatch.setattr(timeclock, "_verify_token", lambda _token: 1)
    monkeypatch.setattr(timeclock, "_mint_token", lambda _person_id: "fresh-token")
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _person_id: PERSON)
    monkeypatch.setattr(timeclock, "_time_off_redirect_if_salaried", lambda *_args: None)
    monkeypatch.setattr(timeclock, "_live_location_active", lambda: active)


def _wire_dashboard(monkeypatch, *, active: bool, clocked_in: bool) -> None:
    _wire_person(monkeypatch, active=active)
    monkeypatch.setattr(
        timeclock,
        "_current_state",
        lambda _odoo_id: {
            "is_clocked_in": clocked_in,
            "current_wc": "Bay 3" if clocked_in else None,
            "check_in_ts": PUNCH_AT if clocked_in else None,
        },
    )
    monkeypatch.setattr(timeclock, "_scheduled_wc_for", lambda _name: "Bay 3")
    monkeypatch.setattr(timeclock, "_approved_full_day_leave_today", lambda _odoo_id: None)
    monkeypatch.setattr(timeclock, "_sync_error_warning", lambda _odoo_id: None)
    monkeypatch.setattr(timeclock, "_saturday_commitment_context", lambda _person_id: None)
    monkeypatch.setattr(timeclock, "_time_off_enabled", lambda: False)


def test_active_live_clock_in_queues_a_day_boundary_without_work_center(monkeypatch):
    _wire_person(monkeypatch, active=True)
    monkeypatch.setattr(timeclock, "_approved_full_day_leave_today", lambda _odoo_id: None)
    opened = []
    monkeypatch.setattr(
        timeclock,
        "_open_log_row",
        lambda *args: opened.append(args) or (71, PUNCH_AT),
    )
    monkeypatch.setattr(timeclock, "_fmt_time", lambda _value: "7:00 AM")
    queued = []
    monkeypatch.setattr(timeclock.timeclock_sync, "sync_one_by_id", queued.append)

    response = client.post("/timeclock/clock-in/token", data={})

    assert response.status_code == 200
    assert opened == [(5, "clock_in", None)]
    assert queued == [71]
    assert "Clocked in" in response.text
    assert "Bay 3" not in response.text


def test_active_live_clock_in_retry_adopts_open_row_without_rewriting_lukes_work_center(
    monkeypatch,
):
    monkeypatch.setattr(
        timeclock_sync.odoo_client,
        "get_current_attendance",
        lambda _person_id: {"id": 99, "wc_name": "Luke Bay 8"},
    )
    create = MagicMock()
    set_wc = MagicMock()
    monkeypatch.setattr(timeclock_sync.odoo_client, "clock_in", create)
    monkeypatch.setattr(timeclock_sync.odoo_client, "set_attendance_wc", set_wc)
    marked = []
    monkeypatch.setattr(timeclock_sync, "_mark_synced", lambda *args: marked.append(args))

    timeclock_sync._retry_one(
        {
            "id": 71,
            "person_odoo_id": 5,
            "action": "clock_in",
            "wc_name": None,
            "occurred_at": PUNCH_AT,
        }
    )

    create.assert_not_called()
    set_wc.assert_not_called()
    assert marked == [(71, 99)]


def test_active_live_clock_out_closes_all_open_rows_at_one_timestamp_before_sync(
    monkeypatch,
):
    order = []
    monkeypatch.setattr(
        timeclock_sync.odoo_client,
        "close_all_open_attendance_rows",
        lambda person_id, ts: order.append(("close_all", person_id, ts)) or (81, 82),
    )
    monkeypatch.setattr(
        timeclock_sync,
        "_mark_synced",
        lambda log_id, attendance_id: order.append(("mark", log_id, attendance_id)),
    )

    timeclock_sync._retry_one(
        {
            "id": 72,
            "person_odoo_id": 5,
            "action": "clock_out",
            "wc_name": None,
            "occurred_at": PUNCH_AT,
            "close_all_open_rows": True,
        }
    )

    assert order == [
        ("close_all", 5, PUNCH_AT),
        ("mark", 72, 82),
    ]


def test_active_live_clock_out_stays_unsynced_when_close_all_verification_fails(
    monkeypatch,
):
    monkeypatch.setattr(
        timeclock_sync.odoo_client,
        "close_all_open_attendance_rows",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("row 82 is still open")),
    )
    mark = MagicMock()
    monkeypatch.setattr(timeclock_sync, "_mark_synced", mark)

    with pytest.raises(RuntimeError, match="still open"):
        timeclock_sync._retry_one(
            {
                "id": 72,
                "person_odoo_id": 5,
                "action": "clock_out",
                "wc_name": None,
                "occurred_at": PUNCH_AT,
                "close_all_open_rows": True,
            }
        )

    mark.assert_not_called()


def test_pre_activation_clock_out_keeps_legacy_single_current_row(monkeypatch):
    monkeypatch.setattr(
        timeclock_sync.odoo_client,
        "get_current_attendance",
        lambda _person_id: {"id": 81},
    )
    closed = []
    monkeypatch.setattr(
        timeclock_sync.odoo_client,
        "clock_out",
        lambda attendance_id, ts: closed.append((attendance_id, ts)),
    )
    monkeypatch.setattr(
        timeclock_sync.odoo_client,
        "close_all_open_attendance_rows",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("legacy clock-out must close only the current row")
        ),
    )
    marked = []
    monkeypatch.setattr(timeclock_sync, "_mark_synced", lambda *args: marked.append(args))

    timeclock_sync._retry_one(
        {
            "id": 72,
            "person_odoo_id": 5,
            "action": "clock_out",
            "wc_name": None,
            "occurred_at": PUNCH_AT,
            "close_all_open_rows": False,
        }
    )

    assert closed == [(81, PUNCH_AT)]
    assert marked == [(72, 81)]


@pytest.mark.parametrize(
    ("accepted_live", "rollout_after_enqueue", "expected_close_all"),
    [(True, False, True), (False, True, False)],
)
def test_clock_out_retry_keeps_the_ownership_intent_from_punch_acceptance(
    monkeypatch, accepted_live, rollout_after_enqueue, expected_close_all
):
    """Activation or rollback after enqueue must not change punch semantics."""
    from zira_dashboard import rounding

    _wire_person(monkeypatch, active=accepted_live)
    inserted = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            inserted.append((" ".join(sql.split()), params))

        def fetchone(self):
            return {"id": 72, "occurred_at": PUNCH_AT}

    monkeypatch.setattr(timeclock.db, "cursor", lambda: Cursor())
    monkeypatch.setattr(timeclock.db, "query", lambda *_args: [])
    monkeypatch.setattr(timeclock.db, "execute", lambda *_args: None)
    monkeypatch.setattr(timeclock, "_fmt_time", lambda _value: "7:00 AM")
    monkeypatch.setattr(timeclock, "_approved_full_day_leave_today", lambda _id: None)
    monkeypatch.setattr(timeclock, "_sync_error_warning", lambda _id: None)
    monkeypatch.setattr(timeclock, "_windows_for_day", lambda *_args: object())
    monkeypatch.setattr(timeclock, "_hours_for_punch", lambda *_args: (None, None))
    monkeypatch.setattr(timeclock, "_effective_punch_wc", lambda *_args: None)
    monkeypatch.setattr(rounding, "apply_rounding", lambda *_args: PUNCH_AT)

    log_id, _ = timeclock._open_log_row(
        5,
        "clock_out",
        None,
        close_all_open_rows=accepted_live,
    )
    punch_insert = inserted[0]
    persisted_intent = punch_insert[1][-1]
    assert persisted_intent is expected_close_all
    assert rollout_after_enqueue is not accepted_live

    close_all = MagicMock(return_value=(81, 82))
    single = MagicMock(return_value={"id": 81})
    monkeypatch.setattr(
        timeclock_sync.odoo_client, "close_all_open_attendance_rows", close_all
    )
    monkeypatch.setattr(timeclock_sync.odoo_client, "get_current_attendance", single)
    monkeypatch.setattr(timeclock_sync.odoo_client, "clock_out", MagicMock())
    monkeypatch.setattr(timeclock_sync, "_mark_synced", MagicMock())

    timeclock_sync._retry_one(
        {
            "id": log_id,
            "person_odoo_id": 5,
            "action": "clock_out",
            "wc_name": None,
            "occurred_at": PUNCH_AT,
            "close_all_open_rows": persisted_intent,
        }
    )

    assert close_all.called is expected_close_all
    assert single.called is (not expected_close_all)


@pytest.mark.parametrize("clocked_in", [False, True])
def test_active_live_dashboard_has_no_picker_or_transfer(monkeypatch, clocked_in):
    _wire_dashboard(monkeypatch, active=True, clocked_in=clocked_in)

    response = client.get("/timeclock/dashboard/token")

    assert response.status_code == 200
    assert "/timeclock/pick-wc/" not in response.text
    assert ">Transfer<" not in response.text
    assert 'name="wc_name"' not in response.text
    if clocked_in:
        assert "/timeclock/clock-out/" in response.text
    else:
        assert "/timeclock/clock-in/" in response.text


def test_active_live_dashboard_shows_owned_source_unavailable_without_punch_choice(
    monkeypatch,
):
    _wire_dashboard(monkeypatch, active=True, clocked_in=False)
    monkeypatch.setattr(
        timeclock,
        "_current_state",
        lambda _odoo_id: {
            "is_clocked_in": None,
            "current_wc": None,
            "check_in_ts": None,
            "attendance_source_unavailable": True,
            "attendance_source_stale": False,
        },
    )

    response = client.get("/timeclock/dashboard/token")

    assert response.status_code == 200
    assert "Odoo attendance is unavailable" in response.text
    assert "/timeclock/clock-in/" not in response.text
    assert "/timeclock/clock-out/" not in response.text


def test_active_live_dashboard_shows_stale_unknown_without_punch_choice(monkeypatch):
    _wire_dashboard(monkeypatch, active=True, clocked_in=False)
    monkeypatch.setattr(
        timeclock,
        "_current_state",
        lambda _odoo_id: {
            "is_clocked_in": None,
            "current_wc": None,
            "check_in_ts": None,
            "attendance_source_unavailable": False,
            "attendance_source_stale": True,
            "attendance_source_error": "incremental sync failed",
        },
    )

    response = client.get("/timeclock/dashboard/token")

    assert response.status_code == 200
    assert "Odoo attendance is stale" in response.text
    assert "Clock state unavailable" in response.text
    assert "/timeclock/clock-in/" not in response.text
    assert "/timeclock/clock-out/" not in response.text


def test_active_live_dashboard_exposes_fresh_source_error(monkeypatch):
    _wire_dashboard(monkeypatch, active=True, clocked_in=True)
    monkeypatch.setattr(
        timeclock,
        "_current_state",
        lambda _odoo_id: {
            "is_clocked_in": True,
            "current_wc": "Bay 8",
            "check_in_ts": PUNCH_AT,
            "attendance_source_unavailable": False,
            "attendance_source_stale": False,
            "attendance_source_error": "incremental sync failed",
        },
    )

    response = client.get("/timeclock/dashboard/token")

    assert response.status_code == 200
    assert "Odoo attendance sync reported a problem" in response.text
    assert "last verified state" in response.text


@pytest.mark.parametrize("rollout", ["off", "shadow", "pending-live"])
def test_pre_activation_dashboard_keeps_picker_and_transfer(monkeypatch, rollout):
    del rollout  # Documents each saved state whose activation predicate is false.
    _wire_dashboard(monkeypatch, active=False, clocked_in=True)

    response = client.get("/timeclock/dashboard/token")

    assert response.status_code == 200
    assert "/timeclock/pick-wc/" in response.text
    assert "Transfer" in response.text


def test_active_live_leave_override_confirmation_carries_no_work_center(monkeypatch):
    _wire_person(monkeypatch, active=True)
    monkeypatch.setattr(
        timeclock,
        "_approved_full_day_leave_today",
        lambda _odoo_id: {"id": 4, "odoo_leave_id": 44},
    )

    response = client.post("/timeclock/clock-in/token", data={})

    assert response.status_code == 200
    assert "approved time off" in response.text.lower()
    assert 'name="wc_name"' not in response.text
    assert "work center" not in response.text.lower()


@pytest.mark.parametrize(
    ("method", "path", "data"),
    [
        ("get", "/timeclock/pick-wc/token", None),
        ("post", "/timeclock/transfer/token", {"new_wc_name": "Bay 8"}),
    ],
)
def test_active_live_legacy_location_routes_return_bilingual_gone(
    monkeypatch, method, path, data
):
    _wire_person(monkeypatch, active=True)

    response = client.request(method, path, data=data)

    assert response.status_code == 410
    assert "plant-floor app" in response.text
    assert "aplicación de planta" in response.text


def test_pre_activation_legacy_location_routes_remain_available(monkeypatch):
    _wire_person(monkeypatch, active=False)
    monkeypatch.setattr(
        timeclock,
        "_wc_list",
        lambda: [{"name": "Bay 3", "bay": "3", "department": "Assembly"}],
    )

    response = client.get("/timeclock/pick-wc/token")

    assert response.status_code == 200
    assert "Bay 3" in response.text
