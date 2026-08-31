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
    result = _run_attendance_hook("({gap: window.gpiAttendanceCorrection.localTimeResolution('2026-03-08T02:30', 'America/Chicago', null), fall: window.gpiAttendanceCorrection.localTimeCandidates('2026-11-01T01:30', 'America/Chicago'), first: window.gpiAttendanceCorrection.localTimeResolution('2026-11-01T01:30', 'America/Chicago', '2026-11-01T06:30:00.000Z'), second: window.gpiAttendanceCorrection.localTimeResolution('2026-11-01T01:30', 'America/Chicago', '2026-11-01T07:30:00.000Z')})")

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

    assert "function pollAttendanceCorrectionJob(jobId)" in js
    assert (
        "'/api/exceptions/attendance-correction/' + encodeURIComponent(jobId)" in js
    )
    assert "resp.poll_after_ms" in js
    assert "setTimeout(function () { pollAttendanceCorrectionJob(jobId); }, delay)" in js
    assert "process_job" not in js
    assert "resp.status === 'complete'" in js
    assert "resp.retryable" in js


def test_job_polling_retries_only_network_and_503_responses():
    decisions = _run_attendance_hook("[503, 401, 403, 404, 500].map(function (status) { return window.gpiAttendanceCorrection.pollDecision(status, false, true); }).concat([window.gpiAttendanceCorrection.pollDecision(503, false, false), window.gpiAttendanceCorrection.pollDecision(200, true, false)])")

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
