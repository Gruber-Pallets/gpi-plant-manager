from pathlib import Path


def _template():
    return Path("src/zira_dashboard/templates/staffing.html").read_text()


def _script():
    return Path("src/zira_dashboard/static/staffing.js").read_text()


def test_staffing_partial_time_off_controls_name_the_person():
    html = _template()
    js = _script()

    assert 'aria-label="Clear partial time off for {{ n }}"' in html
    assert 'aria-label="Clear partial time off for {{ e.name }}"' in html
    assert 'aria-label="Restore partial time off for {{ c.name }}"' in html
    assert 'aria-label="Clear partial time off for {{ a.name }}"' in html
    assert "btn.setAttribute('aria-label', 'Clear partial time off for ' + name);" in js


def test_staffing_custom_hours_controls_are_named_and_busy():
    html = _template()
    js = _script()

    assert 'id="hours-pill"' in html
    assert 'aria-label="Edit shift hours for {{ day }}"' in html
    assert 'id="hours-start" value="{{ eff_hours_start }}" step="300" aria-label="Shift start time"' in html
    assert 'id="hours-end"   value="{{ eff_hours_end }}"   step="300" aria-label="Shift end time"' in html
    assert 'class="b-start" value="{{ b.start }}" step="60" aria-label="Break start time"' in html
    assert 'class="b-end"   value="{{ b.end }}"   step="60" aria-label="Break end time"' in html
    assert 'class="b-name"  value="{{ b.name }}" maxlength="40" aria-label="Break name"' in html
    assert 'class="remove-btn" title="Remove break" aria-label="Remove break"' in html
    assert 'class="save"   id="hours-save" aria-busy="false"' in html
    assert 'aria-label="Break start time"' in js
    assert 'aria-label="Break end time"' in js
    assert 'aria-label="Break name"' in js
    assert 'aria-label="Remove break"' in js
    assert "save.disabled = true;" in js
    assert "save.setAttribute('aria-busy', 'true');" in js
    assert "save.disabled = false;" in js
    assert "save.setAttribute('aria-busy', 'false');" in js


def test_staffing_custom_hours_panel_manages_focus_and_escape():
    html = _template()
    js = _script()

    assert 'aria-controls="hours-editor"' in html
    assert 'aria-expanded="false"' in html
    assert 'id="hours-editor" class="hours-editor" role="dialog" aria-modal="false" aria-labelledby="hours-editor-title" hidden' in html
    assert '<h4 id="hours-editor-title">Custom hours for {{ day }}</h4>' in html
    assert "pill.setAttribute('aria-expanded', 'true');" in js
    assert "pill.setAttribute('aria-expanded', 'false');" in js
    assert "document.getElementById('hours-start').focus();" in js
    assert "pill.focus();" in js
    assert "document.addEventListener('keydown'" in js
    assert "e.key === 'Escape'" in js


def test_staffing_publish_submit_buttons_expose_busy_state():
    html = _template()
    js = _script()

    assert 'class="override-btn publish-submit" aria-busy="false"' in html
    assert 'class="publish-btn publish-submit" aria-busy="false"' in html
    assert "form.addEventListener('submit'" in js
    assert "event.submitter" in js
    assert "submitter.value !== 'publish'" in js
    assert "button.disabled = true;" in js
    assert "button.setAttribute('aria-busy', 'true');" in js


def test_staffing_publish_busy_state_preserves_publish_action():
    js = _script()

    assert "publishIntent.name = 'action';" in js
    assert "publishIntent.value = 'publish';" in js
    assert "form.appendChild(publishIntent);" in js


def test_staffing_slack_post_button_exposes_busy_state():
    html = _template()
    js = _script()

    assert 'class="publish-btn icon-btn share-btn" onclick="postToSlack(this)" title="Post to Slack" aria-label="Post to Slack" aria-busy="false"' in html
    assert "btn.setAttribute('aria-busy', 'true');" in js
    assert "btn.setAttribute('aria-busy', 'false');" in js
