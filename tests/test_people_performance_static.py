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


def test_people_assets_exist_and_are_embedded_in_the_full_page_atomically():
    assert CSS_PATH.exists()
    assert SCRIPT_PATH.exists()
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert '<style data-pp-asset="people-performance.css">' in template
    assert '{{ static_text("people-performance.css") }}' in template
    assert '<script data-pp-asset="people-performance.js">' in template
    assert '{{ static_text("people-performance.js") }}' in template
    assert 'href="/static/people-performance.css' not in template
    assert 'src="/static/people-performance.js' not in template


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


def test_manager_strip_is_sticky_and_manager_groups_wrap_without_scrollbars():
    css = CSS_PATH.read_text(encoding="utf-8")
    strip = re.search(r"\.pp-manager-strip\s*\{([^}]*)\}", css)
    primary = re.search(r"\.pp-manager-primary\s*\{([^}]*)\}", css)
    actions = re.search(r"\.pp-manager-actions\s*\{([^}]*)\}", css)
    warnings = re.search(r"\.pp-source-warnings\s*\{([^}]*)\}", css)
    horizontal = re.search(r"\.pp-horizontal-scroll\s*\{([^}]*)\}", css)

    assert strip
    assert "position: sticky" in strip.group(1)
    assert "display: grid" in strip.group(1)
    assert "grid-template-columns: minmax(0, 1fr)" in strip.group(1)
    assert not re.search(r"(?m)^\s*(?:min-)?height:", strip.group(1))
    assert "height: 2.75rem" not in strip.group(1)
    assert "box-sizing: border-box" in strip.group(1)
    assert "overflow" not in strip.group(1)
    assert primary and "flex-wrap: wrap" in primary.group(1)
    assert actions and "flex-wrap: wrap" in actions.group(1)
    action_rules = re.findall(r"\.pp-manager-actions\s*\{([^}]*)\}", css)
    assert any("flex: 1 1 16rem" in rule for rule in action_rules)
    assert warnings and "flex-wrap: wrap" in warnings.group(1)
    assert "flex: 0 1 auto" in warnings.group(1)
    assert "width: auto" in warnings.group(1)
    assert "overflow-x" not in warnings.group(1)
    assert horizontal and "overflow-x: auto" in horizontal.group(1)
    assert ".pp-page { overflow-x" not in css
    warning_count = re.search(r"\.pp-warning-count\s*\{([^}]*)\}", css)
    assert warning_count
    assert "border-radius: 999px" in warning_count.group(1)
    assert "place-items: center" in warning_count.group(1)


def test_section_headers_share_row_columns_and_time_tracks():
    css = CSS_PATH.read_text(encoding="utf-8")
    shared_grid = re.search(
        r"\.pp-section-header,\s*\.pp-row\s*\{([^}]*)\}", css
    )
    shared_tracks = re.search(
        r"\.pp-schedule-track,\s*\.pp-timeline\s*\{([^}]*)\}", css
    )

    assert shared_grid
    assert (
        "grid-template-columns: minmax(10.5rem, .85fr) minmax(0, 4fr) "
        "minmax(16rem, 1.35fr)"
    ) in shared_grid.group(1)
    assert shared_tracks
    assert "width: max(100%, var(--pp-track-width))" in shared_tracks.group(1)
    time_group = re.search(r"\.pp-schedule-time-group\s*\{([^}]*)\}", css)
    assert time_group and "ui-monospace" in time_group.group(1)
    assert ".pp-schedule-time-group.is-start" in css
    assert ".pp-schedule-time-group.is-end" in css


def test_compact_header_media_queries_preserve_shared_layout_contracts():
    css = CSS_PATH.read_text(encoding="utf-8")
    tablet = css.split("@media (max-width: 1100px)", 1)[1].split(
        "@media (max-width: 760px)", 1
    )[0]
    mobile = css.split("@media (max-width: 760px)", 1)[1]

    tablet_grid = re.search(
        r"\.pp-section-header,\s*\.pp-row\s*\{([^}]*)\}", tablet
    )
    tablet_tracks = re.search(
        r"\.pp-schedule-track,\s*\.pp-timeline\s*\{([^}]*)\}", tablet
    )
    tablet_summary = re.search(r"\.pp-section-summary\s*\{([^}]*)\}", tablet)
    mobile_grid = re.search(
        r"\.pp-section-header,\s*\.pp-row\s*\{([^}]*)\}", mobile
    )

    assert tablet_grid
    assert "grid-template-columns: 12rem minmax(0, 1fr)" in tablet_grid.group(1)
    assert tablet_tracks is None
    assert tablet_summary and "display: none" in tablet_summary.group(1)
    assert mobile_grid
    assert "grid-template-columns: 10rem minmax(0, 1fr)" in mobile_grid.group(1)


def test_manager_groups_wrap_at_all_widths_without_local_horizontal_scroll():
    css = CSS_PATH.read_text(encoding="utf-8")
    controls = re.search(r"\.pp-controls\s*\{([^}]*)\}", css)
    shared = re.search(
        r"\.pp-counts,\s*\.pp-source-warnings,\s*\.pp-controls\s*\{([^}]*)\}",
        css,
    )
    warning_pill = re.search(r"\.pp-warning-trigger\s*\{([^}]*)\}", css)
    mobile = css.split("@media (max-width: 760px)", 1)[1]

    assert shared and "flex-wrap: wrap" in shared.group(1)
    assert controls and "flex-wrap: wrap" in controls.group(1)
    assert "overflow-x" not in controls.group(1)
    assert warning_pill and "white-space: normal" in warning_pill.group(1)
    assert "overflow-wrap: anywhere" in warning_pill.group(1)
    for selector in (".pp-counts", ".pp-source-warnings", ".pp-controls"):
        rule = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", mobile)
        assert not rule or "overflow-x" not in rule.group(1)


def test_warning_triggers_and_panel_have_accessible_action_styles():
    css = CSS_PATH.read_text(encoding="utf-8")
    trigger = re.search(r"\.pp-warning-trigger\s*\{([^}]*)\}", css)
    panel = re.search(r"\.pp-warning-popover\s*\{([^}]*)\}", css)
    actions = re.search(
        r"\.pp-warning-popover button,\s*\.pp-warning-popover a\s*\{([^}]*)\}",
        css,
    )

    assert trigger
    for declaration in (
        "min-height: 44px",
        "cursor: pointer",
        "font: inherit",
        "white-space: normal",
        "overflow-wrap: anywhere",
    ):
        assert declaration in trigger.group(1)
    assert ".pp-warning-trigger:focus-visible" in css
    assert '.pp-warning-trigger[aria-expanded="true"]' in css
    assert panel
    for declaration in (
        "position: absolute",
        "z-index: 1050",
        "width: min(26rem, calc(100vw - 1rem))",
        "max-height: min(34rem, calc(100vh - 1rem))",
        "overflow: auto",
    ):
        assert declaration in panel.group(1)
    assert ".pp-warning-popover[hidden]" in css
    assert ".pp-warning-popover header," in css
    assert ".pp-warning-popover footer" in css
    assert ".pp-warning-popover dl div" in css
    assert actions and "min-height: 44px" in actions.group(1)


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
    assert 'class="pp-schedule-viewport pp-horizontal-scroll"' in template


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

        function detailResponse(token, options) {
          return response(Object.assign(
            {header: 'warning-detail', token}, options || {}
          ));
        }

        function makeEnvironment(today, environmentOptions) {
          environmentOptions = environmentOptions || {};
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
          const actionStatus = {textContent: ''};
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

          function makeControl(key, kind, value, checked) {
            return {
              dataset: {ppControlKey: key},
              kind,
              value: value == null ? '' : value,
              checked: Boolean(checked),
              focusCount: 0,
              closest(selector) {
                return selector === '[data-pp-control-key]' ? this : null;
              },
              focus(options) {
                this.focusCount += 1;
                focusOptions.push(options);
                document.activeElement = this;
                document.emit('focusin', {target: this});
              },
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

          function makeWarningTrigger(key, summary, top) {
            const attributes = {"aria-expanded": "false"};
            const trigger = {
              dataset: {warningKey: key, warningSummary: summary},
              focusCount: 0,
              closest(selector) {
                return selector === '.pp-warning-trigger' ? this : null;
              },
              matches(selector) { return selector === '.pp-warning-trigger'; },
              contains(node) { return node === this; },
              getBoundingClientRect() {
                boundsReads += 1;
                const y = top == null ? 160 : top;
                return {left: 260, right: 310, top: y, bottom: y + 44, width: 50, height: 44};
              },
              setAttribute(name, value) { attributes[name] = String(value); },
              removeAttribute(name) { delete attributes[name]; },
              getAttribute(name) { return attributes[name]; },
              focus(options) {
                this.focusCount += 1;
                focusOptions.push(options);
                document.activeElement = this;
                document.emit('focusin', {target: this});
              },
            };
            return trigger;
          }

          function makeElement(tagName) {
            const attributes = {};
            return {
              tagName: String(tagName || '').toUpperCase(),
              isConnected: false,
              hidden: false,
              disabled: false,
              focusCount: 0,
              removed: false,
              style: {},
              textContent: '',
              id: '',
              className: '',
              children: [],
              parentNode: null,
              appendChild(node) {
                node.parentNode = this;
                setConnected(node, this.isConnected);
                this.children.push(node);
                return node;
              },
              replaceChildren(...nodes) {
                this.children.forEach((node) => {
                  node.parentNode = null;
                  setConnected(node, false);
                });
                this.children = [];
                nodes.forEach((node) => this.appendChild(node));
              },
              setAttribute(name, value) { attributes[name] = String(value); },
              removeAttribute(name) { delete attributes[name]; },
              getAttribute(name) { return attributes[name]; },
              contains(node) {
                return node === this || this.children.some((child) => child.contains && child.contains(node));
              },
              closest(selector) {
                if (selector === '[data-pp-warning-close]' && attributes['data-pp-warning-close'] != null) {
                  return this;
                }
                const action = selector.match(/^\[data-pp-warning-action\]$/);
                if (action && attributes['data-pp-warning-action'] != null) return this;
                return this.parentNode && this.parentNode.closest
                  ? this.parentNode.closest(selector) : null;
              },
              querySelector(selector) {
                const actionMatch = selector.match(/^\[data-pp-warning-action="([^"]+)"\]$/);
                const matches = (
                  selector === '[data-pp-warning-close]'
                    && attributes['data-pp-warning-close'] != null
                ) || (
                  selector === '[data-pp-warning-action]'
                    && attributes['data-pp-warning-action'] != null
                ) || (
                  actionMatch
                    && attributes['data-pp-warning-action'] === actionMatch[1]
                );
                if (matches) return this;
                for (const child of this.children) {
                  const found = child.querySelector ? child.querySelector(selector) : null;
                  if (found) return found;
                }
                return null;
              },
              focus(options) {
                this.focusCount += 1;
                focusOptions.push(options);
                document.activeElement = this;
                document.emit('focusin', {target: this});
              },
              getBoundingClientRect() { return {width: 220, height: 90}; },
              remove() { this.removed = true; },
            };
          }

          function setConnected(node, connected) {
            if (!node) return;
            node.isConnected = connected;
            (node.children || []).forEach((child) => setConnected(child, connected));
          }

          function makeWarningContent(state, label, controls) {
            const content = makeElement('section');
            content.id = 'pp-warning-panel-content';
            content.dataset = {warningState: state};
            content.textContent = label || state;
            (controls || []).forEach((control) => content.appendChild(control));
            return content;
          }

          function makeWarningAction(action) {
            const button = makeElement('button');
            button.setAttribute('data-pp-warning-action', action);
            button.dataset = {ppWarningAction: action};
            button.textContent = action === 'check_again' ? 'Check again' : 'Retry';
            return button;
          }

          function makeWarningClose() {
            const button = makeElement('button');
            button.setAttribute('data-pp-warning-close', '');
            return button;
          }

          function makeWarningLink(action) {
            const link = makeElement('a');
            link.setAttribute('data-pp-warning-action', action);
            link.dataset = {ppWarningAction: action};
            link.textContent = 'Open diagnostics';
            return link;
          }

          function makeRows(day, isToday, triggers, controls, warnings) {
            const attributes = {};
            const rows = {
              dataset: {
                day,
                isToday: String(isToday),
                status: '',
                attention: '0',
                responseKind: 'people-performance-rows',
                asOf: '2:00 PM',
              },
              triggers: triggers || [],
              controls: controls || [],
              warnings: warnings || [],
              viewports: [makeViewport('row')],
              setAttribute(name, value) { attributes[name] = String(value); },
              getAttribute(name) { return attributes[name]; },
              replaceWith(next) {
                replacements.push(next);
                document.rows = next;
                document.controls = next.controls;
                document.warnings = next.warnings;
                document.viewports = [document.axisViewport, ...next.viewports];
              },
            };
            return rows;
          }

          const first = makeTrigger('stable-open', 'Repair 1 working now', 'interval', 160);
          const dateControl = makeControl('day', 'date', '2026-08-28');
          const attentionControl = makeControl('attention', 'checkbox', '1', false);
          document.axisViewport = makeViewport('axis');
          document.rows = makeRows(
            '2026-08-28', today, [first], [dateControl, attentionControl]
          );
          document.controls = document.rows.controls;
          document.warnings = document.rows.warnings;
          document.viewports = [document.axisViewport, ...document.rows.viewports];

          const warningPanel = makeElement('div');
          warningPanel.id = 'pp-warning-popover';
          warningPanel.hidden = true;
          warningPanel.isConnected = true;

          document.body = {
            appendChild(node) { popover = node; },
          };
          document.createElement = makeElement;
          document.getElementById = function (id) {
            if (id === 'people-performance-live') return this.rows;
            if (id === 'pp-live-status') return status;
            if (id === 'pp-warning-popover') return warningPanel;
            if (id === 'pp-action-status') return actionStatus;
            return null;
          };
          document.querySelector = function (selector) {
            if (selector === '.pp-page[data-today="1"]') {
              return page.dataset.today === '1' ? page : null;
            }
            const controlMatch = selector.match(/data-pp-control-key="([^"]+)"/);
            if (controlMatch) {
              return this.controls.find((item) => item.dataset.ppControlKey === controlMatch[1]) || null;
            }
            const warningMatch = selector.match(/\.pp-warning-trigger\[data-warning-key="([^"]+)"\]/);
            if (warningMatch) {
              const key = warningMatch[1].replace(/\\"/g, '"').replace(/\\\\/g, '\\');
              return this.warnings.find((item) => item.dataset.warningKey === key) || null;
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
          windowObject.matchMedia = function (query) {
            return {matches: Boolean(environmentOptions.coarsePointer && query.includes('coarse'))};
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
            actionStatus,
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
            makeControl,
            makeRows,
            makeWarningTrigger,
            makeWarningContent,
            makeWarningAction,
            makeWarningClose,
            makeWarningLink,
            first,
            dateControl,
            attentionControl,
            getPopover: () => popover,
            warningPanel,
            getBoundsReads: () => boundsReads,
          };
        }

        function event(target) {
          return {
            target,
            relatedTarget: null,
            defaultPrevented: false,
            preventDefault() { this.defaultPrevented = true; },
          };
        }

        function elementText(node) {
          if (!node) return '';
          return [node.textContent || '', ...(node.children || []).map(elementText)].join(' ');
        }

        function makeCountControl(kind, value, pressed) {
          return {
            disabled: false,
            dataset: {ppCountFilter: kind, filterValue: value},
            closest(selector) {
              return selector === '[data-pp-count-filter]' ? this : null;
            },
            getAttribute(name) {
              return name === 'aria-pressed' ? String(pressed) : null;
            },
          };
        }

        function expectLastNavigation(env, expected) {
          const actual = env.navigations[env.navigations.length - 1];
          if (JSON.stringify(actual) !== JSON.stringify(['assign', expected])) {
            throw new Error('wrong count-filter navigation: ' + JSON.stringify(actual));
          }
        }

        async function flush() {
          for (let turn = 0; turn < 12; turn += 1) await Promise.resolve();
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

          const filterEnv = makeEnvironment('1');
          const filterController = makeController(filterEnv.document, filterEnv.windowObject);
          filterController.init();
          filterEnv.document.rows.dataset.status = '';
          filterEnv.document.emit('click', event(makeCountControl('status', 'working', false)));
          expectLastNavigation(
            filterEnv, '/people-performance?day=2026-08-28&status=working'
          );

          filterEnv.document.rows.dataset.status = 'working';
          filterEnv.document.rows.dataset.attention = '1';
          filterEnv.document.emit('click', event(makeCountControl('status', 'working', true)));
          expectLastNavigation(filterEnv, '/people-performance?day=2026-08-28&attention=1');

          filterEnv.document.emit('click', event(makeCountControl('attention', '1', true)));
          expectLastNavigation(filterEnv, '/people-performance?day=2026-08-28&status=working');

          const filteredPoll = filterController.refreshRows();
          if (
            filterEnv.requests[0].url
              !== '/people-performance/rows?day=2026-08-28&status=working&attention=1'
          ) {
            throw new Error('polling URL did not preserve both count filters');
          }
          const filteredRollover = filterEnv.makeRows('2026-08-28', '0', []);
          filterEnv.parsed.filteredRollover = filteredRollover;
          filterEnv.requests[0].pending.resolve(response({token: 'filteredRollover'}));
          await filteredPoll;
          expectLastNavigation(
            filterEnv, '/people-performance?status=working&attention=1'
          );
          filterController.destroy();

          // Warning previews work with pointer and keyboard focus, while coarse
          // pointer hover is ignored. A click pins and loads marked detail.
          const warningEnv = makeEnvironment('1');
          const warningOne = warningEnv.makeWarningTrigger(
            'odoo\\source"one', 'Odoo has not updated recently', 150
          );
          const warningTwo = warningEnv.makeWarningTrigger(
            'tablets-two', 'Tablet data is delayed', 30
          );
          warningEnv.document.rows.warnings = [warningOne, warningTwo];
          warningEnv.document.warnings = warningEnv.document.rows.warnings;
          const warningController = makeController(warningEnv.document, warningEnv.windowObject);
          warningController.init();

          warningEnv.document.emit('pointerover', event(warningOne));
          if (
            warningEnv.warningPanel.hidden
            || !elementText(warningEnv.warningPanel).includes('Odoo has not updated recently')
          ) {
            throw new Error('warning pointer preview did not show its summary');
          }
          warningEnv.document.emit('pointerout', {
            target: warningOne, relatedTarget: warningEnv.warningPanel
          });
          if (warningEnv.warningPanel.hidden) {
            throw new Error('warning preview closed while moving into the panel');
          }
          warningEnv.document.emit('pointerout', {target: warningOne, relatedTarget: {}});
          if (!warningEnv.warningPanel.hidden) {
            throw new Error('unpinned warning preview stayed open after pointer exit');
          }
          warningEnv.document.emit('focusin', event(warningOne));
          if (warningEnv.warningPanel.hidden) throw new Error('focus did not preview warning');
          warningEnv.document.emit('focusout', {target: warningOne, relatedTarget: {}});
          if (!warningEnv.warningPanel.hidden) throw new Error('focus exit did not close preview');

          warningEnv.document.emit('click', event(warningOne));
          if (
            warningOne.getAttribute('aria-expanded') !== 'true'
            || warningEnv.warningPanel.getAttribute('aria-busy') !== 'true'
            || warningEnv.requests[0].url
              !== '/people-performance/warnings/odoo%5Csource%22one?day=2026-08-28'
            || warningEnv.requests[0].options.cache !== 'no-store'
          ) {
            throw new Error('pinning a warning did not start an accessible no-store detail load');
          }
          warningEnv.document.emit('click', event(warningTwo));
          if (
            warningOne.getAttribute('aria-expanded') !== 'false'
            || warningTwo.getAttribute('aria-expanded') !== 'true'
            || !warningEnv.aborts[0].signal.aborted
          ) {
            throw new Error('opening another warning did not close and abort the first');
          }
          const freshWarningContent = warningEnv.makeWarningContent('open', 'Fresh tablet detail');
          warningEnv.parsed.warningFresh = freshWarningContent;
          warningEnv.requests[1].pending.resolve(detailResponse('warningFresh'));
          await flush();
          if (
            warningEnv.warningPanel.children[0] !== freshWarningContent
            || warningEnv.warningPanel.getAttribute('aria-busy') !== 'false'
          ) {
            throw new Error('marked warning detail was not adopted into the persistent panel');
          }
          const staleWarningContent = warningEnv.makeWarningContent('open', 'Stale Odoo detail');
          warningEnv.parsed.warningStale = staleWarningContent;
          warningEnv.requests[0].pending.resolve(detailResponse('warningStale'));
          await flush();
          if (warningEnv.warningPanel.children[0] !== freshWarningContent) {
            throw new Error('stale warning detail overwrote the newer warning');
          }

          warningEnv.document.emit('keydown', {key: 'Escape'});
          if (
            !warningEnv.warningPanel.hidden
            || warningTwo.getAttribute('aria-expanded') !== 'false'
            || warningTwo.focusCount !== 1
          ) {
            throw new Error('warning Escape did not close and restore focus');
          }
          warningEnv.document.emit('click', event(warningTwo));
          warningEnv.document.emit('click', event(warningTwo));
          if (!warningEnv.warningPanel.hidden) {
            throw new Error('clicking the active warning did not toggle it closed');
          }
          warningEnv.document.emit('click', event(warningTwo));
          warningEnv.document.emit('pointerdown', event({closest() { return null; }}));
          if (!warningEnv.warningPanel.hidden) throw new Error('outside click did not close warning');
          const focusBeforeWarningClose = warningTwo.focusCount;
          warningEnv.document.emit('click', event(warningTwo));
          warningEnv.document.emit('click', event(warningEnv.makeWarningClose()));
          if (
            !warningEnv.warningPanel.hidden
            || warningTwo.focusCount !== focusBeforeWarningClose + 1
          ) {
            throw new Error('warning close button did not close and restore focus');
          }

          const coarseEnv = makeEnvironment('1', {coarsePointer: true});
          const coarseWarning = coarseEnv.makeWarningTrigger('coarse', 'Touch-only warning');
          coarseEnv.document.rows.warnings = [coarseWarning];
          coarseEnv.document.warnings = coarseEnv.document.rows.warnings;
          const coarseController = makeController(coarseEnv.document, coarseEnv.windowObject);
          coarseController.init();
          coarseEnv.document.emit('pointerover', event(coarseWarning));
          if (!coarseEnv.warningPanel.hidden) {
            throw new Error('coarse pointer hover fetched or previewed warning details');
          }
          coarseEnv.document.emit('focusin', event(coarseWarning));
          if (coarseEnv.warningPanel.hidden) {
            throw new Error('coarse pointer environment lost keyboard warning access');
          }
          coarseController.destroy();

          // Invalid detail responses render only the safe retry state. Retry can
          // recover, and ordinary panel links remain normal navigation.
          for (const invalidKind of ['missing', 'wrong', 'auth', 'redirect']) {
            const invalidEnv = makeEnvironment('1');
            const invalidWarning = invalidEnv.makeWarningTrigger(
              invalidKind, 'Unsafe response test'
            );
            invalidEnv.document.rows.warnings = [invalidWarning];
            invalidEnv.document.warnings = invalidEnv.document.rows.warnings;
            const invalidController = makeController(invalidEnv.document, invalidEnv.windowObject);
            invalidController.init();
            invalidEnv.document.emit('click', event(invalidWarning));
            invalidEnv.parsed['unsafe-' + invalidKind] = invalidEnv.makeWarningContent(
              'open', 'UNSAFE BODY'
            );
            const options = {token: 'unsafe-' + invalidKind};
            if (invalidKind === 'missing') options.header = null;
            if (invalidKind === 'wrong') options.header = 'rows';
            if (invalidKind === 'auth') Object.assign(options, {ok: false, status: 401});
            if (invalidKind === 'redirect') Object.assign(options, {redirected: true});
            invalidEnv.requests[0].pending.resolve(detailResponse(
              'unsafe-' + invalidKind, options
            ));
            await flush();
            const invalidText = elementText(invalidEnv.warningPanel);
            if (
              !invalidText.includes('Details could not be loaded.')
              || invalidText.includes('UNSAFE BODY')
            ) {
              throw new Error(invalidKind + ' warning response was injected or lacked retry');
            }
            const retry = invalidEnv.warningPanel.children[0].children[1];
            invalidEnv.document.emit('click', event(retry));
            if (invalidEnv.requests.length !== 2) throw new Error('Retry did not re-fetch detail');
            const recovered = invalidEnv.makeWarningContent('open', 'Recovered detail');
            invalidEnv.parsed['recovered-' + invalidKind] = recovered;
            invalidEnv.requests[1].pending.resolve(detailResponse('recovered-' + invalidKind));
            await flush();
            if (invalidEnv.warningPanel.children[0] !== recovered) {
              throw new Error('Retry did not adopt recovered warning detail');
            }
            const link = invalidEnv.makeWarningAction('open_diagnostics');
            invalidEnv.warningPanel.children[0].appendChild(link);
            const linkClick = event(link);
            invalidEnv.document.emit('click', linkClick);
            if (linkClick.defaultPrevented || invalidEnv.warningPanel.hidden) {
              throw new Error('ordinary warning action link did not navigate normally');
            }
            invalidController.destroy();
          }

          // Check again is single-flight, refreshes rows first, adopts the
          // replacement trigger, and announces only the requested result.
          const checkEnv = makeEnvironment('1');
          const checkedWarning = checkEnv.makeWarningTrigger('check-key', 'Check this source');
          checkEnv.document.rows.warnings = [checkedWarning];
          checkEnv.document.warnings = checkEnv.document.rows.warnings;
          const checkController = makeController(checkEnv.document, checkEnv.windowObject);
          checkController.init();
          checkedWarning.focus();
          checkEnv.document.emit('click', event(checkedWarning));
          checkEnv.parsed.initialCheck = checkEnv.makeWarningContent('open', 'Initial warning');
          checkEnv.requests[0].pending.resolve(detailResponse('initialCheck'));
          await flush();
          const checkButton = checkEnv.makeWarningAction('check_again');
          checkEnv.document.emit('click', event(checkButton));
          checkEnv.document.emit('click', event(checkButton));
          if (
            checkEnv.requests.length !== 2
            || !checkButton.disabled
            || checkButton.textContent !== 'Checking…'
          ) {
            throw new Error('double Check again was not deduplicated');
          }
          checkEnv.windowObject.scrollY = 432;
          checkEnv.document.axisViewport.scrollLeft = 67;
          const replacementWarning = checkEnv.makeWarningTrigger(
            'check-key', 'Replacement source warning'
          );
          const checkedRows = checkEnv.makeRows(
            '2026-08-28', '1', [], [], [replacementWarning]
          );
          checkEnv.parsed.checkedRows = checkedRows;
          checkEnv.requests[1].pending.resolve(response({token: 'checkedRows'}));
          await flush();
          if (checkEnv.requests.length !== 4 || !checkEnv.aborts[2].signal.aborted) {
            throw new Error('manual check did not supersede background panel restoration');
          }
          const stillOpen = checkEnv.makeWarningContent('open', 'Still active detail');
          checkEnv.parsed.stillOpen = stillOpen;
          checkEnv.requests[3].pending.resolve(detailResponse('stillOpen'));
          await flush();
          if (
            checkEnv.warningPanel.children[0] !== stillOpen
            || replacementWarning.getAttribute('aria-expanded') !== 'true'
            || replacementWarning.focusCount !== 1
            || checkEnv.windowObject.scrollY !== 432
            || checkEnv.document.axisViewport.scrollLeft !== 67
            || checkEnv.actionStatus.textContent !== 'The warning is still active.'
          ) {
            throw new Error('manual warning recheck did not preserve state and announce remains');
          }

          const clearButton = checkEnv.makeWarningAction('check_again');
          checkEnv.document.emit('click', event(clearButton));
          const clearedRows = checkEnv.makeRows('2026-08-28', '1', [], [], []);
          checkEnv.parsed.clearedRows = clearedRows;
          checkEnv.requests[4].pending.resolve(response({token: 'clearedRows'}));
          await flush();
          const cleared = checkEnv.makeWarningContent('cleared', 'Source is healthy');
          checkEnv.parsed.cleared = cleared;
          checkEnv.requests[6].pending.resolve(detailResponse('cleared'));
          await flush();
          if (
            checkEnv.warningPanel.hidden
            || checkEnv.warningPanel.children[0] !== cleared
            || checkEnv.actionStatus.textContent !== 'Issue cleared.'
          ) {
            throw new Error('cleared warning was dismissed locally or not announced');
          }
          checkController.destroy();

          const failedCheckEnv = makeEnvironment('1');
          const failedCheckWarning = failedCheckEnv.makeWarningTrigger('failed-check', 'Check fails');
          failedCheckEnv.document.rows.warnings = [failedCheckWarning];
          failedCheckEnv.document.warnings = failedCheckEnv.document.rows.warnings;
          const failedCheckController = makeController(
            failedCheckEnv.document, failedCheckEnv.windowObject
          );
          failedCheckController.init();
          failedCheckEnv.document.emit('click', event(failedCheckWarning));
          failedCheckEnv.parsed.failedInitial = failedCheckEnv.makeWarningContent('open', 'Open');
          failedCheckEnv.requests[0].pending.resolve(detailResponse('failedInitial'));
          await flush();
          failedCheckEnv.document.emit(
            'click', event(failedCheckEnv.makeWarningAction('check_again'))
          );
          failedCheckEnv.requests[1].pending.resolve(response({ok: false, status: 500}));
          await flush();
          if (
            !elementText(failedCheckEnv.warningPanel).includes('The check could not finish.')
            || failedCheckEnv.actionStatus.textContent !== 'The check could not finish.'
          ) {
            throw new Error('failed manual check did not render and announce its retry state');
          }
          failedCheckController.destroy();

          // A successful row refresh followed by an invalid/failing detail
          // response keeps the warning pinned with Retry UI and announces that
          // the manual check could not finish.
          for (const detailFailureKind of ['http', 'marker']) {
            const detailFailureEnv = makeEnvironment('1');
            const detailFailureWarning = detailFailureEnv.makeWarningTrigger(
              'detail-failure-' + detailFailureKind, 'Detail follow-up fails'
            );
            detailFailureEnv.document.rows.warnings = [detailFailureWarning];
            detailFailureEnv.document.warnings = detailFailureEnv.document.rows.warnings;
            const detailFailureController = makeController(
              detailFailureEnv.document, detailFailureEnv.windowObject
            );
            detailFailureController.init();
            detailFailureEnv.document.emit('click', event(detailFailureWarning));
            const attachedCheckButton = detailFailureEnv.makeWarningAction('check_again');
            detailFailureEnv.parsed['detailFailureInitial-' + detailFailureKind]
              = detailFailureEnv.makeWarningContent(
                'open', 'Initial open detail', [attachedCheckButton]
              );
            detailFailureEnv.requests[0].pending.resolve(
              detailResponse('detailFailureInitial-' + detailFailureKind)
            );
            await flush();
            attachedCheckButton.focus();
            const failedDetailCheck = detailFailureEnv.document.emit(
              'click', event(attachedCheckButton)
            )[0];
            const detailFailureReplacement = detailFailureEnv.makeWarningTrigger(
              'detail-failure-' + detailFailureKind, 'Replacement warning'
            );
            detailFailureEnv.parsed['detailFailureRows-' + detailFailureKind]
              = detailFailureEnv.makeRows(
                '2026-08-28', '1', [], [], [detailFailureReplacement]
              );
            detailFailureEnv.requests[1].pending.resolve(
              response({token: 'detailFailureRows-' + detailFailureKind})
            );
            await flush();
            detailFailureEnv.requests[3].pending.resolve(
              detailFailureKind === 'http'
                ? detailResponse('detailHttpFailure', {ok: false, status: 500})
                : detailResponse('detailMarkerFailure', {header: 'rows'})
            );
            if (await failedDetailCheck !== false) {
              throw new Error('failed detail follow-up reported manual check success');
            }
            if (
              detailFailureEnv.warningPanel.hidden
              || detailFailureReplacement.getAttribute('aria-expanded') !== 'true'
              || !elementText(detailFailureEnv.warningPanel).includes(
                'Details could not be loaded.'
              )
              || detailFailureEnv.actionStatus.textContent !== 'The check could not finish.'
            ) {
              throw new Error(
                detailFailureKind + ' detail failure was not retained and announced'
              );
            }
            detailFailureController.destroy();
          }

          // A check started for one warning must not overwrite a newer pinned
          // choice after its row refresh eventually completes.
          const staleCheckEnv = makeEnvironment('1');
          const staleCheckOne = staleCheckEnv.makeWarningTrigger('stale-one', 'First warning');
          const staleCheckTwo = staleCheckEnv.makeWarningTrigger('stale-two', 'Second warning');
          staleCheckEnv.document.rows.warnings = [staleCheckOne, staleCheckTwo];
          staleCheckEnv.document.warnings = staleCheckEnv.document.rows.warnings;
          const staleCheckController = makeController(
            staleCheckEnv.document, staleCheckEnv.windowObject
          );
          staleCheckController.init();
          staleCheckEnv.document.emit('click', event(staleCheckOne));
          staleCheckEnv.parsed.staleCheckInitial = staleCheckEnv.makeWarningContent(
            'open', 'First detail'
          );
          staleCheckEnv.requests[0].pending.resolve(detailResponse('staleCheckInitial'));
          await flush();
          staleCheckEnv.document.emit(
            'click', event(staleCheckEnv.makeWarningAction('check_again'))
          );
          staleCheckEnv.document.emit('click', event(staleCheckTwo));
          staleCheckEnv.parsed.newChoice = staleCheckEnv.makeWarningContent(
            'open', 'Second detail'
          );
          staleCheckEnv.requests[2].pending.resolve(detailResponse('newChoice'));
          await flush();
          const staleReplacementOne = staleCheckEnv.makeWarningTrigger(
            'stale-one', 'First replacement'
          );
          const staleReplacementTwo = staleCheckEnv.makeWarningTrigger(
            'stale-two', 'Second replacement'
          );
          staleCheckEnv.parsed.staleCheckRows = staleCheckEnv.makeRows(
            '2026-08-28', '1', [], [], [staleReplacementOne, staleReplacementTwo]
          );
          staleCheckEnv.requests[1].pending.resolve(response({token: 'staleCheckRows'}));
          await flush();
          if (
            staleCheckEnv.requests.length !== 4
            || staleReplacementOne.getAttribute('aria-expanded') !== 'false'
            || staleReplacementTwo.getAttribute('aria-expanded') !== 'true'
          ) {
            throw new Error('stale manual check overwrote the newer pinned warning');
          }
          staleCheckEnv.parsed.restoredChoice = staleCheckEnv.makeWarningContent(
            'open', 'Restored second detail'
          );
          staleCheckEnv.requests[3].pending.resolve(detailResponse('restoredChoice'));
          await flush();

          staleCheckEnv.document.emit(
            'click', event(staleCheckEnv.makeWarningAction('check_again'))
          );
          staleCheckEnv.document.emit('click', event(staleReplacementOne));
          const newestChoice = staleCheckEnv.makeWarningContent('open', 'Newest first detail');
          staleCheckEnv.parsed.newestChoice = newestChoice;
          staleCheckEnv.requests[5].pending.resolve(detailResponse('newestChoice'));
          await flush();
          staleCheckEnv.requests[4].pending.resolve(response({ok: false, status: 500}));
          await flush();
          if (
            staleCheckEnv.warningPanel.children[0] !== newestChoice
            || staleCheckEnv.actionStatus.textContent
          ) {
            throw new Error('stale manual check failure overwrote the newer warning');
          }
          staleCheckController.destroy();

          const destroyedCheckEnv = makeEnvironment('1');
          const destroyedCheckWarning = destroyedCheckEnv.makeWarningTrigger(
            'destroyed-check', 'Destroyed check'
          );
          destroyedCheckEnv.document.rows.warnings = [destroyedCheckWarning];
          destroyedCheckEnv.document.warnings = destroyedCheckEnv.document.rows.warnings;
          const destroyedCheckController = makeController(
            destroyedCheckEnv.document, destroyedCheckEnv.windowObject
          );
          destroyedCheckController.init();
          destroyedCheckEnv.document.emit('click', event(destroyedCheckWarning));
          destroyedCheckEnv.parsed.destroyedCheckInitial = destroyedCheckEnv.makeWarningContent(
            'open', 'Open detail'
          );
          destroyedCheckEnv.requests[0].pending.resolve(detailResponse('destroyedCheckInitial'));
          await flush();
          destroyedCheckEnv.document.emit(
            'click', event(destroyedCheckEnv.makeWarningAction('check_again'))
          );
          destroyedCheckController.destroy();
          destroyedCheckEnv.requests[1].pending.resolve(response({ok: false, status: 500}));
          await flush();
          if (!destroyedCheckEnv.warningPanel.hidden) {
            throw new Error('a Check again result reopened its panel after destroy');
          }

          // A timer poll silently restores a pinned warning against the new
          // trigger, including focus and scroll, then reloads marked detail.
          const warningPollEnv = makeEnvironment('1');
          const oldPollWarning = warningPollEnv.makeWarningTrigger('poll-warning', 'Old warning');
          warningPollEnv.document.rows.warnings = [oldPollWarning];
          warningPollEnv.document.warnings = warningPollEnv.document.rows.warnings;
          const warningPollController = makeController(
            warningPollEnv.document, warningPollEnv.windowObject
          );
          warningPollController.init();
          oldPollWarning.focus();
          warningPollEnv.document.emit('click', event(oldPollWarning));
          warningPollEnv.parsed.pollInitial = warningPollEnv.makeWarningContent('open', 'Old detail');
          warningPollEnv.requests[0].pending.resolve(detailResponse('pollInitial'));
          await flush();
          warningPollEnv.windowObject.scrollY = 701;
          warningPollEnv.document.axisViewport.scrollLeft = 71;
          const newPollWarning = warningPollEnv.makeWarningTrigger('poll-warning', 'New warning');
          const timerPoll = warningPollEnv.timers[0].callback();
          warningPollEnv.parsed.pollRows = warningPollEnv.makeRows(
            '2026-08-28', '1', [], [], [newPollWarning]
          );
          warningPollEnv.requests[1].pending.resolve(response({token: 'pollRows'}));
          if (await timerPoll !== true) throw new Error('warning timer poll did not refresh rows');
          if (
            warningPollEnv.requests.length !== 3
            || warningPollEnv.warningPanel.hidden
            || newPollWarning.getAttribute('aria-expanded') !== 'true'
            || newPollWarning.focusCount !== 1
            || warningPollEnv.windowObject.scrollY !== 701
            || warningPollEnv.document.axisViewport.scrollLeft !== 71
          ) {
            throw new Error('timer poll did not restore pinned warning focus and scroll');
          }
          warningPollEnv.parsed.pollDetail = warningPollEnv.makeWarningContent(
            'open', 'Fresh poll detail'
          );
          warningPollEnv.requests[2].pending.resolve(detailResponse('pollDetail'));
          await flush();
          if (warningPollEnv.actionStatus.textContent) {
            throw new Error('background warning poll announced an unchanged result');
          }

          // If polling removes the focused warning trigger, the truthful
          // cleared panel receives focus at its close control. Escape and the
          // close control remain safe even though the old trigger is gone.
          const clearedFocusEnv = makeEnvironment('1');
          const clearedFocusWarning = clearedFocusEnv.makeWarningTrigger(
            'cleared-focus-warning', 'Warning that clears'
          );
          clearedFocusEnv.document.rows.warnings = [clearedFocusWarning];
          clearedFocusEnv.document.warnings = clearedFocusEnv.document.rows.warnings;
          const clearedFocusController = makeController(
            clearedFocusEnv.document, clearedFocusEnv.windowObject
          );
          clearedFocusController.init();
          clearedFocusWarning.focus();
          clearedFocusEnv.document.emit('click', event(clearedFocusWarning));
          clearedFocusEnv.parsed.clearedFocusInitial
            = clearedFocusEnv.makeWarningContent('open', 'Open detail');
          clearedFocusEnv.requests[0].pending.resolve(
            detailResponse('clearedFocusInitial')
          );
          await flush();
          const clearedFocusPoll = clearedFocusEnv.timers[0].callback();
          clearedFocusEnv.parsed.clearedFocusRows = clearedFocusEnv.makeRows(
            '2026-08-28', '1', [], [], []
          );
          clearedFocusEnv.requests[1].pending.resolve(
            response({token: 'clearedFocusRows'})
          );
          if (await clearedFocusPoll !== true) {
            throw new Error('focused warning clear poll did not refresh rows');
          }
          const clearedFocusClose = clearedFocusEnv.makeWarningClose();
          clearedFocusEnv.parsed.clearedFocusDetail
            = clearedFocusEnv.makeWarningContent(
              'cleared', 'Source is healthy', [clearedFocusClose]
            );
          clearedFocusEnv.requests[2].pending.resolve(
            detailResponse('clearedFocusDetail')
          );
          await flush();
          if (
            clearedFocusEnv.warningPanel.hidden
            || !clearedFocusClose.isConnected
            || clearedFocusEnv.document.activeElement !== clearedFocusClose
            || clearedFocusClose.focusCount !== 1
          ) {
            throw new Error('cleared panel did not receive focus after its trigger disappeared');
          }
          clearedFocusEnv.document.emit('keydown', {key: 'Escape'});
          if (!clearedFocusEnv.warningPanel.hidden) {
            throw new Error('Escape was unsafe after the cleared warning trigger disappeared');
          }
          clearedFocusEnv.document.emit('click', event(clearedFocusClose));
          if (!clearedFocusEnv.warningPanel.hidden) {
            throw new Error('close was unsafe after the cleared warning trigger disappeared');
          }
          clearedFocusController.destroy();

          // Panel controls are real attached focus targets. Detail replacement
          // restores the same control identity, or the new close control when
          // the prior action no longer exists.
          for (const panelFocusKind of [
            'check_again', 'retry', 'close', 'open_diagnostics', 'removed_action'
          ]) {
            const panelFocusEnv = makeEnvironment('1');
            const panelFocusWarning = panelFocusEnv.makeWarningTrigger(
              'panel-focus-' + panelFocusKind, 'Panel focus warning'
            );
            panelFocusEnv.document.rows.warnings = [panelFocusWarning];
            panelFocusEnv.document.warnings = panelFocusEnv.document.rows.warnings;
            const panelFocusController = makeController(
              panelFocusEnv.document, panelFocusEnv.windowObject
            );
            panelFocusController.init();
            panelFocusEnv.document.emit('click', event(panelFocusWarning));
            const oldPanelControl = panelFocusKind === 'close'
              ? panelFocusEnv.makeWarningClose()
              : panelFocusKind === 'open_diagnostics'
                ? panelFocusEnv.makeWarningLink(panelFocusKind)
                : panelFocusEnv.makeWarningAction(panelFocusKind);
            panelFocusEnv.parsed['panelFocusInitial-' + panelFocusKind]
              = panelFocusEnv.makeWarningContent(
                'open', 'Initial panel detail', [oldPanelControl]
              );
            panelFocusEnv.requests[0].pending.resolve(
              detailResponse('panelFocusInitial-' + panelFocusKind)
            );
            await flush();
            if (!oldPanelControl.isConnected || !panelFocusEnv.warningPanel.contains(oldPanelControl)) {
              throw new Error('panel focus test control was not attached to the live panel');
            }
            oldPanelControl.focus();
            const panelFocusRefresh = panelFocusController.refreshRows();
            const replacementPanelWarning = panelFocusEnv.makeWarningTrigger(
              'panel-focus-' + panelFocusKind, 'Replacement panel focus warning'
            );
            panelFocusEnv.parsed['panelFocusRows-' + panelFocusKind]
              = panelFocusEnv.makeRows(
                '2026-08-28', '1', [], [], [replacementPanelWarning]
              );
            panelFocusEnv.requests[1].pending.resolve(
              response({token: 'panelFocusRows-' + panelFocusKind})
            );
            if (await panelFocusRefresh !== true) {
              throw new Error('panel focus row refresh failed');
            }
            const newPanelControl = panelFocusKind === 'close' || panelFocusKind === 'removed_action'
              ? panelFocusEnv.makeWarningClose()
              : panelFocusKind === 'open_diagnostics'
                ? panelFocusEnv.makeWarningLink(panelFocusKind)
                : panelFocusEnv.makeWarningAction(panelFocusKind);
            panelFocusEnv.parsed['panelFocusDetail-' + panelFocusKind]
              = panelFocusEnv.makeWarningContent(
                'open', 'Replacement panel detail', [newPanelControl]
              );
            panelFocusEnv.requests[2].pending.resolve(
              detailResponse('panelFocusDetail-' + panelFocusKind)
            );
            await flush();
            if (
              oldPanelControl.isConnected
              || !newPanelControl.isConnected
              || panelFocusEnv.document.activeElement !== newPanelControl
              || newPanelControl.focusCount !== 1
              || !panelFocusEnv.focusOptions.some(
                (value) => value && value.preventScroll === true
              )
            ) {
              throw new Error(panelFocusKind + ' focus was not restored after panel replacement');
            }
            panelFocusController.destroy();
          }

          const supersedeEnv = makeEnvironment('1');
          const supersedeWarning = supersedeEnv.makeWarningTrigger(
            'supersede-warning', 'Supersede old poll'
          );
          supersedeEnv.document.rows.warnings = [supersedeWarning];
          supersedeEnv.document.warnings = supersedeEnv.document.rows.warnings;
          const supersedeController = makeController(
            supersedeEnv.document, supersedeEnv.windowObject
          );
          supersedeController.init();
          supersedeEnv.document.emit('click', event(supersedeWarning));
          supersedeEnv.parsed.supersedeInitial = supersedeEnv.makeWarningContent(
            'open', 'Initial detail'
          );
          supersedeEnv.requests[0].pending.resolve(detailResponse('supersedeInitial'));
          await flush();
          supersedeEnv.timers[0].callback();
          supersedeEnv.document.emit(
            'click', event(supersedeEnv.makeWarningAction('check_again'))
          );
          if (
            supersedeEnv.requests.length !== 3
            || !supersedeEnv.aborts[1].signal.aborted
          ) {
            throw new Error('manual Check again did not supersede the older row poll');
          }
          supersedeController.destroy();

          // Background timer/visibility refreshes share an in-flight manual
          // Check again instead of aborting it and producing a false failure.
          const deferBackgroundEnv = makeEnvironment('1');
          const deferBackgroundWarning = deferBackgroundEnv.makeWarningTrigger(
            'defer-background', 'Manual check owns this refresh'
          );
          deferBackgroundEnv.document.rows.warnings = [deferBackgroundWarning];
          deferBackgroundEnv.document.warnings = deferBackgroundEnv.document.rows.warnings;
          const deferBackgroundController = makeController(
            deferBackgroundEnv.document, deferBackgroundEnv.windowObject
          );
          deferBackgroundController.init();
          deferBackgroundEnv.document.emit('click', event(deferBackgroundWarning));
          deferBackgroundEnv.parsed.deferInitial = deferBackgroundEnv.makeWarningContent(
            'open', 'Initial detail'
          );
          deferBackgroundEnv.requests[0].pending.resolve(detailResponse('deferInitial'));
          await flush();
          const manualCheckPromise = deferBackgroundEnv.document.emit(
            'click', event(deferBackgroundEnv.makeWarningAction('check_again'))
          )[0];
          const timerDuringManual = deferBackgroundEnv.timers[0].callback();
          const visibilityDuringManual = deferBackgroundEnv.document.emit(
            'visibilitychange', {}
          )[0];
          if (
            deferBackgroundEnv.requests.length !== 2
            || deferBackgroundEnv.aborts[1].signal.aborted
            || timerDuringManual !== manualCheckPromise
            || visibilityDuringManual !== manualCheckPromise
          ) {
            throw new Error('background refresh superseded an in-flight manual check');
          }
          const deferReplacement = deferBackgroundEnv.makeWarningTrigger(
            'defer-background', 'Replacement warning'
          );
          deferBackgroundEnv.parsed.deferRows = deferBackgroundEnv.makeRows(
            '2026-08-28', '1', [], [], [deferReplacement]
          );
          deferBackgroundEnv.requests[1].pending.resolve(response({token: 'deferRows'}));
          await flush();
          deferBackgroundEnv.parsed.deferDetail = deferBackgroundEnv.makeWarningContent(
            'open', 'Still active after shared refresh'
          );
          deferBackgroundEnv.requests[3].pending.resolve(detailResponse('deferDetail'));
          if (
            await manualCheckPromise !== true
            || await timerDuringManual !== true
            || await visibilityDuringManual !== true
          ) {
            throw new Error('shared manual/background refresh did not finish together');
          }
          deferBackgroundController.destroy();

          // Destroy aborts simultaneous independent row and warning requests,
          // but retains the server-owned warning panel host.
          const warningDestroyEnv = makeEnvironment('1');
          const destroyWarning = warningDestroyEnv.makeWarningTrigger('destroy-warning', 'Destroy');
          warningDestroyEnv.document.rows.warnings = [destroyWarning];
          warningDestroyEnv.document.warnings = warningDestroyEnv.document.rows.warnings;
          const warningDestroyController = makeController(
            warningDestroyEnv.document, warningDestroyEnv.windowObject
          );
          warningDestroyController.init();
          warningDestroyController.refreshRows();
          warningDestroyEnv.document.emit('click', event(destroyWarning));
          warningDestroyController.destroy();
          if (
            !warningDestroyEnv.aborts[0].signal.aborted
            || !warningDestroyEnv.aborts[1].signal.aborted
            || warningDestroyEnv.warningPanel.removed
          ) {
            throw new Error('destroy did not abort both requests or removed the server warning host');
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

          // Manager controls live inside the replaced partial. In-progress
          // values and focus must survive a refresh without moving the page.
          for (const kind of ['date', 'checkbox']) {
            const controlEnv = makeEnvironment('1');
            const oldControl = kind === 'date'
              ? controlEnv.dateControl : controlEnv.attentionControl;
            const controlController = makeController(
              controlEnv.document, controlEnv.windowObject
            );
            controlController.init();
            const controlPromise = controlController.refreshRows();
            oldControl.focus();
            if (kind === 'date') oldControl.value = '2026-08-29';
            else oldControl.checked = true;
            controlEnv.windowObject.scrollY = 654;
            const newControl = controlEnv.makeControl(
              kind === 'date' ? 'day' : 'attention',
              kind,
              kind === 'date' ? '2026-08-28' : '1',
              false
            );
            controlEnv.parsed['control-' + kind] = controlEnv.makeRows(
              '2026-08-28', '1', [], [newControl]
            );
            controlEnv.requests[0].pending.resolve(response({token: 'control-' + kind}));
            await controlPromise;
            if (newControl.focusCount !== 1) {
              throw new Error(kind + ' control focus was not restored');
            }
            if (!controlEnv.focusOptions.some((value) => value && value.preventScroll === true)) {
              throw new Error(kind + ' control focus was restored without preventScroll');
            }
            if (kind === 'date' && newControl.value !== '2026-08-29') {
              throw new Error('in-progress date value was discarded');
            }
            if (kind === 'checkbox' && newControl.checked !== true) {
              throw new Error('in-progress checkbox value was discarded');
            }
            if (controlEnv.windowObject.scrollY !== 654) {
              throw new Error(kind + ' control restoration moved the page');
            }
            controlController.destroy();
          }

          const todayEnv = makeEnvironment('1');
          const todayLink = todayEnv.makeControl('today', 'link');
          todayEnv.document.controls = [todayLink];
          todayEnv.document.rows.controls = [todayLink];
          const todayController = makeController(todayEnv.document, todayEnv.windowObject);
          todayController.init();
          const todayPromise = todayController.refreshRows();
          todayLink.focus();
          const refreshedTodayLink = todayEnv.makeControl('today', 'link');
          todayEnv.parsed.todayControl = todayEnv.makeRows(
            '2026-08-28', '1', [], [refreshedTodayLink]
          );
          todayEnv.requests[0].pending.resolve(response({token: 'todayControl'}));
          await todayPromise;
          if (refreshedTodayLink.focusCount !== 1) {
            throw new Error('Today link focus was not restored');
          }
          todayController.destroy();

          const tornDownControlEnv = makeEnvironment('1');
          const tornDownController = makeController(
            tornDownControlEnv.document, tornDownControlEnv.windowObject
          );
          tornDownController.init();
          const tornDownPromise = tornDownController.refreshRows();
          tornDownControlEnv.dateControl.focus();
          const forbiddenControl = tornDownControlEnv.makeControl(
            'day', 'date', '2026-08-28'
          );
          tornDownControlEnv.parsed.tornDown = tornDownControlEnv.makeRows(
            '2026-08-28', '1', [], [forbiddenControl]
          );
          tornDownController.destroy();
          tornDownControlEnv.requests[0].pending.resolve(response({token: 'tornDown'}));
          if (await tornDownPromise !== false || forbiddenControl.focusCount !== 0) {
            throw new Error('destroyed refresh restored a detached manager control');
          }

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
