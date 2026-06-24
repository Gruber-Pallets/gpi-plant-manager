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
            return Promise.resolve(responseText ? JSON.parse(responseText) : {});
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

  function bumpCounts(sectionId, delta) {
    var section = document.getElementById(sectionId);
    var sectionCount = section && section.querySelector('.section-open-count');
    if (sectionCount) sectionCount.textContent = countElText(sectionCount) + delta;

    var tileCount = document.querySelector('[data-summary-id="' + sectionId + '"] .summary-count');
    if (tileCount) tileCount.textContent = countElText(tileCount) + delta;

    var total = document.querySelector('[data-total-open]');
    if (total) total.textContent = countElText(total) + delta;
  }

  function bumpUrgent(row, delta) {
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
    else if (actionType === 'late_absence' || actionType === 'late_reason') badgeKey = 'late';
    else if (actionType === 'missing_wc') badgeKey = 'missing_wc';
    else if (actionType === 'missed_punch_out') badgeKey = 'missed_punch_out';
    if (!badgeKey) return;
    var api = window.gpiAlertBadges && window.gpiAlertBadges[badgeKey];
    if (api && typeof api.refreshCount === 'function') api.refreshCount();
  }

  function refreshInboxSummary() {
    if (typeof window.gpiRefreshInboxSummary === 'function') window.gpiRefreshInboxSummary();
  }

  function removeResolvedRow(row) {
    var section = row.closest('.inbox-section');
    var tbody = row.parentElement;
    row.remove();
    if (tbody && !tbody.querySelector('.exception-row') && section) {
      var table = section.querySelector('table');
      var empty = document.createElement('div');
      empty.className = 'empty-row';
      empty.textContent = 'All clear';
      if (table) table.replaceWith(empty);
    }
  }

  function resolveRow(row, label) {
    rowStatus(row, label || 'Done', false);
    setBusy(row, true);
    row.classList.add('is-resolved');
    bumpCounts(row.dataset.sectionId, -1);
    bumpUrgent(row, -1);
    bumpFocusCounts(row, -1);
    refreshSharedBadge(row);
    refreshInboxSummary();
    setTimeout(function () {
      removeResolvedRow(row);
      applyFocus(currentFocus);
    }, 450);
  }

  function failRow(row, label) {
    rowStatus(row, label || 'Error', true);
    setBusy(row, false);
  }

  function requireReason(row) {
    var input = row.querySelector('.js-reason-input');
    var reason = input ? input.value.trim() : '';
    if (!reason) {
      if (input) input.focus();
      failRow(row, 'Reason required.');
      return null;
    }
    return reason;
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
    var sectionParts = Array.from(document.querySelectorAll('.inbox-section')).map(function (section) {
      var rows = Array.from(section.querySelectorAll('.exception-row')).map(function (row) {
        return rowKey(row);
      });
      var count = section.querySelector('.section-open-count');
      return [
        section.id,
        count ? count.textContent.trim() : rows.length,
        rows.join(','),
      ].join('|');
    });
    return (warning ? warning.dataset.sourceErrors || '' : '') + '::' + sectionParts.join('||');
  }

  function snapshotSignature(snapshot) {
    var errors = (snapshot.source_errors || []).map(function (err) {
      return err.source || '';
    }).join(',');
    var sections = (snapshot.sections || []).map(function (section) {
      var rows = (section.rows || []).map(function (row) {
        var action = row.action || {};
        return row.row_key || [
          action.type || '',
          action.request_id || '',
          action.attendance_id || '',
          action.emp_id || '',
          action.wc_name || '',
          action.start_utc || '',
        ].join(':');
      });
      return [section.id, section.count, rows.join(',')].join('|');
    }).join('||');
    return errors + '::' + sections;
  }

  function hasInlineWorkInProgress() {
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
    document.querySelectorAll('.inbox-section').forEach(function (section) {
      var visibleInSection = 0;
      section.querySelectorAll('.exception-row').forEach(function (row) {
        var visible = rowMatchesFocus(row, currentFocus);
        row.hidden = !visible;
        if (visible) {
          visibleRows += 1;
          visibleInSection += 1;
        }
      });
      section.classList.toggle('is-filter-hidden', currentFocus !== 'all' && visibleInSection === 0);
    });
    updateFocusEmpty(visibleRows);
    try { sessionStorage.setItem('exceptions_focus', currentFocus); } catch (e) {}
  }

  function pollFreshness() {
    if (document.hidden) return;
    fetchCompat('/api/exceptions', {headers: {'Accept': 'application/json'}})
      .then(function (r) { return r.json(); })
      .then(function (snapshot) {
        if (!snapshot || !snapshot.sections) return;
        if (snapshotSignature(snapshot) === currentSnapshotSignature()) return;
        if (hasInlineWorkInProgress()) {
          showRefreshNotice();
        } else {
          window.location.reload();
        }
      })
      .catch(function () {});
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
    var personName = row.dataset.personName || row.querySelector('th').textContent.trim();
    var attendanceId = asInt(row.dataset.attendanceId);
    var empId = row.dataset.empId || '';

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
      }).then(function (resp) {
        if (resp && resp.ok) {
          resolveRow(row, 'Assigned');
          if (window.gpiTransferToast) window.gpiTransferToast(resp.transfer);
        } else {
          failRow(row, (resp && resp.error) || 'Assignment failed.');
        }
      }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }

    if (rowBtn.classList.contains('js-snooze')) {
      if (!empId || !personName) {
        failRow(row, 'Missing employee id.');
        return;
      }
      setBusy(row, true);
      rowStatus(row, 'Snoozing...', false);
      postJson('/api/late-report/snooze', {
        emp_id: empId,
        name: personName,
        minutes: 30,
      }).then(function (resp) {
        if (resp && resp.ok) resolveRow(row, 'Snoozed');
        else failRow(row, (resp && resp.error) || 'Snooze failed.');
      }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }

    if (rowBtn.classList.contains('js-absent')) {
      var absentReason = requireReason(row);
      if (!absentReason) return;
      setBusy(row, true);
      rowStatus(row, 'Saving absence...', false);
      postJson('/api/late-report/declare-absent', {
        emp_id: empId,
        name: personName,
        reason: absentReason,
      }).then(function (resp) {
        if (resp && resp.ok) resolveRow(row, 'Marked absent');
        else failRow(row, (resp && resp.error) || 'Save failed.');
      }).catch(function () { failRow(row, 'Network error.'); });
      return;
    }

    if (rowBtn.classList.contains('js-save-late')) {
      var lateReason = requireReason(row);
      if (!lateReason) return;
      setBusy(row, true);
      rowStatus(row, 'Saving reason...', false);
      postJson('/api/late-report/save-late-arrival', {
        emp_id: empId,
        name: personName,
        reason: lateReason,
      }).then(function (resp) {
        if (resp && resp.ok) resolveRow(row, 'Reason saved');
        else failRow(row, (resp && resp.error) || 'Save failed.');
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
        if (resp && resp.ok) resolveRow(row, 'Assigned');
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
        if (resp && resp.ok) resolveRow(row, 'Dismissed');
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
        if (resp && resp.ok) resolveRow(row, 'Corrected');
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
            resolveRow(row, 'Approved');
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

  document.addEventListener('change', function (event) {
    var preset = event.target.closest('.js-reason-preset');
    if (!preset) return;
    var row = preset.closest('.exception-row');
    var input = row && row.querySelector('.js-reason-input');
    if (!input) return;
    if (preset.value) input.value = preset.value;
    input.focus();
  });

  try { currentFocus = sessionStorage.getItem('exceptions_focus') || 'all'; } catch (e) {}
  applyFocus(currentFocus);
  window.setInterval(pollFreshness, POLL_MS);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) pollFreshness();
  });
})();
