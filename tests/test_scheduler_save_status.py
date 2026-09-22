"""Exercise the real autosave controller with deferred network responses."""
import json
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path("src/zira_dashboard/static/staffing.js")


def run_controller(scenario, config=None):
    controller = SCRIPT.read_text().split("  // ---------- Autosave controller ----------", 1)[1].split(
        "  // ---------- Publish submit busy state ----------", 1
    )[0]
    harness = r"""
      import assert from 'node:assert/strict';
      const config = CONFIG;
      const listeners = {};
      const documentListeners = {};
      const makeNode = () => ({ textContent: '', hidden: false, disabled: false, dataset: {},
        classList: {add(){}, remove(){}}, addEventListener(type, fn){ this[type] = fn; } });
      const nodes = Object.fromEntries(['autosave-indicator', 'scheduler-save-status-text',
        'scheduler-save-status-detail', 'scheduler-save-retry'].map(id => [id, makeNode()]));
      const form = {addEventListener(type, fn){ listeners[type] = fn; }, getAttribute(){ return '/staffing'; }};
      const requests = [];
      let reloads = 0;
      const navigations = [];
      let timer = null;
      globalThis.setTimeout = fn => { timer = fn; return 1; };
      globalThis.clearTimeout = () => { timer = null; };
      globalThis.window = {...config, location: {href:'https://plant.test/staffing?day=2026-09-22',origin:'https://plant.test',pathname:'/staffing',search:'?day=2026-09-22',reload(){reloads++;},assign(url){navigations.push(url);}}};
      globalThis.document = {addEventListener(type,fn){documentListeners[type]=fn;},getElementById(id){ return id === 'staffing-form' ? form : nodes[id]; }};
      globalThis.FormData = class {set(){}};
      globalThis.fetch = () => new Promise((resolve, reject) => requests.push({resolve, reject}));
      const __viewingPosted = !!config.SCHEDULE_VIEWING_POSTED;
      const text = () => nodes['scheduler-save-status-text'].textContent;
      const detail = () => nodes['scheduler-save-status-detail'].textContent;
      const state = () => nodes['autosave-indicator'].dataset.state;
      const retry = nodes['scheduler-save-retry'];
      const tick = () => new Promise(resolve => setImmediate(resolve));
      const respond = (data = {}) => requests.shift().resolve({ok: true, json: async () => data});
      eval(CONTROLLER);
      SCENARIO
    """.replace("CONFIG", json.dumps(config or {})).replace("CONTROLLER", json.dumps(controller)).replace("SCENARIO", scenario)
    result = subprocess.run(["node", "--input-type=module", "--eval", harness], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_pending_saving_and_saved_draft_are_persistent():
    run_controller("""
      assert.equal(text(), 'Saved draft');
      assert.equal(detail(), 'Not posted yet.');
      listeners.input();
      assert.equal(text(), 'Changes not saved');
      assert.equal(window.schedulerAutosaveBusy, true);
      const drain = window.flushAutosave();
      assert.equal(text(), 'Saving…');
      respond({ok: true, revision: 'new'});
      await drain;
      assert.equal(text(), 'Saved draft');
      assert.equal(window.SCHEDULE_REVISION, 'new');
      assert.equal(window.schedulerAutosaveBusy, false);
    """)


@pytest.mark.parametrize("config, expected", [
    ({"SCHEDULE_HAS_SNAPSHOT": True}, 'The posted version has not changed.'),
    ({"SCHEDULE_PUBLISHED": True}, 'Changes will be saved as a draft.'),
])
def test_initial_publication_detail(config, expected):
    run_controller(f"assert.equal(detail(), {json.dumps(expected)});", config)


@pytest.mark.parametrize("config", [
    {"SCHEDULE_VIEWING_POSTED": True},
    {"SCHEDULE_PUBLISHED": True, "SCHEDULE_VIEW_MODE": "posted"},
])
def test_posted_views_never_autosave(config):
    run_controller("""
      assert.equal(text(), 'Posted schedule');
      listeners.change();
      await window.flushAutosave({force: true, retry: true});
      assert.equal(requests.length, 0);
      assert.equal(timer, null);
      assert.equal(state(), 'clean');
    """, config)


def test_presentation_and_training_controls_do_not_save():
    run_controller("""
      for (const selector of ['[data-scheduler-presentation]', '#training-sidebar']) {
        listeners.change({target: {closest: value => value === selector}});
        assert.equal(timer, null);
        assert.equal(state(), 'clean');
      }
    """)


def test_json_failure_rejects_flush_and_retry_keeps_error_until_success():
    run_controller("""
      listeners.change();
      const drain = window.flushAutosave();
      respond({ok: false, error: 'Schedule conflict', revision: 'bad'});
      await assert.rejects(drain, /Schedule conflict/);
      assert.equal(state(), 'failed');
      assert.equal(text(), 'Save failed');
      assert.equal(retry.hidden, false);
      assert.equal(window.SCHEDULE_REVISION, undefined);
      const retrying = retry.click();
      await tick();
      assert.equal(state(), 'saving');
      assert.equal(retry.disabled, true);
      assert.match(detail(), /Retry saving/);
      respond({ok: true});
      await retrying;
      assert.equal(text(), 'Saved draft');
      assert.equal(retry.hidden, true);
    """)


def test_repeated_edits_wait_for_queued_save_and_propagate_its_failure():
    run_controller("""
      listeners.change();
      const drain = window.flushAutosave();
      const rejected = assert.rejects(drain, /offline/);
      listeners.change();
      listeners.input();
      respond({revision: 'first'});
      await tick();
      assert.equal(requests.length, 1);
      let settled = false;
      rejected.then(() => settled = true);
      await tick();
      assert.equal(settled, false);
      requests.shift().reject(new Error('offline'));
      await rejected;
      assert.equal(state(), 'failed');
      const retrying = retry.click();
      await tick();
      respond({revision: 'last'});
      await retrying;
      assert.equal(requests.length, 0);
      assert.equal(window.SCHEDULE_REVISION, 'last');
      assert.equal(text(), 'Saved draft');
    """)


def test_failed_retry_remains_visible_and_a_later_edit_can_recover():
    run_controller("""
      listeners.change();
      const drain = window.flushAutosave();
      requests.shift().reject(new Error('offline'));
      await assert.rejects(drain, /offline/);
      const retrying = retry.click();
      await tick();
      respond({ok: false, error: 'still offline'});
      await retrying;
      assert.equal(retry.disabled, false);
      assert.equal(state(), 'failed');
      listeners.input();
      assert.equal(state(), 'failed');
      const next = window.flushAutosave();
      respond({ok: true});
      await next;
      assert.equal(state(), 'clean');
    """)


def test_queued_draft_saves_finish_before_posted_page_reload():
    run_controller("""
      listeners.change();
      const drain = window.flushAutosave();
      listeners.input();
      respond({published: false});
      await tick();
      assert.equal(reloads, 0);
      respond({published: false});
      await drain;
      assert.equal(reloads, 1);
    """, {"SCHEDULE_PUBLISHED": True, "SCHEDULE_VIEW_MODE": "draft"})


def test_status_partial_is_accessible_and_retry_cannot_submit():
    partial = Path('src/zira_dashboard/templates/_staffing_save_status.html').read_text()
    assert 'role="status" aria-live="polite" aria-atomic="true"' in partial
    assert 'id="scheduler-save-retry" type="button" hidden' in partial
    assert 'data-scheduler-presentation' in partial


def test_publish_waits_for_every_queued_save_before_submitting():
    run_controller("""
      let submitted = 0;
      let prevented = 0;
      let stopped = 0;
      const submitter = {name: 'action', value: 'publish'};
      form.requestSubmit = button => { assert.equal(button, submitter); submitted++; };
      const event = {submitter, preventDefault(){prevented++;}, stopImmediatePropagation(){stopped++;}};
      listeners.input();
      const publishing = listeners.submit(event);
      assert.equal(prevented, 1);
      assert.equal(stopped, 1);
      assert.equal(submitted, 0);
      listeners.change();
      await listeners.submit(event);
      respond({ok: true});
      await tick();
      assert.equal(submitted, 0);
      assert.equal(requests.length, 1);
      respond({ok: true});
      await publishing;
      assert.equal(submitted, 1);
      await listeners.submit(event);
      assert.equal(prevented, 2);
    """)


def test_publish_stops_on_save_failure_until_retry_succeeds():
    run_controller("""
      let submitted = 0;
      form.requestSubmit = () => submitted++;
      const event = {submitter: {name: 'action', value: 'publish'}, preventDefault(){}, stopImmediatePropagation(){}};
      listeners.input();
      const publishing = listeners.submit(event);
      respond({ok: false, error: 'Not saved'});
      await publishing;
      assert.equal(submitted, 0);
      assert.equal(state(), 'failed');
      await listeners.submit(event);
      assert.equal(submitted, 0);
      const retrying = retry.click();
      await tick();
      respond({ok: true});
      await retrying;
      assert.equal(state(), 'clean');
      let prevented = false;
      await listeners.submit({...event, preventDefault(){prevented=true;}});
      assert.equal(prevented, false);
    """)


def test_navigation_drains_queued_save_and_blocks_on_failure():
    run_controller("""
      listeners.change();
      const navigation = window.navigateSchedulerAfterSave('/staffing?day=2026-09-23');
      listeners.input(); respond({ok:true}); await tick();
      assert.equal(navigations.length,0);
      respond({ok:false,error:'offline'});
      assert.equal(await navigation,false);
      assert.equal(navigations.length,0);
      assert.equal(state(),'failed');
      const retrying=retry.click(); await tick(); respond({ok:true}); await retrying;
      await window.navigateSchedulerAfterSave('/staffing?day=2026-09-23');
      assert.deepEqual(navigations,['/staffing?day=2026-09-23']);
    """)


def test_navigation_suppresses_old_day_reload_after_published_save():
    run_controller("""
      listeners.input();
      const navigation=window.navigateSchedulerAfterSave('/staffing?day=2026-09-23');
      respond({published:false}); await navigation;
      assert.equal(reloads,0);
      assert.deepEqual(navigations,['/staffing?day=2026-09-23']);
    """, {"SCHEDULE_PUBLISHED": True})


def test_links_drain_saves_but_respect_modifiers_and_special_targets():
    run_controller("""
      const click=(href,props={},attrs={})=>{
        let prevented=false;
        const link={getAttribute(name){return name==='href'?href:attrs[name];},hasAttribute(name){return name in attrs;}};
        documentListeners.click({button:0,target:{closest(){return link;}},preventDefault(){prevented=true;},stopImmediatePropagation(){},...props});
        return prevented;
      };
      listeners.input();
      for(const props of [{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1}])
        assert.equal(click('/staffing?day=2026-09-23',props),false);
      assert.equal(click('/file',{}, {download:''}),false);
      assert.equal(click('/staffing',{}, {target:'_blank'}),false);
      assert.equal(click('#notes'),false);
      assert.equal(click('https://plant.test/staffing?day=2026-09-22#notes'),false);
      assert.equal(click('https://other.test/'),false);
      assert.equal(requests.length,0);
      assert.equal(click('/staffing?day=2026-09-22&view=posted'),true);
      assert.equal(requests.length,1); assert.equal(navigations.length,0);
      respond({ok:true}); await tick();
      assert.deepEqual(navigations,['https://plant.test/staffing?day=2026-09-22&view=posted']);
    """)


def test_day_picker_keeps_current_day_when_navigation_is_blocked():
    source = SCRIPT.read_text().split('  // ---------- Posted schedule lock ----------', 1)[0]
    harness = """
      import assert from 'node:assert/strict';
      const handlers={};
      const picker={value:'2026-09-22',disabled:false,addEventListener(type,fn){handlers[type]=fn;}};
      globalThis.document={getElementById(){return picker;}};
      let finish;
      globalThis.window={navigateSchedulerAfterSave(url){assert.equal(url,'/staffing?day=2026-09-23');return new Promise(resolve=>finish=resolve);}};
      eval(SOURCE);
      picker.value='2026-09-23';
      const change=handlers.change({target:picker});
      assert.equal(picker.value,'2026-09-22'); assert.equal(picker.disabled,true);
      finish(false); await change;
      assert.equal(picker.value,'2026-09-22'); assert.equal(picker.disabled,false);
    """.replace('SOURCE', json.dumps(source))
    result = subprocess.run(['node', '--input-type=module', '--eval', harness], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("config, locked", [
    ({"SCHEDULE_VIEWING_POSTED": True}, True),
    ({"SCHEDULE_PUBLISHED": True, "SCHEDULE_VIEW_MODE": "posted"}, True),
    ({"SCHEDULE_PUBLISHED": True, "SCHEDULE_VIEW_MODE": "draft"}, False),
    ({"SCHEDULE_PUBLISHED": True}, False),
    ({"SCHEDULE_PUBLISHED": False, "SCHEDULE_VIEW_MODE": "draft"}, False),
])
def test_shared_posted_flag_locks_explicit_posted_but_preserves_editable_draft(config, locked):
    source = SCRIPT.read_text().split('  // ---------- Posted schedule lock ----------', 1)[1].split(
        '  // Wake the autosave controller', 1
    )[0]
    harness = """
      import assert from 'node:assert/strict';
      globalThis.window = CONFIG;
      const classes = new Set();
      globalThis.document = {getElementById(){return {classList:{add(name){classes.add(name);}}};}};
      eval(SOURCE);
      assert.equal(classes.has('locked'), LOCKED);
      assert.equal(classes.has('viewing-posted'), LOCKED);
    """.replace('CONFIG', json.dumps(config)).replace('SOURCE', json.dumps(source)).replace('LOCKED', json.dumps(locked))
    result = subprocess.run(['node', '--input-type=module', '--eval', harness], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("viewing_posted,published,view_mode,readonly", [
    (True, False, 'posted', True),
    (False, True, 'posted', True),
    (False, True, 'draft', False),
    (False, False, 'draft', False),
])
def test_daily_and_center_notes_are_readonly_in_both_posted_states(viewing_posted, published, view_mode, readonly):
    from jinja2 import Environment
    from types import SimpleNamespace

    source = Path('src/zira_dashboard/templates/staffing.html').read_text()
    fields = [line.strip() for line in source.splitlines()
              if '<textarea' in line and ('name="notes"' in line or 'name="wc_note__' in line)]
    assert len(fields) == 2
    for field in fields:
        rendered = Environment().from_string(field).render(
            viewing_posted=viewing_posted, published=published, view_mode=view_mode,
            notes='', row=SimpleNamespace(loc=SimpleNamespace(name='Repair'), wc_note=''),
        )
        assert (' readonly' in rendered) is readonly
