"""Dashboard template uses the approved personalized language modes."""
from __future__ import annotations

from pathlib import Path
from datetime import date
from decimal import Decimal

from jinja2 import Environment, FileSystemLoader
import pytest

from zira_dashboard import timeclock_i18n

TEMPLATES = Path("src/zira_dashboard/templates")


def _env():
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    env.globals["static_v"] = lambda _f: "test"
    env.globals["t"] = timeclock_i18n.t
    return env


def _ctx(spanish_level):
    person = {"name": "Maria Garcia", "spanish_level": spanish_level}
    return {
        "person": person,
        "token": "t",
        "is_clocked_in": False,
        "scheduled_wc": None,
        "sync_warning": None,
        "time_off_enabled": True,
        "pending_time_off_count": 0,
        "timeclock_language": timeclock_i18n.language_mode_for_person(person),
    }


def test_dashboard_english_only_for_non_level_three_spanish_employee():
    html = _env().get_template("timeclock_dashboard.html").render(**_ctx(2))
    assert "Pick Work Center" in html
    assert "Elegir estación" not in html


def test_dashboard_level_three_shows_spanish_before_english():
    html = _env().get_template("timeclock_dashboard.html").render(**_ctx(3))
    assert "Pick Work Center" in html
    assert "Elegir estación" in html
    assert 'class="k-es k-primary"' in html
    assert html.index("Elegir estación") < html.index("Pick Work Center")


def test_home_search_input_has_accessible_name():
    html = _env().get_template("timeclock_home.html").render(
        people=[{"id": 1, "name": "Maria Garcia"}],
        session_expired=False,
    )

    assert 'id="filter"' in html
    assert 'aria-label="Search your name"' in html


def test_home_with_people_does_not_show_the_roster_unavailable_alert():
    html = _env().get_template("timeclock_home.html").render(
        people=[{"id": 1, "name": "Maria Garcia"}],
        roster_unavailable=False,
        session_expired=False,
    )

    assert "The employee list is unavailable" not in html
    assert "Maria Garcia" in html
    assert 'id="filter"' in html


def test_celebration_template_renders_birthday_with_first_name_and_hidden_event_id():
    html = _env().get_template("timeclock_celebration.html").render(
        person={"name": "Maria Garcia", "spanish_level": 0},
        token="t",
        celebration={"id": 8, "kind": "birthday", "completed_years": None},
        timeclock_language="en",
    )

    assert "Happy Birthday, Maria!" in html
    assert "Garcia" not in html
    assert 'name="celebration_id" value="8"' in html
    assert 'action="/timeclock/celebration/ack/t"' in html


@pytest.mark.parametrize("raw_name", ["Maria\tGarcia", "  Maria Garcia"])
def test_celebration_template_normalizes_first_name_whitespace(raw_name):
    html = _env().get_template("timeclock_celebration.html").render(
        person={"name": raw_name, "spanish_level": 0},
        token="t",
        celebration={"id": 8, "kind": "birthday", "completed_years": None},
        timeclock_language="en",
    )

    assert "Happy Birthday, Maria!" in html
    assert "Garcia" not in html


def test_celebration_template_uses_translated_nonblank_title_for_blank_name():
    html = _env().get_template("timeclock_celebration.html").render(
        person={"name": " \t", "spanish_level": 0},
        token="t",
        celebration={"id": 8, "kind": "birthday", "completed_years": None},
        timeclock_language="en",
    )

    assert "Happy Birthday! 🎉" in html
    assert "Happy Birthday, " not in html


def test_celebration_template_renders_completed_anniversary_years():
    html = _env().get_template("timeclock_celebration.html").render(
        person={"name": "Maria Garcia", "spanish_level": 0},
        token="t",
        celebration={"id": 9, "kind": "work_anniversary", "completed_years": 5},
        timeclock_language="en",
    )

    assert "Happy 5-Year Work Anniversary, Maria!" in html


def test_celebration_template_is_spanish_primary_for_level_three():
    html = _env().get_template("timeclock_celebration.html").render(
        person={"name": "Maria Garcia", "spanish_level": 3},
        token="t",
        celebration={"id": 8, "kind": "birthday", "completed_years": None},
        timeclock_language="es_primary",
    )

    assert html.index("¡Feliz cumpleaños") < html.index("Happy Birthday")


def test_anniversary_pto_notification_template_is_bilingual():
    html = _env().get_template("timeclock_notifications.html").render(
        person={"name": "Maria Garcia", "spanish_level": 3},
        token="t",
        notifications=[{
            "kind": "anniversary_pto_reminder",
            "anniversary_label": "October 2",
            "balance_label": "2.5 days",
            "anniversary_date": date(2026, 10, 2),
            "balance_amount": Decimal("2.5"),
        }],
        timeclock_language="es_primary",
    )

    assert html.index("Se acerca tu aniversario de trabajo") < html.index(
        "Your work anniversary is coming up"
    )
    assert "2.5 days" in html
    assert html.index("Confirmo que lo leí") < html.index("I acknowledge")
