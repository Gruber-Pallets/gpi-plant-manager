from pathlib import Path


def test_people_matrix_odoo_link_is_visible_on_keyboard_focus():
    css = Path("src/zira_dashboard/static/skills.css").read_text()

    assert ".odoo-link:focus-visible" in css
    assert "opacity: 1 !important" in css


def test_people_matrix_sort_uses_button_triggers():
    js = Path("src/zira_dashboard/static/skills-page.js").read_text()

    # Sorting binds to the header's matrix-sort-trigger button (native
    # keyboard), no longer to a role="button" th keydown handler.
    assert "matrix-sort-trigger" in js
    assert "th.addEventListener('keydown'" not in js


def test_settings_gear_does_not_trigger_sort():
    js = Path("src/zira_dashboard/static/skills-page.js").read_text()

    assert "automation-settings-trigger" in js
    assert "event.stopPropagation()" in js
    assert "matrix-sort-trigger" in js


def test_automation_modal_posts_and_restores_focus():
    js = Path("src/zira_dashboard/static/skills-page.js").read_text()

    assert "'/staffing/skills/automation/' + " in js
    assert "lastAutomationTrigger.focus()" in js


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
