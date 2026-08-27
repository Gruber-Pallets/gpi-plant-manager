"""Private Timeclock celebration presentation and accessibility coverage."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from zira_dashboard import employee_celebrations
from zira_dashboard.app import app
from zira_dashboard.routes import timeclock


client = TestClient(app)


def _base_template() -> str:
    return Path("src/zira_dashboard/templates/timeclock_base.html").read_text()


def _celebration_template() -> str:
    return Path("src/zira_dashboard/templates/timeclock_celebration.html").read_text()


def test_celebration_styles_disable_confetti_and_transforms_for_reduced_motion():
    source = _base_template()

    assert "@media (prefers-reduced-motion: reduce)" in source
    assert ".celebration-confetti" in source
    reduced_motion = source.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert "animation: none" in reduced_motion
    assert "transform: none" in reduced_motion
    assert ".celebration-continue:active" in reduced_motion
    assert "transition: none" in reduced_motion
    assert (
        "#timeclock-screen.htmx-swapping {\n"
        "      opacity: 1 !important;\n"
        "      transition: none !important;"
    ) in reduced_motion


def test_celebration_template_keeps_confetti_decorative_and_the_event_private():
    source = _celebration_template()

    assert '<main class="k-main celebration-screen"' in source
    assert '<h1 id="celebration-title">' in source
    assert 'aria-hidden="true"' in source
    assert "celebration-confetti" in source
    assert 'class="k-btn celebration-continue"' in source
    assert "people" not in source
    assert "coworker" not in source
    assert "<script" not in source
    assert "<img" not in source


def test_celebration_route_renders_the_private_event_screen(monkeypatch):
    person = {
        "id": 1,
        "name": "Maria Garcia",
        "odoo_id": 5,
        "spanish_level": 0,
    }
    celebration = employee_celebrations.Celebration(
        8, 5, "birthday", date(2026, 8, 27), None
    )
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _person_id: person)
    monkeypatch.setattr(employee_celebrations, "next_due", lambda *_: celebration)

    response = client.get(f"/timeclock/celebration/{timeclock._mint_token(1)}")

    assert response.status_code == 200
    assert "Happy Birthday, Maria!" in response.text
    assert "Garcia" not in response.text
    assert 'name="celebration_id" value="8"' in response.text
