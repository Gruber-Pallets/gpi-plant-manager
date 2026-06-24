from pathlib import Path


def _template():
    return Path("src/zira_dashboard/templates/past_schedules.html").read_text()


def test_past_schedules_popover_has_dialog_semantics_and_focus_restore():
    html = _template()

    assert "let popoverOpener = null;" in html
    assert "pop.setAttribute('role', 'dialog');" in html
    assert "pop.setAttribute('aria-modal', 'true');" in html
    assert "pop.setAttribute('aria-labelledby', 'past-schedules-popover-title');" in html
    assert "popoverOpener.focus();" in html
    assert "document.addEventListener('keydown'" in html
    assert "e.key === 'Escape'" in html
    assert "actions.querySelector('button').focus();" in html


def test_past_schedules_password_prompt_has_accessible_name():
    html = _template()

    assert "label.setAttribute('for', 'past-schedules-admin-password');" in html
    assert "label.textContent = 'Admin password';" in html
    assert "pwInput.id = 'past-schedules-admin-password';" in html


def test_past_schedules_destructive_action_exposes_busy_state():
    html = _template()

    assert "btn.setAttribute('aria-busy', 'false');" in html
    assert "btn.disabled = true;" in html
    assert "btn.setAttribute('aria-busy', 'true');" in html
    assert "btn.setAttribute('aria-busy', 'false');" in html
