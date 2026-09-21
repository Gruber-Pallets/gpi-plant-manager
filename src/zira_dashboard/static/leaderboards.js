function toggleAll(btn) {
  var card = btn.closest('.lb-section');
  if (!card) return;
  var hidden = card.querySelectorAll('.lb-row-hidden, .lb-row-revealed');
  var nowVisible = !card.classList.contains('lb-expanded');
  card.classList.toggle('lb-expanded', nowVisible);
  hidden.forEach(function (tr) {
    tr.classList.toggle('lb-row-hidden', !nowVisible);
    tr.classList.toggle('lb-row-revealed', nowVisible);
  });
  if (nowVisible) {
    btn.innerHTML = '&#9650; Hide';
  } else {
    var totalRows = card.querySelectorAll('tbody tr').length;
    btn.innerHTML = '&#9662; Show all (' + totalRows + ')';
  }
}

let lbPopupOpener = null;

async function openLbPopup(btn) {
  lbPopupOpener = btn;
  const name = btn.dataset.name;
  const wc = btn.dataset.wc || '';
  const group = btn.dataset.group || '';
  const start = btn.dataset.start;
  const end = btn.dataset.end;

  const scopeLabel = wc ? wc : (group + ' group');
  document.getElementById('lb-popup-title').textContent =
    `${name} — ${scopeLabel} · ${start} → ${end}`;

  const cardUrl = `/staffing/people/${encodeURIComponent(name)}?start=${start}&end=${end}`;
  document.getElementById('lb-popup-card-link').href = cardUrl;

  const bd = document.getElementById('lb-popup-bd');
  bd.classList.add('show');
  document.getElementById('lb-popup-card-link').focus();
  const tbody = document.querySelector('#lb-popup-table tbody');
  tbody.innerHTML = '<tr><td colspan="4" style="color:var(--muted)">Loading…</td></tr>';
  document.getElementById('lb-popup-empty').style.display = 'none';

  const params = new URLSearchParams({ name, start, end });
  if (wc) params.set('wc', wc); else params.set('group', group);

  try {
    const r = await fetch('/api/staffing/leaderboards/person-days?' + params);
    if (!r.ok) {
      tbody.innerHTML = '<tr><td colspan="4" style="color:var(--bad)">Failed to load (HTTP ' + r.status + ').</td></tr>';
      return;
    }
    const data = await r.json();
    renderLbPopupRows(data.rows || []);
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="4" style="color:var(--bad)">Failed to load: ' + (e.message || 'network error') + '</td></tr>';
  }
}

function renderLbPopupRows(rows) {
  const tbody = document.querySelector('#lb-popup-table tbody');
  const empty = document.getElementById('lb-popup-empty');
  if (!rows.length) {
    tbody.innerHTML = '';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td><a href="/recycling?start=${r.date}&end=${r.date}">${r.date}</a></td>
      <td>${(r.wcs || []).join(', ')}</td>
      <td class="num">${Math.round(r.units || 0).toLocaleString()}</td>
      <td class="num">${Math.round(r.downtime || 0).toLocaleString()}</td>
    </tr>
  `).join('');
}

function closeLbPopup() {
  const bd = document.getElementById('lb-popup-bd');
  if (!bd.classList.contains('show')) return;
  bd.classList.remove('show');
  if (lbPopupOpener && typeof lbPopupOpener.focus === 'function') {
    lbPopupOpener.focus();
  }
}

document.getElementById('lb-popup-bd').addEventListener('click', e => {
  if (e.target.id === 'lb-popup-bd') closeLbPopup();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('lb-popup-bd').classList.contains('show')) closeLbPopup();
});


(function initLeaderboards() {
  let dragged = null;

  function setVisibilityButtonBusy(btn, busy) {
    if (busy) {
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
    } else {
      btn.disabled = false;
      btn.setAttribute('aria-busy', 'false');
    }
  }

  function bindDrag(sec) {
    if (window.gpiAccess && !window.gpiAccess.operate) return;
    sec.addEventListener('dragstart', () => {
      dragged = sec;
      sec.classList.add('dragging');
    });
    sec.addEventListener('dragend', () => {
      if (dragged) dragged.classList.remove('dragging');
      dragged = null;
      saveOrder();
    });
    sec.addEventListener('dragover', (e) => {
      e.preventDefault();
      if (!dragged || dragged === sec) return;
      const rect = sec.getBoundingClientRect();
      const after = (e.clientY - rect.top) > rect.height / 2;
      sec.parentNode.insertBefore(dragged, after ? sec.nextSibling : sec);
    });
  }

  document.querySelectorAll('.lb-section').forEach(bindDrag);

  function saveOrder() {
    for (const kind of ['wc', 'group', 'wc-avg', 'group-avg']) {
      const order = [];
      document.querySelectorAll(`.lb-pane-active .lb-section[data-kind="${kind}"]`).forEach(s => order.push(s.dataset.wc));
      document.querySelectorAll(`.lb-inactive-content .lb-section[data-kind="${kind}"]`).forEach(s => order.push(s.dataset.wc));
      fetch(`/staffing/leaderboards/order?kind=${kind}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({order}),
      }).catch(() => {});
    }
  }

  document.querySelectorAll('.lb-hide-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (btn.disabled) return;
      const sec = btn.closest('.lb-section');
      const name = sec.dataset.wc;
      const kind = sec.dataset.kind || 'wc';
      setVisibilityButtonBusy(btn, true);
      try {
        const resp = await fetch(`/staffing/leaderboards/wc/${encodeURIComponent(name)}/inactive?kind=${kind}`,
                                 {method: 'POST'});
        if (resp.ok) window.location.reload();
        else setVisibilityButtonBusy(btn, false);
      } catch (e) {
        setVisibilityButtonBusy(btn, false);
      }
    });
  });

  document.querySelectorAll('.lb-show-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (btn.disabled) return;
      const sec = btn.closest('.lb-section');
      const name = sec.dataset.wc;
      const kind = sec.dataset.kind || 'wc';
      setVisibilityButtonBusy(btn, true);
      try {
        const resp = await fetch(`/staffing/leaderboards/wc/${encodeURIComponent(name)}/active?kind=${kind}`,
                                 {method: 'POST'});
        if (resp.ok) window.location.reload();
        else setVisibilityButtonBusy(btn, false);
      } catch (e) {
        setVisibilityButtonBusy(btn, false);
      }
    });
  });
})();
