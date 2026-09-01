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
        "data-pp-auto-submit",
        "requestSubmit",
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
            status: options.status || (options.ok === false ? 500 : 200),
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
          const filterSubmissions = [];
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
          const filterForm = {
            requestSubmit() { filterSubmissions.push('requestSubmit'); },
            submit() { filterSubmissions.push('submit'); },
          };
          const filterControl = {
            form: filterForm,
            closest(selector) {
              return selector === '[data-pp-auto-submit]' ? this : null;
            },
          };

          function makeViewport(name) {
            return {
              name,
              scrollLeft: 0,
              matches(selector) { return selector === '.pp-horizontal-scroll'; },
            };
          }

          function makeTrigger(key, detail, kind, top, datasetValues) {
            const attributes = {};
            const markerClasses = new Set();
            const marker = {
              style: {},
              classList: {
                add(value) { markerClasses.add(value); },
                remove(value) { markerClasses.delete(value); },
                contains(value) { return markerClasses.has(value); },
              },
            };
            const trigger = {
              dataset: Object.assign({intervalKey: key, detail}, datasetValues || {}),
              marker,
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
              querySelector(selector) {
                return selector === '.pp-hover-marker' ? marker : null;
              },
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
            filterSubmissions,
            filterControl,
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
          detailEnv.document.emit('change', {target: detailEnv.filterControl});
          if (JSON.stringify(detailEnv.filterSubmissions) !== JSON.stringify(['requestSubmit'])) {
            throw new Error('filter change did not submit exactly once');
          }
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
          detailEnv.document.emit('change', {target: detailEnv.filterControl});
          if (JSON.stringify(detailEnv.filterSubmissions) !== JSON.stringify(['requestSubmit'])) {
            throw new Error('destroy left filter change listener behind');
          }
          if (!tip.removed || !detailEnv.timers[0].cleared) throw new Error('destroy did not clean up');
          for (const values of Object.values(detailEnv.document._listeners)) {
            if (values.length) throw new Error('destroy left document listeners behind');
          }
          for (const values of Object.values(detailEnv.windowObject._listeners)) {
            if (values.length) throw new Error('destroy left window listeners behind');
          }

          // Production intervals select, pin, and restore the exact minute while
          // keyboard/tap access falls back to the latest checkpoint.
          const preciseEnv = makeEnvironment('1');
          preciseEnv.windowObject.Intl = Intl;
          const startMs = Date.UTC(2026, 7, 28, 12, 0);
          const preciseDataset = {
            productionHover: JSON.stringify([
              [startMs, 0, 0, null],
              [startMs + 30 * 60000, 12, 20, 75.4],
              [startMs + 60 * 60000, 30, 40, 92.6],
            ]),
            hoverStartMs: String(startMs),
            hoverEndMs: String(startMs + 60 * 60000),
          };
          const precise = preciseEnv.makeTrigger(
            'precise',
            'old interval detail',
            'interval',
            80,
            preciseDataset
          );
          preciseEnv.document.rows.triggers = [precise];
          const preciseController = makeController(preciseEnv.document, preciseEnv.windowObject);
          preciseController.init();
          preciseEnv.document.emit('pointerover', event(precise));
          preciseEnv.document.emit('pointermove', {...event(precise), clientX: 290});
          const preciseTip = preciseEnv.getPopover();
          if (preciseTip.textContent !== '7:30 AM\nProduction: 12.0 / 20.0\nUptime 75%') {
            throw new Error('precise production tooltip format or value is wrong: ' + preciseTip.textContent);
          }
          if (!precise.marker.classList.contains('is-visible') || precise.marker.style.left !== '50%') {
            throw new Error('precise hover marker did not follow the selected minute');
          }
          preciseEnv.document.emit('click', event(precise));
          preciseEnv.document.emit('pointermove', {...event(precise), clientX: 309});
          if (preciseTip.textContent.includes('30.0 / 40.0')) {
            throw new Error('a pinned precise minute changed during pointer movement');
          }

          const secondBoundsEnv = makeEnvironment('0');
          secondBoundsEnv.windowObject.Intl = Intl;
          const secondStartMs = startMs + 31000;
          const secondEndMs = startMs + 60 * 60000 + 29000;
          const roundedInteriorMs = startMs + 31 * 60000;
          const secondBounds = secondBoundsEnv.makeTrigger(
            'second-bounds',
            'second-bearing bounds',
            'interval',
            80,
            {
              productionHover: JSON.stringify([
                [secondStartMs, 1, 2, 90],
                [roundedInteriorMs, 3, 4, 91],
                [secondEndMs, 5, 6, 92],
              ]),
              hoverStartMs: String(secondStartMs),
              hoverEndMs: String(secondEndMs),
            }
          );
          secondBoundsEnv.document.rows.triggers = [secondBounds];
          const secondBoundsController = makeController(
            secondBoundsEnv.document, secondBoundsEnv.windowObject
          );
          secondBoundsController.init();
          secondBoundsEnv.document.emit(
            'pointermove', {...event(secondBounds), clientX: 270}
          );
          if (
            secondBounds.marker.style.left !== '0%'
            || !secondBoundsEnv.getPopover().textContent.includes('Production: 1.0 / 2.0')
          ) {
            throw new Error('second-bearing left edge did not select the exact interval start');
          }
          secondBoundsEnv.document.emit(
            'pointermove', {...event(secondBounds), clientX: 310}
          );
          if (
            secondBounds.marker.style.left !== '100%'
            || !secondBoundsEnv.getPopover().textContent.includes('Production: 5.0 / 6.0')
          ) {
            throw new Error('second-bearing right edge did not select the exact interval end');
          }
          secondBoundsEnv.document.emit(
            'pointermove', {...event(secondBounds), clientX: 290}
          );
          const interiorMarker = parseFloat(secondBounds.marker.style.left);
          const expectedInterior = 100 * (roundedInteriorMs - secondStartMs)
            / (secondEndMs - secondStartMs);
          if (
            Math.abs(interiorMarker - expectedInterior) > 0.000001
            || !secondBoundsEnv.getPopover().textContent.includes('Production: 3.0 / 4.0')
          ) {
            throw new Error('second-bearing interior selection was not rounded then clamped');
          }
          secondBoundsController.destroy();

          const precisePromise = preciseController.refreshRows();
          const shorterPreciseDataset = {
            productionHover: JSON.stringify([
              [startMs, 0, 0, null],
              [startMs + 20 * 60000, 8, 10, 81.2],
            ]),
            hoverStartMs: String(startMs),
            hoverEndMs: String(startMs + 20 * 60000),
          };
          const refreshedPrecise = preciseEnv.makeTrigger(
            'precise', 'new interval detail', 'interval', 80, shorterPreciseDataset
          );
          preciseEnv.parsed.precise = preciseEnv.makeRows(
            '2026-08-28', '1', [refreshedPrecise]
          );
          preciseEnv.requests[0].pending.resolve(response({token: 'precise'}));
          await precisePromise;
          if (
            preciseTip.textContent !== '7:20 AM\nProduction: 8.0 / 10.0\nUptime 81%'
            || refreshedPrecise.marker.style.left !== '100%'
          ) {
            throw new Error('refresh did not clamp the pinned minute to the shorter interval');
          }
          preciseController.destroy();
          if (refreshedPrecise.marker.classList.contains('is-visible')) {
            throw new Error('destroy did not hide the precise hover marker');
          }

          const lifecycleEnv = makeEnvironment('1');
          lifecycleEnv.windowObject.Intl = Intl;
          const lifecycleKey = '88:production:Repair 1:2026-08-28T12:00:00+00:00';
          const openLifecycle = lifecycleEnv.makeTrigger(
            lifecycleKey,
            'open lifecycle interval',
            'interval',
            80,
            preciseDataset
          );
          lifecycleEnv.document.rows.triggers = [openLifecycle];
          const lifecycleController = makeController(
            lifecycleEnv.document, lifecycleEnv.windowObject
          );
          lifecycleController.init();
          lifecycleEnv.document.emit(
            'pointermove', {...event(openLifecycle), clientX: 290}
          );
          lifecycleEnv.document.emit('click', event(openLifecycle));
          const lifecyclePromise = lifecycleController.refreshRows();
          const closedLifecycle = lifecycleEnv.makeTrigger(
            lifecycleKey,
            'closed lifecycle interval',
            'interval',
            80,
            {
              productionHover: JSON.stringify([
                [startMs, 0, 0, null],
                [startMs + 30 * 60000, 12, 20, 75.4],
                [startMs + 45 * 60000, 18, 30, 88],
              ]),
              hoverStartMs: String(startMs),
              hoverEndMs: String(startMs + 45 * 60000),
            }
          );
          const transferredLifecycle = lifecycleEnv.makeTrigger(
            '88:production:Repair 2:2026-08-28T12:45:00+00:00',
            'transferred lifecycle interval',
            'interval',
            80,
            {
              productionHover: JSON.stringify([
                [startMs + 45 * 60000, 18, 30, null],
                [startMs + 60 * 60000, 22, 38, 90],
              ]),
              hoverStartMs: String(startMs + 45 * 60000),
              hoverEndMs: String(startMs + 60 * 60000),
            }
          );
          lifecycleEnv.parsed.lifecycle = lifecycleEnv.makeRows(
            '2026-08-28', '1', [closedLifecycle, transferredLifecycle]
          );
          lifecycleEnv.requests[0].pending.resolve(response({token: 'lifecycle'}));
          await lifecyclePromise;
          if (
            lifecycleEnv.getPopover().textContent
              !== '7:30 AM\nProduction: 12.0 / 20.0\nUptime 75%'
            || closedLifecycle.getAttribute('aria-expanded') !== 'true'
            || transferredLifecycle.getAttribute('aria-expanded') === 'true'
          ) {
            throw new Error('open-to-closed transfer refresh did not restore the pinned minute by exact key');
          }
          lifecycleController.destroy();

          const focusProductionEnv = makeEnvironment('0');
          focusProductionEnv.windowObject.Intl = Intl;
          const fallbackDataset = {
            productionHover: JSON.stringify([
              [startMs, 0, 0, null],
              [startMs + 30 * 60000, 12, 20, 75.4],
            ]),
            hoverStartMs: String(startMs),
            hoverEndMs: String(startMs + 60 * 60000),
          };
          const focusedProduction = focusProductionEnv.makeTrigger(
            'focused-production', 'old production detail', 'interval', 80, fallbackDataset
          );
          focusProductionEnv.document.rows.triggers = [focusedProduction];
          const focusProductionController = makeController(
            focusProductionEnv.document, focusProductionEnv.windowObject
          );
          focusProductionController.init();
          focusProductionEnv.document.emit('focusin', event(focusedProduction));
          if (
            focusProductionEnv.getPopover().textContent
              !== '8:00 AM\nProduction: 12.0 / 20.0\nUptime 75%'
            || focusedProduction.marker.style.left !== '100%'
          ) {
            throw new Error('keyboard focus did not select the interval end');
          }
          focusProductionEnv.document.emit('click', event(focusedProduction));
          if (
            focusProductionEnv.getPopover().textContent
              !== '8:00 AM\nProduction: 12.0 / 20.0\nUptime 75%'
            || focusedProduction.marker.style.left !== '100%'
          ) {
            throw new Error('tap did not select the interval end');
          }
          focusProductionEnv.document.emit('click', event(focusedProduction));
          if (focusedProduction.marker.classList.contains('is-visible')) {
            throw new Error('closing production details did not hide the marker');
          }
          const tappedProduction = focusProductionEnv.makeTrigger(
            'tapped-production', 'old tapped detail', 'interval', 80, fallbackDataset
          );
          focusProductionEnv.document.rows.triggers.push(tappedProduction);
          focusProductionEnv.document.emit('click', event(tappedProduction));
          if (
            focusProductionEnv.getPopover().textContent
              !== '8:00 AM\nProduction: 12.0 / 20.0\nUptime 75%'
            || tappedProduction.marker.style.left !== '100%'
          ) {
            throw new Error('direct tap did not fall back to the interval end');
          }
          focusProductionController.destroy();

          const shortProductionEnv = makeEnvironment('0');
          shortProductionEnv.windowObject.Intl = Intl;
          const shortProduction = shortProductionEnv.makeTrigger(
            'short-production', 'old short detail', 'shortcut', 80, fallbackDataset
          );
          shortProductionEnv.document.rows.triggers = [shortProduction];
          const shortProductionController = makeController(
            shortProductionEnv.document, shortProductionEnv.windowObject
          );
          shortProductionController.init();
          shortProductionEnv.document.emit('focusin', event(shortProduction));
          if (
            shortProductionEnv.getPopover().textContent
              !== '8:00 AM\nProduction: 12.0 / 20.0\nUptime 75%'
            || shortProduction.marker.style.left !== '100%'
          ) {
            throw new Error('production short move did not fall back to the interval end');
          }
          shortProductionController.destroy();

          const malformedEnv = makeEnvironment('0');
          malformedEnv.windowObject.Intl = Intl;
          const malformed = malformedEnv.makeTrigger(
            'malformed-production',
            'old malformed detail',
            'interval',
            80,
            {
              productionHover: JSON.stringify([null, ['not-a-time', 4, 5, 50]]),
              hoverStartMs: 'not-a-start',
              hoverEndMs: 'not-an-end',
            }
          );
          malformedEnv.document.rows.triggers = [malformed];
          const malformedController = makeController(malformedEnv.document, malformedEnv.windowObject);
          malformedController.init();
          malformedEnv.document.emit('focusin', event(malformed));
          if (malformedEnv.getPopover().textContent !== 'Time unavailable\nProduction: N/A\nUptime N/A') {
            throw new Error('malformed production values did not use the safe N/A card');
          }
          malformedController.destroy();

          const partialEnv = makeEnvironment('0');
          partialEnv.windowObject.Intl = Intl;
          const invalidProduction = partialEnv.makeTrigger(
            'invalid-production',
            'old invalid detail',
            'interval',
            80,
            {
              productionHover: JSON.stringify([[startMs, null, 20, 88.6]]),
              hoverStartMs: String(startMs),
              hoverEndMs: String(startMs + 30 * 60000),
            }
          );
          partialEnv.document.rows.triggers = [invalidProduction];
          const partialController = makeController(partialEnv.document, partialEnv.windowObject);
          partialController.init();
          partialEnv.document.emit('focusin', event(invalidProduction));
          if (partialEnv.getPopover().textContent !== '7:30 AM\nProduction: N/A\nUptime 89%') {
            throw new Error('valid uptime was lost when production was unavailable');
          }
          const unavailableUptime = partialEnv.makeTrigger(
            'unavailable-uptime',
            'old unavailable detail',
            'interval',
            80,
            {
              productionHover: JSON.stringify([[startMs, 10, 20, null]]),
              hoverStartMs: String(startMs),
              hoverEndMs: String(startMs + 30 * 60000),
            }
          );
          partialEnv.document.rows.triggers.push(unavailableUptime);
          partialEnv.document.emit('focusin', event(unavailableUptime));
          if (partialEnv.getPopover().textContent !== '7:30 AM\nProduction: 10.0 / 20.0\nUptime N/A') {
            throw new Error('valid production was lost when uptime was unavailable');
          }
          partialController.destroy();

          const emptyProductionEnv = makeEnvironment('0');
          emptyProductionEnv.windowObject.Intl = Intl;
          const emptyProduction = emptyProductionEnv.makeTrigger(
            'empty-production',
            'old empty detail',
            'interval',
            80,
            {
              productionHover: '[]',
              hoverStartMs: String(startMs),
              hoverEndMs: String(startMs + 60 * 60000),
            }
          );
          emptyProductionEnv.document.rows.triggers = [emptyProduction];
          const emptyProductionController = makeController(
            emptyProductionEnv.document, emptyProductionEnv.windowObject
          );
          emptyProductionController.init();
          emptyProductionEnv.document.emit('focusin', event(emptyProduction));
          if (!emptyProductionEnv.getPopover().textContent.endsWith('Production: N/A\nUptime N/A')) {
            throw new Error('empty production values did not use the N/A detail card');
          }
          emptyProductionController.destroy();

          const forkliftEnv = makeEnvironment('0');
          const forklift = forkliftEnv.makeTrigger(
            'forklift', 'Forklift call details stay unchanged', 'interval', 80
          );
          forkliftEnv.document.rows.triggers = [forklift];
          const forkliftController = makeController(forkliftEnv.document, forkliftEnv.windowObject);
          forkliftController.init();
          forkliftEnv.document.emit('pointerover', event(forklift));
          if (forkliftEnv.getPopover().textContent !== 'Forklift call details stay unchanged') {
            throw new Error('non-production detail text changed');
          }
          forkliftController.destroy();

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

          // Ordinary application errors preserve the last good rows and show
          // a paused status even when an error response has no partial marker.
          for (const statusCode of [404, 500]) {
            const env = makeEnvironment('1');
            const controller = makeController(env.document, env.windowObject);
            controller.init();
            const originalRows = env.document.rows;
            const promise = controller.refreshRows();
            env.requests[0].pending.resolve(response({
              ok: false, status: statusCode, header: null
            }));
            if (await promise !== false) throw new Error('error refresh reported success');
            if (
              env.document.rows !== originalRows
              || env.replacements.length
              || env.navigations.length
            ) {
              throw new Error(statusCode + ' response replaced rows or navigated');
            }
            if (env.status.textContent !== 'Update paused — showing the last good view') {
              throw new Error(statusCode + ' response did not show paused status');
            }
            controller.destroy();
          }

          // Redirect, auth, missing/wrong header, wrong root, and a plant-day rollover all
          // choose safe full navigation instead of swapping unknown HTML.
          for (const scenario of ['redirect', 'auth', 'missing-header', 'header', 'root', 'rollover']) {
            const env = makeEnvironment('1');
            const controller = makeController(env.document, env.windowObject);
            controller.init();
            if (scenario === 'rollover') env.document.rows.dataset.attention = '1';
            const promise = controller.refreshRows();
            if (scenario === 'redirect') {
              env.requests[0].pending.resolve(response({redirected: true, url: '/auth/login'}));
            } else if (scenario === 'auth') {
              env.requests[0].pending.resolve(response({
                ok: false, status: 401, header: null, url: '/auth/login'
              }));
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


def test_people_performance_preview_mode_explicitly_disables_live_polling():
    script = SCRIPT_PATH.read_text(encoding="utf-8")
    template = (ROOT / "src/zira_dashboard/templates/people_performance.html").read_text(
        encoding="utf-8"
    )

    assert 'data-poll-disabled="{{ 1 if poll_disabled else 0 }}"' in template
    assert 'page.dataset.pollDisabled === "1"' in script
    assert 'data-live-polling-disabled="{{ 1 if poll_disabled else 0 }}"' in template

    footer = (ROOT / "src/zira_dashboard/static/footer.js").read_text(encoding="utf-8")
    assert footer.count('document.body.dataset.livePollingDisabled === "1"') >= 2
