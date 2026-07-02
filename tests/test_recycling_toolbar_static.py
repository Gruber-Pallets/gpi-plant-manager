from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "src" / "zira_dashboard" / "templates" / "recycling.html"
CSS = ROOT / "src" / "zira_dashboard" / "static" / "recycling.css"


def test_recycling_range_toolbar_sits_below_subnav_not_header():
    html = TEMPLATE.read_text(encoding="utf-8")
    header_end = html.index("</header>")
    subnav = html.index('{% include "_dashboards_subnav.html" %}')
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
