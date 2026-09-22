"""Presentation choice must never replace or mutate schedule form controls."""
from pathlib import Path
import subprocess
import json


def test_layout_preference_preserves_inputs_and_survives_unavailable_storage():
    script = Path('src/zira_dashboard/static/staffing-layout.js').read_text()
    harness = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = SOURCE;
for (const stored of [null, 'table', 'cards', 'invalid', 'throws']) {
  const originalInput = {name: 'loc__A', checked: true, value: 'Alex'};
  const wrapper = {dataset: {schedulerView: 'cards'}, input: originalInput};
  const buttons = ['cards', 'table'].map(view => ({
    dataset: {schedulerLayout: view}, attributes: {},
    setAttribute(key, value) { this.attributes[key] = value; },
    addEventListener(event, handler) { assert.equal(event, 'click'); this.click = handler; },
  }));
  const writes = [];
  const context = {
    document: {
      querySelector(selector) { assert.equal(selector, '[data-scheduler-view]'); return wrapper; },
      querySelectorAll(selector) { assert.equal(selector, '[data-scheduler-layout]'); return buttons; },
    },
    window: {localStorage: {
      getItem() { if (stored === 'throws') throw Error('blocked'); return stored; },
      setItem(key, value) { if (stored === 'throws') throw Error('blocked'); writes.push([key, value]); },
    }},
  };
  vm.runInNewContext(source, context);
  assert.equal(wrapper.dataset.schedulerView, stored === 'table' ? 'table' : 'cards');
  originalInput.checked = false;
  for (const button of [buttons[1], buttons[0], buttons[1]]) {
    button.click();
    assert.equal(wrapper.dataset.schedulerView, button.dataset.schedulerLayout);
    assert.equal(button.attributes['aria-pressed'], 'true');
    assert.equal(buttons.find(other => other !== button).attributes['aria-pressed'], 'false');
    assert.equal(wrapper.input, originalInput);
    assert.equal(originalInput.checked, false);
  }
  if (stored !== 'throws') assert.deepEqual(writes.at(-1), ['scheduler-phone-layout', 'table']);
}
vm.runInNewContext(source, {document: {querySelector() { return null; }}});
'''
    result = subprocess.run(['node', '-e', harness.replace('SOURCE', json.dumps(script))], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_card_layout_keeps_original_bay_details_and_screen_only_overrides():
    template = Path('src/zira_dashboard/templates/staffing.html').read_text()
    css = Path('src/zira_dashboard/static/staffing-layout.css').read_text()
    assert template.count('name="loc__{{ row.loc.name }}"') == 2  # Regular and reserve pools.
    assert template.count('name="wc_note__{{ row.loc.name }}"') == 1
    assert 'forklift-bay-summary' in template
    assert '{{ bay.subtitle }}' in template
    assert '<div class="scheduler-card-bay">{{ bay.name }}</div>' in template
    for view in ('cards', 'table'):
        assert f'type="button" data-scheduler-layout="{view}" data-scheduler-presentation' in template
    assert '@media screen and (max-width: 768px)' in css
    assert 'position: static; min-width: 0; width: 100%; max-height: 45vh' in css


def test_phone_cards_and_print_keep_table_in_full_width_grid():
    from playwright.sync_api import sync_playwright

    root = Path(__file__).resolve().parents[1] / 'src/zira_dashboard/static'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': 375, 'height': 900})
        page.set_content('''<nav class="sub-nav"><a>Plant Scheduler</a><a>Employee</a>
          <a>Hours</a><a>Time Off</a><a>Skills Matrix</a></nav>
          <div class="layout"><aside class="panel side">People</aside><main class="panel">
          <div class="scheduler-layout-controls"><button>Cards</button><button>Table</button></div>
          <div class="scheduler-table-wrap" data-scheduler-view="cards"><table class="sched">
          <thead><tr><th>Center</th><th>People</th></tr></thead><tbody>
          <tr data-loc="Repair 1"><td>Repair 1</td><td>Alex</td></tr></tbody></table></div>
          </main><aside class="day-context">Notes</aside></div>''')
        page.add_style_tag(path=str(root / 'staffing.css'))
        page.add_style_tag(path=str(root / 'staffing-layout.css'))
        assert page.locator('tr[data-loc]').evaluate('el=>getComputedStyle(el).display') == 'grid'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.emulate_media(media='print')
        page.add_style_tag(path=str(root / 'staffing-print.css'))
        assert page.locator('.scheduler-table-wrap').evaluate('el=>getComputedStyle(el).display') == 'contents'
        assert page.locator('table.sched').evaluate('el=>getComputedStyle(el).display') == 'table'
        table = page.locator('table.sched').bounding_box()
        grid = page.locator('.layout').bounding_box()
        assert abs(table['width'] - grid['width']) < 1
        browser.close()
