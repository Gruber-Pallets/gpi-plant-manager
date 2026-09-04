import json
import subprocess
from pathlib import Path


JS = Path("src/zira_dashboard/static/exceptions.js")


def _js():
    return JS.read_text(encoding="utf-8")


def _run_attendance_hook(expression):
    source = json.dumps(_js())
    script = f"""
global.window = {{
  setInterval: function () {{}},
  setTimeout: function () {{}},
  clearTimeout: function () {{}},
  location: {{reload: function () {{}}}},
}};
global.document = {{
  hidden: false,
  querySelector: function () {{ return null; }},
  querySelectorAll: function () {{ return []; }},
  addEventListener: function () {{}},
}};
global.sessionStorage = {{getItem: function () {{ return null; }}, setItem: function () {{}}}};
eval({source});
Promise.resolve({expression}).then(function (result) {{
  process.stdout.write(JSON.stringify(result));
}});
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_test_work_center_dismiss_click_contract_is_retry_safe():
    source = json.dumps(_js())
    harness = f"""
global.sessionStorage = {{getItem: function () {{ return null; }}, setItem: function () {{}}}};
global.document = {{
  hidden: false,
  querySelector: function () {{ return null; }},
  querySelectorAll: function () {{ return []; }},
  addEventListener: function () {{}},
  createElement: function () {{ throw new Error('dismiss must not create Undo'); }},
}};
global.window = {{
  setInterval: function () {{}},
  location: {{reload: function () {{}}}},
}};
global.setTimeout = function (fn) {{ fn(); return 1; }};
global.clearTimeout = function () {{}};
eval({source});

function makeRow(itemKey) {{
  var state = {{removed: false, error: false}};
  var control = {{disabled: false}};
  var status = {{hidden: true, textContent: ''}};
  var row = {{
    dataset: {{itemKey: itemKey, priority: 'urgent'}},
    classList: {{
      add: function (name) {{ if (name === 'is-resolved') state.resolved = true; }},
      toggle: function (name, enabled) {{ if (name === 'is-error') state.error = enabled; }},
    }},
    querySelectorAll: function () {{ return [control]; }},
    querySelector: function (selector) {{ return selector === '.row-status' ? status : null; }},
    remove: function () {{ state.removed = true; }},
  }};
  return {{row: row, state: state, control: control, status: status}};
}}

async function run(response, rejects) {{
  var fixture = makeRow('attendance-unmapped:test-workcenter');
  var request = null;
  window.gpiFetch = function (url, options) {{
    request = {{url: url, body: JSON.parse(options.body)}};
    if (rejects) return Promise.reject(new Error('offline'));
    return Promise.resolve({{json: function () {{ return Promise.resolve(response); }}}});
  }};
  await window.gpiExceptionInbox.dismissTestWorkCenter(fixture.row);
  return {{
    request: request,
    removed: fixture.state.removed,
    resolved: !!fixture.state.resolved,
    error: fixture.state.error,
    disabled: fixture.control.disabled,
    status: fixture.status.textContent,
  }};
}}

(async function () {{
  var success = await run({{ok: true}}, false);
  var refused = await run({{ok: false, error: 'No longer open.'}}, false);
  var rejected = await run(null, true);
  process.stdout.write(JSON.stringify({{success: success, refused: refused, rejected: rejected}}));
}})().catch(function (error) {{
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
}});
"""

    result = subprocess.run(
        ["node", "--eval", harness], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    states = json.loads(result.stdout)
    assert states["success"]["request"] == {
        "url": "/api/exceptions/attendance-unmapped-location/dismiss",
        "body": {"item_key": "attendance-unmapped:test-workcenter"},
    }
    assert states["success"] | {"request": None} == {
        "request": None,
        "removed": True,
        "resolved": True,
        "error": False,
        "disabled": True,
        "status": "Dismissed",
    }
    assert states["refused"]["removed"] is False
    assert states["refused"]["resolved"] is False
    assert states["refused"]["error"] is True
    assert states["refused"]["disabled"] is False
    assert states["refused"]["status"] == "No longer open."
    assert states["rejected"]["removed"] is False
    assert states["rejected"]["resolved"] is False
    assert states["rejected"]["error"] is True
    assert states["rejected"]["disabled"] is False
    assert states["rejected"]["status"] == "Network error."


def test_correction_javascript_posts_canonical_choices_and_token_only_apply():
    js = _js()

    assert "function attendanceCorrectionPayload()" in js
    assert "employee_odoo_ids:" in js
    assert "work_center_name:" in js
    assert "start_utc:" in js
    assert "end_utc:" in js
    assert "'/api/exceptions/attendance-correction/preview'" in js
    assert "'/api/exceptions/attendance-correction/apply'" in js
    assert "{preview_token: correctionPreviewToken}" in js
    assert "operations: correction" not in js


def test_apply_stays_disabled_until_preview_or_refreshed_confirmation():
    js = _js()

    assert "function invalidateAttendancePreview()" in js
    assert "setAttendanceApplyEnabled(false)" in js
    assert "setAttendanceApplyEnabled(true)" in js
    assert "resp.code === 'source_changed'" in js
    assert "correctionPreviewToken = resp.preview_token" in js
    assert "showAttendanceRefreshConfirmation()" in js
    assert "attendanceCorrectionForm.reset" not in js
    assert "Review the refreshed preview" in js


def test_incomplete_preview_never_arms_apply_even_if_a_server_regresses():
    result = _run_attendance_hook(
        "[null, {}, {employees: []}, {employees: [{intervals_truncated: true}]}, {employees: [{intervals_truncated: false}]}].map(window.gpiAttendanceCorrection.previewIsComplete)"
    )

    assert result == [False, False, False, False, True]
    js = _js()
    assert "attendancePreviewIsComplete(resp.preview)" in js
    assert "The full attendance plan could not be shown" in js


def test_preview_renders_safe_local_intervals_and_still_working_label():
    js = _js()

    assert "function renderAttendancePreview(preview)" in js
    assert "interval.end_label || 'Still working'" in js
    assert "interval.start_label" in js
    assert "source_intervals" in js
    assert "before_intervals" in js
    assert "after_intervals" in js
    assert "operation_summary" in js
    assert ".textContent" in js


def test_datetime_inputs_convert_with_the_plant_timezone_not_browser_timezone():
    js = _js()

    assert "attendanceCorrectionDialog.dataset.plantTimezone" in js
    assert "function plantAttendanceParts(value, timezone)" in js
    assert "Intl.DateTimeFormat('en-US'" in js
    assert "timeZone: attendancePlantTimezone()" in js
    assert "function plantAttendanceUtcValue(input)" in js
    assert "value.getHours()" not in js


def test_clock_change_resolution_rejects_gap_and_requires_both_fall_choices():
    result = _run_attendance_hook(
        "({gap: window.gpiAttendanceCorrection.localTimeResolution('2026-03-08T02:30', 'America/Chicago', null), fall: window.gpiAttendanceCorrection.localTimeCandidates('2026-11-01T01:30', 'America/Chicago'), first: window.gpiAttendanceCorrection.localTimeResolution('2026-11-01T01:30', 'America/Chicago', '2026-11-01T06:30:00.000Z'), second: window.gpiAttendanceCorrection.localTimeResolution('2026-11-01T01:30', 'America/Chicago', '2026-11-01T07:30:00.000Z')})"
    )

    assert result["gap"]["status"] == "nonexistent"
    assert result["fall"] == [
        "2026-11-01T06:30:00.000Z",
        "2026-11-01T07:30:00.000Z",
    ]
    assert result["first"] == {
        "status": "resolved",
        "value": "2026-11-01T06:30:00.000Z",
    }
    assert result["second"] == {
        "status": "resolved",
        "value": "2026-11-01T07:30:00.000Z",
    }

    js = _js()
    assert "Choose the first or second start time." in js
    assert "This start time does not exist because the clock changes." in js


def test_each_dialog_open_clears_an_old_clock_occurrence_choice():
    js = _js()

    assert "function clearAttendanceOccurrenceChoices()" in js
    assert js.index("clearAttendanceOccurrenceChoices();") < js.index(
        "refreshAttendanceOccurrenceChoices();", js.index("function openAttendanceCorrection")
    )


def test_job_polling_is_retry_safe_and_never_advances_the_worker():
    js = _js()

    assert "function pollAttendanceCorrectionJob(jobId, epoch)" in js
    assert "'/api/exceptions/attendance-correction/' + encodeURIComponent(jobId)" in js
    assert "resp.poll_after_ms" in js
    assert "pollAttendanceCorrectionJob(jobId, epoch)" in js
    assert "process_job" not in js
    assert "resp.status === 'complete'" in js
    assert "resp.retryable" in js


def test_job_polling_retries_only_network_and_503_responses():
    decisions = _run_attendance_hook(
        "[503, 401, 403, 404, 500].map(function (status) { return window.gpiAttendanceCorrection.pollDecision(status, false, true); }).concat([window.gpiAttendanceCorrection.pollDecision(503, false, false), window.gpiAttendanceCorrection.pollDecision(200, true, false)])"
    )

    assert [item["retry"] for item in decisions] == [
        True,
        False,
        False,
        False,
        False,
        False,
        False,
    ]
    assert "sign in" in decisions[1]["message"].lower()
    assert "sign in" in decisions[2]["message"].lower()
    assert "refresh" in decisions[3]["message"].lower()
    assert "valid status" in decisions[5]["message"].lower()
    assert "valid status" in decisions[6]["message"].lower()

    js = _js()
    poll_body = js[
        js.index("function pollAttendanceCorrectionJob") : js.index(
            "function applyAttendanceCorrection"
        )
    ]
    assert "var decision = attendancePollDecision" in js
    assert "if (decision.retry)" in js
    assert "Connection lost. Checking again safely" in js
    assert ".catch(" not in poll_body
    assert "attendancePollNetworkFailure" in poll_body


def test_job_poll_timer_is_not_scheduled_for_terminal_or_invalid_responses():
    timers = _run_attendance_hook("""
(async function () {
  async function scheduled(fetcher) {
    var count = 0;
    global.setTimeout = function () { count += 1; return count; };
    global.clearTimeout = function () {};
    window.gpiFetch = fetcher;
    await window.gpiAttendanceCorrection.pollJob(9);
    return count;
  }
  return [
    await scheduled(function () { return Promise.reject(new Error('network')); }),
    await scheduled(function () { return Promise.resolve({status: 503, ok: false, json: function () { return Promise.resolve({ok: false}); }}); }),
    await scheduled(function () { return Promise.resolve({status: 503, ok: false, json: function () { return Promise.reject(new Error('invalid JSON')); }}); }),
    await scheduled(function () { return Promise.resolve({status: 401, ok: false, json: function () { return Promise.resolve({ok: false}); }}); }),
    await scheduled(function () { return Promise.resolve({status: 404, ok: false, json: function () { return Promise.resolve({ok: false}); }}); }),
    await scheduled(function () { return Promise.resolve({status: 200, ok: true, json: function () { return Promise.reject(new Error('invalid JSON')); }}); }),
    await scheduled(function () { return Promise.resolve({status: 200, ok: true, json: function () { return Promise.resolve({ok: true, status: 'mystery', retryable: false}); }}); }),
    await scheduled(function () { return Promise.resolve({status: 200, ok: true, json: function () { return Promise.resolve({ok: true, status: 'applying', retryable: false, poll_after_ms: 2000}); }}); }),
  ];
})()
""")

    assert timers == [1, 1, 0, 0, 0, 0, 0, 1]


def test_dialog_supports_escape_close_and_returns_focus_to_the_card_action():
    js = _js()

    assert "attendanceCorrectionDialog.showModal()" in js
    assert "event.key === 'Escape'" in js
    assert "attendanceCorrectionOpener.focus()" in js
    assert "attendanceCorrectionDialog.addEventListener('cancel'" in js
    assert "attendanceCorrectionDialog.addEventListener('close'" in js


def test_dialog_uses_abortable_epochs_for_preview_apply_and_poll_responses():
    js = _js()

    assert "attendanceCorrectionEpoch" in js
    assert "new AbortController()" in js
    assert "function cancelAttendanceCorrectionRequests()" in js
    assert "function attendanceResponseIsCurrent(epoch)" in js
    assert "if (!attendanceResponseIsCurrent(epoch)) return;" in js
    assert "pollAttendanceCorrectionJob(jobId, epoch)" in js
    assert "scheduleAttendancePoll(jobId, requestedDelay, epoch)" in js
    assert "cancelAttendanceCorrectionRequests();" in js
    assert "signal:" in js


def test_request_epoch_runtime_aborts_and_invalidates_old_dialog_work():
    js = _js()
    start = js.index("  var attendanceCorrectionEpoch = 0;")
    end = js.index("  function padAttendanceTime(value)")
    functions = js[start:end]
    harness = f"""
var attendanceCorrectionDialog = {{open: true}};
var attendanceCorrectionRow = {{item: 'first'}};
var correctionPreviewToken = 'signed';
var attendancePollTimer = null;
{functions}
var request = beginAttendanceCorrectionRequest();
if (!attendanceResponseIsCurrent(request.epoch)) throw new Error('new request not current');
if (request.signal.aborted) throw new Error('new request started aborted');
cancelAttendanceCorrectionRequests();
if (!request.signal.aborted) throw new Error('old request was not aborted');
if (attendanceResponseIsCurrent(request.epoch)) throw new Error('old response stayed current');
var next = beginAttendanceCorrectionRequest();
attendanceCorrectionDialog.open = false;
if (attendanceResponseIsCurrent(next.epoch)) throw new Error('closed dialog accepted response');
"""

    result = subprocess.run(
        ["node", "--eval", harness], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_stale_preview_apply_and_poll_callbacks_cannot_mutate_the_dialog():
    js = _js()
    state = js[
        js.index("  var attendanceCorrectionEpoch = 0;") : js.index(
            "  function padAttendanceTime(value)"
        )
    ]
    completeness = js[
        js.index("  function attendancePreviewIsComplete(preview)") : js.index(
            "  function showAttendanceRefreshConfirmation()"
        )
    ]
    preview = js[
        js.index("  function previewAttendanceCorrection()") : js.index(
            "  function attendanceProgress(text, isError)"
        )
    ]
    poll = js[
        js.index("  function attendanceProgress(text, isError)") : js.index(
            "  function applyAttendanceCorrection()"
        )
    ]
    apply = js[
        js.index("  function applyAttendanceCorrection()") : js.index(
            "  function openAttendanceCorrection(button)"
        )
    ]
    harness = f"""
function deferred() {{
  var resolve;
  var promise = new Promise(function (done) {{ resolve = done; }});
  return {{promise: promise, resolve: resolve}};
}}
function element() {{
  return {{
    hidden: false, disabled: false, textContent: '',
    classList: {{toggle: function () {{}}}},
    replaceChildren: function () {{}},
  }};
}}
var elements = {{
  message: element(), progress: element(), preview: element(), apply: element(),
  output: element(), refresh: element(),
}};
var attendanceCorrectionDialog = {{
  open: true,
  querySelector: function (selector) {{
    if (selector.indexOf('message') !== -1) return elements.message;
    if (selector.indexOf('progress') !== -1) return elements.progress;
    if (selector.indexOf('preview-output') !== -1) return elements.output;
    if (selector.indexOf('refresh-wrap') !== -1) return elements.refresh;
    if (selector.indexOf('attendance-preview]') !== -1) return elements.preview;
    if (selector.indexOf('attendance-apply') !== -1) return elements.apply;
    return element();
  }},
}};
var attendanceCorrectionForm = {{}};
var attendanceCorrectionRow = {{item: 'current'}};
var correctionPreviewToken = null;
var attendancePollTimer = null;
var effects = [];
var scheduled = 0;
function setTimeout() {{ scheduled += 1; return scheduled; }}
function clearTimeout() {{}}
var window = {{
  location: {{reload: function () {{ effects.push('reload'); }}}},
}};
function attendanceCorrectionPayload() {{ return {{item_key: 'current'}}; }}
function renderAttendancePreview() {{ effects.push('render'); }}
function showAttendanceRefreshConfirmation() {{ effects.push('confirm'); }}
function localAttendanceTimeCandidates() {{ return []; }}
function localAttendanceTimeResolution() {{ return {{status: 'nonexistent'}}; }}
var posts = [];
function postJson() {{ return posts.shift().promise; }}
var polls = [];
function fetchCompat() {{ return polls.shift().promise; }}
{state}
{completeness}
{preview}
{poll}
{apply}
(async function () {{
  var oldPreview = deferred();
  posts.push(oldPreview);
  previewAttendanceCorrection();
  cancelAttendanceCorrectionRequests();
  oldPreview.resolve({{ok: true, preview_token: 'old', preview: {{}}}});
  await Promise.resolve();
  await Promise.resolve();
  if (effects.length) throw new Error('stale preview rendered');
  if (correctionPreviewToken !== null) throw new Error('stale preview restored token');
  if (elements.message.textContent !== 'Reading the latest attendance from Odoo…') {{
    throw new Error('stale preview changed message');
  }}

  correctionPreviewToken = 'signed';
  var oldApply = deferred();
  posts.push(oldApply);
  applyAttendanceCorrection();
  cancelAttendanceCorrectionRequests();
  oldApply.resolve({{ok: true, job_id: 9}});
  await Promise.resolve();
  await Promise.resolve();
  if (effects.length) throw new Error('stale apply mutated dialog');
  if (correctionPreviewToken !== 'signed') throw new Error('stale apply changed token');
  if (elements.message.textContent !== 'Queueing the verified Odoo correction…') {{
    throw new Error('stale apply changed message');
  }}

  var oldPoll = deferred();
  polls.push(oldPoll);
  var epoch = attendanceCorrectionEpoch;
  elements.progress.textContent = 'unchanged';
  var pendingPoll = pollAttendanceCorrectionJob(9, epoch);
  cancelAttendanceCorrectionRequests();
  oldPoll.resolve({{
    status: 503,
    ok: false,
    json: function () {{ return Promise.resolve({{ok: false}}); }},
  }});
  await pendingPoll;
  if (elements.progress.textContent !== 'unchanged') {{
    throw new Error('stale poll changed progress');
  }}
  if (scheduled !== 0) throw new Error('stale poll scheduled another request');
}})().catch(function (error) {{
  process.stderr.write(String(error.stack || error));
  process.exitCode = 1;
}});
"""

    result = subprocess.run(
        ["node", "--eval", harness], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
