from pathlib import Path
import subprocess


JS = Path("src/zira_dashboard/static/exceptions.js")


def _js():
    return JS.read_text(encoding="utf-8")


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
    assert "function plantAttendanceParts(value)" in js
    assert "Intl.DateTimeFormat('en-US'" in js
    assert "timeZone: attendancePlantTimezone()" in js
    assert "function plantAttendanceUtcValue(input)" in js
    assert "value.getHours()" not in js


def test_datetime_conversion_rejects_nonexistent_and_ambiguous_plant_times():
    js = _js()
    start = js.index("  function attendancePlantTimezone()")
    end = js.index("  function attendanceCorrectionPayload()")
    functions = js[start:end]
    harness = f"""
var attendanceCorrectionDialog = {{dataset: {{plantTimezone: 'America/Chicago'}}}};
{functions}
function assertEqual(actual, expected, label) {{
  if (actual !== expected) throw new Error(label + ': ' + actual + ' !== ' + expected);
}}
assertEqual(
  plantAttendanceUtcValue({{value: '2026-08-28T11:55'}}),
  '2026-08-28T16:55:00.000Z',
  'ordinary time'
);
assertEqual(plantAttendanceUtcValue({{value: '2026-03-08T02:30'}}), null, 'spring gap');
assertEqual(plantAttendanceUtcValue({{value: '2026-11-01T01:30'}}), null, 'fall overlap');
"""

    result = subprocess.run(
        ["node", "--eval", harness], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_job_polling_is_retry_safe_and_never_advances_the_worker():
    js = _js()

    assert "function pollAttendanceCorrectionJob(jobId, epoch)" in js
    assert "'/api/exceptions/attendance-correction/' + encodeURIComponent(jobId)" in js
    assert "resp.poll_after_ms" in js
    assert "pollAttendanceCorrectionJob(jobId, epoch)" in js
    assert "process_job" not in js
    assert "resp.status === 'complete'" in js
    assert "resp.retryable" in js


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
