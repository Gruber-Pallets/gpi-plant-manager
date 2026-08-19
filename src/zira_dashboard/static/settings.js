  const PROD_MIN = window.PROD_MIN;
  function fmt(n, dp) { return n.toFixed(dp).replace(/\.?0+$/, ''); }
  function computeBreakdown(dailyStr, fallback) {
    const daily = Number(dailyStr) || Number(fallback) || 0;
    if (!daily || !PROD_MIN) return '';
    const perHr   = daily / (PROD_MIN / 60);
    const per15   = daily / (PROD_MIN / 15);
    const perMin  = daily / PROD_MIN;
    const cycleS  = (PROD_MIN * 60) / daily;
    return `${fmt(perHr,1)}/hr · ${fmt(per15,1)}/15m · ${fmt(perMin,2)}/m · ${fmt(cycleS,1)}s`;
  }
  function renderFor(input) {
    const row = input.closest('tr') || input.parentElement;
    const box = row && row.querySelector('[data-breakdown]');
    if (!box) return;
    const text = computeBreakdown(input.value, input.dataset.fallback);
    if (text) { box.textContent = text; box.classList.remove('empty'); }
    else { box.textContent = '—'; box.classList.add('empty'); }
  }
  document.querySelectorAll('.target-input').forEach(inp => {
    renderFor(inp);
    inp.addEventListener('input', () => renderFor(inp));
  });

  // --- Inline save-status next to the clicked Save button ---
  function flashBesideButton(btn, msg, isError) {
    if (!btn || !btn.parentElement) return;
    let span = btn.previousElementSibling;
    if (!span || !span.classList || !span.classList.contains('save-flash')) {
      span = document.createElement('span');
      span.className = 'save-flash';
      btn.parentElement.insertBefore(span, btn);
    }
    span.textContent = msg;
    span.classList.toggle('error', !!isError);
    // force reflow then show
    void span.offsetWidth;
    span.classList.add('show');
    clearTimeout(span._tid);
    span._tid = setTimeout(() => span.classList.remove('show'), 1600);
  }

  // --- Intercept form submits: save via fetch, keep the page as-is ---
  document.querySelectorAll('form[data-section]').forEach(form => {
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      // Track which submit button was used (if any), else fall back to the first.
      const btn = (e.submitter && e.submitter.tagName === 'BUTTON')
        ? e.submitter
        : form.querySelector('button[type=submit]');
      const origText = btn ? btn.textContent : '';
      if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
      fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        headers: { 'Accept': 'application/json' },
      }).then(r => {
        flashBesideButton(btn, r.ok ? 'Saved' : 'Save failed', !r.ok);
      }).catch(() => flashBesideButton(btn, 'Save failed', true))
        .finally(() => {
          if (btn) { btn.disabled = false; btn.textContent = origText; }
        });
    });
  });

  // Schedule editor: add/remove break rows, re-index field names so the POST parses cleanly.
  (function () {
    const list = document.getElementById('breaks-list');
    if (!list) return;
    function reindex() {
      [...list.querySelectorAll('.break-row')].forEach((row, i) => {
        row.querySelectorAll('input').forEach(inp => {
          inp.name = inp.name.replace(/_\d+$/, '_' + i);
        });
      });
    }
    list.addEventListener('click', e => {
      if (e.target.classList.contains('remove-btn')) {
        e.target.closest('.break-row').remove();
        reindex();
      }
    });
    document.getElementById('add-break').addEventListener('click', () => {
      const i = list.querySelectorAll('.break-row').length;
      const row = document.createElement('div');
      row.className = 'break-row';
      row.innerHTML =
        '<input type="time" name="break_start_' + i + '" step="300" required>'
      + '<input type="time" name="break_end_'   + i + '" step="300" required>'
      + '<input type="text" name="break_name_'  + i + '" placeholder="name (e.g. Lunch, Cleanup)">'
      + '<button type="button" class="remove-btn" title="Remove">×</button>';
      list.appendChild(row);
    });
  })();

  (function () {
    const list = document.getElementById('sat-breaks-list');
    if (!list) return;
    function reindex() {
      [...list.querySelectorAll('.break-row')].forEach((row, i) => {
        row.querySelectorAll('input').forEach(inp => {
          inp.name = inp.name.replace(/_\d+$/, '_' + i);
        });
      });
    }
    list.addEventListener('click', e => {
      if (e.target.classList.contains('remove-btn')) {
        e.target.closest('.break-row').remove();
        reindex();
      }
    });
    document.getElementById('sat-add-break').addEventListener('click', () => {
      const i = list.querySelectorAll('.break-row').length;
      const row = document.createElement('div');
      row.className = 'break-row';
      row.innerHTML =
        '<input type="time" name="break_start_' + i + '" step="300" required>'
      + '<input type="time" name="break_end_'   + i + '" step="300" required>'
      + '<input type="text" name="break_name_'  + i + '" placeholder="name (e.g. Lunch)">'
      + '<button type="button" class="remove-btn" title="Remove">×</button>';
      list.appendChild(row);
    });
  })();

  // ---------- Autosave + Undo/Redo + top-center toast ----------
  // Same pattern as the People Matrix and Plant Scheduler. One factory wires
  // up each form independently — they post to different endpoints and have
  // their own undo history.
  function _serializeForm(form) {
    const map = {};
    for (const [k, v] of new FormData(form).entries()) {
      if (!(k in map)) map[k] = [];
      map[k].push(v);
    }
    return map;
  }
  function _formDataForSave(form, before, after) {
    const body = new FormData(form);
    if (form.id !== 'wc-form') return body;
    const fields = new Set([...Object.keys(before), ...Object.keys(after)]);
    for (const field of fields) {
      const isDefaultPeople = field.startsWith('group_default_people__')
        || (field.startsWith('wc__') && field.endsWith('__default_people'));
      if (!isDefaultPeople) continue;
      const oldValues = [...(before[field] || [])].sort();
      const newValues = [...(after[field] || [])].sort();
      if (JSON.stringify(oldValues) !== JSON.stringify(newValues)) {
        body.append('default_people_dirty', field);
      }
    }
    return body;
  }
  function _applyState(form, snap) {
    for (const el of form.querySelectorAll('input, select, textarea')) {
      if (!el.name) continue;
      const vals = snap[el.name] || [];
      if (el.type === 'checkbox' || el.type === 'radio') {
        el.checked = vals.includes(el.value);
      } else if (el instanceof HTMLSelectElement) {
        if (el.multiple) Array.from(el.options).forEach(o => { o.selected = vals.includes(o.value); });
        else el.value = vals[0] ?? '';
      } else {
        el.value = vals[0] ?? '';
      }
    }
  }
  function showSavedToast(message, undoSnap, onUndo) {
    let bd = document.getElementById('save-toast-bd');
    if (!bd) {
      bd = document.createElement('div');
      bd.id = 'save-toast-bd';
      bd.className = 'save-toast-bd';
      document.body.appendChild(bd);
    }
    const el = document.createElement('div');
    const isErr = message.startsWith('Save failed');
    el.className = 'save-toast' + (isErr ? ' error' : '');
    const label = document.createElement('span');
    label.textContent = message;
    el.appendChild(label);
    if (undoSnap && onUndo) {
      const u = document.createElement('button');
      u.type = 'button';
      u.className = 'undo-btn';
      u.textContent = 'Undo';
      u.onclick = () => { onUndo(undoSnap); el.remove(); };
      el.appendChild(u);
    }
    bd.appendChild(el);
    setTimeout(() => { el.classList.add('fade'); setTimeout(() => el.remove(), 300); }, 5000);
  }

  // Single pair of Undo/Redo buttons in the page header drives whichever form
  // most recently saved. Each form's history stays separate; the buttons just
  // dispatch to the active form.
  const __pageUndoBtn = document.getElementById('page-undo-btn');
  const __pageRedoBtn = document.getElementById('page-redo-btn');
  let __activeForm = null;
  function refreshPageBtns() {
    const f = __activeForm;
    const s = (f && f._undoState) ? f._undoState() : { hasUndo: false, hasRedo: false };
    __pageUndoBtn.disabled = !s.hasUndo;
    __pageRedoBtn.disabled = !s.hasRedo;
    __pageUndoBtn.title = f ? `Undo (${f.id})` : 'Undo';
    __pageRedoBtn.title = f ? `Redo (${f.id})` : 'Redo';
  }
  __pageUndoBtn.addEventListener('click', () => {
    if (__activeForm && __activeForm._performUndo) __activeForm._performUndo();
  });
  __pageRedoBtn.addEventListener('click', () => {
    if (__activeForm && __activeForm._performRedo) __activeForm._performRedo();
  });

  function attachAutosaver(form, url) {
    if (!form) return;
    let snapshot = _serializeForm(form);
    let lastUndo = null;
    let lastRedo = null;
    let saving = false;
    let timer = null;

    function notify() { __activeForm = form; refreshPageBtns(); }
    function responseErrorMessage(response) {
      return response.json()
        .then(body => body && body.error ? `Save failed: ${body.error}` : 'Save failed')
        .catch(() => 'Save failed');
    }
    function schedule() { clearTimeout(timer); timer = setTimeout(save, 600); }
    function save() {
      timer = null;
      if (saving) { schedule(); return; }
      saving = true;
      const before = snapshot;
      const after = _serializeForm(form);
      fetch(url, { method: 'POST', body: _formDataForSave(form, before, after), headers: {'Accept':'application/json'} })
        .then(r => {
          if (r.ok) {
            snapshot = after;
            lastUndo = before;
            lastRedo = null;
            notify();
            showSavedToast('Saved', before, () => performUndo(before));
          } else {
            return responseErrorMessage(r).then(showSavedToast);
          }
        })
        .catch(() => showSavedToast('Save failed'))
        .finally(() => { saving = false; });
    }
    function performUndo(snap) {
      if (!snap || saving) return;
      const beforeRevert = _serializeForm(form);
      _applyState(form, snap);
      const afterRevert = _serializeForm(form);
      clearTimeout(timer);
      timer = null;
      saving = true;
      fetch(url, { method: 'POST', body: _formDataForSave(form, beforeRevert, afterRevert), headers: {'Accept':'application/json'} })
        .finally(() => {
          snapshot = _serializeForm(form);
          lastUndo = null;
          lastRedo = beforeRevert;
          notify();
          saving = false;
          showSavedToast('Reverted');
        });
    }
    function performRedo(snap) {
      if (!snap || saving) return;
      const beforeRedo = _serializeForm(form);
      _applyState(form, snap);
      const afterRedo = _serializeForm(form);
      clearTimeout(timer);
      timer = null;
      saving = true;
      fetch(url, { method: 'POST', body: _formDataForSave(form, beforeRedo, afterRedo), headers: {'Accept':'application/json'} })
        .finally(() => {
          snapshot = _serializeForm(form);
          lastUndo = beforeRedo;
          lastRedo = null;
          notify();
          saving = false;
          showSavedToast('Redone');
        });
    }

    // Expose for the global page-header buttons.
    form._performUndo = () => performUndo(lastUndo);
    form._performRedo = () => performRedo(lastRedo);
    form._undoState = () => ({ hasUndo: !!lastUndo, hasRedo: !!lastRedo });

    form.addEventListener('change', schedule);
    form.addEventListener('input', (e) => {
      if (e.target && (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT')) schedule();
    });
    // Button-less autosaved forms: Enter saves in place, never native-submit
    // (full reload). data-section forms are handled by the submit interceptor
    // above, so skip them here to avoid a double save.
    form.addEventListener('submit', (e) => {
      if (form.dataset.section) return;
      e.preventDefault();
      save();
    });
    window.addEventListener('beforeunload', () => {
      if (!timer) return;
      clearTimeout(timer);
      timer = null;
      if (navigator.sendBeacon) {
        const after = _serializeForm(form);
        navigator.sendBeacon(url, _formDataForSave(form, snapshot, after));
      }
    });
  }

  attachAutosaver(document.getElementById('schedule-form'), '/settings/schedule');
  attachAutosaver(document.getElementById('saturday-schedule-form'), '/settings/saturday_schedule');
  attachAutosaver(document.getElementById('wc-form'), '/settings/work_centers');
  attachAutosaver(document.getElementById('auto-lunch-form'), '/settings/auto_lunch');
  document.querySelectorAll('form.ws-rounding-fields').forEach(f => {
    attachAutosaver(f, '/settings/rounding_system');
  });

  // ---------- Picker helpers: summary refresh, × clear, max/min guards ----------
  function refreshPickerSummary(picker) {
    const text = picker.querySelector(':scope > summary > .dd-summary-text');
    if (!text) return;
    // Single-select pickers (radio inputs) — Group + Department.
    if (picker.classList.contains('single-picker')) {
      const checked = picker.querySelector('input[type=radio]:checked');
      if (!checked || !checked.value) {
        text.innerHTML = '<span class="empty">—</span>';
      } else {
        text.textContent = checked.value;
      }
      return;
    }
    // Multi-select pickers (checkbox inputs) — Required Skills + Default People.
    const checked = [...picker.querySelectorAll('.dd-item input[type=checkbox]:checked')];
    if (!checked.length) {
      text.innerHTML = '<span class="empty">—</span>';
      return;
    }
    const parts = checked.map(cb => {
      const item = cb.closest('.dd-item');
      const lvl = item.dataset.level;
      const name = cb.value;
      return lvl != null
        ? '<span class="lvl-' + lvl + '">' + name + '</span>'
        : name;
    });
    text.innerHTML = parts.join(', ');
  }

  // × inside picker summary — clears the picker. For single-select pickers
  // (Group + Department), this means selecting the empty radio. For
  // multi-select pickers, all checkboxes are unchecked.
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.skills-picker > summary > .dd-clear');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const picker = btn.closest('.skills-picker');
    let changed = false;
    if (picker.classList.contains('single-picker')) {
      const empty = picker.querySelector('input[type=radio][value=""]');
      if (empty && !empty.checked) { empty.checked = true; changed = true; }
    } else {
      picker.querySelectorAll('.dd-item input[type=checkbox]:checked').forEach(cb => {
        cb.checked = false;
        changed = true;
      });
    }
    if (!changed) return;
    refreshPickerSummary(picker);
    picker.dispatchEvent(new Event('change', { bubbles: true }));
  });

  // Single-select pickers auto-close after a choice and refresh their summary.
  document.querySelectorAll('details.single-picker').forEach(picker => {
    picker.addEventListener('change', (e) => {
      if (!e.target.matches('input[type=radio]')) return;
      refreshPickerSummary(picker);
      picker.open = false;
    });
  });

  // Generic popup, mirrors scheduler's showPopup.
  function showPopup({ title, msg, overrideLabel = 'Override', cancelLabel = 'Cancel', onOverride, onCancel }) {
    let bd = document.getElementById('settings-popover-bd');
    if (!bd) {
      bd = document.createElement('div');
      bd.id = 'settings-popover-bd';
      bd.className = 'popover-backdrop';
      bd.innerHTML =
        '<div class="popover">'
      + '  <h4 data-role="title"></h4>'
      + '  <p data-role="msg"></p>'
      + '  <div class="actions">'
      + '    <button type="button" class="primary"  data-role="cancel"></button>'
      + '    <button type="button" class="override" data-role="override"></button>'
      + '  </div>'
      + '</div>';
      document.body.appendChild(bd);
    }
    bd.querySelector('[data-role=title]').textContent = title;
    bd.querySelector('[data-role=msg]').textContent = msg;
    bd.querySelector('[data-role=cancel]').textContent = cancelLabel;
    bd.querySelector('[data-role=override]').textContent = overrideLabel;
    bd.classList.add('show');
    const close = () => bd.classList.remove('show');
    bd.querySelector('[data-role=cancel]').onclick   = () => { close(); if (onCancel)   onCancel(); };
    bd.querySelector('[data-role=override]').onclick = () => { close(); if (onOverride) onOverride(); };
  }

  // Default-people picker — block adding past max (with override popup), and
  // auto-close once max is reached. Click intercepts before native toggle.
  document.addEventListener('click', (e) => {
    const item = e.target.closest('.default-people-picker .dd-item');
    if (!item) return;
    if (e.target.closest('.dd-clear')) return;
    const cb = item.querySelector('input[type=checkbox]');
    if (!cb || cb.checked) return;  // unchecking always allowed
    const picker = item.closest('.default-people-picker');
    const max = parseInt(picker.dataset.max, 10);
    if (!max) return;
    const current = picker.querySelectorAll('.dd-item input[type=checkbox]:checked').length;
    if (current + 1 <= max) return;  // within limit
    e.preventDefault();
    e.stopPropagation();
    const loc = picker.dataset.loc;
    showPopup({
      title: loc + ' · More than max',
      msg: 'Max is ' + max + ' at ' + loc + ' and you already have ' + current + '. The default crew is what pre-loads each new day, so going over max means every day starts overstaffed. Override to add anyway.',
      overrideLabel: 'Add anyway',
      onOverride: () => {
        cb.checked = true;
        refreshPickerSummary(picker);
        picker.dispatchEvent(new Event('change', { bubbles: true }));
      },
    });
  }, true);

  // Auto-close at max. Partial default crews are allowed: a work center's
  // minimum applies when staffing a day, not when saving its defaults.
  document.querySelectorAll('details.default-people-picker').forEach(picker => {
    picker.addEventListener('change', (e) => {
      if (!e.target.matches('.dd-item input[type=checkbox]')) return;
      refreshPickerSummary(picker);
      const max = parseInt(picker.dataset.max, 10);
      const checked = picker.querySelectorAll('.dd-item input[type=checkbox]:checked').length;
      if (max && checked === max) picker.open = false;
    });
  });

  // Required-skills picker also keeps its summary in sync on change.
  document.querySelectorAll('details.req-skills-picker').forEach(picker => {
    picker.addEventListener('change', (e) => {
      if (!e.target.matches('.dd-item input[type=checkbox]')) return;
      // Render plain comma list, no level coloring on skills.
      const text = picker.querySelector(':scope > summary > .dd-summary-text');
      const checked = [...picker.querySelectorAll('.dd-item input[type=checkbox]:checked')];
      text.innerHTML = checked.length
        ? checked.map(cb => cb.value).join(', ')
        : '<span class="empty">—</span>';
    });
  });

  // Only one <details> open at a time (covers all pickers and panels).
  document.addEventListener('toggle', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLDetailsElement) || !t.open) return;
    document.querySelectorAll('details[open]').forEach(d => {
      if (d === t || d.contains(t)) return;
      d.open = false;
    });
  }, true);

  // ---------- Groups: Enter on the "+ add new group..." input quick-saves ----------
  // Hits POST /settings/groups/add with just the typed name (doesn't touch
  // any other in-progress edits on the page), then reloads so the new row
  // appears in the list and a fresh empty add-row is ready for the next
  // group. Lets you bang out groups quickly: type, Enter, type, Enter, ...
  (function initGroupQuickAdd() {
    document.addEventListener('keydown', async (e) => {
      if (e.key !== 'Enter') return;
      const input = e.target;
      if (!input || input.name !== 'group_new') return;
      const name = (input.value || '').trim();
      if (!name) return;
      e.preventDefault();
      input.disabled = true;
      const fd = new FormData();
      fd.append('name', name);
      try {
        const resp = await fetch('/settings/groups/add', { method: 'POST', body: fd });
        if (resp.ok) {
          // Reload so Jinja re-renders the groups table with the new row,
          // and the new add-row input is empty + ready for the next entry.
          // Preserve scroll position by stashing it; reload restores so
          // the user stays in the Groups section visually.
          sessionStorage.setItem('settings-scroll-y', String(window.scrollY));
          sessionStorage.setItem('settings-focus-group-new', '1');
          window.location.reload();
          return;
        }
        const data = await resp.json().catch(() => ({}));
        alert('Could not add group: ' + (data.error || ('HTTP ' + resp.status)));
        input.disabled = false;
        input.focus();
      } catch (err) {
        alert('Network error adding group: ' + err.message);
        input.disabled = false;
        input.focus();
      }
    });

    // Restore scroll + focus after the quick-add reload.
    window.addEventListener('DOMContentLoaded', () => {
      const y = sessionStorage.getItem('settings-scroll-y');
      const refocus = sessionStorage.getItem('settings-focus-group-new');
      if (y !== null) {
        sessionStorage.removeItem('settings-scroll-y');
        window.scrollTo(0, parseInt(y, 10) || 0);
      }
      if (refocus) {
        sessionStorage.removeItem('settings-focus-group-new');
        const next = document.querySelector('input[name="group_new"]');
        if (next) next.focus();
      }
    });
  })();

  // ---------- Timeclock sub-tabs (Schedules / Rules / Activity) ----------
  (function initTimeclockTabs() {
    const tablist = document.querySelector('.tc-tabs');
    if (!tablist) return;
    const tabs = [...tablist.querySelectorAll('[data-tc-tab]')];
    const panels = {
      schedules: document.getElementById('tc-tab-schedules'),
      rules:     document.getElementById('tc-tab-rules'),
      activity:  document.getElementById('tc-tab-activity'),
    };
    function activate(name) {
      if (!panels[name]) name = 'schedules';
      tabs.forEach(t => {
        const on = t.dataset.tcTab === name;
        t.classList.toggle('active', on);
        t.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      for (const [k, el] of Object.entries(panels)) {
        if (el) el.style.display = (k === name) ? '' : 'none';
      }
    }
    tabs.forEach(t => t.addEventListener('click', () => {
      activate(t.dataset.tcTab);
      if (history.replaceState) history.replaceState(null, '', '#' + t.dataset.tcTab);
    }));
    const initial = (location.hash || '').replace('#', '');
    activate(panels[initial] ? initial : 'schedules');
  })();


document.querySelectorAll('.roster-filter-toggle').forEach(function (cb) {
  cb.addEventListener('change', function () {
    var li = cb.closest('.roster-filter-row');
    if (!li) return;
    var odoo_id = parseInt(li.dataset.odooId, 10);
    var excluded = !cb.checked;
    var origColor = li.style.background;
    li.style.background = 'var(--accent-dim)';
    fetch('/api/settings/roster-filter/toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({odoo_id: odoo_id, excluded: excluded}),
    }).then(function (r) {
      if (!r.ok) {
        cb.checked = !cb.checked;
        li.style.background = 'var(--bad-dim)';
        setTimeout(function () { li.style.background = origColor; }, 1200);
      } else {
        setTimeout(function () { li.style.background = origColor; }, 600);
      }
    }).catch(function () {
      cb.checked = !cb.checked;
      li.style.background = 'var(--bad-dim)';
      setTimeout(function () { li.style.background = origColor; }, 1200);
    });
  });
});
