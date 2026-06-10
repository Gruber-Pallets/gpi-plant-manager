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

  // Column sort: click any <th> to sort rows. Toggle asc/desc on repeat.
  (function () {
    const table = document.getElementById('skills-table');
    const tbody = table.querySelector('tbody');
    const ths = [...table.querySelectorAll('thead th')];
    let sortIndex = -1, sortDir = 1;
    function cellValue(tr, i) {
      const td = tr.children[i];
      if (!td) return '';
      const skillSpan = td.querySelector('span.skill-display');
      if (skillSpan) {
        // Pull the level from the lvl-N class so '—' sorts as 0.
        const m = (skillSpan.className.match(/lvl-(\d)/) || [, '0'])[1];
        return parseInt(m, 10);
      }
      const badge = td.querySelector('span.active-badge');
      if (badge) return badge.classList.contains('on') ? 1 : 0;
      const cb = td.querySelector('input[type=checkbox]');
      if (cb) return cb.checked ? 1 : 0;
      return td.textContent.trim().toLowerCase();
    }
    ths.forEach((th, i) => {
      th.addEventListener('click', () => {
        if (sortIndex === i) sortDir = -sortDir; else { sortIndex = i; sortDir = 1; }
        ths.forEach(x => x.classList.remove('sort-asc', 'sort-desc'));
        th.classList.add(sortDir > 0 ? 'sort-asc' : 'sort-desc');
        const rows = [...tbody.querySelectorAll('tr')];
        rows.sort((a, b) => {
          const va = cellValue(a, i), vb = cellValue(b, i);
          if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * sortDir;
          return String(va).localeCompare(String(vb)) * sortDir;
        });
        rows.forEach(r => tbody.appendChild(r));
      });
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

    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = !pop.hasAttribute('hidden');
      if (isOpen) {
        pop.setAttribute('hidden', '');
        btn.setAttribute('aria-expanded', 'false');
        closePicker();
      } else {
        render();
        pop.removeAttribute('hidden');
        btn.setAttribute('aria-expanded', 'true');
      }
    });

    document.addEventListener('click', (e) => {
      if (pop.hasAttribute('hidden')) return;
      if (pop.contains(e.target) || e.target === btn) return;
      pop.setAttribute('hidden', '');
      btn.setAttribute('aria-expanded', 'false');
      closePicker();
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
          btn.textContent = originalLabel;
        }
      } catch (e) {
        status.innerHTML = '<span class="sync-error">⚠ Network error: ' +
          (e && e.message ? e.message : 'unknown') + '</span>';
        btn.disabled = false;
        btn.textContent = originalLabel;
      }
    });
  })();
