(function () {
  function postJson(url, payload) {
    var fetcher = window.gpiFetch || window.fetch;
    return fetcher(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload || {}),
    }).then(function (r) { return r.json(); });
  }

  function status(row, text, isError) {
    var el = row.querySelector('.row-status');
    if (!el) return;
    row.classList.toggle('is-error', !!isError);
    el.hidden = false;
    el.textContent = text;
  }

  function busy(row, on) {
    row.querySelectorAll('button, input').forEach(function (el) {
      el.disabled = !!on;
    });
  }

  function countText(el) {
    return Math.max(0, parseInt(el.textContent || '0', 10) || 0);
  }

  function bumpPendingCount(delta) {
    document.querySelectorAll('[data-pending-count]').forEach(function (el) {
      el.textContent = countText(el) + delta;
    });
  }

  function removeResolvedRow(row) {
    var section = row.closest('.inbox-section');
    var tbody = row.parentElement;
    row.remove();
    if (tbody && !tbody.querySelector('.exception-row') && section) {
      var table = section.querySelector('table');
      var empty = document.createElement('div');
      empty.className = 'empty-row';
      empty.textContent = 'No pending time-off requests.';
      if (table) table.replaceWith(empty);
    }
  }

  function textCell(tag, text) {
    var el = document.createElement(tag);
    el.textContent = text || '';
    return el;
  }

  function dateRange(decision) {
    if (!decision) return '';
    if (decision.date_label) return decision.date_label;
    var start = decision.date_from || '';
    var end = decision.date_to || '';
    return end && end !== start ? start + ' to ' + end : start;
  }

  function titleCase(text) {
    text = String(text || '');
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : '';
  }

  function prependDecision(decision) {
    var tbody = document.querySelector('[data-recent-decisions]');
    if (!tbody || !decision) return;
    var empty = document.querySelector('[data-recent-empty]');
    if (empty) empty.remove();

    var row = document.createElement('tr');
    row.className = 'exception-row';
    row.appendChild(textCell('th', decision.person_name || 'Unknown'));

    var detail = textCell(
      'td',
      titleCase(decision.action) + ' · ' + (decision.leave_type || 'Time off') + ' ' + dateRange(decision)
    );
    if (decision.reason) {
      var reason = document.createElement('div');
      reason.className = 'muted';
      reason.textContent = decision.reason;
      detail.appendChild(reason);
    }
    row.appendChild(detail);

    var actor = textCell('td', decision.actor_name || decision.actor_upn || 'Unknown');
    if (decision.decided_label) {
      var decided = document.createElement('div');
      decided.className = 'muted';
      decided.textContent = decision.decided_label;
      actor.appendChild(decided);
    }
    row.appendChild(actor);
    tbody.insertBefore(row, tbody.firstChild);
  }

  function done(row, text) {
    busy(row, true);
    status(row, text, false);
    row.classList.add('is-resolved');
    row.style.opacity = '0.5';
    bumpPendingCount(-1);
    if (typeof window.gpiRefreshInboxSummary === 'function') {
      window.gpiRefreshInboxSummary();
    }
    setTimeout(function () {
      removeResolvedRow(row);
    }, 450);
  }

  document.addEventListener('click', function (event) {
    var btn = event.target.closest('.row-btn');
    if (!btn) return;
    var row = btn.closest('.exception-row');
    if (!row) return;
    var id = encodeURIComponent(row.dataset.requestId || '');
    if (!id) return;

    if (btn.classList.contains('js-approve')) {
      busy(row, true);
      status(row, 'Approving...', false);
      postJson('/api/exceptions/time-off/' + id + '/approve', {source: 'page'})
        .then(function (resp) {
          if (resp && resp.ok && resp.approved === false) {
            status(row, 'Moved forward; refreshing...', false);
            setTimeout(function () { window.location.reload(); }, 600);
          } else if (resp && resp.ok) {
            prependDecision(resp.decision);
            done(row, 'Approved');
          } else {
            busy(row, false);
            status(row, (resp && resp.error) || 'Approval failed.', true);
          }
        }).catch(function () {
          busy(row, false);
          status(row, 'Network error.', true);
        });
      return;
    }

    if (btn.classList.contains('js-refuse')) {
      var input = row.querySelector('.js-reason');
      if (input && input.hidden) {
        input.hidden = false;
        input.focus();
        status(row, 'Enter a reason, then Deny again.', false);
        return;
      }
      var reason = input ? input.value.trim() : '';
      if (!reason) {
        status(row, 'A reason is required to deny.', true);
        if (input) input.focus();
        return;
      }
      busy(row, true);
      status(row, 'Denying...', false);
      postJson('/api/exceptions/time-off/' + id + '/refuse', {
        reason: reason,
        source: 'page',
      }).then(function (resp) {
        if (resp && resp.ok) {
          prependDecision(resp.decision);
          done(row, 'Denied');
        } else {
          busy(row, false);
          status(row, (resp && resp.error) || 'Deny failed.', true);
        }
      }).catch(function () {
        busy(row, false);
        status(row, 'Network error.', true);
      });
    }
  });
})();
