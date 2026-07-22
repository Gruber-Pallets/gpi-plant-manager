from datetime import date, datetime, time

from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard.routes import timeclock, timeclock_saturday
from zira_dashboard import employee_notifications, staffing
from zira_dashboard.saturday_recruiting_store import HomeBanner, Offer
from zira_dashboard.shift_config import SITE_TZ

client = TestClient(app)
PERSON = {"id": 1, "name": "Ana", "odoo_id": 11, "wage_type": "hourly", "spanish_level": 3}
# The deadline must be tz-aware like the real store persists it: a naive
# datetime is reinterpreted in the SYSTEM timezone by format_deadline's
# astimezone(), which renders 2:00 AM on UTC CI runners but 7:00 AM locally.
OFFER = Offer(
    date(2026, 7, 25), time(7), time(12),
    datetime(2026, 7, 24, 7, tzinfo=SITE_TZ), frozenset({1}),
)
AVAILABLE_BANNER = HomeBanner(
    OFFER.day, OFFER.response_deadline, 1, "available", time(7), time(12)
)
PLANNED_BANNER = HomeBanner(
    OFFER.day, OFFER.response_deadline, 0, "tomorrow", time(7), time(12)
)


def _person(monkeypatch):
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _pid: PERSON)
    monkeypatch.setattr(timeclock_saturday, "_person_by_id", lambda _pid: PERSON)


def test_person_lookup_queries_people_table(monkeypatch):
    captured = {}

    def query(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [PERSON]

    monkeypatch.setattr(timeclock.db, "query", query)

    assert timeclock._person_by_id(1) == PERSON
    assert "FROM people" in captured["sql"]
    assert captured["params"] == (1,)


def test_home_shows_bilingual_banner_with_deadline(monkeypatch):
    monkeypatch.setattr(timeclock.db, "query", lambda *_args: [])
    monkeypatch.setattr(
        timeclock.saturday_recruiting_store, "home_banner", lambda _now: AVAILABLE_BANNER
    )
    response = client.get("/timeclock")
    assert "Saturday Work Available" in response.text
    assert "Trabajo disponible el sábado" in response.text
    assert "Friday, July 24 at 7:00 AM" in response.text
    assert (response.text.index('<span class="k-header-prompt"')
            < response.text.index('<div class="saturday-home-banner"')
            < response.text.index('<div class="k-header-actions"'))


def test_home_shows_tomorrow_plan_and_only_published_assignments(monkeypatch):
    monkeypatch.setattr(timeclock.db, "query", lambda *_args: [])
    monkeypatch.setattr(
        timeclock.saturday_recruiting_store, "home_banner", lambda _now: PLANNED_BANNER
    )
    monkeypatch.setattr(
        timeclock.staffing,
        "load_schedule",
        lambda _day: staffing.Schedule(
            OFFER.day, published=True, assignments={"Repair 1": ["Ana", "Bob"]}
        ),
    )

    response = client.get("/timeclock")

    assert "Saturday planned for tomorrow" in response.text
    assert "Repair 1" in response.text
    assert "Ana" in response.text and "Bob" in response.text
    assert "Saturday Work Available" not in response.text


def test_home_uses_posted_snapshot_and_never_exposes_draft_assignments(monkeypatch):
    monkeypatch.setattr(timeclock.db, "query", lambda *_args: [])
    monkeypatch.setattr(
        timeclock.saturday_recruiting_store, "home_banner", lambda _now: PLANNED_BANNER
    )
    monkeypatch.setattr(
        timeclock.staffing,
        "load_schedule",
        lambda _day: staffing.Schedule(
            OFFER.day,
            published=False,
            assignments={"Draft WC": ["Draft Person"]},
            published_snapshot={"assignments": {"Posted WC": ["Posted Person"]}},
        ),
    )

    response = client.get("/timeclock")

    assert "Posted WC" in response.text
    assert "Posted Person" in response.text
    assert "Draft WC" not in response.text
    assert "Draft Person" not in response.text


def test_name_tap_routes_eligible_employee_to_offer(monkeypatch):
    _person(monkeypatch)
    monkeypatch.setattr(employee_notifications, "notifications_enabled", lambda: False)
    monkeypatch.setattr(timeclock.saturday_recruiting_store, "offer_for_person", lambda *_args: OFFER)
    response = client.get("/timeclock/start/1", follow_redirects=False)
    assert "/timeclock/saturday/" in response.headers["location"]


def test_name_tap_after_cancel_routes_employee_back_to_offer(monkeypatch):
    _person(monkeypatch)
    monkeypatch.setattr(employee_notifications, "notifications_enabled", lambda: False)
    monkeypatch.setattr(
        timeclock.saturday_recruiting_store, "offer_for_person", lambda *_args: OFFER
    )

    response = client.get("/timeclock/start/1", follow_redirects=False)

    assert response.status_code == 303
    assert "/timeclock/saturday/" in response.headers["location"]


def test_notifications_keep_priority(monkeypatch):
    _person(monkeypatch)
    monkeypatch.setattr(employee_notifications, "notifications_enabled", lambda: True)
    monkeypatch.setattr(employee_notifications, "has_unacknowledged", lambda _oid: True)
    response = client.get("/timeclock/start/1", follow_redirects=False)
    assert "/timeclock/notifications/" in response.headers["location"]


def test_name_tap_without_offer_continues_to_dashboard(monkeypatch):
    _person(monkeypatch)
    monkeypatch.setattr(employee_notifications, "notifications_enabled", lambda: False)
    monkeypatch.setattr(timeclock.saturday_recruiting_store, "offer_for_person", lambda *_args: None)
    response = client.get("/timeclock/start/1", follow_redirects=False)
    assert "/timeclock/dashboard/" in response.headers["location"]


def test_partial_options_and_tampered_minutes(monkeypatch):
    _person(monkeypatch)
    monkeypatch.setattr(timeclock_saturday.store, "offer_for_person", lambda *_args: OFFER)
    token = timeclock._mint_token(1)
    page = client.get(f"/timeclock/saturday/partial/{token}")
    assert "07:00" in page.text and "07:30" in page.text and "07:15" not in page.text
    bad = client.post(f"/timeclock/saturday/partial/{token}", data={"availability_start": "07:15", "availability_end": "11:30"})
    assert bad.status_code == 422
    assert "30-minute increments" in bad.text
    valid = client.post(
        f"/timeclock/saturday/partial/{token}",
        data={"availability_start": "07:30", "availability_end": "11:30"},
    )
    assert valid.status_code == 200
    assert "Confirm your commitment" in valid.text
    assert "7:30 AM–11:30 AM" in valid.text


def test_yes_opens_confirmation_before_commit(monkeypatch):
    _person(monkeypatch)
    monkeypatch.setattr(timeclock_saturday.store, "offer_for_person", lambda *_args: OFFER)
    monkeypatch.setattr(timeclock_saturday.store, "commit", lambda *_args: (_ for _ in ()).throw(AssertionError("not yet")))
    response = client.post(f"/timeclock/saturday/confirm/{timeclock._mint_token(1)}", data={"day": "2026-07-25", "availability_start": "07:00", "availability_end": "12:00"})
    assert "Confirm your commitment" in response.text
    assert "firm commitment" in response.text


def test_spanish_primary_offer_localizes_date_deadline_and_errors(monkeypatch):
    _person(monkeypatch)
    monkeypatch.setattr(timeclock_saturday.store, "offer_for_person", lambda *_args: OFFER)
    token = timeclock._mint_token(1)
    response = client.get(f"/timeclock/saturday/{token}")
    assert "¿Puedes trabajar el sábado sábado, 25 de julio?" in response.text
    assert "viernes, 24 de julio a las 7:00 AM" in response.text
    bad = client.post(f"/timeclock/saturday/partial/{token}", data={"availability_start": "07:15", "availability_end": "11:30"})
    assert "La disponibilidad debe usar incrementos de 30 minutos" in bad.text


def test_unexpected_decision_store_errors_fail_safe(monkeypatch):
    _person(monkeypatch)
    token = timeclock._mint_token(1)
    boom = lambda *_args: (_ for _ in ()).throw(RuntimeError("database unavailable"))
    for endpoint, method, data in [
        ("commit", "commit", {"day": "2026-07-25", "availability_start": "07:00", "availability_end": "12:00"}),
        ("decline", "decline", {"day": "2026-07-25"}),
        ("later", "record_later", {"day": "2026-07-25"}),
        ("cancel", "cancel_by_employee", {"day": "2026-07-25"}),
    ]:
        monkeypatch.setattr(timeclock_saturday.store, method, boom)
        response = client.post(f"/timeclock/saturday/{endpoint}/{token}", data=data, follow_redirects=False)
        assert response.status_code == 303
        assert "/timeclock/dashboard/" in response.headers["location"]
