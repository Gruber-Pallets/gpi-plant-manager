from pathlib import Path
import subprocess
import textwrap


def _template():
    return Path("src/zira_dashboard/templates/staffing.html").read_text()


def _script():
    return Path("src/zira_dashboard/static/staffing.js").read_text()


def _style():
    return Path("src/zira_dashboard/static/staffing.css").read_text()


def _print_css():
    return Path("src/zira_dashboard/static/staffing-print.css").read_text()


def test_printed_scheduler_hides_saturday_off_and_time_off_rails():
    css = _print_css()

    hidden_sections = css.split(".section.reserves,", 1)[1].split("{", 1)[0]

    assert ".section.saturday-off," in hidden_sections
    assert ".section.timeoff," in hidden_sections
    assert "display: none !important;" in css


def test_scheduler_time_off_rows_expose_editor_data_and_dialog():
    html = _template()

    assert 'data-request-id="{{ e.request_id }}"' in html
    assert 'id="scheduler-time-off-editor"' in html
    assert 'id="scheduler-time-off-save"' in html
    assert 'id="scheduler-time-off-cancel"' in html


def test_scheduler_script_posts_editor_actions_and_restores_focus():
    js = _script()

    assert "/api/staffing/time-off/" in js
    assert "showModal()" in js
    assert "scheduler-time-off-editor" in js
    assert "window.location.href = '/staffing?day='" in js


def test_staffing_schedule_uses_compact_assigned_labels_and_balanced_columns():
    html = _template()
    css = _style()

    assert "{% macro scheduled_operator_name(name) %}" in html
    assert "{{ scheduled_operator_name(a.name) }}" in html
    assert 'value="{{ p.name }}"' in html
    assert "table.sched tbody td.station { min-width: 13rem; }" in css
    assert "table.sched thead th.sched-col    { width: 40%; }" in css
    assert "table.sched thead th.wc-note-col  { width: 23%; }" in css


def test_staffing_bay_cells_keep_panel_background_across_work_center_states():
    css = _style()

    active = 'tr[data-loc][data-on="true"] td { background: var(--accent-dim); }'
    inactive = 'tr.work-center-off td { background: var(--panel-2); }'
    bay_override = (
        'tr[data-loc][data-on="true"] td.bay,\n'
        '  tr.work-center-off td.bay { background: var(--panel-3); }'
    )

    assert bay_override in css
    assert css.index(bay_override) > css.index(active)
    assert css.index(bay_override) > css.index(inactive)


def test_staffing_disabled_rows_dim_non_bay_cells_only():
    css = _style()

    dimmed_non_bay_cells = 'tr.work-center-off td:not(.bay) { opacity: 0.58; }'

    assert dimmed_non_bay_cells in css
    assert 'tr.work-center-off { opacity: 0.58; }' not in css


def test_staffing_partial_time_off_controls_name_the_person():
    html = _template()
    js = _script()

    assert 'aria-label="Clear partial time off for {{ n }}"' in html
    assert 'aria-label="Clear partial time off for {{ e.name }}"' in html
    assert 'aria-label="Restore partial time off for {{ c.name }}"' in html
    assert 'aria-label="Clear partial time off for {{ a.name }}"' in html
    assert "btn.setAttribute('aria-label', 'Clear partial time off for ' + name);" in js


def test_saturday_availability_swap_is_left_rail_only_and_saves_immediately():
    html = _template()
    js = _script()
    css = _style()

    assert 'class="saturday-availability-swap"' in html
    assert 'aria-label="Move {{ n }} to Off"' in html
    assert 'aria-label="Move {{ n }} to Unassigned"' in html
    assert 'saturday-availability-confirm' not in html
    assert "/api/staffing/saturday-availability" in js
    assert "_saveSaturdayAvailability(button)" in js
    assert "button.disabled = true;" in js
    assert "showToast(error.message || 'Could not update Saturday availability.', null, 'error');" in js
    assert "showModal()" not in js[js.index("const __saturdayRecruiting"):js.index("// Partial-day off labels")]
    assert ".saturday-availability-swap { opacity: 0;" in css
    assert ".saturday-person-row:hover .saturday-availability-swap" in css
    assert ".saturday-availability-confirm" not in css


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

    assert 'class="publish-btn publish-submit" aria-busy="false"' in html
    assert 'class="override-btn publish-submit" aria-busy="false"' not in html
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


def test_staffing_publish_banner_has_no_override_or_implicit_slack_publish():
    html = _template()
    js = _script()
    slack_post = js.split("async function postToSlack(btn) {", 1)[1].split(
        "// ---------- Rotation goal", 1,
    )[0]

    assert "Override &amp; Publish" not in html
    assert "publish-override" not in html
    assert 'class="override-btn' not in html
    assert "fd.set('action', 'publish')" not in slack_post
    assert "/staffing/share-to-slack?day=" in slack_post
    assert "&version=" in slack_post


def test_header_uses_only_orange_draft_and_green_posted_toggle():
    html = _template()
    css = _style()

    assert 'class="draft-label"' not in html
    assert 'id="edit-schedule-btn"' not in html
    assert 'view-toggle-btn draft' in html
    assert 'view-toggle-btn posted' in html
    assert ".view-toggle-btn.active.draft {" in css
    assert ".view-toggle-btn.active.posted {" in css


def test_draft_has_publish_and_posted_has_delivery_actions():
    html = _template()

    assert "{% if published or viewing_posted %}" in html
    assert 'onclick="printSchedule(this)"' in html
    assert 'onclick="postToSlack(this)"' in html
    assert "discard_draft" not in html
    assert "save_notes" not in html


def test_browser_records_print_after_dialog_and_polls_live_revision():
    js = _script()

    assert "window.addEventListener('afterprint'" in js
    assert "/staffing/mark-printed?day=" in js
    assert "setInterval(checkLiveRevision, 3000);" in js
    assert "/staffing/live?day=" in js
    assert "window.SCHEDULE_REVISION = data.revision;" in js


def test_posted_save_transitions_to_draft_and_delivery_refreshes_live_revision():
    js = _script()
    autosave = js.split("function fireSave()", 1)[1].split("function onEdit()", 1)[0]
    rebuild = js.split("async function rebuild(mode, options = {}) {", 1)[1].split(
        "const resetScheduleBtn", 1
    )[0]

    assert "const __scheduleReadOnly" not in js
    assert "if (!data.published && window.SCHEDULE_PUBLISHED) {" in js
    assert autosave.index("if (queued)") < autosave.index(
        "if (!data.published && window.SCHEDULE_PUBLISHED) {"
    )
    assert "if (window.SCHEDULE_PUBLISHED) {" in rebuild
    assert rebuild.index("if (window.SCHEDULE_PUBLISHED) {") < rebuild.index(
        "setActiveMode(mode);"
    )
    assert "async function refreshScheduleRevision()" in js
    assert js.count("await refreshScheduleRevision();") == 2


def test_posted_auto_toggle_reloads_draft_and_delivery_owns_revision_window():
    js = _script()
    save_auto = js.split("async function saveAutoCenters(turnOff = []) {", 1)[1].split(
        "// Ordinary rebuilds", 1,
    )[0]
    delivery = js.split("let localDeliveryInFlight = 0;", 1)[1].split(
        "// ---------- Rotation goal", 1,
    )[0]
    live_poll = js.split("async function checkLiveRevision() {", 1)[1].split(
        "// ---------- Rotation goal", 1,
    )[0]
    slack_delivery = js.split("async function postToSlack(btn) {", 1)[1].split(
        "async function refreshScheduleRevision()", 1,
    )[0]

    assert "if (window.SCHEDULE_PUBLISHED) {" in save_auto
    assert "window.location.reload();" in save_auto
    assert "let localDeliveryInFlight = 0;" in js
    assert delivery.count("localDeliveryInFlight += 1;") == 2
    assert delivery.count("localDeliveryInFlight -= 1;") == 2
    assert delivery.index("await refreshScheduleRevision();") < delivery.index(
        "localDeliveryInFlight -= 1;"
    )
    assert delivery.rindex("await refreshScheduleRevision();") < delivery.rindex(
        "localDeliveryInFlight -= 1;"
    )
    assert "let slackDeliveryInFlight = false;" in slack_delivery
    assert slack_delivery.index("localDeliveryInFlight += 1;") < slack_delivery.index(
        "slackDeliveryInFlight = true;"
    )
    assert "if (slackDeliveryInFlight) localDeliveryInFlight -= 1;" in slack_delivery
    assert "if (localDeliveryInFlight > 0) return;" in live_poll
    assert "|| localDeliveryInFlight > 0) return;" in live_poll


def test_staffing_slack_post_button_exposes_busy_state():
    html = _template()
    js = _script()

    assert 'class="publish-btn icon-btn share-btn{% if posted_delivery.slack_posted_at %} complete{% endif %}"' in html
    assert 'onclick="postToSlack(this)"' in html
    assert "btn.setAttribute('aria-busy', 'true');" in js
    assert "btn.setAttribute('aria-busy', 'false');" in js


def test_staffing_print_hides_time_off_sync_note_and_top_aligns_context():
    css = _print_css()

    assert ".timeoff .ts-note," in css
    assert ".section.timeoff {" in css
    assert "align-self: start;" in css
    assert "padding-top: 0;" in css


def test_staffing_print_hides_schedule_goal_and_schedule_actions():
    css = _print_css()

    assert ".rotation-controls," in css
    assert ".sidebar-schedule-actions" in css


def test_staffing_print_balances_schedule_columns_and_keeps_fitting_name_pairs_inline():
    css = _print_css()

    assert "table.sched { table-layout: fixed; }" in css
    assert "table.sched thead th.n, table.sched td.bay { width: 4.5rem; }" in css
    assert "table.sched thead th.wc-col { width: 22%; }" in css
    assert "table.sched thead th.dept { width: 12%; }" in css
    assert "table.sched thead th.sched-col { width: 41%; }" in css
    assert "table.sched thead th.wc-note-col { width: 20.5%; }" in css
    assert "table.sched th.wc-col,\ntable.sched td.station { padding-right: 2pt; }" in css
    assert "table.sched th.dept,\ntable.sched td.dept { padding-left: 2pt; padding-right: 2pt; }" in css
    assert "display: inline;" in css
    assert "margin-right: 0.45em;" in css
    assert "tr:has(.wc-note-print:empty) .multi-dd .dd-summary-text" in css
    assert "white-space: nowrap;" in css


def test_staffing_print_scopes_driving_label_to_transportation_bay_only():
    html = _template()
    screen_css = _style()
    print_css = _print_css()

    assert 'class="bay-screen-label{% if bay.name == \'Transportation\' %} transportation-bay-label{% endif %}"' in html
    assert "{% if bay.name == 'Transportation' %}<div class=\"bay-print-label\">Driving</div>{% endif %}" in html
    assert ".bay-print-label { display: none; }" in screen_css
    assert ".bay-screen-label { display: block !important; }" in print_css
    assert ".transportation-bay-label { display: none !important; }" in print_css
    assert ".bay-print-label { display: block !important; }" in print_css


def test_forklift_live_recalc_hooks_assignment_changes():
    js = _script()

    assert "function recalcForkliftBaySummary()" in js
    assert "function countScheduledForkliftDrivers(model)" in js
    assert "window.FORKLIFT_LIVE_MODEL" in js
    assert "details.sched-dd[data-loc=\"" in js
    assert "recalcForkliftBaySummary();" in js
    # capacity-coverage badge: live gap-based severity + suggested count,
    # no client-side Erlang-C / SLA prediction.
    assert "' suggested'" in js
    assert "Coverage satisfied" in js
    assert "' short'" in js
    # retired SLA JS is gone
    assert "erlangCWaitSeconds" not in js
    assert "Predicted Time-to-Claim" not in js


def test_reset_to_defaults_reconciles_left_rail():
    js = _script()

    assert "function syncLeftRailWithSchedule()" in js
    assert "syncLeftRailWithSchedule();" in js
    assert "const scheduledNames = new Set();" in js
    assert 'details.sched-dd input[name^="loc__"]:checked' in js
    assert "Object.keys(__peopleMeta || {}).forEach(name => {" in js
    assert "scheduledNames.has(name)" in js
    assert "addBackToCorrectList(name);" in js


def test_rotation_warning_supports_structured_coverage_issues():
    html = _template()
    js = _script()
    renderer = js.split("function renderCoverageIssues(warnings, issues) {", 1)[1].split(
        "function selectedAutoCenters()", 1
    )[0]

    assert 'id="rotation-warnings" role="alert"' in html
    assert "{% if not rotation_warnings and not rotation_issues %}hidden{% endif %}" in html
    assert 'class="coverage-why"' in html
    assert "rotation_issues" in html
    assert "renderCoverageIssues" in js
    assert "ROTATION_ISSUES" in js
    assert "list.replaceChildren();" in renderer
    assert "document.createElement('li')" in renderer
    assert "message.textContent = issue.message" in renderer
    assert "reason.textContent = `${rejection.person}: ${rejection.detail}`;" in renderer
    assert "item.textContent = warning;" in renderer
    assert "innerHTML" not in renderer
    assert "const issueMessages = new Set();" in renderer
    assert "if (issueMessages.has(warning)) return;" in renderer
    assert "warnBox.hidden = list.childElementCount === 0;" in renderer


def test_rotation_warning_success_replaces_alert_with_authoritative_response():
    js = _script()
    save_auto = js.split("async function saveAutoCenters(turnOff = []) {", 1)[1].split(
        "// Reconcile every enabled Auto picker's checkboxes", 1
    )[0]
    apply_rebuild = js.split("function applyRebuild(", 1)[1].split(
        "async function rebuild(mode, options = {})", 1
    )[0]

    call = "clearStaleAutoWarnings();"
    assert call in save_auto
    assert """renderCoverageIssues(
        data.warnings,
        [...(data.coverage?.issues || []), ...partialPlacementIssues(data)],
      );""" in apply_rebuild


def test_auto_toggle_failures_preserve_current_issues_and_append_once():
    js = _script()
    assert "function renderCoverageFailure(message) {" in js
    helper = js.split("function renderCoverageFailure(message) {", 1)[1].split(
        "function selectedAutoCenters()", 1
    )[0]
    save_auto = js.split("async function saveAutoCenters(turnOff = []) {", 1)[1].split(
        "// Reconcile every enabled Auto picker's checkboxes", 1
    )[0]
    assert "const warnings = [...(window.ROTATION_WARNINGS || [])];" in helper
    assert "if (!warnings.includes(message)) warnings.push(message);" in helper
    assert "renderCoverageIssues(warnings, window.ROTATION_ISSUES);" in helper
    assert "renderCoverageFailure(" in save_auto


def test_reset_to_defaults_uses_default_only_endpoint_mode():
    js = _script()
    rotation = js.split("// ---------- Rotation goal", 1)[1].split(
        "// Assignments to Do modal", 1
    )[0]
    reset = rotation.split("const resetScheduleBtn", 1)[1].split(
        "modeBtns.forEach", 1
    )[0]
    assert "await rebuild(currentMode(), { resetToDefaults: true })" in reset
    assert "Replace every assignment with saved defaults and next group rotations?" in reset
    assert "This removes manual and automated assignments." in reset
    assert "Previous schedule will be kept" not in reset
    assert "Rebuild enabled Auto work centers" not in reset


def test_reset_to_defaults_clears_the_selected_schedule_goal_after_success():
    js = _script()
    rotation = js.split("// ---------- Rotation goal", 1)[1].split(
        "// Assignments to Do modal", 1
    )[0]
    reset = rotation.split("const resetScheduleBtn", 1)[1].split(
        "modeBtns.forEach", 1
    )[0]
    assert "function clearActiveMode()" in rotation
    assert "b.classList.remove('active');" in rotation
    assert "b.setAttribute('aria-pressed', 'false');" in rotation
    assert "window.RECYCLED_ROTATION_MODE = null;" in rotation
    assert "helpEl.textContent = '';" in rotation
    assert "if (succeeded) {" in reset
    assert "clearActiveMode();" in reset


def test_reset_to_defaults_reconciles_every_picker_from_the_server_map():
    js = _script()
    apply_rebuild = js.split("function applyRebuild(data, { resetToDefaults = false } = {})", 1)[1].split(
        "async function rebuild(mode, options = {})", 1
    )[0]
    rebuild = js.split("async function rebuild(mode, options = {})", 1)[1].split(
        "const resetScheduleBtn", 1
    )[0]

    assert "function applyRebuild(data, { resetToDefaults = false } = {})" in js
    assert "const pickerLocations = resetToDefaults" in apply_rebuild
    assert "? [...document.querySelectorAll('details.sched-dd[data-loc]')].map(dd => dd.dataset.loc)" in apply_rebuild
    assert ": enabled;" in apply_rebuild
    assert "applyRebuild(data, options);" in rebuild


def test_failed_rebuild_keeps_grid_and_renders_person_issues():
    js = _script()
    rebuild = js.split("async function rebuild", 1)[1].split(
        "const resetScheduleBtn", 1
    )[0]
    assert "if (!resp.ok || !data.ok)" in rebuild
    assert "renderPlacementFailure(data)" in rebuild
    failure_branch = rebuild.split("if (!resp.ok || !data.ok)", 1)[1].split(
        "applyRebuild", 1
    )[0]
    assert "applyRebuild" not in failure_branch
    assert "kickAutosave" not in failure_branch


def test_auto_capacity_turn_off_dialog_is_removed():
    html = _template()
    js = _script()
    css = _style()

    assert 'id="auto-capacity-dialog"' not in html
    assert "auto-capacity-" not in html
    for obsolete_js in (
        "showAutoCapacityDialog",
        "closeAutoCapacityDialog",
        "updateCapacityConfirm",
        "capacityDialogState",
        "capacityDialog",
        "capacityForm",
        "capacityReplacements",
        "capacityCancel",
        "capacityConfirm",
        "required_disable_count",
        "resp.status === 409",
    ):
        assert obsolete_js not in js
    assert "#auto-capacity" not in css
    assert ".auto-capacity" not in css
    assert ".dialog-actions" not in css


def test_auto_center_success_requires_server_enabled_centers():
    js = _script()
    signature = "async function saveAutoCenters(turnOff = []) {"
    assert signature in js
    save_auto = js.split(signature, 1)[1].split(
        "// Reconcile every enabled Auto picker's checkboxes", 1
    )[0]

    assert js.count("Array.isArray(data.enabled_work_centers)") == 1
    assert "data.enabled_work_centers || requestedWorkCenters" not in js
    assert "data.enabled_work_centers || workCenters.filter" not in js
    assert save_auto.index("applyEnabledCenters(data.enabled_work_centers);") < save_auto.index(
        "clearStaleAutoWarnings();"
    )


def test_work_center_row_clicks_and_keyboard_use_the_row_state_model():
    js = _script()

    assert "const workCenterRows = [...document.querySelectorAll('tr[data-loc]')];" in js
    assert ".filter(row => row.dataset.on === 'true')" in js
    assert "function toggleWorkCenterRow(row) {" in js
    assert "setWorkCenterOnState(name, !enabled);" in js
    assert "saveAutoCenters(enabled ? [name] : []);" in js
    assert "document.addEventListener('keydown', event => {" in js
    assert "if (!toggle || (event.key !== 'Enter' && event.key !== ' ')) return;" in js


def test_work_center_row_toggle_excludes_controls_and_rolls_back_failures():
    js = _script()

    assert ".sched-cell, .wc-note-cell" in js
    assert "target.closest('a, button, input, select, textarea, label, summary, [contenteditable=\"true\"], .sched-cell, .wc-note-cell, .sub')" in js
    assert "applyEnabledCenters(window.AUTO_SCHEDULE_WC_NAMES || []);" in js


def test_turning_off_a_work_center_sends_it_and_reconciles_returned_assignments():
    js = _script()

    assert "async function saveAutoCenters(turnOff = []) {" in js
    assert "postAutoCenters(requestedWorkCenters, turnOff)" in js
    assert "saveAutoCenters(enabled ? [name] : []);" in js
    assert "function applyAutoCenterAssignments(assignments) {" in js
    assert "applyAutoCenterAssignments(data.assignments);" in js
    save_auto = js.split("async function saveAutoCenters(turnOff = []) {", 1)[1].split(
        "// Ordinary rebuilds", 1
    )[0]
    assert "kickAutosave();" not in save_auto
    helper = js.split("function applyAutoCenterAssignments(assignments) {", 1)[1].split(
        "function renderSaturdayRecruitingDemand", 1
    )[0]
    assert "syncLeftRailWithSchedule();" in helper
    assert "refreshPickerVisibility();" in helper
    assert "changedCb.checked = !changedCb.checked;" not in js
    assert "const autoCbs =" not in js


def test_flush_autosave_waits_for_a_queued_save_before_resolving():
    """A center toggle must not race a manual edit saved behind an in-flight POST."""
    js = _script()
    controller = js.split("  // ---------- Autosave controller ----------", 1)[1].split(
        "  // ---------- Publish submit busy state ----------", 1
    )[0]
    harness = textwrap.dedent(
        f"""
        const controller = {controller!r};
        const listeners = {{}};
        const form = {{
          addEventListener(type, listener) {{ listeners[type] = listener; }},
          getAttribute() {{ return '/staffing'; }},
        }};
        const resolvers = [];
        global.window = {{}};
        global.document = {{
          getElementById(id) {{ return id === 'staffing-form' ? form : null; }},
        }};
        global.FormData = class FormData {{ set() {{}} }};
        global.fetch = () => new Promise(resolve => resolvers.push(resolve));
        const __viewingPosted = false;
        eval(controller);

        listeners.change();
        window.flushAutosave();
        listeners.change();
        const drained = window.flushAutosave();
        resolvers.shift()({{ ok: true, json: async () => ({{}}) }});
        await new Promise(resolve => setImmediate(resolve));

        let settled = false;
        drained.then(() => {{ settled = true; }});
        if (resolvers.length !== 1) throw new Error('queued autosave did not start');
        await new Promise(resolve => setImmediate(resolve));
        if (settled) throw new Error('flush resolved before queued autosave completed');

        resolvers.shift()({{ ok: true, json: async () => ({{}}) }});
        await drained;
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "--eval", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_flush_autosave_rejects_when_a_manual_save_fails():
    """A center toggle must not reconcile over a manual edit that failed to save."""
    js = _script()
    controller = js.split("  // ---------- Autosave controller ----------", 1)[1].split(
        "  // ---------- Publish submit busy state ----------", 1
    )[0]
    harness = textwrap.dedent(
        f"""
        const controller = {controller!r};
        const listeners = {{}};
        const form = {{
          addEventListener(type, listener) {{ listeners[type] = listener; }},
          getAttribute() {{ return '/staffing'; }},
        }};
        let rejectFetch;
        global.window = {{}};
        global.document = {{
          getElementById(id) {{ return id === 'staffing-form' ? form : null; }},
        }};
        global.FormData = class FormData {{ set() {{}} }};
        global.fetch = () => new Promise((resolve, reject) => {{ rejectFetch = reject; }});
        const __viewingPosted = false;
        eval(controller);

        listeners.change();
        const drained = window.flushAutosave();
        rejectFetch(new Error('network offline'));
        try {{
          await drained;
          throw new Error('flush resolved after the autosave failure');
        }} catch (error) {{
          if (error.message === 'flush resolved after the autosave failure') throw error;
          if (error.message !== 'network offline') throw error;
        }}
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "--eval", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_work_center_toggle_stops_before_reconciling_when_autosave_drain_fails():
    js = _script()
    save_auto = js.split("async function saveAutoCenters(turnOff = [])", 1)[1].split(
        "// Ordinary rebuilds", 1
    )[0]
    harness = textwrap.dedent(
        f"""
        const saveAuto = {('async function saveAutoCenters(turnOff = [])' + save_auto)!r};
        const picker = {{ checked: true }};
        const locks = [];
        let savingAutoCenters = false;
        let postCalls = 0;
        let reconciliations = 0;
        const __viewingPosted = false;
        const window = {{
          AUTO_SCHEDULE_WC_NAMES: ['Repair 1'],
          flushAutosave: async () => {{ throw new Error('network offline'); }},
          showToast: () => {{}},
        }};
        function setAutoCentersSaving(saving) {{ savingAutoCenters = saving; locks.push(saving); }}
        function selectedAutoCenters() {{ return ['Repair 1']; }}
        function postAutoCenters() {{ postCalls += 1; }}
        function applyEnabledCenters() {{}}
        function applyAutoCenterAssignments() {{ reconciliations += 1; picker.checked = false; }}
        function renderSaturdayRecruitingDemand() {{}}
        function clearStaleAutoWarnings() {{}}
        function renderMinimumCrewBalance() {{}}
        function renderCoverageFailure() {{}}
        function showToast() {{}}
        const saveAutoCenters = eval('(' + saveAuto + ')');

        await saveAutoCenters(['Work Orders']);
        if (postCalls !== 0) throw new Error('toggle posted after autosave failure');
        if (reconciliations !== 0 || !picker.checked) throw new Error('toggle reconciled over manual picker state');
        if (savingAutoCenters || locks.join(',') !== 'true,false') throw new Error('toggle lock was not released');
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "--eval", harness],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_work_center_toggle_drains_autosave_and_locks_picker_edits_in_flight():
    js = _script()
    save_auto = js.split("async function saveAutoCenters(turnOff = []) {", 1)[1].split(
        "// Ordinary rebuilds", 1
    )[0]
    saving_state = js.split("function setAutoCentersSaving(saving) {", 1)[1].split(
        "async function saveAutoCenters", 1
    )[0]

    assert "await window.flushAutosave();" in save_auto
    assert save_auto.index("await window.flushAutosave();") < save_auto.index(
        "postAutoCenters(requestedWorkCenters, turnOff)"
    )
    assert "__form.inert = saving;" in saving_state


def test_clear_schedule_remains_a_distinct_local_autosave_action():
    html = _template()
    js = _script()
    css = Path("src/zira_dashboard/static/staffing.css").read_text()
    clear_handler = js.split("const __clearBtn = document.getElementById('clear-schedule-btn');", 1)[1].split(
        "// ---------- Undo / Redo helpers ----------", 1
    )[0]

    assert 'id="reset-schedule-btn" class="clear-btn">Reset to defaults</button>' in html
    assert 'id="clear-schedule-btn" class="clear-btn clear-schedule-btn">Clear schedule</button>' in html
    controls = html.split('<div class="rotation-controls"', 1)[1].split('</aside>', 1)[0]
    assert controls.index('id="reset-schedule-btn"') < controls.index('id="clear-schedule-btn"')
    assert controls.index('id="clear-schedule-btn"') < controls.rindex('</div>')
    assert "Clear every Scheduled cell for this day?" in js
    assert "const resetScheduleBtn = document.getElementById('reset-schedule-btn');" in js
    assert "const __clearBtn = document.getElementById('clear-schedule-btn');" in js
    assert "cb.checked = false;" in js
    assert "item.classList.remove('selected');" in js
    assert "syncLeftRailWithSchedule();" in js
    assert "refreshPickerVisibility();" in js
    assert "kickAutosave();" in js
    assert ".clear-schedule-btn:hover" in css
    assert "if (__viewingPosted) return;" in clear_handler
    assert clear_handler.index("if (__viewingPosted) return;") < clear_handler.index(
        "if (!confirm('Clear every Scheduled cell for this day?"
    )
