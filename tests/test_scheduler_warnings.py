from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'src/zira_dashboard/static/staffing-warnings.js'


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content('''<ul id="warnings" data-persistent-warning="Sync pending"></ul>
          <section class="unscheduled"><ul><li data-name="Lee">Lee</li></ul></section>
          <table><tr data-loc="Repair 1" data-on="true"><td>
          <details class="sched-dd multi-dd"><summary>Choose people</summary><input type="checkbox"></details>
          </td></tr><tr data-loc="Repair 2" data-on="false"><td>Off</td></tr></table>''')
        page.evaluate('''document.addEventListener('click', event => {
            if (event.target.closest('details.multi-dd')) return;
            document.querySelectorAll('details.multi-dd[open]').forEach(el => el.open = false);
        })''')
        if SCRIPT.exists():
            page.add_script_tag(path=str(SCRIPT))
        yield page
        browser.close()


def render(page, issues, warnings=()):
    assert page.evaluate('Boolean(window.SchedulerWarnings)'), 'Actionable warnings are not implemented'
    page.evaluate('(data)=>SchedulerWarnings.render(document.getElementById("warnings"),data.warnings,data.issues)',
                  {'warnings': list(warnings), 'issues': issues})


def test_groups_duplicates_keeps_distinct_reasons_and_opens_exact_picker(page):
    base = {'code': 'short', 'message': 'Needs a person', 'centers': ['Repair 1']}
    render(page, [dict(base, rejections=[{'person': 'Lee', 'detail': 'Away'}]),
                  dict(base, rejections=[{'person': 'Sam', 'detail': 'Untrained'}]),
                  {'code': 'unsafe', 'message': 'Needs a trainer', 'centers': ['Repair 1']}])
    assert page.locator('#warnings').inner_text().count('Needs a person') == 1
    assert page.locator('#warnings').inner_text().count('Sync pending') == 1
    page.get_by_role('button', name='Review Repair 1', exact=True).click()
    assert page.locator('details.sched-dd').get_attribute('open') is not None
    assert page.locator('details.sched-dd > summary').evaluate('(el)=>el===document.activeElement')
    assert 'Lee: Away' in page.locator('#warnings').text_content()
    assert 'Sam: Untrained' in page.locator('#warnings').text_content()


def test_off_and_posted_targets_never_enable_or_open_edit_controls(page):
    render(page, [{'message': 'Check both', 'centers': ['Repair 1', 'Repair 2', 'Unknown']}])
    assert page.get_by_role('button', name='Review Unknown').count() == 0
    page.get_by_role('button', name='Review Repair 2').click()
    assert page.locator('tr[data-loc="Repair 2"]').get_attribute('data-on') == 'false'
    page.evaluate('window.SCHEDULE_VIEWING_POSTED=true')
    page.get_by_role('button', name='Review Repair 1').click()
    assert page.locator('details.sched-dd').get_attribute('open') is None


def test_person_warning_and_untrusted_text_stay_visible_without_html_execution(page):
    render(page, [{'code': 'person_unplaced', 'person': 'Lee', 'message': 'Lee is unassigned'},
                  {'message': '<img src=x onerror="window.hacked=true">'}], ['General warning', 'General warning'])
    assert page.locator('#warnings img').count() == 0
    assert page.locator('#warnings').inner_text().count('General warning') == 1
    page.get_by_role('button', name='Find Lee', exact=True).click()
    assert page.locator('li[data-name="Lee"]').evaluate('(el)=>el===document.activeElement')
