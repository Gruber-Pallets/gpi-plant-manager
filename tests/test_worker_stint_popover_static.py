from pathlib import Path
import json
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "src/zira_dashboard/static/recycling.css").read_text(encoding="utf-8")
SCRIPT = ROOT / "src/zira_dashboard/static/worker-stint-popover.js"


def test_worker_stint_styles_keep_the_normal_bar_height_and_remove_detail_rows():
    assert ".bar-track { height: 20px" in CSS
    assert ".worker-stint-hitarea" in CSS
    assert ".vworker-stint-hitarea" in CSS
    assert ".worker-segment-labels" not in CSS
    assert ".vworker-segment-list" not in CSS


def test_segment_totals_do_not_block_stint_hitareas():
    assert re.search(r"\.segment-total\s*\{[^}]*pointer-events:\s*none;", CSS)
    assert re.search(r"\.vsegment-total\s*\{[^}]*pointer-events:\s*none;", CSS)


def test_vertical_goal_markers_do_not_block_stint_hitareas():
    assert re.search(
        r"\.vworker-segment-goal\s*\{[^}]*pointer-events:\s*none;", CSS
    )


def test_vertical_stint_focus_ring_is_inset_while_horizontal_stays_outset():
    assert re.search(
        r"\.worker-stint-hitarea:focus-visible\s*\{[^}]*"
        r"outline-offset:\s*2px;",
        CSS,
    )
    assert re.search(
        r"\.vworker-stint-hitarea:focus-visible\s*\{[^}]*"
        r"outline-offset:\s*-2px;",
        CSS,
    )


def test_zero_runway_hitareas_have_small_horizontal_and_vertical_targets():
    assert re.search(
        r"\.worker-stint-hitarea\.zero-runway\s*\{[^}]*width:\s*12px;", CSS
    )
    assert re.search(
        r"\.vworker-stint-hitarea\.zero-runway\s*\{[^}]*height:\s*12px;", CSS
    )


def test_worker_stint_popover_supports_hover_focus_tap_outside_and_escape():
    harness = f"""
const makeController = require({json.dumps(str(SCRIPT))});
const documentListeners = {{}};
const windowListeners = {{}};
let appended = null;
let focusCount = 0;
const popover = {{
  hidden: true,
  style: {{}},
  textContent: '',
  id: '',
  className: '',
  setAttribute(name, value) {{ this[name] = value; }},
  contains(node) {{ return node === this; }},
  getBoundingClientRect() {{ return {{width: 180, height: 30}}; }},
}};
const document = {{
  body: {{appendChild(node) {{ appended = node; }}}},
  createElement() {{ return popover; }},
  addEventListener(type, callback) {{ documentListeners[type] = callback; }},
}};
const windowObject = {{
  innerWidth: 800,
  innerHeight: 600,
  scrollX: 0,
  scrollY: 0,
  addEventListener(type, callback) {{ windowListeners[type] = callback; }},
}};
function trigger(detail, left) {{
  const attributes = {{}};
  return {{
    dataset: {{stintDetail: detail}},
    closest(selector) {{ return selector.includes('worker-stint') ? this : null; }},
    contains(node) {{ return node === this; }},
    getBoundingClientRect() {{ return {{left, right: left + 80, top: 100, bottom: 120, width: 80}}; }},
    setAttribute(name, value) {{ attributes[name] = value; }},
    removeAttribute(name) {{ delete attributes[name]; }},
    getAttribute(name) {{ return attributes[name]; }},
    focus() {{ focusCount += 1; }},
  }};
}}
const first = trigger('Humberto S. · 7a-2:33p · 516/700 · 184 behind', 100);
const second = trigger('Ana M. · since 2:35p · 32/25 · 7 ahead', 180);
const outside = {{closest() {{ return null; }}}};
const event = (target) => ({{target, preventDefault() {{}}}});
const controller = makeController(document, windowObject);
controller.init();

documentListeners.pointerover(event(first));
if (popover.hidden || popover.textContent !== first.dataset.stintDetail) throw new Error('hover did not open');
if (first.getAttribute('aria-describedby') !== 'worker-stint-popover') throw new Error('missing description link');
documentListeners.keydown({{key: 'Escape'}});
if (!popover.hidden || focusCount !== 0) throw new Error('hover Escape moved focus');

documentListeners.pointerover(event(first));
documentListeners.pointerout({{target: first, relatedTarget: outside}});
if (!popover.hidden) throw new Error('hover leave did not close');

documentListeners.focusin(event(first));
if (popover.hidden) throw new Error('focus did not open');
documentListeners.focusout({{target: first, relatedTarget: outside}});
if (!popover.hidden) throw new Error('focus leave did not close');

documentListeners.click(event(first));
documentListeners.pointerout({{target: first, relatedTarget: outside}});
if (popover.hidden) throw new Error('tap did not pin');
documentListeners.click(event(second));
if (popover.textContent !== second.dataset.stintDetail) throw new Error('second tap did not replace details');
documentListeners.pointerdown(event(outside));
if (!popover.hidden) throw new Error('outside tap did not close');

documentListeners.click(event(first));
documentListeners.keydown({{key: 'Escape'}});
if (!popover.hidden || focusCount !== 1) throw new Error('pinned Escape did not restore focus once');
if (!appended || appended.id !== 'worker-stint-popover') throw new Error('shared popover was not created');
"""
    result = subprocess.run(
        ["node", "--eval", harness],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
