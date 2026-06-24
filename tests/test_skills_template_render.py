from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path("src/zira_dashboard/templates")


def _env():
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    env.globals["static_v"] = lambda _f: "test"
    env.globals["goat_holders"] = lambda: {}
    return env


def _render_skills_html(*, employee_id=None, odoo_url=""):
    person = SimpleNamespace(
        name="Maria Garcia",
        active=True,
        reserve=False,
        employee_id=employee_id,
        skills={"Repair": 2},
    )

    return _env().get_template("skills.html").render(
        active="skills",
        active_count=1,
        inactive_count=0,
        skills=["Repair"],
        type_by_skill={"Repair": "Production Skills"},
        hidden_skills=[],
        person_certs={},
        people=[person],
        views=[],
        default_view_name=None,
        default_view_state=None,
        sync_last_at=None,
        sync_error=None,
        odoo_url=odoo_url,
    )


def test_people_matrix_filter_has_accessible_name():
    html = _render_skills_html()

    assert 'id="wheel-filter"' in html
    assert 'aria-label="Filter people"' in html


def test_people_matrix_reserve_checkbox_names_person():
    html = _render_skills_html()

    assert 'name="reserve__Maria Garcia"' in html
    assert 'aria-label="Reserve Maria Garcia"' in html


def test_people_matrix_odoo_link_names_person():
    html = _render_skills_html(employee_id=42, odoo_url="https://odoo.example")

    assert "https://odoo.example/web#id=42" in html
    assert 'aria-label="Open Maria Garcia in Odoo"' in html
