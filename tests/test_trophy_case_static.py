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
