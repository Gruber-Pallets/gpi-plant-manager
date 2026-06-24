(function () {
  if (!window.gpiFetch) {
    window.gpiFetch = function (url, opts) {
      opts = opts || {};
      if (typeof fetch === 'function') return fetch(url, opts);
      return new Promise(function (resolve, reject) {
        var xhr = new XMLHttpRequest();
        xhr.open(opts.method || 'GET', url, true);
        var headers = opts.headers || {};
        Object.keys(headers).forEach(function (name) {
          xhr.setRequestHeader(name, headers[name]);
        });
        xhr.onload = function () {
          var responseText = xhr.responseText || '';
          resolve({
            ok: xhr.status >= 200 && xhr.status < 300,
            status: xhr.status,
            json: function () {
              return Promise.resolve(responseText ? JSON.parse(responseText) : {});
            },
            text: function () { return Promise.resolve(responseText); },
          });
        };
        xhr.onerror = function () { reject(new Error('network error')); };
        xhr.send(opts.body || null);
      });
    };
  }
})();

(function () {
  var btn = null;
  var dot = null;
  var modal = null;
  var body = null;
  var panelLoaded = false;

  function getCutoff() {
    try { return localStorage.getItem('changelog_cutoff') || ''; } catch (e) { return ''; }
  }

  function setCutoff(value) {
    try { localStorage.setItem('changelog_cutoff', value || ''); } catch (e) {}
  }

  function getRead() {
    try { return new Set(JSON.parse(localStorage.getItem('changelog_read') || '[]')); }
    catch (e) { return new Set(); }
  }

  function setRead(readSet) {
    try { localStorage.setItem('changelog_read', JSON.stringify(Array.from(readSet))); } catch (e) {}
  }

  function whenOf(key) {
    return String(key || '').split('#')[0];
  }

  function isUnread(key) {
    return whenOf(key) > getCutoff() && !getRead().has(key);
  }

  function isTvMode() {
    return !!(document.documentElement.dataset && document.documentElement.dataset.tvTheme);
  }

  function injectButton() {
    if (isTvMode()) return;
    var header = document.querySelector('header');
    if (!header || header.querySelector('.whatsnew-btn')) return;
    btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'whatsnew-btn';
    btn.setAttribute('aria-label', "What's new");
    btn.setAttribute('aria-haspopup', 'dialog');
    btn.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" '
      + 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
      + 'stroke-linejoin="round"><path d="M12 3v3.5M12 17.5v3.5M3 12h3.5'
      + 'M17.5 12h3.5M15.5 8.5l2.1-2.1M8.5 8.5l-2.1-2.1M15.5 15.5l2.1 2.1'
      + 'M8.5 15.5l-2.1 2.1"></path></svg>'
      + '<span class="whatsnew-dot" hidden></span>';
    dot = btn.querySelector('.whatsnew-dot');
    var slot = document.createElement('div');
    slot.className = 'whatsnew-slot';
    slot.appendChild(btn);
    header.appendChild(slot);
    btn.addEventListener('click', openPanel);
  }

  function refreshDot() {
    if (!dot) return;
    window.gpiFetch('/changelog/latest')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var latest = data && data.latest_date;
        var hasCutoff = false;
        try { hasCutoff = localStorage.getItem('changelog_cutoff') != null; } catch (e) {}
        if (!hasCutoff) {
          var seen = '';
          try { seen = localStorage.getItem('changelog_seen') || ''; } catch (e2) {}
          setCutoff(seen || latest || '');
        }
        dot.hidden = !(latest && isUnread(latest));
      })
      .catch(function () {});
  }

  function ensureModal() {
    modal = document.getElementById('changelog-modal');
    body = document.getElementById('changelog-body');
    if (!modal) return false;
    if (modal.dataset.wired) return true;
    modal.dataset.wired = '1';
    var backdrop = document.getElementById('changelog-backdrop');
    var closeBtn = document.getElementById('changelog-close');
    var markAll = document.getElementById('changelog-markall');
    if (backdrop) backdrop.addEventListener('click', closePanel);
    if (closeBtn) closeBtn.addEventListener('click', closePanel);
    if (markAll) markAll.addEventListener('click', markAllRead);
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && modal && !modal.hidden) closePanel();
    });
    return true;
  }

  function openPanel(event) {
    if (event) event.preventDefault();
    if (!ensureModal()) return;
    modal.hidden = false;
    document.documentElement.style.overflow = 'hidden';
    if (!panelLoaded) {
      window.gpiFetch('/changelog')
        .then(function (r) { return r.text(); })
        .then(function (htmlText) {
          body.innerHTML = htmlText;
          panelLoaded = true;
          wireCards();
          applyReadState();
        })
        .catch(function () { body.innerHTML = '<p>Could not load changelog.</p>'; });
    } else {
      applyReadState();
    }
  }

  function closePanel() {
    if (modal) modal.hidden = true;
    document.documentElement.style.overflow = '';
  }

  function applyReadState() {
    if (!body) return;
    Array.prototype.forEach.call(body.querySelectorAll('.cl-entry'), function (card) {
      var key = card.getAttribute('data-key');
      var unread = isUnread(key);
      card.classList.toggle('cl-read', !unread);
      var markRead = card.querySelector('.cl-markread');
      if (markRead) markRead.hidden = !unread;
    });
    refreshDotFromCards();
  }

  function refreshDotFromCards() {
    if (!dot || !body) return;
    var anyUnread = Array.prototype.some.call(
      body.querySelectorAll('.cl-entry'),
      function (card) { return isUnread(card.getAttribute('data-key')); }
    );
    dot.hidden = !anyUnread;
  }

  function wireCards() {
    Array.prototype.forEach.call(body.querySelectorAll('.cl-markread'), function (markRead) {
      markRead.addEventListener('click', function () {
        var readSet = getRead();
        readSet.add(markRead.getAttribute('data-key'));
        setRead(readSet);
        applyReadState();
      });
    });
  }

  function markAllRead() {
    if (!body) return;
    var newest = getCutoff();
    Array.prototype.forEach.call(body.querySelectorAll('.cl-entry'), function (card) {
      var when = whenOf(card.getAttribute('data-key'));
      if (when > newest) newest = when;
    });
    setCutoff(newest);
    setRead(new Set());
    applyReadState();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      injectButton();
      refreshDot();
    });
  } else {
    injectButton();
    refreshDot();
  }
})();

// Global nav badges + modals — present on every page. Four instances share
// the makeBadgeModal factory below: "Assignments to Do", "Late/Absence
// Report", "Missing Work Center", and "Missed Punch Out". Element IDs /
// classes / endpoints are unchanged from the original per-feature IIFEs
// (footer.css and the inline dashboard scripts depend on them).
(function () {
  function settingsLink() {
    return document.querySelector('header nav a[href="/settings"]')
        || document.querySelector('header.app nav a[href="/settings"]');
  }

  function ensureInboxLink() {
    var anchor = settingsLink();
    if (!anchor || !anchor.parentNode) return;
    var existing = anchor.parentNode.querySelector('a[href="/exceptions"]');
    if (existing) {
      existing.classList.add('inbox-nav-link');
      return existing;
    }
    var link = document.createElement('a');
    link.href = '/exceptions';
    link.textContent = 'Inbox';
    link.className = 'inbox-nav-link';
    if (window.location && window.location.pathname === '/exceptions') {
      link.classList.add('active');
    }
    anchor.parentNode.insertBefore(link, anchor);
    return link;
  }

  function ensureInboxLabel(link) {
    if (!link) return null;
    var label = link.querySelector('.inbox-nav-label');
    if (!label) {
      link.textContent = '';
      label = document.createElement('span');
      label.className = 'inbox-nav-label';
      label.textContent = 'Inbox';
      link.appendChild(label);
    }
    return label;
  }

  function ensureInboxCount(link) {
    ensureInboxLabel(link);
    var count = link.querySelector('.inbox-nav-count');
    if (!count) {
      count = document.createElement('span');
      count.className = 'inbox-nav-count';
      count.hidden = true;
      link.appendChild(count);
    }
    return count;
  }

  function updateInboxSummaryLink(link, data) {
    if (!link || !data) return;
    var total = parseInt(data.total || 0, 10) || 0;
    var urgent = parseInt(data.urgent_total || 0, 10) || 0;
    var degraded = !!(data.source_errors && data.source_errors.length);
    var count = ensureInboxCount(link);
    link.classList.toggle('has-open', total > 0);
    link.classList.toggle('has-urgent', urgent > 0);
    link.classList.toggle('is-degraded', degraded);
    count.hidden = total <= 0 && !degraded;
    count.textContent = degraded && total <= 0 ? '!' : total > 99 ? '99+' : String(total);
    var title = total > 0
      ? 'Exception Inbox: ' + total + ' open' + (urgent > 0 ? ', ' + urgent + ' urgent' : '')
      : 'Exception Inbox: all clear';
    if (degraded) title += ' (some checks could not load)';
    link.title = title;
  }

  function refreshInboxSummary(link) {
    if (!link) return;
    window.gpiFetch('/api/exceptions/summary').then(function (r) { return r.json(); }).then(function (d) {
      updateInboxSummaryLink(link, d);
    }).catch(function () {});
  }

  function readInboxSummaryBootstrap() {
    var el = document.getElementById('gpi-inbox-summary-bootstrap');
    if (!el) return null;
    try {
      return JSON.parse(el.textContent || '{}');
    } catch (e) {
      return null;
    }
  }

  function startInboxSummary(link) {
    if (!link) return;
    window.gpiRefreshInboxSummary = function () { refreshInboxSummary(link); };
    var initial = readInboxSummaryBootstrap();
    if (initial) {
      updateInboxSummaryLink(link, initial);
    } else {
      setTimeout(function () { refreshInboxSummary(link); }, 650);
    }
    setInterval(function () {
      if (!document.hidden) refreshInboxSummary(link);
    }, 60000);
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) refreshInboxSummary(link);
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }

  function postJson(url, payload) {
    return window.gpiFetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    }).then(function (r) { return r.json(); });
  }

  // Shared scaffold for a nav badge + modal pair: fetches cfg.endpoint
  // (initialDelay ms after load, or at DOMContentLoaded when unset; re-polled
  // every cfg.pollMs while the tab is visible), shows the badge per
  // cfg.badgeVisible/updateBadge/insertionPoint, and on click opens a modal
  // built from cfg.prefix ('atd'/'late' → -backdrop/-card/-head/-body/-close
  // classes) + modalClass/ariaLabel/heading. cfg.render(body, data, api)
  // fills the modal body and wires its handlers.
  function makeBadgeModal(cfg) {
    var navBadge = null;
    var modal = null;
    var data = null;

    function refreshCount() {
      window.gpiFetch(cfg.endpoint).then(function (r) { return r.json(); }).then(function (d) {
        data = d;
        injectOrUpdateBadge();
      }).catch(function () {});
    }

    function injectOrUpdateBadge() {
      if (!data || !cfg.badgeVisible(data)) {
        if (navBadge) { navBadge.remove(); navBadge = null; }
        return;
      }
      var anchor = settingsLink();
      if (!anchor) return;
      if (!navBadge) {
        navBadge = document.createElement('a');
        navBadge.href = '#';
        navBadge.className = cfg.badgeClass;
        navBadge.title = cfg.badgeTitle;
        navBadge.addEventListener('click', function (e) { e.preventDefault(); openModal(); });
        var ref = cfg.insertionPoint ? cfg.insertionPoint(anchor) : anchor.nextSibling;
        anchor.parentNode.insertBefore(navBadge, ref);
      }
      cfg.updateBadge(navBadge, data);
    }

    function openModal() {
      closeModal();
      modal = document.createElement('div');
      modal.className = cfg.modalClass;
      modal.innerHTML = ''
        + '<div class="' + cfg.prefix + '-backdrop"></div>'
        + '<div class="' + cfg.prefix + '-card" role="dialog" aria-modal="true" aria-label="' + cfg.ariaLabel + '">'
        + '  <div class="' + cfg.prefix + '-head"><h3>' + cfg.heading + '</h3>'
        + '    <button type="button" class="' + cfg.prefix + '-close" aria-label="Close">×</button></div>'
        + '  <div class="' + cfg.prefix + '-body">Loading…</div>'
        + '</div>';
      document.body.appendChild(modal);
      document.documentElement.style.overflow = 'hidden';
      modal.querySelector('.' + cfg.prefix + '-backdrop').addEventListener('click', closeModal);
      modal.querySelector('.' + cfg.prefix + '-close').addEventListener('click', closeModal);
      document.addEventListener('keydown', escClose);
      window.gpiFetch(cfg.endpoint).then(function (r) { return r.json(); }).then(renderModal);
    }

    function closeModal() {
      if (modal) { modal.remove(); modal = null; }
      document.documentElement.style.overflow = '';
      document.removeEventListener('keydown', escClose);
    }

    function escClose(e) { if (e.key === 'Escape') closeModal(); }

    function renderModal(d) {
      data = d;
      if (!modal) return;
      cfg.render(modal.querySelector('.' + cfg.prefix + '-body'), d, api);
    }

    var api = {
      refreshCount: refreshCount,
      closeModal: closeModal,
      openModal: openModal,
      renderModal: renderModal,
      injectOrUpdateBadge: injectOrUpdateBadge,
      isModalOpen: function () { return !!modal; },
      setData: function (d) { data = d; },
    };

    if (cfg.initialDelay != null) {
      setTimeout(refreshCount, cfg.initialDelay);
    } else if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', refreshCount);
    } else {
      refreshCount();
    }
    if (cfg.pollMs) {
      setInterval(function () {
        if (document.hidden) return;  // don't poll background tabs
        refreshCount();
      }, cfg.pollMs);
      document.addEventListener('visibilitychange', function () {
        if (!document.hidden) refreshCount();
      });
    }

    return api;
  }

  startInboxSummary(ensureInboxLink());
  window.gpiAlertBadges = window.gpiAlertBadges || {};

  // ---------- "Assignments to Do" ----------

  function to24h(label) {
    // label like "1:05 PM" / "8:50 AM" -> "13:05" / "08:50"
    var m = /^(\d{1,2}):(\d{2})\s*(AM|PM)$/i.exec(String(label || '').trim());
    if (!m) return '';
    var h = parseInt(m[1], 10) % 12;
    if (/pm/i.test(m[3])) h += 12;
    return (h < 10 ? '0' : '') + h + ':' + m[2];
  }

  function localTimeToIso(dayIso, hhmm) {
    // dayIso "2026-06-02", hhmm "13:05" -> UTC ISO using the browser's local tz
    // (plant kiosks/managers run in plant-local time).
    var p = hhmm.split(':');
    var dp = dayIso.split('-');
    var dt = new Date(parseInt(dp[0], 10), parseInt(dp[1], 10) - 1,
                      parseInt(dp[2], 10), parseInt(p[0], 10), parseInt(p[1], 10), 0, 0);
    return dt.toISOString();
  }

  // t is resp.transfer: {transfer: 'moved'|'opened', person, to_dept, closed_id?, new_id}
  // Returns true if a toast was shown (and this function now owns the page reload),
  // false otherwise (caller should do its own reload).
  function maybeTransferToast(t) {
    if (!t || (t.transfer !== 'moved' && t.transfer !== 'opened')) return false;
    var toast = document.createElement('div');
    toast.className = 'atd-transfer-toast';
    var verb = t.transfer === 'opened' ? 'Clocked in' : 'Transferred';
    toast.innerHTML = escapeHtml(verb + ' ' + (t.person || '') + ' → ' + (t.to_dept || ''))
      + ' <button type="button" class="atd-transfer-undo">Undo</button>';
    document.body.appendChild(toast);
    // Auto-dismiss after 8s, then refresh the dashboard so the new assignment shows.
    var dismiss = setTimeout(function () {
      if (toast.parentNode) toast.remove();
      location.reload();
    }, 8000);
    toast.querySelector('.atd-transfer-undo').addEventListener('click', function () {
      this.disabled = true;
      clearTimeout(dismiss);
      postJson('/api/staffing/transfer/undo', {closed_id: t.closed_id || null, new_id: t.new_id})
        .then(function () {
          toast.textContent = 'Transfer undone.';
          setTimeout(function () { toast.remove(); location.reload(); }, 1200);
        }).catch(function () {
          toast.textContent = 'Undo failed — network error.';
          setTimeout(function () { toast.remove(); location.reload(); }, 2000);
        });
    });
    return true;
  }

  // Expose so inline-assign popovers (recycling.html / new_dept.html) can reuse it.
  window.gpiTransferToast = maybeTransferToast;

  function renderAtdBody(body, d) {
    var html = '';
    var personOpts = '';
    (d.people || []).forEach(function (n) {
      personOpts += '<option value="' + escapeHtml(n) + '">' + escapeHtml(n) + '</option>';
    });
    if (d.items && d.items.length) {
      html += '<p class="atd-help">These work centers produced units today but had no one scheduled. Pick the person who actually worked there. Any active employee can be picked, even if they\'re scheduled elsewhere.</p>';
      html += '<ul class="atd-list">';
      d.items.forEach(function (item) {
        html += '<li class="atd-item" data-wc="' + escapeHtml(item.wc_name) + '" data-day="' + escapeHtml(d.today) + '" data-start="' + escapeHtml(item.first_iso) + '" data-end="' + escapeHtml(item.last_iso) + '">';
        html += '<div class="atd-item-head"><strong>' + escapeHtml(item.wc_name) + '</strong>';
        html += ' <span class="atd-meta">' + item.units + ' pallets · ' + escapeHtml(item.first_label) + '–' + escapeHtml(item.last_label) + '</span></div>';
        html += '<div class="atd-pick"><select class="atd-person"><option value="">— pick person —</option>' + personOpts;
        html += '</select> <button type="button" class="atd-save">Save</button>';
        html += ' <button type="button" class="atd-testing-btn">Testing</button>';
        html += '<span class="atd-status" hidden></span></div>';
        // Hidden testing panel — start/end prefilled with the sensed window.
        html += '<div class="atd-testing-panel" hidden>';
        html += '<label>Testing from <input type="time" class="atd-test-start" value="' + to24h(item.first_label) + '"></label>';
        html += '<label>to <input type="time" class="atd-test-end" value="' + to24h(item.last_label) + '"></label>';
        html += '<div class="atd-test-remainder"><label>Who worked after testing? '
              + '<select class="atd-test-person"><option value="">— no one (all testing) —</option>' + personOpts;
        html += '</select></label></div>';
        html += '<button type="button" class="atd-test-confirm">Confirm testing</button>';
        html += '<span class="atd-test-status" hidden></span></div>';
        html += '</li>';
      });
      html += '</ul>';
    } else {
      html += '<p class="atd-help">Nothing to attribute right now — every metered WC with production today has someone assigned.</p>';
    }
    if (d.saved && d.saved.length) {
      html += '<h4 class="atd-section-title">Saved today</h4>';
      html += '<ul class="atd-list">';
      d.saved.forEach(function (r) {
        html += '<li class="atd-saved-item" data-attribution-id="' + r.id + '">';
        html += '<span class="atd-saved-text"><strong>' + escapeHtml(r.wc_name) + '</strong> — ' + escapeHtml(r.person_name);
        html += ' <span class="atd-saved-meta">' + escapeHtml(r.first_label) + '–' + escapeHtml(r.last_label) + '</span></span>';
        html += '<button type="button" class="atd-delete" title="Remove this attribution" aria-label="Remove">×</button></li>';
      });
      html += '</ul>';
    }
    body.innerHTML = html;
    wireAtdHandlers(body);
  }

  function wireAtdHandlers(body) {
    body.querySelectorAll('.atd-save').forEach(function (b) {
      b.addEventListener('click', function () {
        var li = b.closest('.atd-item');
        var sel = li.querySelector('.atd-person');
        var status = li.querySelector('.atd-status');
        var person = sel.value;
        if (!person) { status.hidden = false; status.textContent = 'Pick a person.'; return; }
        b.disabled = true; sel.disabled = true; status.hidden = true;
        postJson('/api/staffing/attribute', {
          day: li.dataset.day, wc_name: li.dataset.wc, person_name: person,
          start_utc: li.dataset.start,
        }).then(function (resp) {
          if (resp.ok) {
            status.hidden = false; status.textContent = 'Saved ✓ ' + person;
            li.classList.add('atd-saved');
            // Hard reload so the dashboards' bars/downtime widgets show the
            // new name immediately in the (no assignment) slot.
            if (!maybeTransferToast(resp.transfer)) {
              setTimeout(function () { location.reload(); }, 500);
            }
          } else {
            b.disabled = false; sel.disabled = false;
            status.hidden = false; status.textContent = 'Failed: ' + (resp.error || 'unknown');
          }
        }).catch(function () {
          b.disabled = false; sel.disabled = false;
          status.hidden = false; status.textContent = 'Network error.';
        });
      });
    });
    body.querySelectorAll('.atd-delete').forEach(function (b) {
      b.addEventListener('click', function () {
        var li = b.closest('.atd-saved-item');
        if (!li || !confirm('Remove this attribution?')) return;
        b.disabled = true;
        window.gpiFetch('/api/staffing/attribute/' + encodeURIComponent(li.dataset.attributionId), {method: 'DELETE'})
          .then(function (r) { return r.json(); })
          .then(function (resp) {
            if (resp.ok) {
              li.style.opacity = '0.4'; li.style.textDecoration = 'line-through';
              setTimeout(function () { location.reload(); }, 500);
            } else {
              b.disabled = false; alert('Delete failed: ' + (resp.error || 'unknown'));
            }
          }).catch(function () { b.disabled = false; alert('Network error.'); });
      });
    });
    body.querySelectorAll('.atd-testing-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        var li = b.closest('.atd-item');
        var panel = li.querySelector('.atd-testing-panel');
        panel.hidden = !panel.hidden;
      });
    });
    body.querySelectorAll('.atd-test-confirm').forEach(function (b) {
      b.addEventListener('click', function () {
        var li = b.closest('.atd-item');
        var startV = li.querySelector('.atd-test-start').value;
        var endV = li.querySelector('.atd-test-end').value;
        var person = li.querySelector('.atd-test-person').value;
        var status = li.querySelector('.atd-test-status');
        if (!startV || !endV || endV <= startV) {
          status.hidden = false; status.textContent = 'Testing end must be after start.'; return;
        }
        b.disabled = true; status.hidden = true;
        postJson('/api/staffing/attribute-with-testing', {
          day: li.dataset.day, wc_name: li.dataset.wc,
          testing_start_utc: localTimeToIso(li.dataset.day, startV),
          testing_end_utc: localTimeToIso(li.dataset.day, endV),
          sensed_end_utc: li.dataset.end,
          remainder_person: person || null,
        }).then(function (resp) {
          if (resp.ok) {
            status.hidden = false;
            var creditedPerson = person && resp.ids && resp.ids.length > 1;
            status.textContent = creditedPerson ? ('Saved ✓ testing + ' + person) : 'Saved ✓ testing';
            if (!maybeTransferToast(resp.transfer)) {
              setTimeout(function () { location.reload(); }, 800);
            }
          } else {
            b.disabled = false; status.hidden = false;
            status.textContent = 'Failed: ' + (resp.error || 'unknown');
          }
        }).catch(function () {
          b.disabled = false; status.hidden = false; status.textContent = 'Network error.';
        });
      });
    });
  }

  window.gpiAlertBadges.assignments = makeBadgeModal({
    endpoint: '/api/assignments-todo',
    badgeClass: 'assign-todo-nav-badge',
    badgeTitle: 'Production happened at unscheduled work centers — click to assign',
    badgeVisible: function (d) { return !!d.count; },
    updateBadge: function (el, d) {
      el.innerHTML = '⚠ <span class="cnt">' + d.count + '</span> to Assign';
    },
    prefix: 'atd',
    modalClass: 'atd-modal',
    ariaLabel: 'Assignments to do',
    heading: 'Assignments to Do',
    render: renderAtdBody,
    // Kick it off after a short delay so the rest of the page renders first.
    initialDelay: 300,
  });

  // ---------- "Late/Absence Report" ----------

  var LATE_ENDPOINT = '/api/late-report';

  function renderLateBody(body, d, api) {
    var html = '';

    // Quick-pick reason editor (Sick / Car issues / Overslept / Other +
    // free-text input + gated Save). Shared by the needs_reason rows
    // (always visible, saves a late-arrival reason) and the Declare Absent
    // editor (hidden until toggled, saves an absence).
    function reasonRow(extraClass, hidden, saveClass) {
      return ''
        + '<div class="late-reason-row' + (extraClass ? ' ' + extraClass : '') + '"' + (hidden ? ' hidden' : '') + '>'
        + '  <button type="button" class="late-quickpick" data-pick="Sick">Sick</button>'
        + '  <button type="button" class="late-quickpick" data-pick="Car issues">Car issues</button>'
        + '  <button type="button" class="late-quickpick" data-pick="Overslept">Overslept</button>'
        + '  <button type="button" class="late-quickpick" data-pick="">Other</button>'
        + '  <input type="text" class="late-reason-input" placeholder="Reason required">'
        + '  <button type="button" class="' + saveClass + '" disabled>Save</button>'
        + '</div>';
    }

    function renderActionableRow(item, sectionKind) {
      // sectionKind: 'scheduled' | 'unscheduled' | 'needs_reason'
      var rowClass = 'late-item late-item-' + sectionKind;
      var minsHtml = '';
      if (sectionKind === 'scheduled') {
        minsHtml = '<span class="late-item-mins">' + item.minutes_late + ' min late</span>';
      } else if (sectionKind === 'needs_reason') {
        minsHtml = '<span class="late-item-mins">clocked in ' + item.minutes_late + ' min late</span>';
      }
      var actionsHtml;
      if (sectionKind === 'needs_reason') {
        actionsHtml = reasonRow('', false, 'late-save-late');
      } else {
        actionsHtml = ''
          + '<span class="late-item-actions">'
          + '  <button type="button" class="late-snooze">Snooze 30 min</button>'
          + '  <button type="button" class="late-declare">Declare Absent</button>'
          + '</span>'
          + reasonRow('late-declare-reason', true, 'late-save-absent');
      }
      return ''
        + '<li class="' + rowClass + '" data-emp-id="' + escapeHtml(item.emp_id)
        +    '" data-name="' + escapeHtml(item.name) + '">'
        + '<span class="late-item-name">' + escapeHtml(item.name) + '</span>'
        + minsHtml
        + actionsHtml
        + '<span class="late-status" hidden></span>'
        + '</li>';
    }

    var anyActionable = false;

    if (d.scheduled_late && d.scheduled_late.length) {
      anyActionable = true;
      html += '<h4 class="late-section-title">Scheduled — haven\'t clocked in</h4>';
      html += '<ul class="late-list">';
      d.scheduled_late.forEach(function (item) {
        html += renderActionableRow(item, 'scheduled');
      });
      html += '</ul>';
    }

    if (d.unscheduled_late && d.unscheduled_late.length) {
      anyActionable = true;
      html += '<h4 class="late-section-title">Unscheduled — also haven\'t clocked in</h4>';
      html += '<ul class="late-list">';
      d.unscheduled_late.forEach(function (item) {
        html += renderActionableRow(item, 'unscheduled');
      });
      html += '</ul>';
    }

    if (d.needs_reason && d.needs_reason.length) {
      anyActionable = true;
      html += '<h4 class="late-section-title">Late arrivals — reason needed</h4>';
      html += '<ul class="late-list">';
      d.needs_reason.forEach(function (item) {
        html += renderActionableRow(item, 'needs_reason');
      });
      html += '</ul>';
    }

    if (!anyActionable) {
      html += '<p class="late-help">No one is currently flagged. Anyone scheduled today who hasn\'t clocked in by 15 min past shift-start, anyone unscheduled in the same situation, or anyone who clocked in late without a recorded reason, will appear here.</p>';
    }

    if (d.snoozed && d.snoozed.length) {
      html += '<h4 class="late-section-title">Snoozed</h4>';
      html += '<ul class="late-list">';
      d.snoozed.forEach(function (s) {
        html += '<li class="late-snoozed-item">';
        html += '<span class="late-snoozed-name">' + escapeHtml(s.name) + '</span>';
        html += '<span>re-checks in ' + s.mins_remaining + ' min</span>';
        html += '</li>';
      });
      html += '</ul>';
    }

    body.innerHTML = html;
    wireLateHandlers(body, api);
  }

  function wireLateHandlers(body, api) {
    // Save button is gated on the reason input having content. Empty
    // input → button disabled. Each row gets its own listener.
    function refreshSaveDisabled(input) {
      var row = input.closest('.late-reason-row');
      if (!row) return;
      var save = row.querySelector('.late-save-late, .late-save-absent');
      if (!save) return;
      save.disabled = (input.value || '').trim().length === 0;
    }

    body.querySelectorAll('.late-reason-input').forEach(function (input) {
      refreshSaveDisabled(input);
      input.addEventListener('input', function () { refreshSaveDisabled(input); });
    });

    // Quick-pick buttons populate the adjacent text input. Sick / Car
    // issues / Overslept have a non-empty data-pick — those auto-save
    // (one click, done). "Other" has an empty data-pick — that clears
    // the input and waits for the user to type, then Save fires the
    // record manually.
    body.querySelectorAll('.late-quickpick').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var input = btn.parentElement.querySelector('.late-reason-input');
        if (!input) return;
        var pick = btn.dataset.pick || '';
        input.value = pick;
        refreshSaveDisabled(input);
        if (pick) {
          var save = btn.parentElement.querySelector('.late-save-late, .late-save-absent');
          if (save) save.click();
        } else {
          input.focus();
        }
      });
    });

    // Snooze. Always closes the modal immediately on success — Snooze
    // means "out of my face for 30 min", and forcing the user to also
    // dismiss the modal defeats the purpose. Other actions (Save / Declare
    // Absent) keep the modal open so the user can deal with remaining
    // people in one sitting.
    body.querySelectorAll('.late-snooze').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var li = btn.closest('.late-item');
        doLateAction(li, '/api/late-report/snooze', {
          emp_id: li.dataset.empId,
          name: li.dataset.name,
          minutes: 30,
        }, { alwaysClose: true }, api);
      });
    });

    // Declare Absent — toggles the inline reason editor.
    body.querySelectorAll('.late-declare').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var li = btn.closest('.late-item');
        var editor = li.querySelector('.late-declare-reason');
        if (editor) {
          editor.hidden = false;
          var input = editor.querySelector('.late-reason-input');
          if (input) input.focus();
        }
      });
    });

    // Save (Declare Absent).
    body.querySelectorAll('.late-save-absent').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var li = btn.closest('.late-item');
        var input = li.querySelector('.late-reason-input');
        doLateAction(li, '/api/late-report/declare-absent', {
          emp_id: li.dataset.empId,
          name: li.dataset.name,
          reason: input ? input.value : '',
        }, null, api);
      });
    });

    // Save (Late Arrival reason).
    body.querySelectorAll('.late-save-late').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var li = btn.closest('.late-item');
        var input = li.querySelector('.late-reason-input');
        doLateAction(li, '/api/late-report/save-late-arrival', {
          emp_id: li.dataset.empId,
          name: li.dataset.name,
          reason: input ? input.value : '',
        }, null, api);
      });
    });
  }

  function doLateAction(li, url, payload, opts, api) {
    opts = opts || {};
    var status = li.querySelector('.late-status');
    if (status) { status.hidden = false; status.textContent = 'Saving…'; }
    postJson(url, payload).then(function (resp) {
      if (resp && resp.ok) {
        // Re-pull the report so the saved row drops out. If nothing's
        // actionable left OR the caller set `alwaysClose: true` (Snooze),
        // close the modal — otherwise re-render so the user can keep
        // working without "Saving…" lingering on the saved row.
        window.gpiFetch(LATE_ENDPOINT).then(function (r) { return r.json(); }).then(function (d) {
          api.setData(d);
          api.injectOrUpdateBadge();
          var anyActionable = (d.scheduled_late && d.scheduled_late.length)
              || (d.unscheduled_late && d.unscheduled_late.length)
              || (d.needs_reason && d.needs_reason.length);
          if (opts.alwaysClose || !anyActionable) {
            api.closeModal();
          } else if (api.isModalOpen()) {
            api.renderModal(d);
          }
        });
      } else {
        if (status) { status.textContent = 'Failed: ' + ((resp && resp.error) || 'unknown'); }
      }
    }).catch(function () {
      if (status) { status.textContent = 'Network error.'; }
    });
  }

  window.gpiAlertBadges.late = makeBadgeModal({
    endpoint: LATE_ENDPOINT,
    badgeClass: 'late-nav-badge',
    badgeTitle: 'Scheduled people who haven\'t clocked in yet — click to manage',
    badgeVisible: function (d) {
      return !!(d.count || (d.snoozed && d.snoozed.length));
    },
    // Sit to the right of any existing assignments-todo badge, or directly
    // after Settings if that badge isn't present.
    insertionPoint: function (anchor) {
      var prev = anchor.nextSibling;
      while (prev && prev.classList && prev.classList.contains('assign-todo-nav-badge')) {
        prev = prev.nextSibling;
      }
      return prev;
    },
    updateBadge: function (el, d) {
      if (d.count) {
        el.innerHTML = '🚨 <span class="cnt">' + d.count + '</span> Late/Absence';
        el.style.display = '';
      } else {
        // Only snoozed people remain — render in a lower-key style but still visible.
        el.innerHTML = '⏱ <span class="cnt">' + d.snoozed.length + '</span> Snoozed';
        el.style.animation = 'none';
      }
    },
    prefix: 'late',
    modalClass: 'late-modal',
    ariaLabel: 'Late and absence report',
    heading: 'Late / Absence Report',
    render: renderLateBody,
    initialDelay: 400,
    // Re-poll every 60 seconds — keeps the badge fresh as people arrive late
    // or as snoozes expire, without a full page reload.
    pollMs: 60000,
  });

  // ---------- "Missing Work Center" ----------
  // Mirrors the Late/Absence badge/modal and reuses its .late-* styling.

  function wcOptions(wcs) {
    var opts = '<option value="">Pick work center…</option>';
    (wcs || []).forEach(function (w) {
      opts += '<option value="' + escapeHtml(w) + '">' + escapeHtml(w) + '</option>';
    });
    return opts;
  }

  function renderMwcBody(body, d, api) {
    var rows = (d && d.rows) || [];
    if (!rows.length) {
      body.innerHTML = '<p class="late-help">No attendance records are missing a work center. '
        + 'Any hourly employee with an attendance record in the last 14 days that has no '
        + 'work center will appear here.</p>';
      return;
    }
    var html = '<ul class="late-list">';
    rows.forEach(function (item) {
      html += '<li class="late-item" data-att="' + item.attendance_id + '">'
        + '<span class="late-item-name">' + escapeHtml(item.name) + '</span>'
        + '<span class="late-item-mins">clocked in ' + escapeHtml(item.check_in_label) + '</span>'
        + '<span class="late-item-actions">'
        + '  <button type="button" class="mwc-assign-btn">Assign</button>'
        + '  <button type="button" class="mwc-dismiss-btn">Dismiss</button>'
        + '</span>'
        + '<div class="late-reason-row mwc-assign-row" hidden>'
        + '  <select class="mwc-wc-select">' + wcOptions(d.work_centers) + '</select>'
        + '  <button type="button" class="mwc-save-btn" disabled>Save</button>'
        + '</div>'
        + '<span class="late-status" hidden></span>'
        + '</li>';
    });
    html += '</ul>';
    body.innerHTML = html;
    wireMwcActions(body, api);
  }

  function wireMwcActions(body, api) {
    function finishRow(li, label, ok) {
      var status = li.querySelector('.late-status');
      status.textContent = label;
      status.hidden = false;
      if (ok) {
        li.querySelectorAll('button, select').forEach(function (el) { el.disabled = true; });
        li.style.opacity = '0.6';
        api.refreshCount();
      }
    }

    body.querySelectorAll('.mwc-assign-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        btn.closest('.late-item').querySelector('.mwc-assign-row').hidden = false;
      });
    });
    body.querySelectorAll('.mwc-wc-select').forEach(function (sel) {
      sel.addEventListener('change', function () {
        sel.parentElement.querySelector('.mwc-save-btn').disabled = !sel.value;
      });
    });
    body.querySelectorAll('.mwc-save-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var li = btn.closest('.late-item');
        var sel = li.querySelector('.mwc-wc-select');
        if (!sel.value) return;
        btn.disabled = true;
        postJson('/missing-wc/assign', {
          attendance_id: parseInt(li.getAttribute('data-att'), 10),
          wc_name: sel.value,
          name: li.querySelector('.late-item-name').textContent,
        }).then(function (res) {
          finishRow(li, res && res.ok ? 'Assigned ✓' : 'Error', !!(res && res.ok));
        }).catch(function () { finishRow(li, 'Error', false); btn.disabled = false; });
      });
    });
    body.querySelectorAll('.mwc-dismiss-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var li = btn.closest('.late-item');
        btn.disabled = true;
        postJson('/missing-wc/dismiss', {
          attendance_id: parseInt(li.getAttribute('data-att'), 10),
          name: li.querySelector('.late-item-name').textContent,
        }).then(function (res) {
          finishRow(li, res && res.ok ? 'Dismissed' : 'Error', !!(res && res.ok));
        }).catch(function () { finishRow(li, 'Error', false); btn.disabled = false; });
      });
    });
  }

  window.gpiAlertBadges.missing_wc = makeBadgeModal({
    endpoint: '/api/missing-wc',
    badgeClass: 'late-nav-badge mwc-nav-badge',
    badgeTitle: 'Attendance records with no work center — click to assign',
    badgeVisible: function (d) { return !!d.count; },
    updateBadge: function (el, d) {
      el.innerHTML = '📍 <span class="cnt">' + d.count + '</span> No Work Center';
      el.style.display = '';
    },
    prefix: 'late',
    modalClass: 'late-modal mwc-modal',
    ariaLabel: 'Missing work center',
    heading: 'Missing Work Center',
    render: renderMwcBody,
    pollMs: 60000,
  });

  // ---------- "Missed Punch Out" ----------
  // Each row takes a time the manager enters; saving rewrites that attendance's
  // check_out (from midnight to the entered time) and clears the row.

  function finishMpoRow(li, label, ok, api) {
    var status = li.querySelector('.late-status');
    status.textContent = label;
    status.hidden = false;
    if (ok) {
      li.querySelectorAll('button, input').forEach(function (el) { el.disabled = true; });
      li.style.opacity = '0.6';
      api.refreshCount();
    }
  }

  function wireMpoActions(body, api) {
    body.querySelectorAll('.mpo-save-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var li = btn.closest('.late-item');
        var input = li.querySelector('.mpo-time-input');
        if (!input.value) { input.focus(); return; }
        btn.disabled = true;
        postJson('/missed-punch-out/correct', {
          attendance_id: parseInt(li.getAttribute('data-att'), 10),
          time: input.value,
        }).then(function (res) {
          if (res && res.ok) {
            finishMpoRow(li, 'Corrected ✓', true, api);
          } else {
            finishMpoRow(li, (res && res.error) || 'Error', false, api);
            btn.disabled = false;
          }
        }).catch(function () { finishMpoRow(li, 'Error', false, api); btn.disabled = false; });
      });
    });
  }

  function renderMpoBody(body, d, api) {
    var rows = (d && d.rows) || [];
    if (!rows.length) {
      body.innerHTML = '<p class="late-help">No missed punch-outs. Anyone left clocked in '
        + 'overnight is auto-clocked-out at midnight and appears here so you can set the '
        + 'time they actually left.</p>';
      return;
    }
    var html = '<ul class="late-list">';
    rows.forEach(function (item) {
      html += '<li class="late-item" data-att="' + item.attendance_id + '">'
        + '<span class="late-item-name">' + escapeHtml(item.name) + '</span>'
        + '<span class="late-item-mins">clocked in ' + escapeHtml(item.check_in_label)
        + ' · auto-closed at midnight</span>'
        + '<div class="late-reason-row">'
        + '  <label>Actually left at '
        + '    <input type="time" class="mpo-time-input" />'
        + '  </label>'
        + '  <button type="button" class="mpo-save-btn">Save</button>'
        + '</div>'
        + '<span class="late-status" hidden></span>'
        + '</li>';
    });
    html += '</ul>';
    body.innerHTML = html;
    wireMpoActions(body, api);
  }

  window.gpiAlertBadges.missed_punch_out = makeBadgeModal({
    endpoint: '/api/missed-punch-out',
    badgeClass: 'late-nav-badge mpo-nav-badge',
    badgeTitle: 'Employees auto-clocked-out at midnight — click to set the real time',
    badgeVisible: function (d) { return !!d.count; },
    updateBadge: function (el, d) {
      el.innerHTML = '⏰ <span class="cnt">' + d.count + '</span> Missed Punch Out';
      el.style.display = '';
    },
    prefix: 'late',
    modalClass: 'late-modal mpo-modal',
    ariaLabel: 'Missed punch out',
    heading: 'Missed Punch Out',
    render: renderMpoBody,
    pollMs: 60000,
  });
})();

// ---------- Feedback modal (Send) + View Feedback list ----------
(function () {
  var PLACEHOLDERS = {
    bug: 'What broke, and what did you expect?',
    feature: 'What would you like to see, and why?',
  };
  var attachments = [];   // {file, name, url}
  var currentType = 'bug';

  function $(id) { return document.getElementById(id); }

  function openModal(el) {
    if (!el) return;
    el.hidden = false;
    document.documentElement.style.overflow = 'hidden';
  }
  function closeModal(el) {
    if (!el) return;
    el.hidden = true;
    document.documentElement.style.overflow = '';
  }

  function resetSendForm() {
    attachments = [];
    currentType = 'bug';
    var desc = $('fb-desc');
    if (desc) { desc.value = ''; desc.placeholder = PLACEHOLDERS.bug; }
    setType('bug');
    renderAttachments();
    var status = $('fb-status');
    if (status) { status.hidden = true; status.textContent = ''; }
  }

  function setType(type) {
    currentType = (type === 'feature') ? 'feature' : 'bug';
    Array.prototype.forEach.call(document.querySelectorAll('.fb-type-btn'), function (btn) {
      var active = btn.getAttribute('data-type') === currentType;
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    var desc = $('fb-desc');
    if (desc) desc.placeholder = PLACEHOLDERS[currentType];
  }

  function addFiles(fileList) {
    Array.prototype.forEach.call(fileList || [], function (file) {
      if (!file) return;
      var isImage = /^image\//.test(file.type);
      attachments.push({
        file: file,
        name: file.name || (isImage ? 'screenshot.png' : 'file'),
        url: isImage ? URL.createObjectURL(file) : null,
      });
    });
    renderAttachments();
  }

  function renderAttachments() {
    var box = $('fb-attachments');
    if (!box) return;
    box.innerHTML = '';
    attachments.forEach(function (att, idx) {
      var chip = document.createElement('span');
      chip.className = 'fb-attachment-chip';
      if (att.url) {
        var img = document.createElement('img');
        img.src = att.url; img.alt = '';
        chip.appendChild(img);
      }
      var label = document.createElement('span');
      label.textContent = att.name;
      chip.appendChild(label);
      var rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'fb-attachment-remove';
      rm.setAttribute('aria-label', 'Remove attachment');
      rm.textContent = '×';
      rm.addEventListener('click', function () {
        attachments.splice(idx, 1);
        renderAttachments();
      });
      chip.appendChild(rm);
      box.appendChild(chip);
    });
  }

  function submitFeedback() {
    var desc = $('fb-desc');
    var status = $('fb-status');
    var submit = $('fb-submit');
    var message = ((desc && desc.value) || '').trim();
    if (status) status.hidden = false;
    if (!message) { if (status) status.textContent = 'Please enter a description.'; return; }
    if (submit) submit.disabled = true;
    if (status) status.textContent = 'Sending…';

    var form = new FormData();
    form.append('type', currentType);
    form.append('description', message);
    form.append('page_url', window.location.href);
    attachments.forEach(function (att) { form.append('files', att.file, att.name); });

    window.gpiFetch('/feedback', { method: 'POST', body: form })
      .then(function (r) { return r.json(); })
      .then(function (resp) {
        if (resp && resp.ok) {
          if (status) status.textContent = 'Thanks — sent!';
          setTimeout(function () { closeModal($('fb-modal')); resetSendForm(); }, 1200);
        } else if (status) {
          status.textContent = 'Failed: ' + ((resp && resp.error) || 'unknown');
        }
        if (submit) submit.disabled = false;
      })
      .catch(function () {
        if (status) status.textContent = 'Network error.';
        if (submit) submit.disabled = false;
      });
  }

  function statusLabel(s) {
    return { open: 'Open', done: 'Done', rejected: 'Rejected' }[s] || 'Open';
  }

  function renderMyFeedback(data) {
    var body = $('fb-view-body');
    if (!body) return;
    var items = (data && data.items) || [];
    if (!items.length) {
      body.innerHTML = '<p class="fb-view-empty">You haven\'t sent any feedback yet.</p>';
      return;
    }
    body.innerHTML = '';
    items.forEach(function (it) {
      var row = document.createElement('div');
      row.className = 'fb-view-item';
      var main = document.createElement('div');
      main.className = 'fb-view-main';
      var title = document.createElement('div');
      title.className = 'fb-view-title';
      title.textContent = it.title;
      var meta = document.createElement('div');
      meta.className = 'fb-view-meta';
      var typeLabel = it.type === 'feature' ? 'Feature request' : 'Bug';
      meta.textContent = typeLabel + ' · ' + (it.created_at || '').slice(0, 10);
      main.appendChild(title); main.appendChild(meta);
      var pill = document.createElement('span');
      pill.className = 'fb-status-pill is-' + (it.status || 'open');
      pill.textContent = statusLabel(it.status);
      row.appendChild(main); row.appendChild(pill);
      body.appendChild(row);
    });
    if (data && data.status_available === false) {
      var note = document.createElement('p');
      note.className = 'fb-view-empty';
      note.textContent = 'Status temporarily unavailable.';
      body.appendChild(note);
    }
  }

  function openView() {
    var body = $('fb-view-body');
    if (body) body.textContent = 'Loading…';
    openModal($('fb-view-modal'));
    window.gpiFetch('/api/feedback/mine')
      .then(function (r) { return r.json(); })
      .then(renderMyFeedback)
      .catch(function () {
        if (body) body.innerHTML = '<p class="fb-view-empty">Could not load your feedback.</p>';
      });
  }

  function wire() {
    var openBtn = $('fb-open');
    var viewBtn = $('fb-view-open');
    if (!openBtn && !viewBtn) return;
    if (openBtn) openBtn.addEventListener('click', function () {
      resetSendForm(); openModal($('fb-modal')); var d = $('fb-desc'); if (d) d.focus();
    });
    if (viewBtn) viewBtn.addEventListener('click', openView);

    var close = $('fb-close'), cancel = $('fb-cancel'), backdrop = $('fb-backdrop');
    [close, cancel, backdrop].forEach(function (el) {
      if (el) el.addEventListener('click', function () { closeModal($('fb-modal')); });
    });
    var vClose = $('fb-view-close'), vBackdrop = $('fb-view-backdrop');
    [vClose, vBackdrop].forEach(function (el) {
      if (el) el.addEventListener('click', function () { closeModal($('fb-view-modal')); });
    });

    Array.prototype.forEach.call(document.querySelectorAll('.fb-type-btn'), function (btn) {
      btn.addEventListener('click', function () { setType(btn.getAttribute('data-type')); });
    });

    var uploadBtn = $('fb-upload-btn'), fileInput = $('fb-file-input');
    if (uploadBtn && fileInput) {
      uploadBtn.addEventListener('click', function () { fileInput.click(); });
      fileInput.addEventListener('change', function () { addFiles(fileInput.files); fileInput.value = ''; });
    }

    var desc = $('fb-desc');
    if (desc) {
      desc.addEventListener('paste', function (event) {
        var items = (event.clipboardData && event.clipboardData.items) || [];
        var imgs = [];
        Array.prototype.forEach.call(items, function (it) {
          if (it.kind === 'file' && /^image\//.test(it.type)) {
            var f = it.getAsFile();
            if (f) imgs.push(f);
          }
        });
        if (imgs.length) { event.preventDefault(); addFiles(imgs); }
      });
    }

    var submit = $('fb-submit');
    if (submit) submit.addEventListener('click', submitFeedback);

    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      var m = $('fb-modal'), v = $('fb-view-modal');
      if (m && !m.hidden) closeModal(m);
      if (v && !v.hidden) closeModal(v);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }
})();
