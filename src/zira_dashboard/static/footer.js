(function () {
  var btn = document.getElementById('changelog-open');
  var modal = document.getElementById('changelog-modal');
  var backdrop = document.getElementById('changelog-backdrop');
  var closeBtn = document.getElementById('changelog-close');
  var body = document.getElementById('changelog-body');
  if (!btn || !modal) return;
  var loaded = false;
  var latestDate = null;

  // Check whether there's a newer entry than the user has seen.
  fetch('/changelog/latest')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      latestDate = data && data.latest_date;
      if (!latestDate) return;
      var seen = '';
      try { seen = localStorage.getItem('changelog_seen') || ''; } catch (e) {}
      if (latestDate > seen) btn.classList.add('has-new');
    })
    .catch(function () { /* offline / first-load — silently skip */ });

  function open(e) {
    if (e) e.preventDefault();
    modal.hidden = false;
    document.documentElement.style.overflow = 'hidden';
    var seenAtOpen = '';
    try { seenAtOpen = localStorage.getItem('changelog_seen') || ''; } catch (e2) {}
    if (!loaded) {
      fetch('/changelog')
        .then(function (r) { return r.text(); })
        .then(function (html) {
          body.innerHTML = html;
          loaded = true;
          // Highlight any deployment section newer than seenAtOpen — flash 3s.
          body.querySelectorAll('.changelog-deploy[data-when]').forEach(function (sec) {
            if (sec.dataset.when > seenAtOpen) sec.classList.add('changelog-new');
          });
        })
        .catch(function () { body.innerHTML = '<p>Could not load changelog.</p>'; });
    } else {
      // Modal opened a second time in the same page-load — re-flash anything
      // still considered new (in case localStorage shifted).
      body.querySelectorAll('.changelog-deploy[data-when]').forEach(function (sec) {
        sec.classList.remove('changelog-new');
        if (sec.dataset.when > seenAtOpen) {
          // Re-trigger animation by reflowing.
          void sec.offsetWidth;
          sec.classList.add('changelog-new');
        }
      });
    }
    // Mark as read AFTER capturing seenAtOpen so the highlight uses pre-open value.
    if (latestDate) {
      try { localStorage.setItem('changelog_seen', latestDate); } catch (e3) {}
      btn.classList.remove('has-new');
    }
  }
  function close() {
    modal.hidden = true;
    document.documentElement.style.overflow = '';
  }
  btn.addEventListener('click', open);
  backdrop.addEventListener('click', close);
  closeBtn.addEventListener('click', close);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !modal.hidden) close();
  });
})();

// Global "Assignments to Do" badge + modal — present on every page.
(function () {
  var navBadge = null;
  var modal = null;
  var data = null;
  var ATD_ENDPOINT = '/api/assignments-todo';

  function settingsLink() {
    return document.querySelector('header nav a[href="/settings"]')
        || document.querySelector('header.app nav a[href="/settings"]');
  }

  function refreshCount() {
    fetch(ATD_ENDPOINT).then(function (r) { return r.json(); }).then(function (d) {
      data = d;
      injectOrUpdateBadge();
    }).catch(function () {});
  }

  function injectOrUpdateBadge() {
    if (!data || !data.count) {
      if (navBadge) { navBadge.remove(); navBadge = null; }
      return;
    }
    var anchor = settingsLink();
    if (!anchor) return;
    if (!navBadge) {
      navBadge = document.createElement('a');
      navBadge.href = '#';
      navBadge.className = 'assign-todo-nav-badge';
      navBadge.title = 'Production happened at unscheduled work centers — click to assign';
      navBadge.addEventListener('click', function (e) { e.preventDefault(); openModal(); });
      // Always sits to the RIGHT of the Settings link.
      anchor.parentNode.insertBefore(navBadge, anchor.nextSibling);
    }
    navBadge.innerHTML = '⚠ <span class="cnt">' + data.count + '</span> to Assign';
  }

  function openModal() {
    closeModal();
    modal = document.createElement('div');
    modal.className = 'atd-modal';
    modal.innerHTML = ''
      + '<div class="atd-backdrop"></div>'
      + '<div class="atd-card" role="dialog" aria-modal="true" aria-label="Assignments to do">'
      + '  <div class="atd-head"><h3>Assignments to Do</h3>'
      + '    <button type="button" class="atd-close" aria-label="Close">×</button></div>'
      + '  <div class="atd-body">Loading…</div>'
      + '</div>';
    document.body.appendChild(modal);
    document.documentElement.style.overflow = 'hidden';
    modal.querySelector('.atd-backdrop').addEventListener('click', closeModal);
    modal.querySelector('.atd-close').addEventListener('click', closeModal);
    document.addEventListener('keydown', escClose);
    fetch(ATD_ENDPOINT).then(function (r) { return r.json(); }).then(renderModal);
  }

  function closeModal() {
    if (modal) { modal.remove(); modal = null; }
    document.documentElement.style.overflow = '';
    document.removeEventListener('keydown', escClose);
  }

  function escClose(e) { if (e.key === 'Escape') closeModal(); }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }

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

  function renderModal(d) {
    data = d;
    if (!modal) return;
    var body = modal.querySelector('.atd-body');
    var html = '';
    if (d.items && d.items.length) {
      html += '<p class="atd-help">These work centers produced units today but had no one scheduled. Pick the person who actually worked there. Any active employee can be picked, even if they\'re scheduled elsewhere.</p>';
      html += '<ul class="atd-list">';
      d.items.forEach(function (item) {
        html += '<li class="atd-item" data-wc="' + escapeHtml(item.wc_name) + '" data-day="' + escapeHtml(d.today) + '" data-start="' + escapeHtml(item.first_iso) + '" data-end="' + escapeHtml(item.last_iso) + '">';
        html += '<div class="atd-item-head"><strong>' + escapeHtml(item.wc_name) + '</strong>';
        html += ' <span class="atd-meta">' + item.units + ' pallets · ' + escapeHtml(item.first_label) + '–' + escapeHtml(item.last_label) + '</span></div>';
        html += '<div class="atd-pick"><select class="atd-person"><option value="">— pick person —</option>';
        d.people.forEach(function (n) {
          html += '<option value="' + escapeHtml(n) + '">' + escapeHtml(n) + '</option>';
        });
        html += '</select> <button type="button" class="atd-save">Save</button>';
        html += ' <button type="button" class="atd-testing-btn">Testing</button>';
        html += '<span class="atd-status" hidden></span></div>';
        // Hidden testing panel — start/end prefilled with the sensed window.
        html += '<div class="atd-testing-panel" hidden>';
        html += '<label>Testing from <input type="time" class="atd-test-start" value="' + to24h(item.first_label) + '"></label>';
        html += '<label>to <input type="time" class="atd-test-end" value="' + to24h(item.last_label) + '"></label>';
        html += '<div class="atd-test-remainder"><label>Who worked after testing? '
              + '<select class="atd-test-person"><option value="">— no one (all testing) —</option>';
        d.people.forEach(function (n) {
          html += '<option value="' + escapeHtml(n) + '">' + escapeHtml(n) + '</option>';
        });
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
    wireBodyHandlers();
  }

  function wireBodyHandlers() {
    if (!modal) return;
    modal.querySelectorAll('.atd-save').forEach(function (b) {
      b.addEventListener('click', function () {
        var li = b.closest('.atd-item');
        var sel = li.querySelector('.atd-person');
        var status = li.querySelector('.atd-status');
        var person = sel.value;
        if (!person) { status.hidden = false; status.textContent = 'Pick a person.'; return; }
        b.disabled = true; sel.disabled = true; status.hidden = true;
        fetch('/api/staffing/attribute', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            day: li.dataset.day, wc_name: li.dataset.wc, person_name: person,
            start_utc: li.dataset.start,
          }),
        }).then(function (r) { return r.json(); }).then(function (resp) {
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
    modal.querySelectorAll('.atd-delete').forEach(function (b) {
      b.addEventListener('click', function () {
        var li = b.closest('.atd-saved-item');
        if (!li || !confirm('Remove this attribution?')) return;
        b.disabled = true;
        fetch('/api/staffing/attribute/' + encodeURIComponent(li.dataset.attributionId), {method: 'DELETE'})
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
    modal.querySelectorAll('.atd-testing-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        var li = b.closest('.atd-item');
        var panel = li.querySelector('.atd-testing-panel');
        panel.hidden = !panel.hidden;
      });
    });
    modal.querySelectorAll('.atd-test-confirm').forEach(function (b) {
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
        fetch('/api/staffing/attribute-with-testing', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            day: li.dataset.day, wc_name: li.dataset.wc,
            testing_start_utc: localTimeToIso(li.dataset.day, startV),
            testing_end_utc: localTimeToIso(li.dataset.day, endV),
            sensed_end_utc: li.dataset.end,
            remainder_person: person || null,
          }),
        }).then(function (r) { return r.json(); }).then(function (resp) {
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
      fetch('/api/staffing/transfer/undo', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({closed_id: t.closed_id || null, new_id: t.new_id}),
      }).then(function (r) { return r.json(); }).then(function () {
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

  // Kick it off after a short delay so the rest of the page renders first.
  setTimeout(refreshCount, 300);
})();

// Global "Late/Absence Report" badge + modal — present on every page.
(function () {
  var navBadge = null;
  var modal = null;
  var data = null;
  var LATE_ENDPOINT = '/api/late-report';

  function settingsLink() {
    return document.querySelector('header nav a[href="/settings"]')
        || document.querySelector('header.app nav a[href="/settings"]');
  }

  function refreshCount() {
    fetch(LATE_ENDPOINT).then(function (r) { return r.json(); }).then(function (d) {
      data = d;
      injectOrUpdateBadge();
    }).catch(function () {});
  }

  function injectOrUpdateBadge() {
    if (!data || (!data.count && !(data.snoozed && data.snoozed.length))) {
      if (navBadge) { navBadge.remove(); navBadge = null; }
      return;
    }
    var anchor = settingsLink();
    if (!anchor) return;
    if (!navBadge) {
      navBadge = document.createElement('a');
      navBadge.href = '#';
      navBadge.className = 'late-nav-badge';
      navBadge.title = 'Scheduled people who haven\'t clocked in yet — click to manage';
      navBadge.addEventListener('click', function (e) { e.preventDefault(); openModal(); });
      // Sit to the right of any existing assignments-todo badge, or directly
      // after Settings if that badge isn't present.
      var prev = anchor.nextSibling;
      while (prev && prev.classList && prev.classList.contains('assign-todo-nav-badge')) {
        prev = prev.nextSibling;
      }
      anchor.parentNode.insertBefore(navBadge, prev);
    }
    if (data.count) {
      navBadge.innerHTML = '🚨 <span class="cnt">' + data.count + '</span> Late/Absence';
      navBadge.style.display = '';
    } else {
      // Only snoozed people remain — render in a lower-key style but still visible.
      navBadge.innerHTML = '⏱ <span class="cnt">' + data.snoozed.length + '</span> Snoozed';
      navBadge.style.animation = 'none';
    }
  }

  function openModal() {
    closeModal();
    modal = document.createElement('div');
    modal.className = 'late-modal';
    modal.innerHTML = ''
      + '<div class="late-backdrop"></div>'
      + '<div class="late-card" role="dialog" aria-modal="true" aria-label="Late and absence report">'
      + '  <div class="late-head"><h3>Late / Absence Report</h3>'
      + '    <button type="button" class="late-close" aria-label="Close">×</button></div>'
      + '  <div class="late-body">Loading…</div>'
      + '</div>';
    document.body.appendChild(modal);
    document.documentElement.style.overflow = 'hidden';
    modal.querySelector('.late-backdrop').addEventListener('click', closeModal);
    modal.querySelector('.late-close').addEventListener('click', closeModal);
    document.addEventListener('keydown', escClose);
    fetch(LATE_ENDPOINT).then(function (r) { return r.json(); }).then(renderModal);
  }

  function closeModal() {
    if (modal) { modal.remove(); modal = null; }
    document.documentElement.style.overflow = '';
    document.removeEventListener('keydown', escClose);
  }

  function escClose(e) { if (e.key === 'Escape') closeModal(); }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }

  function renderModal(d) {
    data = d;
    if (!modal) return;
    var body = modal.querySelector('.late-body');
    var html = '';

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
        actionsHtml = ''
          + '<div class="late-reason-row">'
          + '  <button type="button" class="late-quickpick" data-pick="Sick">Sick</button>'
          + '  <button type="button" class="late-quickpick" data-pick="Car issues">Car issues</button>'
          + '  <button type="button" class="late-quickpick" data-pick="Overslept">Overslept</button>'
          + '  <button type="button" class="late-quickpick" data-pick="">Other</button>'
          + '  <input type="text" class="late-reason-input" placeholder="Reason required">'
          + '  <button type="button" class="late-save-late" disabled>Save</button>'
          + '</div>';
      } else {
        actionsHtml = ''
          + '<span class="late-item-actions">'
          + '  <button type="button" class="late-snooze">Snooze 30 min</button>'
          + '  <button type="button" class="late-declare">Declare Absent</button>'
          + '</span>'
          + '<div class="late-reason-row late-declare-reason" hidden>'
          + '  <button type="button" class="late-quickpick" data-pick="Sick">Sick</button>'
          + '  <button type="button" class="late-quickpick" data-pick="Car issues">Car issues</button>'
          + '  <button type="button" class="late-quickpick" data-pick="Overslept">Overslept</button>'
          + '  <button type="button" class="late-quickpick" data-pick="">Other</button>'
          + '  <input type="text" class="late-reason-input" placeholder="Reason required">'
          + '  <button type="button" class="late-save-absent" disabled>Save</button>'
          + '</div>';
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
    wireBodyHandlers();
  }

  function wireBodyHandlers() {
    if (!modal) return;
    var body = modal.querySelector('.late-body');

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
        doAction(li, '/api/late-report/snooze', {
          emp_id: li.dataset.empId,
          name: li.dataset.name,
          minutes: 30,
        }, { alwaysClose: true });
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
        doAction(li, '/api/late-report/declare-absent', {
          emp_id: li.dataset.empId,
          name: li.dataset.name,
          reason: input ? input.value : '',
        });
      });
    });

    // Save (Late Arrival reason).
    body.querySelectorAll('.late-save-late').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var li = btn.closest('.late-item');
        var input = li.querySelector('.late-reason-input');
        doAction(li, '/api/late-report/save-late-arrival', {
          emp_id: li.dataset.empId,
          name: li.dataset.name,
          reason: input ? input.value : '',
        });
      });
    });
  }

  function doAction(li, url, payload, opts) {
    opts = opts || {};
    var status = li.querySelector('.late-status');
    if (status) { status.hidden = false; status.textContent = 'Saving…'; }
    fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    }).then(function (r) { return r.json(); }).then(function (resp) {
      if (resp && resp.ok) {
        // Re-pull the report so the saved row drops out. If nothing's
        // actionable left OR the caller set `alwaysClose: true` (Snooze),
        // close the modal — otherwise re-render so the user can keep
        // working without "Saving…" lingering on the saved row.
        fetch(LATE_ENDPOINT).then(function (r) { return r.json(); }).then(function (d) {
          data = d;
          injectOrUpdateBadge();
          var anyActionable = (d.scheduled_late && d.scheduled_late.length)
              || (d.unscheduled_late && d.unscheduled_late.length)
              || (d.needs_reason && d.needs_reason.length);
          if (opts.alwaysClose || !anyActionable) {
            closeModal();
          } else if (modal) {
            renderModal(d);
          }
        });
      } else {
        if (status) { status.textContent = 'Failed: ' + ((resp && resp.error) || 'unknown'); }
      }
    }).catch(function () {
      if (status) { status.textContent = 'Network error.'; }
    });
  }

  setTimeout(refreshCount, 400);
  // Re-poll every 60 seconds — keeps the badge fresh as people arrive late
  // or as snoozes expire, without a full page reload.
  setInterval(refreshCount, 60000);
})();
