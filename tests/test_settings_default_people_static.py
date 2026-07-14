from pathlib import Path


def test_partial_default_crew_remains_an_advisory_warning():
    js = Path("src/zira_dashboard/static/settings.js").read_text()

    assert "checked > 0 && checked < min" in js
    assert "title: loc + ' · Fewer than min'" in js
    assert "overrideLabel: 'OK'" in js
    assert "onCancel: () => { picker.open = true; }" in js
