from pathlib import Path


BASE = Path("src/zira_dashboard/templates/timeclock_base.html")


def _html() -> str:
    return BASE.read_text(encoding="utf-8")


def test_timeclock_has_persistent_feedback_trigger_outside_htmx_swap():
    html = _html()

    screen_end = html.index("</div>", html.index('<div id="timeclock-screen">'))
    trigger = html.index('id="timeclock-feedback-open"')
    assert trigger > screen_end
    assert "data-feedback-open" in html[trigger:]
    assert 'aria-controls="fb-modal"' in html[trigger:]
    assert "{% include '_feedback.html' %}" in html[trigger:]


def test_timeclock_feedback_bar_reserves_space_instead_of_covering_controls():
    html = _html()

    assert ".k-feedback-bar" in html
    assert "flex: 0 0 auto" in html
    start = html.index(".k-feedback-bar")
    rule = html[start:html.index("}", start)]
    assert "position: fixed" not in rule
    assert ".k-feedback-trigger" in html
    assert "min-height: 48px" in html
