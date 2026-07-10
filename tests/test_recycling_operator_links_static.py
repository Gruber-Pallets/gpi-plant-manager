from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RECYCLING_HTML = (ROOT / "src/zira_dashboard/templates/recycling.html").read_text()
DEPARTMENT_WIDGETS = (ROOT / "src/zira_dashboard/templates/_department_dashboard_widgets.html").read_text()
RECYCLING_CSS = (ROOT / "src/zira_dashboard/static/recycling.css").read_text()


def test_recycling_person_names_link_to_operator_dashboard():
    assert "operator_links_by_wc.get(b.name)" in DEPARTMENT_WIDGETS
    assert 'class="name-primary operator-dashboard-link"' in DEPARTMENT_WIDGETS


def test_recycling_operator_links_keep_name_styling():
    assert ".operator-dashboard-link" in RECYCLING_CSS
    assert "text-decoration" in RECYCLING_CSS
