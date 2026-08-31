(function () {
  var POLL_MS = 60000;
  var currentFocus = 'all';

  function fetchCompat(url, opts) {
    if (window.gpiFetch) return window.gpiFetch(url, opts);
    if (typeof window.fetch === 'function') return window.fetch(url, opts);
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open((opts && opts.method) || 'GET', url, true);
      Object.keys((opts && opts.headers) || {}).forEach(function (name) {
        xhr.setRequestHeader(name, opts.headers[name]);
      });
      xhr.onload = function () {
        var responseText = xhr.responseText || '';
        resolve({
          ok: xhr.status >= 200 && xhr.status < 300,
          status: xhr.status,
          json: function () {
            return new Promise(function (jsonResolve, jsonReject) {
              try {
                jsonResolve(responseText ? JSON.parse(responseText) : {});
              } catch (error) {
                jsonReject(error);
              }
            });
          },
        });
      };
      xhr.onerror = function () { reject(new Error('network error')); };
      xhr.send((opts && opts.body) || null);
    });
  }

  function postJson(url, payload) {
    return fetchCompat(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload || {}),
    }).then(function (r) { return r.json(); });
  }

  // ---- Verified Odoo attendance correction -------------------------------
  var attendanceCorrectionDialog = document.querySelector('[data-attendance-correction-dialog]');
  var attendanceCorrectionForm = document.querySelector('[data-attendance-correction-form]');
  var attendanceCorrectionOpener = null;
  var attendanceCorrectionRow = null;
  var correctionPreviewToken = null;
  var attendancePollTimer = null;

  function attendanceEl(selector) {
    return attendanceCorrectionDialog && attendanceCorrectionDialog.querySelector(selector);
  }

  function attendanceMessage(text, isError) {
    var el = attendanceEl('[data-attendance-message]');
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('is-error', !!isError);
  }

  function setAttendanceApplyEnabled(enabled) {
    var btn = attendanceEl('[data-attendance-apply]');
    if (btn) btn.disabled = !enabled;
  }

  function setAttendancePreviewBusy(busy) {
    var btn = attendanceEl('[data-attendance-preview]');
    if (!btn) return;
    btn.disabled = !!busy;
    btn.textContent = busy ? 'Building preview…' : 'Preview Odoo change';
  }

  function clearAttendancePreview() {
    var output = attendanceEl('[data-attendance-preview-output]');
    if (!output) return;
    output.replaceChildren();
    output.hidden = true;
  }

  function hideAttendanceRefreshConfirmation() {
    var wrap = attendanceEl('[data-attendance-refresh-wrap]');
    if (wrap) wrap.hidden = true;
  }

  function invalidateAttendancePreview() {
    correctionPreviewToken = null;
    setAttendanceApplyEnabled(false);
    hideAttendanceRefreshConfirmation();
    clearAttendancePreview();
  }

  function padAttendanceTime(value) {
    return String(value).padStart(2, '0');
  }

  function attendancePlantTimezone() {
    return (attendanceCorrectionDialog && attendanceCorrectionDialog.dataset.plantTimezone) || 'America/Chicago';
  }

  function plantAttendanceParts(value, timezone) {
    var parts = new Intl.DateTimeFormat('en-US', {
      timeZone: timezone || attendancePlantTimezone(),
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(value);
    var result = {};
    parts.forEach(function (part) {
      if (part.type !== 'literal') result[part.type] = Number(part.value);
    });
    return result;
  }

  function parsedAttendanceLocalTime(localValue) {
    if (!localValue) return null;
    var match = localValue.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/);
    if (!match) return null;
    var parts = {
      year: Number(match[1]),
      month: Number(match[2]),
      day: Number(match[3]),
      hour: Number(match[4]),
      minute: Number(match[5]),
    };
    var desired = Date.UTC(
      parts.year, parts.month - 1, parts.day, parts.hour, parts.minute
    );
    var check = new Date(desired);
    if (
      check.getUTCFullYear() !== parts.year ||
      check.getUTCMonth() !== parts.month - 1 ||
      check.getUTCDate() !== parts.day ||
      check.getUTCHours() !== parts.hour ||
      check.getUTCMinutes() !== parts.minute
    ) return null;
    return {parts: parts, desired: desired};
  }

  function sameAttendanceLocalTime(parts, desired) {
    return (
      parts.year === desired.year &&
      parts.month === desired.month &&
      parts.day === desired.day &&
      parts.hour === desired.hour &&
      parts.minute === desired.minute
    );
  }

  function localAttendanceTimeCandidates(localValue, timezone) {
    var parsed = parsedAttendanceLocalTime(localValue);
    if (!parsed) return [];
    var zone = timezone || attendancePlantTimezone();
    var offsets = {};
    for (var hour = -36; hour <= 36; hour += 6) {
      var instant = parsed.desired + (hour * 60 * 60 * 1000);
      var observed = plantAttendanceParts(new Date(instant), zone);
      var observedUtc = Date.UTC(
        observed.year, observed.month - 1, observed.day,
        observed.hour, observed.minute
      );
      offsets[String(observedUtc - instant)] = true;
    }
    var candidates = [];
    Object.keys(offsets).forEach(function (offsetText) {
      var instant = parsed.desired - Number(offsetText);
      var observed = plantAttendanceParts(new Date(instant), zone);
      if (sameAttendanceLocalTime(observed, parsed.parts)) {
        candidates.push(new Date(instant).toISOString());
      }
    });
    return Array.from(new Set(candidates)).sort();
  }

  function localAttendanceTimeResolution(localValue, timezone, occurrence) {
    var candidates = localAttendanceTimeCandidates(localValue, timezone);
    if (!candidates.length) return {status: 'nonexistent'};
    if (candidates.length > 1 && candidates.indexOf(occurrence) === -1) {
      return {status: 'ambiguous', candidates: candidates};
    }
    return {
      status: 'resolved',
      value: candidates.length === 1 ? candidates[0] : occurrence,
    };
  }

  function localAttendanceInputValue(iso) {
    if (!iso) return '';
    var value = new Date(iso);
    if (Number.isNaN(value.getTime())) return '';
    var parts = plantAttendanceParts(value);
    return [
      parts.year, '-', padAttendanceTime(parts.month), '-',
      padAttendanceTime(parts.day), 'T', padAttendanceTime(parts.hour), ':',
      padAttendanceTime(parts.minute),
    ].join('');
  }

  function attendanceOccurrenceContainer(input) {
    if (input === attendanceEl('[data-attendance-start]')) {
      return attendanceEl('[data-attendance-start-occurrence]');
    }
    return attendanceEl('[data-attendance-end-occurrence]');
  }

  function selectedAttendanceOccurrence(container) {
    var selected = container && container.querySelector('input:checked');
    return selected ? selected.value : null;
  }

  function attendanceTimeResolution(input) {
    if (!input || !input.value) return {status: 'invalid'};
    var container = attendanceOccurrenceContainer(input);
    return localAttendanceTimeResolution(
      input.value,
      attendancePlantTimezone(),
      selectedAttendanceOccurrence(container)
    );
  }

  function plantAttendanceUtcValue(input) {
    var resolution = attendanceTimeResolution(input);
    return resolution.status === 'resolved' ? resolution.value : null;
  }

  function attendanceOccurrenceLabel(value, index) {
    var label = new Intl.DateTimeFormat('en-US', {
      timeZone: attendancePlantTimezone(),
      hour: 'numeric',
      minute: '2-digit',
      timeZoneName: 'short',
    }).format(new Date(value));
    return (index === 0 ? 'First occurrence — ' : 'Second occurrence — ') + label;
  }

  function renderAttendanceOccurrenceChoices(input, container, name) {
    if (!input || !container) return;
    var previous = selectedAttendanceOccurrence(container);
    var options = container.querySelector('[data-attendance-occurrence-options]');
    if (!options) return;
    options.replaceChildren();
    var candidates = localAttendanceTimeCandidates(
      input.value, attendancePlantTimezone()
    );
    container.hidden = candidates.length < 2;
    if (candidates.length < 2) return;
    candidates.forEach(function (candidate, index) {
      var label = document.createElement('label');
      var radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = name;
      radio.value = candidate;
      radio.checked = candidate === previous;
      label.appendChild(radio);
      label.appendChild(document.createTextNode(attendanceOccurrenceLabel(candidate, index)));
      options.appendChild(label);
    });
  }

  function refreshAttendanceOccurrenceChoices() {
    renderAttendanceOccurrenceChoices(
      attendanceEl('[data-attendance-start]'),
      attendanceEl('[data-attendance-start-occurrence]'),
      'attendance_start_occurrence'
    );
    renderAttendanceOccurrenceChoices(
      attendanceEl('[data-attendance-end]'),
      attendanceEl('[data-attendance-end-occurrence]'),
      'attendance_end_occurrence'
    );
  }

  function clearAttendanceOccurrenceChoices() {
    [
      attendanceEl('[data-attendance-start-occurrence]'),
      attendanceEl('[data-attendance-end-occurrence]'),
    ].forEach(function (container) {
      if (!container) return;
      var options = container.querySelector('[data-attendance-occurrence-options]');
      if (options) options.replaceChildren();
      container.hidden = true;
    });
  }

  function attendanceCorrectionPayload() {
    if (!attendanceCorrectionRow) return null;
    var employeeIds = Array.from(
      attendanceCorrectionForm.querySelectorAll('input[name="employee_odoo_ids"]:checked')
    ).map(function (input) { return asInt(input.value); }).filter(function (value) {
      return value !== null && value > 0;
    });
    var startInput = attendanceEl('[data-attendance-start]');
    var endInput = attendanceEl('[data-attendance-end]');
    var openInput = attendanceEl('[data-attendance-open-ended]');
    var target = attendanceEl('[data-attendance-work-center]');
    var startResolution = attendanceTimeResolution(startInput);
    var endResolution = openInput && openInput.checked
      ? {status: 'open', value: null}
      : attendanceTimeResolution(endInput);
    var startUtc = plantAttendanceUtcValue(startInput);
    var endUtc = openInput && openInput.checked ? null : plantAttendanceUtcValue(endInput);
    if (!employeeIds.length) {
      attendanceMessage('Choose at least one worker.', true);
      return null;
    }
    if (startResolution.status === 'nonexistent') {
      attendanceMessage('This start time does not exist because the clock changes.', true);
      return null;
    }
    if (startResolution.status === 'ambiguous') {
      attendanceMessage('Choose the first or second start time.', true);
      return null;
    }
    if (!startUtc) {
      attendanceMessage('Enter an exact start time.', true);
      return null;
    }
    if (endResolution.status === 'nonexistent') {
      attendanceMessage('This end time does not exist because the clock changes.', true);
      return null;
    }
    if (endResolution.status === 'ambiguous') {
      attendanceMessage('Choose the first or second end time.', true);
      return null;
    }
    if (!openInput.checked && !endUtc) {
      attendanceMessage('Enter an end time or choose Still working.', true);
      return null;
    }
    if (endUtc && new Date(endUtc) <= new Date(startUtc)) {
      attendanceMessage('The end time must be later than the start time.', true);
      return null;
    }
    if (!target || !target.value) {
      attendanceMessage('Choose a target work center.', true);
      return null;
    }
    return {
      item_key: attendanceCorrectionRow.dataset.correctionItemKey,
      employee_odoo_ids: employeeIds,
      work_center_name: target.value,
      start_utc: startUtc,
      end_utc: endUtc,
    };
  }

  function textElement(tag, className, textValue) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = textValue || '';
    return element;
  }

  function renderAttendanceIntervals(parent, title, intervals) {
    var group = document.createElement('div');
    group.className = 'attendance-preview-group';
    group.appendChild(textElement('span', 'attendance-preview-group-title', title));
    if (!intervals || !intervals.length) {
      group.appendChild(textElement('p', 'attendance-preview-summary', 'No attendance rows'));
    } else {
      intervals.forEach(function (interval) {
        var row = document.createElement('div');
        row.className = 'attendance-preview-interval';
        row.appendChild(textElement('span', '', interval.work_center_name));
        row.appendChild(textElement(
          'span', '', interval.start_label + ' to ' + (interval.end_label || 'Still working')
        ));
        group.appendChild(row);
      });
    }
    parent.appendChild(group);
  }

  function operationSummaryText(summary) {
    summary = summary || {};
    return [
      Number(summary.create || 0) + ' create',
      Number(summary.update || 0) + ' change',
      Number(summary.delete || 0) + ' remove',
    ].join(' · ');
  }

  function renderAttendancePreview(preview) {
    var output = attendanceEl('[data-attendance-preview-output]');
    if (!output || !preview) return;
    output.replaceChildren();
    output.appendChild(textElement(
      'h3', 'attendance-preview-heading',
      preview.target_work_center_name + ' · ' + preview.start_label + ' to ' + preview.end_label
    ));
    (preview.employees || []).forEach(function (employee) {
      var person = document.createElement('section');
      person.className = 'attendance-preview-person';
      person.appendChild(textElement('h3', '', employee.name));
      renderAttendanceIntervals(person, 'Odoo source rows', employee.source_intervals);
      renderAttendanceIntervals(person, 'Before', employee.before_intervals);
      renderAttendanceIntervals(person, 'After', employee.after_intervals);
      person.appendChild(textElement(
        'p', 'attendance-preview-summary',
        'Odoo plan: ' + operationSummaryText(employee.operation_summary)
      ));
      output.appendChild(person);
    });
    output.hidden = false;
  }

  function showAttendanceRefreshConfirmation() {
    setAttendanceApplyEnabled(false);
    var wrap = attendanceEl('[data-attendance-refresh-wrap]');
    if (wrap) wrap.hidden = false;
    attendanceMessage('Review the refreshed preview. Confirm it before applying.', true);
  }

  function previewAttendanceCorrection() {
    var payload = attendanceCorrectionPayload();
    if (!payload) return;
    invalidateAttendancePreview();
    setAttendancePreviewBusy(true);
    attendanceMessage('Reading the latest attendance from Odoo…', false);
    postJson('/api/exceptions/attendance-correction/preview', payload)
      .then(function (resp) {
        setAttendancePreviewBusy(false);
        if (!resp || !resp.ok) {
          attendanceMessage((resp && resp.error) || 'The preview could not be built.', true);
          return;
        }
        correctionPreviewToken = resp.preview_token;
        renderAttendancePreview(resp.preview);
        setAttendanceApplyEnabled(true);
        attendanceMessage('Preview ready. Review it before applying.', false);
      })
      .catch(function () {
        setAttendancePreviewBusy(false);
        attendanceMessage('Network error. Nothing was changed.', true);
      });
  }

  function attendanceProgress(text, isError) {
    var progress = attendanceEl('[data-attendance-progress]');
    if (!progress) return;
    progress.hidden = false;
    progress.textContent = text || '';
    progress.classList.toggle('is-error', !!isError);
  }

  function scheduleAttendancePoll(jobId, requestedDelay) {
    if (attendancePollTimer) clearTimeout(attendancePollTimer);
    var delay = Math.max(1000, Math.min(Number(requestedDelay) || 2000, 10000));
    attendancePollTimer = setTimeout(function () { pollAttendanceCorrectionJob(jobId); }, delay);
  }

  function attendancePollDecision(status, ok, validJson) {
    if (status === 401 || status === 403) {
      return {retry: false, message: 'Sign in again, then refresh the inbox to check this correction.'};
    }
    if (status === 404) {
      return {retry: false, message: 'This correction status was not found. Refresh the inbox before trying again.'};
    }
    if (!validJson) {
      return {retry: false, message: 'Plant Manager did not return a valid status. Refresh the inbox to check the correction.'};
    }
    if (status === 503) {
      return {retry: true, message: 'Odoo status is briefly unavailable. Checking again safely…'};
    }
    if (!ok) {
      return {retry: false, message: 'Plant Manager could not check this correction. Refresh the inbox before trying again.'};
    }
    return {retry: false, message: ''};
  }

  window.gpiAttendanceCorrection = Object.freeze({
    localTimeCandidates: localAttendanceTimeCandidates,
    localTimeResolution: localAttendanceTimeResolution,
    pollDecision: attendancePollDecision,
    pollJob: pollAttendanceCorrectionJob,
  });

  function attendancePollNetworkFailure(jobId) {
    attendanceProgress('Connection lost. Checking again safely…', true);
    scheduleAttendancePoll(jobId, 3000);
  }

  function handleAttendancePollResult(jobId, response, resp, validJson) {
    var decision = attendancePollDecision(
      response && response.status, response && response.ok, validJson
    );
    if (decision.retry) {
      attendanceProgress(decision.message, true);
      scheduleAttendancePoll(jobId, 3000);
      return;
    }
    if (decision.message) {
      attendanceProgress(decision.message, true);
      return;
    }
    if (!resp || !resp.ok) {
      attendanceProgress(
        (resp && resp.error) || 'Correction status stopped. Refresh the inbox to check it.',
        true
      );
      return;
    }
    var knownStatuses = [
      'planned', 'applying', 'verifying', 'recalculating', 'complete', 'failed',
    ];
    if (knownStatuses.indexOf(resp.status) === -1) {
      attendanceProgress(
        'Plant Manager did not return a valid status. Refresh the inbox to check the correction.',
        true
      );
      return;
    }
    if (resp.status === 'complete') {
      attendanceProgress('Correction complete. Refreshing the inbox…', false);
      setTimeout(function () { window.location.reload(); }, 900);
      return;
    }
    if (resp.retryable) {
      attendanceProgress(resp.error || 'Odoo could not finish. Preview and try again.', true);
      setAttendancePreviewBusy(false);
      invalidateAttendancePreview();
      return;
    }
    if (resp.status === 'failed') {
      attendanceProgress(
        resp.error || 'Odoo could not finish. Refresh the inbox before trying again.',
        true
      );
      return;
    }
    attendanceProgress(
      'Correction in progress · ' + Number(resp.completed_operation_count || 0) + ' Odoo changes saved',
      false
    );
    scheduleAttendancePoll(jobId, resp.poll_after_ms);
  }

  function pollAttendanceCorrectionJob(jobId) {
    var request;
    try {
      request = fetchCompat(
        '/api/exceptions/attendance-correction/' + encodeURIComponent(jobId),
        {headers: {'Accept': 'application/json'}}
      );
    } catch (error) {
      attendancePollNetworkFailure(jobId);
      return;
    }
    return request.then(function (response) {
      if (!response || typeof response.json !== 'function') {
        handleAttendancePollResult(jobId, response, null, false);
        return;
      }
      var body;
      try {
        body = response.json();
      } catch (error) {
        handleAttendancePollResult(jobId, response, null, false);
        return;
      }
      return body.then(
        function (resp) {
          var validJson = !!resp && typeof resp === 'object' && !Array.isArray(resp);
          handleAttendancePollResult(jobId, response, resp, validJson);
        },
        function () {
          handleAttendancePollResult(jobId, response, null, false);
        }
      );
    }, function () {
      attendancePollNetworkFailure(jobId);
    });
  }

  function applyAttendanceCorrection() {
    if (!correctionPreviewToken) return;
    setAttendanceApplyEnabled(false);
    setAttendancePreviewBusy(true);
    attendanceMessage('Queueing the verified Odoo correction…', false);
    postJson('/api/exceptions/attendance-correction/apply', {preview_token: correctionPreviewToken})
      .then(function (resp) {
        setAttendancePreviewBusy(false);
        if (resp && resp.code === 'source_changed') {
          correctionPreviewToken = resp.preview_token;
          renderAttendancePreview(resp.preview);
          showAttendanceRefreshConfirmation();
          return;
        }
        if (!resp || !resp.ok) {
          attendanceMessage((resp && resp.error) || 'The correction could not be queued.', true);
          return;
        }
        correctionPreviewToken = null;
        attendanceMessage('Correction queued. Plant Manager is checking Odoo.', false);
        attendanceProgress('Starting the correction…', false);
        pollAttendanceCorrectionJob(resp.job_id);
      })
      .catch(function () {
        setAttendancePreviewBusy(false);
        attendanceMessage('Network error. Check status before trying again.', true);
      });
  }

  function openAttendanceCorrection(button) {
    if (!attendanceCorrectionDialog || !attendanceCorrectionForm) return;
    attendanceCorrectionOpener = button;
    attendanceCorrectionRow = button.closest('.exception-row');
    if (!attendanceCorrectionRow) return;
    if (attendancePollTimer) clearTimeout(attendancePollTimer);
    attendancePollTimer = null;
    attendanceCorrectionForm.querySelectorAll('input[name="employee_odoo_ids"]').forEach(function (input) {
      input.checked = false;
    });
    var start = attendanceEl('[data-attendance-start]');
    var end = attendanceEl('[data-attendance-end]');
    var openEnded = attendanceEl('[data-attendance-open-ended]');
    var target = attendanceEl('[data-attendance-work-center]');
    clearAttendanceOccurrenceChoices();
    start.value = localAttendanceInputValue(attendanceCorrectionRow.dataset.correctionStartUtc);
    end.value = localAttendanceInputValue(attendanceCorrectionRow.dataset.correctionEndUtc);
    openEnded.checked = attendanceCorrectionRow.dataset.correctionEndOpen === 'true';
    end.disabled = openEnded.checked;
    target.value = attendanceCorrectionRow.dataset.correctionWorkCenter || '';
    refreshAttendanceOccurrenceChoices();
    var progress = attendanceEl('[data-attendance-progress]');
    if (progress) {
      progress.hidden = true;
      progress.textContent = '';
    }
    invalidateAttendancePreview();
    attendanceMessage('', false);
    attendanceCorrectionDialog.showModal();
    var firstPerson = attendanceCorrectionForm.querySelector('input[name="employee_odoo_ids"]');
    if (firstPerson) firstPerson.focus();
  }

  function closeAttendanceCorrection() {
    if (!attendanceCorrectionDialog || !attendanceCorrectionDialog.open) return;
    attendanceCorrectionDialog.close();
  }

  function initAttendanceCorrection() {
    if (!attendanceCorrectionDialog || !attendanceCorrectionForm) return;
    document.querySelectorAll('[data-attendance-correction-open]').forEach(function (button) {
      button.addEventListener('click', function () { openAttendanceCorrection(button); });
    });
    attendanceCorrectionDialog.querySelectorAll('[data-attendance-close]').forEach(function (button) {
      button.addEventListener('click', closeAttendanceCorrection);
    });
    attendanceEl('[data-attendance-preview]').addEventListener('click', previewAttendanceCorrection);
    attendanceEl('[data-attendance-apply]').addEventListener('click', applyAttendanceCorrection);
    attendanceEl('[data-attendance-refresh-confirm]').addEventListener('click', function () {
      hideAttendanceRefreshConfirmation();
      setAttendanceApplyEnabled(true);
      attendanceMessage('Refreshed preview confirmed. Apply when ready.', false);
    });
    attendanceCorrectionForm.addEventListener('change', function (event) {
      if (event.target.matches('[data-attendance-open-ended]')) {
        var end = attendanceEl('[data-attendance-end]');
        end.disabled = event.target.checked;
        if (event.target.checked) end.value = '';
        refreshAttendanceOccurrenceChoices();
      }
      invalidateAttendancePreview();
    });
    attendanceCorrectionForm.addEventListener('input', function (event) {
      if (event.target.matches('[data-attendance-start], [data-attendance-end]')) {
        refreshAttendanceOccurrenceChoices();
      }
      invalidateAttendancePreview();
    });
    attendanceCorrectionDialog.addEventListener('cancel', function (event) {
      event.preventDefault();
      closeAttendanceCorrection();
    });
    attendanceCorrectionDialog.addEventListener('close', function () {
      if (attendanceCorrectionOpener && typeof attendanceCorrectionOpener.focus === 'function') {
        attendanceCorrectionOpener.focus();
      }
      attendanceCorrectionOpener = null;
      attendanceCorrectionRow = null;
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && attendanceCorrectionDialog.open) {
        event.preventDefault();
        closeAttendanceCorrection();
      }
    });
  }

  function openAlert(key) {
    var api = window.gpiAlertBadges && window.gpiAlertBadges[key];
    if (api && typeof api.openModal === 'function') {
      api.openModal();
    }
  }

  function asInt(value) {
    var parsed = parseInt(value, 10);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function rowStatus(row, text, isError) {
    var status = row.querySelector('.row-status');
    if (!status) return;
    row.classList.toggle('is-error', !!isError);
    status.hidden = false;
    status.textContent = text;
  }

  function setBusy(row, busy) {
    row.querySelectorAll('button, input, select').forEach(function (el) {
      el.disabled = !!busy;
    });
  }

  function countElText(el) {
    return Math.max(0, parseInt(el.textContent || '0', 10) || 0);
  }

  function bumpCount(el, delta) {
    if (el) el.textContent = countElText(el) + delta;
  }

  function bumpTotal(delta) {
    bumpCount(document.querySelector('[data-total-open]'), delta);
  }

  function bumpUrgentInline(row, delta) {
    if (!row || row.dataset.priority !== 'urgent') return;
    var urgent = document.querySelector('[data-urgent-open]');
    var wrap = document.querySelector('[data-urgent-wrap]');
    if (!urgent || !wrap) return;
    var next = countElText(urgent) + delta;
    urgent.textContent = next;
    wrap.hidden = next <= 0;
  }

  function bumpFocusCount(key, delta) {
    var el = document.querySelector('[data-focus-count="' + key + '"]');
    if (el) el.textContent = countElText(el) + delta;
  }

  function bumpFocusCounts(row, delta) {
    bumpFocusCount('all', delta);
    if (row && row.dataset.priority === 'urgent') bumpFocusCount('urgent', delta);
    if (row && row.dataset.priority === 'muted') bumpFocusCount('followup', delta);
  }

  function refreshSharedBadge(row) {
    var actionType = row && row.dataset.actionType;
    var badgeKey = null;
    if (actionType === 'assignment') badgeKey = 'assignments';
    else if (actionType === 'late_absence') badgeKey = 'late';
    else if (actionType === 'missing_wc') badgeKey = 'missing_wc';
    else if (actionType === 'missed_punch_out') badgeKey = 'missed_punch_out';
    else if (actionType === 'breakdown' || actionType === 'breakdown_header') badgeKey = 'breakdown';
    if (!badgeKey) return;
    var api = window.gpiAlertBadges && window.gpiAlertBadges[badgeKey];
    if (api && typeof api.refreshCount === 'function') api.refreshCount();
  }

  function refreshInboxSummary() {
    if (typeof window.gpiRefreshInboxSummary === 'function') window.gpiRefreshInboxSummary();
  }

  function updateQueueEmpty() {
    var queue = document.querySelector('[data-queue]');
    var empty = document.querySelector('[data-queue-empty]');
    if (!queue || !empty) return;
    var hasRows = !!queue.querySelector('.exception-row');
    empty.hidden = hasRows;
  }

  function removeResolvedRow(row) {
    row.remove();
    updateQueueEmpty();
  }

  var UNDO_MS = 5000;

  function undoRow(row, eventId) {
    setBusy(row, true);
    rowStatus(row, 'Undoing...', false);
    postJson('/api/exceptions/undo/' + encodeURIComponent(eventId), {})
      .then(function (resp) {
        if (resp && resp.ok) {
          window.location.reload();
        } else {
          rowStatus(row, (resp && resp.error) || 'Undo failed.', true);
        }
      })
      .catch(function () { rowStatus(row, 'Network error.', true); });
  }

  function finalizeResolved(row) {
    bumpTotal(-1);
    bumpUrgentInline(row, -1);
    bumpFocusCounts(row, -1);
    refreshSharedBadge(row);
    refreshInboxSummary();
    removeResolvedRow(row);
    applyFocus(currentFocus);
  }

  function resolveRow(row, label, eventId) {
    setBusy(row, true);
    row.classList.add('is-resolved');
    var status = row.querySelector('.row-status');
    if (eventId && status) {
      status.hidden = false;
      status.textContent = (label || 'Done') + ' · ';
      var undo = document.createElement('button');
      undo.type = 'button';
      undo.className = 'undo-link';
      undo.setAttribute('data-undo', String(eventId));
      undo.textContent = 'Undo';
      status.appendChild(undo);
      var timer = setTimeout(function () { finalizeResolved(row); }, UNDO_MS);
      undo.addEventListener('click', function () {
        clearTimeout(timer);
        undoRow(row, eventId);
      });
    } else {
      rowStatus(row, label || 'Done', false);
      setTimeout(function () { finalizeResolved(row); }, 450);
    }
  }

  function failRow(row, label) {
    rowStatus(row, label || 'Error', true);
    setBusy(row, false);
  }

  function setForgotPunchMode(row, enabled) {
    if (!row) return;
    ['.js-forgot-punch-time', '.js-forgot-wc', '.js-forgot-punch-save'].forEach(function (selector) {
      var el = row.querySelector(selector);
      if (el) el.hidden = !enabled;
    });
  }

  function submitRowInput(input, selector) {
    if (!input || input.hidden) return false;
    var row = input.closest('.exception-row');
    var btn = row && row.querySelector(selector);
    if (!btn || btn.disabled) return false;
    btn.click();
    return true;
  }

  function fallbackRowKey(row) {
    return [
      row.dataset.actionType || '',
      row.dataset.requestId || '',
      row.dataset.attendanceId || '',
      row.dataset.empId || '',
      row.dataset.wcName || '',
      row.dataset.startUtc || '',
    ].join(':');
  }

  function rowKey(row) {
    return row.dataset.rowKey || fallbackRowKey(row);
  }

  function currentSnapshotSignature() {
    var warning = document.querySelector('[data-source-warning]');
    var total = document.querySelector('[data-total-open]');
    var rows = Array.from(document.querySelectorAll('.exception-row')).map(function (row) {
      return (row.dataset.itemKey || '') + '#' + rowKey(row);
    });
    return [
      warning ? warning.dataset.sourceErrors || '' : '',
      total ? total.textContent.trim() : rows.length,
      rows.join(','),
    ].join('::');
  }

  function snapshotSignature(snapshot) {
    var errors = (snapshot.source_errors || []).map(function (err) {
      return err.source || '';
    }).join(',');
    var rows = (snapshot.queue || []).map(function (row) {
      var action = row.action || {};
      var key = row.row_key || [
        action.type || '',
        action.request_id || '',
        action.attendance_id || '',
        action.emp_id || '',
        action.wc_name || '',
        action.start_utc || '',
      ].join(':');
      return (row.item_key || '') + '#' + key;
    });
    return [errors, snapshot.total, rows.join(',')].join('::');
  }

  function hasInlineWorkInProgress() {
    if (attendanceCorrectionDialog && attendanceCorrectionDialog.open) return true;
    var active = document.activeElement;
    if (active && active.closest && active.closest('.row-actions')) return true;
    if (document.querySelector('.exception-row.is-error')) return true;
    return Array.from(document.querySelectorAll('.row-actions input, .row-actions select')).some(function (el) {
      return !el.disabled && !!String(el.value || '').trim();
    });
  }

  function showRefreshNotice() {
    var notice = document.querySelector('[data-refresh-notice]');
    if (notice) notice.hidden = false;
  }

  function rowMatchesFocus(row, mode) {
    if (mode === 'urgent') return row.dataset.priority === 'urgent';
    if (mode === 'followup') return row.dataset.priority === 'muted';
    return true;
  }

  function updateFocusEmpty(visibleRows) {
    var empty = document.querySelector('[data-focus-empty]');
    if (!empty) return;
    empty.hidden = visibleRows !== 0 || currentFocus === 'all';
  }

  function applyFocus(mode) {
    currentFocus = mode || 'all';
    document.querySelectorAll('[data-focus-mode]').forEach(function (btn) {
      btn.classList.toggle('active', btn.dataset.focusMode === currentFocus);
    });
    var visibleRows = 0;
    document.querySelectorAll('.exception-row').forEach(function (row) {
      var visible = rowMatchesFocus(row, currentFocus);
      row.hidden = !visible;
      if (visible) visibleRows += 1;
    });
    updateFocusEmpty(visibleRows);
    try { sessionStorage.setItem('exceptions_focus', currentFocus); } catch (e) {}
  }

  function pollFreshness() {
    if (document.hidden) return;
    fetchCompat('/api/exceptions', {headers: {'Accept': 'application/json'}})
      .then(function (r) { return r.json(); })
      .then(function (snapshot) {
        if (!snapshot || !snapshot.queue) return;
        if (snapshotSignature(snapshot) === currentSnapshotSignature()) return;
        if (hasInlineWorkInProgress()) {
          showRefreshNotice();
        } else {
          window.location.reload();
        }
      })
      .catch(function () {});
  }

  // ---- Archive --------------------------------------------------------------
  var archiveLoaded = false;
  var archiveNextBefore = null;
  var archiveKnownActors = {};

  function archiveEls() {
    return {
      toggle: document.querySelector('[data-archive-toggle]'),
      body: document.querySelector('[data-archive-body]'),
      groups: document.querySelector('[data-archive-groups]'),
      empty: document.querySelector('[data-archive-empty]'),
      more: document.querySelector('[data-archive-more]'),
      count: document.querySelector('[data-archive-count]'),
      actor: document.querySelector('[data-archive-actor]'),
      hideAuto: document.querySelector('[data-archive-hide-auto]'),
    };
  }

  function archiveQuery(extra) {
    var els = archiveEls();
    var params = [];
    if (els.hideAuto && !els.hideAuto.checked) params.push('include_auto=true');
    var actor = els.actor ? els.actor.value : '';
    if (actor) params.push('actor=' + encodeURIComponent(actor));
    if (extra) params.push(extra);
    return '/api/exceptions/archive' + (params.length ? '?' + params.join('&') : '');
  }

  function glyphFor(action) {
    if (action === 'deny') return {text: '✗', cls: 'bad'};
    if (action === 'dismiss') return {text: '–', cls: 'muted'};
    if (action === 'auto_resolved') return {text: '↻', cls: 'muted'};
    return {text: '✓', cls: 'ok'};
  }

  function defaultOutcome(action) {
    if (action === 'approve') return 'Approved';
    if (action === 'deny') return 'Denied';
    if (action === 'dismiss') return 'Dismissed';
    if (action === 'correct') return 'Corrected';
    if (action === 'assign') return 'Assigned';
    if (action === 'absent') return 'Marked absent';
    if (action === 'auto_resolved') return 'Auto-resolved';
    return 'Resolved';
  }

  function rememberActor(event) {
    if (event.auto || !event.actor_upn) return;
    if (archiveKnownActors[event.actor_upn]) return;
    archiveKnownActors[event.actor_upn] = event.actor_name || event.actor_upn;
  }

  function syncActorOptions() {
    var els = archiveEls();
    if (!els.actor) return;
    var current = els.actor.value;
    var upns = Object.keys(archiveKnownActors).sort(function (a, b) {
      return archiveKnownActors[a].localeCompare(archiveKnownActors[b]);
    });
    els.actor.innerHTML = '';
    var everyone = document.createElement('option');
    everyone.value = '';
    everyone.textContent = 'Everyone';
    els.actor.appendChild(everyone);
    upns.forEach(function (upn) {
      var opt = document.createElement('option');
      opt.value = upn;
      opt.textContent = archiveKnownActors[upn];
      if (upn === current) opt.selected = true;
      els.actor.appendChild(opt);
    });
  }

  function renderArchiveEvent(event) {
    var row = document.createElement('div');
    row.className = 'archive-event' + (event.auto ? ' is-auto' : '');

    var glyph = glyphFor(event.action);
    var glyphEl = document.createElement('span');
    glyphEl.className = 'archive-glyph ' + glyph.cls;
    glyphEl.setAttribute('aria-hidden', 'true');
    glyphEl.textContent = glyph.text;
    row.appendChild(glyphEl);

    var main = document.createElement('div');
    main.className = 'archive-event-main';

    var head = document.createElement('div');
    head.className = 'archive-event-head';
    if (event.person_name) {
      var name = document.createElement('span');
      name.className = 'archive-event-name';
      name.textContent = event.person_name;
      head.appendChild(name);
    }
    if (event.category_label) {
      var tag = document.createElement('span');
      tag.className = 'category-tag tone-info';
      tag.textContent = event.category_label;
      head.appendChild(tag);
    }
    main.appendChild(head);

    var outcome = document.createElement('div');
    outcome.className = 'archive-event-outcome';
    var by = event.auto ? 'auto-resolved' : (event.actor_name || event.actor_upn || 'unknown');
    var text = (event.outcome || defaultOutcome(event.action)) + ' by ' + by;
    if (event.before_value) text += ' (was ' + event.before_value + ')';
    outcome.textContent = text;
    if (event.reason) {
      var reason = document.createElement('span');
      reason.className = 'archive-event-reason';
      reason.textContent = ' “' + event.reason + '”';
      outcome.appendChild(reason);
    }
    main.appendChild(outcome);
    row.appendChild(main);

    var time = document.createElement('span');
    time.className = 'archive-event-time';
    time.textContent = event.time_label || '';
    row.appendChild(time);
    return row;
  }

  function renderArchiveGroups(groups, append) {
    var els = archiveEls();
    if (!els.groups) return;
    if (!append) els.groups.innerHTML = '';
    groups.forEach(function (group) {
      var dayEl = document.createElement('div');
      dayEl.className = 'archive-day';
      var label = document.createElement('p');
      label.className = 'archive-day-label';
      label.textContent = group.label || group.day;
      dayEl.appendChild(label);
      var list = document.createElement('div');
      list.className = 'archive-list';
      (group.events || []).forEach(function (event) {
        rememberActor(event);
        list.appendChild(renderArchiveEvent(event));
      });
      dayEl.appendChild(list);
      els.groups.appendChild(dayEl);
    });
    syncActorOptions();
    var hasAny = !!els.groups.querySelector('.archive-event');
    if (els.empty) els.empty.hidden = hasAny;
  }

  function fetchArchive(before, append) {
    var els = archiveEls();
    return fetchCompat(archiveQuery(before ? 'before=' + encodeURIComponent(before) : ''), {
      headers: {'Accept': 'application/json'},
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data) return;
        archiveNextBefore = data.next_before || null;
        renderArchiveGroups(data.groups || [], append);
        if (els.more) els.more.hidden = !archiveNextBefore;
      })
      .catch(function () {});
  }

  function reloadArchive() {
    archiveKnownActors = {};
    var els = archiveEls();
    if (els.actor && els.actor.value && els.actor.value !== '') {
      // keep the selected actor visible even if it has no events this fetch
      archiveKnownActors[els.actor.value] = els.actor.options[els.actor.selectedIndex].textContent;
    }
    fetchArchive(null, false);
  }

  function toggleArchive() {
    var els = archiveEls();
    if (!els.toggle || !els.body) return;
    var open = els.body.hidden;
    els.body.hidden = !open;
    els.toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && !archiveLoaded) {
      archiveLoaded = true;
      fetchArchive(null, false);
    }
  }

  document.addEventListener('click', function (event) {
    var refreshBtn = event.target.closest('[data-refresh-now]');
    if (refreshBtn) {
      event.preventDefault();
      window.location.reload();
      return;
    }

    var focusBtn = event.target.closest('[data-focus-mode]');
    if (focusBtn) {
      event.preventDefault();
      applyFocus(focusBtn.dataset.focusMode || 'all');
      return;
    }

    var archiveToggle = event.target.closest('[data-archive-toggle]');
    if (archiveToggle) {
      event.preventDefault();
      toggleArchive();
      return;
    }

    var archiveMore = event.target.closest('[data-archive-more]');
    if (archiveMore) {
      event.preventDefault();
      if (archiveNextBefore) fetchArchive(archiveNextBefore, true);
      return;
    }

    var btn = event.target.closest('[data-alert-open]');
    if (btn) {
      event.preventDefault();
      openAlert(btn.getAttribute('data-alert-open'));
      return;
    }

    var rowBtn = event.target.closest('.row-btn');
    if (!rowBtn) return;
    var row = rowBtn.closest('.exception-row');
    if (!row) return;
    var personName = row.dataset.personName || (row.querySelector('.exception-name') ? row.querySelector('.exception-name').textContent.trim() : '');
    var attendanceId = asInt(row.dataset.attendanceId);
    var empId = row.dataset.empId || '';
    var incidentId = row.dataset.incidentId;
    var breakdownWc = row.dataset.wcName;
    var employeeOdooId = asInt(row.dataset.employeeOdooId);

    if (rowBtn.classList.contains('js-assign')) {
      var person = row.querySelector('.js-person').value;
      if (!person) {
        failRow(row, 'Pick a person.');
        return;
      }
      setBusy(row, true);
      rowStatus(row, 'Saving...', false);
      postJson('/api/staffing/attribute', {
        day: row.dataset.day,
        wc_name: row.dataset.wcName,
        person_name: person,
        start_utc: row.dataset.startUtc,
        source: 'inbox',
      }).then(function (resp) {
        if (resp && resp.ok) {
          resolveRow(row, 'Assigned', resp.event_id);
          if (window.gpiTransferToast) window.gpiTransferToast(resp.transfer);
        } else {
          failRow(row, (resp && resp.error) || 'Assignment failed.');
        }
      }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }

    if (rowBtn.classList.contains('js-running-late')) {
      if (!empId || !personName) {
        failRow(row, 'Missing employee id.');
        return;
      }
      setBusy(row, true);
      rowStatus(row, 'Saving running late...', false);
      postJson('/api/late-report/running-late', {
        emp_id: empId,
        name: personName,
      }).then(function (resp) {
        if (resp && resp.ok) resolveRow(row, 'Re-checks in 60 min', resp.event_id);
        else failRow(row, (resp && resp.error) || 'Save failed.');
      }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }

    if (rowBtn.classList.contains('js-absent')) {
      if (!empId || !personName) {
        failRow(row, 'Missing employee id.');
        return;
      }
      setBusy(row, true);
      rowStatus(row, 'Saving absence...', false);
      postJson('/api/late-report/declare-absent', {
        emp_id: empId,
        name: personName,
      }).then(function (resp) {
        if (resp && resp.ok) resolveRow(row, 'Marked absent', resp.event_id);
        else {
          failRow(row, (resp && resp.error) || 'Save failed.');
        }
      }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }

    if (rowBtn.matches('.js-forgot-punch-open')) {
      setForgotPunchMode(row, true);
      rowStatus(row, 'Missed punch: enter time and work center.', false);
      var punchTimeInput = row.querySelector('.js-forgot-punch-time');
      if (punchTimeInput) punchTimeInput.focus();
      return;
    }

    if (rowBtn.classList.contains('js-forgot-punch-save')) {
      if (!empId || !personName) {
        failRow(row, 'Missing employee id.');
        return;
      }
      var forgotTime = row.querySelector('.js-forgot-punch-time').value;
      var forgotWc = row.querySelector('.js-forgot-wc').value;
      if (!forgotTime || !forgotWc) {
        failRow(row, forgotTime ? 'Pick a work center.' : 'Enter a clock-in time.');
        return;
      }
      setBusy(row, true);
      rowStatus(row, 'Clocking in...', false);
      postJson('/api/late-report/forgot-punch-in', {
        emp_id: empId,
        name: personName,
        time: forgotTime,
        wc_name: forgotWc,
      }).then(function (resp) {
        if (resp && resp.ok) resolveRow(row, 'Clocked in');
        else failRow(row, (resp && resp.error) || 'Clock in failed.');
      }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }

    if (rowBtn.classList.contains('js-missing-wc-save')) {
      var wc = row.querySelector('.js-wc').value;
      if (!attendanceId || !wc) {
        failRow(row, wc ? 'Missing attendance id.' : 'Pick a work center.');
        return;
      }
      setBusy(row, true);
      rowStatus(row, 'Assigning...', false);
      postJson('/missing-wc/assign', {
        attendance_id: attendanceId,
        wc_name: wc,
        name: personName,
      }).then(function (resp) {
        if (resp && resp.ok) resolveRow(row, 'Assigned', resp.event_id);
        else failRow(row, (resp && resp.error) || 'Assign failed.');
      }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }

    if (rowBtn.classList.contains('js-missing-wc-dismiss')) {
      if (!attendanceId) {
        failRow(row, 'Missing attendance id.');
        return;
      }
      setBusy(row, true);
      rowStatus(row, 'Dismissing...', false);
      postJson('/missing-wc/dismiss', {
        attendance_id: attendanceId,
        name: personName,
      }).then(function (resp) {
        if (resp && resp.ok) resolveRow(row, 'Dismissed', resp.event_id);
        else failRow(row, (resp && resp.error) || 'Dismiss failed.');
      }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }

    if (rowBtn.classList.contains('js-breakdown-transfer')) {
      var toWc = row.querySelector('.js-wc').value;
      if (!incidentId || !toWc) {
        failRow(row, toWc ? 'Missing incident id.' : 'Pick a work center.');
        return;
      }
      setBusy(row, true);
      rowStatus(row, 'Transferring...', false);
      postJson('/api/exceptions/breakdown/transfer', {
        incident_id: incidentId,
        person_name: personName,
        employee_odoo_id: employeeOdooId,
        to_wc: toWc,
      }).then(function (resp) {
        if (resp && resp.ok) resolveRow(row, 'Transferred', resp.event_id);
        else failRow(row, (resp && resp.error) || 'Transfer failed.');
      }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }

    if (rowBtn.classList.contains('js-breakdown-snooze')) {
      if (!incidentId || !personName) {
        failRow(row, 'Missing incident id.');
        return;
      }
      setBusy(row, true);
      rowStatus(row, 'Snoozing...', false);
      postJson('/api/exceptions/breakdown/snooze', {
        incident_id: incidentId,
        person_name: personName,
        employee_odoo_id: employeeOdooId,
      }).then(function (resp) {
        if (resp && resp.ok) resolveRow(row, 'Snoozed');
        else failRow(row, (resp && resp.error) || 'Snooze failed.');
      }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }

    if (rowBtn.classList.contains('js-breakdown-dismiss')) {
      if (!incidentId) {
        failRow(row, 'Missing incident id.');
        return;
      }
      setBusy(row, true);
      rowStatus(row, 'Dismissing...', false);
      postJson('/api/exceptions/breakdown/dismiss', {
        incident_id: incidentId,
      }).then(function (resp) {
        if (resp && resp.ok) resolveRow(row, 'Not a breakdown', resp.event_id);
        else failRow(row, (resp && resp.error) || 'Dismiss failed.');
      }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }

    if (rowBtn.classList.contains('js-punch-save')) {
      var time = row.querySelector('.js-punch-time').value;
      if (!attendanceId || !time) {
        failRow(row, time ? 'Missing attendance id.' : 'Enter a time.');
        return;
      }
      setBusy(row, true);
      rowStatus(row, 'Correcting...', false);
      postJson('/missed-punch-out/correct', {
        attendance_id: attendanceId,
        time: time,
      }).then(function (resp) {
        if (resp && resp.ok) resolveRow(row, (resp && resp.message) || 'Corrected');
        else failRow(row, (resp && resp.error) || 'Correction failed.');
      }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }

    if (rowBtn.classList.contains('js-time-off-approve')) {
      setBusy(row, true);
      rowStatus(row, 'Approving...', false);
      postJson('/api/exceptions/time-off/' + encodeURIComponent(row.dataset.requestId) + '/approve', {
        source: 'inbox',
      })
        .then(function (resp) {
          if (resp && resp.ok && resp.approved === false) {
            rowStatus(row, 'Moved forward; refreshing...', false);
            setTimeout(function () { window.location.reload(); }, 600);
          } else if (resp && resp.ok) {
            resolveRow(row, resp.recorded_locally
              ? 'Approved — recorded here (Odoo schedule conflict)'
              : 'Approved');
          } else {
            failRow(row, (resp && resp.error) || 'Approval failed.');
          }
        }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }

    if (rowBtn.classList.contains('js-time-off-refuse')) {
      var reasonInput = row.querySelector('.js-time-off-reason');
      if (reasonInput && reasonInput.hidden) {
        reasonInput.hidden = false;
        reasonInput.focus();
        rowStatus(row, 'Enter a reason, then Deny again.', false);
        return;
      }
      var denyReason = reasonInput ? reasonInput.value.trim() : '';
      if (!denyReason) {
        if (reasonInput) reasonInput.focus();
        failRow(row, 'A reason is required to deny.');
        return;
      }
      setBusy(row, true);
      rowStatus(row, 'Denying...', false);
      postJson('/api/exceptions/time-off/' + encodeURIComponent(row.dataset.requestId) + '/refuse', {
        reason: denyReason,
        source: 'inbox',
      })
        .then(function (resp) {
          if (resp && resp.ok) resolveRow(row, 'Denied');
          else failRow(row, (resp && resp.error) || 'Deny failed.');
        }).catch(function () { failRow(row, 'Network error.'); });
    }
  });

  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter') return;
    if (!event.target || !event.target.closest) return;
    var input = event.target.closest('.js-time-off-reason');
    if (submitRowInput(input, '.js-time-off-refuse')) {
      event.preventDefault();
      return;
    }
    input = event.target.closest('.js-forgot-punch-time');
    if (submitRowInput(input, '.js-forgot-punch-save')) {
      event.preventDefault();
      return;
    }
    input = event.target.closest('.js-punch-time');
    if (submitRowInput(input, '.js-punch-save')) event.preventDefault();
  });

  document.addEventListener('change', function (event) {
    if (event.target.closest('[data-archive-actor]') || event.target.closest('[data-archive-hide-auto]')) {
      reloadArchive();
    }
  });

  // Coverage chip: hover shows the tooltip on desktop (CSS); on touch, a tap
  // toggles it open and a tap elsewhere closes it.
  document.addEventListener('click', function (event) {
    var wrap = event.target.closest('[data-cov]');
    document.querySelectorAll('[data-cov].cov-open').forEach(function (open) {
      if (open !== wrap) open.classList.remove('cov-open');
    });
    if (wrap) {
      event.stopPropagation();
      wrap.classList.toggle('cov-open');
    }
  });

  // Manual "+ Report a breakdown" control (page header, not a row action).
  var reportBreakdownBtn = document.querySelector('.js-report-breakdown');
  if (reportBreakdownBtn) {
    reportBreakdownBtn.addEventListener('click', function () {
      var select = document.querySelector('.js-report-breakdown-wc');
      var wcName = select ? select.value : '';
      if (!wcName) {
        if (select) select.focus();
        return;
      }
      reportBreakdownBtn.disabled = true;
      postJson('/api/exceptions/breakdown/report', {wc_name: wcName})
        .then(function (resp) {
          reportBreakdownBtn.disabled = false;
          if (resp && resp.ok) {
            window.location.reload();
          }
        })
        .catch(function () {
          reportBreakdownBtn.disabled = false;
        });
    });
  }

  try { currentFocus = sessionStorage.getItem('exceptions_focus') || 'all'; } catch (e) {}
  initAttendanceCorrection();
  applyFocus(currentFocus);
  updateQueueEmpty();
  window.setInterval(pollFreshness, POLL_MS);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) pollFreshness();
  });
})();
