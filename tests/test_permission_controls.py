from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path('src/zira_dashboard/templates')


def env(role):
    result = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    result.globals.update(static_v=lambda _: 'test', goat_holders=lambda: {}, month_name=lambda _: 'September',
        nav_inbox_summary=lambda: {'total': 0, 'urgent_total': 0, 'source_errors': []},
        can_admin=lambda: role == 'admin', can_hr=lambda: role in ('admin', 'hr'),
        can_operate=lambda: role in ('admin', 'hr', 'manager'))
    return result


@pytest.mark.parametrize('role', ['admin', 'hr', 'manager', 'visitor'])
def test_leaderboard_controls(role):
    module = env(role).get_template('leaderboards.html').make_module({})
    section = SimpleNamespace(loc_name='Repair', rows=[], is_manually_inactive=True)
    html = str(module.lb_days_section(section, 'wc'))
    assert ('class="lb-hide-btn"' in html) == (role == 'admin')
    assert ('class="lb-drag-handle"' in html) == (role != 'visitor')
    assert ('draggable="true"' in html) == (role != 'visitor')
    inactive = str(module.lb_days_section(section, 'wc', True))
    assert ('class="lb-show-btn"' in inactive) == (role == 'admin')


@pytest.mark.parametrize('role', ['admin', 'hr', 'manager', 'visitor'])
def test_trophy_override_hidden_but_filters_remain(role):
    html = env(role).get_template('trophy_case.html').render(
        goats=[{'group': 'Repair', 'name': None}], year=2026, month=9,
        years=[2026], months=[], annual_groups=[], monthly_groups=[], today="2026-09-21")
    assert ('<button class="tc-edit"' in html) == (role == 'admin')
    assert ('id="tc-modal-bd"' in html) == (role == 'admin')
    assert ('fetch(\'/api/awards/override\'' in html) == (role == 'admin')
    assert "getElementById('year-picker')" in html


@pytest.mark.parametrize('role', ['admin', 'hr', 'manager'])
def test_skill_automation_admin_only(role):
    html = env(role).get_template('skills.html').render(
        active='skills', active_count=0, inactive_count=0,
        skills=[{'name':'Repair','odoo_id':1,'skill_type':'Production Skills'}],
        type_by_skill={'Repair':'Production Skills'}, hidden_skills=[], person_certs={},
        people=[], views=[], automation_groups={'Repair': {}}, odoo_url='')
    assert ('class="automation-settings-trigger"' in html) == (role == 'admin')
    assert ('id="automation-save-btn"' in html) == (role == 'admin')


def test_grid_read_only_returns_before_persistence_but_keeps_picker():
    script = Path('src/zira_dashboard/static/dashboard-grid.js').read_text()
    stop = script.index('if (window.gpiAccess && !window.gpiAccess.admin) return;')
    assert script.index("getElementById('wc-picker')") < stop < script.index("grid.on('change'")
    assert script.index('fitGridToViewport();') < stop


@pytest.mark.parametrize('role', ['admin', 'hr', 'manager', 'visitor'])
@pytest.mark.parametrize('orientation', ['horizontal', 'vertical'])
def test_department_assignments_require_operational_access(role, orientation):
    module = env(role).get_template('_department_dashboard_widgets.html').make_module({
        'customs': {'bars': {'orientation': orientation}}, 'today': '2026-09-21',
        'assignments_todo_by_wc': {'Repair 1': {'first_iso': 'start', 'last_iso': 'end'}},
    })
    html = str(module.department_bar_chart('bars', [{'name': 'Repair 1', 'units': 0,
        'expected': 0, 'target_pct': None, 'color': None, 'pct': 0}]))
    assert ('class="no-assign-btn"' in html) == (role != 'visitor')
    assert 'Repair 1' in html


@pytest.mark.parametrize('role', ['admin', 'hr', 'manager', 'visitor'])
@pytest.mark.parametrize('name', ['recycling.html', 'new_dept.html'])
def test_department_toolbar_keeps_filters_but_limits_layout(role, name):
    template = env(role).get_template(name)
    context = template.new_context({'tv_mode': False, 'windows': [('today', 'Today')],
        'window': 'today', 'ribbon_winners': [], 'goat_watch': []})
    html = ''.join(template.blocks['subnav'](context))
    assert ('id="reset-layout"' in html) == (role == 'admin')
    assert 'name="start"' in html
    assert '>Today</a>' in html


@pytest.mark.parametrize('tv_mode', [False, True])
def test_read_only_grid_runs_without_wiring_writes(tv_mode):
    import json
    import shutil
    import subprocess

    node = shutil.which('node')
    if not node:
        pytest.skip('Node is unavailable')
    script = Path('src/zira_dashboard/static/dashboard-grid.js').read_text()
    bootstrap = '''
const assert = require('assert');
let options, fits = 0, pickerWired = false;
const tv = TV_MODE;
const grid = {save: () => [], cellHeight: () => fits++, on: () => {throw Error('write handler');}};
global.GridStack = {init: value => {options = value; return grid;}};
global.window = {gpiAccess: {admin: false}, innerHeight: 1000, addEventListener: () => {}};
global.requestAnimationFrame = fn => fn();
global.fetch = () => {throw Error('unexpected write');};
global.document = {
  documentElement: {hasAttribute: () => false},
  querySelector: selector => selector === '.grid-stack'
    ? {dataset: {tvMode: tv ? '1' : '0', layoutPage: 'new'}} : null,
  getElementById: id => {
    assert.equal(id, 'wc-picker');
    return {addEventListener: () => {pickerWired = true;}};
  }
};
'''.replace('TV_MODE', json.dumps(tv_mode))
    assertions = '''
assert.equal(options.staticGrid, true);
assert.equal(pickerWired, !tv);
assert.equal(fits > 0, tv);
'''
    subprocess.run([node, '-e', bootstrap + script + assertions], check=True, capture_output=True)


@pytest.mark.parametrize('role', ['admin', 'hr', 'manager', 'visitor'])
def test_work_center_picker_remains_without_layout_controls(role):
    template = env(role).get_template('wc_dashboard.html')
    context = template.new_context({'tv_mode': False, 'wc_options': [{'slug':'repair-1','name':'Repair 1'}],
        'slug':'repair-1', 'operators_display':'Maria'})
    html = ''.join(template.blocks['subnav'](context))
    assert ('class="operator-strip-right"' in html) == (role == 'admin')
    assert ('id="reset-layout"' in html) == (role == 'admin')
    assert 'id="wc-picker"' in html
    assert 'Repair 1' in html
    assert 'Maria' in html


@pytest.mark.parametrize('role', ['admin', 'hr', 'manager', 'visitor'])
def test_employee_links_on_trophies_and_forklift(role):
    trophy = env(role).get_template('trophy_case.html').render(
        goats=[{'group': 'Repair', 'winner': {'name':'Maria','units':100,'pph':10}}],
        today='2026-09-21', year=2026, month=9)
    row = {'name':'Maria', 'display_name':'Maria', 'score':100, 'days':3,
           'calls':10, 'on_time':9, 'late':1, 'ontime_pct':90, 'avg_ms':1000}
    forklift = env(role).get_template('forklift_leaderboards.html').render(
        lb={key:[row] for key in ('overall','most_calls','on_time','fastest')}, min_calls=1)
    for html in (trophy, forklift):
        assert 'Maria' in html
        assert ('href="/staffing/people/Maria"' in html) == (role != 'visitor')
    assert 'class="tc-name-link"' in trophy


@pytest.mark.parametrize('role', ['admin', 'hr', 'manager', 'visitor'])
@pytest.mark.parametrize('tv_mode', [False, True])
def test_work_center_award_links_preserve_tv_and_limit_visitors(role, tv_mode):
    from datetime import date

    html = env(role).get_template('wc_dashboard.html').render(
        tv_mode=tv_mode, layout={}, customs={}, wc_name='Repair 1', slug='repair-1',
        kpi={'pallets_per_hour':0, 'up_time_pct':0}, downtime_row={'down':0, 'working':0, 'working_pct':0}, pallets={'target_today':0, 'units_today':0, 'target_full_day':0},
        downtime_elapsed_minutes=0, month=9, year=2026,
        ribbons={'group':'Repair','entries':[{'name':'Maria','position':1,'units':100,'day':date(2026,9,21)}]})
    assert 'Maria' in html
    assert ('href="/staffing/people/Maria"' in html) == (tv_mode or role != 'visitor')
    if tv_mode:
        assert 'id="reset-layout"' not in html
        assert 'id="wc-picker"' not in html
