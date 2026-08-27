"""Settings contracts for the Staffing Hours payroll-period configuration."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from urllib.parse import unquote_plus

from zira_dashboard.staffing_hours import PayPeriodConfig


class _FormRequest:
    def __init__(self, values):
        self._values = values

    async def form(self):
        return self._values


def _render_settings_context(monkeypatch):
    """Exercise Settings without requiring Odoo or a populated local database."""
    from zira_dashboard import (
        auto_lunch_settings,
        db,
        odoo_sync,
        rounding_system_store,
        saturday_schedule_store,
        work_schedule_store,
    )
    from zira_dashboard.routes import settings

    monkeypatch.setattr(settings.auth, "request_is_super_admin", lambda _request: False)
    monkeypatch.setattr(odoo_sync, "sync", lambda *, force: None)
    monkeypatch.setattr(settings.shift_config, "productive_minutes_per_day", lambda: 480)
    monkeypatch.setattr(settings.staffing, "load_roster", lambda: [])
    monkeypatch.setattr(settings, "_default_auto_work_centers", lambda _day: [])
    monkeypatch.setattr(settings.settings_context, "work_center_rows", lambda *_args: [])
    monkeypatch.setattr(settings.settings_context, "group_summary", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        settings.settings_context,
        "with_group_default_context",
        lambda rows, *_args, **_kwargs: rows,
    )
    monkeypatch.setattr(settings.work_centers_store, "default_target_conflicts", lambda: {})
    monkeypatch.setattr(settings.schedule_store, "current", lambda: SimpleNamespace())
    monkeypatch.setattr(settings.settings_context, "schedule_context", lambda *_args: {})
    monkeypatch.setattr(work_schedule_store, "all_overrides", lambda: [])
    monkeypatch.setattr(rounding_system_store, "all_systems", lambda: [])
    monkeypatch.setattr(rounding_system_store, "department_map", lambda: {})
    monkeypatch.setattr(settings.settings_context, "work_schedule_context", lambda *_args: [])
    monkeypatch.setattr(settings.settings_context, "rounding_system_context", lambda *_args: [])
    monkeypatch.setattr(settings.settings_context, "department_rounding_context", lambda *_args: [])
    monkeypatch.setattr(db, "query", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(saturday_schedule_store, "current", lambda: SimpleNamespace())
    monkeypatch.setattr(settings.settings_context, "saturday_schedule_context", lambda *_args: {})
    monkeypatch.setattr(auto_lunch_settings, "current", lambda: SimpleNamespace())
    monkeypatch.setattr(settings.settings_context, "auto_lunch_context", lambda *_args: {})
    monkeypatch.setattr(settings.work_centers_store, "synced_departments", lambda: [])
    monkeypatch.setattr(settings.work_centers_store, "registered_groups", lambda: [])
    monkeypatch.setattr(
        settings.templates,
        "TemplateResponse",
        lambda _request, _name, context: context,
    )

    return settings.settings_page(SimpleNamespace(), section="timeclock")


def test_hours_pay_period_form_renders_current_anchor_and_cycle(monkeypatch):
    from zira_dashboard.routes import settings

    monkeypatch.setattr(
        settings.staffing_hours,
        "current_pay_period_config",
        lambda: PayPeriodConfig(date(2026, 8, 16), 14),
    )

    context = _render_settings_context(monkeypatch)

    assert context["staffing_hours_pay_period"] == {
        "anchor": "2026-08-16",
        "cycle_days": 14,
    }


def test_hours_pay_period_form_includes_bounded_inputs_and_visible_errors():
    from pathlib import Path

    html = Path("src/zira_dashboard/templates/settings.html").read_text()

    assert '<form method="post" action="/settings/staffing-hours-pay-period">' in html
    assert 'type="date" name="anchor" value="{{ staffing_hours_pay_period.anchor }}"' in html
    assert 'type="number" name="cycle_days" min="1" max="31" step="1"' in html
    assert "{% if error %}" in html
    assert "{{ error }}" in html


def test_hours_pay_period_post_saves_only_through_domain_api(monkeypatch):
    from zira_dashboard.routes import settings

    saved = []
    monkeypatch.setattr(
        settings.staffing_hours,
        "save_pay_period_config",
        lambda anchor, cycle_days: saved.append((anchor, cycle_days)),
    )

    response = asyncio.run(
        settings.settings_save_staffing_hours_pay_period(
            _FormRequest({"anchor": "2026-09-01", "cycle_days": "15"})
        )
    )

    assert saved == [("2026-09-01", "15")]
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1&section=timeclock"


def test_hours_pay_period_post_rejects_invalid_anchor(monkeypatch):
    from zira_dashboard.routes import settings

    monkeypatch.setattr(
        settings.staffing_hours,
        "save_pay_period_config",
        lambda *_args: (_ for _ in ()).throw(
            ValueError("Pay-period anchor must be a valid ISO date.")
        ),
    )

    response = asyncio.run(
        settings.settings_save_staffing_hours_pay_period(
            _FormRequest({"anchor": "not-a-date", "cycle_days": "14"})
        )
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith(
        "/settings?section=timeclock&error="
    )
    assert unquote_plus(response.headers["location"].split("error=", 1)[1]) == (
        "Pay-period anchor must be a valid ISO date."
    )
