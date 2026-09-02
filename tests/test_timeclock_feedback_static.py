from pathlib import Path


BASE = Path("src/zira_dashboard/templates/timeclock_base.html")


def _html() -> str:
    return BASE.read_text(encoding="utf-8")


def test_timeclock_renders_shared_whats_new_panel_outside_htmx_swap():
    html = _html()

    screen_end = html.index("</div>", html.index('<div id="timeclock-screen">'))
    footer = html.index("{% include '_footer.html' %}")

    assert footer > screen_end
    assert "k-feedback-bar" not in html
    assert "timeclock-feedback-open" not in html
    assert "{% include '_feedback.html' %}" not in html


def test_timeclock_idle_redirect_pauses_while_feedback_is_open():
    html = _html()

    assert "var feedbackPaused = false;" in html
    assert "if (feedbackPaused) return;" in html
    assert "gpi:feedback-opened" in html
    assert "feedbackPaused = true;" in html
    assert "clearTimeout(timer);" in html
    assert "gpi:feedback-closed" in html
    assert "feedbackPaused = false;" in html


def test_timeclock_feedback_asks_for_and_posts_one_employee_id():
    feedback_html = Path("src/zira_dashboard/templates/_feedback.html").read_text(
        encoding="utf-8"
    )
    feedback_js = Path("src/zira_dashboard/static/feedback.js").read_text(
        encoding="utf-8"
    )

    assert "Who is submitting this?" in feedback_html
    assert 'id="fb-submitter"' in feedback_html
    assert "window.location.pathname.indexOf('/timeclock') === 0" in feedback_js
    assert "window.gpiFetch('/api/feedback/submitters')" in feedback_js
    assert "form.append('submitter_employee_id', submitter.value)" in feedback_js
