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

  function resolveRow(row, label) {
    rowStatus(row, label || 'Done', false);
    setBusy(row, true);
    row.classList.add('is-resolved');
    bumpTotal(-1);
    bumpUrgentInline(row, -1);
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
    var html = '<option value="">Everyone</option>';
    upns.forEach(function (upn) {
      var selected = upn === current ? ' selected' : '';
      html += '<option value="' + upn + '"' + selected + '>' + archiveKnownActors[upn] + '</option>';
    });
    els.actor.innerHTML = html;
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

  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter') return;
    if (!event.target || !event.target.closest) return;
    var input = event.target.closest('.js-time-off-reason');
    if (submitRowInput(input, '.js-time-off-refuse')) {
      event.preventDefault();
      return;
    }
    input = event.target.closest('.js-reason-input');
    if (submitRowInput(input, '.js-absent, .js-save-late')) {
      event.preventDefault();
      return;
    }
    input = event.target.closest('.js-punch-time');
    if (submitRowInput(input, '.js-punch-save')) event.preventDefault();
  });

  document.addEventListener('change', function (event) {
    var preset = event.target.closest('.js-reason-preset');
    if (preset) {
      var row = preset.closest('.exception-row');
      var input = row && row.querySelector('.js-reason-input');
      if (!input) return;
      if (preset.value) input.value = preset.value;
      input.focus();
      return;
    }

    if (event.target.closest('[data-archive-actor]') || event.target.closest('[data-archive-hide-auto]')) {
      reloadArchive();
    }
  });

  try { currentFocus = sessionStorage.getItem('exceptions_focus') || 'all'; } catch (e) {}
  applyFocus(currentFocus);
  updateQueueEmpty();
  window.setInterval(pollFreshness, POLL_MS);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) pollFreshness();
  });
})();
