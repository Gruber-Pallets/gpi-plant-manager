from pathlib import Path


def test_people_matrix_odoo_link_is_visible_on_keyboard_focus():
    css = Path("src/zira_dashboard/static/skills.css").read_text()

    assert ".odoo-link:focus-visible" in css
    assert "opacity: 1 !important" in css


def test_people_matrix_sort_headers_handle_keyboard_activation():
    js = Path("src/zira_dashboard/static/skills-page.js").read_text()

    assert "th.addEventListener('keydown'" in js
    assert "e.key === 'Enter'" in js
    assert "e.key === ' '" in js


def test_people_matrix_skill_sort_reads_any_skill_display_control():
    js = Path("src/zira_dashboard/static/skills-page.js").read_text()

    assert "td.querySelector('.skill-display')" in js
    assert "td.querySelector('span.skill-display')" not in js


def test_people_matrix_view_popover_closes_on_escape():
    js = Path("src/zira_dashboard/static/skills-page.js").read_text()

    assert "document.addEventListener('keydown'" in js
    assert "e.key === 'Escape'" in js
    assert "btn.focus()" in js


def test_people_matrix_refresh_button_exposes_busy_state():
    js = Path("src/zira_dashboard/static/skills-page.js").read_text()

    assert "btn.setAttribute('aria-busy', 'true')" in js
    assert "btn.setAttribute('aria-busy', 'false')" in js


def test_people_matrix_skill_picker_posts_live_cell_update():
    js = Path("src/zira_dashboard/static/skills-page.js").read_text()

    assert "initSkillCellPicker" in js
    assert "fetch('/staffing/skills/cell'" in js
    assert "person_odoo_id" in js
    assert "skill_odoo_id" in js
    assert "updateSkillButton" in js


def test_people_matrix_skill_picker_surfaces_odoo_saved_local_warning():
    js = Path("src/zira_dashboard/static/skills-page.js").read_text()

    assert "data.warning" in js
    assert "showSavedToast(null, data.warning" in js


def test_people_matrix_error_toasts_are_assertively_announced():
    js = Path("src/zira_dashboard/static/skills-page.js").read_text()

    assert (
        "if (errorMsg) {\n"
        "      el.setAttribute('role', 'alert');\n"
        "      el.setAttribute('aria-live', 'assertive');\n"
        "    }"
    ) in js


def test_people_matrix_skill_picker_handles_escape_and_focus_return():
    js = Path("src/zira_dashboard/static/skills-page.js").read_text()

    assert "skill-picker" in js
    assert "e.key === 'Escape'" in js
    assert "activeSkillButton.focus()" in js


def test_people_matrix_skill_picker_css_exists():
    css = Path("src/zira_dashboard/static/skills.css").read_text()

    assert ".skill-cell-btn" in css
    assert ".skill-cell-btn.saving" in css
    assert ".skill-picker" in css
