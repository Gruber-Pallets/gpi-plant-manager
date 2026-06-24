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


def test_people_matrix_filter_has_accessible_name():
    person = SimpleNamespace(
        name="Maria Garcia",
        active=True,
        reserve=False,
        employee_id=None,
        skills={"Repair": 2},
    )

    html = _env().get_template("skills.html").render(
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
        odoo_url="",
    )

    assert 'id="wheel-filter"' in html
    assert 'aria-label="Filter people"' in html
