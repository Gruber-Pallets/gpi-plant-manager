from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "src" / "zira_dashboard" / "templates" / "recycling.html"
CSS = ROOT / "src" / "zira_dashboard" / "static" / "recycling.css"
SHARED = ROOT / "src" / "zira_dashboard" / "templates" / "_department_dashboard_widgets.html"


def test_recycling_range_toolbar_sits_below_subnav_not_header():
    html = TEMPLATE.read_text(encoding="utf-8")
    header_end = html.index("</header>")
    subnav = html.index('{% include "_performance_subnav.html" %}')
    toolbar = html.index('<form class="rc-toolbar"')

    assert toolbar > header_end
    assert toolbar > subnav


def test_recycling_edit_bar_shares_toolbar_row():
    # 2026-07-02: the edit bar (save indicator + Reset Layout) moved into
    # the range-chip toolbar row to reclaim vertical space.
    html = TEMPLATE.read_text(encoding="utf-8")
    form_start = html.index('<form class="rc-toolbar"')
    form_end = html.index("</form>", form_start)
    assert '<div class="edit-bar">' in html[form_start:form_end]

    css = CSS.read_text(encoding="utf-8")
    start = css.index(".edit-bar {")
    block = css[start:css.index("}", start)]
    assert "margin-left: auto" in block  # pushed to the right of the chips


def test_recycling_uses_shared_department_widget_macros():
    html = TEMPLATE.read_text(encoding="utf-8")
    shared = SHARED.read_text(encoding="utf-8")
    assert '_department_dashboard_widgets.html' in html
    assert "macro department_bar_chart" in shared
    assert "macro department_progress_chart" in shared
    assert "macro department_downtime_report" in shared


def test_shared_daily_progress_stays_bar_based():
    html = TEMPLATE.read_text(encoding="utf-8")
    shared = SHARED.read_text(encoding="utf-8")
    assert 'class="bars"' in shared
    assert "cumulative_progress_chart" in html
