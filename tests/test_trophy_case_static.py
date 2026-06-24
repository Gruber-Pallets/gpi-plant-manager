from pathlib import Path


def _template():
    return Path("src/zira_dashboard/templates/trophy_case.html").read_text()


def test_trophy_case_override_modal_controls_have_accessible_names():
    html = _template()

    assert 'id="tc-action" aria-label="Award edit action"' in html
    assert 'id="tc-name"' in html
    assert 'aria-label="Replacement award winner"' in html
    assert 'id="tc-note"' in html
    assert 'aria-label="Award override note"' in html


def test_trophy_case_edit_buttons_have_accessible_names():
    html = _template()

    assert 'aria-label="Edit {{ g.group }} GOAT award"' in html
    assert 'aria-label="Edit top-day award for {{ s.name }}"' in html
    assert 'aria-label="Edit best {{ blk.group }} award"' in html
    assert 'aria-label="Edit best {{ w.wc }} award"' in html
    assert 'aria-label="Edit monthly ribbon for {{ s.name }}"' in html


def test_trophy_case_override_modal_has_dialog_semantics():
    html = _template()

    assert '<div class="tc-modal" role="dialog" aria-modal="true" aria-labelledby="tc-modal-title">' in html


def test_trophy_case_override_modal_closes_with_escape_and_restores_focus():
    html = _template()

    assert "var opener = null;" in html
    assert "function closeModal()" in html
    assert "opener.focus();" in html
    assert "document.addEventListener('keydown'" in html
    assert "e.key === 'Escape'" in html


def test_trophy_case_year_and_month_pickers_have_accessible_names():
    html = _template()

    assert 'id="year-picker" aria-label="Trophy year"' in html
    assert 'id="month-picker" aria-label="Ribbon month"' in html


def test_trophy_case_override_modal_focuses_action_on_open():
    html = _template()

    assert "document.getElementById('tc-action').focus();" in html


def test_trophy_case_save_button_exposes_busy_state():
    html = _template()

    assert 'id="tc-save" aria-busy="false"' in html
    assert "var saveBtn = document.getElementById('tc-save');" in html
    assert "saveBtn.disabled = true;" in html
    assert "saveBtn.setAttribute('aria-busy', 'true');" in html
    assert "saveBtn.setAttribute('aria-busy', 'false');" in html


def test_trophy_case_reopen_resets_replacement_name_row():
    html = _template()

    assert "document.getElementById('tc-name-row').style.display = 'block';" in html
