(() => {
  const panel = document.getElementById('saturday-recruiting');
  if (!panel) return;
  const error = document.getElementById('saturday-recruiting-error');
  const rows = document.getElementById('saturday-opening-rows');
  const template = document.getElementById('saturday-opening-draft-template');
  const day = panel.dataset.day;
  const endpoint = '/api/staffing/saturday-recruiting';

  function showError(message) { error.textContent = message; error.hidden = !message; }
  function busy(value) {
    panel.querySelectorAll('button').forEach(button => {
      if (value) {
        button.dataset.saturdayWasDisabled = String(button.disabled);
        button.disabled = true;
      } else {
        button.disabled = button.dataset.saturdayWasDisabled === 'true';
        delete button.dataset.saturdayWasDisabled;
      }
    });
  }
  function shiftValues() { return { shift_start: panel.querySelector('[data-shift-start]').value, shift_end: panel.querySelector('[data-shift-end]').value }; }
  function requestedCounts() {
    const counts = {};
    rows.querySelectorAll('[data-wc-id]').forEach(row => { counts[row.dataset.wcId] = Number(row.dataset.requested); });
    rows.querySelectorAll('.saturday-opening-draft').forEach(row => {
      const id = row.querySelector('[data-opening-wc]').value;
      counts[id] = (counts[id] || 0) + Number(row.querySelector('[data-opening-count]').value);
    });
    return counts;
  }
  async function post(path, payload) {
    busy(true); showError('');
    try {
      const response = await fetch(endpoint + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || 'Could not update Saturday recruiting.');
      window.location.assign('/staffing?day=' + encodeURIComponent(day));
    } catch (err) { showError(err.message); busy(false); }
  }
  function addDraft() { rows.appendChild(template.content.cloneNode(true)); }
  panel.addEventListener('click', event => {
    const button = event.target.closest('button'); if (!button || button.disabled) return;
    if (button.dataset.saturdayAction === 'add-draft-opening') return addDraft();
    if (button.dataset.saturdayAction === 'activate') return post('/activate', { day, ...shiftValues(), requested_counts: requestedCounts() });
    if (button.dataset.saturdayAction === 'add-opening') return addDraft();
    if (button.dataset.saturdayAction === 'save-openings') return post('/openings', { day, ...shiftValues(), requested_counts: requestedCounts() });
    if (button.dataset.saturdayAction === 'cancel') {
      const names = Array.from(panel.querySelectorAll('[data-committed-name]')).map(node => node.dataset.committedName);
      const warning = names.length ? `Committed: ${names.join(', ')}. ` : '';
      if (!window.confirm(`${warning}Cancel Saturday? We will notify committed people, but management must directly contact anyone who may not tap the timeclock again.`)) return;
      return post('/cancel', { day });
    }
    if (button.dataset.commitmentCancel) {
      const reason = window.prompt(`Why is ${button.dataset.commitmentName}'s Saturday commitment being cancelled?`);
      if (reason === null) return;
      if (!reason.trim()) return showError('A cancellation reason is required.');
      return post(`/commitments/${button.dataset.commitmentCancel}/cancel`, { day, reason });
    }
    const action = button.dataset.openingAction;
    if (!action) return;
    const row = button.closest('[data-wc-id]');
    const requested = Number(row.dataset.requested);
    if (action === 'decrease' && requested <= 1) return;
    row.dataset.requested = String(action === 'increase' ? requested + 1 : requested - 1);
    post('/openings', { day, ...shiftValues(), requested_counts: requestedCounts() });
  });
})();
