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
