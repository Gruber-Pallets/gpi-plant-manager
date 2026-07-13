from pathlib import Path


def _template():
    return Path("src/zira_dashboard/templates/staffing.html").read_text()


def _script():
    return Path("src/zira_dashboard/static/staffing.js").read_text()


def _print_css():
    return Path("src/zira_dashboard/static/staffing-print.css").read_text()


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


def test_current_published_schedule_has_a_local_edit_gate_but_snapshot_does_not():
    html = _template()
    js = _script()

    assert "{% if published and not viewing_posted %}" in html
    assert 'id="edit-schedule-btn"' in html
    assert 'name="viewing_posted" value="1"' in html
    assert "const __editScheduleBtn = document.getElementById('edit-schedule-btn');" in js
    assert "if (__viewingPosted) return;" in js
    assert "__unlocked = true;" in js
    assert "__form.classList.remove('locked');" in js
    assert "__editScheduleBtn.disabled = true;" in js
    assert "__editScheduleBtn.hidden = true;" in js


def test_posted_snapshot_blocks_autosave_and_mutating_client_handlers():
    js = _script()
    picker_handler = js.split("const item = e.target.closest('.multi-dd .dd-item');", 1)[1].split(
        "// ---------- Per-dropdown quick clear", 1
    )[0]
    autosave = js.split("function fireSave()", 1)[1].split("function onEdit()", 1)[0]

    assert "if (__viewingPosted) return;" in js
    assert "if (__viewingPosted) { return; }" in js
    assert autosave.index("if (__viewingPosted) { return; }") < autosave.index("new FormData(form)")
    assert picker_handler.index("if (__viewingPosted) return;") < picker_handler.index(
        "e.preventDefault();"
    )


def test_staffing_slack_post_button_exposes_busy_state():
    html = _template()
    js = _script()

    assert 'class="publish-btn icon-btn share-btn" onclick="postToSlack(this)" title="Post to Slack" aria-label="Post to Slack" aria-busy="false"' in html
    assert "btn.setAttribute('aria-busy', 'true');" in js
    assert "btn.setAttribute('aria-busy', 'false');" in js


def test_staffing_print_hides_time_off_sync_note_and_top_aligns_context():
    css = _print_css()

    assert ".timeoff .ts-note," in css
    assert ".section.timeoff {" in css
    assert "align-self: start;" in css
    assert "padding-top: 0;" in css


def test_forklift_live_recalc_hooks_assignment_changes():
    js = _script()

    assert "function erlangCWaitSeconds(c, lambdaPerHr, meanHandleSeconds)" in js
    assert "function recalcForkliftBaySummary()" in js
    assert "window.FORKLIFT_LIVE_MODEL" in js
    assert "details.sched-dd[data-loc=\"" in js
    assert "recalcForkliftBaySummary();" in js
    assert "Predicted Time-to-Claim " in js
    assert "TTC overloaded" in js


def test_reset_to_defaults_reconciles_left_rail():
    js = _script()

    assert "function syncLeftRailWithSchedule()" in js
    assert "syncLeftRailWithSchedule();" in js
    assert "const scheduledNames = new Set();" in js
    assert 'details.sched-dd input[name^="loc__"]:checked' in js
    assert "Object.keys(__peopleMeta || {}).forEach(name => {" in js
    assert "scheduledNames.has(name)" in js
    assert "addBackToCorrectList(name);" in js


def test_auto_toggle_removes_only_disabled_center_warnings():
    js = _script()

    assert "function removeDisabledAutoWarnings()" in js
    assert "warning.startsWith(center + ' is staffed below its minimum')" in js
    assert "warning === 'No safe operator pairing available for ' + center + '.'" in js
    assert "renderWarnings((window.ROTATION_WARNINGS || []).filter" in js
    assert "removeDisabledAutoWarnings();" in js


def test_auto_capacity_dialog_has_replacement_controls():
    html = _template()
    js = _script()

    assert 'id="auto-capacity-dialog"' in html
    assert 'aria-labelledby="auto-capacity-title"' in html
    assert 'id="auto-capacity-replacements"' in html
    assert "required_disable_count" in js
    assert "turn_off" in js
    assert "showAutoCapacityDialog" in js


def test_disabled_auto_warning_filter_keeps_capacity_warning_visible():
    js = _script()

    assert "Auto centers need " in js
    assert "warning.startsWith(center + ' could not be staffed to its minimum')" in js


def test_auto_capacity_replacement_payload_excludes_selected_turn_off_centers():
    js = _script()

    assert "const workCenters = [...new Set([requestedCenter, ...selectedAutoCenters()])]\n          .filter(center => !turnOff.includes(center));" in js


def test_auto_center_success_requires_server_enabled_centers():
    js = _script()

    assert js.count("Array.isArray(data.enabled_work_centers)") == 2
    assert "data.enabled_work_centers || requestedWorkCenters" not in js
    assert "data.enabled_work_centers || workCenters.filter" not in js


def test_clear_schedule_is_distinct_from_reset_and_uses_existing_autosave_flow():
    html = _template()
    js = _script()
    css = Path("src/zira_dashboard/static/staffing.css").read_text()
    reset_handler = js.split("const __resetBtn = document.getElementById('reset-schedule-btn');", 1)[1].split(
        "const __clearBtn = document.getElementById('clear-schedule-btn');", 1
    )[0]
    clear_handler = js.split("const __clearBtn = document.getElementById('clear-schedule-btn');", 1)[1].split(
        "// ---------- Undo / Redo helpers ----------", 1
    )[0]

    assert 'id="reset-schedule-btn" class="clear-btn">Reset to defaults</button>' in html
    assert 'id="clear-schedule-btn" class="clear-btn clear-schedule-btn">Clear schedule</button>' in html
    assert "Reset every Scheduled cell to the page defaults?" in js
    assert "Clear every Scheduled cell for this day?" in js
    assert "const __resetBtn = document.getElementById('reset-schedule-btn');" in js
    assert "const __clearBtn = document.getElementById('clear-schedule-btn');" in js
    assert "cb.checked = false;" in js
    assert "item.classList.remove('selected');" in js
    assert "syncLeftRailWithSchedule();" in js
    assert "refreshPickerVisibility();" in js
    assert "kickAutosave();" in js
    assert ".clear-schedule-btn:hover" in css
    assert "if (__viewingPosted) return;" in reset_handler
    assert reset_handler.index("if (__viewingPosted) return;") < reset_handler.index(
        'if (!confirm("Reset every Scheduled cell to the page defaults?'
    )
    assert "if (__viewingPosted) return;" in clear_handler
    assert clear_handler.index("if (__viewingPosted) return;") < clear_handler.index(
        "if (!confirm('Clear every Scheduled cell for this day?"
    )
