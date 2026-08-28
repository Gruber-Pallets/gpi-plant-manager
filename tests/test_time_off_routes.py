"""Kiosk time-off route tests.

The route surface is HMAC-token-gated like the rest of the kiosk; the
two tests below cover the easy gate-fail case (bogus token → redirect)
and stub a placeholder for the happy-path test once the suite gets a
seeded-person fixture (Task 16 in the plan promises to wire it).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

# Import after conftest sets AUTH_DISABLED
from zira_dashboard import company_holidays
from zira_dashboard.app import app
from zira_dashboard.routes.timeclock import _mint_token
from zira_dashboard.routes import timeclock_time_off


def _token_for(person_id: int) -> str:
    return _mint_token(person_id)


def test_leave_type_fallback_uses_named_cache_invalidator(monkeypatch):
    events = []
    types = [
        {
            "id": 7,
            "name": "Vacation",
            "request_unit": "day",
            "requires_allocation": "yes",
            "color": 1,
            "active": True,
        }
    ]
    monkeypatch.setattr(
        timeclock_time_off.odoo_client,
        "invalidate_leave_types_cache",
        lambda: events.append("invalidate"),
    )
    monkeypatch.setattr(
        timeclock_time_off.odoo_client,
        "fetch_leave_types",
        lambda: events.append("fetch") or types,
    )
    monkeypatch.setattr(
        timeclock_time_off.db,
        "execute",
        lambda *args, **kwargs: events.append("cache"),
    )

    assert timeclock_time_off._fallback_fetch_and_cache_leave_types() == [
        {
            "holiday_status_id": 7,
            "name": "Vacation",
            "request_unit": "day",
            "requires_allocation": "yes",
        }
    ]
    assert events == ["invalidate", "fetch", "cache"]


def test_landing_route_redirects_when_token_invalid(monkeypatch):
    monkeypatch.setenv("KIOSK_TIME_OFF_ENABLED", "1")
    client = TestClient(app)
    r = client.get("/timeclock/time-off/bogus.token", follow_redirects=False)
    assert r.status_code in (302, 303, 307)


def test_landing_counts_eligible_and_open_absence_pto_requests(monkeypatch):
    captured = {}
    person = {"id": 3, "name": "Ana", "odoo_id": 44}
    monkeypatch.setattr(timeclock_time_off, "_verify_token", lambda _token: 3)
    monkeypatch.setattr(timeclock_time_off, "_person_by_id", lambda _id: person)
    monkeypatch.setattr(timeclock_time_off, "_mint_token", lambda _id: "fresh")
    monkeypatch.setattr(timeclock_time_off, "_pending_count", lambda _id: 1)
    monkeypatch.setattr(timeclock_time_off, "_all_count", lambda _id: 2)
    monkeypatch.setattr(timeclock_time_off, "_sync_error_warning", lambda _id: None)
    monkeypatch.setattr(
        timeclock_time_off.absence_pto,
        "list_candidates",
        lambda _id, _today: [
            SimpleNamespace(eligible=True),
            SimpleNamespace(eligible=False),
        ],
    )
    monkeypatch.setattr(
        timeclock_time_off.absence_pto,
        "employee_requests",
        lambda _id, limit=100: pytest.fail("landing must use count aggregate"),
    )
    monkeypatch.setattr(
        timeclock_time_off.absence_pto,
        "employee_request_counts",
        lambda _id: SimpleNamespace(
            total=4,
            unresolved=3,
            actionable=2,
        ),
    )
    monkeypatch.setattr(timeclock_time_off, "plant_today", lambda: date(2026, 8, 28))

    def fake_response(request, template, context, status_code=200):
        captured.update(template=template, context=context, status_code=status_code)
        return SimpleNamespace(context=context, headers={})

    monkeypatch.setattr(timeclock_time_off.templates, "TemplateResponse", fake_response)

    timeclock_time_off.time_off_landing(SimpleNamespace(), "token")

    assert captured["context"]["absence_pto_count"] == 3
    assert captured["context"]["pending_count"] == 4
    assert captured["context"]["all_count"] == 6


def test_my_requests_combines_ordinary_and_linked_rows_with_exact_urls(monkeypatch):
    captured = {}
    person = {"id": 3, "name": "Ana", "odoo_id": 44}
    ordinary_created = datetime(2026, 8, 20, 12, tzinfo=UTC)
    linked_requested = datetime(2026, 8, 21, 12, tzinfo=UTC)
    monkeypatch.setattr(timeclock_time_off, "_verify_token", lambda _token: 3)
    monkeypatch.setattr(timeclock_time_off, "_person_by_id", lambda _id: person)
    monkeypatch.setattr(timeclock_time_off, "_mint_token", lambda _id: "fresh")
    monkeypatch.setattr(
        timeclock_time_off,
        "_list_my_requests",
        lambda _id: [{
            "id": 12,
            "state": "confirm",
            "type_name": "Vacation",
            "date_from": date(2026, 8, 22),
            "date_to": date(2026, 8, 22),
            "hour_from": None,
            "hour_to": None,
            "created_at": ordinary_created,
        }],
    )
    monkeypatch.setattr(
        timeclock_time_off.absence_pto,
        "employee_requests",
        lambda _id, limit=100: [SimpleNamespace(
            id=41,
            state="needs_review",
            absence_day=date(2026, 8, 20),
            requested_at=linked_requested,
        )],
    )

    def fake_response(request, template, context, status_code=200):
        captured.update(template=template, context=context, status_code=status_code)
        return SimpleNamespace(context=context, headers={})

    monkeypatch.setattr(timeclock_time_off.templates, "TemplateResponse", fake_response)

    timeclock_time_off.mine_list(SimpleNamespace(), "token")

    rows = captured["context"]["requests"]
    assert [row["request_kind"] for row in rows] == ["absence_pto", "ordinary"]
    assert rows[0] == {
        "request_kind": "absence_pto",
        "id": 41,
        "type_name": "Past absence · Paid Time Off",
        "date_from": date(2026, 8, 20),
        "date_to": date(2026, 8, 20),
        "hour_from": None,
        "hour_to": None,
        "bucket": "Needs review",
        "detail_url": "/timeclock/time-off/past-absence/fresh/requests/41",
    }
    assert rows[1]["detail_url"] == "/timeclock/time-off/mine/fresh/12"
    assert rows[1]["bucket"] == "Pending"


def test_needs_review_is_not_counted_as_available_or_pending(monkeypatch):
    monkeypatch.setattr(
        timeclock_time_off.absence_pto,
        "list_candidates",
        lambda _id, _today: [],
    )
    monkeypatch.setattr(
        timeclock_time_off.absence_pto,
        "employee_request_counts",
        lambda _id: SimpleNamespace(total=1, unresolved=1, actionable=0),
    )

    assert timeclock_time_off._absence_pto_counts(44) == (0, 1, 1)


def test_ordinary_history_hides_the_pto_mirror_owned_by_a_linked_request(
    monkeypatch,
):
    captured = {}

    def fake_query(sql, params):
        captured.update(sql=sql, params=params)
        return []

    monkeypatch.setattr(timeclock_time_off.db, "query", fake_query)

    assert timeclock_time_off._list_my_requests(44) == []
    assert "NOT EXISTS" in captured["sql"]
    assert "FROM absence_pto_requests" in captured["sql"]
    assert "linked.pto_leave_id = r.odoo_leave_id" in captured["sql"]
    assert "linked.original_absence_leave_id = r.odoo_leave_id" in captured["sql"]
    assert captured["params"] == (44,)


def test_my_requests_count_uses_the_exact_linked_mirror_exclusion(monkeypatch):
    calls = []

    def fake_query(sql, params):
        calls.append((sql, params))
        if "COUNT(*) AS n" in sql:
            return [{"n": 0}]
        return []

    monkeypatch.setattr(timeclock_time_off.db, "query", fake_query)

    assert timeclock_time_off._all_count(44) == 0
    assert timeclock_time_off._list_my_requests(44) == []
    count_sql, list_sql = calls[0][0], calls[1][0]
    assert "FROM time_off_requests r" in count_sql
    assert timeclock_time_off._LINKED_MIRROR_EXCLUSION in count_sql
    assert timeclock_time_off._LINKED_MIRROR_EXCLUSION in list_sql


def test_approved_linked_request_counts_once_when_both_odoo_mirrors_are_hidden(
    monkeypatch,
):
    person = {"id": 3, "name": "Ana", "odoo_id": 44}
    captured = {}
    monkeypatch.setattr(timeclock_time_off, "_verify_token", lambda _token: 3)
    monkeypatch.setattr(timeclock_time_off, "_person_by_id", lambda _id: person)
    monkeypatch.setattr(timeclock_time_off, "_mint_token", lambda _id: "fresh")
    monkeypatch.setattr(timeclock_time_off, "_pending_count", lambda _id: 0)
    monkeypatch.setattr(timeclock_time_off, "_all_count", lambda _id: 0)
    monkeypatch.setattr(timeclock_time_off, "_sync_error_warning", lambda _id: None)
    monkeypatch.setattr(timeclock_time_off, "_absence_pto_counts", lambda _id: (0, 0, 1))

    def fake_response(request, template, context, status_code=200):
        captured.update(context=context)
        return SimpleNamespace(context=context, headers={})

    monkeypatch.setattr(timeclock_time_off.templates, "TemplateResponse", fake_response)

    timeclock_time_off.time_off_landing(SimpleNamespace(), "token")

    # The approved linked row is visible once; its original Absence and PTO
    # mirror rows are excluded by the ordinary count/query scope.
    assert captured["context"]["all_count"] == 1


def test_combined_history_requests_only_top_100_linked_rows(monkeypatch):
    seen = []
    monkeypatch.setattr(timeclock_time_off, "_list_my_requests", lambda _id: [])
    monkeypatch.setattr(
        timeclock_time_off.absence_pto,
        "employee_requests",
        lambda person_id, *, limit: seen.append((person_id, limit)) or [],
    )

    assert timeclock_time_off._combined_my_requests(44, "token") == []
    assert seen == [(44, 100)]


def test_combined_history_merges_top_100_from_each_source_then_keeps_newest_100(
    monkeypatch,
):
    base = datetime(2026, 8, 28, tzinfo=UTC)
    ordinary = [
        {
            "id": index,
            "state": "validate",
            "type_name": "Vacation",
            "date_from": date(2026, 8, 20),
            "date_to": date(2026, 8, 20),
            "hour_from": None,
            "hour_to": None,
            "created_at": base.replace(microsecond=index),
        }
        for index in range(100)
    ]
    linked = [
        SimpleNamespace(
            id=1000 + index,
            state="approved",
            absence_day=date(2026, 8, 20),
            requested_at=base.replace(microsecond=100 + index),
        )
        for index in range(100)
    ]
    monkeypatch.setattr(timeclock_time_off, "_list_my_requests", lambda _id: ordinary)
    monkeypatch.setattr(
        timeclock_time_off.absence_pto,
        "employee_requests",
        lambda _id, *, limit: linked if limit == 100 else pytest.fail(limit),
    )

    rows = timeclock_time_off._combined_my_requests(44, "token")

    assert len(rows) == 100
    assert {row["request_kind"] for row in rows} == {"absence_pto"}
    assert {row["id"] for row in rows} == {1000 + index for index in range(100)}


def test_my_requests_real_template_links_each_request_kind_to_its_detail(monkeypatch):
    monkeypatch.setattr(timeclock_time_off, "_verify_token", lambda _token: 3)
    monkeypatch.setattr(
        timeclock_time_off,
        "_person_by_id",
        lambda _id: {"id": 3, "name": "Ana", "odoo_id": 44},
    )
    monkeypatch.setattr(timeclock_time_off, "_mint_token", lambda _id: "fresh")
    monkeypatch.setattr(
        timeclock_time_off,
        "_combined_my_requests",
        lambda _id, _token: [
            {
                "id": 41,
                "type_name": "Past absence · Paid Time Off",
                "date_from": date(2026, 8, 20),
                "date_to": date(2026, 8, 20),
                "hour_from": None,
                "hour_to": None,
                "bucket": "Pending",
                "detail_url": (
                    "/timeclock/time-off/past-absence/fresh/requests/41"
                ),
            },
            {
                "id": 12,
                "type_name": "Vacation",
                "date_from": date(2026, 8, 22),
                "date_to": date(2026, 8, 22),
                "hour_from": None,
                "hour_to": None,
                "bucket": "Approved",
                "detail_url": "/timeclock/time-off/mine/fresh/12",
            },
        ],
    )

    response = TestClient(app).get(
        "/timeclock/time-off/mine/token", follow_redirects=False
    )

    assert response.status_code == 200
    assert 'href="/timeclock/time-off/past-absence/fresh/requests/41"' in response.text
    assert 'href="/timeclock/time-off/mine/fresh/12"' in response.text


def test_landing_route_renders_when_token_valid(monkeypatch):
    """The real landing template renders the past-absence action safely."""
    monkeypatch.setattr(timeclock_time_off, "_verify_token", lambda _token: 3)
    monkeypatch.setattr(
        timeclock_time_off,
        "_person_by_id",
        lambda _id: {"id": 3, "name": "Ana", "odoo_id": 44},
    )
    monkeypatch.setattr(timeclock_time_off, "_mint_token", lambda _id: "fresh")
    monkeypatch.setattr(timeclock_time_off, "_pending_count", lambda _id: 0)
    monkeypatch.setattr(timeclock_time_off, "_all_count", lambda _id: 0)
    monkeypatch.setattr(timeclock_time_off, "_sync_error_warning", lambda _id: None)
    monkeypatch.setattr(
        timeclock_time_off,
        "_absence_pto_counts",
        lambda _id: (2, 1, 1),
    )

    response = TestClient(app).get(
        "/timeclock/time-off/token", follow_redirects=False
    )

    assert response.status_code == 200
    assert "Use PTO for a Past Absence" in response.text
    assert 'href="/timeclock/time-off/past-absence/fresh"' in response.text
    assert "2 available or pending" in response.text


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
    # No pre-existing overlap on the happy path — stub the guard added by the
    # overlapping-request block so this DB-free test reaches the insert.
    monkeypatch.setattr("zira_dashboard.time_off_sync.find_conflicting_request",
                        lambda *a, **k: None)
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
    # No overlap — stub the guard so the test reaches the time-validation
    # (422) branch rather than short-circuiting on the conflict (409) branch.
    monkeypatch.setattr("zira_dashboard.time_off_sync.find_conflicting_request",
                        lambda *a, **k: None)
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
    # No pre-existing overlap — stub the guard so this DB-free test proceeds.
    monkeypatch.setattr("zira_dashboard.time_off_sync.find_conflicting_request",
                        lambda *a, **k: None)
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


def test_approved_by_day_reads_mirrored_holidays_and_fans_them_out(monkeypatch):
    seen = []
    monkeypatch.setattr(timeclock_time_off.db, "query", lambda *args: [])
    monkeypatch.setattr(
        company_holidays,
        "for_range",
        lambda start, end: seen.append((start, end)) or [{
            "id": 9,
            "name": "Independence Day",
            "date_from": "2026-07-03",
            "date_to": "2026-07-04",
            "calendar_id": False,
        }],
    )

    start = date(2026, 7, 1)
    end = date(2026, 7, 6)
    by_day = timeclock_time_off._approved_by_day(start, end)

    assert seen == [(start, end)]
    assert sorted(by_day) == [date(2026, 7, 3), date(2026, 7, 4)]
    for day in by_day:
        assert by_day[day] == [{
            "name": "Independence Day",
            "label": "Plant Closed",
            "source": "holiday",
        }]


def test_approved_by_day_fails_soft_when_holiday_mirror_lookup_fails(monkeypatch):
    monkeypatch.setattr(timeclock_time_off.db, "query", lambda *args: [])

    def boom(start, end):
        raise RuntimeError("mirror unavailable")

    monkeypatch.setattr(company_holidays, "for_range", boom)

    assert timeclock_time_off._approved_by_day(
        date(2026, 7, 1), date(2026, 7, 6),
    ) == {}


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


def test_submit_blocks_overlapping_request(monkeypatch):
    """A submit that overlaps an existing request posts nothing, queues no
    push, and re-renders with conflict=True at HTTP 409."""
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._verify_token", lambda t: 1)
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._person_by_id",
        lambda pid: {"id": 1, "name": "T", "odoo_id": 5, "spanish_speaker": False})
    # A conflicting request exists in the mirror.
    monkeypatch.setattr(
        "zira_dashboard.time_off_sync.find_conflicting_request",
        lambda *a, **k: {"id": 99})
    # Capture the render context instead of rendering Jinja.
    captured = {}

    def fake_tr(request, name, context, status_code=200):
        captured["name"] = name
        captured["context"] = context
        captured["status"] = status_code
        from fastapi.responses import HTMLResponse
        return HTMLResponse("conflict", status_code=status_code)

    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off.templates.TemplateResponse",
        fake_tr)
    # These MUST NOT run on the conflict path.
    inserted = []
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._insert_request_row",
        lambda **kw: inserted.append(kw) or 1)
    queued = []
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._queue_push",
        lambda rid: queued.append(rid))
    # _details_context dependencies — stub so no real DB / Odoo.
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._shift_window_for",
        lambda pid: (6.0, 14.5))
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._fetch_visible_leave_types",
        lambda shape: [{"id": 1, "name": "PTO",
                        "request_unit": "day", "requires_allocation": "no"}])
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off.time_off_balances.get_for_employee",
        lambda pid: [])
    import types as _types
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off.schedule_store.current",
        lambda: _types.SimpleNamespace(work_weekdays=[0, 1, 2, 3, 4]))

    client = TestClient(app)
    r = client.post(
        "/timeclock/time-off/request/anytoken/submit",
        data={"shape": "full_day", "holiday_status_id": "1",
              "date_from": "2026-06-01", "date_to": "2026-06-03", "note": ""},
        follow_redirects=False,
    )
    assert r.status_code == 409
    assert captured["context"].get("conflict") is True
    assert inserted == []
    assert queued == []


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
    # No conflict on the happy path — guard passes, update proceeds.
    monkeypatch.setattr(
        "zira_dashboard.time_off_sync.find_conflicting_request",
        lambda *a, **k: None)
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


def test_edit_blocks_overlapping_request(monkeypatch):
    """Editing a request onto dates that overlap a DIFFERENT request blocks
    with conflict=True at 409 and does not update or queue a push. The check
    passes exclude_rid so a request never conflicts with itself."""
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._verify_token", lambda t: 1)
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._person_by_id",
        lambda pid: {"id": 1, "name": "T", "odoo_id": 5, "spanish_speaker": False})
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._load_request",
        lambda rid, pid: {"id": rid, "person_odoo_id": pid,
                          "shape": "full_day", "state": "confirm",
                          "odoo_leave_id": 999, "holiday_status_id": 1})
    seen_exclude = {}

    def fake_conflict(person_odoo_id, date_from, date_to,
                      exclude_rid=None, established_only=False):
        seen_exclude["rid"] = exclude_rid
        return {"id": 77}

    monkeypatch.setattr(
        "zira_dashboard.time_off_sync.find_conflicting_request", fake_conflict)
    captured = {}

    def fake_tr(request, name, context, status_code=200):
        captured["context"] = context
        captured["status"] = status_code
        from fastapi.responses import HTMLResponse
        return HTMLResponse("conflict", status_code=status_code)

    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off.templates.TemplateResponse",
        fake_tr)
    updated = []
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._update_request_row",
        lambda **kw: updated.append(kw))
    queued = []
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._queue_push",
        lambda rid: queued.append(rid))
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._shift_window_for",
        lambda pid: (6.0, 14.5))
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._fetch_visible_leave_types",
        lambda shape: [{"id": 1, "name": "PTO",
                        "request_unit": "day", "requires_allocation": "no"}])
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off.time_off_balances.get_for_employee",
        lambda pid: [])
    import types as _types
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off.schedule_store.current",
        lambda: _types.SimpleNamespace(work_weekdays=[0, 1, 2, 3, 4]))

    client = TestClient(app)
    r = client.post(
        "/timeclock/time-off/mine/anytoken/42/edit",
        data={"shape": "full_day", "holiday_status_id": "1",
              "date_from": "2026-06-10", "date_to": "2026-06-12", "note": ""},
        follow_redirects=False,
    )
    assert r.status_code == 409
    assert captured["context"].get("conflict") is True
    assert captured["context"].get("edit_mode") is True
    assert seen_exclude["rid"] == 42
    assert updated == []
    assert queued == []


def test_submit_conflict_renders_modal(monkeypatch):
    """Real-render path: the conflict response contains the modal message and
    a My Requests link."""
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._verify_token", lambda t: 1)
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._person_by_id",
        lambda pid: {"id": 1, "name": "T", "odoo_id": 5, "spanish_speaker": False})
    monkeypatch.setattr(
        "zira_dashboard.time_off_sync.find_conflicting_request",
        lambda *a, **k: {"id": 99})
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._shift_window_for",
        lambda pid: (6.0, 14.5))
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._fetch_visible_leave_types",
        lambda shape: [{"id": 1, "name": "PTO",
                        "request_unit": "day", "requires_allocation": "no"}])
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off.time_off_balances.get_for_employee",
        lambda pid: [])
    import types as _types
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off.schedule_store.current",
        lambda: _types.SimpleNamespace(work_weekdays=[0, 1, 2, 3, 4]))
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._insert_request_row",
        lambda **kw: 1)
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._queue_push", lambda rid: None)

    client = TestClient(app)
    r = client.post(
        "/timeclock/time-off/request/anytoken/submit",
        data={"shape": "full_day", "holiday_status_id": "1",
              "date_from": "2026-06-01", "date_to": "2026-06-03", "note": ""},
        follow_redirects=False,
    )
    assert r.status_code == 409
    assert "You already have time off for this time" in r.text
    assert "/timeclock/time-off/mine/" in r.text


# --------------------------------------------------------------------------
# local_record rows (absence recorded despite an Odoo work-schedule
# rejection; the Odoo copy sits refused). The kiosk must not offer Edit —
# _push_edit would write to the refused hr.leave and strand the row — and
# the edit routes must bounce even a hand-crafted POST. Cancel stays
# available (its push path settles locally).
# --------------------------------------------------------------------------


def _local_record_row(rid, pid):
    from datetime import date as _date
    return {
        "id": rid, "person_odoo_id": pid, "originating_kiosk_user": True,
        "shape": "full_day", "holiday_status_id": 1,
        "date_from": _date(2026, 7, 3), "date_to": _date(2026, 7, 3),
        "hour_from": None, "hour_to": None, "note": None,
        "state": "validate", "odoo_leave_id": 999, "sync_error": None,
        "local_record": True,
    }


def _wire_local_record_kiosk(monkeypatch):
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._verify_token", lambda t: 1)
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._person_by_id",
        lambda pid: {"id": 1, "name": "T", "odoo_id": 5,
                     "spanish_speaker": False})
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._load_request",
        lambda rid, pid: _local_record_row(rid, pid))


def test_mine_detail_hides_edit_for_local_record(monkeypatch):
    _wire_local_record_kiosk(monkeypatch)
    client = TestClient(app)
    r = client.get("/timeclock/time-off/mine/anytoken/42")
    assert r.status_code == 200
    assert "Edit Request" not in r.text
    assert "Cancel This Request" in r.text


def test_edit_get_bounces_local_record_row(monkeypatch):
    _wire_local_record_kiosk(monkeypatch)
    client = TestClient(app)
    r = client.get("/timeclock/time-off/mine/anytoken/42/edit",
                   follow_redirects=False)
    assert r.status_code == 303


def test_edit_post_rejects_local_record_row(monkeypatch):
    _wire_local_record_kiosk(monkeypatch)
    updates = []
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._update_request_row",
        lambda **kw: updates.append(kw))
    queued = []
    monkeypatch.setattr(
        "zira_dashboard.routes.timeclock_time_off._queue_push",
        lambda rid: queued.append(rid))
    client = TestClient(app)
    r = client.post(
        "/timeclock/time-off/mine/anytoken/42/edit",
        data={
            "shape": "full_day", "holiday_status_id": "1",
            "date_from": "2026-07-10", "date_to": "2026-07-10",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert updates == []
    assert queued == []


def test_load_request_selects_local_record(monkeypatch):
    import zira_dashboard.routes.timeclock_time_off as mod
    seen = {}

    def fake_query(sql, params=None):
        seen["sql"] = sql
        return []

    monkeypatch.setattr(mod.db, "query", fake_query)
    mod._load_request(42, 5)
    assert "local_record" in seen["sql"]
