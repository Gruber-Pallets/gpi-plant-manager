  // Session-only name search. The matrix is view-only; only Reserve is
  // editable and autosaves below. Active/Reserve filtering lives in the
  // View popover and applies via .row-hidden, not display:none here.
  const filterInput = document.getElementById('wheel-filter');
  const rows = [...document.querySelectorAll('#skills-table tbody tr')];
  function apply() {
    const q = (filterInput.value || '').toLowerCase().trim();
    rows.forEach(r => {
      const name = r.querySelector('td.name').textContent.toLowerCase();
      const nameMatch = !q || name.includes(q);
      r.style.display = nameMatch ? '' : 'none';
    });
  }
  filterInput.addEventListener('input', apply);
  apply();

  // Column sort: activate a header's matrix-sort-trigger button to sort rows.
  // Toggle asc/desc on repeat. The trigger is a real <button>, so Enter/Space
  // work natively — no th keydown handler — which lets the automation gear sit
  // beside it without nesting one activatable control inside another.
  (function () {
    const table = document.getElementById('skills-table');
    const tbody = table.querySelector('tbody');
    const ths = [...table.querySelectorAll('thead th')];
    let sortIndex = -1, sortDir = 1;
    function cellValue(tr, i) {
      const td = tr.children[i];
      if (!td) return '';
      const skillDisplay = td.querySelector('.skill-display');
      if (skillDisplay) {
        // Pull the level from the lvl-N class so '—' sorts as 0.
        const m = (skillDisplay.className.match(/lvl-(\d)/) || [, '0'])[1];
        return parseInt(m, 10);
      }
      const badge = td.querySelector('span.active-badge');
      if (badge) return badge.classList.contains('on') ? 1 : 0;
      const cb = td.querySelector('input[type=checkbox]');
      if (cb) return cb.checked ? 1 : 0;
      return td.textContent.trim().toLowerCase();
    }
    function sortBy(i, th) {
      if (sortIndex === i) sortDir = -sortDir; else { sortIndex = i; sortDir = 1; }
      ths.forEach(x => {
        x.classList.remove('sort-asc', 'sort-desc');
        x.setAttribute('aria-sort', 'none');
      });
      th.classList.add(sortDir > 0 ? 'sort-asc' : 'sort-desc');
      th.setAttribute('aria-sort', sortDir > 0 ? 'ascending' : 'descending');
      const rows = [...tbody.querySelectorAll('tr')];
      rows.sort((a, b) => {
        const va = cellValue(a, i), vb = cellValue(b, i);
        if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * sortDir;
        return String(va).localeCompare(String(vb)) * sortDir;
      });
      rows.forEach(r => tbody.appendChild(r));
    }
    ths.forEach((th, i) => {
      const trigger = th.querySelector('.matrix-sort-trigger');
      if (!trigger) return;
      trigger.addEventListener('click', () => sortBy(i, th));
    });
  })();

  // ---------- Autosave (Reserve only) + Undo + top-center toast ----------
  const form = document.getElementById('skills-form');
  const undoBtn = document.getElementById('undo-btn');
  const redoBtn = document.getElementById('redo-btn');

  function serializeForm(f) {
    const map = {};
    for (const [k, v] of new FormData(f).entries()) {
      if (!(k in map)) map[k] = [];
      map[k].push(v);
    }
    return map;
  }
  function applyState(f, snap) {
    for (const el of f.querySelectorAll('input, select, textarea')) {
      if (!el.name) continue;
      const vals = snap[el.name] || [];
      if (el.type === 'checkbox' || el.type === 'radio') {
        el.checked = vals.includes(el.value);
      } else {
        el.value = vals[0] ?? '';
      }
    }
  }

  let __undoSnapshot = serializeForm(form);
  let __lastUndoSnap = null;
  let __lastRedoSnap = null;
  let __saveTimer = null;
  let __saving = false;

  function updateBtns() {
    undoBtn.disabled = !__lastUndoSnap;
    redoBtn.disabled = !__lastRedoSnap;
  }

  function doSave() {
    if (__saving) { scheduleAutosave(); return; }
    __saving = true;
    const before = __undoSnapshot;
    const after = serializeForm(form);
    fetch(form.action, { method: 'POST', body: new FormData(form), headers: { 'Accept': 'application/json' } })
      .then(r => {
        if (r.ok) {
          __undoSnapshot = after;
          __lastUndoSnap = before;
          __lastRedoSnap = null;  // new save branches the history
          updateBtns();
          showSavedToast(before);
        } else {
          showSavedToast(null, 'Save failed');
        }
      })
      .catch(() => showSavedToast(null, 'Save failed'))
      .finally(() => { __saving = false; });
  }
  function scheduleAutosave() {
    clearTimeout(__saveTimer);
    __saveTimer = setTimeout(doSave, 600);
  }

  function performUndo(snap) {
    if (!snap) return;
    const beforeRevert = serializeForm(form);
    applyState(form, snap);
    clearTimeout(__saveTimer);
    __saving = true;
    fetch(form.action, { method: 'POST', body: new FormData(form), headers: { 'Accept': 'application/json' } })
      .finally(() => {
        __undoSnapshot = serializeForm(form);
        __lastUndoSnap = null;
        __lastRedoSnap = beforeRevert;  // enable redo to put it back
        updateBtns();
        __saving = false;
        showSavedToast(null, 'Reverted');
      });
  }
  function performRedo(snap) {
    if (!snap) return;
    const beforeRedo = serializeForm(form);
    applyState(form, snap);
    clearTimeout(__saveTimer);
    __saving = true;
    fetch(form.action, { method: 'POST', body: new FormData(form), headers: { 'Accept': 'application/json' } })
      .finally(() => {
        __undoSnapshot = serializeForm(form);
        __lastRedoSnap = null;
        __lastUndoSnap = beforeRedo;  // re-arm undo
        updateBtns();
        __saving = false;
        showSavedToast(null, 'Redone');
      });
  }

  undoBtn.addEventListener('click', () => performUndo(__lastUndoSnap));
  redoBtn.addEventListener('click', () => performRedo(__lastRedoSnap));

  // Don't actually submit — Enter in the search box would otherwise GET the page.
  form.addEventListener('submit', (e) => e.preventDefault());

  // Autosave only on real data changes — Reserve checkboxes are the sole
  // editable inputs in the matrix.  Search/"active only" filter inputs live
  // outside the table and have no `name`.
  document.getElementById('skills-table').addEventListener('change', scheduleAutosave);

  // Flush any pending debounced save before the page unloads. Without this, a
  // recent toggle is lost because the 600ms autosave timer never fires.
  window.addEventListener('beforeunload', () => {
    if (!__saveTimer) return;
    clearTimeout(__saveTimer);
    __saveTimer = null;
    if (navigator.sendBeacon) navigator.sendBeacon(form.action, new FormData(form));
  });

  function showSavedToast(undoSnap, errorMsg) {
    let bd = document.getElementById('save-toast-bd');
    if (!bd) {
      bd = document.createElement('div');
      bd.id = 'save-toast-bd';
      bd.className = 'save-toast-bd';
      document.body.appendChild(bd);
    }
    const el = document.createElement('div');
    el.className = 'save-toast' + (errorMsg ? ' error' : '');
    if (errorMsg) {
      el.setAttribute('role', 'alert');
      el.setAttribute('aria-live', 'assertive');
    }
    const label = document.createElement('span');
    label.textContent = errorMsg || 'Saved';
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

  // ---------- Live skill cell picker ----------
  (function initSkillCellPicker() {
    const table = document.getElementById('skills-table');
    if (!table) return;

    const LEVELS = [
      { level: 0, label: 'not trained', text: '—' },
      { level: 1, label: 'practicing', text: '1' },
      { level: 2, label: 'competent', text: '2' },
      { level: 3, label: 'proficient', text: '3' },
    ];

    let picker = null;
    let activeSkillButton = null;

    function levelLabel(level) {
      const found = LEVELS.find(item => item.level === Number(level));
      return found ? found.label : 'not trained';
    }

    function closePicker() {
      if (picker) {
        picker.remove();
        picker = null;
      }
      if (activeSkillButton) {
        activeSkillButton.setAttribute('aria-expanded', 'false');
      }
    }

    function updateSkillButton(btn, level) {
      const numeric = Number(level);
      btn.dataset.level = String(numeric);
      btn.textContent = numeric > 0 ? String(numeric) : '—';
      btn.classList.remove('lvl-0', 'lvl-1', 'lvl-2', 'lvl-3');
      btn.classList.add('lvl-' + numeric);
      const person = btn.dataset.personName || 'person';
      const skill = btn.dataset.skillName || 'skill';
      btn.setAttribute(
        'aria-label',
        'Edit ' + person + ' ' + skill + ' skill, current level ' + numeric + ' ' + levelLabel(numeric)
      );
    }

    async function saveSkillLevel(btn, level) {
      const previousLevel = Number(btn.dataset.level || '0');
      if (Number(level) === previousLevel) {
        closePicker();
        return;
      }

      btn.disabled = true;
      btn.classList.add('saving');
      closePicker();

      try {
        const resp = await fetch('/staffing/skills/cell', {
          method: 'POST',
          headers: {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            person_odoo_id: Number(btn.dataset.personOdooId),
            skill_odoo_id: Number(btn.dataset.skillOdooId),
            level: Number(level),
          }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.ok) {
          throw new Error(data.error || 'Odoo save failed');
        }
        updateSkillButton(btn, data.level);
        showSavedToast(null, data.warning || undefined);
      } catch (e) {
        updateSkillButton(btn, previousLevel);
        showSavedToast(null, e && e.message ? e.message : 'Odoo save failed');
      } finally {
        btn.disabled = false;
        btn.classList.remove('saving');
        btn.focus();
      }
    }

    function openPicker(btn) {
      closePicker();
      activeSkillButton = btn;
      btn.setAttribute('aria-expanded', 'true');

      picker = document.createElement('div');
      picker.className = 'skill-picker';
      picker.id = 'skill-picker';
      picker.setAttribute('role', 'dialog');
      picker.setAttribute('aria-label', 'Choose skill level');

      LEVELS.forEach(item => {
        const choice = document.createElement('button');
        choice.type = 'button';
        choice.className = 'skill-picker-choice lvl-' + item.level;
        choice.dataset.level = String(item.level);
        choice.textContent = item.level + ' ' + item.label;
        if (item.level === 0) choice.textContent = '0 ' + item.label;
        choice.addEventListener('click', () => saveSkillLevel(btn, item.level));
        picker.appendChild(choice);
      });

      document.body.appendChild(picker);
      const rect = btn.getBoundingClientRect();
      picker.style.top = String(window.scrollY + rect.bottom + 4) + 'px';
      picker.style.left = String(window.scrollX + rect.left) + 'px';

      const first = picker.querySelector('button');
      if (first) first.focus();
    }

    table.addEventListener('click', e => {
      const btn = e.target.closest('.skill-cell-btn');
      if (!btn || btn.disabled) return;
      openPicker(btn);
    });

    table.addEventListener('keydown', e => {
      const btn = e.target.closest('.skill-cell-btn');
      if (!btn || btn.disabled) return;
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openPicker(btn);
      }
    });

    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && picker) {
        closePicker();
        if (activeSkillButton) activeSkillButton.focus();
      }
    });

    document.addEventListener('click', e => {
      if (!picker) return;
      if (picker.contains(e.target)) return;
      if (e.target.closest('.skill-cell-btn') === activeSkillButton) return;
      closePicker();
    });
  })();

  // ---------- View ▾ popover (replaces Columns ▾) ----------
  // Saved server-side views; client-session state in localStorage with a
  // dirty flag. Filters apply via .col-hidden (TH/TD) and .row-hidden (TR).
  (function initViewPopover() {
    const btn = document.getElementById('view-btn');
    const pop = document.getElementById('view-popover');
    if (!btn || !pop) return;

    // ---- Server bootstrap data (window.* set inline in skills.html) ----
    let views = window.SKILLS_VIEWS;
    const defaultViewName = window.SKILLS_DEFAULT_VIEW_NAME;
    const defaultViewState = window.SKILLS_DEFAULT_VIEW_STATE;
    const allPeople = window.SKILLS_ALL_PEOPLE;
    const skillTypes = window.SKILLS_SKILL_TYPES;
    const allSkills = window.SKILLS_ALL_SKILLS;

    // ---- Local state ----
    const STORAGE_KEY = 'skillMatrixSession';
    const HARD_DEFAULTS = () => ({
      hidden_skills: [],
      visible_people: null,
      active_filter: 'active',
      reserve_filter: 'include',
    });

    function normalizeReserve(rf) {
      // 'all' is legacy; render as 'include'.
      if (rf === 'all' || !rf) return 'include';
      if (rf === 'include' || rf === 'exclude' || rf === 'only') return rf;
      return 'include';
    }
    function normalizeActive(af) {
      if (af === 'active' || af === 'inactive' || af === 'all') return af;
      return 'active';
    }
    function normalizeView(v) {
      // Server view dict -> normalized current state.
      return {
        hidden_skills: Array.isArray(v.hidden_skills) ? v.hidden_skills.slice() : [],
        visible_people: Array.isArray(v.visible_people) && v.visible_people.length > 0 ? v.visible_people.slice() : null,
        active_filter: normalizeActive(v.active_filter),
        reserve_filter: normalizeReserve(v.reserve_filter),
      };
    }

    let session = null;  // {loaded_view_name, current, dirty}

    function loadSession() {
      try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (raw) {
          const parsed = JSON.parse(raw);
          if (parsed && typeof parsed === 'object' && parsed.current) {
            return {
              loaded_view_name: parsed.loaded_view_name || null,
              current: {
                hidden_skills: Array.isArray(parsed.current.hidden_skills) ? parsed.current.hidden_skills.slice() : [],
                visible_people: Array.isArray(parsed.current.visible_people) ? parsed.current.visible_people.slice() : (parsed.current.visible_people === null ? null : null),
                active_filter: normalizeActive(parsed.current.active_filter),
                reserve_filter: normalizeReserve(parsed.current.reserve_filter),
              },
              dirty: !!parsed.dirty,
            };
          }
        }
      } catch (e) { /* fall through */ }
      // No valid session; fall back.
      if (defaultViewState) {
        return {
          loaded_view_name: defaultViewName,
          current: normalizeView(defaultViewState),
          dirty: false,
        };
      }
      return {
        loaded_view_name: null,
        current: HARD_DEFAULTS(),
        dirty: false,
      };
    }

    function saveSession() {
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(session)); } catch (e) {}
    }

    function findView(name) {
      return (views || []).find(v => v.name === name) || null;
    }

    function setsEqual(a, b) {
      if (a === b) return true;
      if (a === null || b === null) return false;
      if (a.length !== b.length) return false;
      const sa = new Set(a);
      for (const x of b) if (!sa.has(x)) return false;
      return true;
    }

    function statesEqual(a, b) {
      if (!a || !b) return false;
      if (normalizeActive(a.active_filter) !== normalizeActive(b.active_filter)) return false;
      if (normalizeReserve(a.reserve_filter) !== normalizeReserve(b.reserve_filter)) return false;
      if (!setsEqual(a.hidden_skills || [], b.hidden_skills || [])) return false;
      const av = (Array.isArray(a.visible_people) && a.visible_people.length > 0) ? a.visible_people : null;
      const bv = (Array.isArray(b.visible_people) && b.visible_people.length > 0) ? b.visible_people : null;
      if (av === null && bv === null) return true;
      if (av === null || bv === null) return false;
      return setsEqual(av, bv);
    }

    function recomputeDirty() {
      if (!session.loaded_view_name) {
        // No view loaded — dirty has no meaning here; treat as false.
        session.dirty = false;
        return;
      }
      const v = findView(session.loaded_view_name);
      if (!v) {
        session.dirty = true;
        return;
      }
      const saved = normalizeView(v);
      session.dirty = !statesEqual(session.current, saved);
    }

    // ---- Apply state to DOM ----
    function applyState(state) {
      const hideSkillSet = new Set(state.hidden_skills || []);
      // Columns
      document.querySelectorAll('.skills-table th[data-skill], .skills-table td[data-skill]').forEach(el => {
        el.classList.toggle('col-hidden', hideSkillSet.has(el.dataset.skill));
      });
      // Rows
      const visibleSet = (Array.isArray(state.visible_people) && state.visible_people.length > 0)
        ? new Set(state.visible_people) : null;
      document.querySelectorAll('.skills-table tbody tr[data-name]').forEach(tr => {
        const name = tr.dataset.name;
        const isActive = tr.dataset.active === '1';
        const isReserve = tr.dataset.reserve === '1';
        let hide = false;
        if (state.active_filter === 'active' && !isActive) hide = true;
        if (state.active_filter === 'inactive' && isActive) hide = true;
        if (state.reserve_filter === 'exclude' && isReserve) hide = true;
        if (state.reserve_filter === 'only' && !isReserve) hide = true;
        if (visibleSet && !visibleSet.has(name)) hide = true;
        tr.classList.toggle('row-hidden', hide);
      });
    }

    // ---- Popover render ----
    function render() {
      pop.innerHTML = '';

      // Loaded view section
      const loadedSec = document.createElement('div');
      loadedSec.className = 'view-section';
      const loadedHdr = document.createElement('div');
      loadedHdr.className = 'view-section-header';
      loadedHdr.textContent = 'Loaded view';
      loadedSec.appendChild(loadedHdr);

      const loadedRow = document.createElement('div');
      loadedRow.className = 'view-loaded';
      const sel = document.createElement('select');
      const blankOpt = document.createElement('option');
      blankOpt.value = '';
      blankOpt.textContent = '(no view)';
      sel.appendChild(blankOpt);
      for (const v of (views || [])) {
        const opt = document.createElement('option');
        opt.value = v.name;
        opt.textContent = v.name + (v.is_default ? ' (default)' : '');
        if (v.name === session.loaded_view_name) opt.selected = true;
        sel.appendChild(opt);
      }
      const newOpt = document.createElement('option');
      newOpt.value = '__new__';
      newOpt.textContent = '+ Save new view…';
      sel.appendChild(newOpt);
      sel.addEventListener('change', () => {
        const name = sel.value;
        if (name === '__new__') {
          handleSaveAsNew();
          return;
        }
        if (name === '') {
          session.loaded_view_name = null;
          recomputeDirty();
          saveSession();
          render();
          return;
        }
        const v = findView(name);
        if (v) {
          session.loaded_view_name = v.name;
          session.current = normalizeView(v);
          recomputeDirty();
          saveSession();
          applyState(session.current);
          render();
        }
      });
      loadedRow.appendChild(sel);
      const dot = document.createElement('span');
      dot.className = 'view-dirty-dot';
      dot.textContent = '● unsaved';
      if (!session.dirty) dot.setAttribute('hidden', '');
      loadedRow.appendChild(dot);
      loadedSec.appendChild(loadedRow);
      pop.appendChild(loadedSec);

      // Active section
      const activeSec = document.createElement('div');
      activeSec.className = 'view-section';
      const activeHdr = document.createElement('div');
      activeHdr.className = 'view-section-header';
      activeHdr.textContent = 'Active';
      activeSec.appendChild(activeHdr);
      const activeOpts = [
        ['active', 'Active only'],
        ['inactive', 'Inactive only'],
        ['all', 'All'],
      ];
      for (const [val, label] of activeOpts) {
        const row = document.createElement('div');
        row.className = 'view-row';
        const lab = document.createElement('label');
        const r = document.createElement('input');
        r.type = 'radio';
        r.name = 'view-active';
        r.value = val;
        if (session.current.active_filter === val) r.checked = true;
        r.addEventListener('change', () => {
          if (r.checked) {
            session.current.active_filter = val;
            recomputeDirty();
            saveSession();
            applyState(session.current);
            render();
          }
        });
        lab.appendChild(r);
        lab.appendChild(document.createTextNode(' ' + label));
        row.appendChild(lab);
        activeSec.appendChild(row);
      }
      pop.appendChild(activeSec);

      // Reserve section
      const reserveSec = document.createElement('div');
      reserveSec.className = 'view-section';
      const reserveHdr = document.createElement('div');
      reserveHdr.className = 'view-section-header';
      reserveHdr.textContent = 'Reserve';
      reserveSec.appendChild(reserveHdr);
      const reserveOpts = [
        ['include', 'Include'],
        ['exclude', 'Exclude'],
        ['only', 'Reserves only'],
      ];
      for (const [val, label] of reserveOpts) {
        const row = document.createElement('div');
        row.className = 'view-row';
        const lab = document.createElement('label');
        const r = document.createElement('input');
        r.type = 'radio';
        r.name = 'view-reserve';
        r.value = val;
        if (session.current.reserve_filter === val) r.checked = true;
        r.addEventListener('change', () => {
          if (r.checked) {
            session.current.reserve_filter = val;
            recomputeDirty();
            saveSession();
            applyState(session.current);
            render();
          }
        });
        lab.appendChild(r);
        lab.appendChild(document.createTextNode(' ' + label));
        row.appendChild(lab);
        reserveSec.appendChild(row);
      }
      pop.appendChild(reserveSec);

      // People section
      const peopleSec = document.createElement('div');
      peopleSec.className = 'view-section';
      const peopleHdr = document.createElement('div');
      peopleHdr.className = 'view-section-header';
      peopleHdr.textContent = 'People';
      peopleSec.appendChild(peopleHdr);

      const allRow = document.createElement('div');
      allRow.className = 'view-row';
      const allLab = document.createElement('label');
      const allR = document.createElement('input');
      allR.type = 'radio';
      allR.name = 'view-people';
      allR.value = 'all';
      if (session.current.visible_people === null) allR.checked = true;
      allR.addEventListener('change', () => {
        if (allR.checked) {
          session.current.visible_people = null;
          recomputeDirty();
          saveSession();
          applyState(session.current);
          render();
        }
      });
      allLab.appendChild(allR);
      allLab.appendChild(document.createTextNode(' All people'));
      allRow.appendChild(allLab);
      peopleSec.appendChild(allRow);

      const selRow = document.createElement('div');
      selRow.className = 'view-row';
      const selLab = document.createElement('label');
      const selR = document.createElement('input');
      selR.type = 'radio';
      selR.name = 'view-people';
      selR.value = 'selected';
      if (session.current.visible_people !== null) selR.checked = true;
      selR.addEventListener('change', () => {
        if (selR.checked) {
          // Default to currently-empty selection (all hidden) — user clicks Edit to populate.
          if (session.current.visible_people === null) {
            session.current.visible_people = [];
          }
          recomputeDirty();
          saveSession();
          applyState(session.current);
          render();
        }
      });
      selLab.appendChild(selR);
      selLab.appendChild(document.createTextNode(' Selected only'));
      selRow.appendChild(selLab);
      const editBtn = document.createElement('button');
      editBtn.type = 'button';
      editBtn.className = 'view-people-edit-btn';
      editBtn.textContent = 'Edit selection…';
      editBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        openPeoplePicker();
      });
      selRow.appendChild(editBtn);
      if (session.current.visible_people !== null) {
        const cnt = document.createElement('span');
        cnt.className = 'view-people-count';
        cnt.textContent = '(' + session.current.visible_people.length + ' / ' + allPeople.length + ')';
        selRow.appendChild(cnt);
      }
      peopleSec.appendChild(selRow);
      pop.appendChild(peopleSec);

      // Skill columns section
      const skillsSec = document.createElement('div');
      skillsSec.className = 'view-section';
      const skillsHdr = document.createElement('div');
      skillsHdr.className = 'view-section-header';
      skillsHdr.textContent = 'Skill Columns';
      skillsSec.appendChild(skillsHdr);

      // Group skills by type, preserving order from `allSkills`.
      const grouped = new Map();  // type -> [skill]
      for (const s of allSkills) {
        const t = skillTypes[s] || '(no type)';
        if (!grouped.has(t)) grouped.set(t, []);
        grouped.get(t).push(s);
      }
      const hiddenSet = new Set(session.current.hidden_skills);
      for (const [type, items] of grouped.entries()) {
        const groupEl = document.createElement('div');
        groupEl.className = 'view-skill-group';
        const gh = document.createElement('label');
        gh.className = 'view-skill-group-header';
        const gcb = document.createElement('input');
        gcb.type = 'checkbox';
        const allHidden = items.every(s => hiddenSet.has(s));
        const noneHidden = items.every(s => !hiddenSet.has(s));
        gcb.checked = noneHidden;
        gcb.indeterminate = !allHidden && !noneHidden;
        gcb.addEventListener('change', () => {
          const hide = !gcb.checked;
          for (const s of items) {
            if (hide) hiddenSet.add(s); else hiddenSet.delete(s);
          }
          session.current.hidden_skills = Array.from(hiddenSet);
          recomputeDirty();
          saveSession();
          applyState(session.current);
          render();
        });
        gh.appendChild(gcb);
        const ghText = document.createElement('span');
        ghText.textContent = ' ' + type + ' (' + items.length + ')';
        gh.appendChild(ghText);
        groupEl.appendChild(gh);

        const grid = document.createElement('div');
        grid.className = 'view-skill-grid';
        for (const s of items) {
          const item = document.createElement('label');
          item.className = 'view-skill-item';
          const cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.checked = !hiddenSet.has(s);
          cb.addEventListener('change', () => {
            if (cb.checked) hiddenSet.delete(s); else hiddenSet.add(s);
            session.current.hidden_skills = Array.from(hiddenSet);
            recomputeDirty();
            saveSession();
            applyState(session.current);
            render();
          });
          item.appendChild(cb);
          const sp = document.createElement('span');
          sp.textContent = ' ' + s;
          item.appendChild(sp);
          grid.appendChild(item);
        }
        groupEl.appendChild(grid);
        skillsSec.appendChild(groupEl);
      }
      pop.appendChild(skillsSec);

      // Actions section
      const actSec = document.createElement('div');
      actSec.className = 'view-section';
      const actHdr = document.createElement('div');
      actHdr.className = 'view-section-header';
      actHdr.textContent = 'Actions';
      actSec.appendChild(actHdr);
      const grid2 = document.createElement('div');
      grid2.className = 'view-actions';

      const saveChangesBtn = document.createElement('button');
      saveChangesBtn.type = 'button';
      saveChangesBtn.textContent = 'Save changes';
      saveChangesBtn.disabled = !(session.loaded_view_name && session.dirty);
      saveChangesBtn.addEventListener('click', handleSaveChanges);
      grid2.appendChild(saveChangesBtn);

      const saveNewBtn = document.createElement('button');
      saveNewBtn.type = 'button';
      saveNewBtn.textContent = 'Save as new…';
      saveNewBtn.addEventListener('click', handleSaveAsNew);
      grid2.appendChild(saveNewBtn);

      const setDefaultBtn = document.createElement('button');
      setDefaultBtn.type = 'button';
      setDefaultBtn.textContent = 'Set as default';
      setDefaultBtn.disabled = !session.loaded_view_name;
      setDefaultBtn.addEventListener('click', handleSetDefault);
      grid2.appendChild(setDefaultBtn);

      const delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.className = 'danger';
      delBtn.textContent = 'Delete view';
      delBtn.disabled = !session.loaded_view_name;
      delBtn.addEventListener('click', handleDelete);
      grid2.appendChild(delBtn);

      actSec.appendChild(grid2);
      pop.appendChild(actSec);
    }

    // ---- People picker ----
    let pickerEl = null;
    function closePicker() {
      if (pickerEl) { pickerEl.remove(); pickerEl = null; }
    }
    function openPeoplePicker() {
      closePicker();
      pickerEl = document.createElement('div');
      pickerEl.className = 'people-picker';
      // Position it next to the popover via CSS (absolute, top:0, right:100%).
      // Append to popover so it positions relative to it.
      pop.style.position = 'absolute';

      const search = document.createElement('input');
      search.type = 'search';
      search.placeholder = 'Search…';
      pickerEl.appendChild(search);

      const list = document.createElement('div');
      list.className = 'people-picker-list';
      pickerEl.appendChild(list);

      // Working copy of selection
      let selected = new Set(
        Array.isArray(session.current.visible_people)
          ? session.current.visible_people
          : allPeople.map(p => p.name)
      );

      function renderList() {
        list.innerHTML = '';
        const q = (search.value || '').toLowerCase().trim();
        for (const p of allPeople) {
          if (q && !p.name.toLowerCase().includes(q)) continue;
          const row = document.createElement('label');
          row.className = 'people-picker-row';
          const cb = document.createElement('input');
          cb.type = 'checkbox';
          cb.checked = selected.has(p.name);
          cb.addEventListener('change', () => {
            if (cb.checked) selected.add(p.name); else selected.delete(p.name);
          });
          row.appendChild(cb);
          const sp = document.createElement('span');
          sp.textContent = p.name;
          row.appendChild(sp);
          list.appendChild(row);
        }
      }
      search.addEventListener('input', renderList);
      renderList();

      const actions = document.createElement('div');
      actions.className = 'people-picker-actions';
      const selAll = document.createElement('button');
      selAll.type = 'button';
      selAll.textContent = 'Select all';
      selAll.addEventListener('click', (e) => {
        e.stopPropagation();
        selected = new Set(allPeople.map(p => p.name));
        renderList();
      });
      const clearBtn = document.createElement('button');
      clearBtn.type = 'button';
      clearBtn.textContent = 'Clear';
      clearBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        selected = new Set();
        renderList();
      });
      const doneBtn = document.createElement('button');
      doneBtn.type = 'button';
      doneBtn.textContent = 'Done';
      doneBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        // If everyone is selected, revert to "All people" (null).
        if (selected.size === allPeople.length) {
          session.current.visible_people = null;
        } else {
          session.current.visible_people = Array.from(selected);
        }
        recomputeDirty();
        saveSession();
        applyState(session.current);
        closePicker();
        render();
      });
      actions.appendChild(selAll);
      actions.appendChild(clearBtn);
      actions.appendChild(doneBtn);
      pickerEl.appendChild(actions);

      pop.appendChild(pickerEl);
      // Click inside picker shouldn't close popover.
      pickerEl.addEventListener('click', (e) => e.stopPropagation());
    }

    // ---- Save flows ----
    function currentBody(name) {
      return {
        name: name,
        hidden_skills: session.current.hidden_skills.slice(),
        visible_people: session.current.visible_people === null ? null : session.current.visible_people.slice(),
        active_filter: session.current.active_filter,
        reserve_filter: session.current.reserve_filter,
      };
    }

    function refreshViews(updatedView, opts) {
      // updatedView: the {view: {...}} payload's view dict, optional.
      // opts.removedName: name to drop from local list.
      // opts.setDefaultName: name to mark default (and clear others).
      // opts.clearDefault: if true, clear all is_default flags.
      opts = opts || {};
      let next = (views || []).slice();
      if (opts.removedName) {
        next = next.filter(v => v.name !== opts.removedName);
      }
      if (updatedView) {
        const idx = next.findIndex(v => v.name === updatedView.name);
        if (idx >= 0) next[idx] = updatedView;
        else next.push(updatedView);
      }
      if (opts.setDefaultName) {
        next = next.map(v => Object.assign({}, v, { is_default: v.name === opts.setDefaultName }));
      }
      if (opts.clearDefault) {
        next = next.map(v => Object.assign({}, v, { is_default: false }));
      }
      views = next;
    }

    function handleSaveChanges() {
      if (!session.loaded_view_name || !session.dirty) return;
      const name = session.loaded_view_name;
      fetch('/staffing/skills/views/' + encodeURIComponent(name), {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(currentBody(name)),
      }).then(async (r) => {
        if (!r.ok) {
          alert('Save failed: ' + r.status);
          return;
        }
        let payload = null;
        try { payload = await r.json(); } catch (e) {}
        const view = (payload && payload.view) ? payload.view : Object.assign({ is_default: false }, currentBody(name));
        refreshViews(view);
        session.dirty = false;
        saveSession();
        render();
      }).catch(e => alert('Save failed: ' + e));
    }

    function handleSaveAsNew() {
      let proposed = '';
      while (true) {
        proposed = prompt('View name?', proposed || '');
        if (proposed === null) { render(); return; }
        const name = proposed.trim();
        if (!name) { alert('Name required'); continue; }
        const body = currentBody(name);
        const result = fetch('/staffing/skills/views', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body),
        });
        // Async handling — break out of the loop and use .then chain.
        result.then(async (r) => {
          if (r.status === 409) {
            alert('A view named "' + name + '" already exists.');
            handleSaveAsNew();  // re-prompt
            return;
          }
          if (!r.ok) { alert('Save failed: ' + r.status); render(); return; }
          let payload = null;
          try { payload = await r.json(); } catch (e) {}
          const view = (payload && payload.view) ? payload.view : Object.assign({ is_default: false }, body);
          refreshViews(view);
          session.loaded_view_name = name;
          session.dirty = false;
          saveSession();
          render();
        }).catch(e => alert('Save failed: ' + e));
        return;
      }
    }

    function handleSetDefault() {
      if (!session.loaded_view_name) return;
      const name = session.loaded_view_name;
      fetch('/staffing/skills/views/' + encodeURIComponent(name) + '/default', {
        method: 'POST',
      }).then(r => {
        if (!r.ok) { alert('Set default failed: ' + r.status); return; }
        refreshViews(null, { setDefaultName: name });
        render();
      }).catch(e => alert('Set default failed: ' + e));
    }

    function handleDelete() {
      if (!session.loaded_view_name) return;
      const name = session.loaded_view_name;
      if (!confirm('Delete view "' + name + '"?')) return;
      fetch('/staffing/skills/views/' + encodeURIComponent(name), {
        method: 'DELETE',
      }).then(r => {
        if (!r.ok) { alert('Delete failed: ' + r.status); return; }
        refreshViews(null, { removedName: name });
        // Fall back to default view (if any other than the deleted) or hard defaults.
        const def = (views || []).find(v => v.is_default);
        if (def) {
          session.loaded_view_name = def.name;
          session.current = normalizeView(def);
        } else {
          session.loaded_view_name = null;
          session.current = HARD_DEFAULTS();
        }
        recomputeDirty();
        saveSession();
        applyState(session.current);
        render();
      }).catch(e => alert('Delete failed: ' + e));
    }

    // ---- Init ----
    session = loadSession();
    recomputeDirty();
    applyState(session.current);

    function closeViewPopover({ restoreFocus = false } = {}) {
      pop.setAttribute('hidden', '');
      btn.setAttribute('aria-expanded', 'false');
      closePicker();
      if (restoreFocus) btn.focus();
    }

    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = !pop.hasAttribute('hidden');
      if (isOpen) {
        closeViewPopover();
      } else {
        render();
        pop.removeAttribute('hidden');
        btn.setAttribute('aria-expanded', 'true');
      }
    });

    document.addEventListener('click', (e) => {
      if (pop.hasAttribute('hidden')) return;
      if (pop.contains(e.target) || e.target === btn) return;
      closeViewPopover();
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !pop.hasAttribute('hidden')) {
        e.preventDefault();
        closeViewPopover({ restoreFocus: true });
      }
    });
  })();

  // ---------- Refresh from Odoo: AJAX with live status feedback ----------
  // The Odoo XML-RPC sync can take 5–20s when the Odoo.sh container is
  // cold-starting. The previous form-submit button gave no feedback and
  // would silently time out at Railway's 30s gateway. AJAX it so we can
  // show "Refreshing..." text and surface errors cleanly.
  (function initRefreshFromOdoo() {
    const btn = document.getElementById('refresh-btn');
    const status = document.getElementById('sync-status');
    if (!btn || !status) return;
    const originalLabel = btn.textContent;
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      btn.textContent = 'Refreshing…';
      status.innerHTML = '<em>Refreshing from Odoo… can take a few seconds while Odoo wakes up.</em>';
      try {
        const resp = await fetch('/staffing/skills/refresh', {
          method: 'POST',
          headers: { 'Accept': 'application/json' },
        });
        const data = await resp.json().catch(() => ({}));
        if (data.ok) {
          status.innerHTML = '<em>Synced ' + (data.employee_count || 0) +
            ' employees, ' + (data.skill_column_count || 0) +
            ' skills. Reloading…</em>';
          window.location.reload();
        } else {
          status.innerHTML = '<span class="sync-error">⚠ ' +
            (data.error ? String(data.error).slice(0, 200) : 'Refresh failed') +
            '</span>';
          btn.disabled = false;
          btn.setAttribute('aria-busy', 'false');
          btn.textContent = originalLabel;
        }
      } catch (e) {
        status.innerHTML = '<span class="sync-error">⚠ Network error: ' +
          (e && e.message ? e.message : 'unknown') + '</span>';
        btn.disabled = false;
        btn.setAttribute('aria-busy', 'false');
        btn.textContent = originalLabel;
      }
    });
  })();

  // ---------- Recycled Rotation editor (per-person preferences + blocks) ----------
  // One shared modal opened per row via the ⟳ button. Preferences POST one
  // changed select to /api/rotations/preferences; the level-0-only training
  // block form POSTs to /api/rotations/training-blocks; pause/resume/end route
  // through the per-block lifecycle endpoints. Reuses the page's showSavedToast.
  (function initRotationEditor() {
    const backdrop = document.getElementById('rotation-modal-backdrop');
    const modal = document.getElementById('rotation-modal');
    if (!backdrop || !modal) return;

    const GROUPS = Array.isArray(window.ROTATION_GROUPS) ? window.ROTATION_GROUPS : [];
    const PREFS = window.ROTATION_PREFERENCES || {};
    const PREFERENCE_TARGETS_BY_PERSON = window.ROTATION_PREFERENCE_TARGETS_BY_PERSON || {};
    const LEVELS = window.ROTATION_LEVELS || {};
    const ACTIVE_PEOPLE = Array.isArray(window.ROTATION_ACTIVE_PEOPLE) ? window.ROTATION_ACTIVE_PEOPLE : [];
    let BLOCKS = Array.isArray(window.ROTATION_ACTIVE_BLOCKS) ? window.ROTATION_ACTIVE_BLOCKS.slice() : [];

    const personLabel = document.getElementById('rotation-modal-person');
    const closeBtn = document.getElementById('rotation-modal-close');
    const prefGrid = document.getElementById('rotation-pref-grid');
    const blockList = document.getElementById('rotation-block-list');
    const blockEmpty = document.getElementById('rotation-block-empty');
    const blockErr = document.getElementById('rotation-block-error');
    const blockForm = document.getElementById('rotation-block-form');
    const unavailableNote = document.getElementById('rotation-block-unavailable');
    const groupSel = document.getElementById('rotation-block-group');
    const trainerSel = document.getElementById('rotation-block-trainer');
    const startInput = document.getElementById('rotation-block-start');
    const workdaysInput = document.getElementById('rotation-block-workdays');
    const submitBtn = document.getElementById('rotation-block-submit');

    let currentPerson = null;
    let opener = null;

    function levelOf(name, group) {
      const byGroup = LEVELS[name] || {};
      return Number(byGroup[group] || 0);
    }

    function todayISO() {
      const d = new Date();
      const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
      return local.toISOString().slice(0, 10);
    }

    async function postJSON(url, body) {
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await resp.json().catch(() => ({}));
      return { resp, data };
    }

    // ---- soft preferences ----
    function renderPreferences(person) {
      prefGrid.textContent = '';
      const saved = PREFS[person] || {};
      (PREFERENCE_TARGETS_BY_PERSON[person] || []).forEach(target => {
        const label = document.createElement('label');
        label.className = 'rotation-pref';
        const name = document.createElement('span');
        name.className = 'rotation-pref-group';
        name.textContent = target.label;
        const select = document.createElement('select');
        select.className = 'rotation-pref-select';
        select.dataset.rotationPreference = '';
        select.dataset.group = target.key;
        select.dataset.person = person;
        select.dataset.prev = saved[target.key] || 'regular';
        select.setAttribute('aria-label', target.label + ' scheduling preference');
        ['primary', 'regular', 'occasional', 'never'].forEach(value => {
          select.add(new Option(value[0].toUpperCase() + value.slice(1), value));
        });
        select.value = select.dataset.prev;
        select.addEventListener('change', () => savePreference(select));
        label.append(name, select);
        prefGrid.appendChild(label);
      });
    }

    async function savePreference(select) {
      const person = select.dataset.person;
      const group = select.dataset.group;
      const preference = select.value;
      if (!person) return;
      select.disabled = true;
      try {
        const { resp, data } = await postJSON('/api/rotations/preferences', { person, group, preference });
        if (!resp.ok || !data.ok) throw new Error(data.error || 'Save failed');
        select.dataset.prev = preference;
        if (!PREFS[person]) PREFS[person] = {};
        PREFS[person][group] = preference;
        showSavedToast(null);  // "Saved"
      } catch (e) {
        select.value = select.dataset.prev || 'regular';  // revert the visible choice
        showSavedToast(null, e && e.message ? e.message : 'Save failed');
      } finally {
        select.disabled = false;
      }
    }

    // ---- active training blocks ----
    function statusLabel(s) {
      if (s === 'paused') return 'Paused';
      if (s === 'active') return 'Active';
      return s;
    }

    function renderBlocks(person) {
      blockList.textContent = '';
      const mine = BLOCKS.filter(b =>
        b.trainee === person && b.status !== 'ended' && b.status !== 'completed'
      );
      if (!mine.length) {
        blockEmpty.hidden = false;
        return;
      }
      blockEmpty.hidden = true;
      mine.forEach(b => blockList.appendChild(renderBlockItem(b)));
    }

    function renderBlockItem(b) {
      const li = document.createElement('li');
      li.className = 'rotation-block-item';

      const info = document.createElement('div');
      info.className = 'rotation-block-info';
      const title = document.createElement('span');
      title.className = 'rotation-block-title';
      title.textContent = b.group + ' · trainer ' + b.trainer;
      const meta = document.createElement('span');
      meta.className = 'rotation-block-meta';
      meta.textContent = 'from ' + b.start_day + ' · ' + b.planned_attended_days +
        ' workdays · ' + statusLabel(b.status);
      info.appendChild(title);
      info.appendChild(meta);
      li.appendChild(info);

      const actions = document.createElement('div');
      actions.className = 'rotation-block-actions';
      if (b.status === 'active') {
        actions.appendChild(lifecycleBtn(b, 'pause', 'Pause'));
      } else if (b.status === 'paused') {
        actions.appendChild(lifecycleBtn(b, 'resume', 'Resume'));
      }
      actions.appendChild(lifecycleBtn(b, 'end', 'End'));
      li.appendChild(actions);
      return li;
    }

    function lifecycleBtn(b, action, label) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'rotation-block-btn' + (action === 'end' ? ' danger' : '');
      btn.textContent = label;
      btn.addEventListener('click', () => runLifecycle(b, action, btn));
      return btn;
    }

    async function runLifecycle(b, action, btn) {
      btn.disabled = true;
      try {
        const { resp, data } = await postJSON(
          '/api/rotations/training-blocks/' + b.id + '/' + action, {}
        );
        if (!resp.ok || !data.ok) throw new Error(data.error || 'Update failed');
        if (action === 'end') {
          BLOCKS = BLOCKS.filter(x => x.id !== b.id);
        } else {
          b.status = data.status;  // 'paused' or 'active'
        }
        renderBlocks(currentPerson);  // refresh the active-block list
        showSavedToast(null);
      } catch (e) {
        btn.disabled = false;
        showSavedToast(null, e && e.message ? e.message : 'Update failed');
      }
    }

    // ---- start a level-0 training block ----
    function eligibleGroups(person) {
      return GROUPS.filter(g => levelOf(person, g) === 0);
    }

    function fillTrainers(person, group) {
      trainerSel.textContent = '';
      const candidates = ACTIVE_PEOPLE.filter(n => n !== person && levelOf(n, group) === 3);
      if (!candidates.length) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = 'No level-3 trainer available';
        opt.disabled = true;
        trainerSel.appendChild(opt);
        return;
      }
      candidates.forEach(n => {
        const opt = document.createElement('option');
        opt.value = n;
        opt.textContent = n;
        trainerSel.appendChild(opt);
      });
    }

    function fillBlockForm(person) {
      blockErr.textContent = '';
      const groups = eligibleGroups(person);
      groupSel.textContent = '';
      if (!groups.length) {
        // Training blocks are only for a level-0 target skill.
        blockForm.hidden = true;
        unavailableNote.hidden = false;
        return;
      }
      blockForm.hidden = false;
      unavailableNote.hidden = true;
      groups.forEach(g => {
        const opt = document.createElement('option');
        opt.value = g;
        opt.textContent = g;
        groupSel.appendChild(opt);
      });
      fillTrainers(person, groupSel.value);
      startInput.value = todayISO();
      workdaysInput.value = '5';
    }

    groupSel.addEventListener('change', () => {
      if (currentPerson) fillTrainers(currentPerson, groupSel.value);
    });

    submitBtn.addEventListener('click', async () => {
      if (!currentPerson) return;
      blockErr.textContent = '';
      const parsedWorkdays = parseInt(workdaysInput.value, 10);
      submitBtn.disabled = true;
      try {
        const { resp, data } = await postJSON('/api/rotations/training-blocks', {
          trainee: currentPerson,
          trainer: trainerSel.value,
          group: groupSel.value,
          start_day: startInput.value,
          // Send the raw value when it isn't an integer so the server rejects it
          // with its own message instead of us silently coercing.
          workdays: Number.isFinite(parsedWorkdays) ? parsedWorkdays : workdaysInput.value,
        });
        if (!resp.ok || !data.ok) throw new Error(data.error || 'Could not start training block.');
        if (data.block) {
          BLOCKS.push({
            id: data.block.id,
            trainee: data.block.trainee,
            trainer: data.block.trainer,
            group: data.block.group,
            start_day: data.block.start_day,
            planned_attended_days: data.block.planned_attended_days,
            status: data.block.status,
          });
        }
        renderBlocks(currentPerson);   // refresh the active-block list
        fillBlockForm(currentPerson);  // reset the form + clear the error region
        showSavedToast(null);
      } catch (e) {
        // Non-200: surface the error and RETAIN every entered value.
        blockErr.textContent = e && e.message ? e.message : 'Could not start training block.';
      } finally {
        submitBtn.disabled = false;
      }
    });

    // ---- modal open/close ----
    function onKeydown(e) {
      if (e.key === 'Escape') closeModal();
    }

    function openModal(person, btn) {
      currentPerson = person;
      opener = btn || null;
      personLabel.textContent = person;
      renderPreferences(person);
      renderBlocks(person);
      fillBlockForm(person);
      backdrop.hidden = false;
      closeBtn.focus();
      document.addEventListener('keydown', onKeydown);
    }

    function closeModal() {
      backdrop.hidden = true;
      document.removeEventListener('keydown', onKeydown);
      if (opener && document.contains(opener)) opener.focus();
      currentPerson = null;
      opener = null;
    }

    closeBtn.addEventListener('click', closeModal);
    backdrop.addEventListener('click', e => {
      if (e.target === backdrop) closeModal();
    });

    document.querySelectorAll('.rotation-open-btn').forEach(btn => {
      btn.addEventListener('click', () => openModal(btn.dataset.person, btn));
    });
  })();

  // Automatic Repair/Dismantle skill settings. Opened from the gear beside the
  // Repair or Dismantle header. Reads thresholds + work-center goals from
  // window.AUTOMATION_GROUPS, previews the daily unit target each threshold
  // implies, and POSTs to the recalculate endpoint on save.
  (function () {
    const backdrop = document.getElementById('automation-modal-backdrop');
    if (!backdrop) return;
    const groups = window.AUTOMATION_GROUPS || {};
    const titleEl = document.getElementById('automation-modal-title');
    const gridEl = document.getElementById('automation-bucket-grid');
    const previewEl = document.getElementById('automation-preview');
    const statusEl = document.getElementById('automation-run-status');
    const saveBtn = document.getElementById('automation-save-btn');
    const closeBtn = document.getElementById('automation-modal-close');

    let lastAutomationTrigger = null;
    let activeAutomationSkill = null;

    const LEVELS = [
      { key: 'level_3_min', label: 'Level 3 · proficient' },
      { key: 'level_2_min', label: 'Level 2 · competent' },
      { key: 'level_1_min', label: 'Level 1 · practicing' },
    ];

    function fmtPct(v) {
      return (Math.round(Number(v) * 100) / 100).toString();
    }

    function readThresholds() {
      const out = {};
      gridEl.querySelectorAll('[data-automation-level]').forEach(input => {
        out[input.dataset.automationLevel] = Number(input.value);
      });
      return out;
    }

    function renderBucketGrid(settings) {
      gridEl.innerHTML = '';
      LEVELS.forEach(({ key, label }) => {
        const row = document.createElement('label');
        row.className = 'automation-bucket-row';
        const span = document.createElement('span');
        span.textContent = label;
        const input = document.createElement('input');
        input.type = 'number';
        input.min = '0';
        input.max = '100';
        input.step = '0.1';
        input.value = fmtPct(settings[key]);
        input.dataset.automationLevel = key;
        input.setAttribute('aria-label', label + ' minimum percent of goal');
        input.addEventListener('input', renderPreview);
        const suffix = document.createElement('span');
        suffix.className = 'automation-bucket-suffix';
        suffix.textContent = '% of goal';
        row.appendChild(span);
        row.appendChild(input);
        row.appendChild(suffix);
        gridEl.appendChild(row);
      });
    }

    function renderPreview() {
      const data = groups[activeAutomationSkill];
      if (!data) { previewEl.innerHTML = ''; return; }
      const thresholds = readThresholds();
      const centers = data.work_centers || [];
      if (!centers.length) {
        previewEl.innerHTML =
          '<p class="automation-preview-empty">No work-center goals configured for this group yet.</p>';
        return;
      }
      const body = centers.map(wc => {
        const goal = Number(wc.goal) || 0;
        const cells = LEVELS.map(({ key }) => {
          const pct = Number(thresholds[key]) || 0;
          const solo = Math.round(pct / 100 * goal);
          const paired = Math.round(pct / 100 * goal / 2);
          return '<td>' + solo + '<span class="automation-preview-sub"> / ' + paired + '</span></td>';
        }).join('');
        return '<tr><th scope="row">' + wc.name + '</th><td>' + Math.round(goal) + '</td>' + cells + '</tr>';
      }).join('');
      previewEl.innerHTML =
        '<table><caption>Full-day units needed to reach each level — alone / per person when two share the center</caption>' +
        '<thead><tr><th scope="col">Work center</th><th scope="col">Goal</th>' +
        '<th scope="col">L3</th><th scope="col">L2</th><th scope="col">L1</th></tr></thead>' +
        '<tbody>' + body + '</tbody></table>';
    }

    function renderRunSummary(summary) {
      if (!summary) { statusEl.textContent = ''; statusEl.className = 'automation-run-status'; return; }
      const parts = [
        summary.changed + ' changed',
        summary.unchanged + ' unchanged',
        summary.skipped + ' without enough data',
      ];
      let msg = 'Recalculated ' + summary.evaluated + ' eligible: ' + parts.join(', ') + '.';
      const failures = summary.failures || [];
      if (failures.length) {
        msg += ' Odoo rejected ' + failures.map(f => f.name).join(', ') + '.';
        statusEl.className = 'automation-run-status has-failures';
      } else {
        statusEl.className = 'automation-run-status ok';
      }
      if (summary.run_at) msg += ' (' + summary.trigger + ' run)';
      statusEl.textContent = msg;
    }

    function openAutomationModal(trigger) {
      const skill = trigger.dataset.automationSkill;
      const data = groups[skill];
      if (!data) return;
      lastAutomationTrigger = trigger;
      activeAutomationSkill = skill;
      titleEl.textContent = 'Automatic ' + skill + ' levels';
      renderBucketGrid(data.settings || {});
      renderPreview();
      renderRunSummary(data.last_run);
      saveBtn.disabled = false;
      backdrop.hidden = false;
      const firstInput = gridEl.querySelector('input');
      if (firstInput) firstInput.focus();
      document.addEventListener('keydown', onKeydown);
    }

    function closeAutomationModal() {
      backdrop.hidden = true;
      document.removeEventListener('keydown', onKeydown);
      activeAutomationSkill = null;
      if (lastAutomationTrigger && document.contains(lastAutomationTrigger)) {
        lastAutomationTrigger.focus();
      }
    }

    function onKeydown(e) {
      if (e.key === 'Escape') closeAutomationModal();
    }

    async function saveAutomationSettings() {
      const data = groups[activeAutomationSkill];
      if (!data) return;
      const payload = readThresholds();
      saveBtn.disabled = true;
      statusEl.setAttribute('role', 'status');
      statusEl.className = 'automation-run-status';
      statusEl.textContent = 'Saving and recalculating…';
      try {
        const response = await fetch('/staffing/skills/automation/' + encodeURIComponent(data.group), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const respBody = await response.json().catch(() => ({}));
        if (!response.ok || !respBody.ok) {
          statusEl.setAttribute('role', 'alert');
          statusEl.className = 'automation-run-status has-failures';
          statusEl.textContent = respBody.error || 'Could not save automatic skill settings.';
          return;
        }
        data.settings = respBody.settings;
        if (respBody.summary) data.last_run = respBody.summary;
        renderBucketGrid(respBody.settings);
        renderPreview();
        renderRunSummary(respBody.summary);
      } catch (err) {
        statusEl.setAttribute('role', 'alert');
        statusEl.className = 'automation-run-status has-failures';
        statusEl.textContent = 'Network error — settings were not saved.';
      } finally {
        saveBtn.disabled = false;
      }
    }

    document.querySelectorAll('.automation-settings-trigger').forEach(trigger => {
      trigger.addEventListener('click', event => {
        event.stopPropagation();
        openAutomationModal(trigger);
      });
    });
    closeBtn.addEventListener('click', closeAutomationModal);
    backdrop.addEventListener('click', e => { if (e.target === backdrop) closeAutomationModal(); });
    saveBtn.addEventListener('click', saveAutomationSettings);
  })();
