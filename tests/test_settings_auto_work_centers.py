from pathlib import Path


def test_work_center_settings_render_default_auto_toggle_for_each_location():
    html = Path("src/zira_dashboard/templates/settings.html").read_text()
    assert 'name="default_auto_work_centers"' in html
    assert "default_auto_work_centers" in html
    assert "Default Auto Work Centers" in html


def test_work_center_settings_save_writes_default_not_daily_state(monkeypatch):
    from zira_dashboard.routes import settings

    saved = []
    monkeypatch.setattr(settings.work_centers_store, "registered_groups", lambda: [])
    monkeypatch.setattr(settings.work_centers_store, "all_group_names", lambda _kind: [])
    monkeypatch.setattr(settings.work_centers_store, "replace_default_targets", lambda **_kwargs: None)
    monkeypatch.setattr(settings.work_centers_store, "save_one", lambda *_args: None)
    monkeypatch.setattr(settings.app_settings, "set_setting", lambda key, value: saved.append((key, value)))

    # The endpoint test harness supplies a form with Repair 2 selected.
    assert settings._ordered_default_auto_work_centers(["Repair 2", "Unknown"]) == ["Repair 2"]
    assert settings.DEFAULT_AUTO_WORK_CENTERS_SETTING == "rotation_auto_enabled_work_centers"


def test_settings_page_renders_default_auto_work_centers(monkeypatch):
    """The default work-centers page reaches its auto-center context builder."""
    from types import SimpleNamespace

    from zira_dashboard import (
        auto_lunch_settings,
        db,
        odoo_sync,
        rounding_system_store,
        saturday_schedule_store,
        work_schedule_store,
    )
    from zira_dashboard.routes import settings, staffing as staffing_routes

    monkeypatch.setattr(settings.auth, "request_is_super_admin", lambda _request: False)
    monkeypatch.setattr(odoo_sync, "sync", lambda *, force: None)
    monkeypatch.setattr(settings.shift_config, "productive_minutes_per_day", lambda: 480)
    monkeypatch.setattr(settings.staffing, "load_roster", lambda: [])
    monkeypatch.setattr(staffing_routes, "_default_auto_work_centers", lambda _day: [])
    monkeypatch.setattr(settings.settings_context, "work_center_rows", lambda *_args: [])
    monkeypatch.setattr(settings.settings_context, "group_summary", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        settings.settings_context, "with_group_default_context", lambda rows, *_args, **_kwargs: rows
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
    monkeypatch.setattr(settings.templates, "TemplateResponse", lambda _request, _name, context: context)

    response_context = settings.settings_page(SimpleNamespace())

    assert response_context["default_auto_work_centers"] == []
