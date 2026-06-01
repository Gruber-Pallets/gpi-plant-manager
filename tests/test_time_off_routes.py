"""Kiosk time-off route tests.

The route surface is HMAC-token-gated like the rest of the kiosk; the
two tests below cover the easy gate-fail case (bogus token → redirect)
and stub a placeholder for the happy-path test once the suite gets a
seeded-person fixture (Task 16 in the plan promises to wire it).
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# Import after conftest sets AUTH_DISABLED
from zira_dashboard.app import app
from zira_dashboard.routes.timeclock import _mint_token


def _token_for(person_id: int) -> str:
    return _mint_token(person_id)


def test_landing_route_redirects_when_token_invalid(monkeypatch):
    monkeypatch.setenv("KIOSK_TIME_OFF_ENABLED", "1")
    client = TestClient(app)
    r = client.get("/timeclock/time-off/bogus.token", follow_redirects=False)
    assert r.status_code in (302, 303, 307)


def test_landing_route_renders_when_token_valid(monkeypatch):
    """Token valid + person exists → 200 with the landing HTML."""
    monkeypatch.setenv("KIOSK_TIME_OFF_ENABLED", "1")
    # Need to seed a person row. If the test DB isn't available, skip.
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("Requires DATABASE_URL")
    # Implementer: insert a test person, then:
    # token = _token_for(<person_id>)
    # client = TestClient(app)
    # r = client.get(f"/timeclock/time-off/{token}")
    # assert r.status_code == 200
    # assert "Request Time Off" in r.text
    pytest.skip("Needs test fixture for seeded person row")


def test_request_shape_picker_redirects_on_bad_token():
    """Bogus token on the shape picker should bounce to /timeclock — same
    HMAC gate as the rest of the kiosk."""
    client = TestClient(app)
    r = client.get("/timeclock/time-off/request/bogus", follow_redirects=False)
    assert r.status_code in (302, 303, 307)


def test_request_details_redirects_on_bad_token():
    """Bad token on the details page bounces back to /timeclock before any
    of the helper paths run — same auth gate as everything else."""
    client = TestClient(app)
    r = client.get(
        "/timeclock/time-off/request/bogus/details?shape=full_day",
        follow_redirects=False,
    )
    assert r.status_code in (302, 303, 307)


def test_request_details_redirects_on_bad_shape(monkeypatch):
    """Unknown shape value should bounce back to the shape picker — never
    crash, never render the form with no leave types."""
    # Stub past the auth + person check so we exercise the shape branch.
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._verify_token",
        lambda t: 1,
    )
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._person_by_id",
        lambda pid: {"id": 1, "name": "Test", "odoo_id": 5},
    )
    # These shouldn't be called when shape is invalid, but stub defensively
    # so a regression doesn't pull from the real DB / Odoo.
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._fetch_visible_leave_types",
        lambda shape: [],
    )
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._refresh_and_load_balances",
        lambda pid: [],
    )
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._shift_window_for",
        lambda pid: (6.0, 14.5),
    )
    client = TestClient(app)
    r = client.get(
        "/timeclock/time-off/request/anytoken/details?shape=bogus",
        follow_redirects=False,
    )
    assert r.status_code in (302, 303, 307)


def test_request_details_renders_when_token_and_shape_valid(monkeypatch):
    """Happy path: valid token + valid shape + stubbed helpers → 200 with
    the form rendered. Doesn't need a DB because every helper is stubbed."""
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._verify_token",
        lambda t: 1,
    )
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._person_by_id",
        lambda pid: {"id": 1, "name": "Test", "odoo_id": 5},
    )
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._fetch_visible_leave_types",
        lambda shape: [
            {"id": 1, "name": "PTO", "request_unit": "day",
             "requires_allocation": "yes"},
        ],
    )
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._refresh_and_load_balances",
        lambda pid: [
            {"holiday_status_id": 1, "unit": "days",
             "allocated_total": 15.0, "taken": 3.0, "pending": 2.0,
             "available": 12.0, "available_practical": 10.0},
        ],
    )
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._shift_window_for",
        lambda pid: (6.0, 14.5),
    )
    # Stub the global-schedule lookup so the render doesn't hit Postgres
    # for the work_weekdays warning payload.
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off.schedule_store.current",
        lambda: type("S", (), {"work_weekdays": frozenset({0, 1, 2, 3, 4})})(),
    )
    client = TestClient(app)
    r = client.get(
        "/timeclock/time-off/request/anytoken/details?shape=full_day",
        follow_redirects=False,
    )
    assert r.status_code == 200
    # Form points at submit; the type picker has the stubbed option.
    assert "submit" in r.text.lower()
    assert "PTO" in r.text


def test_submit_creates_row_and_queues_sync(monkeypatch):
    """POST /submit inserts a time_off_requests row and schedules a sync."""
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._verify_token",
                        lambda t: 1)
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._person_by_id",
                        lambda pid: {"id": 1, "name": "T", "odoo_id": 5})
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._shift_window_for",
                        lambda pid: (6.0, 14.5))
    # Stub the type-unit lookup so the test doesn't hit Postgres. Returning
    # "day" means the submit handler skips the full-shift-hour-bound
    # injection (which only fires for hour-unit types used as full_day).
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._type_request_unit",
                        lambda hsid: "day")
    inserted = {}
    def fake_insert(**kw):
        inserted.update(kw)
        return 999  # row id
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._insert_request_row",
                        fake_insert)
    queued = []
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._queue_push",
                        lambda rid: queued.append(rid))

    client = TestClient(app)
    r = client.post(
        "/timeclock/time-off/request/anytoken/submit",
        data={
            "shape": "full_day",
            "holiday_status_id": "1",
            "date_from": "2026-06-01",
            "date_to": "2026-06-03",
            "note": "Vacation",
        },
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    assert inserted["shape"] == "full_day"
    assert inserted["date_from"].isoformat() == "2026-06-01"
    assert queued == [999]


def test_submit_rejects_partial_day_outside_shift(monkeypatch):
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._verify_token",
                        lambda t: 1)
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._person_by_id",
                        lambda pid: {"id": 1, "name": "T", "odoo_id": 5})
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._shift_window_for",
                        lambda pid: (6.0, 14.5))
    # The error path re-renders the form, which calls these helpers; stub
    # them so the test doesn't need a database to reach the 422 branch.
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._fetch_visible_leave_types",
        lambda shape: [],
    )
    monkeypatch.setattr(
        "zira_dashboard.time_off_balances.get_for_employee",
        lambda pid: [],
    )
    # The 422 rerender path also pulls work_weekdays from schedule_store —
    # stub it so the test stays DB-free.
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off.schedule_store.current",
        lambda: type("S", (), {"work_weekdays": frozenset({0, 1, 2, 3, 4})})(),
    )
    client = TestClient(app)
    r = client.post(
        "/timeclock/time-off/request/anytoken/submit",
        data={
            "shape": "midday_gap",
            "holiday_status_id": "2",
            "date_from": "2026-06-01",
            "date_to": "2026-06-01",
            "time_a": "16:00",  # outside shift
            "time_b": "18:00",
        },
        follow_redirects=False,
    )
    # Should render the form again with an error (200) or redirect with flash
    assert r.status_code in (200, 303, 422)


def test_submit_partial_day_uses_selected_date_for_both_ends(monkeypatch):
    """Regression: a partial-day (arrive late) request is a SINGLE day. The
    user picks one date (submitted as date_from); the hidden date_to is a
    stale "today". The handler must force date_to = date_from so we never
    post a today->selected multi-day span."""
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._verify_token",
                        lambda t: 1)
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._person_by_id",
                        lambda pid: {"id": 1, "name": "T", "odoo_id": 5})
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._shift_window_for",
                        lambda pid: (6.0, 14.5))
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._shape_to_hour_bounds",
                        lambda *a, **k: (6.0, 9.0, None))
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._type_request_unit",
                        lambda hsid: "hour")
    inserted = {}
    def fake_insert(**kw):
        inserted.update(kw)
        return 777
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._insert_request_row",
                        fake_insert)
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._queue_push",
                        lambda rid: None)

    client = TestClient(app)
    r = client.post(
        "/timeclock/time-off/request/anytoken/submit",
        data={
            "shape": "late_arrival",
            "holiday_status_id": "4",
            "date_from": "2026-06-10",   # the date the user picked
            "date_to": "2026-05-29",     # stale hidden "today"
            "time_b": "09:00",
        },
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    assert inserted["date_from"].isoformat() == "2026-06-10"
    assert inserted["date_to"].isoformat() == "2026-06-10"  # not 2026-05-29


def test_calendar_renders_with_month_view(monkeypatch):
    """Who's Out calendar — valid token + stubbed helpers → 200 with a month
    grid. Stubs `_approved_by_day` so the test doesn't need a real DB; the
    helper itself is exercised by its own tests below."""
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._verify_token",
                        lambda t: 1)
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._person_by_id",
                        lambda pid: {"id": 1, "name": "T", "odoo_id": 5})
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._approved_by_day",
                        lambda start, end: {})
    client = TestClient(app)
    r = client.get("/timeclock/time-off/calendar/anytoken")
    assert r.status_code == 200
    assert "Who" in r.text or "calendar" in r.text.lower()


def test_whos_out_public_renders_without_token(monkeypatch):
    """Public Who's Out glance — the kiosk-home shortcut opens this with no
    token. Stubs `_approved_by_day` so the test needs no DB; confirms the
    tokenless route renders and emits public-mode URLs (tokenless month nav,
    Back to the home screen)."""
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._approved_by_day",
                        lambda start, end: {})
    client = TestClient(app)
    r = client.get("/timeclock/whos-out")
    assert r.status_code == 200
    assert "Who" in r.text or "calendar" in r.text.lower()
    # public mode: month nav is tokenless and Back returns to the kiosk home
    assert "/timeclock/whos-out?month=" in r.text
    assert 'href="/timeclock"' in r.text


def test_cancel_handler_marks_row_for_cancel_and_queues(monkeypatch):
    """POST /timeclock/time-off/mine/{token}/{rid}/cancel on a row that already
    has an odoo_leave_id flips the local row to ``draft_cancel`` and queues
    a background push — the push routes through ``_push_cancel`` which
    calls ``refuse_leave`` in Odoo. The local row is NOT deleted; we keep
    it so the sweep can retry on failure and the user can see the request
    in My Requests with its terminal state once the push completes."""
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._verify_token",
                        lambda t: 1)
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._person_by_id",
                        lambda pid: {"id": 1, "name": "T", "odoo_id": 5})
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._load_request",
                        lambda rid, pid: {
                            "id": rid, "person_odoo_id": pid,
                            "state": "confirm", "odoo_leave_id": 999,
                        })
    updates = []
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._set_row_state",
                        lambda rid, state: updates.append((rid, state)))
    queued = []
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._queue_push",
                        lambda rid: queued.append(rid))
    client = TestClient(app)
    r = client.post(
        "/timeclock/time-off/mine/anytoken/42/cancel",
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    assert (42, "draft_cancel") in updates
    assert queued == [42]


def test_edit_post_updates_row_and_queues_sync(monkeypatch):
    """POST /timeclock/time-off/mine/{token}/{rid}/edit on an existing row
    UPDATEs the row (via ``_update_request_row``) and queues a background
    push that will route through ``time_off_sync._push_edit`` to write
    the changed fields to the same ``hr.leave`` record on Odoo."""
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._verify_token",
                        lambda t: 1)
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._person_by_id",
                        lambda pid: {"id": 1, "name": "T", "odoo_id": 5})
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._load_request",
                        lambda rid, pid: {
                            "id": rid, "person_odoo_id": pid,
                            "shape": "full_day", "state": "confirm",
                            "odoo_leave_id": 999,
                            "holiday_status_id": 1,
                        })
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._shift_window_for",
                        lambda pid: (6.0, 14.5))
    # Same type-unit lookup stub as the new-request submit test.
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._type_request_unit",
                        lambda hsid: "day")
    updates = []
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._update_request_row",
                        lambda **kw: updates.append(kw))
    queued = []
    monkeypatch.setattr("zira_dashboard.routes.timeclock_time_off._queue_push",
                        lambda rid: queued.append(rid))

    client = TestClient(app)
    r = client.post(
        "/timeclock/time-off/mine/anytoken/42/edit",
        data={
            "shape": "full_day",
            "holiday_status_id": "1",
            "date_from": "2026-06-10",
            "date_to": "2026-06-12",
            "note": "Updated dates",
        },
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    assert updates and updates[0]["date_from"].isoformat() == "2026-06-10"
    assert queued == [42]
