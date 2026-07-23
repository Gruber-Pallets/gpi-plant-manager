from pathlib import Path


def test_scheduler_uses_recruit_action_not_separate_panel():
    template = Path("src/zira_dashboard/templates/staffing.html").read_text()

    assert 'data-saturday-action="activate-from-schedule"' in template
    assert 'class="publish-btn saturday-recruit-button"' in template
    assert 'Recruit <span data-saturday-recruit-demand>{{ saturday_recruit_enabled_count }}</span>' in template
    assert '{% if not saturday_recruit_enabled_count %}hidden disabled{% endif %}' in template
    assert "_saturday_recruiting_panel.html" not in template


def test_saturday_hides_publish_until_staffing_is_prepared():
    template = Path("src/zira_dashboard/templates/staffing.html").read_text()

    assert "{% if not day_is_saturday or saturday_staffing_prepared %}" in template


def test_staffing_template_has_live_saturday_recruiting_demand_target():
    template = Path("src/zira_dashboard/templates/staffing.html").read_text()

    assert 'data-saturday-recruit-demand' in template


def test_work_center_save_renders_server_recruiting_demand():
    js = Path("src/zira_dashboard/static/staffing.js").read_text()

    assert "function renderSaturdayRecruitingDemand(bundle, enabledCenters)" in js
    assert "renderSaturdayRecruitingDemand(data.saturday_recruiting, data.enabled_work_centers);" in js
    assert "button.hidden = count === 0;" in js
    assert "button.disabled = count === 0;" in js
    assert "const requested = Number(coverage.requested || 0);" in js
    assert "const filled = Number(coverage.total || 0);" in js


def test_response_counts_are_focusable_and_list_names():
    template = Path("src/zira_dashboard/templates/staffing.html").read_text()

    assert 'class="saturday-response-summary"' in template
    assert 'tabindex="0"' in template
    assert "saturday_response_summary[key]|join" in template


def test_scheduler_recruit_script_posts_directly_without_confirmation_dialog():
    js = Path("src/zira_dashboard/static/saturday-recruiting.js").read_text()

    assert 'data-saturday-action="activate-from-schedule"' in js
    assert "/api/staffing/saturday-recruiting/activate-from-schedule" in js
    assert "window.confirm" not in js


def test_scheduler_recruit_waits_for_autosave_before_activation():
    js = Path("src/zira_dashboard/static/saturday-recruiting.js").read_text()

    flush_call = "await window.flushAutosave();"
    activation_call = (
        "const response = await fetch("
        "'/api/staffing/saturday-recruiting/activate-from-schedule'"
    )
    assert flush_call in js
    assert activation_call in js
    assert js.index(flush_call) < js.index(activation_call)
    assert "Could not save the schedule. Recruiting was not started." in js


def test_scheduler_recruit_style_has_blue_button_and_accessible_summary_focus():
    css = Path("src/zira_dashboard/static/saturday-recruiting.css").read_text()

    assert ".saturday-recruit-button" in css
    assert "background: #2563eb" in css
    assert ".title-bar .publish-btn.saturday-recruit-button" in css
    assert ".saturday-response-count:focus" in css
