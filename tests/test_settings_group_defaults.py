from pathlib import Path


def test_settings_group_table_has_default_people_picker():
    html = Path("src/zira_dashboard/templates/settings.html").read_text()
    assert "group_default_people_present__{{ g.name }}" in html
    assert 'name="group_default_people__{{ g.name }}"' in html
    assert "g.default_people" in html
    assert "g.default_conflicts" in html
    assert "protected anchors used when Auto runs" in html


def test_settings_route_uses_one_atomic_default_replacement():
    source = Path("src/zira_dashboard/routes/settings.py").read_text()
    handler = source.split("async def settings_save_work_centers", 1)[1].split(
        '@router.post("/settings")', 1
    )[0]
    assert "replace_default_targets(" in handler
    assert "group_default_people__" in handler
    assert 'updates["default_people"]' not in handler
