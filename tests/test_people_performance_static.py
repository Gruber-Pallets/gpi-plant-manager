from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[1]
CSS_PATH = ROOT / "src/zira_dashboard/static/people-performance.css"
SCRIPT_PATH = ROOT / "src/zira_dashboard/static/people-performance.js"
TEMPLATE_PATH = ROOT / "src/zira_dashboard/templates/people_performance.html"
ROWS_TEMPLATE_PATH = ROOT / "src/zira_dashboard/templates/_people_performance_rows.html"


def test_people_assets_exist_and_are_loaded_by_the_full_page():
    assert CSS_PATH.exists()
    assert SCRIPT_PATH.exists()
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert 'static_v("people-performance.css")' in template
    assert 'static_v("people-performance.js")' in template


def test_color_shape_readability_and_tablet_contracts_are_present():
    css = CSS_PATH.read_text(encoding="utf-8")

    assert "--pp-ahead: #15803d" in css
    assert "--pp-behind: #c2413b" in css
    assert ".state-ahead" in css and ".state-behind" in css
    assert ".pp-result-text" in css
    assert re.search(r"\.pp-break\s*\{[^}]*repeating-linear-gradient", css)
    assert ".pp-interval-trigger:focus-visible" in css
    assert "@media (max-width: 1100px)" in css
    assert "@media (max-width: 760px)" in css

    font_sizes = [float(value) for value in re.findall(r"font-size:\s*(0?\.\d+)rem", css)]
    assert font_sizes
    assert min(font_sizes) >= 0.75

    focus = re.search(
        r"\.pp-interval-trigger:focus-visible,\s*"
        r"\.pp-interval-shortcut:focus-visible\s*\{([^}]*)\}",
        css,
    )
    assert focus
    assert "outline: none" in focus.group(1)
    focus_ring = re.search(
        r"\.pp-interval-trigger:focus-visible::after,\s*"
        r"\.pp-interval-shortcut:focus-visible::after\s*\{([^}]*)\}",
        css,
    )
    assert focus_ring
    assert "box-shadow" in focus_ring.group(1)
    assert "#fff" in focus_ring.group(1)
    assert "#111827" in focus_ring.group(1)


def test_sticky_axis_is_outside_horizontal_overflow_and_scroll_is_local():
    css = CSS_PATH.read_text(encoding="utf-8")
    axis = re.search(r"\.pp-axis\s*\{([^}]*)\}", css)
    horizontal = re.search(r"\.pp-horizontal-scroll\s*\{([^}]*)\}", css)

    assert axis
    assert "position: sticky" in axis.group(1)
    assert "overflow" not in axis.group(1)
    assert horizontal
    assert "overflow-x: auto" in horizontal.group(1)
    assert "overscroll-behavior-x: contain" in horizontal.group(1)
    assert ".pp-page { overflow-x" not in css


def test_short_intervals_have_a_separate_nonoverlapping_touch_target():
    css = CSS_PATH.read_text(encoding="utf-8")
    template = ROWS_TEMPLATE_PATH.read_text(encoding="utf-8")
    shortcut = re.search(r"\.pp-interval-shortcut\s*\{([^}]*)\}", css)

    assert shortcut
    assert "min-height: 44px" in shortcut.group(1)
    assert "min-width: 44px" in shortcut.group(1)
    assert 'class="pp-interval-shortcut"' in template
    assert "row.short_intervals" in template
    assert 'class="pp-timeline-viewport pp-horizontal-scroll"' in template
    assert 'class="pp-axis-viewport pp-horizontal-scroll"' in template


def test_refresh_source_contract_places_capture_immediately_before_replacement():
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    for token in (
        "AbortController",
        "requestEpoch",
        "response.redirected",
        "X-People-Performance-Response",
        "data-response-kind",
        "aria-busy",
        "preventScroll",
        "visibilitychange",
        "removeEventListener",
        "30000",
        '.pp-page[data-today="1"]',
    ):
        assert token in source

    refresh = source.split("function refreshRows()", 1)[1].split("function scheduleRefresh", 1)[0]
    assert refresh.index("response.text()") < refresh.index("captureState()")
    assert refresh.index("captureState()") < refresh.index("replaceWith")


def test_controller_runtime_handles_details_races_navigation_and_teardown():
    harness = textwrap.dedent(
        r"""
        const makeController = require(__SCRIPT__);

        function target(base) {
          base = base || {};
          base._listeners = {};
          base.addEventListener = function (type, callback) {
            (this._listeners[type] || (this._listeners[type] = [])).push(callback);
          };
          base.removeEventListener = function (type, callback) {
            const values = this._listeners[type] || [];
            this._listeners[type] = values.filter((value) => value !== callback);
          };
          base.emit = function (type, event) {
            return (this._listeners[type] || []).slice().map((callback) => callback(event || {}));
          };
          return base;
        }

        function deferred() {
          let resolve;
          let reject;
          const promise = new Promise((ok, bad) => { resolve = ok; reject = bad; });
          return {promise, resolve, reject};
        }

        function response(options) {
          options = options || {};
          const header = Object.prototype.hasOwnProperty.call(options, 'header')
            ? options.header : 'rows';
          return {
            ok: options.ok !== false,
            redirected: Boolean(options.redirected),
            url: options.url || '/people-performance/rows',
            headers: {
              get(name) {
                return name === 'X-People-Performance-Response' ? header : null;
              },
            },
            text() { return Promise.resolve(options.token || 'good'); },
          };
        }

        function makeEnvironment(today) {
          const requests = [];
          const timers = [];
          const aborts = [];
          const navigations = [];
          const focusOptions = [];
          const replacements = [];
          const parsed = {};
          let boundsReads = 0;
          let popover = null;

          const document = target({visibilityState: 'visible', activeElement: null});
          const windowObject = target({
            innerWidth: 320,
            innerHeight: 200,
            scrollX: 0,
            scrollY: 10,
          });
          const status = {textContent: ''};
          const page = {dataset: {today: String(today)}};

          function makeViewport(name) {
            return {
              name,
              scrollLeft: 0,
              matches(selector) { return selector === '.pp-horizontal-scroll'; },
            };
          }

          function makeTrigger(key, detail, kind, top) {
            const attributes = {};
            const trigger = {
              dataset: {intervalKey: key, detail},
              kind: kind || 'interval',
              focusCount: 0,
              closest(selector) {
                if (this.kind === 'shortcut' && selector.includes('.pp-interval-shortcut')) return this;
                if (this.kind === 'interval' && selector.includes('.pp-interval-trigger')) return this;
                return null;
              },
              matches(selector) {
                return (this.kind === 'shortcut' && selector === '.pp-interval-shortcut')
                  || (this.kind === 'interval' && selector === '.pp-interval-trigger');
              },
              contains(node) { return node === this; },
              getBoundingClientRect() {
                boundsReads += 1;
                const y = top == null ? 160 : top;
                return {left: 270, right: 310, top: y, bottom: y + 24, width: 40, height: 24};
              },
              setAttribute(name, value) { attributes[name] = String(value); },
              removeAttribute(name) { delete attributes[name]; },
              getAttribute(name) {
                if (name === 'aria-label') return detail;
                return attributes[name];
              },
              focus(options) {
                this.focusCount += 1;
                focusOptions.push(options);
                document.activeElement = this;
                document.emit('focusin', {target: this});
              },
            };
            return trigger;
          }

          function makeRows(day, isToday, triggers) {
            const attributes = {};
            const rows = {
              dataset: {
                day,
                isToday: String(isToday),
                attention: '0',
                responseKind: 'people-performance-rows',
                asOf: '2:00 PM',
              },
              triggers: triggers || [],
              viewports: [makeViewport('row')],
              setAttribute(name, value) { attributes[name] = String(value); },
              getAttribute(name) { return attributes[name]; },
              replaceWith(next) {
                replacements.push(next);
                document.rows = next;
                document.viewports = [document.axisViewport, ...next.viewports];
              },
            };
            return rows;
          }

          const first = makeTrigger('stable-open', 'Repair 1 working now', 'interval', 160);
          document.axisViewport = makeViewport('axis');
          document.rows = makeRows('2026-08-28', today, [first]);
          document.viewports = [document.axisViewport, ...document.rows.viewports];

          document.body = {
            appendChild(node) { popover = node; },
          };
          document.createElement = function () {
            const attributes = {};
            return {
              hidden: true,
              removed: false,
              style: {},
              textContent: '',
              id: '',
              className: '',
              setAttribute(name, value) { attributes[name] = String(value); },
              contains(node) { return node === this; },
              getBoundingClientRect() { return {width: 180, height: 50}; },
              remove() { this.removed = true; },
            };
          };
          document.getElementById = function (id) {
            if (id === 'people-performance-live') return this.rows;
            if (id === 'pp-live-status') return status;
            return null;
          };
          document.querySelector = function (selector) {
            if (selector === '.pp-page[data-today="1"]') {
              return page.dataset.today === '1' ? page : null;
            }
            const match = selector.match(/data-interval-key="([^"]+)"/);
            if (!match) return null;
            const key = match[1].replace(/\\"/g, '"').replace(/\\\\/g, '\\');
            const wantsShortcut = selector.includes('.pp-interval-shortcut');
            return this.rows.triggers.find((item) => (
              item.dataset.intervalKey === key && (wantsShortcut ? item.kind === 'shortcut' : item.kind === 'interval')
            )) || null;
          };
          document.querySelectorAll = function (selector) {
            return selector === '.pp-horizontal-scroll' ? this.viewports : [];
          };

          class AbortController {
            constructor() {
              this.signal = {aborted: false};
              aborts.push(this);
            }
            abort() { this.signal.aborted = true; }
          }
          windowObject.AbortController = AbortController;
          windowObject.gpiFetch = function (url, options) {
            const pending = deferred();
            requests.push({url, options, pending});
            return pending.promise;
          };
          windowObject.setInterval = function (callback, milliseconds) {
            const value = {callback, milliseconds, cleared: false};
            timers.push(value);
            return value;
          };
          windowObject.clearInterval = function (value) { value.cleared = true; };
          windowObject.scrollTo = function (x, y) { this.scrollX = x; this.scrollY = y; };
          windowObject.location = {
            href: '/people-performance',
            assign(value) { navigations.push(['assign', value]); },
            reload() { navigations.push(['reload']); },
          };
          windowObject.DOMParser = class {
            parseFromString(token) {
              return {getElementById() { return parsed[token] || null; }};
            }
          };

          return {
            document,
            windowObject,
            status,
            page,
            requests,
            timers,
            aborts,
            navigations,
            focusOptions,
            replacements,
            parsed,
            makeTrigger,
            makeRows,
            first,
            getPopover: () => popover,
            getBoundsReads: () => boundsReads,
          };
        }

        function event(target) {
          return {target, relatedTarget: null, preventDefault() {}};
        }

        async function flush() {
          await Promise.resolve();
          await Promise.resolve();
          await Promise.resolve();
        }

        (async () => {
          // Hover/focus/pin/toggle/outside/Escape plus describedby cleanup,
          // viewport flip/clamp, local scroll synchronization, and teardown.
          const detailEnv = makeEnvironment('1');
          const detailController = makeController(detailEnv.document, detailEnv.windowObject);
          detailController.init();
          if (detailEnv.timers.length !== 1 || detailEnv.timers[0].milliseconds !== 30000) {
            throw new Error('Today refresh timer was not scheduled');
          }
          const second = detailEnv.makeTrigger('second', 'Tablets calls', 'interval', 160);
          detailEnv.document.rows.triggers.push(second);
          detailEnv.document.emit('pointerover', event(detailEnv.first));
          const tip = detailEnv.getPopover();
          if (tip.hidden || detailEnv.first.getAttribute('aria-describedby') !== 'pp-detail-popover') {
            throw new Error('hover did not open details');
          }
          if (parseInt(tip.style.top, 10) >= 160 + detailEnv.windowObject.scrollY) {
            throw new Error('popover did not flip above a bottom-edge trigger');
          }
          if (parseInt(tip.style.left, 10) < 8) throw new Error('popover was not clamped');
          detailEnv.document.emit('pointerover', event(second));
          if (detailEnv.first.getAttribute('aria-describedby')) {
            throw new Error('opening a new trigger left stale describedby');
          }
          if (detailEnv.first.getAttribute('aria-expanded') !== 'false') {
            throw new Error('opening a new trigger left stale expanded state');
          }
          detailEnv.document.emit('focusin', event(detailEnv.first));
          detailEnv.document.emit('click', event(detailEnv.first));
          detailEnv.document.emit('pointerout', {target: detailEnv.first, relatedTarget: {}});
          if (tip.hidden) throw new Error('pinned details closed on pointerout');
          detailEnv.document.emit('click', event(detailEnv.first));
          if (!tip.hidden) throw new Error('second tap did not toggle pinned details closed');
          detailEnv.document.emit('click', event(detailEnv.first));
          detailEnv.document.emit('pointerdown', event({closest() { return null; }}));
          if (!tip.hidden) throw new Error('outside pointer did not close details');
          detailEnv.document.emit('click', event(detailEnv.first));
          detailEnv.document.emit('keydown', {key: 'Escape'});
          if (!tip.hidden || detailEnv.first.focusCount !== 1) {
            throw new Error('pinned Escape did not close and restore focus');
          }
          detailEnv.document.axisViewport.scrollLeft = 81;
          detailEnv.document.emit('scroll', {target: detailEnv.document.axisViewport});
          if (detailEnv.document.rows.viewports[0].scrollLeft !== 81) {
            throw new Error('local horizontal scroll was not synchronized');
          }
          const reads = detailEnv.getBoundsReads();
          detailEnv.document.emit('pointerover', event(detailEnv.first));
          detailEnv.windowObject.emit('resize', {});
          detailEnv.document.emit('scroll', {target: detailEnv.document.axisViewport});
          if (detailEnv.getBoundsReads() <= reads) throw new Error('popover did not reposition');
          detailController.destroy();
          if (!tip.removed || !detailEnv.timers[0].cleared) throw new Error('destroy did not clean up');
          for (const values of Object.values(detailEnv.document._listeners)) {
            if (values.length) throw new Error('destroy left document listeners behind');
          }
          for (const values of Object.values(detailEnv.windowObject._listeners)) {
            if (values.length) throw new Error('destroy left window listeners behind');
          }

          // A poll followed by a visibility refresh must abort/epoch-guard the
          // poll. State changed after both requests start is captured only at swap.
          const raceEnv = makeEnvironment('1');
          const raceController = makeController(raceEnv.document, raceEnv.windowObject);
          raceController.init();
          const pollPromise = raceEnv.timers[0].callback();
          const visiblePromise = raceEnv.document.emit('visibilitychange', {})[0];
          if (raceEnv.requests.length !== 2 || !raceEnv.aborts[0].signal.aborted) {
            throw new Error('visibility refresh did not abort the older poll');
          }
          raceEnv.first.focus();
          raceEnv.document.emit('click', event(raceEnv.first));
          raceEnv.windowObject.scrollY = 777;
          raceEnv.document.axisViewport.scrollLeft = 55;
          const fresh = raceEnv.makeTrigger('stable-open', 'fresh detail', 'interval', 100);
          const freshRows = raceEnv.makeRows('2026-08-28', '1', [fresh]);
          raceEnv.parsed.fresh = freshRows;
          raceEnv.requests[1].pending.resolve(response({token: 'fresh'}));
          if (await visiblePromise !== true) throw new Error('fresh response was not applied');
          if (raceEnv.document.rows !== freshRows || fresh.focusCount !== 1) {
            throw new Error('focus key was not restored after replacement');
          }
          if (!raceEnv.focusOptions.some((value) => value && value.preventScroll === true)) {
            throw new Error('focus was restored without preventScroll');
          }
          if (raceEnv.windowObject.scrollY !== 777 || raceEnv.document.axisViewport.scrollLeft !== 55) {
            throw new Error('scroll state changed while awaiting was not restored');
          }
          if (fresh.getAttribute('aria-expanded') !== 'true') {
            throw new Error('pinned interval was not restored');
          }
          const stale = raceEnv.makeTrigger('stale', 'stale detail', 'interval', 100);
          raceEnv.parsed.stale = raceEnv.makeRows('2026-08-28', '1', [stale]);
          raceEnv.requests[0].pending.resolve(response({token: 'stale'}));
          if (await pollPromise !== false || raceEnv.document.rows !== freshRows) {
            throw new Error('stale poll response replaced the fresh visibility response');
          }

          const focusBeforeMissing = raceEnv.focusOptions.length;
          const missingPromise = raceController.refreshRows();
          const missingRows = raceEnv.makeRows('2026-08-28', '1', []);
          raceEnv.parsed.missing = missingRows;
          raceEnv.requests[2].pending.resolve(response({token: 'missing'}));
          await missingPromise;
          if (raceEnv.focusOptions.length !== focusBeforeMissing || !raceEnv.getPopover().hidden) {
            throw new Error('missing key moved focus or kept stale details open');
          }

          // Keyboard focus without a pin remains focused and keeps its details
          // available after replacement.
          const focusEnv = makeEnvironment('1');
          const focusController = makeController(focusEnv.document, focusEnv.windowObject);
          focusController.init();
          const focusPromise = focusController.refreshRows();
          focusEnv.first.focus();
          const refreshedFocus = focusEnv.makeTrigger('stable-open', 'keyboard detail', 'interval', 100);
          focusEnv.parsed.keyboard = focusEnv.makeRows('2026-08-28', '1', [refreshedFocus]);
          focusEnv.requests[0].pending.resolve(response({token: 'keyboard'}));
          await focusPromise;
          if (refreshedFocus.focusCount !== 1 || refreshedFocus.getAttribute('aria-expanded') !== 'true') {
            throw new Error('un-pinned keyboard focus details were not restored');
          }
          focusController.destroy();

          // An open interval can age past the short-move threshold while a
          // request is in flight. Its stable key must restore focus and the pin
          // on the remaining timeline trigger even though the trigger kind changed.
          const agingEnv = makeEnvironment('1');
          const agingController = makeController(agingEnv.document, agingEnv.windowObject);
          agingController.init();
          const agingShortcut = agingEnv.makeTrigger(
            'aging-open', 'short open detail', 'shortcut', 100
          );
          agingEnv.document.rows.triggers = [agingShortcut];
          agingShortcut.focus();
          agingEnv.document.emit('click', event(agingShortcut));
          const agingPromise = agingController.refreshRows();
          const grownInterval = agingEnv.makeTrigger(
            'aging-open', 'grown timeline detail', 'interval', 100
          );
          agingEnv.parsed.aging = agingEnv.makeRows('2026-08-28', '1', [grownInterval]);
          agingEnv.requests[0].pending.resolve(response({token: 'aging'}));
          await agingPromise;
          if (grownInterval.focusCount !== 1 || grownInterval.getAttribute('aria-expanded') !== 'true') {
            throw new Error('shortcut-to-timeline refresh lost focus or pinned details');
          }
          agingController.destroy();

          // Historical pages neither poll nor refresh on visibility.
          const historyEnv = makeEnvironment('0');
          const historyController = makeController(historyEnv.document, historyEnv.windowObject);
          historyController.init();
          historyEnv.document.emit('visibilitychange', {});
          if (historyEnv.timers.length || historyEnv.requests.length) {
            throw new Error('historical page scheduled or requested a refresh');
          }
          historyController.destroy();

          // Redirect, missing/wrong header, wrong root, and a plant-day rollover all
          // choose safe full navigation instead of swapping unknown HTML.
          for (const scenario of ['redirect', 'missing-header', 'header', 'root', 'rollover']) {
            const env = makeEnvironment('1');
            const controller = makeController(env.document, env.windowObject);
            controller.init();
            if (scenario === 'rollover') env.document.rows.dataset.attention = '1';
            const promise = controller.refreshRows();
            if (scenario === 'redirect') {
              env.requests[0].pending.resolve(response({redirected: true, url: '/auth/login'}));
            } else if (scenario === 'missing-header') {
              env.parsed.missingHeader = env.makeRows('2026-08-28', '1', []);
              env.requests[0].pending.resolve(response({
                header: null, token: 'missingHeader'
              }));
            } else if (scenario === 'header') {
              env.requests[0].pending.resolve(response({header: 'not-rows'}));
            } else if (scenario === 'root') {
              env.requests[0].pending.resolve(response({token: 'no-root'}));
            } else {
              env.parsed.rollover = env.makeRows('2026-08-28', '0', []);
              env.requests[0].pending.resolve(response({token: 'rollover'}));
            }
            await promise;
            await flush();
            if (!env.navigations.length || env.replacements.length) {
              throw new Error(scenario + ' did not choose safe full navigation');
            }
            if (
              scenario === 'rollover'
              && JSON.stringify(env.navigations[0]) !== JSON.stringify([
                'assign', '/people-performance?attention=1'
              ])
            ) {
              throw new Error('rollover did not preserve the attention filter');
            }
            controller.destroy();
          }

          // Destroy aborts a live request as well as removing timers/listeners.
          const destroyEnv = makeEnvironment('1');
          const destroyController = makeController(destroyEnv.document, destroyEnv.windowObject);
          destroyController.init();
          destroyController.refreshRows();
          destroyController.destroy();
          if (!destroyEnv.aborts[0].signal.aborted || !destroyEnv.timers[0].cleared) {
            throw new Error('destroy did not abort the live request and timer');
          }
        })().catch((error) => {
          console.error(error.stack || error);
          process.exitCode = 1;
        });
        """
    ).replace("__SCRIPT__", json.dumps(str(SCRIPT_PATH)))

    result = subprocess.run(
        ["node", "--eval", harness],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
