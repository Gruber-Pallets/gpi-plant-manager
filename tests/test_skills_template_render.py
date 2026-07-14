from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path("src/zira_dashboard/templates")


def _env():
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    env.globals["static_v"] = lambda _f: "test"
    env.globals["goat_holders"] = lambda: {}
    # _topnav.html server-renders the Inbox badge via this global (see deps.py).
    env.globals["nav_inbox_summary"] = lambda: {
        "total": 0, "urgent_total": 0, "source_errors": [],
    }
    return env


_DEFAULT_AUTOMATION_GROUPS = {
    "Repair": {
        "group": "Repair",
        "settings": {"level_3_min": 90.0, "level_2_min": 80.0, "level_1_min": 70.0},
        "last_run": None,
        "work_centers": [{"name": "Repair 1", "goal": 100.0}],
    }
}


def _render_skills_html(
    *, employee_id=None, odoo_url="", skill_odoo_id=88, automation_groups=None
):
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
        skills=[{"name": "Repair", "odoo_id": skill_odoo_id, "skill_type": "Production Skills"}],
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
        automation_groups=_DEFAULT_AUTOMATION_GROUPS
        if automation_groups is None
        else automation_groups,
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


def test_people_matrix_active_badge_has_status_label():
    html = _render_skills_html()

    assert 'class="active-badge on"' in html
    assert 'aria-label="Active"' in html


def test_people_matrix_sort_headers_use_button_triggers():
    html = _render_skills_html()

    # Sorting is a real <button> inside the th (keyboard-native), not a
    # role="button" th, so a settings gear can sit beside it without nesting
    # one activatable control inside another.
    assert 'class="matrix-sort-trigger" aria-label="Sort by Name"' in html
    assert 'class="matrix-sort-trigger" aria-label="Sort by Repair"' in html
    assert 'role="button" tabindex="0"' not in html


def test_repair_header_has_sort_and_automation_buttons():
    html = _render_skills_html()

    assert 'class="matrix-sort-trigger"' in html
    assert 'class="automation-settings-trigger" data-automation-skill="Repair"' in html
    assert 'aria-label="Configure automatic Repair skills"' in html
    assert 'id="automation-modal-backdrop"' in html


def test_non_automated_skill_has_no_gear():
    html = _render_skills_html(
        automation_groups={
            "Repair": _DEFAULT_AUTOMATION_GROUPS["Repair"],
        }
    )

    # Only Repair/Dismantle get a gear; a plain production skill must not.
    assert html.count('class="automation-settings-trigger"') == 1


def test_automation_groups_bootstrapped_for_javascript():
    html = _render_skills_html()

    assert "window.AUTOMATION_GROUPS =" in html
    assert '"work_centers"' in html or "work_centers" in html


def test_people_matrix_skill_cell_is_button_when_odoo_ids_exist():
    html = _render_skills_html(employee_id=42, skill_odoo_id=88)

    assert 'class="skill-cell-btn skill-display lvl-2"' in html
    assert 'data-person-odoo-id="42"' in html
    assert 'data-skill-odoo-id="88"' in html
    assert 'data-skill-name="Repair"' in html
    assert 'data-level="2"' in html
    assert 'aria-label="Edit Maria Garcia Repair skill, current level 2 competent"' in html


def test_people_matrix_skill_cell_stays_readonly_without_skill_odoo_id():
    html = _render_skills_html(employee_id=42, skill_odoo_id=None)

    assert 'data-skill-odoo-id=' not in html
    assert 'class="skill-display lvl-2"' in html
    assert 'skill-cell-btn' not in html


def test_people_matrix_view_popover_has_accessible_relationship():
    html = _render_skills_html()

    assert 'id="view-btn" aria-haspopup="dialog" aria-expanded="false" aria-controls="view-popover"' in html
    assert 'id="view-popover" role="dialog" aria-label="People Matrix view options" hidden' in html


def test_people_matrix_sync_status_is_announced_politely():
    html = _render_skills_html()

    assert 'id="sync-status" role="status" aria-live="polite"' in html


def test_people_matrix_refresh_button_starts_not_busy():
    html = _render_skills_html()

    assert 'id="refresh-btn" aria-busy="false"' in html
