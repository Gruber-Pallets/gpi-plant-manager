  // Day picker: jump to another day on change (no Load button needed).
  const __dayPicker = document.getElementById('day-picker');
  if (__dayPicker) {
    __dayPicker.addEventListener('change', (e) => {
      const v = e.target.value;
      if (v) location.href = '/staffing?day=' + encodeURIComponent(v);
    });
    // Click anywhere in the input box opens the calendar (not just the icon).
    __dayPicker.addEventListener('click', () => {
      if (typeof __dayPicker.showPicker === 'function') {
        __dayPicker.showPicker();
      }
    });
  }

  // ---------- Per-WC training checkbox ----------
  function setWcTraining(loc, on) {
    const dd = document.querySelector('details.sched-dd[data-loc="' + CSS.escape(loc) + '"]');
    if (dd) dd.dataset.training = on ? '1' : '';
    const cb = document.querySelector('.wc-training-cb[data-loc="' + CSS.escape(loc) + '"]');
    if (cb) cb.checked = !!on;
  }
  document.querySelectorAll('.wc-training-cb').forEach(cb => {
    cb.addEventListener('change', (e) => setWcTraining(cb.dataset.loc, e.target.checked));
  });

  // ---------- Posted schedule lock / Edit gate ----------
  const __isPublished = !!window.SCHEDULE_PUBLISHED;
  const __viewingPosted = !!window.SCHEDULE_VIEWING_POSTED;
  const __form = document.getElementById('staffing-form');
  let __unlocked = !__isPublished && !__viewingPosted;
  if (__isPublished || __viewingPosted) {
    __form.classList.add('locked');
  }

  // Wake the autosave controller after a programmatic DOM mutation (pill
  // remove, checkbox toggled by code, options stripped from a select, etc.).
  // The controller listens for `input`/`change` events on the form, but
  // assigning to .checked or removing nodes doesn't fire those — dispatch a
  // synthetic, bubbling change so autosave is triggered exactly once.
  function kickAutosave() {
    __form.dispatchEvent(new Event('change', { bubbles: true }));
  }

  // ---------- Reset to defaults ----------
  // Replaces every Scheduled cell using the page-rendered default map: smart
  // defaults first, with stored Work Center defaults as the route fallback.
  // These values are not editable from the scheduler.
  const __defaultsByLoc = window.SMART_DEFAULTS_BY_LOC || window.DEFAULTS_BY_LOC;
  const __clearBtn = document.getElementById('clear-schedule-btn');
  if (__clearBtn) {
    __clearBtn.addEventListener('click', () => {
      if (__isPublished && !__unlocked) {
        alert("This schedule is Posted. Click Edit first if you need to reset it.");
        return;
      }
      if (!confirm("Reset every Scheduled cell to the page defaults?\n\n(Time off and notes stay. Anyone manually scheduled is replaced with the defaults shown on this page.)")) return;
      document.querySelectorAll('details.sched-dd').forEach(dd => {
        const wanted = new Set(__defaultsByLoc[dd.dataset.loc] || []);
        dd.querySelectorAll('.dd-item').forEach(item => {
          const cb = item.querySelector('input[type=checkbox]');
          if (!cb) return;
          const should = wanted.has(item.dataset.name);
          cb.checked = should;
          item.classList.toggle('selected', should);
        });
        updateDdSummary(dd);
        __prevSel.set(dd, [...dd.querySelectorAll('.dd-item.selected')].map(i => i.dataset.name));
      });
      syncLeftRailWithSchedule();
      refreshPickerVisibility();
      kickAutosave();
    });
  }

  // ---------- Undo / Redo helpers ----------

  function serializeForm(form) {
    const map = {};
    for (const [k, v] of new FormData(form).entries()) {
      if (!(k in map)) map[k] = [];
      map[k].push(v);
    }
    return map;
  }

  function applyState(form, snap) {
    // Reset every input's value/checked to the snapshot.
    for (const el of form.querySelectorAll('input, select, textarea')) {
      if (!el.name) continue;
      const vals = snap[el.name] || [];
      if (el.type === 'checkbox' || el.type === 'radio') {
        el.checked = vals.includes(el.value);
      } else if (el instanceof HTMLSelectElement) {
        if (el.multiple) {
          Array.from(el.options).forEach(o => { o.selected = vals.includes(o.value); });
        } else {
          el.value = vals[0] ?? '';
        }
      } else {
        el.value = vals[0] ?? '';
      }
    }
  }

  let __undoSnapshot = serializeForm(__form);
  // One-shot undo/redo snapshots, shared by the title-bar buttons and the toast.
  // After autosave: __lastUndoSnap = pre-save state, __lastRedoSnap cleared.
  // After undo:     __lastUndoSnap = null, __lastRedoSnap = pre-undo state.
  // After redo:     __lastUndoSnap = pre-redo state, __lastRedoSnap = null.
  let __lastUndoSnap = null;
  let __lastRedoSnap = null;
  let __saving = false;

  const __undoBtn = document.getElementById('undo-btn');
  const __redoBtn = document.getElementById('redo-btn');
  function updateUndoRedoBtns() {
    if (__undoBtn) __undoBtn.disabled = !__lastUndoSnap || __viewingPosted;
    if (__redoBtn) __redoBtn.disabled = !__lastRedoSnap || __viewingPosted;
  }

  // Sync DOM that's normally driven by the click handler back from form state.
  // Called after applyState() reverts the form so the picker pills, summaries,
  // training toggles, and cross-picker visibility match the new checkbox state.
  function reapplyVisualState() {
    document.querySelectorAll('details.multi-dd .dd-item').forEach(item => {
      const cb = item.querySelector('input[type=checkbox]');
      if (cb) item.classList.toggle('selected', cb.checked);
    });
    document.querySelectorAll('details.multi-dd').forEach(dd => updateDdSummary(dd));
    document.querySelectorAll('.wc-training-cb').forEach(cb => {
      const dd = document.querySelector('details.sched-dd[data-loc="' + CSS.escape(cb.dataset.loc) + '"]');
      if (dd) dd.dataset.training = cb.checked ? '1' : '';
    });
    const tdInput = document.getElementById('testing-day-input');
    const tdPill = document.getElementById('testing-pill');
    if (tdInput && tdPill) tdPill.style.display = (tdInput.value === '1') ? '' : 'none';
    refreshPickerVisibility();
  }

  // Defaults are managed in Settings → Work Centers. The scheduler's autosave
  // never sends `defaults_dirty__` markers, so the server skips writing
  // default_people during this page's saves — exactly what we want.
  function __buildAutosaveBody() { return new FormData(__form); }

  function performUndo(snap) {
    if (!snap || __saving || __viewingPosted) return;
    const beforeRevert = serializeForm(__form);
    applyState(__form, snap);
    reapplyVisualState();
    __saving = true;
    const body = __buildAutosaveBody();
    const url = __form.action + (__form.action.includes('?') ? '&' : '?') + 'auto=1';
    fetch(url, { method: 'POST', body, headers: { 'Accept': 'application/json' } })
      .finally(() => {
        __undoSnapshot = serializeForm(__form);
        __lastUndoSnap = null;
        __lastRedoSnap = beforeRevert;
        updateUndoRedoBtns();
        __saving = false;
        showSavedToast(null, 'Reverted');
      });
  }
  function performRedo(snap) {
    if (!snap || __saving || __viewingPosted) return;
    const beforeRedo = serializeForm(__form);
    applyState(__form, snap);
    reapplyVisualState();
    __saving = true;
    const body = __buildAutosaveBody();
    const url = __form.action + (__form.action.includes('?') ? '&' : '?') + 'auto=1';
    fetch(url, { method: 'POST', body, headers: { 'Accept': 'application/json' } })
      .finally(() => {
        __undoSnapshot = serializeForm(__form);
        __lastUndoSnap = beforeRedo;
        __lastRedoSnap = null;
        updateUndoRedoBtns();
        __saving = false;
        showSavedToast(null, 'Redone');
      });
  }
  if (__undoBtn) __undoBtn.addEventListener('click', () => performUndo(__lastUndoSnap));
  if (__redoBtn) __redoBtn.addEventListener('click', () => performRedo(__lastRedoSnap));
  updateUndoRedoBtns();

  function showSavedToast(undoSnap, errorMsg, successOverride) {
    let bd = document.getElementById('save-toast-bd');
    if (!bd) {
      bd = document.createElement('div');
      bd.id = 'save-toast-bd';
      bd.className = 'save-toast-bd';
      document.body.appendChild(bd);
    }
    const el = document.createElement('div');
    el.className = 'save-toast' + (errorMsg ? ' error' : '');
    const label = document.createElement('span');
    label.textContent = errorMsg || successOverride || 'Saved';
    el.appendChild(label);
    if (!errorMsg && undoSnap) {
      const u = document.createElement('button');
      u.type = 'button';
      u.className = 'undo-btn';
      u.textContent = 'Undo';
      u.onclick = () => { performUndo(undoSnap); el.remove(); };
      el.appendChild(u);
    }
    bd.appendChild(el);
    setTimeout(() => { el.classList.add('fade'); setTimeout(() => el.remove(), 300); }, 5000);
  }

  // Simple popup helper.
  function showPopup({title, msg, overrideLabel='Override', onOverride, onCancel}) {
    let bd = document.getElementById('popover-bd');
    if (!bd) {
      bd = document.createElement('div');
      bd.id = 'popover-bd';
      bd.className = 'popover-backdrop';
      bd.innerHTML =
        '<div class="popover">'
      + '  <h4 data-role="title"></h4>'
      + '  <p data-role="msg"></p>'
      + '  <div class="actions">'
      + '    <button type="button" class="primary" data-role="cancel">Cancel</button>'
      + '    <button type="button" class="override" data-role="override"></button>'
      + '  </div>'
      + '</div>';
      document.body.appendChild(bd);
    }
    bd.querySelector('[data-role=title]').textContent = title;
    bd.querySelector('[data-role=msg]').textContent = msg;
    bd.querySelector('[data-role=override]').textContent = overrideLabel;
    bd.classList.add('show');
    const cancel = bd.querySelector('[data-role=cancel]');
    const ovr = bd.querySelector('[data-role=override]');
    const close = () => bd.classList.remove('show');
    cancel.onclick = () => { close(); if (onCancel) onCancel(); };
    ovr.onclick    = () => { close(); if (onOverride) onOverride(); };
  }

  function countChecked(picker) {
    return picker.querySelectorAll('input[type=checkbox]:checked').length;
  }

  function erlangCWaitSeconds(c, lambdaPerHr, meanHandleSeconds) {
    if (c < 1 || lambdaPerHr <= 0 || meanHandleSeconds <= 0) return 0;
    const mu = 3600 / meanHandleSeconds;
    const a = lambdaPerHr / mu;
    if (c <= a) return Infinity;
    let summ = 0;
    let term = 1;
    for (let k = 0; k < c; k += 1) {
      if (k > 0) term *= a / k;
      summ += term;
    }
    const acOverCfact = term * (a / c);
    const top = acOverCfact * (c / (c - a));
    const pWait = top / (summ + top);
    const wqHours = pWait / (c * mu - lambdaPerHr);
    return wqHours * 3600;
  }

  function forkliftStatusForPrediction(predictedSeconds, targetSeconds, overloaded) {
    if (overloaded || predictedSeconds == null) return 'danger';
    if (predictedSeconds <= targetSeconds) return 'ok';
    if (predictedSeconds <= targetSeconds * 1.5) return 'warn';
    return 'danger';
  }

  function countScheduledForkliftDrivers(model) {
    const names = new Set();
    (model.driver_wc_names || []).forEach(loc => {
      const dd = document.querySelector('details.sched-dd[data-loc="' + CSS.escape(loc) + '"]');
      if (!dd) return;
      dd.querySelectorAll('input[type=checkbox]:checked').forEach(cb => names.add(cb.value));
    });
    return names.size;
  }

  function recalcForkliftBaySummary() {
    const model = window.FORKLIFT_LIVE_MODEL || {};
    if (!model.available) return;
    const summary = document.querySelector('.forklift-bay-summary');
    if (!summary) return;
    const suggested = summary.querySelector('.forklift-bay-suggested');
    const prediction = summary.querySelector('.forklift-bay-prediction');
    if (!prediction) return;

    if (suggested && model.recommended) {
      suggested.textContent = model.recommended + ' Suggested';
    }
    const scheduled = countScheduledForkliftDrivers(model);
    const raw = erlangCWaitSeconds(
      scheduled,
      Number(model.lambda_per_hr || 0),
      Number(model.mean_handle_seconds || 0)
    );
    const overloaded = scheduled < 1 || !Number.isFinite(raw);
    const predicted = overloaded ? null : Number(model.calibration_k || 1) * raw;
    const status = forkliftStatusForPrediction(
      predicted,
      Number(model.target_seconds || 0),
      overloaded
    );

    summary.classList.remove('ok', 'warn', 'danger');
    summary.classList.add(status);
    if (overloaded) {
      prediction.textContent = 'TTC overloaded';
      summary.title = 'Scheduled drivers overloaded';
    } else {
      const minutes = predicted / 60;
      prediction.textContent = 'Predicted Time-to-Claim ' + minutes.toFixed(1) + ' min';
      summary.title = 'Predicted scheduled time-to-claim: ' + minutes.toFixed(1) + ' min';
    }
  }

  function syncSummary(picker) {
    const loc = picker.dataset.loc;
    const pills = picker.querySelector(':scope > summary .pills');
    const count = countChecked(picker);
    picker.querySelector(':scope > summary .sched-count').textContent = count;
    if (!pills) return;
    pills.innerHTML = '';
    const checked = [...picker.querySelectorAll('input[type=checkbox]:checked')];
    if (!checked.length) {
      const s = document.createElement('span');
      s.className = 'pills-empty'; s.textContent = '—';
      pills.appendChild(s);
      return;
    }
    for (const cb of checked) {
      const level = cb.dataset.level || '2';
      const name = cb.value;
      const span = document.createElement('span');
      span.className = 'pill lvl-' + level;
      span.dataset.name = name;
      span.textContent = name;
      pills.appendChild(span);
    }
  }

  function flagTestingDay() {
    const inp = document.getElementById('testing-day-input');
    inp.value = '1';
    const pill = document.getElementById('testing-pill');
    if (pill) pill.style.display = '';
    // Programmatic value mutation doesn't fire change/input on its own.
    kickAutosave();
  }

  // × inside the Testing Day pill: hits the dedicated clear endpoint
  // (the regular save path can't reach testing_day on a posted schedule
  // because save_notes deliberately preserves it). Syncs the hidden form
  // input so subsequent autosaves don't re-add the flag.
  (function() {
    const btn = document.getElementById('testing-pill-clear');
    if (!btn) return;
    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (!confirm('Clear the Testing Day flag for this schedule? Output for the day will count toward employees again.')) return;
      btn.style.opacity = '0.4';
      try {
        const resp = await fetch('/api/staffing/clear-testing-day', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({day: window.SCHEDULE_DAY}),
        });
        const j = await resp.json();
        if (j.ok) {
          const pill = document.getElementById('testing-pill');
          if (pill) pill.style.display = 'none';
          const inp = document.getElementById('testing-day-input');
          if (inp) inp.value = '0';
        } else {
          btn.style.opacity = '';
          alert('Clear failed: ' + (j.error || 'unknown'));
        }
      } catch (err) {
        btn.style.opacity = '';
        alert('Network error.');
      }
    });
  })();

  // ---------- Cross-picker visibility ----------
  // No one should appear in two Scheduled pickers, in a Scheduled picker if they're
  // off today, or in two Defaults pickers. Items stay visible in the picker that
  // already owns them (so the user can unselect there).
  function refreshPickerVisibility() {
    // Scheduled side: taken = scheduled anywhere ∪ legacy time-off form ∪
    // StratusTime-driven time off. The legacy `loc____time_off` hidden
    // inputs are only present for back-compat; `__timeOffNames` is the
    // current source of truth for who is off today.
    const schedTaken = new Set();
    document.querySelectorAll('details.sched-dd input[name^="loc__"]:checked').forEach(cb => schedTaken.add(cb.value));
    document.querySelectorAll('input[name="loc____time_off"]').forEach(inp => schedTaken.add(inp.value));
    __timeOffNames.forEach(n => schedTaken.add(n));
    document.querySelectorAll('details.sched-dd').forEach(dd => {
      const own = new Set();
      dd.querySelectorAll('.dd-item.selected').forEach(it => own.add(it.dataset.name));
      dd.querySelectorAll('.dd-item').forEach(it => {
        it.classList.toggle('taken-elsewhere', schedTaken.has(it.dataset.name) && !own.has(it.dataset.name));
      });
    });
    recalcForkliftBaySummary();

  }

  // Append the Recycled rotation "why" badge to a summary name span, mirroring
  // the server-rendered .rotation-reason pill. No-op unless the engine reported
  // a reason for this person at this work center (i.e. a generated placement),
  // so manually-placed people never get a spurious reason.
  function appendReasonBadge(parentEl, loc, name) {
    const reasons = (window.ROTATION_REASONS || {})[loc];
    const reason = reasons && reasons[name];
    if (!reason) return;
    const span = document.createElement('span');
    span.className = 'rotation-reason';
    span.title = reason;
    span.textContent = reason;
    parentEl.appendChild(span);
  }

  // ---------- Dropdown item click: toggles checkbox + selected class + summary ----------
  function updateDdSummary(dd) {
    const text = dd.querySelector('.dd-summary-text');
    if (!text) return;
    const items = [...dd.querySelectorAll('.dd-item.selected')];
    if (!items.length) {
      text.innerHTML = '<span class="empty">—</span>';
      return;
    }
    // Rebuild via DOM so we can attach cert + reason badges to each name span.
    text.innerHTML = '';
    items.forEach((it, idx) => {
      const lvl = it.dataset.level;
      const name = it.dataset.name;
      const span = document.createElement('span');
      span.className = 'lvl-' + (lvl || '2');
      span.appendChild(document.createTextNode(name));
      appendCertBadges(span, name);
      appendReasonBadge(span, dd.dataset.loc, name);
      text.appendChild(span);
      if (idx < items.length - 1) {
        const sep = document.createElement('span');
        sep.className = 'sep';
        sep.textContent = ', ';
        text.appendChild(sep);
      }
    });
  }

  // Track prior selection per Scheduled dropdown so we can revert an over-max attempt.
  const __prevSel = new WeakMap();
  document.querySelectorAll('details.sched-dd').forEach(dd => {
    __prevSel.set(dd, [...dd.querySelectorAll('.dd-item.selected')].map(i => i.dataset.name));
  });

  document.addEventListener('click', (e) => {
    const x = e.target.closest('.timeoff .pill-x');
    if (!x) return;
    e.preventDefault();
    const pill = x.closest('.pill');
    if (!pill) return;
    const name = pill.dataset.name;
    pill.remove();
    // Live-update the left rail: send them back to whichever list owns them.
    if (name) addBackToCorrectList(name);
    refreshTimeOffCount();
    kickAutosave();
  });

  // ---------- Time Off helpers ----------
  function refreshTimeOffCount() {
    const sect = document.querySelector('.section.timeoff');
    if (!sect) return;
    const count = sect.querySelectorAll('.pill').length;
    const cspan = sect.querySelector('h3 .count');
    if (cspan) cspan.textContent = count;
  }

  // Remove `name` from every Scheduled WC dropdown. Returns the list of WC
  // location names she was removed from (used to surface a toast).
  function removeFromAllScheduledWcs(name) {
    const removedFrom = [];
    document.querySelectorAll('details.sched-dd').forEach(dd => {
      const items = dd.querySelectorAll('.dd-item');
      let removedHere = false;
      items.forEach(item => {
        if (item.dataset.name !== name) return;
        const cb = item.querySelector('input[type=checkbox]');
        if (cb && cb.checked) {
          cb.checked = false;
          item.classList.remove('selected');
          removedHere = true;
        }
      });
      if (removedHere) {
        updateDdSummary(dd);
        __prevSel.set(dd, [...dd.querySelectorAll('.dd-item.selected')].map(i => i.dataset.name));
        removedFrom.push(dd.dataset.loc);
      }
    });
    return removedFrom;
  }

  // Remove `name` from the Unscheduled / Reserves left-rail lists if present.
  // The lists are rendered server-side, so client-side mutation is needed to
  // keep them in sync with autosaved state until the next page reload.
  function removeFromUnscheduled(name) {
    document.querySelectorAll('.section.unscheduled ul li').forEach(li => {
      if (li.dataset.name === name) li.remove();
    });
    const sect = document.querySelector('.section.unscheduled');
    if (sect) {
      const items = sect.querySelectorAll('ul li:not(.empty)');
      const cspan = sect.querySelector('h3 .count');
      if (cspan) cspan.textContent = items.length;
      const ul = sect.querySelector('ul');
      if (ul && items.length === 0 && !ul.querySelector('li.empty')) {
        const empty = document.createElement('li');
        empty.className = 'empty';
        empty.textContent = '— all scheduled —';
        ul.appendChild(empty);
      }
    }
  }
  function removeFromReserves(name) {
    document.querySelectorAll('.section.reserves ul li').forEach(li => {
      if (li.dataset.name === name) li.remove();
    });
    const sect = document.querySelector('.section.reserves');
    if (sect) {
      const items = sect.querySelectorAll('ul li:not(.empty)');
      const cspan = sect.querySelector('h3 .count');
      if (cspan) cspan.textContent = items.length;
    }
  }

  // Per-person metadata (reserve flag) for routing add-backs to the right list.
  const __peopleMeta = window.PEOPLE_META;

  // Partial-day off labels (name -> "arrives 11:30am" / "gone 10am–12pm" /
  // hours). Used to re-attach the amber "off" badge when a partial person is
  // moved back into Unscheduled/Reserves dynamically (e.g. after clearing
  // them from a WC), so the off note follows them without a full reload.
  const __partialRangeByName = window.PARTIAL_RANGE_BY_NAME;
  const __partialHoursByName = window.PARTIAL_HOURS_BY_NAME;

  // Names of people on time off today. Single source of truth for client-side
  // "is this person on time off?" checks — the Time Off section in the DOM is
  // rendered as <li.time-off-row> without a reliable data-name on every row,
  // so DOM selectors against `.section.timeoff` are brittle. This Set is
  // server-rendered from `time_off_names` and stays static for the page life.
  const __timeOffNames = new Set(window.TIME_OFF_NAMES);

  // Defensive sweep: if the server-rendered Unscheduled or Reserves list
  // contains anyone whose PTO landed after this page was rendered (rare —
  // requires the StratusTime cache to refresh between the unassigned and
  // time_off_entries computations), drop them now. Keeps the left rail
  // consistent with the Time Off section without a full page reload.
  __timeOffNames.forEach(n => {
    removeFromUnscheduled(n);
    removeFromReserves(n);
  });

  // Append the amber clearable off-window badge to a freshly built left-rail
  // <li>, mirroring the server-rendered Unscheduled/Reserves badge. No-op for
  // anyone without a partial today. Binds the ✕ handler inline so clearing
  // works without re-scanning the whole page.
  function _appendPartialBadge(li, name) {
    const range = __partialRangeByName[name];
    const hours = __partialHoursByName[name];
    if (range == null && hours == null) return;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'partial-hours-badge clearable';
    btn.dataset.day = window.SCHEDULE_DAY;
    btn.dataset.name = name;
    btn.dataset.bound = '1';
    btn.title = 'Click to clear this partial for ' + name + ' — they actually worked through it';
    btn.setAttribute('aria-label', 'Clear partial time off for ' + name);
    btn.textContent = (range || (hours + 'h')) + ' ✕';
    btn.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      _doPartialAction(btn, true);
    });
    li.appendChild(btn);
  }

  function _insertSorted(ul, name) {
    const li = document.createElement('li');
    li.dataset.name = name;
    const a = document.createElement('a');
    a.href = '/staffing/people/' + encodeURIComponent(name);
    a.className = 'person-card-link';
    a.textContent = name;
    li.appendChild(a);
    appendCertBadges(li, name);
    _appendPartialBadge(li, name);
    const existing = [...ul.querySelectorAll('li:not(.empty)')];
    // Sort by data-name (textContent now includes cert badge text).
    const target = existing.find(other => (other.dataset.name || '').toLowerCase() > name.toLowerCase());
    if (target) ul.insertBefore(li, target); else ul.appendChild(li);
  }

  function addToUnscheduled(name) {
    if (!name) return;
    // Belt-and-suspenders: never add a time-off person here, regardless of
    // which caller decided to add them. The earlier guard in
    // addBackToCorrectList catches the common path; this catches any
    // direct caller (current or future) that bypasses it.
    if (__timeOffNames.has(name)) return;
    const sect = document.querySelector('.section.unscheduled');
    if (!sect) return;
    const ul = sect.querySelector('ul');
    if (!ul) return;
    if (ul.querySelector(`li[data-name="${CSS.escape(name)}"]`)) return;  // already there
    const empty = ul.querySelector('li.empty');
    if (empty) empty.remove();
    _insertSorted(ul, name);
    const items = ul.querySelectorAll('li:not(.empty)');
    const cspan = sect.querySelector('h3 .count');
    if (cspan) cspan.textContent = items.length;
  }

  function addToReserves(name) {
    if (!name) return;
    if (__timeOffNames.has(name)) return;  // same rule as addToUnscheduled
    const sect = document.querySelector('.section.reserves');
    if (!sect) return;
    const ul = sect.querySelector('ul');
    if (!ul) return;
    if (ul.querySelector(`li[data-name="${CSS.escape(name)}"]`)) return;
    const empty = ul.querySelector('li.empty');
    if (empty) empty.remove();
    _insertSorted(ul, name);
    const items = ul.querySelectorAll('li:not(.empty)');
    const cspan = sect.querySelector('h3 .count');
    if (cspan) cspan.textContent = items.length;
  }

  /** Decide where this person belongs when removed from a WC or Time Off,
   * based on the per-person reserve flag rendered into __peopleMeta.
   * Belt-and-suspenders: people currently on time off must not appear in
   * Unscheduled or Reserves either — they belong only in the Time Off list. */
  function addBackToCorrectList(name) {
    if (!name) return;
    if (__timeOffNames.has(name)) return;
    const meta = __peopleMeta[name];
    if (!meta) return;  // unknown person (e.g., old-name reference); silently noop
    if (meta.reserve) {
      addToReserves(name);
    } else {
      addToUnscheduled(name);
    }
  }

  function syncLeftRailWithSchedule() {
    const scheduledNames = new Set();
    document.querySelectorAll('details.sched-dd input[name^="loc__"]:checked').forEach(cb => {
      scheduledNames.add(cb.value);
    });
    Object.keys(__peopleMeta || {}).forEach(name => {
      if (__timeOffNames.has(name) || scheduledNames.has(name)) {
        removeFromUnscheduled(name);
        removeFromReserves(name);
      } else {
        addBackToCorrectList(name);
      }
    });
  }

  // Remove `name` from the Time Off pill list (silent — caller just acted).
  function removeFromTimeOff(name) {
    const pills = document.querySelectorAll('.section.timeoff .pill');
    let any = false;
    pills.forEach(pill => {
      if (pill.dataset.name === name) {
        pill.remove();
        any = true;
      }
    });
    if (any) {
      // Re-add the option to the time-off "+ Add" select so the user can
      // re-select this person without reloading.
      const sel = document.querySelector('.add-select[data-loc="__time_off"]');
      if (sel && !Array.from(sel.options).some(o => o.value.split('|')[0] === name)) {
        const opt = document.createElement('option');
        opt.value = name + '|2';
        opt.textContent = name;
        sel.appendChild(opt);
        // Sort the trailing options alphabetically (skip the "+ Add" placeholder).
        const placeholder = sel.options[0];
        const rest = Array.from(sel.options).slice(1)
          .sort((a, b) => a.textContent.localeCompare(b.textContent));
        sel.innerHTML = '';
        sel.appendChild(placeholder);
        rest.forEach(o => sel.appendChild(o));
        sel.value = '';
      }
      refreshTimeOffCount();
    }
    return any;
  }

  // ---------- Time Off "+ Add" select: build pill + hidden input on change ----------
  document.addEventListener('change', (e) => {
    const sel = e.target.closest('.add-select[data-loc="__time_off"]');
    if (!sel) return;
    const raw = sel.value;
    if (!raw) return;
    // Value format: "Name|level". Default level to 2 if missing.
    const pipe = raw.indexOf('|');
    const name = pipe >= 0 ? raw.slice(0, pipe) : raw;
    const level = pipe >= 0 ? raw.slice(pipe + 1) : '2';
    const pillsDiv = document.querySelector('.section.timeoff .pills[data-loc="__time_off"]');
    if (!pillsDiv) { sel.value = ''; return; }
    // Don't double-add.
    const already = Array.from(pillsDiv.querySelectorAll('.pill'))
      .some(p => p.dataset.name === name);
    if (!already) {
      const span = document.createElement('span');
      span.className = 'pill lvl-' + (level || '2');
      span.dataset.name = name;
      span.textContent = name;
      appendCertBadges(span, name);
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'pill-x';
      btn.setAttribute('aria-label', 'remove');
      btn.textContent = '×';
      span.appendChild(btn);
      const hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = 'loc____time_off';
      hidden.value = name;
      span.appendChild(hidden);
      pillsDiv.appendChild(span);

      // Mutual exclusion: pull this person out of any WC they were scheduled at,
      // and out of the Unscheduled / Reserves left-rail lists.
      const removedFrom = removeFromAllScheduledWcs(name);
      if (removedFrom.length) {
        showSavedToast(null, null, name + ' removed from ' + removedFrom.join(', '));
      }
      removeFromUnscheduled(name);
      removeFromReserves(name);
      refreshTimeOffCount();
      refreshPickerVisibility();
    }
    // Drop the chosen option from the select (avoids re-adding the same person)
    // and reset to placeholder so the next pick fires `change` again.
    Array.from(sel.options).forEach(o => {
      if (o.value && o.value.split('|')[0] === name) o.remove();
    });
    sel.value = '';
    kickAutosave();
  });

  document.addEventListener('click', (e) => {
    const item = e.target.closest('.multi-dd .dd-item');
    if (!item) return;
    // We don't want the label's default click-to-check behavior to double-fire.
    e.preventDefault();
    const cb = item.querySelector('input[type=checkbox]');
    const wasSelected = item.classList.contains('selected');
    const nowSelected = !wasSelected;

    const dd = item.closest('details.multi-dd');
    const isSched = dd.classList.contains('sched-dd');
    if (isSched && nowSelected) {
      // Overstaff check on attempt to ADD.
      const max = dd.dataset.max ? parseInt(dd.dataset.max, 10) : null;
      const current = [...dd.querySelectorAll('.dd-item.selected')].length;
      if (max !== null && current + 1 > max) {
        const loc = dd.dataset.loc;
        showPopup({
          title: loc + ' · Overstaffed',
          msg: 'Max is ' + max + ' at ' + loc + ' and you already have ' + current + '. Remove someone first, or override — this enables Training mode for ' + loc + ' and flags today as a Testing Day so output isn’t counted against employees.',
          overrideLabel: 'Override (Training + Testing Day)',
          onOverride: () => {
            cb.checked = true; item.classList.add('selected');
            updateDdSummary(dd);
            __prevSel.set(dd, [...dd.querySelectorAll('.dd-item.selected')].map(i => i.dataset.name));
            setWcTraining(loc, true);
            flagTestingDay();
            // Mutual exclusion + live left-rail update.
            removeFromTimeOff(item.dataset.name);
            removeFromUnscheduled(item.dataset.name);
            removeFromReserves(item.dataset.name);
            refreshPickerVisibility();
            kickAutosave();
          },
        });
        return;
      }
    }

    cb.checked = nowSelected;
    item.classList.toggle('selected', nowSelected);
    updateDdSummary(dd);

    // Auto-close at max for the Scheduled picker.
    {
      const max = dd.dataset.max ? parseInt(dd.dataset.max, 10) : null;
      const count = [...dd.querySelectorAll('.dd-item.selected')].length;
      if (nowSelected && max !== null && count === max) dd.open = false;
    }

    if (isSched) {
      // Just remember the current selection for the over-max revert path; the
      // understaffed warning is fired on picker close, not on every selection.
      __prevSel.set(dd, [...dd.querySelectorAll('.dd-item.selected')].map(i => i.dataset.name));
      const personName = item.dataset.name;
      if (nowSelected) {
        // Mutual exclusion + live-update left rail when scheduling.
        removeFromTimeOff(personName);
        removeFromUnscheduled(personName);
        removeFromReserves(personName);
      } else {
        // Unchecking from a WC — unless they're scheduled elsewhere or in
        // time off, return them to the right left-rail list. (Modern
        // time-off rows aren't .pill elements, so we use __timeOffNames
        // rather than a DOM selector against the Time Off section.)
        const stillScheduled = !!document.querySelector(
          'details.sched-dd input[type=checkbox]:checked[value="' + CSS.escape(personName) + '"]'
        );
        const inTimeOff = __timeOffNames.has(personName);
        if (!stillScheduled && !inTimeOff) addBackToCorrectList(personName);
      }
    }
    // Re-filter every picker so this person disappears from / reappears in others.
    refreshPickerVisibility();

    // Kick autosave on every selection change.
    kickAutosave();
  });

  // ---------- Per-dropdown quick clear (Scheduled picker × button) ----------
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.sched-clear');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    if (__isPublished && !__unlocked) {
      alert("This schedule is Posted. Click Edit first if you need to clear it.");
      return;
    }
    if (__viewingPosted) return;
    const dd = document.querySelector('details.sched-dd[data-loc="' + CSS.escape(btn.dataset.loc) + '"]');
    if (!dd) return;
    const cleared = [...dd.querySelectorAll('.dd-item.selected')].map(i => i.dataset.name);
    dd.querySelectorAll('.dd-item.selected').forEach(item => {
      item.classList.remove('selected');
      const cb = item.querySelector('input[type=checkbox]');
      if (cb) cb.checked = false;
    });
    updateDdSummary(dd);
    __prevSel.set(dd, []);
    // Send each cleared person back to the left rail unless they're still
    // scheduled at another WC or in Time Off. (See note above re: modern
    // Time Off rows aren't .pill elements — __timeOffNames is the
    // authoritative source.)
    cleared.forEach(name => {
      const stillScheduled = !!document.querySelector(
        'details.sched-dd input[type=checkbox]:checked[value="' + CSS.escape(name) + '"]'
      );
      const inTimeOff = __timeOffNames.has(name);
      if (!stillScheduled && !inTimeOff) addBackToCorrectList(name);
    });
    refreshPickerVisibility();
    kickAutosave();
  });

  // Click a clearable partial pill (the amber 9-10a badge on a scheduled
  // person OR a partial row in the Time Off section) → confirm → clear.
  // Click a "restore" button in the Cleared-today footer → un-clear.
  async function _doPartialAction(target, isClear) {
    const day = target.dataset.day;
    const name = target.dataset.name;
    if (!day || !name) return;
    if (isClear) {
      if (!confirm(`Clear ${name}'s partial off for today? They’ll show as fully available.`)) return;
    }
    const url = isClear ? '/api/staffing/clear-partial' : '/api/staffing/restore-partial';
    const payload = {day, name};
    target.style.opacity = '0.5';
    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      const j = await resp.json();
      if (j.ok) {
        location.reload();
      } else {
        target.style.opacity = '';
        alert((isClear ? 'Clear' : 'Restore') + ' failed: ' + (j.error || 'unknown'));
      }
    } catch (err) {
      target.style.opacity = '';
      alert('Network error.');
    }
  }

  // Direct per-element listeners. More reliable than document delegation
  // for elements inside <summary> (the scheduler dropdowns), where
  // browser default actions can interfere.
  function _bindPartialHandlers() {
    document.querySelectorAll('button.partial-hours-badge.clearable').forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        _doPartialAction(btn, true);
      });
    });
    document.querySelectorAll('.time-off-row.clearable').forEach((row) => {
      if (row.dataset.bound) return;
      row.dataset.bound = '1';
      row.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        _doPartialAction(row, true);
      });
    });
    document.querySelectorAll('.time-off-restore').forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        _doPartialAction(btn, false);
      });
    });
    // Retro WC attribution × — removes the saved attribution row.
    document.querySelectorAll('.attribution-clear').forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      btn.addEventListener('click', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const id = btn.dataset.attributionId;
        const name = btn.dataset.name || 'this attribution';
        if (!id) return;
        if (!confirm(`Remove ${name}'s attribution at this work center?`)) return;
        btn.disabled = true;
        try {
          const resp = await fetch('/api/staffing/attribute/' + encodeURIComponent(id), {method: 'DELETE'});
          const j = await resp.json();
          if (j.ok) {
            location.reload();
          } else {
            btn.disabled = false;
            alert('Remove failed: ' + (j.error || 'unknown'));
          }
        } catch (err) {
          btn.disabled = false;
          alert('Network error.');
        }
      });
    });
  }
  _bindPartialHandlers();

  // Close dropdowns when clicking outside.
  document.addEventListener('click', (e) => {
    if (e.target.closest('details.multi-dd')) return;
    document.querySelectorAll('details.multi-dd[open]').forEach(d => d.open = false);
  });

  // Only one <details> open at a time across the page. Skips ancestors of the
  // just-opened element so opening a nested group (e.g. Reserves inside a picker)
  // doesn't collapse its parent.
  document.addEventListener('toggle', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLDetailsElement) || !t.open) return;
    document.querySelectorAll('details[open]').forEach(d => {
      if (d === t || d.contains(t)) return;
      d.open = false;
    });
  }, true);

  // Understaffed warning fires only when a Scheduled picker is closing with
  // a partial pick (between 1 and min-1 operators). Avoids spamming the user
  // mid-selection at multi-person work centers.
  document.addEventListener('toggle', (e) => {
    const dd = e.target;
    if (!(dd instanceof HTMLDetailsElement)) return;
    if (dd.open) return;
    if (!dd.classList.contains('sched-dd')) return;
    const min = parseInt(dd.dataset.min, 10) || 0;
    if (min < 2) return;
    const count = [...dd.querySelectorAll('.dd-item.selected')].length;
    if (count <= 0 || count >= min) return;
    const loc = dd.dataset.loc;
    showPopup({
      title: loc + ' · Understaffed',
      msg: loc + ' needs at least ' + min + ' operators but you only have ' + count + '. Add ' + (min - count) + ' more, remove all to close the station, or override — this flags today as a Testing Day.',
      overrideLabel: 'Override (Testing Day)',
      onOverride: () => { flagTestingDay(); kickAutosave(); },
    });
  }, true);

  // Initial pass on page load so people already double-booked from prior data
  // are filtered out of the pickers right away.
  refreshPickerVisibility();

  // ---------- Custom hours editor ----------
  (function() {
    const pill   = document.getElementById('hours-pill');
    const editor = document.getElementById('hours-editor');
    const list   = document.getElementById('hours-breaks-list');
    const addBtn = document.getElementById('hours-add-break');
    const cancel = document.getElementById('hours-cancel');
    const reset  = document.getElementById('hours-reset');
    const save   = document.getElementById('hours-save');
    if (!pill || !editor) return;

    function open() {
      editor.hidden = false;
      pill.setAttribute('aria-expanded', 'true');
      document.getElementById('hours-start').focus();
    }
    function close() {
      editor.hidden = true;
      pill.setAttribute('aria-expanded', 'false');
      pill.focus();
    }
    pill.addEventListener('click', open);
    cancel.addEventListener('click', close);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !editor.hidden) close();
    });

    addBtn.addEventListener('click', () => {
      const row = document.createElement('div');
      row.className = 'break-row';
      row.innerHTML =
        '<input type="time" class="b-start" step="60" aria-label="Break start time">'
      + '<input type="time" class="b-end"   step="60" aria-label="Break end time">'
      + '<input type="text" class="b-name" placeholder="Name" maxlength="40" aria-label="Break name">'
      + '<button type="button" class="remove-btn" title="Remove" aria-label="Remove break">×</button>';
      list.appendChild(row);
    });

    list.addEventListener('click', (e) => {
      const btn = e.target.closest('.remove-btn');
      if (!btn) return;
      btn.closest('.break-row').remove();
    });

    reset.addEventListener('click', () => {
      // Reset clears the override entirely on save. Mark a flag and trigger save.
      save.dataset.resetMode = '1';
      save.click();
    });

    function collect() {
      const start = document.getElementById('hours-start').value;
      const end   = document.getElementById('hours-end').value;
      const breaks = [...list.querySelectorAll('.break-row')].map(r => ({
        start: r.querySelector('.b-start').value,
        end:   r.querySelector('.b-end').value,
        name:  r.querySelector('.b-name').value.trim() || 'Break',
      })).filter(b => b.start && b.end);
      return { start, end, breaks };
    }

    function setSaveBusy(busy) {
      if (busy) {
        save.disabled = true;
        save.setAttribute('aria-busy', 'true');
      } else {
        save.disabled = false;
        save.setAttribute('aria-busy', 'false');
      }
    }

    save.addEventListener('click', async () => {
      if (save.disabled) return;
      // Past-day edits retroactively reshuffle leaderboards + player cards
      // for any window that contains this day. Make the user confirm.
      const today_iso = window.SCHEDULE_TODAY;
      const day_iso = window.SCHEDULE_DAY;
      if (day_iso < today_iso) {
        if (!confirm("Editing past-day hours updates leaderboards and player cards for any window that includes " + day_iso + ". Continue?")) {
          save.dataset.resetMode = '';
          return;
        }
      }
      const reset_mode = save.dataset.resetMode === '1';
      save.dataset.resetMode = '';
      const body = new FormData();
      const day = window.SCHEDULE_DAY;
      body.append('day', day);
      if (reset_mode) {
        body.append('reset', '1');
      } else {
        const c = collect();
        if (!c.start || !c.end || c.start >= c.end) {
          alert('Shift start must be before shift end.');
          return;
        }
        for (const b of c.breaks) {
          if (b.start >= b.end) { alert('Each break must start before it ends.'); return; }
          if (b.start < c.start || b.end > c.end) { alert('Breaks must fall within the shift.'); return; }
        }
        body.append('start', c.start);
        body.append('end',   c.end);
        for (const b of c.breaks) {
          body.append('break_start', b.start);
          body.append('break_end',   b.end);
          body.append('break_name',  b.name);
        }
      }

      setSaveBusy(true);
      try {
        const r = await fetch('/staffing/hours', { method: 'POST', body });
        if (r.ok) location.reload();
        else {
          setSaveBusy(false);
          alert('Save failed: ' + (await r.text()));
        }
      } catch (e) {
        setSaveBusy(false);
        alert('Network error.');
      }
    });
  })();

  function printSchedule() {
    window.print();
  }

  function showToast(message, link, severity) {
    let host = document.getElementById('toast-host');
    if (!host) {
      host = document.createElement('div');
      host.id = 'toast-host';
      host.style.cssText =
        'position:fixed;bottom:20px;right:20px;z-index:9999;' +
        'display:flex;flex-direction:column;gap:8px;';
      document.body.appendChild(host);
    }
    const toast = document.createElement('div');
    const isErr = severity === 'error';
    toast.style.cssText =
      'background:' + (isErr ? '#fee' : '#efe') + ';' +
      'color:' + (isErr ? '#900' : '#060') + ';' +
      'border:1px solid ' + (isErr ? '#fcc' : '#cfc') + ';' +
      'border-radius:6px;padding:10px 14px;font-size:0.9rem;' +
      'box-shadow:0 2px 8px rgba(0,0,0,0.15);max-width:340px;';
    toast.textContent = message;
    if (link) {
      toast.appendChild(document.createTextNode(' '));
      const a = document.createElement('a');
      a.href = link;
      a.target = '_blank';
      a.textContent = 'View in Slack';
      a.style.cssText = 'color:#06c;text-decoration:underline;';
      toast.appendChild(a);
    }
    host.appendChild(toast);
    setTimeout(() => toast.remove(), 6000);
  }

  // ---------- Autosave controller ----------
  // Debounced fetch POST of the scheduler form. Three states reflected
  // in #autosave-indicator: clean (hidden), dirty (red dot), saving
  // (spinner). Exposes window.flushAutosave() for the publish/share
  // flow to await any in-flight save.
  (function () {
    const form = document.getElementById('staffing-form');
    if (!form) return;

    const indicator = document.getElementById('autosave-indicator');
    const DEBOUNCE_MS = 750;
    let debounceTimer = null;
    let inFlight = null;
    let queued = false;

    function setState(state) {
      if (!indicator) return;
      indicator.classList.remove('clean', 'dirty', 'saving');
      indicator.classList.add(state);
      indicator.dataset.state = state;
    }

    function fireSave() {
      setState('saving');
      const formData = new FormData(form);
      // On a published-and-still-locked schedule, the only fields the user
      // can edit are the notes textareas (CSS exempts them from .locked).
      // Save those through the notes-only path so the schedule stays
      // Published instead of dropping back to Draft.
      const notesOnly = __isPublished && !__unlocked;
      formData.set('action', notesOnly ? 'save_notes' : 'save');
      const url = form.getAttribute('action')
        || (window.location.pathname + window.location.search);
      inFlight = fetch(url, {
        method: 'POST',
        body: formData,
        headers: { 'Accept': 'application/json' },
      })
        .then(r => {
          if (!r.ok && !(r.status >= 300 && r.status < 400)) {
            throw new Error('HTTP ' + r.status);
          }
          return r;
        })
        .then(() => {
          inFlight = null;
          if (queued) {
            queued = false;
            fireSave();
          } else {
            setState('clean');
          }
        })
        .catch(err => {
          inFlight = null;
          setState('dirty');
          if (window.showToast) {
            showToast('Autosave failed: ' + (err.message || 'unknown'), null, 'error');
          }
        });
      return inFlight;
    }

    function onEdit() {
      setState('dirty');
      if (inFlight) {
        queued = true;
        return;
      }
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        debounceTimer = null;
        fireSave();
      }, DEBOUNCE_MS);
    }

    form.addEventListener('input', onEdit);
    form.addEventListener('change', onEdit);

    window.flushAutosave = function () {
      if (debounceTimer) {
        clearTimeout(debounceTimer);
        debounceTimer = null;
        if (!inFlight) fireSave();
      }
      return inFlight || Promise.resolve();
    };
  })();

  // ---------- Publish submit busy state ----------
  (function () {
    const form = document.getElementById('staffing-form');
    if (!form) return;

    form.addEventListener('submit', (event) => {
      const submitter = event.submitter;
      if (!submitter || submitter.name !== 'action' || submitter.value !== 'publish') return;
      let publishIntent = form.querySelector('input[type="hidden"][data-publish-intent="1"]');
      if (!publishIntent) {
        publishIntent = document.createElement('input');
        publishIntent.type = 'hidden';
        publishIntent.dataset.publishIntent = '1';
        publishIntent.name = 'action';
        form.appendChild(publishIntent);
      }
      publishIntent.value = 'publish';
      document.querySelectorAll('.publish-submit').forEach((button) => {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
      });
    });
  })();

  // ---------- First-edit-on-posted: one-time toast + drop ?view=posted ----------
  // When the page is loaded with ?view=posted, the first input/change
  // event flips us back to draft mode silently after a brief toast.
  // Subsequent edits in the same session are silent.
  (function () {
    if (window.SCHEDULE_VIEW_MODE !== 'posted') return;
    const form = document.getElementById('staffing-form');
    if (!form) return;

    let warned = false;
    function onFirstEdit() {
      if (warned) return;
      warned = true;
      if (window.showToast) {
        showToast(
          'Switched to draft — Re-publish to update the posted version.',
          null,
          'info'
        );
      }
      const url = new URL(window.location.href);
      url.searchParams.delete('view');
      history.replaceState({}, '', url.toString());
      const pill = document.querySelector('.title-bar .pub-pill.on');
      if (pill) pill.style.display = 'none';
    }
    form.addEventListener('input', onFirstEdit);
    form.addEventListener('change', onFirstEdit);
  })();

  async function postToSlack(btn) {
    const originalContent = btn.innerHTML;
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    btn.innerHTML = '<span style="font-size:0.85rem;font-weight:600">Posting…</span>';
    try {
      const day = window.SCHEDULE_DAY
        || new URLSearchParams(window.location.search).get('day');
      if (!day) {
        showToast('No day available — refresh the page', null, 'error');
        return;
      }

      // Re-publish confirmation if the schedule has been published before.
      if (window.SCHEDULE_PUBLISHED) {
        const ok = confirm(
          'Re-publish and post to Slack? Anyone with the previous version will see the revised schedule.'
        );
        if (!ok) return;
      }

      // Make sure any pending autosave finishes before publishing.
      if (window.flushAutosave) {
        await window.flushAutosave();
      }

      // Step 1: publish via the form's POST endpoint with action=publish.
      const form = document.getElementById('staffing-form');
      const fd = new FormData(form);
      fd.set('action', 'publish');
      const formAction = form.getAttribute('action')
        || ('/staffing?day=' + encodeURIComponent(day));
      const pubRes = await fetch(formAction, {
        method: 'POST',
        body: fd,
        headers: { 'Accept': 'application/json' },
      });
      if (pubRes.status >= 400) {
        throw new Error('Publish failed: HTTP ' + pubRes.status);
      }

      // Step 2: post the resulting PDF to Slack.
      const r = await fetch('/staffing/share-to-slack?day=' + encodeURIComponent(day), {
        method: 'POST',
        headers: { 'Accept': 'application/json' },
      });
      const data = await r.json();
      if (data.ok) {
        showToast('Published & posted to #' + data.channel_name, data.permalink);
      } else {
        showToast('Slack post failed: ' + data.error, null, 'error');
      }
    } catch (e) {
      showToast(e.message || 'Slack post failed', null, 'error');
    } finally {
      btn.disabled = false;
      btn.setAttribute('aria-busy', 'false');
      btn.innerHTML = originalContent;
    }
  }

  // ---------- Rotation goal (mode buttons + auto-center toggles + reset) ----------
  // The three mode buttons set the goal (optimized / normal / training) and
  // rebuild enabled Auto work centers in that mode. The server is
  // authoritative: it returns the full assignment map with manual/default locks
  // preserved, so we reconcile only enabled Auto pickers.
  (function () {
    const controls = document.querySelector('.rotation-controls');
    if (!controls) return;
    const modeBtns = [...controls.querySelectorAll('.rotation-mode-btn')];
    const resetBtn = document.getElementById('rotation-reset-btn');
    const warnBox = document.getElementById('rotation-warnings');
    const helpEl = document.getElementById('rotation-mode-help');
    const autoCbs = [...document.querySelectorAll('.wc-auto-cb')];
    const capacityDialog = document.getElementById('auto-capacity-dialog');
    const capacityForm = document.getElementById('auto-capacity-form');
    const capacityMessage = document.getElementById('auto-capacity-message');
    const capacityReplacements = document.getElementById('auto-capacity-replacements');
    const capacityCancel = document.getElementById('auto-capacity-cancel');
    const capacityConfirm = document.getElementById('auto-capacity-confirm');
    const day = controls.dataset.day || window.SCHEDULE_DAY;
    let rebuilding = false;
    let savingAutoCenters = false;
    let capacityDialogState = null;

    // Per-mode help lines mirror routes/staffing.py::_ROTATION_MODE_HELP so the
    // hint updates instantly when the goal changes (no reload).
    const HELP = {
      optimized: 'Optimized favors the strongest coverage on auto work centers.',
      normal: 'Normal balances coverage, preferences, and fair rotation.',
      training: 'Training develops level-1/2 operators while protecting coverage.',
    };

    function currentMode() {
      const active = modeBtns.find(b => b.classList.contains('active'));
      return (active && active.dataset.rotationMode)
        || window.RECYCLED_ROTATION_MODE || 'normal';
    }

    function setActiveMode(mode) {
      modeBtns.forEach(b => {
        const on = b.dataset.rotationMode === mode;
        b.classList.toggle('active', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      window.RECYCLED_ROTATION_MODE = mode;
      if (helpEl && HELP[mode]) helpEl.textContent = HELP[mode];
    }

    function renderWarnings(warnings) {
      window.ROTATION_WARNINGS = warnings || [];
      if (!warnBox) return;
      warnBox.innerHTML = '';
      if (!warnings || !warnings.length) {
        warnBox.hidden = true;
        return;
      }
      const ul = document.createElement('ul');
      warnings.forEach(w => {
        const li = document.createElement('li');
        li.textContent = w;
        ul.appendChild(li);
      });
      warnBox.appendChild(ul);
      warnBox.hidden = false;
    }

    function selectedAutoCenters() {
      return autoCbs.filter(cb => cb.checked).map(cb => cb.dataset.loc).filter(Boolean);
    }

    function applyEnabledCenters(names) {
      const enabled = new Set(names || []);
      window.AUTO_SCHEDULE_WC_NAMES = [...enabled];
      autoCbs.forEach(cb => {
        cb.checked = enabled.has(cb.dataset.loc);
      });
    }

    function removeDisabledAutoWarnings() {
      const enabled = new Set(selectedAutoCenters());
      renderWarnings((window.ROTATION_WARNINGS || []).filter(warning => {
        // This is a day-wide shortage.  It remains visible until the server
        // accepts a replacement selection and sends the next authority state.
        if (warning.startsWith('Auto centers need ')) return true;
        const center = autoCbs.map(cb => cb.dataset.loc).find(center =>
          center && (
            warning.startsWith(center + ' is staffed below its minimum')
            || warning.startsWith(center + ' could not be staffed to its minimum')
            || warning === 'No safe operator pairing available for ' + center + '.'
          )
        );
        return !center || enabled.has(center);
      }));
    }

    function postAutoCenters(workCenters, turnOff) {
      return fetch('/api/rotations/auto-work-centers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify({ day, work_centers: workCenters, turn_off: turnOff }),
      });
    }

    function setAutoCentersSaving(saving) {
      savingAutoCenters = saving;
      autoCbs.forEach(cb => { cb.disabled = saving; });
    }

    function closeAutoCapacityDialog() {
      const changedCb = capacityDialogState && capacityDialogState.changedCb;
      capacityDialogState = null;
      if (capacityDialog && capacityDialog.open) capacityDialog.close();
      if (changedCb) changedCb.focus();
    }

    function updateCapacityConfirm() {
      if (!capacityDialogState || !capacityConfirm || !capacityReplacements) return;
      const chosen = capacityReplacements.querySelectorAll('input:checked').length;
      capacityConfirm.disabled = chosen < capacityDialogState.requiredDisableCount;
    }

    function showAutoCapacityDialog(data, requestedCenter, changedCb) {
      if (!capacityDialog || !capacityMessage || !capacityReplacements || !capacityConfirm) {
        if (window.showToast) showToast((data && data.error) || 'Auto-center capacity could not be updated.', null, 'error');
        return;
      }
      const requiredDisableCount = Math.max(0, Number(data.required_disable_count) || 0);
      capacityDialogState = { requestedCenter, changedCb, requiredDisableCount };
      capacityMessage.textContent = (data && data.error) || 'Choose work centers to turn off.';
      capacityReplacements.innerHTML = '';
      selectedAutoCenters()
        .filter(center => center !== requestedCenter)
        .forEach(center => {
          const label = document.createElement('label');
          label.className = 'auto-capacity-replacement';
          const input = document.createElement('input');
          input.type = 'checkbox';
          input.value = center;
          input.addEventListener('change', updateCapacityConfirm);
          label.append(input, document.createTextNode(center));
          capacityReplacements.appendChild(label);
        });
      updateCapacityConfirm();
      if (!capacityDialog.open) capacityDialog.showModal();
      window.requestAnimationFrame(() => {
        const firstChoice = capacityReplacements.querySelector('input');
        (firstChoice || capacityCancel).focus();
      });
    }

    async function saveAutoCenters(changedCb) {
      if (savingAutoCenters) return;
      const requestedCenter = changedCb.dataset.loc;
      const requestedWorkCenters = selectedAutoCenters();
      // Browser checkbox changes are optimistic by default.  Put this one
      // straight back until the server has accepted the requested selection.
      changedCb.checked = !changedCb.checked;
      setAutoCentersSaving(true);
      try {
        const resp = await postAutoCenters(requestedWorkCenters, []);
        const data = await resp.json().catch(() => ({}));
        if (resp.status === 409) {
          showAutoCapacityDialog(data, requestedCenter, changedCb);
          return;
        }
        if (!resp.ok || !data.ok) {
          throw new Error((data && data.error) || ('HTTP ' + resp.status));
        }
        if (!Array.isArray(data.enabled_work_centers)) {
          throw new Error('Server did not return enabled Auto work centers.');
        }
        applyEnabledCenters(data.enabled_work_centers);
        removeDisabledAutoWarnings();
        if (window.showToast) showToast('Auto work centers saved');
      } catch (err) {
        if (window.showToast) showToast('Auto toggle failed: ' + (err.message || 'network error'), null, 'error');
      } finally {
        setAutoCentersSaving(false);
      }
    }

    if (capacityCancel) {
      capacityCancel.addEventListener('click', closeAutoCapacityDialog);
    }
    if (capacityDialog) {
      capacityDialog.addEventListener('cancel', event => {
        event.preventDefault();
        closeAutoCapacityDialog();
      });
    }
    if (capacityForm) {
      capacityForm.addEventListener('submit', async event => {
        event.preventDefault();
        if (!capacityDialogState || savingAutoCenters) return;
        const { requestedCenter, requiredDisableCount } = capacityDialogState;
        const turnOff = [...capacityReplacements.querySelectorAll('input:checked')]
          .map(input => input.value);
        if (turnOff.length < requiredDisableCount) return;
        const workCenters = [...new Set([requestedCenter, ...selectedAutoCenters()])]
          .filter(center => !turnOff.includes(center));
        setAutoCentersSaving(true);
        capacityConfirm.disabled = true;
        try {
          const resp = await postAutoCenters(workCenters, turnOff);
          const data = await resp.json().catch(() => ({}));
          if (resp.status === 409) {
            showAutoCapacityDialog(data, requestedCenter, capacityDialogState.changedCb);
            return;
          }
          if (!resp.ok || !data.ok) {
            throw new Error((data && data.error) || ('HTTP ' + resp.status));
          }
          if (!Array.isArray(data.enabled_work_centers)) {
            throw new Error('Server did not return enabled Auto work centers.');
          }
          applyEnabledCenters(data.enabled_work_centers);
          removeDisabledAutoWarnings();
          if (window.showToast) showToast('Auto work centers saved');
          closeAutoCapacityDialog();
        } catch (err) {
          if (capacityMessage) capacityMessage.textContent = err.message || 'Auto-center capacity could not be updated.';
          if (window.showToast) showToast('Auto toggle failed: ' + (err.message || 'network error'), null, 'error');
        } finally {
          setAutoCentersSaving(false);
          updateCapacityConfirm();
        }
      });
    }

    // Reconcile every enabled Auto picker's checkboxes to the server-returned
    // assignment map. Manual/default pills are already in that map, so they
    // stay checked; regenerated people replace the rest.
    function applyRebuild(data) {
      const assignments = data.assignments || {};
      window.ROTATION_REASONS = data.reasons || {};
      applyEnabledCenters(data.enabled_work_centers || window.AUTO_SCHEDULE_WC_NAMES || []);
      const enabled = new Set(window.AUTO_SCHEDULE_WC_NAMES || []);
      enabled.forEach(loc => {
        const dd = document.querySelector('details.sched-dd[data-loc="' + CSS.escape(loc) + '"]');
        if (!dd) return;
        const wanted = new Set(assignments[loc] || []);
        dd.querySelectorAll('.dd-item').forEach(item => {
          const cb = item.querySelector('input[type=checkbox]');
          if (!cb) return;
          const should = wanted.has(item.dataset.name);
          cb.checked = should;
          item.classList.toggle('selected', should);
        });
        updateDdSummary(dd);
        __prevSel.set(dd, [...dd.querySelectorAll('.dd-item.selected')].map(i => i.dataset.name));
      });
      renderWarnings(data.warnings);
      syncLeftRailWithSchedule();
      refreshPickerVisibility();
      kickAutosave();
    }

    async function rebuild(mode) {
      if (rebuilding || !mode) return;
      if (__isPublished && !__unlocked) {
        alert('This schedule is Posted. Click Edit first to change the Recycled goal.');
        return;
      }
      if (__viewingPosted) return;
      rebuilding = true;
      controls.classList.add('rebuilding');
      modeBtns.forEach(b => { b.disabled = true; });
      if (resetBtn) resetBtn.disabled = true;
      try {
        const resp = await fetch('/api/rotations/rebuild', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
          body: JSON.stringify({ day, mode }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.ok) {
          const err = (data && data.error) || ('HTTP ' + resp.status);
          // Surface the failure without wiping the grid.
          renderWarnings(['Could not rebuild the schedule: ' + err]);
          if (window.showToast) showToast('Rebuild failed: ' + err, null, 'error');
          return;
        }
        setActiveMode(mode);
        applyRebuild(data);
      } catch (err) {
        renderWarnings(['Could not rebuild the schedule: ' + (err.message || 'network error')]);
        if (window.showToast) showToast('Rebuild failed — network error', null, 'error');
      } finally {
        rebuilding = false;
        controls.classList.remove('rebuilding');
        modeBtns.forEach(b => { b.disabled = false; });
        if (resetBtn) resetBtn.disabled = false;
      }
    }

    modeBtns.forEach(btn => {
      btn.addEventListener('click', () => rebuild(btn.dataset.rotationMode));
    });
    if (resetBtn) {
      resetBtn.addEventListener('click', () => rebuild(currentMode()));
    }
    autoCbs.forEach(cb => {
      cb.addEventListener('change', () => saveAutoCenters(cb));
    });
  })();

  // Assignments to Do modal moved to the global footer; no per-page handler here.
