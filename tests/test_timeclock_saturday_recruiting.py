from datetime import UTC, date, datetime, time
import logging

from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard.routes import timeclock, timeclock_saturday
from zira_dashboard import employee_notifications, staffing
from zira_dashboard.saturday_recruiting_store import CommitmentStatus, HomeBanner, Offer
from zira_dashboard.shift_config import SITE_TZ

client = TestClient(app)
PERSON = {"id": 1, "name": "Ana", "odoo_id": 11, "wage_type": "hourly", "spanish_level": 3}
# The deadline must be tz-aware like the real store persists it: a naive
# datetime is reinterpreted in the SYSTEM timezone by format_deadline's
# astimezone(), which renders 2:00 AM on UTC CI runners but 7:00 AM locally.
OFFER = Offer(
    date(2026, 7, 25),
    time(7),
    time(12),
    datetime(2026, 7, 24, 7, tzinfo=SITE_TZ),
    frozenset({1}),
)
AVAILABLE_BANNER = HomeBanner(OFFER.day, OFFER.response_deadline, 1, "available", time(7), time(12))
PLANNED_BANNER = HomeBanner(OFFER.day, OFFER.response_deadline, 0, "tomorrow", time(7), time(12))
HOLIDAY_OFFER = Offer(
    date(2026, 11, 27),
    time(7),
    time(12),
    datetime(2026, 11, 25, 7, tzinfo=SITE_TZ),
    frozenset({1}),
    "holiday",
    "Black Friday",
)
HOLIDAY_AVAILABLE_BANNER = HomeBanner(
    HOLIDAY_OFFER.day,
    HOLIDAY_OFFER.response_deadline,
    1,
    "available",
    time(7),
    time(12),
    "holiday",
    "Black Friday",
)
HOLIDAY_PLANNED_BANNER = HomeBanner(
    HOLIDAY_OFFER.day,
    HOLIDAY_OFFER.response_deadline,
    0,
    "tomorrow",
    time(7),
    time(12),
    "holiday",
    "Black Friday",
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


def test_empty_home_roster_shows_bilingual_manager_alert_and_logs_diagnostics(
    monkeypatch,
    caplog,
):
    last_good_sync = datetime(2026, 8, 12, 12, 58, tzinfo=UTC)
    alert = {"error": "unsafe snapshot", "invalid_count": 36}
    monkeypatch.setattr(timeclock.db, "query", lambda *_args: [])
    monkeypatch.setattr(timeclock, "_saturday_banner_context", lambda: None)
    monkeypatch.setattr(timeclock.odoo_sync, "_read_last_sync", lambda: last_good_sync)
    monkeypatch.setattr(timeclock.odoo_sync, "roster_sync_alert", lambda: alert)

    with caplog.at_level(logging.CRITICAL, logger=timeclock.__name__):
        response = client.get("/timeclock")

    assert response.status_code == 200
    assert "The employee list is unavailable. Please tell a manager." in response.text
    assert "La lista de empleados no está disponible. Avísale a un gerente." in response.text
    assert 'id="filter"' not in response.text
    assert "timeclock roster is empty" in caplog.text
    assert "last_sync_at=2026-08-12T12:58:00+00:00" in caplog.text
    assert "unsafe snapshot" in caplog.text


def test_home_shows_bilingual_banner_with_deadline(monkeypatch):
    monkeypatch.setattr(timeclock.db, "query", lambda *_args: [])
    monkeypatch.setattr(
        timeclock.saturday_recruiting_store, "home_banner", lambda _now: AVAILABLE_BANNER
    )
    response = client.get("/timeclock")
    assert "Saturday Work Available" in response.text
    assert "Trabajo disponible el sábado" in response.text
    assert "Friday, July 24 at 7:00 AM" in response.text
    assert (
        response.text.index('<span class="k-header-prompt"')
        < response.text.index('<div class="saturday-home-banner"')
        < response.text.index('<div class="k-header-actions"')
    )


def test_home_shows_named_bilingual_holiday_offer_in_existing_banner(monkeypatch):
    monkeypatch.setattr(timeclock.db, "query", lambda *_args: [])
    monkeypatch.setattr(
        timeclock.saturday_recruiting_store,
        "home_banner",
        lambda _now: HOLIDAY_AVAILABLE_BANNER,
    )

    response = client.get("/timeclock")

    assert "Holiday Work Available — Black Friday" in response.text
    assert "Trabajo disponible en el día festivo — Black Friday" in response.text
    assert 'class="saturday-home-banner"' in response.text
    assert (
        response.text.index('<span class="k-header-prompt"')
        < response.text.index('<div class="saturday-home-banner"')
        < response.text.index('<div class="k-header-actions"')
    )


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


def test_home_labels_named_holiday_schedule_without_changing_modal_hooks(monkeypatch):
    monkeypatch.setattr(timeclock.db, "query", lambda *_args: [])
    monkeypatch.setattr(
        timeclock.saturday_recruiting_store,
        "home_banner",
        lambda _now: HOLIDAY_PLANNED_BANNER,
    )
    monkeypatch.setattr(
        timeclock.staffing,
        "load_schedule",
        lambda _day: staffing.Schedule(
            HOLIDAY_OFFER.day,
            published=True,
            assignments={"Repair 1": ["Ana"]},
        ),
    )

    response = client.get("/timeclock")

    assert "Holiday work planned for tomorrow — Black Friday" in response.text
    assert '<h2 id="saturday-schedule-title">Holiday work schedule</h2>' in response.text
    assert 'id="saturday-schedule-trigger"' in response.text
    assert 'aria-controls="saturday-schedule-modal"' in response.text
    assert 'id="saturday-schedule-modal"' in response.text
    assert "Repair 1" in response.text


def test_home_unpublished_holiday_plan_never_calls_it_a_saturday_schedule(monkeypatch):
    monkeypatch.setattr(timeclock.db, "query", lambda *_args: [])
    monkeypatch.setattr(
        timeclock.saturday_recruiting_store,
        "home_banner",
        lambda _now: HOLIDAY_PLANNED_BANNER,
    )
    monkeypatch.setattr(
        timeclock.staffing,
        "load_schedule",
        lambda _day: staffing.Schedule(HOLIDAY_OFFER.day, published=False),
    )

    response = client.get("/timeclock")

    assert "Holiday work schedule has not been published yet." in response.text
    assert "Saturday schedule has not been published yet." not in response.text


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
    monkeypatch.setattr(
        timeclock.saturday_recruiting_store, "offer_for_person", lambda *_args: OFFER
    )
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
    monkeypatch.setattr(
        timeclock.saturday_recruiting_store, "offer_for_person", lambda *_args: None
    )
    response = client.get("/timeclock/start/1", follow_redirects=False)
    assert "/timeclock/dashboard/" in response.headers["location"]


def test_partial_options_and_tampered_minutes(monkeypatch):
    _person(monkeypatch)
    monkeypatch.setattr(timeclock_saturday.store, "offer_for_person", lambda *_args: OFFER)
    token = timeclock._mint_token(1)
    page = client.get(f"/timeclock/saturday/partial/{token}")
    assert "<title>Saturday Work</title>" in page.text
    assert "Solo puedo trabajar parte del turno" in page.text
    assert "I can work only part of the shift" in page.text
    assert "I can work only part of the shift —" not in page.text
    assert f'action="/timeclock/saturday/partial/{token}"' in page.text
    assert 'select name="availability_start"' in page.text
    assert 'select name="availability_end"' in page.text
    assert 'class="k-btn success"' in page.text
    assert "07:00" in page.text and "07:30" in page.text and "07:15" not in page.text
    bad = client.post(
        f"/timeclock/saturday/partial/{token}",
        data={"availability_start": "07:15", "availability_end": "11:30"},
    )
    assert bad.status_code == 422
    assert "30-minute increments" in bad.text
    valid = client.post(
        f"/timeclock/saturday/partial/{token}",
        data={"availability_start": "07:30", "availability_end": "11:30"},
    )
    assert valid.status_code == 200
    assert "Confirm your commitment" in valid.text
    assert "7:30 AM–11:30 AM" in valid.text


def test_holiday_partial_page_names_event_in_browser_title_and_heading(monkeypatch):
    _person(monkeypatch)
    monkeypatch.setattr(
        timeclock_saturday.store,
        "offer_for_person",
        lambda *_args: HOLIDAY_OFFER,
    )
    token = timeclock._mint_token(1)

    page = client.get(f"/timeclock/saturday/partial/{token}")

    assert "<title>Holiday Work — Black Friday</title>" in page.text
    assert "Solo puedo trabajar parte del turno — Black Friday" in page.text
    assert "I can work only part of the shift — Black Friday" in page.text
    assert f'action="/timeclock/saturday/partial/{token}"' in page.text
    assert 'select name="availability_start"' in page.text
    assert 'select name="availability_end"' in page.text
    assert 'class="k-btn success"' in page.text


def test_holiday_partial_page_escapes_event_name_in_title_and_heading(monkeypatch):
    _person(monkeypatch)
    unsafe_offer = Offer(
        HOLIDAY_OFFER.day,
        HOLIDAY_OFFER.shift_start,
        HOLIDAY_OFFER.shift_end,
        HOLIDAY_OFFER.response_deadline,
        HOLIDAY_OFFER.eligible_wc_ids,
        "holiday",
        "<Black & Friday>",
    )
    monkeypatch.setattr(
        timeclock_saturday.store,
        "offer_for_person",
        lambda *_args: unsafe_offer,
    )

    page = client.get(f"/timeclock/saturday/partial/{timeclock._mint_token(1)}")

    assert "<title>Holiday Work — &lt;Black &amp; Friday&gt;</title>" in page.text
    assert "I can work only part of the shift — &lt;Black &amp; Friday&gt;" in page.text
    assert "<Black & Friday>" not in page.text


def test_holiday_offer_partial_error_and_commitment_are_named_and_bilingual(monkeypatch):
    _person(monkeypatch)
    monkeypatch.setattr(
        timeclock_saturday.store,
        "offer_for_person",
        lambda *_args: HOLIDAY_OFFER,
    )
    token = timeclock._mint_token(1)

    offer = client.get(f"/timeclock/saturday/{token}")
    assert "¿Puedes trabajar este día festivo, Black Friday, el viernes, 27 de noviembre?" in (
        offer.text
    )
    assert "Can you work this holiday, Black Friday, on Friday, November 27?" in offer.text
    assert 'action="/timeclock/saturday/confirm/' in offer.text
    assert 'action="/timeclock/saturday/decline/' in offer.text
    assert 'action="/timeclock/saturday/later/' in offer.text
    assert 'class="saturday-partial-link"' in offer.text

    bad = client.post(
        f"/timeclock/saturday/partial/{token}",
        data={"availability_start": "07:15", "availability_end": "11:30"},
    )
    assert bad.status_code == 422
    assert (
        "Availability must use 30-minute increments and stay within the optional shift." in bad.text
    )
    assert (
        "La disponibilidad debe usar incrementos de 30 minutos y mantenerse dentro del "
        "turno opcional." in bad.text
    )

    confirm = client.post(
        f"/timeclock/saturday/confirm/{token}",
        data={
            "day": HOLIDAY_OFFER.day.isoformat(),
            "availability_start": "07:00",
            "availability_end": "12:00",
        },
    )
    assert (
        "Al confirmar, te comprometes a trabajar este día festivo de 7:00 AM–12:00 PM."
        in confirm.text
    )
    assert "By confirming, you commit to work this holiday from 7:00 AM–12:00 PM." in confirm.text
    assert 'action="/timeclock/saturday/commit/' in confirm.text


def test_offer_context_escapes_named_holiday_variables(monkeypatch):
    _person(monkeypatch)
    unsafe_offer = Offer(
        HOLIDAY_OFFER.day,
        HOLIDAY_OFFER.shift_start,
        HOLIDAY_OFFER.shift_end,
        HOLIDAY_OFFER.response_deadline,
        HOLIDAY_OFFER.eligible_wc_ids,
        "holiday",
        "<Black & Friday>",
    )
    monkeypatch.setattr(
        timeclock_saturday.store,
        "offer_for_person",
        lambda *_args: unsafe_offer,
    )

    response = client.get(f"/timeclock/saturday/{timeclock._mint_token(1)}")

    assert "&lt;Black &amp; Friday&gt;" in response.text
    assert "<Black & Friday>" not in response.text


def test_commitment_context_carries_persisted_holiday_metadata(monkeypatch):
    status = CommitmentStatus(
        HOLIDAY_OFFER.day,
        time(7),
        time(12),
        HOLIDAY_OFFER.response_deadline,
        True,
        "holiday",
        "Black Friday",
    )
    monkeypatch.setattr(
        timeclock.saturday_recruiting_store,
        "commitment_for_person",
        lambda *_args: status,
    )

    context = timeclock._saturday_commitment_context(1)

    assert context["day_kind"] == "holiday"
    assert context["event_name"] == "Black Friday"
    assert context["is_holiday"] is True


def test_holiday_commitment_status_is_named_and_keeps_existing_form_hook():
    rendered = timeclock.templates.env.get_template("_timeclock_saturday_status.html").render(
        saturday_commitment={
            "day": HOLIDAY_OFFER.day.isoformat(),
            "day_label": "Friday, November 27",
            "day_label_es": "viernes, 27 de noviembre",
            "hours": "7:00 AM–12:00 PM",
            "deadline_label": "Wednesday, November 25 at 7:00 AM",
            "deadline_label_es": "miércoles, 25 de noviembre a las 7:00 AM",
            "can_employee_cancel": True,
            "day_kind": "holiday",
            "event_name": "Black Friday",
            "is_holiday": True,
        },
        token="signed-token",
        timeclock_language="es_primary",
    )

    assert "Tu compromiso de trabajo en día festivo — Black Friday" in rendered
    assert "Your holiday work commitment — Black Friday" in rendered
    assert "Cancelar compromiso de trabajo en día festivo" in rendered
    assert "Cancel holiday work commitment" in rendered
    assert 'class="saturday-status"' in rendered
    assert 'action="/timeclock/saturday/cancel/signed-token"' in rendered
    assert 'name="day" value="2026-11-27"' in rendered


def test_yes_opens_confirmation_before_commit(monkeypatch):
    _person(monkeypatch)
    monkeypatch.setattr(timeclock_saturday.store, "offer_for_person", lambda *_args: OFFER)
    monkeypatch.setattr(
        timeclock_saturday.store,
        "commit",
        lambda *_args: (_ for _ in ()).throw(AssertionError("not yet")),
    )
    response = client.post(
        f"/timeclock/saturday/confirm/{timeclock._mint_token(1)}",
        data={"day": "2026-07-25", "availability_start": "07:00", "availability_end": "12:00"},
    )
    assert "Confirm your commitment" in response.text
    assert "firm commitment" in response.text


def test_spanish_primary_offer_localizes_date_deadline_and_errors(monkeypatch):
    _person(monkeypatch)
    monkeypatch.setattr(timeclock_saturday.store, "offer_for_person", lambda *_args: OFFER)
    token = timeclock._mint_token(1)
    response = client.get(f"/timeclock/saturday/{token}")
    assert "¿Puedes trabajar el sábado sábado, 25 de julio?" in response.text
    assert "viernes, 24 de julio a las 7:00 AM" in response.text
    bad = client.post(
        f"/timeclock/saturday/partial/{token}",
        data={"availability_start": "07:15", "availability_end": "11:30"},
    )
    assert "La disponibilidad debe usar incrementos de 30 minutos" in bad.text


def test_unanswered_saturday_offer_has_no_back_exit(monkeypatch):
    _person(monkeypatch)
    monkeypatch.setattr(timeclock_saturday.store, "offer_for_person", lambda *_args: OFFER)

    response = client.get(f"/timeclock/saturday/{timeclock._mint_token(1)}")

    assert response.status_code == 200
    assert 'class="k-back"' not in response.text
    assert 'action="/timeclock/saturday/confirm/' in response.text
    assert 'action="/timeclock/saturday/decline/' in response.text
    assert 'action="/timeclock/saturday/later/' in response.text


def test_unanswered_saturday_offer_keeps_normal_idle_timeout(monkeypatch):
    _person(monkeypatch)
    monkeypatch.setattr(timeclock_saturday.store, "offer_for_person", lambda *_args: OFFER)

    response = client.get(f"/timeclock/saturday/{timeclock._mint_token(1)}")

    assert "timer = setTimeout(function() { location.href = '/timeclock'; }, ms);" in response.text
    assert "return 30000;" in response.text


def test_filled_saturday_offer_error_keeps_back_exit(monkeypatch):
    _person(monkeypatch)
    monkeypatch.setattr(timeclock_saturday.store, "offer_for_person", lambda *_args: None)
    monkeypatch.setattr(
        timeclock_saturday.store,
        "commit",
        lambda *_args: (_ for _ in ()).throw(timeclock_saturday.store.NoCompatibleOpening),
    )

    response = client.post(
        f"/timeclock/saturday/commit/{timeclock._mint_token(1)}",
        data={
            "day": "2026-07-25",
            "availability_start": "07:00",
            "availability_end": "12:00",
        },
    )

    assert response.status_code == 409
    assert 'class="k-back"' in response.text


def test_unexpected_decision_store_errors_fail_safe(monkeypatch):
    _person(monkeypatch)
    token = timeclock._mint_token(1)
    boom = lambda *_args: (_ for _ in ()).throw(RuntimeError("database unavailable"))
    for endpoint, method, data in [
        (
            "commit",
            "commit",
            {"day": "2026-07-25", "availability_start": "07:00", "availability_end": "12:00"},
        ),
        ("decline", "decline", {"day": "2026-07-25"}),
        ("later", "record_later", {"day": "2026-07-25"}),
        ("cancel", "cancel_by_employee", {"day": "2026-07-25"}),
    ]:
        monkeypatch.setattr(timeclock_saturday.store, method, boom)
        response = client.post(
            f"/timeclock/saturday/{endpoint}/{token}", data=data, follow_redirects=False
        )
        assert response.status_code == 303
        assert "/timeclock/dashboard/" in response.headers["location"]
