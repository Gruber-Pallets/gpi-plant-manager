from pathlib import Path
from types import SimpleNamespace

import pytest

from zira_dashboard.routes import timeclock_time_off


PERSON_ES = {
    "id": 2,
    "name": "José",
    "odoo_id": 7,
    "wage_type": "hourly",
    "spanish_speaker": True,
    "spanish_level": 3,
}
PERSON_LEVEL_2 = {
    "id": 3,
    "name": "Luis",
    "odoo_id": 8,
    "wage_type": "hourly",
    "spanish_speaker": True,
    "spanish_level": 2,
}


def _template():
    return Path("src/zira_dashboard/templates/timeclock_time_off_request_details.html").read_text()


def _detail_template():
    return Path("src/zira_dashboard/templates/timeclock_time_off_mine_detail.html").read_text()


def _landing_template():
    return Path("src/zira_dashboard/templates/timeclock_time_off_landing.html").read_text()


def _mine_template():
    return Path("src/zira_dashboard/templates/timeclock_time_off_mine.html").read_text()


def _script():
    return Path("src/zira_dashboard/static/timeclock_time_off.js").read_text()


def _base_template():
    return Path("src/zira_dashboard/templates/timeclock_base.html").read_text()


def test_htmx_swaps_time_off_conflict_and_validation_rerenders():
    # The kiosk is hx-boost="true", so forms submit via htmx. htmx does NOT
    # swap 4xx/5xx responses by default — it fires htmx:responseError and
    # leaves the DOM untouched. That silently swallowed the time-off request/
    # edit form's 409 (overlap conflict modal) and 422 (invalid times) HTML
    # re-renders, so the green Submit button appeared to do nothing. A
    # beforeSwap handler must opt those two statuses back into the swap so the
    # returned screen (with its modal / error banner) replaces the form.
    base = _base_template()
    assert "htmx:beforeSwap" in base
    assert "=== 409" in base
    assert "=== 422" in base
    assert "shouldSwap = true" in base
    assert "isError = false" in base


def test_time_off_request_submit_exposes_busy_state():
    html = _template()
    js = _script()

    assert 'class="time-off-request-form"' in html
    assert 'id="submit-btn" class="k-btn success" aria-busy="false"' in html
    assert 'document.querySelector(".time-off-request-form")' in js
    assert "form.addEventListener(\"submit\"" in js
    assert "submitBtn.disabled = true;" in js
    assert "submitBtn.setAttribute(\"aria-busy\", \"true\");" in js


def test_time_off_cancel_submit_exposes_busy_state():
    html = _detail_template()

    assert 'class="time-off-cancel-form"' in html
    assert 'class="k-btn danger" aria-busy="false"' in html
    assert "document.querySelectorAll('.time-off-cancel-form').forEach" in html
    assert "form.addEventListener('submit'" in html
    assert "btn.disabled = true;" in html
    assert "btn.setAttribute('aria-busy', 'true');" in html


def test_landing_exposes_large_past_absence_pto_action_with_count():
    html = _landing_template()

    assert 'href="/timeclock/time-off/past-absence/{{ token }}"' in html
    assert '{{ t("Use PTO for a Past Absence") }}' in html
    assert "absence_pto_count" in html


def test_my_requests_uses_server_normalized_detail_urls():
    html = _mine_template()

    assert 'href="{{ r.detail_url }}"' in html
    assert '/timeclock/time-off/mine/{{ token }}/{{ r.id }}' not in html


@pytest.mark.parametrize(
    ("person", "expected_language"),
    [(PERSON_ES, "es_primary"), (PERSON_LEVEL_2, "en")],
)
def test_time_off_request_context_uses_personalized_language_mode(
    monkeypatch, person, expected_language
):
    captured = {}

    class FakeTemplates:
        @staticmethod
        def TemplateResponse(request, template, context):
            captured["context"] = context
            return SimpleNamespace(context=context, headers={})

    monkeypatch.setattr(timeclock_time_off, "templates", FakeTemplates())
    monkeypatch.setattr(timeclock_time_off, "_verify_token", lambda token: person["id"])
    monkeypatch.setattr(timeclock_time_off, "_person_by_id", lambda person_id: person)
    monkeypatch.setattr(timeclock_time_off, "_mint_token", lambda person_id: "fresh")
    monkeypatch.setattr(timeclock_time_off, "_refresh_and_load_balances", lambda odoo_id: [])
    monkeypatch.setattr(timeclock_time_off, "_shift_window_for", lambda odoo_id: (7.0, 15.5))
    monkeypatch.setattr(timeclock_time_off, "_fetch_visible_leave_types", lambda shape: [])
    monkeypatch.setattr(
        timeclock_time_off.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )

    timeclock_time_off.request_details(SimpleNamespace(), "token", "full_day")

    assert captured["context"]["timeclock_language"] == expected_language
